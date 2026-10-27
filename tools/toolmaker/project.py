"""A tool being written: a folder of `tool.json`, `tool.py` and `tests.py`.

Everything here runs the tool the way the device will: through the real
registry and in the real child process (`firmware/assistant/extensions`), with
the internet replaced by answers the test wrote down. So a tool that passes
here is refused or accepted by a device for the same reasons.
"""

import json
import os
import re
import time
import traceback

from firmware.assistant import extensions, tools
from firmware.assistant.extensions import host, package
from firmware.assistant.tools import registry as registry_mod

FILES = ("tool.json", "tool.py", "tests.py")
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "template")
_ID = re.compile(r"^[a-z0-9]+(?:[-.][a-z0-9]+)*$")
NOW = "2026-10-02T15:10"
TZ = "Europe/Ljubljana"


class ProjectError(Exception):
    pass


def folder_of(root, project_id):
    if not isinstance(project_id, str) or not _ID.match(project_id) \
            or len(project_id) > 40:
        raise ProjectError("an id may hold a-z, 0-9, dots and dashes")
    # The pattern already rules out "/" and "..". Checked again on the real
    # path, so no id can ever name a folder outside the projects folder.
    base = os.path.realpath(root)
    folder = os.path.realpath(os.path.join(base, project_id))
    if os.path.dirname(folder) != base:
        raise ProjectError("an id may hold a-z, 0-9, dots and dashes")
    return folder


def listing(root):
    found = []
    if not os.path.isdir(root):
        return found
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name, "tool.json")
        if not _ID.match(name) or not os.path.isfile(path):
            continue
        title = name
        try:
            with open(path, encoding="utf-8") as fh:
                title = str(json.load(fh).get("name") or name)
        except (OSError, ValueError):
            pass
        found.append({"id": name, "name": title})
    return found


def create(root, project_id, name):
    """A new project from the template: a working weather tool to change."""
    folder = folder_of(root, project_id)
    if os.path.exists(folder):
        raise ProjectError("there is already a tool called %s" % project_id)
    name = " ".join(str(name or "").split())[:40] or project_id
    os.makedirs(folder)
    for filename in FILES:
        with open(os.path.join(TEMPLATE, filename), encoding="utf-8") as fh:
            text = fh.read()
        if filename == "tool.json":
            manifest = json.loads(text)
            manifest["id"], manifest["name"] = project_id, name
            text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        write(root, project_id, filename, text)
    return folder


def read(root, project_id):
    folder = folder_of(root, project_id)
    files = {}
    for filename in FILES:
        try:
            with open(os.path.join(folder, filename), encoding="utf-8") as fh:
                files[filename] = fh.read()
        except OSError:
            files[filename] = ""
    return files


def write(root, project_id, filename, text):
    if filename not in FILES:
        raise ProjectError("a tool has %s" % ", ".join(FILES))
    folder = folder_of(root, project_id)
    if not os.path.isdir(folder):
        raise ProjectError("there is no tool called %s" % project_id)
    tmp = os.path.join(folder, filename + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, os.path.join(folder, filename))


def _package(root, project_id):
    """The project as a device would read it, or ProjectError saying what to
    fix. Goes through `pack`, so the export and the checks cannot disagree."""
    files = read(root, project_id)
    try:
        manifest = json.loads(files["tool.json"])
    except ValueError as exc:
        raise ProjectError("tool.json is not valid JSON: %s" % exc)
    members = {"tool.py": files["tool.py"].encode("utf-8")}
    if files["tests.py"].strip():
        members["tests.py"] = files["tests.py"].encode("utf-8")
    try:
        data = package.pack(manifest, members)
        return package.read(data, tools.BUILT_IN_NAMES), data
    except package.PackageError as exc:
        raise ProjectError(str(exc))


def export(root, project_id):
    """(file name, bytes) of the `.trsttool`."""
    pkg, data = _package(root, project_id)
    return "%s-%s%s" % (pkg.id, pkg.version, package.EXTENSION), data


# ---- running ----

class FakeInternet:
    """Answers written down by a test, keyed by address without the query.
    `{"https://host/path": {"status": 200, "json": {...}}}`; `"text"` in place
    of `"json"` for an answer that is not JSON. An address with no answer is a
    404, and is listed so the test's author can see what was asked."""

    def __init__(self, answers=None, down=False):
        self.answers = dict(answers or {})
        self.down = down
        self.requests = []

    def __call__(self, tool_id, method, url, headers, body):
        bare = url.split("?", 1)[0]
        self.requests.append({"method": method, "url": url})
        if self.down:
            raise OSError("no internet")
        answer = self.answers.get(url, self.answers.get(bare))
        if answer is None:
            self.requests[-1]["unanswered"] = True
            return {"status": 404, "text": ""}
        text = (json.dumps(answer["json"], ensure_ascii=False)
                if "json" in answer else str(answer.get("text", "")))
        return {"status": int(answer.get("status", 200)), "text": text}


class _Memory:
    """Storage for one run, so a test starts from what it says it does."""

    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key):
        return (key in self.data), self.data.get(key)

    def set(self, key, value):
        candidate = dict(self.data, **{key: value})
        if len(json.dumps(candidate).encode("utf-8")) > host.STORAGE_BYTES:
            raise host.Denied("the tool's storage is full (%d KB)"
                              % (host.STORAGE_BYTES // 1024))
        self.data = candidate

    def delete(self, key):
        self.data.pop(key, None)


class _Ctx:
    turn_id = "toolmaker"

    def __init__(self, lang, now):
        self.lang = lang
        self._now = now

    def now_local(self):
        return self._now

    def tz(self):
        return TZ


class Outcome:
    """What one call did. `data` is what the tool returned; `error` is what
    the model would be told if it failed; `reads` is the exact text the model
    gets either way."""

    def __init__(self, result, internet, storage, output, seconds):
        self.ok = result.ok
        self.data = result.payload.get("data", {}) if result.ok else {}
        self.error = "" if result.ok else str(result.payload.get("error", ""))
        self.reads = result.content
        self.requests = internet.requests
        self.storage = storage.data
        self.output = output
        self.seconds = seconds

    def as_dict(self):
        return {"ok": self.ok, "data": self.data, "error": self.error,
                "reads": self.reads, "requests": self.requests,
                "storage": self.storage, "output": self.output,
                "seconds": round(self.seconds, 2)}


def call(root, project_id, tool_name, args, internet=None, storage=None,
         lang="en", now=NOW, down=False, live=False):
    """Run one tool of the project once, as the device would."""
    pkg, _ = _package(root, project_id)
    folder = folder_of(root, project_id)
    fake = FakeInternet(internet, down)
    memory = _Memory(storage)
    printed = []

    if live:
        def fetcher(tool_id, method, url, headers, body):
            fake.requests.append({"method": method, "url": url, "live": True})
            return host.fetch(tool_id, method, url, headers, body)
    else:
        fetcher = fake

    def run_one(tool):
        def fn(ctx, **given):
            given = {k: v for k, v in given.items() if v is not None}
            payload, problem = host.run(
                folder, tool["function"], given,
                host.Call(pkg, memory if pkg.storage else None, fetcher),
                lang=ctx.lang, now=ctx.now_local(), tz=ctx.tz(),
                on_output=printed.append)
            if problem:
                raise registry_mod.ToolError(problem)
            return payload
        fn.spec = registry_mod.Spec(
            tool["name"], tool["description"], dict(tool["params"]),
            final=False, destructive=False, open_world=True, preview=None,
            fn=fn)
        return fn

    registry = tools.build([run_one(tool) for tool in pkg.tools])
    began = time.monotonic()
    result = registry.run(tool_name, dict(args or {}), _Ctx(lang, now))
    took = time.monotonic() - began
    time.sleep(0.05)                # let the child's prints arrive
    return Outcome(result, fake, memory, "".join(printed), took)


def summary(root, project_id):
    """What the page shows beside the editor: is it a valid tool, and what
    would a device and the model be told about it."""
    try:
        pkg, data = _package(root, project_id)
    except ProjectError as exc:
        return {"valid": False, "problem": str(exc)}
    built = tools.build()
    own = registry_mod.Registry(extensions.functions(pkg, ""))
    return {
        "valid": True, "id": pkg.id, "name": pkg.name,
        "version": pkg.version, "bytes": len(data),
        "hosts": list(pkg.hosts), "storage": pkg.storage,
        "tools": [{"name": t["name"], "description": t["description"],
                   "params": t["params"]} for t in pkg.tools],
        # The same rough count the firmware's own budget test uses.
        "tokens": len(json.dumps(own.schemas("plain"))) // 4,
        "built_in_tokens": len(json.dumps(built.schemas("plain"))) // 4,
    }


def _example_args(tool):
    """Arguments that pass the registry's checks, for the standard checks."""
    args = {}
    for name, p in tool["params"].items():
        if p.get("optional"):
            continue
        if "enum" in p:
            args[name] = p["enum"][0]
        elif p["type"] == "string":
            args[name] = "test"
        elif p["type"] == "boolean":
            args[name] = True
        else:
            args[name] = p.get("min", 1)
    return args


def standard_checks(root, project_id, examples=None):
    """The checks every tool gets, whatever its own tests say: the things a
    device will do to it sooner or later. `examples` maps a tool's name to
    arguments that work (taken from its tests when there are any)."""
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok),
                       "detail": "" if ok else detail})

    try:
        pkg, data = _package(root, project_id)
    except ProjectError as exc:
        add("the file is one a device accepts", False, str(exc))
        return checks
    add("the file is one a device accepts", True)
    for tool in pkg.tools:
        name = tool["name"]
        args = (examples or {}).get(name) or _example_args(tool)
        down = call(root, project_id, name, args, down=True)
        uses_net = bool(down.requests)
        if uses_net:
            add("%s: with no internet it fails in words, not with a fault"
                % name, not down.ok and "fault" not in down.error
                and "longer than" not in down.error,
                down.error or "it reported success with no internet")
            requests = [r["url"].split("?", 1)[0] for r in call(
                root, project_id, name, args).requests] or [
                r["url"].split("?", 1)[0] for r in down.requests]
            broken = call(root, project_id, name, args, internet={
                url: {"status": 500, "text": "<html>Server Error</html>"}
                for url in requests})
            add("%s: an error page from the server does not break it" % name,
                "fault" not in broken.error, broken.error)
            garbage = call(root, project_id, name, args, internet={
                url: {"status": 200, "text": "not json at all"}
                for url in requests})
            add("%s: an answer that is not JSON does not break it" % name,
                "fault" not in garbage.error, garbage.error)
            empty = call(root, project_id, name, args, internet={
                url: {"status": 200, "json": {}} for url in requests})
            add("%s: an answer with nothing in it does not break it" % name,
                "fault" not in empty.error, empty.error)
        else:
            add("%s: it runs" % name, "fault" not in down.error, down.error)
        add("%s: it answers within two seconds when the server is quick"
            % name, down.seconds < 2.0, "%.1f s" % down.seconds)
        wrong = call(root, project_id, name, {k: [] for k in args} or
                     {"unknown": 1})
        if args:
            add("%s: arguments of the wrong kind are refused before it runs"
                % name, not wrong.ok and not wrong.requests, wrong.reads)
    return checks


def run_tests(root, project_id):
    """Run `tests.py`: every function called `test_...`, each given `run`.

        def test_it_says_the_temperature(run):
            r = run("get_weather", city="Maribor", internet={...})
            assert r.ok and r.data["degrees"] == 12

    `tests.py` runs in this process: it is the author's own file on their own
    machine, and never part of what a device installs. The tool it calls
    still runs in the child process, guards and all."""
    files = read(root, project_id)
    results, examples = [], {}

    def run(tool_name, internet=None, storage=None, lang="en", now=NOW,
            down=False, **args):
        outcome = call(root, project_id, tool_name, args, internet=internet,
                       storage=storage, lang=lang, now=now, down=down)
        if outcome.ok:
            examples.setdefault(tool_name, args)
        return outcome

    space = {"__name__": "tests"}
    try:
        _package(root, project_id)
        exec(compile(files["tests.py"], "tests.py", "exec"), space)
    except ProjectError as exc:
        return {"tests": [{"name": "the tool is valid", "ok": False,
                           "detail": str(exc)}], "examples": {}}
    except Exception:
        return {"tests": [{"name": "tests.py loads", "ok": False,
                           "detail": traceback.format_exc(limit=3)}],
                "examples": {}}
    for name, fn in space.items():
        if not name.startswith("test_") or not callable(fn):
            continue
        label = name[5:].replace("_", " ")
        try:
            fn(run)
            results.append({"name": label, "ok": True, "detail": ""})
        except AssertionError as exc:
            frame = traceback.extract_tb(exc.__traceback__)[-1]
            results.append({"name": label, "ok": False, "detail":
                            "line %d: %s%s" % (
                                frame.lineno, frame.line or "assert",
                                (" (%s)" % exc) if str(exc) else "")})
        except Exception:
            results.append({"name": label, "ok": False,
                            "detail": traceback.format_exc(limit=2)})
    return {"tests": results, "examples": examples}

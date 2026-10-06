"""The tools an owner has installed, on disk, and the page's commands for
them (plan 10, step 3).

Under `<data root>/tools/`:

    installed/<id>/tool.json, tool.py    what `package.unpack` keeps
    data/<id>.json                       the tool's own storage, if it has any
    state.json                           which ones are switched on

Nothing here runs a tool. `preview` and `install` read a file's description
and check it (`package.read`); the code is only ever started by a call, in its
own process (`host.run`).

A folder that no longer loads (edited on the card, or written by an older
firmware) is listed with the reason and is never switched on, so one bad tool
cannot take the assistant down with it.

The commands change which tools the model is offered, so like every other
change they run on the main thread. The worker reads `worker.registry` once
per turn, and a new registry is put there whole.
"""

import logging
import os
import shutil

from ... import events as ev
from ...storage import atomic
from .. import tools
from . import MAX_ENABLED, WARN_FROM, functions, host, package

log = logging.getLogger("trstenko.tools")


class StoreError(Exception):
    def __init__(self, message, kind=ev.INVALID):
        super().__init__(message)
        self.kind = kind


class ToolStore:
    def __init__(self, root, fetcher=host.fetch):
        self.root = root
        self.fetcher = fetcher
        self._installed = os.path.join(root, "installed")
        self._data = os.path.join(root, "data")
        self._state = os.path.join(root, "state.json")
        self._enabled = self._read_state()

    # ---- on disk ----

    def _read_state(self):
        import json

        try:
            with open(self._state, "r", encoding="utf-8") as fh:
                names = json.load(fh).get("enabled") or []
        except (OSError, ValueError, AttributeError):
            return []
        return [n for n in names if isinstance(n, str)]

    def _save_state(self):
        os.makedirs(self.root, exist_ok=True)
        atomic.write_json(self._state, {"enabled": list(self._enabled)})

    def _folder(self, tool_id):
        return os.path.join(self._installed, tool_id)

    def _ids(self):
        try:
            return sorted(n for n in os.listdir(self._installed)
                          if os.path.isdir(self._folder(n)))
        except OSError:
            return []

    def _load(self, tool_id):
        return package.load(self._folder(tool_id), tools.BUILT_IN_NAMES)

    def _packages(self):
        """(id, package or None, why it would not load), in a fixed order."""
        out = []
        for tool_id in self._ids():
            try:
                out.append((tool_id, self._load(tool_id), ""))
            except package.PackageError as exc:
                out.append((tool_id, None, str(exc)))
        return out

    def _need(self, tool_id):
        if not isinstance(tool_id, str) or tool_id not in self._ids():
            raise StoreError("There is no tool called %s."
                             % str(tool_id)[:40], ev.NOT_FOUND)
        return tool_id

    # ---- reading ----

    def enabled_functions(self, without=None):
        """How many tool functions are switched on, across every file."""
        return sum(len(pkg.tools) for tool_id, pkg, _ in self._packages()
                   if pkg is not None and tool_id in self._enabled
                   and tool_id != without)

    def functions(self):
        """The registry functions of every tool that is switched on."""
        out = []
        for tool_id, pkg, _ in self._packages():
            if pkg is not None and tool_id in self._enabled:
                out.extend(functions(pkg, self._folder(tool_id), self._data,
                                     self.fetcher))
        return out

    def describe(self, pkg):
        manifest = pkg.manifest
        return {"id": pkg.id, "name": pkg.name, "version": pkg.version,
                "author": manifest.get("author", ""),
                "about": manifest.get("about", ""),
                "hosts": list(pkg.hosts), "storage": pkg.storage,
                "tools": [{"name": t["name"],
                           "description": t["description"]}
                          for t in pkg.tools]}

    def listing(self):
        installed = []
        for tool_id, pkg, why in self._packages():
            if pkg is None:
                installed.append({"id": tool_id, "name": tool_id,
                                  "error": why, "enabled": False})
                continue
            entry = self.describe(pkg)
            entry["enabled"] = tool_id in self._enabled
            installed.append(entry)
        installed.sort(key=lambda e: (e["name"].lower(), e["id"]))
        return {"built_in": list(tools.BUILT_IN_NAMES),
                "installed": installed,
                "enabled_functions": self.enabled_functions(),
                "max": MAX_ENABLED, "warn_from": WARN_FROM,
                # The separate user with no network needs the board (step 4).
                # Until this is true the page says a tool is code you trust.
                "sandboxed": False}

    def exported(self):
        """For the privacy export: what is installed and what each has kept."""
        out = []
        for entry in self.listing()["installed"]:
            row = {k: entry.get(k) for k in ("id", "name", "version",
                                             "author", "enabled")}
            if entry.get("storage"):
                row["stored"] = host.Storage(os.path.join(
                    self._data, entry["id"] + ".json"))._read()
            out.append(row)
        return out

    # ---- changing ----

    def _read(self, data):
        if not isinstance(data, (bytes, bytearray)):
            raise StoreError("Send the .trsttool file.")
        try:
            pkg = package.read(bytes(data), tools.BUILT_IN_NAMES)
        except package.PackageError as exc:
            raise StoreError("This tool cannot be installed: %s." % exc)
        names = {t["name"] for t in pkg.tools}
        for tool_id, other, _ in self._packages():
            if other is None or tool_id == pkg.id:
                continue
            taken = names & {t["name"] for t in other.tools}
            if taken:
                raise StoreError(
                    "This tool cannot be installed: %s already has a "
                    "function called %s." % (other.name, sorted(taken)[0]),
                    ev.CONFLICT)
        return pkg

    def _old_version(self, tool_id):
        if tool_id not in self._ids():
            return None
        try:
            return self._load(tool_id).version
        except package.PackageError:
            return ""

    def preview(self, data):
        """What a file is, for the owner to agree to. Nothing is kept."""
        pkg = self._read(data)
        entry = self.describe(pkg)
        after = self.enabled_functions(without=pkg.id) + len(pkg.tools)
        entry["replaces"] = self._old_version(pkg.id)
        entry["will_enable"] = after <= MAX_ENABLED and (
            entry["replaces"] is None or pkg.id in self._enabled)
        entry["enabled_functions_after"] = (
            after if entry["will_enable"] else self.enabled_functions(
                without=pkg.id))
        entry["max"], entry["warn_from"] = MAX_ENABLED, WARN_FROM
        return entry

    def install(self, data):
        """Keep the file. A new tool is switched on if there is room; a newer
        version of an installed one keeps the switch it had."""
        pkg = self._read(data)
        new = pkg.id not in self._ids()
        room = (self.enabled_functions(without=pkg.id) + len(pkg.tools)
                <= MAX_ENABLED)
        package.unpack(pkg, self._folder(pkg.id))
        on = room and (new or pkg.id in self._enabled)
        self._enabled = [n for n in self._enabled if n != pkg.id]
        if on:
            self._enabled.append(pkg.id)
        self._save_state()
        entry = self.describe(pkg)
        entry["enabled"] = on
        return entry

    def set_enabled(self, tool_id, on):
        tool_id = self._need(tool_id)
        if not on:
            self._enabled = [n for n in self._enabled if n != tool_id]
            self._save_state()
            return
        try:
            pkg = self._load(tool_id)
        except package.PackageError as exc:
            raise StoreError("This tool can no longer be loaded: %s." % exc)
        if tool_id in self._enabled:
            return
        if self.enabled_functions() + len(pkg.tools) > MAX_ENABLED:
            raise StoreError("%d tool functions are already switched on. "
                             "Switch one off first." % self.enabled_functions(),
                             ev.CONFLICT)
        self._enabled.append(tool_id)
        self._save_state()

    def remove(self, tool_id):
        """The tool and whatever it kept."""
        tool_id = self._need(tool_id)
        self._enabled = [n for n in self._enabled if n != tool_id]
        self._save_state()
        shutil.rmtree(self._folder(tool_id), ignore_errors=True)
        try:
            os.remove(os.path.join(self._data, tool_id + ".json"))
        except FileNotFoundError:
            pass


def default_root(data_root):
    return os.path.join(data_root, "tools")


def wipe(data_root):
    """Factory reset: installed tools are the owner's, and so is what they
    kept."""
    shutil.rmtree(default_root(data_root), ignore_errors=True)


def attach(device, assistant, store):
    """Register the page's commands for `store` on `device`."""

    def rebuild():
        try:
            registry = tools.build(store.functions())
        except Exception:
            # Two tools with one name, say. The built-in ones still work.
            log.exception("the installed tools could not be loaded")
            registry = tools.build()
        if assistant.worker is not None:
            assistant.worker.registry = registry
        if assistant.session is not None:
            # The conversation so far may mention a tool that is now gone.
            assistant.session.reset()

    def guarded(fn):
        def run(args):
            if not isinstance(args, dict):
                raise ev.CommandError("expected an object", ev.INVALID)
            try:
                return fn(args)
            except StoreError as exc:
                raise ev.CommandError(str(exc), exc.kind)
            except OSError as exc:
                log.exception("the tools folder could not be written")
                raise ev.CommandError("The tool could not be saved: %s."
                                      % exc.strerror, ev.UNAVAILABLE)
        return run

    def install(args):
        entry = store.install(args.get("data"))
        rebuild()
        return dict(store.listing(), tool=entry)

    def enable(args):
        store.set_enabled(args.get("id"), bool(args.get("on")))
        rebuild()
        return store.listing()

    def remove(args):
        store.remove(args.get("id"))
        rebuild()
        return store.listing()

    device.command("tools.list", lambda args: store.listing())
    device.command("tools.preview",
                   guarded(lambda args: store.preview(args.get("data"))))
    device.command("tools.install", guarded(install))
    device.command("tools.enable", guarded(enable))
    device.command("tools.remove", guarded(remove))

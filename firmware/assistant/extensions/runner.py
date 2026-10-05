"""The process a third-party tool runs in. Started fresh for every call:

    python -I -B runner.py <tool dir>

It is a file on its own: it imports nothing from the firmware, and the firmware
never imports the tool. Everything the tool wants from the outside it asks its
parent for, one JSON object per line on stdout, and reads the answer from
stdin. So the list of hosts a tool may reach is enforced by the parent, not by
code the tool could edit.

The audit hook below refuses sockets, new processes, `ctypes` and writing
files. It is a tripwire: it catches an honest mistake and a lazy attack, and
it makes the refusal readable ("a tool may not open a socket"). It is not the
wall. The wall is the operating system on the device, which runs this process
as a user with no network and nothing to read (plan 10).
"""

import io
import json
import os
import sys
import traceback

MAX_LINE = 256 * 1024

_BLOCKED = {
    "socket.connect": "open a socket; use ctx.http",
    "socket.bind": "open a socket; use ctx.http",
    "socket.getaddrinfo": "look up a host; use ctx.http",
    "socket.gethostbyname": "look up a host; use ctx.http",
    "subprocess.Popen": "start a program",
    "os.system": "start a program",
    "os.exec": "start a program",
    "os.posix_spawn": "start a program",
    "os.spawn": "start a program",
    "os.startfile": "start a program",
    "os.fork": "start a process",
    "os.forkpty": "start a process",
    "os.kill": "signal a process",
    "ctypes.dlopen": "load native code",
    "ctypes.dlsym": "load native code",
    "ctypes.cdata": "use ctypes",
    "os.remove": "delete files",
    "os.rename": "move files",
    "os.mkdir": "make folders",
    "os.rmdir": "delete folders",
    "os.chmod": "change file permissions",
    "os.chown": "change file owners",
    "os.link": "link files",
    "os.symlink": "link files",
    "os.truncate": "change files",
    "shutil.rmtree": "delete folders",
    "shutil.move": "move files",
    "shutil.copyfile": "copy files",
    "winreg.OpenKey": "read the registry",
    "winreg.ConnectRegistry": "read the registry",
    "webbrowser.open": "open a browser",
}


class ToolError(Exception):
    """Raise this for a failure the model should hear, written the way a
    person should hear it: `raise ToolError("I could not find that city.")`."""


class Refused(Exception):
    """The tool tried something a tool may not do."""


class Response:
    def __init__(self, status, text, truncated=False, location=""):
        self.status = status
        self.text = text
        self.truncated = truncated
        self.location = location
        self.ok = 200 <= status < 300

    def json(self):
        try:
            return json.loads(self.text)
        except ValueError:
            raise ToolError("The answer from the internet was not readable.")


class _Channel:
    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer

    def read(self):
        line = self._reader.readline(MAX_LINE + 1)
        if not line or len(line) > MAX_LINE:
            raise SystemExit(3)
        return json.loads(line)

    def write(self, message):
        self._writer.write(json.dumps(message, ensure_ascii=False,
                                      default=str) + "\n")
        self._writer.flush()

    def ask(self, message):
        self.write(message)
        answer = self.read()
        if not answer.get("ok"):
            raise ToolError(str(answer.get("error") or "that did not work"))
        return answer


class Http:
    """`ctx.http.get(url, params=None, headers=None)` and `.post(url,
    json=None, headers=None)`. HTTPS only, and only to the hosts listed under
    `permissions.internet` in tool.json."""

    def __init__(self, channel):
        self._channel = channel

    def _send(self, method, url, params, headers, body):
        answer = self._channel.ask({
            "op": "http", "method": method, "url": url,
            "params": params or {}, "headers": headers or {}, "json": body})
        return Response(answer["status"], answer.get("text", ""),
                        bool(answer.get("truncated")),
                        answer.get("location", ""))

    def get(self, url, params=None, headers=None):
        return self._send("GET", url, params, headers, None)

    def post(self, url, json=None, params=None, headers=None):
        return self._send("POST", url, params, headers, json)


class Storage:
    """`ctx.storage.get(key, default=None)`, `.set(key, value)` and
    `.delete(key)`. A few kilobytes of JSON that belong to this tool."""

    def __init__(self, channel):
        self._channel = channel

    def get(self, key, default=None):
        answer = self._channel.ask({"op": "storage_get", "key": key})
        return answer["value"] if answer.get("found") else default

    def set(self, key, value):
        self._channel.ask({"op": "storage_set", "key": key, "value": value})

    def delete(self, key):
        self._channel.ask({"op": "storage_delete", "key": key})


class Context:
    def __init__(self, channel, request):
        self.http = Http(channel)
        self.storage = Storage(channel)
        self.lang = request.get("lang", "en")
        self.now = request.get("now", "")       # local time, 2026-10-02T15:10
        self.tz = request.get("tz", "")


def _guard(tool_dir):
    """Refuse what a tool has no business doing, from here to the end of the
    process: an audit hook cannot be taken off again."""
    allowed = [os.path.realpath(tool_dir)]
    for entry in list(sys.path) + [sys.prefix, sys.base_prefix,
                                   sys.exec_prefix]:
        if entry:
            allowed.append(os.path.realpath(entry))
    allowed = tuple(os.path.normcase(p) for p in allowed)

    def inside(path):
        try:
            real = os.path.normcase(os.path.realpath(path))
        except (OSError, ValueError):
            return False
        return any(real == root or real.startswith(root + os.sep)
                   for root in allowed)

    def hook(event, args):
        why = _BLOCKED.get(event)
        if why is not None:
            raise Refused("a tool may not %s" % why)
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if isinstance(path, int):
                return
            writing = (isinstance(mode, str) and any(c in mode
                                                     for c in "wax+")) or (
                isinstance(flags, int) and flags & (
                    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND
                    | os.O_TRUNC))
            if writing:
                raise Refused("a tool may not write files; use ctx.storage")
            if isinstance(path, bytes):
                path = os.fsdecode(path)
            if not inside(str(path)):
                raise Refused("a tool may not read files outside its own "
                              "folder")

    sys.addaudithook(hook)


def main(argv):
    tool_dir = os.path.abspath(argv[1])
    # The pipe is ours. Whatever the tool prints goes to stderr, where the
    # tool creator shows it and the device throws it away.
    reader = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8",
                              newline="\n")
    writer = io.TextIOWrapper(os.fdopen(os.dup(sys.stdout.fileno()), "wb"),
                              encoding="utf-8", newline="\n")
    sys.stdout = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)
    sys.stdin = io.StringIO("")
    channel = _Channel(reader, writer)
    request = channel.read()

    with open(os.path.join(tool_dir, "tool.py"), "r", encoding="utf-8") as fh:
        source = fh.read()

    import types
    api = types.ModuleType("trsttool")
    api.ToolError = ToolError
    sys.modules["trsttool"] = api
    module = types.ModuleType("tool")
    module.__file__ = os.path.join(tool_dir, "tool.py")

    os.chdir(tool_dir)
    _guard(tool_dir)
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        fn = getattr(module, request["function"], None)
        if not callable(fn):
            raise ToolError("the tool has no function called %s"
                            % request["function"])
        payload = fn(Context(channel, request), **request.get("args", {}))
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"result": payload}
        channel.write({"op": "result", "payload": payload})
    except ToolError as exc:
        channel.write({"op": "error", "kind": "tool", "message": str(exc)})
    except Refused as exc:
        channel.write({"op": "error", "kind": "refused", "message": str(exc)})
    except Exception as exc:
        try:
            traceback.print_exc(file=sys.stderr)
        except Exception:
            pass
        channel.write({"op": "error", "kind": "crash",
                       "message": "%s: %s" % (type(exc).__name__, exc)})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

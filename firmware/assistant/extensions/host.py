"""The firmware's side of a third-party tool: start its process, answer what
it asks for, and stop it when its time is up.

A call is one process (`runner.py`). The tool cannot reach the internet or a
file on its own; it asks here, and the two things it may ask for are checked
here against what its `tool.json` declared:

- `http`: HTTPS, port 443, GET or POST, to a host on the tool's list, and only
  if that name resolves to a public address. Five seconds, 64 KB of answer.
  Redirects are not followed: the tool gets the 3xx and the `Location`, and may
  ask again if that host is on its list too.
- `storage`: one small JSON file per tool.

This runs on the assistant's worker thread, never on the main loop, and
changes no device state: the storage file belongs to the tool alone.

## Where the process runs

On the device, `TRSTENKO_TOOL_SOCKET` names a systemd socket, and each call
is one connection to it: systemd starts `runner.py` in a unit of its own, as
a throwaway user with no network and nothing on disk but the tool and Python
(`deploy/trstenko-tool@.service`). When the variable is set the socket is the
only way a tool runs; if it cannot be reached, the call fails rather than
falling back to a process with the firmware's own rights.

Everywhere else (the emulator, the tool creator, a laptop), the runner is a
child process, with memory, CPU time, file size and open files capped by the
kernel where the platform allows it.
"""

import http.client
import ipaddress
import json
import logging
import os
import queue
import socket
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode, urlsplit

log = logging.getLogger("trstenko.extensions")

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner.py")
CALL_SECONDS = 8.0
HTTP_SECONDS = 5.0
MAX_ANSWER = 64 * 1024
MAX_REQUESTS = 4            # internet requests in one call
MAX_BODY = 8 * 1024
MAX_LINE = 256 * 1024
STORAGE_BYTES = 8 * 1024
MAX_KEY = 64
_HEADERS_REFUSED = {"host", "content-length", "transfer-encoding",
                    "connection", "proxy-authorization"}
SOCKET_ENV = "TRSTENKO_TOOL_SOCKET"
# For the child process off the device. The unit sets its own on the device.
MEMORY_LIMIT = 256 * 1024 * 1024
OPEN_FILES_LIMIT = 64


class Denied(Exception):
    """A request the tool was not allowed to make. The message is for the
    tool's author and the model."""


def fetch(tool_id, method, url, headers=None, body=None,
          timeout=HTTP_SECONDS):
    """One HTTPS request. The caller has already checked the host against the
    tool's list. Returns `{"status", "text", "truncated", "location"}`."""
    parts = urlsplit(url)
    host = parts.hostname
    path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    conn = http.client.HTTPSConnection(
        host, 443, timeout=timeout, context=ssl.create_default_context())
    try:
        conn.connect()
        # The name was on the list; where it points also has to be outside
        # the house. A public name that resolves to 192.168.1.1 is how a tool
        # would talk to the router.
        peer = ipaddress.ip_address(conn.sock.getpeername()[0])
        if not peer.is_global:
            raise Denied("%s is not a public address" % host)
        send = {"User-Agent": "Trstenko-tool/%s" % tool_id,
                "Accept": "application/json, text/plain;q=0.8"}
        for key, value in (headers or {}).items():
            if key.lower() not in _HEADERS_REFUSED:
                send[str(key)] = str(value)
        conn.request(method, path, body=body, headers=send)
        response = conn.getresponse()
        raw = response.read(MAX_ANSWER + 1)
        return {"status": response.status,
                "text": raw[:MAX_ANSWER].decode("utf-8", "replace"),
                "truncated": len(raw) > MAX_ANSWER,
                "location": response.getheader("Location", "") or ""}
    finally:
        conn.close()


class Storage:
    """A tool's own few kilobytes, as one JSON file."""

    def __init__(self, path):
        self.path = path

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key):
        data = self._read()
        return (True, data[key]) if key in data else (False, None)

    def set(self, key, value):
        data = self._read()
        data[key] = value
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > STORAGE_BYTES:
            raise Denied("the tool's storage is full (%d KB)"
                         % (STORAGE_BYTES // 1024))
        self._write(text)

    def delete(self, key):
        data = self._read()
        if data.pop(key, None) is not None:
            self._write(json.dumps(data, ensure_ascii=False,
                                   separators=(",", ":")))

    def _write(self, text):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, self.path)

    def wipe(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


class Call:
    """What one call may do. Made per call so the request count starts at
    nought."""

    def __init__(self, package, storage=None, fetcher=fetch):
        self.package = package
        self.storage = storage
        self.fetcher = fetcher
        self.requests = 0

    def answer(self, message):
        """The reply to one thing the tool asked for."""
        try:
            op = message.get("op")
            if op == "http":
                return self._http(message)
            if op in ("storage_get", "storage_set", "storage_delete"):
                return self._storage(op, message)
            return {"ok": False, "error": "unknown request %r" % (op,)}
        except Denied as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            log.info("tool %s: request failed: %s", self.package.id,
                     type(exc).__name__)
            return {"ok": False, "error": "the request did not get through"}

    def _http(self, message):
        if not self.package.hosts:
            raise Denied("this tool did not ask for the internet")
        self.requests += 1
        if self.requests > MAX_REQUESTS:
            raise Denied("at most %d requests in one call" % MAX_REQUESTS)
        method = str(message.get("method", "GET")).upper()
        if method not in ("GET", "POST"):
            raise Denied("only GET and POST")
        url = str(message.get("url", ""))
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise Denied("only https:// addresses")
        if parts.username or parts.password:
            raise Denied("no user names in the address")
        try:
            port = parts.port
        except ValueError:
            raise Denied("that address is not valid")
        host = (parts.hostname or "").lower().rstrip(".")
        if port not in (None, 443):
            raise Denied("only port 443")
        if host not in self.package.hosts:
            raise Denied("%s is not on this tool's list of hosts (%s)"
                         % (host or "that", ", ".join(self.package.hosts)))
        params = message.get("params") or {}
        if not isinstance(params, dict):
            raise Denied("params must be an object")
        if params:
            query = urlencode({str(k): str(v) for k, v in params.items()})
            url = "https://%s%s?%s" % (host, parts.path or "/", "&".join(
                q for q in (parts.query, query) if q))
        else:
            url = "https://%s%s%s" % (host, parts.path or "/",
                                      "?" + parts.query if parts.query
                                      else "")
        headers = message.get("headers") or {}
        if not isinstance(headers, dict):
            raise Denied("headers must be an object")
        headers = dict(headers)
        body = None
        if method == "POST":
            body = json.dumps(message.get("json"),
                              ensure_ascii=False).encode("utf-8")
            if len(body) > MAX_BODY:
                raise Denied("the request body is larger than %d KB"
                             % (MAX_BODY // 1024))
            headers.setdefault("Content-Type", "application/json")
        answer = self.fetcher(self.package.id, method, url, headers, body)
        return {"ok": True, "status": int(answer["status"]),
                "text": str(answer.get("text", ""))[:MAX_ANSWER],
                "truncated": bool(answer.get("truncated")),
                "location": str(answer.get("location", ""))[:500]}

    def _storage(self, op, message):
        if not self.package.storage or self.storage is None:
            raise Denied("this tool did not ask for storage")
        key = message.get("key")
        if not isinstance(key, str) or not key or len(key) > MAX_KEY:
            raise Denied("a storage key is text of at most %d characters"
                         % MAX_KEY)
        if op == "storage_get":
            found, value = self.storage.get(key)
            return {"ok": True, "found": found, "value": value}
        if op == "storage_set":
            self.storage.set(key, message.get("value"))
        else:
            self.storage.delete(key)
        return {"ok": True}


def _environment():
    """Next to nothing: no keys, no paths, no home. Windows will not start
    Python without knowing where Windows is."""
    env = {}
    for name in ("SYSTEMROOT", "SystemRoot"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


class _Connection:
    """One call over the device's tool socket, shaped like the parts of a
    `Popen` that `run` uses. Closing it is what stops the call; the unit's
    own time limit stops a tool that ignores that."""

    stderr = None

    def __init__(self, path, timeout):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.settimeout(timeout)
            self._sock.connect(path)
            self._sock.settimeout(None)
        except OSError:
            self._sock.close()
            raise
        self.stdin = self._sock.makefile("wb")
        self.stdout = self._sock.makefile("rb")

    def kill(self):
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    def wait(self, timeout=None):
        return 0


def _limit(pid, seconds):
    """Cap what the child may take, where the kernel lets us say so for
    another process. Applied a moment after it starts, before it has read
    its request, let alone run the tool."""
    try:
        import resource
        prlimit = resource.prlimit
    except (ImportError, AttributeError):
        return
    cpu = int(seconds) + 2
    for name, value in (("RLIMIT_AS", MEMORY_LIMIT),
                        ("RLIMIT_CPU", cpu),
                        ("RLIMIT_FSIZE", 0),
                        ("RLIMIT_CORE", 0),
                        ("RLIMIT_NOFILE", OPEN_FILES_LIMIT)):
        which = getattr(resource, name, None)
        if which is None:
            continue
        try:
            prlimit(pid, which, (value, value))
        except (OSError, ValueError):
            pass


def _start(folder, call, seconds, python, on_output):
    """The runner for one call: a connection to the device's socket, or a
    child process. Raises OSError if neither can be had."""
    path = os.environ.get(SOCKET_ENV)
    if path:
        return _Connection(path, timeout=min(2.0, seconds))
    process = subprocess.Popen(
        [python or sys.executable, "-I", "-B", RUNNER, folder],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if on_output else subprocess.DEVNULL,
        cwd=folder, env=_environment(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _limit(process.pid, seconds)
    return process


def run(folder, function, args, call, lang="en", now="", tz="",
        seconds=CALL_SECONDS, python=None, on_output=None):
    """Run one function of the tool unpacked in `folder`.

    Returns `(payload, None)` when the tool returned, or `(None, message)`
    when it failed, timed out, or broke a rule. Never raises for anything the
    tool did."""
    try:
        process = _start(folder, call, seconds, python, on_output)
    except OSError as exc:
        log.error("tool %s would not start: %s", call.package.id, exc)
        return None, "the tool could not be started"

    lines = queue.Queue()

    def pump():
        try:
            for raw in iter(lambda: process.stdout.readline(MAX_LINE + 1),
                            b""):
                lines.put(raw)
        except (OSError, ValueError):
            pass
        lines.put(None)

    threading.Thread(target=pump, daemon=True,
                     name="tool-%s" % call.package.id).start()
    if on_output and process.stderr is not None:
        def drain():
            try:
                text = process.stderr.read(64 * 1024)
                on_output(text.decode("utf-8", "replace"))
            except (OSError, ValueError):
                pass
        threading.Thread(target=drain, daemon=True).start()

    def send(message):
        process.stdin.write((json.dumps(message, ensure_ascii=False,
                                        default=str) + "\n").encode("utf-8"))
        process.stdin.flush()

    deadline = time.monotonic() + seconds
    try:
        send({"function": function, "args": args, "lang": lang, "now": now,
              "tz": tz, "tool": os.path.basename(os.path.normpath(folder))})
        while True:
            try:
                raw = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return None, "the tool took longer than %d seconds" % seconds
            if raw is None:
                return None, "the tool stopped without an answer"
            if len(raw) > MAX_LINE:
                return None, "the tool's answer was too large"
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return None, "the tool's answer was not readable"
            if not isinstance(message, dict):
                return None, "the tool's answer was not readable"
            op = message.get("op")
            if op == "result":
                payload = message.get("payload")
                return (payload if isinstance(payload, dict) else {}), None
            if op == "error":
                text = " ".join(str(message.get("message", "")).split())
                kind = message.get("kind")
                if kind == "tool":
                    return None, text[:200] or "that did not work"
                log.warning("tool %s %s: %s", call.package.id, kind,
                            text[:200])
                if kind == "refused":
                    return None, text[:200]
                return None, ("the tool has a fault: " + text)[:200]
            send(call.answer(message))
    except (OSError, ValueError):
        return None, "the tool stopped without an answer"
    finally:
        try:
            process.kill()
        except OSError:
            pass
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except OSError:
                pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass


def resolves_publicly(host):
    """Does this name point outside the house right now? For the install
    page, which says so before the owner agrees."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and all(
        ipaddress.ip_address(info[4][0]).is_global for info in infos)

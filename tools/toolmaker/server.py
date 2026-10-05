"""The tool creator's page and its small API, on this machine only.

    python -m tools.toolmaker            # opens the browser
    python -m tools.toolmaker --dir D:/my-tools --port 8765 --no-browser

It listens on 127.0.0.1 and nothing else. Running a tool means running code,
so every request but the page itself must carry the token the page was given:
another site open in the same browser cannot ask this server to run anything.
"""

import argparse
import json
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from . import project

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "static", "index.html")
FONT = os.path.join(os.path.dirname(os.path.dirname(HERE)), "firmware",
                    "webui", "static", "fonts", "quicksand.woff2")
MAX_BODY = 512 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "TrstenkoToolmaker"
    root = ""
    token = ""
    lock = threading.Lock()         # one run at a time: they share a folder

    def log_message(self, fmt, *args):
        pass

    # ---- plumbing ----

    def _send(self, status, body, kind="application/json; charset=utf-8",
              extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' "
                         "'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _local(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _allowed(self):
        return self._local() and secrets.compare_digest(
            self.headers.get("X-Toolmaker", ""), self.token)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise project.ProjectError("that is too large")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise project.ProjectError("send a JSON object")
        return data

    def _route(self, method):
        parts = [unquote(p) for p in
                 urlsplit(self.path).path.strip("/").split("/")]
        try:
            if method == "GET" and parts == [""]:
                if not self._local():
                    return self._send(403, {"error": "local only"})
                with open(PAGE, encoding="utf-8") as fh:
                    page = fh.read().replace("__TOKEN__", self.token)
                return self._send(200, page.encode("utf-8"),
                                  "text/html; charset=utf-8")
            if method == "GET" and parts == ["quicksand.woff2"]:
                with open(FONT, "rb") as fh:
                    return self._send(200, fh.read(), "font/woff2")
            # A download is a plain link, which cannot carry a header.
            if method == "GET" and len(parts) == 4 and parts[:2] == [
                    "api", "export"] and self._local() \
                    and secrets.compare_digest(parts[3], self.token):
                name, data = project.export(self.root, parts[2])
                return self._send(200, data, "application/octet-stream", {
                    "Content-Disposition": 'attachment; filename="%s"' % name})
            if parts[0] != "api" or not self._allowed():
                return self._send(403, {"error": "not allowed"})
            return self._send(200, self._api(method, parts[1:]))
        except project.ProjectError as exc:
            return self._send(400, {"error": str(exc)})
        except (ValueError, OSError) as exc:
            return self._send(400, {"error": str(exc)})

    def _api(self, method, parts):
        root = self.root
        if parts == ["projects"]:
            if method == "POST":
                body = self._body()
                project.create(root, body.get("id"), body.get("name"))
            return {"projects": project.listing(root), "folder": root}
        if len(parts) >= 2 and parts[0] == "project":
            pid = parts[1]
            rest = parts[2:]
            if method == "GET" and not rest:
                return {"files": project.read(root, pid),
                        "summary": project.summary(root, pid),
                        "folder": project.folder_of(root, pid)}
            if method == "PUT" and rest == ["file"]:
                body = self._body()
                project.write(root, pid, body.get("name"),
                              str(body.get("text", "")))
                return {"summary": project.summary(root, pid)}
            if method == "POST" and rest == ["test"]:
                with self.lock:
                    ran = project.run_tests(root, pid)
                    return {"tests": ran["tests"],
                            "checks": project.standard_checks(
                                root, pid, ran["examples"])}
            if method == "POST" and rest == ["try"]:
                body = self._body()
                with self.lock:
                    return project.call(
                        root, pid, body.get("tool"), body.get("args") or {},
                        internet=body.get("internet") or {},
                        storage=body.get("storage") or {},
                        lang=body.get("lang") or "en",
                        live=bool(body.get("live"))).as_dict()
        raise project.ProjectError("no such thing")

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")


def serve(root, port=0, token=None):
    """(server, address). The caller runs `server.serve_forever()`."""
    os.makedirs(root, exist_ok=True)
    handler = type("Bound", (Handler,), {
        "root": root, "token": token or secrets.token_urlsafe(24)})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server, "http://127.0.0.1:%d/" % server.server_address[1]


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m tools.toolmaker",
        description="Write, test and export a .trsttool for Trstenko.")
    parser.add_argument("--dir", default=os.path.join(
        os.path.expanduser("~"), "trstenko-tools"),
        help="where your tools are kept (default: ~/trstenko-tools)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        server, address = serve(os.path.abspath(args.dir), args.port)
    except OSError:
        server, address = serve(os.path.abspath(args.dir), 0)
    print("Tool creator: %s" % address)
    print("Your tools:   %s" % os.path.abspath(args.dir))
    print("Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

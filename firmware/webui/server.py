"""A standard-library server for the update page.

Flask is the plan for the device (`app.py`, and the reasoning is in
`docs/research/software-stack.md` -- the captive-portal examples are all
Flask). This exists so the emulator and a test can serve the same page and the
same API without anything installed, and so a Pi with a broken virtualenv can
still be talked to.

    from firmware.webui import serve
    server = serve(api, port=8080)   # background thread
    ...
    server.shutdown()
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

from .headers import MAX_BODY, SECURITY_HEADERS

STATIC = os.path.join(os.path.dirname(__file__), "static")

TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css",
         ".js": "text/javascript", ".png": "image/png", ".svg": "image/svg+xml"}


class _Handler(BaseHTTPRequestHandler):
    api = None
    server_version = "trstenko"

    def log_message(self, fmt, *args):
        pass  # the device's journal is not a web access log

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path.startswith("/api/"):
            self._api("GET", path, {}, dict(parse_qsl(query)))
            return
        self._static(path)

    def _token(self):
        """`Authorization: Bearer <token>`, as the page sends it."""
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return self.headers.get("X-Trstenko-Token", "").strip()

    def do_POST(self):
        self._with_body("POST")

    def do_PATCH(self):
        self._with_body("PATCH")

    def _with_body(self, method):
        path, _, query = self.path.partition("?")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._send(413, json.dumps({"error": "request too large"}).encode(),
                       "application/json")
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._send(400, json.dumps({"error": "body is not JSON"}).encode(),
                       "application/json")
            return
        self._api(method, path, body, dict(parse_qsl(query)))

    def _api(self, method, path, body, query=None):
        status, payload = self.api.dispatch(method, path, body, self._token(),
                                            query)
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json")

    def _static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = os.path.normpath(os.path.join(STATIC, name))
        if not target.startswith(STATIC) or not os.path.isfile(target):
            self._send(404, b"not found", "text/plain")
            return
        with open(target, "rb") as fh:
            data = fh.read()
        ext = os.path.splitext(target)[1]
        self._send(200, data, TYPES.get(ext, "application/octet-stream"))

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def serve(api, host="0.0.0.0", port=8080, background=True):
    handler = type("Handler", (_Handler,), {"api": api})
    server = ThreadingHTTPServer((host, port), handler)
    if background:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    else:
        server.serve_forever()
    return server

"""A model provider that lives on 127.0.0.1 and does what the test tells it to.

The real `firmware/assistant/provider.py` is under test, over a real socket, so
what is proved is the code that will run on the device and not a stand-in for
it. This speaks Chat Completions (streamed, or whole) and
`/audio/transcriptions`, and a test queues what each request should get:

    fake = FakeProvider()
    base = fake.start()
    fake.queue(text("Hello there.", usage=(120, 6)))
    fake.queue(error(429, "rate_limit_exceeded", retry_after=1))

Each request takes the next item. What it can do beyond answering: wait before
the first byte, pause between chunks, stall for good after some of them, hang up
in the middle, send usage in the last chunk or with the finish or not at all,
number its tool calls the way Google's endpoint does, and answer with a status
and headers of its own. Every request is recorded with its headers and body, so
a test can check what the device actually sent: the key in the header and
nowhere else, the parameters the table allows and no others.

Standard library only.
"""

import collections
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ---- what to answer with ----

def text(content, usage=None, pieces=3, **faults):
    """A streamed text reply. `usage` is (prompt, completion) or a dict in the
    provider's own shape."""
    return dict(kind="chat", text=content, calls=[], usage=usage,
                pieces=pieces, **faults)


def calls(*specs, **kw):
    """A reply that asks for tools. Each spec is `(name, args)`, or a dict with
    `name`, `args` and optionally `id` and `extra` (a field to carry, like a
    thought signature)."""
    return dict(kind="chat", text=kw.pop("say", ""), calls=list(specs),
                usage=kw.pop("usage", None), pieces=kw.pop("pieces", 3), **kw)


def error(status, code="", message="", headers=None, body=None,
          retry_after=None, **kw):
    """An error response. `body` replaces the whole JSON body."""
    return dict(kind="error", status=status, code=code,
                message=message or code or "error", headers=headers or {},
                body=body, retry_after=retry_after, **kw)


def transcript(content, status=200, **kw):
    return dict(kind="stt", text=content, status=status, **kw)


def whole(content, usage=None, status=200):
    """A reply that ignores `stream` and answers with one JSON body."""
    return dict(kind="chat", text=content, calls=[], usage=usage, pieces=1,
                whole=True, status=status)


class Recorded:
    """What one request carried."""

    def __init__(self, path, headers, raw, connection):
        self.path = path
        self.headers = headers
        self.raw = raw
        self.connection = connection
        self.at = time.monotonic()
        try:
            self.json = json.loads(raw)
        except ValueError:
            self.json = None


def _usage(spec):
    if spec is None:
        return None
    if isinstance(spec, dict):
        return spec
    prompt, completion = spec[0], spec[1]
    cached = spec[2] if len(spec) > 2 else 0
    reasoning = spec[3] if len(spec) > 3 else 0
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens_details": {"reasoning_tokens": reasoning}}


def _split(text_, pieces):
    """Cut text into `pieces` parts at spaces, keeping every character."""
    words = text_.split(" ")
    if pieces <= 1 or len(words) <= 1:
        return [text_] if text_ else []
    step = max(1, -(-len(words) // pieces))
    parts = [" ".join(words[i:i + step]) for i in range(0, len(words), step)]
    return [p + (" " if n < len(parts) - 1 else "")
            for n, p in enumerate(parts)]


def chunks_for(b):
    """The streamed chunks for a chat behaviour, as dicts."""
    out = [{"choices": [{"index": 0, "delta": {"role": "assistant",
                                               "content": ""}}]}]
    for piece in _split(b["text"], b["pieces"]):
        out.append({"choices": [{"index": 0, "delta": {"content": piece}}]})
    for number, spec in enumerate(b["calls"]):
        if isinstance(spec, tuple):
            spec = {"name": spec[0], "args": spec[1]}
        args = spec["args"]
        raw = args if isinstance(args, str) else json.dumps(args)
        index = 0 if b.get("index_bug") else number
        head = {"index": index, "type": "function",
                "function": {"name": spec["name"], "arguments": ""}}
        if not b.get("no_ids"):
            head["id"] = spec.get("id", "call_%d" % number)
        head.update(spec.get("extra", {}))
        out.append({"choices": [{"index": 0,
                                 "delta": {"tool_calls": [head]}}]})
        cut = max(1, len(raw) // 2)
        for fragment in (raw[:cut], raw[cut:]):
            if fragment:
                part = {"index": index, "function": {"arguments": fragment}}
                if b.get("index_bug") and not b.get("no_ids"):
                    part["id"] = head["id"]
                out.append({"choices": [{"index": 0, "delta":
                                         {"tool_calls": [part]}}]})
    final = {"choices": [{"index": 0, "delta": {},
                          "finish_reason": "tool_calls" if b["calls"]
                          else "stop"}]}
    usage = _usage(b.get("usage"))
    style = b.get("usage_style", "last_chunk")
    if usage and style == "with_finish":
        final["usage"] = usage
    if not b.get("no_finish"):
        out.append(final)
    if usage and style == "last_chunk":
        out.append({"choices": [], "usage": usage})
    return out


class FakeProvider:
    def __init__(self):
        self.requests = []
        self.connections = 0
        self._script = collections.deque()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    # ---- running it ----

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                with fake._lock:
                    fake.connections += 1
                    self.number = fake.connections

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(size) if size else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                rec = Recorded(self.path, headers, raw, self.number)
                with fake._lock:
                    fake.requests.append(rec)
                    behaviour = (fake._script.popleft() if fake._script
                                 else None)
                fake._answer(self, rec, behaviour)

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                # A client that hangs up mid-request is what the tests do on
                # purpose; the default prints a traceback for each.
                pass

        self._server = Quiet(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return "http://127.0.0.1:%d/v1" % self._server.server_address[1]

    def stop(self):
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def queue(self, *behaviours):
        with self._lock:
            self._script.extend(behaviours)

    def clear(self):
        with self._lock:
            self._script.clear()
            self.requests = []

    def chat_requests(self):
        return [r for r in self.requests if r.path.endswith("/chat/completions")]

    # ---- answering ----

    def _answer(self, h, rec, b):
        if b is None:
            return self._plain(h, 500, {"error": {
                "message": "the fake provider has nothing queued"}})
        wait = b.get("first_byte_delay", 0)
        if wait and self._stop.wait(wait):
            return
        if b["kind"] == "error":
            return self._error(h, b)
        if b["kind"] == "stt":
            if b["status"] != 200:
                return self._plain(h, b["status"], {"error": {
                    "message": "transcription failed"}})
            return self._plain(h, 200, {"text": b["text"]})
        if b.get("whole"):
            message = {"role": "assistant", "content": b["text"]}
            body = {"choices": [{"index": 0, "message": message,
                                 "finish_reason": "stop"}]}
            if b.get("usage"):
                body["usage"] = _usage(b["usage"])
            return self._plain(h, b.get("status", 200), body)
        self._stream(h, b)

    def _plain(self, h, status, body, headers=None):
        data = (body if isinstance(body, bytes)
                else json.dumps(body).encode("utf-8"))
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            h.send_header(k, str(v))
        h.end_headers()
        h.wfile.write(data)
        h.wfile.flush()

    def _error(self, h, b):
        headers = dict(b["headers"])
        if b["retry_after"] is not None:
            headers["Retry-After"] = b["retry_after"]
        body = b["body"]
        if body is None:
            body = {"error": {"message": b["message"], "code": b["code"],
                              "type": b["code"]}}
        self._plain(h, b["status"], body, headers)

    def _stream(self, h, b):
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Transfer-Encoding", "chunked")
        for k, v in b.get("headers", {}).items():
            h.send_header(k, str(v))
        h.end_headers()
        sent = 0
        for chunk in chunks_for(b):
            if "stall_after" in b and sent >= b["stall_after"]:
                self._stop.wait(60)
                return
            if "drop_after" in b and sent >= b["drop_after"]:
                try:
                    h.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                h.close_connection = True
                return
            self._write(h, b"data: " + json.dumps(chunk).encode("utf-8")
                        + b"\n\n")
            sent += 1
            if sent == 1 and b.get("inject"):
                # Something other than a chunk, in the middle of the stream.
                for extra in b["inject"]:
                    self._write(h, b"data: " + json.dumps(extra).encode()
                                + b"\n\n")
                self._write(h, b"")
                return
            gap = b.get("gap", 0)
            if gap and self._stop.wait(gap):
                return
        if b.get("endless"):
            while not self._stop.is_set() and not h.close_connection:
                self._write(h, b"data: " + json.dumps({"choices": [{
                    "index": 0, "delta": {"content": "x" * 1000}}]}).encode()
                    + b"\n\n")
            return
        if "stall_after" in b and sent >= b["stall_after"]:
            self._stop.wait(60)
            return
        if b.get("done", True):
            self._write(h, b"data: [DONE]\n\n")
        self._write(h, b"")          # the terminating chunk
        h.wfile.flush()
        if b.get("hangup_after"):
            # Closes without saying so, which is what a server does to a
            # connection that has sat idle too long.
            try:
                h.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            h.close_connection = True

    @staticmethod
    def _write(h, data):
        try:
            h.wfile.write(b"%x\r\n%b\r\n" % (len(data), data))
            h.wfile.flush()
        except OSError:
            h.close_connection = True

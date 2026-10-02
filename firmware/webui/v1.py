"""The `/api/v1` endpoints: the device's settings and reminders, over HTTP.

    GET   /api/v1/info                     API version and the device's identity
    GET   /api/v1/state                    what the Home screen needs, in one call
    GET   /api/v1/reminders                ?state=pending|missed|done|...
    POST  /api/v1/reminders                {"text", "local_time" | "in_seconds",
                                            "repeat"?, "tz"?}
    POST  /api/v1/reminders/{id}/cancel
    POST  /api/v1/reminders/{id}/snooze    {"minutes"?}
    GET   /api/v1/settings                 everything but secrets
    PATCH /api/v1/settings                 {"voice": {"volume": 0.4}}, validated

Like the rest of the page's API this is a plain function of (method, path, body)
with no web framework in it, so `server.py` (standard library) and `app.py`
(Flask) serve exactly the same thing and a test calls it directly.

**Every change goes through a `Command` to the main thread** (plan 01). The
handler posts it and waits up to two seconds for the answer; a device that is
too busy to answer in that time gives a 503, not a hang. Reads go the same way,
so a page never shows numbers that were halfway through changing. The web
thread never touches the scheduler, the settings or the database itself.

**Versioned on purpose.** The page is the first client of this API and a phone
app may be the second (plan 07), so `info` says which version this is and the
shapes do not change within one.

Errors are `{"error": "a sentence"}`. The sentence is the same one the voice
tool would say, because both come from the same place.
"""

from firmware import events as ev
from firmware import product
from firmware.updates import version as version_mod

API_VERSION = 1

# What a client can rely on being here. A new capability is added to this list
# and never changes an old one, so an app can ask rather than guess.
FEATURES = ("reminders", "settings", "memories", "keys", "usage", "privacy",
            "live")

STATUS = {ev.INVALID: 400, ev.NOT_FOUND: 404, ev.CONFLICT: 409,
          ev.UNAVAILABLE: 503}

STATES = ("pending", "ringing", "done", "missed", "cancelled", "interrupted")

REMINDER_FIELDS = ("text", "local_time", "in_seconds", "repeat", "tz")

BUSY = (503, {"error": "The device is busy. Try again in a moment."})
NO_DEVICE = (503, {"error": "The device is not ready yet."})


def _bad(message):
    return 400, {"error": message}


# Plan 07: at most this many live pages at once; each holds a connection and a
# thread open. A fourth is told to poll `/state` instead.
MAX_STREAMS = 3
STREAM_PERIOD = 1.0
NL = bytes([10])
EOL = NL + NL
STREAM_MAX_SECONDS = 3600.0


class V1Api:
    def __init__(self, owner, commander=None, timeout=ev.COMMAND_TIMEOUT):
        """`owner` is the `UpdateApi` this belongs to, which holds the device
        and the updater: the emulator swaps them when it restarts into a new
        release, and this has to follow. `commander(cmd)` puts a command in
        front of the main thread; by default that is the device's inbox. A test
        passes a function that also runs a tick, since there is no loop
        running in another thread."""
        self.owner = owner
        self.timeout = timeout
        self._commander = commander

    @property
    def device(self):
        return self.owner.device

    @property
    def manager(self):
        return self.owner.manager

    # ---- routing ----

    def dispatch(self, method, path, body=None, query=None):
        """`path` is already normalised and the caller already holds a valid
        token. Returns (status, payload)."""
        query = query or {}
        parts = path.strip("/").split("/")[2:]   # after "api" and "v1"

        if parts == ["info"]:
            return self._only(method, "GET", self._info)
        if parts == ["state"]:
            return self._only(method, "GET", self._state)
        if parts == ["settings"]:
            if method == "GET":
                return self._command("settings.get")
            if method == "PATCH":
                return self._settings_patch(body)
            return self._wrong_method("GET, PATCH")
        if parts == ["reminders"]:
            if method == "GET":
                return self._reminders_list(query)
            if method == "POST":
                return self._reminders_add(body)
            return self._wrong_method("GET, POST")
        if len(parts) == 3 and parts[0] == "reminders" \
                and parts[2] in ("cancel", "snooze"):
            if not parts[1].isdigit():
                return 404, {"error": "There is no reminder %s." % parts[1][:20]}
            if method != "POST":
                return self._wrong_method("POST")
            job_id = int(parts[1])
            if parts[2] == "cancel":
                return self._command("reminders.cancel", {"id": job_id})
            return self._reminders_snooze(job_id, body)
        if parts == ["live"]:
            return self._only(method, "GET", lambda: self._command("live"))
        if parts == ["assistant"]:
            return self._only(method, "GET",
                              lambda: self._command("assistant.status"))
        if parts == ["memories"]:
            if method == "GET":
                return self._command("memories.list")
            if method == "POST":
                return self._memory_add(body)
            return self._wrong_method("GET, POST")
        if parts and parts[0] == "memories" and len(parts) in (2, 3):
            return self._memory_item(method, parts, body)
        if parts == ["keys"]:
            return self._only(method, "GET",
                              lambda: self._command("keys.status"))
        if len(parts) in (2, 3) and parts[0] == "keys":
            return self._key(method, parts, body)
        if parts == ["usage"]:
            return self._only(method, "GET", lambda: self._usage(query))
        if parts == ["privacy", "export"]:
            return self._only(method, "GET",
                              lambda: self._command("privacy.export"))
        if parts == ["privacy", "delete"]:
            return self._only(method, "POST",
                              lambda: self._privacy_delete(body))
        return 404, {"error": "no such endpoint"}

    # ---- handlers ----

    def _memory_add(self, body):
        if not isinstance(body, dict):
            return _bad("Send the memory as a JSON object.")
        status, payload = self._command("memories.save", {
            "text": body.get("text"), "category": body.get("category"),
            "pinned": bool(body.get("pinned")), "source": "web"})
        if status == 200 and not payload.get("ok"):
            return 400, {"error": payload.get("error", "not saved"),
                         "code": payload.get("code")}
        return (201 if status == 200 else status), payload

    def _memory_item(self, method, parts, body):
        if not parts[1].isdigit():
            return 404, {"error": "There is no memory %s." % parts[1][:20]}
        memory_id = int(parts[1])
        if len(parts) == 2:
            if method != "DELETE":
                return self._wrong_method("DELETE")
            status, payload = self._command("memories.forget",
                                            {"id": memory_id})
            if status == 200 and not payload.get("ok"):
                return 404, {"error": payload.get("error", "not found")}
            return status, payload
        if parts[2] != "pin":
            return 404, {"error": "no such endpoint"}
        if method != "POST":
            return self._wrong_method("POST")
        pinned = True if not isinstance(body, dict) else             bool(body.get("pinned", True))
        return self._command("memories.pin", {"id": memory_id,
                                              "pinned": pinned})

    def _key(self, method, parts, body):
        provider = parts[1]
        if len(parts) == 3:
            if parts[2] != "test":
                return 404, {"error": "no such endpoint"}
            if method != "POST":
                return self._wrong_method("POST")
            return self._command("keys.test", {"provider": provider})
        if method != "POST":
            return self._wrong_method("POST")
        if not isinstance(body, dict):
            return _bad("Send the key as a JSON object.")
        args = {"provider": provider}
        if provider == "ollama":
            args["host"] = body.get("host", "")
        else:
            args["key"] = body.get("key", "")
        return self._command("keys.set", args)

    def _usage(self, query):
        try:
            days = int(query.get("days", 30))
        except (TypeError, ValueError):
            return _bad("days must be 7, 30 or 90.")
        return self._command("usage.history", {"days": days})

    def _privacy_delete(self, body):
        """Needs the PIN again: it cannot be undone."""
        if not isinstance(body, dict):
            return _bad("Send what to delete as a JSON object.")
        refused = self._confirm(body)
        if refused is not None:
            return refused
        return self._command("privacy.delete", {"what": body.get("what")})

    def _confirm(self, body):
        """None if the caller may go on, else the refusal."""
        auth = self.owner.auth
        if auth is None:
            return None
        ok, wait = auth.confirm(body.get("pin"))
        if ok:
            return None
        if wait > 0:
            return 429, {"error": "Too many wrong PINs. Wait a moment.",
                         "retry_after": round(wait, 1)}
        return 403, {"error": "That is not the PIN on the device.",
                     "needs_pin": True}

    def _info(self):
        data_root = (self.manager.config.data_root
                     if self.manager is not None else None)
        return 200, {
            "api_version": API_VERSION,
            "features": list(FEATURES),
            "firmware": str(version_mod.current()),
            "product": product.identity(data_root),
        }

    def _state(self):
        status, payload = self._command("state")
        if status != 200:
            return status, payload
        if self.manager is not None:
            # The updater is read from here, as the update page already does:
            # its status reads files, which the main thread should not.
            update = self.manager.status()
            payload["update"] = {
                "state": update["state"], "installed": update["installed"],
                "available": (update["release"] or {}).get("version"),
            }
        return 200, payload

    def _settings_patch(self, body):
        if not isinstance(body, dict):
            return _bad("Send the settings to change as a JSON object, such "
                        'as {"voice": {"volume": 0.4}}.')
        return self._command("settings.update", body)

    def _reminders_list(self, query):
        state = query.get("state")
        if state is not None and state not in STATES:
            return _bad("state must be one of: %s." % ", ".join(STATES))
        status, payload = self._command("reminders.list", {"state": state})
        return status, payload

    def _reminders_add(self, body):
        if not isinstance(body, dict):
            return _bad("Send the reminder as a JSON object.")
        unknown = sorted(set(body) - set(REMINDER_FIELDS))
        if unknown:
            return _bad("Unknown field: %s. A reminder takes %s."
                        % (unknown[0], ", ".join(REMINDER_FIELDS)))
        args = dict(body, source="web")
        status, payload = self._command("reminders.add", args)
        return (201 if status == 200 else status), payload

    def _reminders_snooze(self, job_id, body):
        if body is not None and not isinstance(body, dict):
            return _bad("Send the snooze as a JSON object.")
        args = {"id": job_id}
        if body and "minutes" in body:
            args["minutes"] = body["minutes"]
        return self._command("reminders.snooze", args)

    # ---- the live stream ----

    def open_stream(self, sleep=None):
        """For `GET /api/v1/events`. Returns (status, body) where a 200 body
        is an iterator of Server-Sent-Events frames, one `live` event a second.
        It ends by itself after an hour (the page reconnects), when the device
        goes away, or when the consumer stops iterating."""
        import json
        import threading
        import time

        sleep = sleep or time.sleep
        lock = self.__dict__.setdefault("_stream_lock", threading.Lock())
        with lock:
            if self.__dict__.get("_streams", 0) >= MAX_STREAMS:
                return 503, {"error": "Too many pages are open. Close one."}
            self._streams = self.__dict__.get("_streams", 0) + 1

        def frames():
            started = time.monotonic()
            try:
                yield b"retry: 3000" + EOL
                while time.monotonic() - started < STREAM_MAX_SECONDS:
                    status, payload = self._command("live")
                    if status == 200:
                        yield (b"event: live" + NL + b"data: "
                               + json.dumps(payload).encode("utf-8") + EOL)
                    else:
                        yield b": device busy" + EOL
                    sleep(STREAM_PERIOD)
            finally:
                with lock:
                    self._streams -= 1
        return 200, frames()

    # ---- plumbing ----

    def _only(self, method, allowed, fn):
        if method != allowed:
            return self._wrong_method(allowed)
        return fn()

    def _wrong_method(self, allowed):
        return 405, {"error": "wrong method", "allowed": allowed}

    def _command(self, name, args=None):
        """Send it to the main thread, wait for the answer, and turn it into a
        status and a body."""
        if self.device is None:
            return NO_DEVICE
        cmd = ev.Command(name, args)
        (self._commander or self.device.events.post)(cmd)
        try:
            result = cmd.wait(self.timeout)
        except ev.CommandTimeout:
            return BUSY
        except ev.CommandError as exc:
            return STATUS.get(exc.kind, 400), {"error": str(exc)}
        return 200, result

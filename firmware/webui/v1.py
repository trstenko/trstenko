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
FEATURES = ("reminders", "settings")

STATUS = {ev.INVALID: 400, ev.NOT_FOUND: 404, ev.CONFLICT: 409,
          ev.UNAVAILABLE: 503}

STATES = ("pending", "ringing", "done", "missed", "cancelled", "interrupted")

REMINDER_FIELDS = ("text", "local_time", "in_seconds", "repeat", "tz")

BUSY = (503, {"error": "The device is busy. Try again in a moment."})
NO_DEVICE = (503, {"error": "The device is not ready yet."})


def _bad(message):
    return 400, {"error": message}


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
        return 404, {"error": "no such endpoint"}

    # ---- handlers ----

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

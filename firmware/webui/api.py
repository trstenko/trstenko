"""The update endpoints, with no web framework in sight.

`UpdateApi.dispatch` takes a method, a path and the caller's token, and
returns `(status, dict)`. Flask serves it on the device, the emulator serves
it from the standard library, and a test calls it directly. Putting the logic
in a framework would mean the emulator needed Flask installed to exercise an
update flow.

Everything except pairing needs a token. The page is served over the device's
own Wi-Fi, so without that, anyone who can see the hotspot can install
firmware on it -- see `auth.py`.

    POST /api/auth              {"pin": "123456"} -> {"token": "..."}
    GET  /api/update            what the page renders
    POST /api/update/check      look now
    POST /api/update/approve    yes, install it
    POST /api/update/defer      not now  {"hours": 24}
    POST /api/update/skip       never this version
    POST /api/update/cancel     throw away the staged bundle
    POST /api/update/install    unpack it and swap to it
    POST /api/update/revert     go back to the release before this one

    POST /api/v1/pin            {"pin", "new_pin"}: choose your own PIN
    /api/v1/...                 settings and reminders; see v1.py
"""

from firmware.updates import version as version_mod

from . import captive
from .v1 import V1Api

NOT_FOUND = (404, {"error": "no such endpoint"})
BAD_METHOD = (405, {"error": "wrong method"})
UNPAIRED = (401, {"error": "pair with the device first",
                  "needs_pin": True})


class UpdateApi:
    def __init__(self, manager, device=None, auth=None, commander=None):
        self.manager = manager
        self.device = device
        self.v1 = V1Api(self, commander)
        # No auth object means no lock. Only a test does that, and it has to
        # say so explicitly rather than getting it by forgetting an argument.
        self.auth = auth

    def dispatch(self, method, path, body=None, token=None, query=None):
        method = method.upper()
        path = "/" + path.strip("/")
        raw = body
        body = body if isinstance(body, dict) else {}

        if path == "/api/auth":
            if method != "POST":
                return BAD_METHOD
            return self._pair(body)

        if self.auth is not None and not self.auth.check(token):
            return UNPAIRED

        if path == "/api/v1/pin":
            if method != "POST":
                return BAD_METHOD
            return self._change_pin(body, token)

        if path == "/api/v1" or path.startswith("/api/v1/"):
            return self.v1.dispatch(method, path, raw, query)

        if path == "/api/update":
            if method != "GET":
                return BAD_METHOD
            return 200, self.status()

        actions = {
            "/api/update/check": self._check,
            "/api/update/approve": self._approve,
            "/api/update/defer": self._defer,
            "/api/update/skip": self._skip,
            "/api/update/cancel": self._cancel,
            "/api/update/install": self._install,
            "/api/update/revert": self._revert,
        }
        action = actions.get(path)
        if action is None:
            return NOT_FOUND
        if method != "POST":
            return BAD_METHOD
        return action(body)

    def captive(self, host_header):
        """Where a request for `host_header` should be sent instead, or None.
        Only ever a redirect while the setup hotspot is up (`captive.py`).
        Reads one attribute of the device and changes nothing."""
        setup = getattr(self.device, "netsetup", None)
        if setup is None:
            return None
        return captive.redirect(host_header, setup.state)

    def stream(self, path, token=None):
        """`GET /api/v1/events`, which is the one answer that is not a dict.
        Returns None when `path` is not the stream, else (status, body)."""
        if "/" + path.strip("/") != "/api/v1/events":
            return None
        if self.auth is not None and not self.auth.check(token):
            return UNPAIRED
        return self.v1.open_stream()

    # ---- handlers ----

    def _change_pin(self, body, token):
        """The owner's own PIN, in place of the one the device made. It asks
        for the current PIN again, under the same lockout as pairing. Handled
        here and not in a command: the PIN is the web layer's, not the
        device's, and the main thread never sees it."""
        if self.auth is None:
            return 409, {"error": "this device is not locked"}
        ok, wait = self.auth.confirm(body.get("pin"))
        if not ok:
            if wait > 0:
                return 429, {"error": "Too many wrong PINs. Wait a moment.",
                             "retry_after": round(wait, 1)}
            return 403, {"error": "That is not the PIN on the device.",
                         "needs_pin": True}
        problem = self.auth.set_pin(body.get("new_pin"), keep=token)
        if problem:
            return 400, {"error": problem}
        return 200, {"ok": True}

    def _pair(self, body):
        """Swap the PIN shown on the device for a token.

        The wait after a wrong PIN is reported rather than hidden: somebody
        mistyping deserves to know why the next attempt is being ignored, and
        somebody guessing learns nothing they could not measure anyway.
        """
        if self.auth is None:
            return 200, {"token": "", "note": "this device is not locked"}
        # No PIN is not a guess. Counting it would let anything that can
        # reach this address hold the lockout shut without trying a PIN.
        if not isinstance(body.get("pin"), str) or not body["pin"]:
            return 400, {"error": "send the PIN shown on the device"}
        token, wait = self.auth.verify(body.get("pin"))
        if token:
            return 200, {"token": token}
        if wait > 0:
            return 429, {"error": "too many wrong PINs",
                         "retry_after": round(wait, 1)}
        return 401, {"error": "that is not the PIN on the device"}

    def status(self):
        from firmware import product

        out = self.manager.status()
        out["version"] = str(version_mod.current())
        out["product"] = product.identity(self.manager.config.data_root)
        if self.device is not None:
            out["device"] = self.device.summary()
        return out

    def _ok(self, accepted, why=""):
        status = self.status()
        status["accepted"] = bool(accepted)
        if why:
            status["message"] = why
        return (200 if accepted else 409), status

    def _check(self, body):
        started = self.manager.check(force=bool(body.get("force", True)))
        return self._ok(started, "" if started else "a check is already running")

    def _approve(self, body):
        return self._ok(self.manager.approve(),
                        "" if self.manager.release else "nothing to install")

    def _defer(self, body):
        hours = body.get("hours")
        try:
            hours = float(hours) if hours is not None else None
        except (TypeError, ValueError):
            return 400, {"error": "hours must be a number"}
        return self._ok(self.manager.defer(hours))

    def _skip(self, body):
        accepted = self.manager.skip()
        why = ""
        if not accepted and self.manager.release is not None:
            why = "an urgent update cannot be skipped, only postponed"
        return self._ok(accepted, why)

    def _cancel(self, body):
        return self._ok(self.manager.cancel())

    def _install(self, body):
        started = self.manager.install()
        return self._ok(started,
                        "" if started else "nothing is staged to install")

    def _revert(self, body):
        went = self.manager.revert()
        return self._ok(went,
                        "" if went else "there is no earlier release to go "
                                        "back to")

"""The update endpoints, with no web framework in sight.

`UpdateApi.dispatch` takes a method and a path and returns `(status, dict)`.
Flask serves it on the device, the emulator serves it from the standard
library, and a test calls it directly. Putting the logic in a framework would
mean the emulator needed Flask installed to exercise an update flow.

    GET  /api/update            what the page renders
    POST /api/update/check      look now
    POST /api/update/approve    yes, install it
    POST /api/update/defer      not now  {"hours": 24}
    POST /api/update/skip       never this version
    POST /api/update/cancel     throw away the staged bundle
    POST /api/update/install    unpack it and swap to it
    POST /api/update/revert     go back to the release before this one
"""

from firmware.updates import version as version_mod

NOT_FOUND = (404, {"error": "no such endpoint"})
BAD_METHOD = (405, {"error": "wrong method"})


class UpdateApi:
    def __init__(self, manager, device=None):
        self.manager = manager
        self.device = device

    def dispatch(self, method, path, body=None):
        method = method.upper()
        path = "/" + path.strip("/")
        body = body if isinstance(body, dict) else {}

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

    # ---- handlers ----

    def status(self):
        out = self.manager.status()
        out["version"] = str(version_mod.current())
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

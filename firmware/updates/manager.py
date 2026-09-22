"""The update state machine.

The device's own web UI drives this. It calls `check()` when the user presses
the button and `tick()` on a timer; the manager decides whether the user gets
asked or told, and the UI renders whatever `status()` returns.

## Who decides, by urgency

| Urgency | What happens |
|---|---|
| `optional`, `recommended` | The UI asks. Nothing downloads until the user says yes. They can defer, or skip that version for good. |
| `urgent` | The UI says it is going to update. A grace period runs, then it downloads, installs and restarts on its own. It can be pushed back once, by hours, not forever. |

A yes means the whole thing. Pressing "Install 0.4.0" downloads, unpacks and
restarts into it; there is no second button between the download finishing
and the install starting, because nobody pressing the first one meant "fetch
it and ask me again". An urgent release reaches the same place without the
press.

Urgency changes who is asked, never what is checked. An urgent update still
fails preflight on a full SD card or a browning-out board.

## Threads

Checking and downloading are network calls, and the face runs at 30Hz. Both
happen on a worker thread; `tick()` picks the result up on the caller's thread.
Nothing in this module may block the loop that draws the face.
"""

import json
import os
import threading
import time

from . import preflight
from . import version as version_mod
from .installer import (ApplyError, Installer, StagingError,
                        base_version)
from .releases import ReleaseError

IDLE = "idle"
CHECKING = "checking"
UP_TO_DATE = "up_to_date"
AWAITING_CONSENT = "awaiting_consent"
SCHEDULED = "scheduled"
DOWNLOADING = "downloading"
STAGED = "staged"
INSTALLING = "installing"
APPLIED = "applied"
ON_TRIAL = "on_trial"
RESTARTING = "restarting"
BLOCKED = "blocked"
FAILED = "failed"
DEFERRED = "deferred"


class UpdateManager:
    def __init__(self, config, source, system, clock=time.time,
                 on_event=None, installer=None, on_restart=None):
        self.config = config
        self.source = source
        self.system = system
        self.clock = clock
        self.on_event = on_event or (lambda kind, payload: None)

        # The emulator supplies its own installer and its own idea of what a
        # restart is; the device takes the defaults.
        self.installer = installer or Installer(
            source, config.staging_dir, install_root=config.install_root,
            log=lambda line: self._emit("log", {"line": line}))
        self.guard = self.installer.guard
        self.on_restart = on_restart or self.installer.restart

        self.state = IDLE
        self.release = None
        self.staged = None
        self.applied = None
        self.trial = None
        # Whether a staged bundle is meant to go in. Set when the user says
        # yes and when an urgent release runs out its grace period; cleared by
        # a cancel. A bundle staged without it -- left over from a previous
        # boot -- waits to be asked about instead of installing itself.
        self.intent_install = False
        self.checks = None
        self.error = ""
        self.progress = 0.0
        self.deadline = None

        self._lock = threading.Lock()
        self._worker = None
        self._result = None
        self._busy_hint = lambda: False

        self.persisted = _load_state(config.state_path)

    # ---- what the device tells us ----

    def set_busy_hint(self, fn):
        """The device answers "am I mid-conversation" so preflight can say so
        and an automatic download can wait its turn."""
        self._busy_hint = fn

    # ---- queries ----

    @property
    def busy(self):
        return self._worker is not None and self._worker.is_alive()

    def status(self):
        """Everything the web UI renders, in one dict."""
        out = {
            "state": self.state,
            "installed": str(version_mod.current()),
            "channel": self.config.channel,
            "repo": self.config.repo,
            "auto_check": self.config.auto_check,
            "last_check": self.persisted.get("last_check"),
            "error": self.error,
            "progress": round(self.progress, 4),
            "release": self.release.as_dict() if self.release else None,
            "staged": self.staged.as_dict() if self.staged else None,
            "checks": self.checks.as_dict() if self.checks else None,
            "skipped": self.persisted.get("skipped", []),
            "applied": self.applied.as_dict() if self.applied else None,
            "base": base_version(self.config.install_root),
            "on_trial": self.trial.as_dict() if self.trial else None,
            "running_release": self.guard.layout.read_current(),
            "can_revert": len(self._other_releases()) > 0,
            "will_install": self.intent_install,
            "signatures_required": self.config.require_signature,
            "seconds_until_auto": None,
            "decision": None,
        }
        if self.deadline is not None:
            out["seconds_until_auto"] = max(0.0, self.deadline - self.clock())
        if self.release is not None:
            out["decision"] = ("notify" if self.release.urgent else "ask")
        return out

    # ---- the loop calls this ----

    def tick(self):
        """Call once a frame. Applies finished worker results, starts a due
        automatic check, and lets a scheduled urgent update go ahead."""
        self._collect()

        now = self.clock()
        if self.state in (DOWNLOADING, CHECKING, INSTALLING, RESTARTING):
            return

        if self.state == APPLIED:
            # Restarting mid-sentence would cut the user off, so it waits.
            if not self._busy_hint():
                self.restart_now()
            return

        if self.state == SCHEDULED and self.deadline is not None:
            if now >= self.deadline and not self._busy_hint():
                self._start_download()
            return

        if self.state == STAGED:
            # Carry the yes through to the end. Waits for the device to be
            # out of a conversation first: unpacking is minutes of CPU.
            if self.intent_install and not self._busy_hint():
                self.install()
            return

        if self.state in (AWAITING_CONSENT, BLOCKED, ON_TRIAL):
            return

        if self.config.auto_check and self._check_due(now):
            self.check()

    def _check_due(self, now):
        until = self.persisted.get("deferred_until")
        if until and now < until:
            return False
        last = self.persisted.get("last_check") or 0.0
        return now - last >= self.config.check_interval_hours * 3600.0

    # ---- user actions, from the web UI ----

    def check(self, force=False):
        """Look for a newer release. Returns False if one is already running
        or if a deferral is still in effect."""
        if self.busy:
            return False
        if not force and not self._check_due(self.clock()):
            if self.state in (AWAITING_CONSENT, SCHEDULED, STAGED):
                return False
        self.state = CHECKING
        self.error = ""
        self._emit("checking", {})
        self._run(self._do_check)
        return True

    def approve(self):
        """The user said yes, or pressed "update now" during an urgent
        notice."""
        if self.state not in (AWAITING_CONSENT, SCHEDULED, FAILED):
            return False
        if self.release is None:
            return False
        self._start_download()
        return True

    def defer(self, hours=None):
        """Not now. An urgent update gets a shorter ceiling than it asks for,
        because "urgent" that can be postponed forever is just a label."""
        if self.release is None:
            return False
        urgent = self.release.urgent
        hours = self.config.defer_hours if hours is None else float(hours)
        if urgent:
            hours = min(hours, self.config.urgent_max_defer_hours)

        until = self.clock() + hours * 3600.0
        self.persisted["deferred_until"] = until
        _save_state(self.config.state_path, self.persisted)

        if urgent:
            # Still going to happen. The deadline moves; the decision does not.
            self.state = SCHEDULED
            self.deadline = until
        else:
            self.state = DEFERRED
            self.deadline = None
        self._emit("deferred", {"until": until, "urgent": urgent})
        return True

    def skip(self):
        """Never offer this version again. Refused for urgent releases -- the
        user can defer one, but the device does not let them opt out of a fix
        that was marked urgent."""
        if self.release is None or self.release.urgent:
            return False
        skipped = set(self.persisted.get("skipped", []))
        skipped.add(str(self.release.version))
        self.persisted["skipped"] = sorted(skipped)
        _save_state(self.config.state_path, self.persisted)
        self._reset_to(UP_TO_DATE)
        self._emit("skipped", {"version": str(self.release.version)})
        return True

    def cancel(self):
        """Throw away whatever is staged and go back to idle."""
        if self.busy:
            return False
        self.intent_install = False
        self.installer.clear()
        self._reset_to(IDLE)
        self._emit("cancelled", {})
        return True

    def install(self):
        """Unpack the staged bundle and swap `current` to it.

        Runs on the worker thread: unpacking and building a virtualenv take
        minutes on a Pi 3 A+, and the face keeps drawing throughout.
        """
        if self.state != STAGED or self.staged is None or self.busy:
            return False

        # Checked again, because the answers change. The card can have filled
        # up since the download, and writing a release onto a board that is
        # browning out is how one gets bricked.
        self.checks = preflight.run(
            self.system, self.release, self.config.install_root,
            busy=self._busy_hint(), allow_metered=self.config.allow_metered,
            stage=preflight.INSTALL, keys=self.config.trusted_keys(),
            require_signature=self.config.require_signature)
        if not self.checks.ok:
            self.state = BLOCKED
            self.error = self.checks.reason()
            self._emit("blocked", {"reason": self.error})
            return False

        self.state = INSTALLING
        self.error = ""
        self._emit("installing", {"version": str(self.staged.version)})
        staged = self.staged
        self._run(lambda: ("applied", self.installer.apply(staged)))
        return True

    def restart_now(self):
        """Hand over to the release that was just applied.

        Once only. `systemctl restart` takes a moment to arrive, and the loop
        keeps running meanwhile -- without the state change this would fire a
        restart on every frame until the signal landed.
        """
        if self.state != APPLIED or self.applied is None:
            return False
        self.state = RESTARTING
        self._emit("restarting", self.applied.as_dict())
        self.on_restart()
        return True

    def _other_releases(self):
        layout = self.guard.layout
        current = layout.read_current()
        return [n for n in layout.installed() if n != current]

    def revert(self):
        """Go back to the previous release on the user's say-so, rather than
        because this one failed. Cheap, because a rollback is what the
        install already has to be able to do."""
        others = self._other_releases()
        if not others:
            return False
        layout = self.guard.layout
        current = layout.read_current()
        previous = others[-1]
        layout.set_current(previous)
        self.state = RESTARTING
        self._emit("reverted", {"version": previous, "from": current})
        self.on_restart()
        return True

    # ---- the trial a new release has to pass ----

    def begin_trial(self, now):
        """Called once at startup. True when this boot is a release on trial
        and something has to confirm it."""
        armed = self.guard.start_trial(now)
        self.trial = self.guard.read() if armed else None
        if armed:
            self.state = ON_TRIAL
            self._emit("on_trial", self.trial.as_dict())
        return armed

    def trial_tick(self, healthy, now):
        """Called every frame while on trial. Confirms a release that came
        up, and rolls back one that did not."""
        if not self.guard.on_trial():
            return
        if healthy:
            # Report the release that was on trial, not whatever VERSION the
            # running files say -- those are the same thing on the device and
            # are not in the emulator.
            confirmed = self.trial.new if self.trial else ""
            self.guard.confirm()
            self.trial = None
            self.state = UP_TO_DATE
            self._emit("confirmed", {"version": confirmed})
            return
        if not self.guard.overdue(now):
            return

        went_back = self.guard.rollback("did not come up in time")
        self.trial = None
        if went_back is None:
            # Nothing to go back to. Saying so is all that is left; the
            # device keeps running whatever it has.
            self.state = FAILED
            self.error = ("this release did not come up, and there is no "
                          "previous one to return to")
            self._emit("rollback_impossible", {})
            return
        self.state = IDLE
        self.error = ""
        self._emit("rolled_back", {"version": went_back})
        self.on_restart()

    # ---- worker ----

    def _run(self, fn):
        self._worker = threading.Thread(target=self._wrap, args=(fn,),
                                        daemon=True)
        self._worker.start()

    def _wrap(self, fn):
        try:
            result = fn()
        except (ReleaseError, StagingError, ApplyError, OSError) as exc:
            result = ("error", str(exc))
        except Exception as exc:  # a driver or parser we did not anticipate
            result = ("error", "%s: %s" % (type(exc).__name__, exc))
        with self._lock:
            self._result = result

    def _collect(self):
        with self._lock:
            result = self._result
            self._result = None
        if result is None:
            return

        kind, payload = result
        if kind == "error":
            self.error = payload
            self.state = FAILED
            self.progress = 0.0
            self._emit("failed", {"error": payload})
        elif kind == "checked":
            self._on_checked(payload)
        elif kind == "applied":
            self.applied = payload
            self.state = APPLIED
            self.progress = 1.0
            self.installer.clear()
            self._emit("applied", payload.as_dict())
        elif kind == "staged":
            self.staged = payload
            self.state = STAGED
            self.progress = 1.0
            self._emit("staged", payload.as_dict())

    def _do_check(self):
        release = self.source.latest(self.config.channel)
        self.persisted["last_check"] = self.clock()
        _save_state(self.config.state_path, self.persisted)
        return ("checked", release)

    def _on_checked(self, release):
        running = version_mod.current()

        if release is None or release.version <= running:
            self._reset_to(UP_TO_DATE)
            self._emit("up_to_date", {"installed": str(running)})
            return

        if (str(release.version) in self.persisted.get("skipped", [])
                and not release.urgent):
            self._reset_to(UP_TO_DATE)
            self._emit("skipped_version", {"version": str(release.version)})
            return

        self.release = release
        self.checks = preflight.run(
            self.system, release, self.config.install_root,
            busy=self._busy_hint(), allow_metered=self.config.allow_metered,
            keys=self.config.trusted_keys(),
            require_signature=self.config.require_signature)

        if not self.checks.ok:
            self.state = BLOCKED
            self.error = self.checks.reason()
            self._emit("blocked", {"release": release.as_dict(),
                                   "reason": self.error})
            return

        self.error = ""
        if release.urgent:
            self.state = SCHEDULED
            self.deadline = self.clock() + self.config.urgent_grace_seconds
            self._emit("urgent_scheduled", {
                "release": release.as_dict(),
                "seconds": self.config.urgent_grace_seconds})
        else:
            self.state = AWAITING_CONSENT
            self.deadline = None
            self._emit("consent_needed", {"release": release.as_dict()})

    def _start_download(self):
        if self.busy or self.release is None:
            return

        # Re-run preflight: the offer may be hours old and the card may have
        # filled up since.
        self.checks = preflight.run(
            self.system, self.release, self.config.install_root,
            busy=self._busy_hint(), allow_metered=self.config.allow_metered,
            keys=self.config.trusted_keys(),
            require_signature=self.config.require_signature)
        if not self.checks.ok:
            self.state = BLOCKED
            self.error = self.checks.reason()
            self._emit("blocked", {"release": self.release.as_dict(),
                                   "reason": self.error})
            return

        self.persisted.pop("deferred_until", None)
        _save_state(self.config.state_path, self.persisted)

        self.intent_install = True
        self.state = DOWNLOADING
        self.progress = 0.0
        self.error = ""
        self.deadline = None
        release = self.release
        self._emit("downloading", {"release": release.as_dict()})

        def job():
            staged = self.installer.download(release, progress=self._progress)
            return ("staged", staged)

        self._run(job)

    def _progress(self, written, total):
        self.progress = (written / total) if total else 0.0

    # ---- helpers ----

    def _reset_to(self, state):
        self.state = state
        self.release = None
        self.staged = None
        self.checks = None
        self.deadline = None
        self.progress = 0.0
        self.error = ""

    def _emit(self, kind, payload):
        try:
            self.on_event(kind, payload)
        except Exception:
            # A listener that throws must not take the updater down with it.
            pass


def _load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        # The data root also holds API keys and the pairing PIN. Nothing here
        # needs to be readable by every account on the machine.
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, path)
    except OSError:
        # Losing the deferral is survivable; crashing the device over it is not.
        pass

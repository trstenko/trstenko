"""The emulator with no window, for checking the thing actually works.

    python -m tools.emulator.headless

Runs the device loop as fast as it will go against emulated hardware and walks
the paths that are tedious to reach by hand: a full conversation, a failed
turn, the mute switch pulled mid-sentence, an optional update that waits for
consent, an urgent one that does not, a download onto a card with no room, and
a corrupted bundle. Prints a line per step and exits non-zero if any of them
does something other than what it says.

This is the check to run after touching the updater or the device loop.
"""

import io
import os
import shutil
import sys
import tempfile
import threading
import time

from firmware import events as ev
from firmware import pipeline as pipe
from firmware.device import Device
from firmware.hal.base import VOL_DOWN, VOL_UP
from firmware.updates import UpdateConfig, UpdateManager
from firmware.webui import Auth, UpdateApi

from . import audio as audio_mod
from .clock import FakeClock
from .fake_releases import FakeReleases
from .hal import EmulatedHAL
from .pipeline import ScriptedPipeline

FRAME = 1.0 / 30.0


class Rig:
    """The emulator's guts on a clock you control, so a three-second gesture
    takes three simulated seconds and no real ones."""

    def __init__(self, data_root, ssaa=1, **release_kw):
        self.t = 1000.0
        self.hal = EmulatedHAL()
        self.hal.now = lambda: self.t

        self.releases = FakeReleases(kbps=1e9, clock=lambda: self.t,
                                     **release_kw)
        self.config = UpdateConfig(repo="emulator/trstenko",
                                   data_root=data_root,
                                   install_root=data_root, auto_check=False,
                                   urgent_grace_seconds=10.0)
        self.events = []
        self.restarts = 0
        self.updater = self._manager()

        self.pipeline = ScriptedPipeline(self.hal.speaker, seed=7)
        # Things to advance alongside the device, for rigs that add one.
        self.hooks = []
        self.logs = []
        self.ssaa = ssaa
        self.device = Device(self.hal, self.pipeline, self.updater, ssaa=ssaa,
                             on_log=self.logs.append)

    def _manager(self):
        return UpdateManager(
            self.config, self.releases, self.hal.system,
            clock=lambda: self.t,
            on_event=lambda kind, payload: self.events.append(kind),
            on_restart=self._restart)

    def _restart(self):
        """Stand in for `systemctl restart`. The scenario decides when the
        replacement process actually comes up, because that gap is where the
        interesting failures live."""
        self.restarts += 1

    def reboot(self, healthy=True):
        """Start the firmware again, the way systemd would after a restart.

        A fresh manager and a fresh Device against the same install root: the
        new process reads the marker off disk and works out for itself that
        it is on trial.
        """
        self.updater = self._manager()
        self.device = Device(self.hal, self.pipeline, self.updater,
                             ssaa=self.ssaa, on_log=self.logs.append)
        if not healthy:
            # A release that starts but never gets anywhere -- a config it
            # cannot parse, a server that never binds. It draws, so it is not
            # dead; it just never becomes a working device.
            self.device.healthy = lambda now=None: False
        return self.device

    def step(self, seconds):
        """Advance simulated time, ticking the device at 30Hz throughout."""
        target = self.t + seconds
        while self.t < target:
            self.t += FRAME
            for hook in self.hooks:
                hook(FRAME)
            self.device.tick()

    def settle(self, predicate, timeout=30.0):
        """Tick until something the worker thread is doing lands."""
        deadline = time.monotonic() + timeout
        while not predicate():
            self.t += FRAME
            self.device.tick()
            time.sleep(0.001)  # the download runs on another thread
            if time.monotonic() > deadline:
                return False
        return True


class Report:
    def __init__(self):
        self.failed = 0

    def check(self, label, ok, detail=""):
        print("%-4s %s%s" % ("ok" if ok else "FAIL", label,
                             ("  -- " + detail) if detail and not ok else ""))
        if not ok:
            self.failed += 1

    def done(self):
        print("\n%s" % ("all checks passed" if not self.failed
                        else "%d check(s) failed" % self.failed))
        return 1 if self.failed else 0


def scenario_boot_and_talk(report, root):
    rig = Rig(os.path.join(root, "talk"))
    rig.step(0.2)
    report.check("boots into the boot face",
                 rig.device.face.current == "face_boot",
                 rig.device.face.current)

    rig.step(4.0)
    report.check("lands in idle", rig.device.face.current == "face_idle",
                 rig.device.face.current)

    rig.pipeline.trigger()
    rig.step(0.3)
    report.check("wake word starts listening",
                 rig.device.face.current == "face_listening",
                 rig.device.face.current)

    seen = set()
    for _ in range(400):
        rig.step(0.1)
        seen.add(rig.device.face.current)
        if rig.device.turn is None:
            break
    report.check("the turn goes listening -> thinking -> talking -> idle",
                 {"face_listening", "face_thinking", "face_talking"} <= seen,
                 str(sorted(seen)))
    report.check("returns to idle", rig.device.face.current == "face_idle",
                 rig.device.face.current)


def scenario_mute(report, root):
    rig = Rig(os.path.join(root, "mute"))
    rig.step(4.0)
    rig.pipeline.trigger()
    rig.step(1.0)

    rig.hal.set_mute(True)
    rig.step(0.5)
    report.check("the switch shows the muted face",
                 rig.device.face.current == "face_muted",
                 rig.device.face.current)
    report.check("muting cancels the turn in flight", rig.device.turn is None)

    rig.pipeline.trigger()
    rig.step(1.0)
    report.check("a muted device cannot be woken", rig.device.turn is None)

    rig.hal.set_mute(False)
    rig.step(1.0)
    report.check("unmuting comes back to idle",
                 rig.device.face.current == "face_idle",
                 rig.device.face.current)


def scenario_failed_turn(report, root):
    rig = Rig(os.path.join(root, "fail"))
    rig.step(4.0)
    rig.pipeline.fail_next = pipe.THINKING
    rig.pipeline.trigger()

    for _ in range(300):
        rig.step(0.1)
        if rig.device.face.current == "face_error":
            break
    report.check("a failed turn shows the error face",
                 rig.device.face.current == "face_error",
                 rig.device.face.current)

    rig.step(5.0)
    report.check("the error face gives up after a few seconds",
                 rig.device.face.current == "face_idle",
                 rig.device.face.current)


def scenario_setup_gesture(report, root):
    rig = Rig(os.path.join(root, "setup"))
    rig.step(4.0)

    rig.hal.press(VOL_UP)
    rig.step(0.3)
    volume_before = rig.hal.speaker.get_volume()
    rig.hal.release(VOL_UP)
    rig.step(0.1)
    report.check("a short volume press steps the volume",
                 rig.hal.speaker.get_volume() > volume_before)

    volume = rig.hal.speaker.get_volume()
    rig.hal.press(VOL_DOWN)
    rig.hal.press(VOL_UP)
    rig.step(3.5)
    rig.hal.release(VOL_DOWN)
    rig.hal.release(VOL_UP)
    rig.step(0.2)

    report.check("holding both volume buttons raises setup",
                 rig.device.setup_mode)
    report.check("the setup gesture does not also change the volume",
                 abs(rig.hal.speaker.get_volume() - volume) < 1e-9,
                 "%.2f -> %.2f" % (volume, rig.hal.speaker.get_volume()))


def scenario_optional_update(report, root):
    rig = Rig(os.path.join(root, "optional"), version="0.4.0",
              urgency="optional", size_mb=4.0)
    rig.step(1.0)

    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")
    report.check("an optional update waits for consent",
                 rig.updater.state == "awaiting_consent", rig.updater.state)

    rig.updater.defer(24)
    report.check("deferring stops it there", rig.updater.state == "deferred",
                 rig.updater.state)
    report.check("and nothing is queued to install",
                 rig.updater.intent_install is False)

    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")
    rig.updater.approve()
    ok = rig.settle(lambda: rig.updater.state in ("staged", "installing",
                                                  "failed"))
    report.check("approving downloads and verifies it",
                 ok and rig.updater.state != "failed",
                 rig.updater.error or rig.updater.state)
    report.check("the staged bundle is on disk",
                 rig.updater.staged is not None
                 and os.path.exists(rig.updater.staged.path))

    # This scenario's feed serves filler rather than a real archive, which
    # is the other thing that has to fail safely -- and it now happens on its
    # own, without anyone pressing install.
    rig.settle(lambda: rig.updater.state in ("failed", "applied"))
    report.check("a bundle that is not an archive is rejected",
                 rig.updater.state == "failed"
                 and "unpack" in rig.updater.error, rig.updater.error[:60])
    report.check("and nothing was swapped",
                 rig.updater.guard.layout.read_current() is None)


def scenario_urgent_update(report, root):
    rig = Rig(os.path.join(root, "urgent"), version="0.5.0", urgency="urgent",
              size_mb=2.0)
    rig.step(1.0)

    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")
    report.check("an urgent update is scheduled, not offered",
                 rig.updater.state == "scheduled", rig.updater.state)
    report.check("it cannot be skipped", rig.updater.skip() is False)

    rig.updater.defer(999)
    capped = rig.updater.deadline - rig.t
    report.check("deferring an urgent update is capped",
                 capped <= rig.config.urgent_max_defer_hours * 3600 + 1,
                 "%.0f s" % capped)
    report.check("it stays scheduled after a deferral",
                 rig.updater.state == "scheduled", rig.updater.state)

    # Let the deferral run out.
    rig.t += capped + 1
    ok = rig.settle(lambda: rig.updater.state in ("staged", "failed",
                                                  "downloading"))
    ok = ok and rig.settle(lambda: rig.updater.state in ("staged", "failed",
                                                         "installing"))
    report.check("it downloads itself once the grace period is up",
                 "staged" in rig.events, str(sorted(set(rig.events))))


def scenario_urgent_end_to_end(report, root):
    """Nobody touches it. An urgent release has to reach the point of
    running on its own -- that is what marking one urgent means."""
    rig = Rig(os.path.join(root, "urgent-auto"), version="0.7.0",
              urgency="urgent", size_mb=0.4, real_bundle=True)
    rig.step(4.0)

    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")
    report.check("it is scheduled rather than offered",
                 rig.updater.state == "scheduled", rig.updater.state)

    rig.t += rig.config.urgent_grace_seconds + 1
    ok = rig.settle(lambda: rig.updater.state in ("applied", "restarting",
                                                  "failed", "blocked"),
                    timeout=60)
    report.check("it downloads, installs and restarts with nobody present",
                 ok and rig.updater.state in ("applied", "restarting"),
                 rig.updater.error or rig.updater.state)
    report.check("current points at the new release",
                 rig.updater.guard.layout.read_current() == "0.7.0",
                 str(rig.updater.guard.layout.read_current()))
    report.check("and a rollback is armed for it",
                 rig.updater.guard.read() is not None)


def scenario_urgent_still_checked(report, root):
    """Urgent skips asking the user. It does not skip the checks."""
    rig = Rig(os.path.join(root, "urgent-checked"), version="0.7.0",
              urgency="urgent", size_mb=0.4, real_bundle=True)
    rig.step(4.0)
    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")

    rig.t += rig.config.urgent_grace_seconds + 1
    rig.settle(lambda: rig.updater.state == "downloading")
    # Hold it at the staged step so the install can be examined on its own
    # rather than racing the hand-off out of the download.
    rig.updater.intent_install = False
    rig.settle(lambda: rig.updater.state in ("staged", "failed"))

    # The supply browns out before it is written.
    rig.hal.system.is_undervolt = True
    started = rig.updater.install()
    report.check("an under-voltage board stops even an urgent install",
                 started is False and rig.updater.state == "blocked",
                 rig.updater.state)
    report.check("and says why", "under-voltage" in rig.updater.error,
                 rig.updater.error[:60])
    report.check("nothing was swapped",
                 rig.updater.guard.layout.read_current() is None,
                 str(rig.updater.guard.layout.read_current()))

    # Sort the supply out and it goes through.
    rig.hal.system.is_undervolt = False
    rig.updater.state = "staged"
    rig.updater.install()
    ok = rig.settle(lambda: rig.updater.state in ("applied", "restarting",
                                                  "failed"), timeout=60)
    report.check("fixing it lets the update through",
                 ok and rig.updater.state in ("applied", "restarting"),
                 rig.updater.error or rig.updater.state)


def scenario_install_waits_for_the_turn(report, root):
    """Unpacking is minutes of CPU on a Pi 3 A+. Not during a sentence.

    The download waits for a turn as well -- it competes with the STT and
    model calls for one wifi radio -- so this drives the two separately.
    """
    rig = Rig(os.path.join(root, "busy"), version="0.7.0", urgency="urgent",
              size_mb=0.4, real_bundle=True)
    rig.step(4.0)
    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")

    rig.t += rig.config.urgent_grace_seconds + 1
    rig.settle(lambda: rig.updater.state == "downloading")
    # Hold it at staged so the install can be examined on its own.
    rig.updater.intent_install = False
    rig.settle(lambda: rig.updater.state in ("staged", "failed"))
    report.check("the bundle is staged and waiting",
                 rig.updater.state == "staged", rig.updater.state)

    rig.pipeline.trigger()
    rig.step(0.3)
    # Hold them mid-sentence: `settle` spins the simulated clock while it
    # waits on a worker thread, and a normal turn would be long over.
    rig.device.turn._listen_for = 1e6
    report.check("a turn is running", rig.device.turn is not None)

    rig.updater.intent_install = True
    rig.step(2.0)
    report.check("the install waits for it",
                 rig.updater.state == "staged" and rig.device.turn is not None,
                 rig.updater.state)

    rig.device.turn._listen_for = 0.01
    for _ in range(400):
        rig.step(0.1)
        if rig.device.turn is None:
            break
    ok = rig.settle(lambda: rig.updater.state in ("applied", "restarting",
                                                  "failed"), timeout=60)
    report.check("and goes ahead once the turn is over",
                 ok and rig.updater.state in ("applied", "restarting"),
                 rig.updater.error or rig.updater.state)


def scenario_signing(report, root):
    """A checksum says the bundle arrived intact. A signature says who made
    it, and that is the one that matters when the download came from the
    same place the checksum did."""
    data = os.path.join(root, "signing")
    rig = Rig(data, version="0.8.0", size_mb=0.3, real_bundle=True)
    # The device trusts the feed's throwaway key, as a real one trusts the
    # key the team generated with `python -m tools.release keygen`.
    rig.config.extra_keys = [rig.releases.public_key]
    rig.step(4.0)

    report.check("with a key configured, signatures are required",
                 rig.config.require_signature)

    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")
    report.check("a properly signed release is offered",
                 rig.updater.state == "awaiting_consent", rig.updater.state)
    report.check("and says who signed it",
                 rig.updater.release.signed
                 and rig.updater.release.key_id, rig.updater.release.key_id)

    # Signed, then edited. The bundle still hashes correctly; the manifest
    # no longer matches what was signed.
    rig.releases.break_signature = True
    rig.updater.cancel()
    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")
    report.check("a manifest altered after signing is refused",
                 rig.updater.state == "blocked", rig.updater.state)
    report.check("and says the signature did not check out",
                 "trust" in rig.updater.error or "signed" in rig.updater.error,
                 rig.updater.error[:70])

    # Signed by somebody else entirely.
    rig.releases.break_signature = False
    from tools.release import keypair
    rig.releases.seed = keypair()[0]
    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")
    report.check("a release signed with an unknown key is refused",
                 rig.updater.state == "blocked", rig.updater.state)

    # And with no keys at all, it installs but nobody is pretending.
    rig.config.extra_keys = []
    rig.releases.sign_releases = False
    report.check("with no keys, signatures are not required",
                 rig.config.require_signature is False)
    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")
    report.check("an unsigned release is allowed",
                 rig.updater.state == "awaiting_consent", rig.updater.state)
    report.check("but is marked unsigned rather than passed off as fine",
                 rig.updater.release.signed is False
                 and "no trusted keys" in rig.updater.release.signature_note,
                 rig.updater.release.signature_note)


def scenario_no_room(report, root):
    rig = Rig(os.path.join(root, "full"), version="0.4.0", size_mb=200.0)
    rig.hal.system.free_mb = 120
    rig.step(1.0)

    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")
    report.check("no room on the card blocks the update",
                 rig.updater.state == "blocked", rig.updater.state)
    report.check("and says how much it needed",
                 "MB free" in rig.updater.error, rig.updater.error)

    rig.hal.system.free_mb = 4096
    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")
    report.check("clearing space unblocks it",
                 rig.updater.state == "awaiting_consent", rig.updater.state)


def scenario_offline_and_corrupt(report, root):
    rig = Rig(os.path.join(root, "offline"), version="0.4.0", size_mb=2.0)
    rig.hal.system.is_online = False
    rig.releases.offline = True
    rig.step(1.0)

    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")
    report.check("a check with no network fails rather than hanging",
                 rig.updater.state == "failed", rig.updater.state)

    rig.hal.system.is_online = True
    rig.releases.offline = False
    rig.releases.corrupt = True
    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")
    rig.updater.approve()
    ok = rig.settle(lambda: rig.updater.state in ("staged", "failed"))
    report.check("a corrupted download is caught by its checksum",
                 ok and rig.updater.state == "failed", rig.updater.state)
    report.check("and nothing is left staged",
                 rig.updater.installer.staged_bytes() == 0)


def _install(rig, report, label):
    """Say yes once, and let it run to the end. Returns the Applied record."""
    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")
    rig.updater.approve()
    ok = rig.settle(lambda: rig.updater.state in ("applied", "restarting", "failed", "blocked"))
    report.check("%s: one yes carries it through to installed" % label,
                 ok and rig.updater.state in ("applied", "restarting"),
                 rig.updater.error or rig.updater.state)
    return rig.updater.applied


def scenario_install(report, root):
    """The whole thing: unpack, swap, restart, confirm."""
    data = os.path.join(root, "install")
    rig = Rig(data, version="0.4.0", size_mb=0.4, real_bundle=True)
    layout = rig.updater.guard.layout
    rig.step(4.0)

    applied = _install(rig, report, "first install")
    report.check("it downloaded, unpacked and swapped without a second press",
                 {"downloading", "staged", "installing", "applied"}
                 <= set(rig.events), str(sorted(set(rig.events))))
    report.check("the release is unpacked on disk",
                 os.path.isdir(layout.release_dir("0.4.0"))
                 and os.path.exists(os.path.join(
                     layout.release_dir("0.4.0"), "firmware", "main.py")))
    report.check("current points at it", layout.read_current() == "0.4.0",
                 str(layout.read_current()))
    report.check("the recovery script is kept outside releases/",
                 os.path.exists(os.path.join(layout.bin_dir, "bootcheck.py"))
                 and os.path.exists(os.path.join(layout.bin_dir, "boot.py")))
    report.check("nothing is left in staging",
                 rig.updater.installer.staged_bytes() == 0)

    # It restarts on its own once the device is not mid-sentence.
    rig.step(0.5)
    report.check("it restarts itself when idle", rig.restarts == 1,
                 str(rig.restarts))

    # Second release, so there is something to roll back to.
    rig.releases.version = "0.5.0"
    applied = _install(rig, report, "second install")
    report.check("the second install records what it replaced",
                 applied is not None and applied.previous == "0.4.0",
                 applied and applied.previous)
    report.check("a rollback is armed", rig.updater.guard.read() is not None)

    rig.step(0.5)
    device = rig.reboot(healthy=True)
    report.check("the new release knows it is on trial",
                 rig.updater.state == "on_trial", rig.updater.state)

    rig.step(6.0)
    report.check("coming up confirms it",
                 rig.updater.guard.read() is None
                 and rig.updater.state != "on_trial", rig.updater.state)
    report.check("the previous release is kept for a rollback",
                 os.path.isdir(layout.release_dir("0.4.0")))


def scenario_rollback(report, root):
    """A release that starts and never becomes a working device."""
    data = os.path.join(root, "rollback")
    rig = Rig(data, version="0.4.0", size_mb=0.4, real_bundle=True)
    rig.step(4.0)
    _install(rig, report, "baseline")
    rig.step(0.5)

    rig.releases.version = "0.5.0"
    _install(rig, report, "the one that fails")
    rig.step(0.5)
    layout = rig.updater.guard.layout
    report.check("current moved to the new release",
                 layout.read_current() == "0.5.0", str(layout.read_current()))

    before = rig.restarts
    rig.reboot(healthy=False)
    report.check("it is on trial", rig.updater.state == "on_trial",
                 rig.updater.state)

    rig.step(30.0)
    report.check("it waits out the deadline before giving up",
                 rig.updater.state == "on_trial", rig.updater.state)

    rig.step(100.0)
    report.check("it rolls back on its own",
                 layout.read_current() == "0.4.0", str(layout.read_current()))
    report.check("and restarts into the old release",
                 rig.restarts > before, str(rig.restarts - before))
    report.check("the marker is cleared", rig.updater.guard.read() is None)


def scenario_bootcheck(report, root):
    """The release is so broken that none of its code runs. The rollback has
    to come from outside it."""
    from firmware.updates import bootcheck

    data = os.path.join(root, "bootcheck")
    rig = Rig(data, version="0.4.0", size_mb=0.4, real_bundle=True)
    rig.step(4.0)
    _install(rig, report, "baseline")
    rig.step(0.5)

    rig.releases.version = "0.5.0"
    rig.releases.broken_release = True
    _install(rig, report, "a release that cannot import")
    rig.step(0.5)

    layout = rig.updater.guard.layout
    report.check("the broken release is what current points at",
                 layout.read_current() == "0.5.0", str(layout.read_current()))

    # systemd restarts it; the app dies before anything of ours runs, so the
    # only thing that executes is bootcheck.
    attempts = 0
    for _ in range(6):
        attempts += 1
        bootcheck.main([data])
        if layout.read_current() == "0.4.0":
            break
    report.check("bootcheck gives up and rolls back",
                 layout.read_current() == "0.4.0", str(layout.read_current()))
    report.check("after a few attempts, not the first",
                 2 <= attempts <= 5, "%d boots" % attempts)
    report.check("the marker is gone afterwards",
                 rig.updater.guard.read() is None)


def scenario_base_image(report, root):
    """The honest limit: a release that needs a newer OS is refused, not
    half-applied."""
    data = os.path.join(root, "base")
    rig = Rig(data, version="0.6.0", size_mb=0.3, real_bundle=True)
    rig.releases.requires_base = 99
    rig.step(4.0)

    rig.updater.check(force=True)
    rig.settle(lambda: rig.updater.state != "checking")
    rig.updater.approve()
    rig.settle(lambda: rig.updater.state in ("staged", "failed"))
    rig.updater.install()
    rig.settle(lambda: rig.updater.state not in ("installing",))

    report.check("it refuses rather than applying",
                 rig.updater.state == "failed", rig.updater.state)
    report.check("and says the card needs reflashing",
                 "reflash" in rig.updater.error, rig.updater.error[:70])
    report.check("nothing was swapped",
                 rig.updater.guard.layout.read_current() is None,
                 str(rig.updater.guard.layout.read_current()))


def scenario_hostile_bundle(report, root):
    """A bundle that writes outside its own directory is refused."""
    import tarfile as tar

    from firmware.updates.installer import Installer, Staged, StagingError

    data = os.path.join(root, "hostile")
    os.makedirs(data, exist_ok=True)
    path = os.path.join(data, "evil.tar.gz")
    with tar.open(path, "w:gz") as archive:
        info = tar.TarInfo("../escaped.txt")
        info.size = 4
        archive.addfile(info, io.BytesIO(b"evil"))

    installer = Installer(None, os.path.join(data, "staging"),
                          install_root=data)
    caught = ""
    try:
        installer.apply(Staged(path, "9.9.9", "", 0))
    except StagingError as exc:
        caught = str(exc)
    report.check("a bundle escaping its directory is refused",
                 "outside" in caught, caught[:60])
    report.check("and nothing landed outside",
                 not os.path.exists(os.path.join(root, "escaped.txt")))


def scenario_pairing(report, root):
    """The page is reachable by anyone on the same Wi-Fi. Without a lock,
    so is installing firmware on it."""
    data = os.path.join(root, "pairing")
    rig = Rig(data, version="0.4.0", size_mb=0.3, real_bundle=True)
    clock = [1000.0]
    auth = Auth(data, clock=lambda: clock[0])
    api = UpdateApi(rig.updater, rig.device, auth)
    rig.step(4.0)

    report.check("the PIN is six digits and not a fixed one",
                 len(auth.pin) == 6 and auth.pin.isdigit(), auth.pin)

    status, body = api.dispatch("GET", "/api/update")
    report.check("an unpaired caller is refused",
                 status == 401 and body.get("needs_pin"), str(status))

    status, body = api.dispatch("POST", "/api/update/check", {}, "made-up")
    report.check("so is one waving an invented token", status == 401,
                 str(status))

    status, body = api.dispatch("POST", "/api/auth", {"pin": "000000"})
    wrong_ok = status == 401 or (status == 429)
    report.check("a wrong PIN gets nothing", wrong_ok and "token" not in body,
                 str(status))

    # Keep guessing. The device has to stop answering rather than let a
    # script walk through a million combinations.
    for _ in range(6):
        api.dispatch("POST", "/api/auth", {"pin": "111111"})
    status, body = api.dispatch("POST", "/api/auth", {"pin": auth.pin})
    report.check("guessing locks it, even against the right PIN",
                 status == 429 and body.get("retry_after", 0) > 0,
                 str(status))

    clock[0] += 3600.0
    status, body = api.dispatch("POST", "/api/auth", {"pin": auth.pin})
    token = body.get("token", "")
    report.check("the right PIN pairs once the wait is over",
                 status == 200 and len(token) == 64, str(status))

    status, body = api.dispatch("GET", "/api/update", {}, token)
    report.check("and the token opens the door", status == 200, str(status))

    status, body = api.dispatch("GET", "/api/update", {}, token)
    report.check("the status carries who made the device",
                 body["product"]["serial"] and body["product"]["support_until"],
                 str(body.get("product")))

    clock[0] += 25 * 3600.0
    status, body = api.dispatch("GET", "/api/update", {}, token)
    report.check("a token does not last forever", status == 401, str(status))

    # Handing the device on: the old pairing has to stop working.
    clock[0] += 1
    fresh, _ = auth.verify(auth.pin)
    old_pin = auth.pin
    auth.reset_pin()
    report.check("resetting makes a new PIN", auth.pin != old_pin)
    status, _ = api.dispatch("GET", "/api/update", {}, fresh)
    report.check("and drops everyone paired with the old one",
                 status == 401, str(status))


def scenario_web_api(report, root):
    rig = Rig(os.path.join(root, "web"), version="0.4.0", size_mb=0.3,
              real_bundle=True)
    api = UpdateApi(rig.updater, rig.device)
    rig.step(1.0)

    status, body = api.dispatch("GET", "/api/update")
    report.check("the page can read the device's state",
                 status == 200 and body["state"] and body["device"]["face"],
                 str(status))

    api.dispatch("POST", "/api/update/check", {})
    rig.settle(lambda: rig.updater.state != "checking")
    status, body = api.dispatch("GET", "/api/update")
    report.check("a check through the API finds the release",
                 body["release"] is not None and body["decision"] == "ask",
                 str(body["state"]))

    status, body = api.dispatch("POST", "/api/update/approve", {})
    rig.settle(lambda: rig.updater.state in ("staged", "failed"))

    rig.settle(lambda: rig.updater.state in ("applied", "restarting",
                                             "failed"), timeout=60)
    status, body = api.dispatch("GET", "/api/update")
    report.check("and sees it applied, waiting to restart",
                 body["state"] in ("applied", "restarting")
                 and body["applied"]["version"] == "0.4.0",
                 "%s %s" % (body["state"], rig.updater.error))
    report.check("the page is told which base image the card has",
                 isinstance(body.get("base"), int), str(body.get("base")))

    status, body = api.dispatch("POST", "/api/update/install", {})
    report.check("asking to install again is refused, with a reason",
                 status == 409 and body.get("message"), str(status))

    status, body = api.dispatch("GET", "/api/nope")
    report.check("an unknown endpoint is a 404", status == 404)


def scenario_blit(report, root):
    """The panel only shows what was pushed. If a state animates outside the
    region it declared, the difference is visible here and on the desk."""
    rig = Rig(os.path.join(root, "blit"))
    rig.step(6.0)
    panel = rig.hal.panel
    report.check("frames reach the panel", panel.pushes > 100,
                 str(panel.pushes))
    report.check("the dirty rect stays inside the blit region",
                 panel.last_bbox is None
                 or _inside(panel.last_bbox, rig.device.face.blit()),
                 "%s vs %s" % (panel.last_bbox, rig.device.face.blit()))

    per_frame = panel.bytes_pushed / max(1, panel.pushes)
    report.check("a frame costs less than the full screen",
                 per_frame < 320 * 240 * 2,
                 "%.0f bytes/frame" % per_frame)


class CannedInput(audio_mod.Input):
    """A microphone with a script. Stands in for a sound card so the audio
    path can be tested on a machine with no microphone, and so an utterance
    is the same every run."""

    name = "canned"
    label = "canned"
    live = True

    def __init__(self):
        self.talking = False
        self.buffer = bytearray()

    def advance(self, seconds):
        """Produce this much audio, the way a sound card would. Producing it
        on demand instead would let a reader pull an unlimited amount."""
        self.buffer += b"" * int(audio_mod.RATE * seconds)

    def read(self, frames=audio_mod.CHUNK_FRAMES):
        want = int(frames) * audio_mod.SAMPLE_BYTES
        out = bytes(self.buffer[:want])
        del self.buffer[:want]
        return out

    def level(self):
        return 0.5 if self.talking else 0.0


def scenario_audio(report, root):
    rig = Rig(os.path.join(root, "audio"))
    canned = CannedInput()
    bridge = audio_mod.AudioBridge(rig.hal.mic, rig.hal.speaker)
    bridge.input = canned
    rig.pipeline.bridge = bridge
    rig.hooks.append(lambda dt: (canned.advance(dt), bridge.pump()))

    rig.step(4.0)
    canned.talking = True
    rig.step(0.2)
    report.check("a live input reaches the mic the firmware reads",
                 rig.hal.mic.level() > 0.0 and len(rig.hal.mic.read(160)) > 0,
                 "level %.2f" % rig.hal.mic.level())

    rig.pipeline.trigger()
    rig.step(0.2)
    report.check("the listening face follows the room",
                 rig.device.turn is not None
                 and abs(rig.device.turn.level() - 0.5) < 0.01,
                 str(rig.device.turn and rig.device.turn.level()))

    rig.step(1.2)
    report.check("it keeps listening while someone is talking",
                 rig.device.turn is not None
                 and rig.device.turn.stage == "listening",
                 rig.device.turn and rig.device.turn.stage)

    canned.talking = False
    for _ in range(200):
        rig.step(0.1)
        if rig.device.turn is None or rig.device.turn.stage != "listening":
            break
    turn = rig.device.turn
    report.check("silence ends the utterance",
                 turn is not None and turn.stage == "thinking",
                 turn and turn.stage)

    heard = len(turn._captured) / float(audio_mod.RATE * 2) if turn else 0.0
    # 0.25s of pre-roll, 1.4s of talking, and the 0.7s of quiet that ended it.
    report.check("it recorded the utterance and not the pause",
                 1.5 < heard < 3.0, "%.2fs" % heard)

    for _ in range(400):
        rig.step(0.05)
        if rig.device.turn is None or rig.device.turn.stage == "speaking":
            break
    report.check("the reply is the audio it heard, played back",
                 rig.hal.speaker.last_played == len(turn._captured),
                 "%d vs %d" % (rig.hal.speaker.last_played,
                               len(turn._captured)))

    rig.hal.set_mute(True)
    rig.step(0.5)
    report.check("muting stops audio reaching the firmware",
                 rig.hal.mic.level() == 0.0
                 and rig.hal.mic.read(160) == b"")


def scenario_no_ghosts(report, root):
    """The panel holds its pixels. Anything the firmware lights up it also has
    to clean up, or the boot animation leaves a trail and the mic glyph stays
    on screen after the switch goes back."""
    # ssaa 2 is what the device runs, and it is where this goes wrong: the
    # downscale filter reaches past the bbox the renderer reports.
    rig = Rig(os.path.join(root, "ghosts"), ssaa=2)

    # Boot's eyes rise from y 301, well below where idle ever draws.
    rig.step(8.0)
    report.check("boot leaves no trail below the face",
                 not _lit(rig.hal.panel.frame, (0, 200, 320, 240)),
                 "pixels still lit under y 200")

    rig.hal.set_mute(True)
    rig.step(3.0)
    report.check("the mic glyph is on screen while muted",
                 _lit(rig.hal.panel.frame, (140, 110, 180, 190)))

    rig.hal.set_mute(False)
    rig.step(3.0)
    report.check("and gone once the switch goes back",
                 not _lit(rig.hal.panel.frame, (150, 150, 170, 190)),
                 "the mic stem or base is still lit")

    # The eyes rest between y 60 and 152. Anything lit below that is an
    # outline a moving shape left behind.
    report.check("no outline is left under the eyes",
                 not _lit(rig.hal.panel.frame, (48, 156, 272, 192)),
                 "pixels lit between the eyes and the bottom of the region")


def _lit(frame, box):
    """Is anything drawn in this box? The background is the only dark thing on
    this screen, so 'brighter than the background' is enough."""
    region = frame.crop(box)
    return max(sum(p) for p in region.getdata()) > sum((18, 25, 29)) + 24


def _inside(box, region):
    return (box[0] >= region[0] and box[1] >= region[1]
            and box[2] <= region[2] and box[3] <= region[3])


def scenario_events(report, root):
    queue = ev.EventQueue()

    # A post from another thread has to end a long wait at once, or a wake
    # word at 2Hz would be half a second late for no reason.
    threading.Timer(0.05, lambda: queue.post(ev.Event(ev.WAKE_WORD))).start()
    start = time.monotonic()
    queue.wait(5.0)
    waited = time.monotonic() - start
    report.check("a post from another thread wakes the wait at once",
                 0.02 < waited < 1.0, "%.2fs" % waited)
    report.check("waiting does not take the event off the queue",
                 len(queue) == 1)

    queue.drain()
    start = time.monotonic()
    queue.wait(0.1)
    report.check("an empty queue waits out its timeout",
                 time.monotonic() - start >= 0.09)

    queue.post(ev.Event(ev.CLOCK_SYNCED))
    start = time.monotonic()
    queue.wait(5.0)
    report.check("waiting on a queue that already has something returns at once",
                 time.monotonic() - start < 0.5)
    queue.drain()

    for n in range(120):
        queue.post(ev.Event(ev.SENTENCE, data=n))
    first = queue.drain(ev.DRAIN_LIMIT)
    report.check("drain stops at its limit", len(first) == 50, str(len(first)))
    report.check("and the rest wait for the next frame", len(queue) == 70)
    report.check("oldest first", [e.data for e in first] == list(range(50)))
    report.check("a smaller limit is respected", len(queue.drain(5)) == 5)

    queue.drain(1000)
    posters = [threading.Thread(
        target=lambda k=k: [queue.post(ev.Event(ev.SENTENCE, data=(k, i)))
                            for i in range(200)]) for k in range(4)]
    for t in posters:
        t.start()
    for t in posters:
        t.join()
    got = []
    while len(queue):
        got += queue.drain()
    report.check("four threads posting at once lose nothing", len(got) == 800,
                 str(len(got)))

    cmd = ev.Command("set_volume", 0.4)
    queue.post(cmd)

    def main_thread():
        item = queue.drain()[0]
        item.reply({"volume": item.args})

    threading.Thread(target=main_thread).start()
    try:
        answer = cmd.wait(2.0)
    except ev.CommandTimeout:
        answer = None
    report.check("a command's reply reaches the thread that asked",
                 answer == {"volume": 0.4}, str(answer))

    refused = ev.Command("settings.update", {"volume": 9})
    refused.fail("volume must be between 0 and 1")
    try:
        refused.wait(0.1)
        message = ""
    except ev.CommandError as exc:
        message = str(exc)
    report.check("a refusal comes back as an error with its message",
                 message == "volume must be between 0 and 1", message)

    ignored = ev.Command("set_volume", 0.1)
    start = time.monotonic()
    try:
        ignored.wait(0.15)
        timed_out = False
    except ev.CommandTimeout:
        timed_out = True
    report.check("a command nobody answers times out",
                 timed_out and time.monotonic() - start >= 0.1)

    report.check("every kind the plans name is a constant",
                 {"WakeWord", "SpeechStarted", "SpeechEnded", "MicLost",
                  "MicBack", "TurnProgress", "Sentence", "TurnDone",
                  "TurnFailed", "SpeakingStarted", "SpeakingDone", "TtsDied",
                  "SpeakingLevel", "Due", "ClockSynced", "NetworkChanged",
                  "LowVoltage", "Hot", "Shutdown",
                  "WorkerDied"} == set(ev.KINDS))


def scenario_fake_clock(report, root):
    """The device on clocks the test moves. No real second passes."""
    clock = FakeClock(mono=5000.0, wall=1_790_000_000.0, synced=False)
    hal = EmulatedHAL()
    hal.now = clock.mono
    device = Device(hal, ssaa=1, mono=clock.mono, wall=clock.wall)

    real = time.monotonic()
    for _ in range(int(8.0 * 30)):
        clock.advance(FRAME)
        device.tick()
    report.check("the boot animation runs on the clock it was given",
                 device.face.current == "face_idle", device.face.current)
    report.check("and eight simulated seconds took far less real time",
                 time.monotonic() - real < 6.0)

    before = clock.mono()
    clock.set_wall(clock.wall() + 3 * 86400)
    report.check("stepping the date leaves the monotonic clock alone",
                 clock.mono() == before and device.mono() == before)
    report.check("and the device sees the new date",
                 device.wall() == clock.wall())
    report.check("the clock can say it has not synced", not clock.is_synced())
    clock.set_synced(True)
    report.check("and then that it has", clock.is_synced())

    try:
        clock.advance(-1)
        refused = False
    except ValueError:
        refused = True
    report.check("time cannot be run backwards by accident", refused)

    plain = Device(EmulatedHAL(), ssaa=1)
    report.check("with no clocks given the device keeps its old defaults",
                 abs(plain.wall() - time.time()) < 5
                 and abs(plain.mono() - plain.hal.now()) < 5)


def main(argv=None):
    root = tempfile.mkdtemp(prefix="trstenko-headless-")
    report = Report()
    scenarios = [
        ("the event queue", scenario_events),
        ("the device on a fake clock", scenario_fake_clock),
        ("boot and a full turn", scenario_boot_and_talk),
        ("the mute switch", scenario_mute),
        ("a turn that fails", scenario_failed_turn),
        ("buttons and the setup gesture", scenario_setup_gesture),
        ("an optional update", scenario_optional_update),
        ("an urgent update", scenario_urgent_update),
        ("an urgent update, start to finish", scenario_urgent_end_to_end),
        ("urgent is not exempt from the checks", scenario_urgent_still_checked),
        ("installing waits for the conversation",
         scenario_install_waits_for_the_turn),
        ("an SD card with no room", scenario_no_room),
        ("offline, then a corrupt bundle", scenario_offline_and_corrupt),
        ("the update page's API", scenario_web_api),
        ("pairing with the page", scenario_pairing),
        ("what reaches the panel", scenario_blit),
        ("pixels left behind", scenario_no_ghosts),
        ("audio in and out", scenario_audio),
        ("installing an update", scenario_install),
        ("a release that will not come up", scenario_rollback),
        ("a release that cannot even start", scenario_bootcheck),
        ("a release that needs a newer OS", scenario_base_image),
        ("a bundle that misbehaves", scenario_hostile_bundle),
        ("who signed the release", scenario_signing),
    ]
    try:
        for title, fn in scenarios:
            print("\n== %s" % title)
            fn(report, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return report.done()


if __name__ == "__main__":
    sys.exit(main())

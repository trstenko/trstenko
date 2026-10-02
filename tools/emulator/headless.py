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
import json
import logging
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time

from firmware import events as ev
from firmware import logs
from firmware import main as firmware_main
from firmware import sdnotify
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


class Clocked:
    """A bare Device on a FakeClock: no updater, no pipeline, nothing that
    needs real time, so a test can run minutes of device in a blink."""

    def __init__(self, **kw):
        self.clock = FakeClock()
        self.hal = EmulatedHAL()
        self.hal.now = self.clock.mono
        self.logs = []
        self.device = Device(self.hal, ssaa=1, mono=self.clock.mono,
                             wall=self.clock.wall, on_log=self.logs.append,
                             **kw)

    def step(self, seconds):
        for _ in range(int(round(seconds / FRAME))):
            self.clock.advance(FRAME)
            self.device.tick()


class quiet_logs:
    """A step that raises on purpose logs a traceback; keep it out of the
    scenario output."""

    def __enter__(self):
        logging.disable(logging.CRITICAL)

    def __exit__(self, *exc):
        logging.disable(logging.NOTSET)


def scenario_frame_rates(report, root):
    from firmware.device import FPS_ACTIVE, FPS_IDLE, FPS_SCREEN_OFF

    rig = Clocked()
    rig.step(0.2)
    report.check("the boot animation runs at the full rate",
                 abs(rig.device.frame_interval() - 1 / FPS_ACTIVE) < 1e-9)

    rig.step(6.0)
    report.check("a settled idle face drops to 10Hz",
                 rig.device.face.current == "face_idle"
                 and abs(rig.device.frame_interval() - 1 / FPS_IDLE) < 1e-9,
                 "%.3f" % rig.device.frame_interval())

    rig.hal.set_mute(True)
    rig.step(1.0)
    report.check("the muted face is as still as idle, so 10Hz too",
                 rig.device.face.current == "face_muted"
                 and abs(rig.device.frame_interval() - 1 / FPS_IDLE) < 1e-9)
    rig.hal.set_mute(False)
    rig.step(1.0)

    rig.device.fail("test", rig.clock.mono())
    report.check("an error brings the full rate back",
                 abs(rig.device.frame_interval() - 1 / FPS_ACTIVE) < 1e-9)
    rig.step(5.0)

    rig.device.sleep_screen()
    report.check("screen off is 2Hz",
                 abs(rig.device.frame_interval() - 1 / FPS_SCREEN_OFF) < 1e-9)
    report.check("and the backlight really is off",
                 rig.hal.panel.backlight_level == 0.0)

    frames = rig.device.frames
    rig.step(2.0)
    report.check("a dark screen is not drawn", rig.device.frames == frames)

    rig.hal.press(VOL_UP)
    rig.step(0.2)
    rig.hal.release(VOL_UP)
    rig.step(0.2)
    report.check("a button press wakes the screen",
                 not rig.device.screen_off
                 and rig.hal.panel.backlight_level == 1.0)

    # A turn must be seen at 30Hz even from a cold start.
    pipeline = ScriptedPipeline(rig.hal.speaker, seed=3)
    rig.device.pipeline = pipeline
    pipeline.trigger()
    rig.step(0.5)
    report.check("a turn runs at the full rate",
                 rig.device.turn is not None
                 and abs(rig.device.frame_interval() - 1 / FPS_ACTIVE) < 1e-9)

    sleepy = Clocked(screen_sleep_after=60.0)
    sleepy.step(30.0)
    report.check("the screen stays on before its idle time is up",
                 not sleepy.device.screen_off)
    sleepy.step(40.0)
    report.check("and sleeps after it", sleepy.device.screen_off)

    never = Clocked()
    never.step(120.0)
    report.check("with no sleep time set the screen never sleeps",
                 not never.device.screen_off)


def scenario_loop_wakes_on_events(report, root):
    """Waiting on the inbox, not sleeping: an event has to be handled within a
    frame, at every frame rate, or a reminder at 2Hz would be late for no
    reason."""
    for label, screen_off in (("idle", False), ("screen off", True)):
        hal = EmulatedHAL()
        device = Device(hal, ssaa=1)
        seen = []
        device.on(ev.WAKE_WORD, lambda e: seen.append(time.monotonic()))
        if screen_off:
            device.sleep_screen()
        thread = threading.Thread(target=device.run, daemon=True)
        thread.start()
        # Long enough that the loop is deep in a wait, not mid-frame.
        time.sleep(0.8 if label == "idle" else 0.3)

        start = time.monotonic()
        device.events.post(ev.Event(ev.WAKE_WORD))
        deadline = start + 2.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.001)
        late = (seen[0] - start) if seen else 9.9
        report.check("an event is handled within a frame (%s)" % label,
                     late < 0.08, "%.0f ms" % (late * 1000))

        device.stop()
        device.events.post(ev.Event("Wake"))
        thread.join(2.0)
        report.check("the loop stops when asked (%s)" % label,
                     not thread.is_alive())


def scenario_step_failures(report, root):
    healthy = Clocked()
    healthy.step(8.0)
    report.check("a device with nothing failing is healthy",
                 healthy.device.healthy() and not healthy.device.failing())

    rig = Clocked()

    def broken(now):
        raise RuntimeError("scheduler fell over")

    rig.device.add_step("scheduler", broken)
    with quiet_logs():
        rig.step(8.0)

    device = rig.device
    report.check("a failing step does not stop the face",
                 device.face.current == "face_idle" and device.frames > 200,
                 "%s, %d frames" % (device.face.current, device.frames))
    report.check("its failures are counted",
                 device.faults["scheduler"] == device.ticks,
                 str(device.faults))
    report.check("healthy() goes false when a step fails every tick",
                 not device.healthy() and device.failing() == ["scheduler"])
    report.check("and the page can see which one",
                 device.summary()["failing"] == ["scheduler"])

    flaky = Clocked()
    calls = []

    def sometimes(now):
        calls.append(now)
        if len(calls) % 5 == 0:
            raise RuntimeError("once in a while")

    flaky.device.add_step("flaky", sometimes)
    with quiet_logs():
        flaky.step(8.0)
    report.check("a step that only fails now and then is not unhealthy",
                 flaky.device.healthy() and flaky.device.faults["flaky"] > 0)

    recovers = Clocked()
    state = {"broken": True}

    def mends(now):
        if state["broken"]:
            raise RuntimeError("not yet")

    recovers.device.add_step("mends", mends)
    with quiet_logs():
        recovers.step(8.0)
        state["broken"] = False
        recovers.step(0.5)
    report.check("and one that recovers stops counting against the release",
                 recovers.device.healthy())

    slow = Clocked()
    seen = []
    slow.device.add_step("housekeeping", lambda now: seen.append(now),
                         every=1.0)
    slow.step(10.0)
    report.check("a step with an interval runs once per interval",
                 8 <= len(seen) <= 11, str(len(seen)))


def scenario_commands(report, root):
    rig = Clocked()
    device = rig.device

    cmd = ev.Command("set_volume", 0.35)
    device.events.post(cmd)
    report.check("a command waits for the main thread",
                 not cmd.answered)
    rig.step(0.1)
    report.check("set_volume is applied and answered",
                 cmd.answered and cmd.result == {"volume": 0.35}
                 and abs(rig.hal.speaker.get_volume() - 0.35) < 1e-9,
                 str(cmd.result))

    for bad in (1.5, -0.1, "loud", None, True):
        cmd = ev.Command("set_volume", bad)
        device.events.post(cmd)
        rig.step(0.1)
        report.check("volume %r is refused with a reason" % (bad,),
                     cmd.error and "volume" in cmd.error, str(cmd.error))
    report.check("a refused volume leaves it where it was",
                 abs(rig.hal.speaker.get_volume() - 0.35) < 1e-9)

    cmd = ev.Command("make_coffee")
    device.events.post(cmd)
    rig.step(0.1)
    report.check("an unknown command is refused by name",
                 cmd.error and "make_coffee" in cmd.error, str(cmd.error))

    cmd = ev.Command("settings.update", {"volume": 0.2})
    device.events.post(cmd)
    rig.step(0.1)
    report.check("settings.update on a device with no settings is refused",
                 cmd.answered and cmd.error)

    def explode(args):
        raise ZeroDivisionError("a bug")

    device.command("explode", explode)
    cmd = ev.Command("explode")
    device.events.post(cmd)
    with quiet_logs():
        rig.step(0.1)
    report.check("a bug in a command is an answer, not a dead loop",
                 cmd.error == "the device could not do that"
                 and device.faults["commands"] == 1)

    cmd = ev.Command("set_volume", 0.5)
    device.events.post(cmd)
    rig.step(0.1)
    report.check("and the loop is still serving commands",
                 cmd.result == {"volume": 0.5})

    for _ in range(120):
        device.events.post(ev.Event(ev.SENTENCE))
    heard = []
    device.on(ev.SENTENCE, lambda e: heard.append(e))
    device.tick()
    report.check("one tick handles at most 50 events",
                 len(heard) == 50 and len(device.events) == 70,
                 "%d, %d left" % (len(heard), len(device.events)))
    device.tick()
    device.tick()
    report.check("the rest follow on the next frames", len(heard) == 120)

    device.on(ev.TTS_DIED, lambda e: 1 / 0)
    device.on(ev.TTS_DIED, lambda e: heard.append("second"))
    device.events.post(ev.Event(ev.TTS_DIED))
    with quiet_logs():
        device.tick()
    report.check("a handler that raises does not stop the next one",
                 "second" in heard and device.faults["events"] == 1)

    device.events.post(ev.Event(ev.SHUTDOWN))
    rig.step(0.1)
    report.check("a Shutdown event stops the loop", not device.running)


def scenario_worker_restarts(report, root):
    rig = Clocked()
    device = rig.device
    starts = []
    device.supervisor.add("audio", lambda: starts.append(rig.clock.mono()),
                          rig.clock.mono())
    report.check("a worker is started when it is added", len(starts) == 1)

    def die():
        device.events.post(ev.Event(ev.WORKER_DIED, data="audio"))
        rig.step(0.1)

    die()
    report.check("a death does not restart it at once", len(starts) == 1)
    rig.step(1.2)
    report.check("it comes back after one second", len(starts) == 2,
                 str(len(starts)))

    die()
    rig.step(3.0)
    report.check("the second wait is longer", len(starts) == 2)
    rig.step(3.0)
    report.check("five seconds", len(starts) == 3, str(len(starts)))

    die()
    rig.step(20.0)
    report.check("the third wait is longer still", len(starts) == 3)
    rig.step(11.0)
    report.check("thirty seconds", len(starts) == 4, str(len(starts)))

    die()
    rig.step(120.0)
    report.check("after that it is left off", len(starts) == 4)
    report.check("and the page can say which service is down",
                 device.supervisor.down() == ["audio"]
                 and device.summary()["workers"]["audio"]["state"] == "down")
    report.check("a missing worker does not make the release unhealthy",
                 device.healthy())

    steady = Clocked()
    runs = []
    steady.device.supervisor.add("tts", lambda: runs.append(1),
                                 steady.clock.mono())
    for _ in range(6):
        steady.step(200.0)
        steady.device.events.post(ev.Event(ev.WORKER_DIED, data="tts"))
        steady.step(2.0)
    report.check("a worker that ran for minutes between deaths is not given up on",
                 len(runs) == 7 and not steady.device.supervisor.down(),
                 str(len(runs)))

    stubborn = Clocked()
    attempts = []

    def wont_start():
        attempts.append(1)
        if len(attempts) > 1:
            raise OSError("no sound card")

    stubborn.device.supervisor.add("audio", wont_start, stubborn.clock.mono())
    stubborn.device.events.post(ev.Event(ev.WORKER_DIED, data="audio"))
    stubborn.step(60.0)
    report.check("a worker that cannot even start ends up off, not looping",
                 stubborn.device.supervisor.down() == ["audio"]
                 and len(attempts) <= 5, str(len(attempts)))


def scenario_notify(report, root):
    """READY, then WATCHDOG from the main loop, then STOPPING."""
    off = sdnotify.Notifier(address="")
    report.check("with no NOTIFY_SOCKET every call does nothing",
                 not off.enabled and off.ready() is False
                 and off.watchdog() is False and off.sent == 0)

    sent = []
    notifier = sdnotify.Notifier(address="/run/systemd/notify",
                                 send=sent.append)

    clock = FakeClock()
    hal = EmulatedHAL()
    hal.now = clock.mono
    config = UpdateConfig(repo="emulator/trstenko",
                          data_root=os.path.join(root, "notify"),
                          install_root=os.path.join(root, "notify"),
                          auto_check=False)
    device, _ = firmware_main.build(hal, config=config, notifier=notifier)
    for _ in range(int(10 * 30)):
        clock.advance(FRAME)
        device.tick()
    pings = sent.count(b"WATCHDOG=1")
    report.check("the loop pings about once a second, and no faster",
                 9 <= pings <= 11, "%d pings in 10s" % pings)

    stalled = len(sent)
    device.add_step("wedge", lambda now: 1 / 0, before="watchdog")
    with quiet_logs():
        for _ in range(60):
            clock.advance(FRAME)
            device.tick()
    report.check("a step that raises does not stop the pings",
                 len(sent) > stalled)

    # The real thing: the loop on its own thread, in real time.
    sent.clear()
    hal2 = EmulatedHAL()
    device2, _ = firmware_main.build(hal2, config=config, notifier=notifier)
    pipeline = ScriptedPipeline(hal2.speaker, seed=5)
    device2.pipeline = pipeline
    order = []
    device2.add_closer("storage", lambda: order.append("storage"))

    def failing_closer():
        order.append("flaky")
        raise OSError("disk went away")

    device2.add_closer("flaky", failing_closer)
    device2.add_closer("after", lambda: order.append("after"))
    pipeline.trigger()

    thread = threading.Thread(target=firmware_main.run,
                              args=(device2, notifier), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while b"WATCHDOG=1" not in sent and time.monotonic() < deadline:
        time.sleep(0.02)
    report.check("READY comes first, then the watchdog",
                 sent[:1] == [b"READY=1"] and b"WATCHDOG=1" in sent,
                 str(sent[:3]))
    report.check("a turn is in flight when the signal arrives",
                 device2.turn is not None)

    with quiet_logs():
        firmware_main.terminate_handler(device2)(signal.SIGTERM, None)
        thread.join(5.0)
    report.check("SIGTERM brings the loop down within a moment",
                 not thread.is_alive())
    report.check("STOPPING is the last thing systemd hears",
                 sent[-1] == b"STOPPING=1", str(sent[-2:]))
    report.check("the turn was cancelled", device2.turn is None)
    report.check("the screen is off", hal2.panel.backlight_level == 0.0)
    report.check("every closer ran, in order, even after one failed",
                 order == ["storage", "flaky", "after"], str(order))
    device2.close()
    report.check("closing twice is harmless", order.count("storage") == 1)

    if hasattr(socket, "AF_UNIX"):
        path = os.path.join(root, "notify.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(path)
        server.settimeout(2.0)
        real = sdnotify.Notifier(address=path)
        real.ready()
        real.watchdog()
        got = [server.recv(64), server.recv(64)]
        server.close()
        report.check("over a real unix socket: READY, then WATCHDOG",
                     got == [b"READY=1", b"WATCHDOG=1"], str(got))
    else:
        report.check("over a real unix socket (SKIPPED: this Python has no "
                     "AF_UNIX; CI on Linux runs it)", True)

    gone = sdnotify.Notifier(address=os.path.join(root, "nobody-here.sock"))
    with quiet_logs():
        report.check("a socket that is not there never raises",
                     gone.ready() is False)


def scenario_logging(report, root):
    seen = []
    logs.setup(stream=False, callback=seen.append)
    try:
        logging.getLogger("trstenko.scheduler").info("reminder 3 is due")
        report.check("a callback sees other modules' lines, named",
                     "scheduler: reminder 3 is due" in seen, str(seen))

        quiet = Clocked()
        quiet.device.on_log = seen.append
        quiet.device._has_callback = True
        quiet.device.log("volume 40%")
        report.check("a device line reaches the callback exactly once",
                     seen.count("volume 40%") == 1, str(seen))

        seen.clear()
        logging.getLogger("trstenko.scheduler").debug("per-request detail")
        report.check("DEBUG stays off unless asked for", not seen, str(seen))
    finally:
        logs.teardown()

    seen.clear()
    logging.getLogger("trstenko.scheduler").info("after teardown")
    report.check("tearing down stops the callback", not seen)

    out = io.StringIO()
    logs.setup(stream=out)
    try:
        logging.getLogger("trstenko.web").info("paired")
    finally:
        logs.teardown()
    report.check("stdout gets level, module and message, no timestamp",
                 out.getvalue() == "INFO trstenko.web: paired\n",
                 repr(out.getvalue()))


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}


def _migration_dir(root, name, files):
    directory = os.path.join(root, name)
    os.makedirs(directory, exist_ok=True)
    for filename, sql in files.items():
        with open(os.path.join(directory, filename), "w",
                  encoding="utf-8") as fh:
            fh.write(sql)
    return directory


def scenario_database(report, root):
    import sqlite3

    from firmware.storage import Database, log_event
    from firmware.storage import migrate as migrate_mod

    base = os.path.join(root, "db")
    db = Database(os.path.join(base, "trstenko.db"))
    conn = db.conn()

    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    report.check("the database is in WAL mode", mode == "wal", mode)
    report.check("with synchronous=NORMAL",
                 conn.execute("PRAGMA synchronous").fetchone()[0] == 1)
    report.check("and a 2s busy timeout",
                 conn.execute("PRAGMA busy_timeout").fetchone()[0] == 2000)

    report.check("a thread gets the same connection every time",
                 db.conn() is conn)
    other = []
    thread = threading.Thread(target=lambda: other.append(db.conn()))
    thread.start()
    thread.join()
    report.check("and another thread gets its own",
                 other and other[0] is not conn)

    first = migrate_mod.migrate(db)
    report.check("a new database is migrated from 0 to 1", first == (0, 1),
                 str(first))
    wanted = {"schema_version", "memories", "jobs", "usage", "usage_daily",
              "turn_log", "events_log"}
    report.check("with every table the plans name",
                 wanted <= _tables(conn), str(wanted - _tables(conn)))
    report.check("and no backup, because there was nothing to back up",
                 not os.path.exists(os.path.join(base, "backups")))

    second = migrate_mod.migrate(db)
    report.check("migrating again does nothing", second == (1, 1),
                 str(second))
    rows = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    report.check("and keeps exactly one version row", rows == 1, str(rows))

    conn.execute("INSERT INTO memories (text, created_at) VALUES "
                 "('likes tea', 1.0)")
    sql2 = ("-- adds a column and a table\n"
            "ALTER TABLE memories ADD COLUMN mood TEXT DEFAULT 'calm';\n"
            "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT);\n")
    v2 = _migration_dir(root, "mig2", {
        "001_init.sql": open(os.path.join(migrate_mod.MIGRATIONS_DIR,
                                          "001_init.sql"),
                             encoding="utf-8").read(),
        "002_notes.sql": sql2})
    result = migrate_mod.migrate(db, migrations_dir=v2)
    report.check("a new migration moves the version on", result == (1, 2),
                 str(result))
    backup = os.path.join(base, "backups", "trstenko-v1.db")
    report.check("and a backup of the old database was made first",
                 os.path.exists(backup))
    copy = sqlite3.connect(backup)
    held = copy.execute("SELECT text FROM memories").fetchall()
    has_new = "notes" in {r[0] for r in copy.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    copy.close()
    report.check("the backup holds the data as it was, without the new table",
                 held == [("likes tea",)] and not has_new, str(held))

    # The rollback. Release 0.5 only knows migration 001; the database has
    # been taken to 2 by release 0.6.
    old_release = migrate_mod.migrate(db)
    report.check("a rolled-back release leaves a newer database alone",
                 old_release == (2, 2), str(old_release))
    conn.execute("INSERT INTO memories (text, created_at) VALUES "
                 "('prefers mornings', 2.0)")
    conn.execute("INSERT INTO jobs (kind, text, state, created_at, source) "
                 "VALUES ('timer', 'pasta', 'pending', 3.0, 'voice')")
    read = conn.execute("SELECT text FROM memories ORDER BY id").fetchall()
    report.check("and the old code still reads and writes its tables",
                 [r[0] for r in read] == ["likes tea", "prefers mornings"],
                 str(read))
    report.check("the new column has its default for rows the old code wrote",
                 conn.execute("SELECT mood FROM memories WHERE id = 2")
                 .fetchone()[0] == "calm")

    # Backups keep the last three.
    files = {"001_init.sql": open(os.path.join(migrate_mod.MIGRATIONS_DIR,
                                               "001_init.sql"),
                                  encoding="utf-8").read(),
             "002_notes.sql": sql2}
    for n in range(3, 7):
        files["%03d_extra.sql" % n] = "CREATE TABLE extra%d (id INTEGER);\n" % n
        directory = _migration_dir(root, "mig%d" % n, dict(files))
        migrate_mod.migrate(db, migrations_dir=directory)
    kept = sorted(os.listdir(os.path.join(base, "backups")))
    report.check("only the last three backups are kept",
                 kept == ["trstenko-v3.db", "trstenko-v4.db",
                          "trstenko-v5.db"], str(kept))

    # A migration that fails half way changes nothing.
    broken = _migration_dir(root, "mig-broken", dict(files, **{
        "007_half.sql": ("CREATE TABLE half_done (id INTEGER);\n"
                         "CREATE TABLE half_done (id INTEGER);\n")}))
    before = migrate_mod.current_version(conn)
    try:
        migrate_mod.migrate(db, migrations_dir=broken)
        failed = False
    except migrate_mod.MigrationError:
        failed = True
    report.check("a migration that fails is reported",
                 failed)
    report.check("and leaves neither its tables nor a new version",
                 "half_done" not in _tables(conn)
                 and migrate_mod.current_version(conn) == before)

    # The events log keeps its last 500.
    for n in range(520):
        log_event(conn, "test", "event %d" % n, at=float(n))
    count = conn.execute("SELECT COUNT(*) FROM events_log").fetchone()[0]
    oldest = conn.execute("SELECT MIN(at) FROM events_log").fetchone()[0]
    report.check("the events log is trimmed to the last 500",
                 count == 500 and oldest == 20.0, "%d, %s" % (count, oldest))

    db.close()
    report.check("closing the database folds the WAL back in",
                 not os.path.exists(db.path + "-wal")
                 or os.path.getsize(db.path + "-wal") == 0)
    try:
        db.conn()
        refused = False
    except sqlite3.ProgrammingError:
        refused = True
    report.check("and a closed database refuses new connections", refused)

    from tools import check_layout

    report.check("the shipped migrations pass the layout check",
                 check_layout.check_migrations() == [])
    bad = _migration_dir(root, "bad-mig", {
        "001_init.sql": "CREATE TABLE a (id INTEGER);\n",
        "002_bad.sql": "-- DROP in a comment is fine\nDROP TABLE a;\n",
        "003_rename.sql": "ALTER TABLE a RENAME TO b;\n"})
    found = check_layout.check_migrations(bad)
    report.check("a migration containing DROP or RENAME fails it",
                 len(found) == 2 and "DROP" in found[0]
                 and "RENAME" in found[1], str(found))
    commented = _migration_dir(root, "ok-mig", {
        "001_init.sql": "-- never DROP anything\n/* or RENAME */\n"
                        "CREATE TABLE a (id INTEGER);\n"})
    report.check("but the words in a comment do not",
                 check_layout.check_migrations(commented) == [])
    gap = _migration_dir(root, "gap-mig", {
        "001_a.sql": "CREATE TABLE a (id INTEGER);\n",
        "003_c.sql": "CREATE TABLE c (id INTEGER);\n"})
    report.check("and a gap in the numbering fails it",
                 len(check_layout.check_migrations(gap)) == 1)


def scenario_settings_file(report, root):
    from firmware.storage import atomic
    from firmware.storage.settings import (DEFAULTS, Settings, SettingsError,
                                           default_path)

    data = os.path.join(root, "settings")
    path = default_path(data)

    fresh = Settings.load(path)
    report.check("with no file, the defaults apply",
                 fresh.data == DEFAULTS and not fresh.repaired)
    report.check("and the plan's values are in them",
                 fresh.get("voice", "volume") == 0.6
                 and fresh.get("quiet", "from") == "22:00"
                 and fresh.get("owner", "timezone") == "Europe/Ljubljana"
                 and fresh.get("budget", "monthly_usd") == 5.0)

    # What is already in the file: the updater's section, and a setting from a
    # release newer than this one.
    os.makedirs(data, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"updates": {"channel": "beta", "auto_check": False},
                   "voice": {"volume": 0.3, "from_the_future": 7},
                   "from_a_newer_release": {"x": 1}}, fh)
    loaded = Settings.load(path)
    report.check("a saved setting is read back",
                 loaded.get("voice", "volume") == 0.3)

    loaded.apply(loaded.validate({"quiet": {"enabled": False}}))
    loaded.save()
    config = UpdateConfig.load(path=path, data_root=data)
    report.check("UpdateConfig.load still reads its section from the same file",
                 config.channel == "beta" and config.auto_check is False,
                 "%s %s" % (config.channel, config.auto_check))
    with open(path, "r", encoding="utf-8") as fh:
        written = json.load(fh)
    report.check("what this release does not understand is kept on save",
                 written.get("from_a_newer_release") == {"x": 1}
                 and written["voice"].get("from_the_future") == 7)
    report.check("and the change was written",
                 written["quiet"]["enabled"] is False)

    # A write that dies after the temp file is complete but before the rename.
    settings = Settings.load(path)
    settings.apply(settings.validate({"voice": {"volume": 0.9}}))
    real_replace = os.replace

    def killed(src, dst):
        raise KeyboardInterrupt("power cut")

    os.replace = killed
    try:
        try:
            settings.save()
        except KeyboardInterrupt:
            pass
    finally:
        os.replace = real_replace
    after = Settings.load(path)
    report.check("a write killed before the rename leaves the old file whole",
                 after.get("voice", "volume") == 0.3
                 and after.get("quiet", "enabled") is False
                 and not after.repaired)
    report.check("the half-written copy is only ever the temp file",
                 os.path.exists(path + ".tmp"))
    settings.save()
    report.check("and the next save simply replaces it",
                 Settings.load(path).get("voice", "volume") == 0.9
                 and not os.path.exists(path + ".tmp"))

    # A write killed in the middle of the temp file.
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        fh.write('{"voice": {"volu')
    report.check("a torn temp file is never read",
                 Settings.load(path).get("voice", "volume") == 0.9)

    # A main file that is itself damaged.
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"voice": {"volu')
    broken = Settings.load(path)
    report.check("a damaged settings file gives defaults, not an exception",
                 broken.data == DEFAULTS and broken.repaired)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"voice": {"volume": "loud"}, "quiet": {"from": "25:99"},
                   "budget": {"max_tool_steps": 3}}, fh)
    partly = Settings.load(path)
    report.check("a bad value is dropped and the rest are kept",
                 partly.get("voice", "volume") == 0.6
                 and partly.get("quiet", "from") == "22:00"
                 and partly.get("budget", "max_tool_steps") == 3
                 and len(partly.repaired) == 2, str(partly.repaired))

    # Validation: all the complaints at once, and nothing applied.
    s = Settings.load(os.path.join(data, "other.json"))
    bad = {"voice": {"volume": 2, "piper_voice": 5},
           "quiet": {"from": "late"}, "nonsense": {"x": 1},
           "owner": {"timezone": "Mars/Olympus_Mons"},
           "budget": {"monthly_usd": -1, "max_tool_steps": 2.5}}
    try:
        s.validate(bad)
        message = ""
    except SettingsError as exc:
        message = str(exc)
    for needle in ("voice.volume", "voice.piper_voice", "quiet.from",
                   "nonsense", "owner.timezone", "budget.monthly_usd",
                   "budget.max_tool_steps"):
        report.check("the refusal names %s" % needle, needle in message,
                     message)
    report.check("and nothing was applied", s.data == DEFAULTS)

    for patch, why in (({}, "an empty patch"), ("volume", "not an object"),
                       ({"voice": 3}, "a section that is not an object"),
                       ({"updates": {"channel": "dev"}},
                        "the updater's own section"),
                       ({"voice": {"volume": True}}, "a bool for a number"),
                       ({"voice": {"volume": float("nan")}}, "NaN")):
        try:
            s.validate(patch)
            refused = False
        except SettingsError:
            refused = True
        report.check("refused: %s" % why, refused)

    clean = s.validate({"budget": {"daily_usd": None, "monthly_usd": 12},
                        "voice": {"volume": 1}})
    report.check("null is allowed where the plan allows it, and ints widen",
                 clean["budget"] == {"daily_usd": None, "monthly_usd": 12.0}
                 and clean["voice"]["volume"] == 1.0
                 and isinstance(clean["voice"]["volume"], float))
    changed = s.apply(clean)
    report.check("apply says which settings really changed",
                 sorted(changed) == ["budget.monthly_usd", "voice.volume"],
                 str(changed))

    atomic_path = os.path.join(data, "atomic.json")
    atomic.write_json(atomic_path, {"a": 1})
    with open(atomic_path, "r", encoding="utf-8") as fh:
        report.check("the atomic writer writes what it was given",
                     json.load(fh) == {"a": 1})


def scenario_secrets(report, root):
    from firmware.storage import redact
    from firmware.storage.secrets import Secrets, SecretsError, default_path

    path = default_path(os.path.join(root, "secrets"))
    keys = Secrets.load(path)
    report.check("with no file there are no keys",
                 not keys.has_key("gemini") and keys.key("gemini") == "")

    gemini = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE12345x9Qk"
    status = keys.set_key("gemini", "  " + gemini + "\n")
    report.check("setting a key says it is set, and shows the last four",
                 status["set"] and status["last4"] == "x9Qk", str(status))
    report.check("and that answer does not contain the key",
                 gemini not in json.dumps(status))
    report.check("nor does the status the page reads",
                 gemini not in json.dumps(keys.status()))
    report.check("the key is used by the code that needs it",
                 keys.key("gemini") == gemini)

    if os.name == "posix":
        import stat

        mode = stat.S_IMODE(os.stat(path).st_mode)
        report.check("the secrets file is mode 0600", mode == 0o600,
                     oct(mode))
    else:
        report.check("the secrets file is mode 0600 (SKIPPED: this OS has no "
                     "unix modes; CI on Linux runs it)", True)

    again = Secrets.load(path)
    report.check("keys survive a restart", again.key("gemini") == gemini)

    for provider, key, why in (("gemini", "short", "too short"),
                               ("gemini", "has a space in it 12345", "spaces"),
                               ("gemini", "x" * 600, "too long"),
                               ("nobody", "sk-" + "a" * 30, "unknown provider"),
                               ("gemini", 12345678, "not text")):
        try:
            again.set_key(provider, key)
            message = None
        except SecretsError as exc:
            message = str(exc)
        report.check("a key that is %s is refused" % why, message is not None)
        report.check("and the refusal does not quote it",
                     message is None or str(key) not in message)
    report.check("a refused key leaves the old one in place",
                 again.key("gemini") == gemini)

    again.set_key("gemini", "")
    report.check("an empty key removes it",
                 not again.has_key("gemini")
                 and Secrets.load(path).key("gemini") == "")

    plain = "plainly-not-shaped-like-anything-7f3a9c"
    again.set_key("groq", plain)
    report.check("a key with no recognisable shape is blanked by value",
                 plain not in redact.redact("header was %s today" % plain))
    again.clear()
    report.check("a factory reset deletes the file and forgets the keys",
                 not os.path.exists(path) and not again.has_key("groq")
                 and plain in redact.redact(plain))

    shaped = {"openai": "sk-proj-" + "A1b2C3d4" * 4,
              "xai": "xai-" + "Q9w8E7r6" * 3,
              "groq": "gsk_" + "Zx1Cv2Bn" * 3}
    seen = {k: redact.redact("key=%s end" % v) for k, v in shaped.items()}
    report.check("keys shaped like OpenAI, xAI and Groq ones are blanked",
                 all(v == "key=%s end" % redact.MASK for v in seen.values()),
                 str(seen))
    report.check("ordinary text is left alone",
                 redact.redact("sk-learn and gsk_ are fine") ==
                 "sk-learn and gsk_ are fine")


def scenario_no_key_in_output(report, root):
    """Put a key everywhere a careless line could carry it, and look for it."""
    import io

    from firmware.storage import Database, log_event
    from firmware.storage import migrate as migrate_mod
    from firmware.storage.secrets import Secrets, default_path
    from firmware.storage.settings import Settings

    key = "AIzaSyNOTAREALKEYNOTAREALKEY0000000abcd"
    odd = "unshaped-secret-0011223344556677"
    keys = Secrets.load(default_path(os.path.join(root, "leak")))
    keys.set_key("gemini", key)
    keys.set_key("groq", odd)

    stream = io.StringIO()
    callback = []
    logs.setup(stream=stream, callback=callback.append)
    try:
        logger = logging.getLogger("trstenko.provider")
        logger.info("request with Authorization: Bearer %s", key)
        logger.warning("groq said no to %s", odd)
        try:
            raise RuntimeError("connection failed for key %s" % key)
        except RuntimeError:
            logger.exception("request failed")
        logger.info("a dict: %r", {"key": key, "other": odd})

        quiet = Clocked()
        quiet.device.on_log = callback.append
        quiet.device._has_callback = True
        quiet.device.log("using %s" % key)
        quiet.device.log("using %s" % odd)
    finally:
        logs.teardown()

    where = {"stdout": stream.getvalue(), "callback": "\n".join(callback)}
    for name, text in where.items():
        report.check("the key is nowhere in the %s output" % name,
                     key not in text and odd not in text, text[:120])
    report.check("but the lines are still there",
                 "Authorization: Bearer" in where["stdout"]
                 and "request failed" in where["stdout"])
    report.check("including the traceback",
                 "RuntimeError" in where["stdout"])

    db = Database(os.path.join(root, "leak", "trstenko.db"))
    migrate_mod.migrate(db)
    log_event(db.conn(), "provider", "401 for key %s" % key)
    row = db.conn().execute("SELECT detail FROM events_log").fetchone()[0]
    report.check("an events_log row is redacted before it is stored",
                 key not in row and redact_mask() in row, row)
    db.close()

    settings = Settings.load(os.path.join(root, "leak", "settings.json"))
    settings.save()
    with open(os.path.join(root, "leak", "settings.json"), "r",
              encoding="utf-8") as fh:
        text = fh.read()
    report.check("the settings file and what the page reads hold no keys",
                 key not in text and odd not in text
                 and key not in json.dumps(settings.public()))


def redact_mask():
    from firmware.storage import redact

    return redact.MASK


def scenario_settings_command(report, root):
    import datetime

    from firmware.quiet import QuietHours
    from firmware.storage.settings import Settings, default_path

    data = os.path.join(root, "settings-cmd")
    path = default_path(data)

    def boot():
        return Clocked(settings=Settings.load(path))

    def ask(rig, patch):
        cmd = ev.Command("settings.update", patch)
        rig.device.events.post(cmd)
        rig.step(0.1)
        return cmd

    rig = boot()
    heard = []
    rig.device.on_settings(heard.append)
    report.check("a device with settings starts at the saved volume",
                 abs(rig.hal.speaker.get_volume() - 0.6) < 1e-9,
                 str(rig.hal.speaker.get_volume()))

    bad = ask(rig, {"voice": {"volume": 7}, "quiet": {"from": "tonight"},
                    "made_up": {"x": 1}})
    report.check("a bad patch is refused",
                 bad.answered and bad.result is None and bad.error)
    report.check("and says what is wrong with each part",
                 all(n in bad.error for n in
                     ("voice.volume", "quiet.from", "made_up")), bad.error)
    report.check("nothing changed in memory or on the speaker",
                 abs(rig.hal.speaker.get_volume() - 0.6) < 1e-9
                 and rig.device.settings.get("voice", "volume") == 0.6)
    report.check("nothing was written", not os.path.exists(path))
    report.check("and nobody was told", heard == [])

    readonly = ask(rig, {"updates": {"channel": "dev"}})
    report.check("the updater's own settings are not changed through here",
                 readonly.error and "updates" in readonly.error,
                 str(readonly.error))
    nothing = ask(rig, {})
    report.check("an empty patch is refused with an example",
                 nothing.error and "volume" in nothing.error)

    good = ask(rig, {"voice": {"volume": 0.35},
                     "quiet": {"from": "23:30", "to": "06:00"}})
    report.check("a good patch is answered with what changed",
                 good.error is None
                 and sorted(good.result["changed"]) == ["quiet.from",
                                                         "quiet.to",
                                                         "voice.volume"],
                 str(good.result and good.result["changed"]))
    report.check("the speaker follows the volume setting",
                 abs(rig.hal.speaker.get_volume() - 0.35) < 1e-9)
    report.check("quiet hours follow the quiet settings",
                 rig.device.quiet.start == 23 * 60 + 30
                 and rig.device.quiet.end == 6 * 60)
    report.check("the services that care are told which settings changed",
                 heard == [good.result["changed"]], str(heard))
    report.check("the file is written before the answer comes back",
                 os.path.exists(path))

    same = ask(rig, {"voice": {"volume": 0.35}})
    report.check("a patch that changes nothing writes nothing and tells nobody",
                 same.result["changed"] == [] and len(heard) == 1)

    # A restart: a new device from the same file.
    again = boot()
    report.check("a good patch survives a restart",
                 abs(again.hal.speaker.get_volume() - 0.35) < 1e-9
                 and again.device.quiet.start == 23 * 60 + 30
                 and again.device.settings.get("quiet", "to") == "06:00")

    # A write that fails is undone rather than left half-true.
    heard.clear()
    real_replace = os.replace

    def refuse(src, dst):
        raise OSError("read-only file system")

    os.replace = refuse
    try:
        with quiet_logs():
            failed = ask(rig, {"voice": {"volume": 0.9}})
    finally:
        os.replace = real_replace
    report.check("a patch that cannot be saved is refused",
                 failed.error and "could not be saved" in failed.error,
                 str(failed.error))
    report.check("and is undone: memory, speaker and listeners all untouched",
                 rig.device.settings.get("voice", "volume") == 0.35
                 and abs(rig.hal.speaker.get_volume() - 0.35) < 1e-9
                 and heard == [])

    # Volume from the buttons and the command is remembered, but not by
    # writing the card on every press.
    rig = boot()
    with open(path, "r", encoding="utf-8") as fh:
        saved = json.load(fh)["voice"]["volume"]
    rig.hal.press(VOL_UP)
    rig.step(0.2)
    rig.hal.release(VOL_UP)
    rig.step(0.2)
    with open(path, "r", encoding="utf-8") as fh:
        right_after = json.load(fh)["voice"]["volume"]
    report.check("a volume button press does not write the card at once",
                 right_after == saved and
                 rig.device.settings.get("voice", "volume") > saved,
                 "%s -> %s" % (saved, right_after))
    rig.step(6.0)
    with open(path, "r", encoding="utf-8") as fh:
        later = json.load(fh)["voice"]["volume"]
    report.check("it is written once things have been still",
                 later == rig.device.settings.get("voice", "volume")
                 and later > saved, "%s" % later)

    cmd = ev.Command("set_volume", 0.15)
    rig.device.events.post(cmd)
    rig.step(0.1)
    rig.device.close()
    with open(path, "r", encoding="utf-8") as fh:
        closed = json.load(fh)["voice"]["volume"]
    report.check("and anything still pending is written on shutdown",
                 closed == 0.15, str(closed))

    # Quiet hours wrap midnight.
    night = QuietHours(True, "22:00", "07:00", 0.4)
    day = datetime.datetime(2026, 10, 2)
    at = lambda h, m=0: day.replace(hour=h, minute=m)
    report.check("22:00 to 07:00 covers late evening and early morning",
                 night.active(at(23)) and night.active(at(2))
                 and night.active(at(22, 0)) and night.active(at(6, 59)))
    report.check("and not the day",
                 not night.active(at(7, 0)) and not night.active(at(12))
                 and not night.active(at(21, 59)))
    report.check("a window inside one day works too",
                 QuietHours(True, "13:00", "15:00").active(at(14))
                 and not QuietHours(True, "13:00", "15:00").active(at(16)))
    report.check("switched off, it is never quiet",
                 not QuietHours(False, "22:00", "07:00").active(at(23)))


def main(argv=None):
    root = tempfile.mkdtemp(prefix="trstenko-headless-")
    report = Report()
    scenarios = [
        ("the event queue", scenario_events),
        ("the device on a fake clock", scenario_fake_clock),
        ("frame rates", scenario_frame_rates),
        ("the loop wakes on events", scenario_loop_wakes_on_events),
        ("a step that fails", scenario_step_failures),
        ("commands and the inbox", scenario_commands),
        ("restarting a dead worker", scenario_worker_restarts),
        ("telling systemd, and shutting down", scenario_notify),
        ("logging", scenario_logging),
        ("changing settings through the main thread", scenario_settings_command),
        ("the database and its migrations", scenario_database),
        ("the settings file", scenario_settings_file),
        ("API keys", scenario_secrets),
        ("a key never reaches the output", scenario_no_key_in_output),
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

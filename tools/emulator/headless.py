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
from firmware.hal.base import POWER, VOL_DOWN, VOL_UP
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
                     late < 0.15, "%.0f ms" % (late * 1000))

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


TZ = "Europe/Ljubljana"


def local_epoch(y, mo, d, h=0, mi=0, tz=TZ):
    import datetime

    from firmware.scheduler import clock as sched_clock

    return sched_clock.to_epoch(datetime.datetime(y, mo, d, h, mi), tz)


class SchedRig:
    """A device with storage and a scheduler, on a clock you move. Rebooting it
    brings up a new one against the same files, the way the robot would."""

    open_rigs = []

    def __init__(self, root, name, wall=None, mono=5000.0, synced=True,
                 settings_patch=None):
        from firmware.scheduler.wiring import wire
        from firmware.storage import Database
        from firmware.storage import migrate as migrate_mod
        from firmware.storage.settings import Settings, default_path

        self.root, self.name = root, name
        self.data = os.path.join(root, name)
        self.clock = FakeClock(
            mono=mono,
            wall=wall if wall is not None
            else local_epoch(2026, 10, 2, 17, 50),
            synced=synced)
        self.hal = EmulatedHAL()
        self.hal.now = self.clock.mono
        self.plays = []
        real_play = self.hal.speaker.play
        self.hal.speaker.play = lambda pcm, rate=22050: (
            self.plays.append((self.clock.mono(), len(pcm))),
            real_play(pcm, rate))
        self.logs = []
        self.said = []

        self.settings = Settings.load(default_path(self.data))
        if settings_patch:
            self.settings.apply(self.settings.validate(settings_patch))
        self.db = Database(os.path.join(self.data, "trstenko.db"))
        migrate_mod.migrate(self.db)
        self.device = Device(self.hal, ssaa=1, mono=self.clock.mono,
                             wall=self.clock.wall, on_log=self.logs.append,
                             db=self.db, settings=self.settings)
        self.scheduler, self.announcer = wire(
            self.device, synced=self.clock.is_synced, say=self.said.append)
        SchedRig.open_rigs.append(self)

    # ---- time ----

    def tick(self, times=1):
        for _ in range(times):
            self.device.tick()

    def jump(self, seconds):
        """The clock moves in one step and the device notices on its next
        tick, which is how a loop that sleeps behaves; two more ticks let an
        event it posted be handled."""
        self.clock.advance(seconds)
        self.tick(3)

    def run(self, seconds, dt=0.25):
        steps = int(round(seconds / dt))
        for _ in range(steps):
            self.clock.advance(dt)
            self.device.tick()

    def advance_to(self, epoch, dt=None):
        self.jump(epoch - self.clock.wall())

    # ---- doing things ----

    def ask(self, name, args=None):
        cmd = ev.Command(name, args)
        self.device.events.post(cmd)
        self.tick()
        return cmd

    def add(self, **kw):
        cmd = self.ask("reminders.add", kw)
        return cmd.result["reminder"] if cmd.error is None else cmd.error

    def press(self, name=VOL_UP):
        self.hal.press(name)
        self.run(0.1, 0.05)
        self.hal.release(name)
        self.run(0.1, 0.05)

    def jobs(self, state=None):
        return self.scheduler.list(state)

    def state_of(self, job_id):
        return self.scheduler.get(job_id)["state"]

    def reboot(self, off_seconds=0.0, synced=True, wall=None):
        self.device.close()
        self.db.close()
        later = wall if wall is not None else (
            self.clock.wall() + off_seconds)
        return SchedRig(self.root, self.name, wall=later, mono=12.0,
                        synced=synced)

    def close(self):
        self.device.close()
        self.db.close()
        if self in SchedRig.open_rigs:
            SchedRig.open_rigs.remove(self)


def scenario_scheduler_timers(report, root):
    rig = SchedRig(root, "timers")
    timer = rig.add(seconds=600)
    report.check("a timer is created and listed", timer["kind"] == "timer"
                 and timer["state"] == "pending"
                 and abs(timer["remaining"] - 600) < 1, str(timer))

    rig.jump(300)
    report.check("half way, it has not fired",
                 rig.state_of(timer["id"]) == "pending"
                 and abs(rig.scheduler.get(timer["id"])["remaining"]
                         - 300) < 1)
    rig.jump(299.9)
    report.check("a tenth of a second short, it has not fired",
                 rig.state_of(timer["id"]) == "pending")
    rig.jump(0.1)
    report.check("at ten minutes it fires", rig.state_of(timer["id"])
                 == "ringing", rig.state_of(timer["id"]))
    report.check("and the chime plays", len(rig.plays) == 1)
    report.check("the alert face is up", rig.device.face.current
                 == "face_talking" or rig.announcer.active)
    rig.run(1.5)
    report.check("with the line spoken",
                 rig.said == ["Your ten-minute timer is done."],
                 str(rig.said))

    rig.press()
    report.check("a button acknowledges it",
                 rig.state_of(timer["id"]) == "done"
                 and rig.scheduler.get(timer["id"])["acked_at"] is not None)
    report.check("and silences the alert", not rig.announcer.active
                 and rig.announcer.current is None)

    labelled = rig.add(seconds=90, text="pasta")
    rig.jump(90)
    rig.run(1.0)
    report.check("a labelled timer says its label",
                 rig.said[-1] == "The pasta timer is done.", rig.said[-1])
    rig.press()

    odd = rig.add(seconds=95)
    rig.jump(95)
    rig.run(1.0)
    report.check("a length that is not a round unit gets the plain line",
                 rig.said[-1] == "Your timer is done.", rig.said[-1])
    rig.press()

    gone = rig.add(seconds=60)
    rig.ask("reminders.cancel", {"id": gone["id"]})
    rig.jump(120)
    report.check("a cancelled timer never fires",
                 rig.state_of(gone["id"]) == "cancelled"
                 and not rig.announcer.active)

    for bad, why in ((0, "zero"), (-5, "negative"), (86401, "over a day"),
                     ("ten", "text"), (True, "a bool"),
                     (float("nan"), "NaN")):
        message = rig.add(seconds=bad)
        report.check("a timer of %s is refused with a sentence" % why,
                     isinstance(message, str) and message.endswith("."),
                     str(message))
    report.check("24 hours exactly is allowed",
                 isinstance(rig.add(seconds=86400), dict))
    report.check("a timer cannot repeat",
                 "cannot repeat" in str(rig.add(seconds=60, repeat="daily")))

    # A timer needs no clock at all.
    cold = SchedRig(root, "timers-cold", synced=False)
    early = cold.add(seconds=30)
    cold.jump(30)
    report.check("a timer works before the clock has synced",
                 isinstance(early, dict)
                 and cold.state_of(early["id"]) == "ringing")
    rig.close()
    cold.close()


def scenario_scheduler_reminders(report, root):
    rig = SchedRig(root, "reminders", wall=local_epoch(2026, 10, 2, 17, 50))
    mum = rig.add(text="call mum", local_time="2026-10-02T18:00")
    report.check("a reminder is created for a time, with its zone",
                 mum["kind"] == "reminder" and mum["tz"] == TZ
                 and mum["local_time"] == "2026-10-02T18:00", str(mum))
    report.check("and says when, in words", mum["say"] == "six in the evening",
                 mum["say"])

    rig.jump(9 * 60 + 59)
    report.check("a second before six it has not fired",
                 rig.state_of(mum["id"]) == "pending")
    rig.jump(1)
    report.check("at six it fires", rig.state_of(mum["id"]) == "ringing")
    rig.run(1.5)
    report.check("saying its own text, with no model involved",
                 rig.said == ["Reminder: call mum."], str(rig.said))
    rig.press()

    tomorrow = rig.add(text="dentist", local_time="2026-10-03T07:30")
    report.check("a reminder for tomorrow says so",
                 tomorrow["say"] == "tomorrow at half past seven in the "
                 "morning", tomorrow["say"])

    # Before the clock is right, a reminder waits. It is neither fired nor
    # judged missed, even if the stale clock says it is long past due.
    first = SchedRig(root, "reminders-sync",
                     wall=local_epoch(2026, 10, 2, 17, 50))
    due = first.add(text="take the bins out", local_time="2026-10-02T18:00")
    stale = local_epoch(2026, 10, 2, 21, 0)
    cold = first.reboot(wall=stale, synced=False)
    cold.run(30, 1.0)
    report.check("with the clock unsynced, a reminder does not fire even if "
                 "the clock says it is past due",
                 cold.state_of(due["id"]) == "pending"
                 and not cold.announcer.active)
    report.check("and is not judged missed", cold.jobs("missed") == [])
    report.check("a new reminder is refused until the clock is known",
                 "do not know the time" in str(cold.add(
                     text="x", local_time="2026-10-03T09:00")))

    # NTP corrects the clock to a time before the reminder.
    cold.clock.set_wall(local_epoch(2026, 10, 2, 17, 55))
    cold.clock.set_synced(True)
    cold.run(3, 0.5)
    report.check("on ClockSynced it is still waiting for its time",
                 cold.state_of(due["id"]) == "pending" and cold.scheduler.synced)
    cold.jump(5 * 60)
    report.check("and fires when six comes",
                 cold.state_of(due["id"]) == "ringing")
    cold.run(1.5)
    report.check("saying its line", cold.said == ["Reminder: take the bins "
                                                  "out."], str(cold.said))
    cold.press()

    # Or it corrects to a time after the reminder: it came due while we waited.
    first2 = SchedRig(root, "reminders-sync2",
                      wall=local_epoch(2026, 10, 2, 17, 50))
    late = first2.add(text="take the bins out", local_time="2026-10-02T18:00")
    cold2 = first2.reboot(wall=local_epoch(2026, 10, 2, 9, 0), synced=False)
    cold2.run(10, 1.0)
    cold2.clock.set_wall(local_epoch(2026, 10, 2, 18, 5))
    cold2.clock.set_synced(True)
    cold2.run(3, 0.5)
    cold2.run(4, 0.5)
    report.check("if the real time turns out to be after it, it is said as a "
                 "late reminder",
                 any("While I was off" in line for line in cold2.said),
                 str(cold2.said))
    for r in (rig, first, cold, first2, cold2):
        r.close()


def scenario_scheduler_limits(report, root):
    rig = SchedRig(root, "limits")
    ids = []
    for n in range(50):
        job = rig.add(text="reminder %d" % n,
                      local_time="2026-10-%02dT09:00" % (3 + n % 20))
        ids.append(job["id"] if isinstance(job, dict) else None)
    report.check("fifty reminders are allowed", all(ids), str(ids[-3:]))
    refused = rig.add(text="one too many", local_time="2026-10-30T09:00")
    report.check("the 51st is refused with a sentence the model can say",
                 refused == "You already have 50 reminders; cancel some "
                 "first.", str(refused))
    refused = rig.add(seconds=60)
    report.check("and so is a 51st timer", "50 reminders" in str(refused))
    rig.ask("reminders.cancel", {"id": ids[0]})
    report.check("cancelling one makes room",
                 isinstance(rig.add(seconds=60), dict))

    clean = SchedRig(root, "limits-rules")
    ok = clean.add(text="x" * 200, local_time="2026-10-03T09:00")
    report.check("200 characters are fine", isinstance(ok, dict))
    for kw, needle, why in (
            (dict(text="x" * 201, local_time="2026-10-03T09:00"),
             "200 characters", "text of 201 characters"),
            (dict(text="", local_time="2026-10-03T09:00"), "something to "
             "say", "an empty reminder"),
            (dict(text="a", local_time="2026-10-02T17:00"), "already passed",
             "a time in the past"),
            (dict(text="a", local_time="2026-10-02T17:50"), "already passed",
             "this very minute"),
            (dict(text="a", local_time="soon"), "written like",
             "a time that is not a time"),
            (dict(text="a", local_time="2026-10-03T09:00+02:00"), "offset",
             "a time with its own offset"),
            (dict(text="a", local_time="2026-10-03T09:00", tz="Mars/Base"),
             "unknown timezone", "an unknown timezone"),
            (dict(text="a", local_time="2026-10-03T09:00", repeat="hourly"),
             "daily, weekdays", "an unsupported repeat"),
            (dict(text="a", local_time="2026-10-03T09:00",
                  repeat="weekly:XX"), "daily, weekdays",
             "a day that does not exist"),
            (dict(text="a"), "either", "neither a time nor a length"),
            (dict(text="a", seconds=5, local_time="2026-10-03T09:00"),
             "not both", "both"),
            (dict(text=12, seconds=5), "words", "text that is a number")):
        message = clean.add(**kw)
        report.check("refused, with a reason: %s" % why,
                     isinstance(message, str) and needle in message,
                     str(message))
    report.check("a bad request created nothing",
                 len(clean.jobs()) == 1)

    unknown = clean.ask("reminders.cancel", {"id": 9999})
    report.check("cancelling a reminder that is not there says so",
                 unknown.error == "There is no reminder number 9999.",
                 str(unknown.error))
    clean.ask("reminders.cancel", {"id": ok["id"]})
    again = clean.ask("reminders.cancel", {"id": ok["id"]})
    report.check("cancelling twice is refused", "already cancelled"
                 in str(again.error), str(again.error))
    for args, why in (({"id": "one"}, "a text id"),
                      ({"id": True}, "a bool id"), (None, "no id")):
        bad = clean.ask("reminders.cancel", args)
        report.check("cancel with %s is refused" % why, bad.error)

    report.check("an unknown command argument shape is an error, not a crash",
                 clean.ask("reminders.add", "remind me").error)

    # What the page and the model see.
    cmd = clean.ask("reminders.add", {"text": "water the plants",
                                      "local_time": "2026-10-02T18:30"})
    report.check("reminders.add answers ok, with the id and the words",
                 cmd.result["ok"] is True and cmd.result["id"]
                 and cmd.result["say"] == "half past six in the evening",
                 str(cmd.result))
    via_web = clean.ask("reminders.add", {"text": "x", "in_seconds": 30,
                                          "source": "web"})
    report.check("in_seconds is accepted, as the page's API spells it",
                 via_web.error is None
                 and via_web.result["reminder"]["source"] == "web")
    report.check("a made-up source is refused",
                 clean.ask("reminders.add", {"text": "x", "seconds": 30,
                                             "source": "psychic"}).error)
    listing = clean.ask("reminders.list", {"state": "pending"})
    report.check("reminders.list filters by state, soonest first",
                 all(r["state"] == "pending" for r in
                     listing.result["reminders"])
                 and [r["due_wall"] for r in listing.result["reminders"]]
                 == sorted(r["due_wall"] for r in
                           listing.result["reminders"]))
    report.check("the list never shows the boot-local timer target",
                 all("due_mono" not in r for r in
                     clean.ask("reminders.list").result["reminders"]))
    plain = Clocked()
    nothing = ev.Command("reminders.list")
    plain.device.events.post(nothing)
    plain.step(0.1)
    report.check("a device with no scheduler says so instead of failing",
                 nothing.error and "not available" in nothing.error)
    rig.close()
    clean.close()


def scenario_announcements(report, root):
    from firmware.scheduler import announce

    rig = SchedRig(root, "announce")
    mum = rig.add(text="call mum", local_time="2026-10-02T18:00")
    rig.jump(10 * 60)
    start = rig.clock.mono()
    rig.run(1.0)
    report.check("the chime is the reminder's, and is decoded from the file",
                 len(rig.plays) == 1
                 and rig.plays[0][1] == len(announce.load_chime(
                     "chime_reminder.wav").pcm))
    report.check("the face is the alert face while it speaks",
                 rig.device.face.current == "face_talking")
    report.check("the announcer says it is announcing",
                 rig.device.summary()["announcing"]["ringing"] == mum["id"])

    rig.run(12.0)
    report.check("after the acknowledgement window the face settles",
                 rig.device.face.current != "face_talking")
    report.check("but the reminder is still ringing, waiting to be heard",
                 rig.state_of(mum["id"]) == "ringing")

    rig.run(60.0)
    rig.run(60.0)
    report.check("unacknowledged, it repeats twice more, a minute apart",
                 len(rig.said) == 3 and len(rig.plays) == 3, "%d %d" % (
                     len(rig.said), len(rig.plays)))
    gaps = [rig.plays[i + 1][0] - rig.plays[i][0] for i in range(2)]
    report.check("exactly a minute apart", all(abs(g - 60) < 0.6
                                               for g in gaps), str(gaps))
    rig.run(20.0)
    report.check("then it becomes missed", rig.state_of(mum["id"])
                 == "missed", rig.state_of(mum["id"]))
    report.check("and shows on the page's missed list",
                 [j["id"] for j in rig.jobs("missed")] == [mum["id"]])
    report.check("the miss is in the owner's event log", rig.db.conn().execute(
        "SELECT COUNT(*) FROM events_log WHERE kind = 'reminder_missed'"
    ).fetchone()[0] == 1)
    report.check("and the device is quiet again",
                 rig.announcer.current is None)

    # A button during a repeat gap still acknowledges, and consumes the press.
    again = rig.add(text="stretch", local_time="2026-10-02T19:00")
    rig.advance_to(local_epoch(2026, 10, 2, 19, 0))
    rig.run(30.0)
    shutdowns = rig.hal.system.shutdowns
    volume = rig.hal.speaker.get_volume()
    rig.press(POWER)
    report.check("a power press while it rings acknowledges instead of "
                 "shutting the device down",
                 rig.state_of(again["id"]) == "done"
                 and rig.hal.system.shutdowns == shutdowns)
    rig.press(VOL_UP)
    report.check("and the next volume press is a volume press again",
                 rig.hal.speaker.get_volume() > volume)

    # Snooze.
    snz = rig.add(text="call the bank", local_time="2026-10-02T20:00")
    rig.advance_to(local_epoch(2026, 10, 2, 20, 0))
    rig.run(2.0)
    answer = rig.ask("reminders.snooze", {"id": snz["id"], "minutes": 5})
    report.check("snoozing stops the alert and moves it five minutes on",
                 answer.error is None and not rig.announcer.active
                 and rig.state_of(snz["id"]) == "pending"
                 and abs(rig.scheduler.get(snz["id"])["due_wall"]
                         - (rig.clock.wall() + 300)) < 1)
    rig.jump(299)
    report.check("it does not ring early", rig.state_of(snz["id"])
                 == "pending")
    rig.jump(2)
    report.check("and rings when the five minutes are up",
                 rig.state_of(snz["id"]) == "ringing")
    rig.run(1.0)
    rig.press()
    report.check("then can be acknowledged", rig.state_of(snz["id"])
                 == "done")

    bad = rig.ask("reminders.snooze", {"id": snz["id"], "minutes": 5})
    report.check("a finished reminder cannot be snoozed",
                 bad.error and "already done" in bad.error, str(bad.error))
    bad = rig.ask("reminders.snooze", {"id": mum["id"], "minutes": 0})
    report.check("snooze needs a sensible length",
                 bad.error and "between 1 minute" in bad.error)

    # A timer: rings every ten seconds, says its line once, gives up at five
    # minutes.
    rig2 = SchedRig(root, "announce-timer")
    timer = rig2.add(seconds=60)
    rig2.jump(60)
    rig2.run(1.0)
    first_ring = rig2.plays[0][0]
    rig2.run(310.0)
    report.check("an unstopped timer rings every ten seconds",
                 abs((rig2.plays[1][0] - rig2.plays[0][0]) - 10) < 0.6
                 and abs((rig2.plays[2][0] - rig2.plays[1][0]) - 10) < 0.6,
                 str([p[0] - first_ring for p in rig2.plays[:4]]))
    report.check("for five minutes and no longer",
                 28 <= len(rig2.plays) <= 31, str(len(rig2.plays)))
    report.check("saying its line only the first time",
                 rig2.said == ["Your one-minute timer is done."],
                 str(rig2.said))
    report.check("then stops and is missed",
                 rig2.state_of(timer["id"]) == "missed"
                 and rig2.announcer.current is None)

    # A cancelled reminder stops ringing.
    rig3 = SchedRig(root, "announce-cancel")
    c = rig3.add(text="never mind", local_time="2026-10-02T18:00")
    rig3.jump(10 * 60)
    rig3.run(1.0)
    rig3.ask("reminders.cancel", {"id": c["id"]})
    report.check("cancelling a reminder that is ringing stops it",
                 rig3.announcer.current is None
                 and rig3.state_of(c["id"]) == "cancelled")
    for r in (rig, rig2, rig3):
        r.close()


def scenario_announcement_timing(report, root):
    import types

    # Due in the middle of a turn: held, and said after the reply.
    rig = SchedRig(root, "timing-turn",
                   wall=local_epoch(2026, 10, 2, 17, 59) + 57.0)
    pipeline = ScriptedPipeline(rig.hal.speaker, seed=11)
    rig.device.pipeline = pipeline
    job = rig.add(text="call mum", local_time="2026-10-02T18:00")
    pipeline.trigger()
    rig.run(0.5)
    report.check("a turn is under way", rig.device.turn is not None)
    for _ in range(40):
        rig.run(0.25)
        if rig.state_of(job["id"]) == "ringing":
            break
    report.check("a reminder due mid-turn is held, not spoken over it",
                 rig.state_of(job["id"]) == "ringing"
                 and rig.device.turn is not None
                 and not rig.announcer.active and not rig.said)
    ended = None
    for _ in range(400):
        rig.run(0.25)
        if rig.device.turn is None and ended is None:
            ended = rig.clock.mono()
        if rig.said:
            break
    report.check("and announced once the reply is over",
                 rig.said == ["Reminder: call mum."] and ended is not None,
                 str(rig.said))
    # The scripted turn plays its own reply through the speaker too; the
    # chime is the first sound after the turn is over.
    started = min(t for t, _ in rig.plays if t >= ended)
    report.check("after a pause of no more than two seconds",
                 0 <= started - ended <= 2.5, "%.2f" % (started - ended))
    rig.press()

    # Due while muted: announced anyway, and it shows.
    rig = SchedRig(root, "timing-muted")
    job = rig.add(text="call mum", local_time="2026-10-02T18:00")
    rig.hal.set_mute(True)
    rig.run(1.0)
    rig.advance_to(local_epoch(2026, 10, 2, 18, 0))
    rig.run(1.0)
    report.check("a reminder due while muted is announced anyway",
                 len(rig.plays) == 1 and rig.said == ["Reminder: call mum."])
    report.check("and the screen shows the alert, not the muted face",
                 rig.device.face.current == "face_talking",
                 rig.device.face.current)
    rig.press()
    rig.run(2.0)
    report.check("afterwards the muted face is back",
                 rig.device.face.current == "face_muted")

    # Quiet hours: a reminder is soft, a timer is not.
    rig = SchedRig(root, "timing-quiet",
                   wall=local_epoch(2026, 10, 2, 22, 50))
    normal = rig.hal.speaker.get_volume()
    seen = []
    real_set = rig.hal.speaker.set_volume
    rig.hal.speaker.set_volume = lambda v: (seen.append(v), real_set(v))
    job = rig.add(text="lights out", local_time="2026-10-02T23:00")
    rig.advance_to(local_epoch(2026, 10, 2, 23, 0))
    rig.run(1.0)
    report.check("in quiet hours a reminder is spoken at the quiet volume",
                 abs(rig.hal.speaker.get_volume() - 0.4) < 1e-9
                 and rig.said == ["Reminder: lights out."],
                 str(rig.hal.speaker.get_volume()))
    rig.press()
    report.check("and the volume goes back afterwards",
                 abs(rig.hal.speaker.get_volume() - normal) < 1e-9,
                 str(rig.hal.speaker.get_volume()))
    timer = rig.add(seconds=60)
    rig.jump(60)
    rig.run(1.0)
    report.check("a timer at the same hour rings at the normal volume",
                 abs(rig.hal.speaker.get_volume() - normal) < 1e-9)
    rig.press()
    report.check("the saved volume was never changed by any of it",
                 rig.device.settings.get("voice", "volume") == normal)

    day = SchedRig(root, "timing-day", wall=local_epoch(2026, 10, 2, 12, 0))
    day.add(text="lunch", local_time="2026-10-02T12:30")
    day.advance_to(local_epoch(2026, 10, 2, 12, 30))
    day.run(1.0)
    report.check("by day a reminder is at the normal volume",
                 abs(day.hal.speaker.get_volume() - normal) < 1e-9)
    day.press()

    off = SchedRig(root, "timing-quiet-off",
                   wall=local_epoch(2026, 10, 2, 22, 50),
                   settings_patch={"quiet": {"enabled": False}})
    off.add(text="late", local_time="2026-10-02T23:00")
    off.advance_to(local_epoch(2026, 10, 2, 23, 0))
    off.run(1.0)
    report.check("with quiet hours switched off it is not soft at night",
                 abs(off.hal.speaker.get_volume() - normal) < 1e-9)
    off.press()

    # An update going in: hold, then announce.
    rig = SchedRig(root, "timing-update")
    rig.device.updater = types.SimpleNamespace(
        state="installing", trial_tick=lambda *a: None, tick=lambda: None)
    job = rig.add(text="call mum", local_time="2026-10-02T18:00")
    rig.jump(10 * 60)
    rig.run(5.0)
    report.check("a reminder due while an update installs is held",
                 rig.state_of(job["id"]) == "ringing" and not rig.said)
    rig.device.updater.state = "idle"
    rig.run(5.0)
    report.check("and spoken once it is out of the way",
                 rig.said == ["Reminder: call mum."])
    rig.press()

    # After a restart a held job is simply due again.
    rig = SchedRig(root, "timing-restart")
    job = rig.add(text="call mum", local_time="2026-10-02T18:00")
    rig.jump(10 * 60)
    report.check("it is ringing when the power goes",
                 rig.state_of(job["id"]) == "ringing")
    after = rig.reboot(off_seconds=120)
    after.run(5.0)
    report.check("after the restart it counts as due, and is said",
                 any("call mum" in line for line in after.said),
                 str(after.said))
    after.close()


def scenario_repeats(report, root):
    import datetime

    from firmware.scheduler import clock as sched_clock
    from firmware.scheduler import repeat

    report.check("repeat spellings are normalised",
                 repeat.normalise("Weekly:we,MO") == "weekly:MO,WE"
                 and repeat.normalise("DAILY") == "daily"
                 and repeat.normalise("weekdays") == "weekdays"
                 and repeat.normalise("weekly:MO,TU,WE,TH,FR") == "weekdays"
                 and repeat.normalise("") is None)

    # Every day at 7, across the night the clocks go back (Sunday 25 October
    # 2026: 03:00 becomes 02:00, so that day is 25 hours long).
    rig = SchedRig(root, "repeat-dst",
                   wall=local_epoch(2026, 10, 23, 12, 0))
    job = rig.add(text="wake up", local_time="2026-10-24T07:00",
                  repeat="daily")
    report.check("a repeating reminder says what it is",
                 job["kind"] == "repeat" and job["repeat"] == "daily")

    fired = []
    for day in (24, 25, 26, 27):
        rig.advance_to(local_epoch(2026, 10, day, 6, 59) + 59.0)
        report.check("on the %dth it has not fired a second early" % day,
                     rig.state_of(job["id"]) == "pending")
        rig.jump(1.0)
        row = rig.scheduler.get(job["id"])
        fired.append(row["fired_at"])
        rig.run(1.0)
        rig.press()
        report.check("and goes back to waiting after it is acknowledged "
                     "(the %dth)" % day,
                     rig.state_of(job["id"]) == "pending")
    hours = [(sched_clock.to_local(t, TZ).hour,
              sched_clock.to_local(t, TZ).minute) for t in fired]
    report.check("it fired at 07:00 local every day, across the clock change",
                 hours == [(7, 0)] * 4, str(hours))
    gaps = [round((fired[i + 1] - fired[i]) / 3600.0) for i in range(3)]
    report.check("with 25 hours between the 24th and the 25th",
                 gaps == [25, 24, 24], str(gaps))

    # Weekdays skip the weekend.
    wk = SchedRig(root, "repeat-weekdays",
                  wall=local_epoch(2026, 10, 1, 12, 0))
    job = wk.add(text="standup", local_time="2026-10-02T09:00",
                 repeat="weekdays")
    days = []
    for _ in range(3):
        due = wk.scheduler.get(job["id"])["due_wall"]
        wk.advance_to(due + 0.5)
        wk.run(1.0)
        wk.press()
        days.append(sched_clock.to_local(due, TZ).strftime("%a"))
    report.check("a weekday repeat goes Friday, then Monday, then Tuesday",
                 days == ["Fri", "Mon", "Tue"], str(days))

    named = SchedRig(root, "repeat-weekly",
                     wall=local_epoch(2026, 10, 1, 12, 0))
    job = named.add(text="bins", local_time="2026-10-05T08:00",
                    repeat="weekly:MO,WE")
    seen = []
    for _ in range(3):
        due = named.scheduler.get(job["id"])["due_wall"]
        named.advance_to(due + 0.5)
        named.run(1.0)
        named.press()
        seen.append(sched_clock.to_local(due, TZ).strftime("%a %d"))
    report.check("a weekly repeat on named days goes Monday, Wednesday, "
                 "Monday", seen == ["Mon 05", "Wed 07", "Mon 12"], str(seen))

    # A missed repeat carries on rather than disappearing.
    miss = SchedRig(root, "repeat-missed",
                    wall=local_epoch(2026, 10, 1, 12, 0))
    job = miss.add(text="vitamins", local_time="2026-10-02T08:00",
                   repeat="daily")
    miss.advance_to(local_epoch(2026, 10, 2, 8, 0))
    miss.run(200.0)
    state = miss.scheduler.get(job["id"])
    report.check("a repeat nobody acknowledged is waiting for tomorrow",
                 state["state"] == "pending"
                 and sched_clock.to_local(state["due_wall"], TZ).day == 3,
                 str(state))
    report.check("and the miss was noted for the owner",
                 miss.db.conn().execute(
                     "SELECT COUNT(*) FROM events_log WHERE kind = "
                     "'reminder_missed'").fetchone()[0] == 1)

    # A repeat in a spring-forward gap lands an hour later rather than
    # vanishing: 02:30 does not exist on Sunday 29 March 2026.
    naive = datetime.datetime(2026, 3, 29, 2, 30)
    epoch = sched_clock.to_epoch(naive, TZ)
    local = sched_clock.to_local(epoch, TZ)
    report.check("a time that does not exist lands after the gap",
                 (local.hour, local.minute) == (3, 30), str(local))
    for r in (rig, wk, named, miss):
        r.close()


def scenario_boot_after_off(report, root):
    # Two reminders due while the device was off for three hours.
    rig = SchedRig(root, "off-short", wall=local_epoch(2026, 10, 2, 9, 0))
    rig.add(text="call mum", local_time="2026-10-02T10:00")
    rig.add(text="take the bins out", local_time="2026-10-02T10:30")
    after = rig.reboot(off_seconds=3 * 3600)
    after.run(10.0)
    report.check("after three hours off, two due reminders are one "
                 "announcement",
                 after.said == ["While I was off, you had two reminders: "
                                "call mum, and take the bins out."],
                 str(after.said))
    report.check("with a single chime", len(after.plays) == 1)
    report.check("and neither is left ringing or missed",
                 {j["state"] for j in after.jobs()} == {"done"})
    again = after.reboot(off_seconds=60)
    again.run(10.0)
    report.check("restarting again does not say it a second time",
                 again.said == [])

    one = SchedRig(root, "off-one", wall=local_epoch(2026, 10, 2, 9, 0))
    one.add(text="call mum", local_time="2026-10-02T10:00")
    gone = one.reboot(off_seconds=2 * 3600)
    gone.run(10.0)
    report.check("a single one is worded for one",
                 gone.said == ["While I was off, you had a reminder: call "
                               "mum."], str(gone.said))

    three = SchedRig(root, "off-three", wall=local_epoch(2026, 10, 2, 9, 0))
    for n, text in enumerate(("milk", "eggs", "bread")):
        three.add(text=text, local_time="2026-10-02T10:%02d" % (10 + n))
    back = three.reboot(off_seconds=2 * 3600)
    back.run(10.0)
    report.check("three are listed with the last one after 'and'",
                 back.said == ["While I was off, you had three reminders: "
                               "milk, eggs, and bread."], str(back.said))

    # Two days off: older than six hours, so no announcement, just missed.
    long_off = SchedRig(root, "off-long", wall=local_epoch(2026, 10, 2, 9, 0))
    a = long_off.add(text="call mum", local_time="2026-10-02T10:00")
    b = long_off.add(text="take the bins out", local_time="2026-10-02T10:30")
    d = long_off.add(text="vitamins", local_time="2026-10-03T08:00",
                     repeat="daily")
    after = long_off.reboot(off_seconds=2 * 86400)
    after.run(10.0)
    report.check("after two days off nothing is announced", after.said == [])
    report.check("both reminders are marked missed, for the page",
                 after.state_of(a["id"]) == "missed"
                 and after.state_of(b["id"]) == "missed")
    nxt = after.scheduler.get(d["id"])
    from firmware.scheduler import clock as sched_clock
    report.check("a repeat skips what it missed and waits for its next "
                 "firing",
                 nxt["state"] == "pending"
                 and nxt["due_wall"] > after.clock.wall()
                 and sched_clock.to_local(nxt["due_wall"], TZ).hour == 8,
                 str(nxt))
    after.run(3.0)
    report.check("it does not fire three times after a weekend off",
                 after.said == [])

    # Exactly at the edge of six hours.
    edge = SchedRig(root, "off-edge", wall=local_epoch(2026, 10, 2, 9, 0))
    e1 = edge.add(text="inside", local_time="2026-10-02T10:00")
    e2 = edge.add(text="outside", local_time="2026-10-02T09:30")
    over = edge.reboot(wall=local_epoch(2026, 10, 2, 15, 45))
    over.run(10.0)
    report.check("a reminder that came due 5h45 ago is said; 6h15 ago it "
                 "is missed",
                 over.said == ["While I was off, you had a reminder: "
                               "inside."]
                 and over.state_of(e2["id"]) == "missed", str(over.said))

    # A timer a reboot cut off.
    t = SchedRig(root, "off-timer")
    timer = t.add(seconds=600, text="pasta")
    plain = t.add(seconds=300)
    t.jump(60)
    back = t.reboot(off_seconds=30)
    report.check("a timer that a restart interrupted is marked so",
                 back.state_of(timer["id"]) == "interrupted"
                 and back.state_of(plain["id"]) == "interrupted")
    back.run(10.0)
    report.check("and the owner is told once, at the next idle moment",
                 back.said == ["Some timers you set were interrupted by a "
                               "restart."], str(back.said))
    back.close()
    solo = SchedRig(root, "off-timer-solo")
    only = solo.add(seconds=600, text="pasta")
    back = solo.reboot(off_seconds=5)
    back.run(10.0)
    report.check("with the label when there is one",
                 back.said == ["The timer you set for the pasta was "
                               "interrupted by a restart."], str(back.said))
    again = back.reboot(off_seconds=5)
    again.run(10.0)
    report.check("and not again on the next restart", again.said == [])


def scenario_housekeeping_jobs(report, root):
    from firmware.scheduler import housekeeping as hk

    rig = SchedRig(root, "housekeeping", synced=False)
    heard = []
    rig.device.on(ev.CLOCK_SYNCED, lambda e: heard.append(e.data))
    rig.run(5.0, 1.0)
    report.check("until the clock is right, it is checked every second",
                 not rig.scheduler.synced and heard == [])
    rig.clock.set_synced(True)
    rig.run(1.5, 0.5)
    report.check("ClockSynced is posted when it is", heard == [None]
                 and rig.scheduler.synced, str(heard))
    rig.run(120.0, 1.0)
    report.check("and not again", heard == [None])

    heard.clear()
    rig.clock.set_wall(rig.clock.wall() + 3 * 86400)
    rig.run(65.0, 1.0)
    report.check("a jump of days in the wall clock is noticed within a minute",
                 heard == ["jump"], str(heard))

    # A reminder survives the jump with the right due time.
    rig2 = SchedRig(root, "housekeeping-jump",
                    wall=local_epoch(2026, 10, 2, 17, 0))
    job = rig2.add(text="call mum", local_time="2026-10-02T18:00")
    rig2.clock.set_wall(local_epoch(2026, 10, 2, 17, 30))
    rig2.run(65.0, 1.0)
    report.check("after the clock is corrected the reminder is still for six",
                 rig2.state_of(job["id"]) == "pending"
                 and rig2.scheduler.get(job["id"])["local_time"]
                 == "2026-10-02T18:00")
    rig2.advance_to(local_epoch(2026, 10, 2, 18, 0))
    report.check("and fires then", rig2.state_of(job["id"]) == "ringing")

    # Timers' displayed wall time is retargeted when the clock is corrected.
    cold = SchedRig(root, "housekeeping-timer", synced=False,
                    wall=local_epoch(2026, 1, 1, 0, 0))
    timer = cold.add(seconds=600)
    cold.clock.set_wall(local_epoch(2026, 10, 2, 17, 50))
    cold.clock.set_synced(True)
    cold.run(3.0, 0.5)
    shown = cold.scheduler.get(timer["id"])["due_wall"]
    report.check("a timer's wall time is corrected when the clock is, "
                 "without moving the timer",
                 abs(shown - (cold.clock.wall() + cold.scheduler.get(
                     timer["id"])["remaining"])) < 2
                 and cold.state_of(timer["id"]) == "pending")

    # The network.
    net = SchedRig(root, "housekeeping-net")
    changes = []
    net.device.on(ev.NETWORK_CHANGED, lambda e: changes.append(e.data))
    net.run(35.0, 1.0)
    net.hal.system.is_online = False
    net.run(35.0, 1.0)
    net.hal.system.is_online = True
    net.run(35.0, 1.0)
    report.check("the network check reports a drop and the return, once each",
                 changes == [False, True], str(changes))

    # The temperature.
    hot = SchedRig(root, "housekeeping-thermal")
    hot.run(15.0, 0.5)
    report.check("a cool device runs at the normal rates",
                 abs(hot.device.frame_interval() - 1 / 10) < 1e-9)
    hot.hal.system.temp_c = 77.0
    hot.run(15.0, 0.5)
    report.check("above 75C the idle face drops to 5Hz",
                 hot.device.summary()["thermal"] == "warm"
                 and abs(hot.device.frame_interval() - 1 / 5) < 1e-9)
    hot.hal.system.temp_c = 82.0
    hot.run(15.0, 0.5)
    hot.device.fail("test", hot.clock.mono())
    report.check("above 80C even an error holds to 10Hz",
                 hot.device.summary()["thermal"] == "hot"
                 and abs(hot.device.frame_interval() - 1 / 10) < 1e-9)
    hot.hal.system.temp_c = 50.0
    hot.run(15.0, 0.5)
    report.check("and it recovers when it cools",
                 hot.device.summary()["thermal"] == "normal")

    # Weekly maintenance: Sunday 04:00 local, prunes old usage rows.
    maint = SchedRig(root, "housekeeping-db",
                     wall=local_epoch(2026, 10, 3, 12, 0))
    conn = maint.db.conn()
    old = maint.clock.wall() - 14 * 31 * 86400
    new = maint.clock.wall() - 5 * 86400
    for at in (old, new):
        conn.execute("INSERT INTO usage (at, day, turn_id, provider, model, "
                     "kind, status) VALUES (?, '2026-01-01', 't', 'p', 'm', "
                     "'chat', 'ok')", (at,))
    maint.run(60.0, 1.0)
    report.check("maintenance does not run on a Saturday afternoon",
                 conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
                 == 2)
    maint.advance_to(local_epoch(2026, 10, 4, 4, 0) + 1.0)
    maint.run(2.0, 1.0)
    left = conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
    report.check("on Sunday at 04:00 it runs, and drops usage rows older "
                 "than 13 months", left == 1, str(left))
    maint.run(86400.0 / 4, 600.0)
    report.check("and keeps the recent ones",
                 conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
                 == 1)
    report.check("the thresholds are the plan's",
                 hk.WARM_C == 75.0 and hk.HOT_C == 80.0
                 and hk.NET_CHECK == 30.0 and hk.THERMAL_CHECK == 10.0)
    for r in (rig, rig2, cold, net, hot, maint):
        r.close()


def scenario_spoken_time(report, root):
    import datetime
    import wave

    from firmware.scheduler import announce, speech

    at = lambda h, m=0, d=2: datetime.datetime(2026, 10, d, h, m)
    now = at(9, 0)
    table = [
        (at(18, 0), "six in the evening"),
        (at(7, 0), "seven in the morning"),
        (at(7, 30), "half past seven in the morning"),
        (at(7, 15), "quarter past seven in the morning"),
        (at(7, 45), "quarter to eight in the morning"),
        (at(7, 5), "seven oh five in the morning"),
        (at(7, 20), "seven twenty in the morning"),
        (at(13, 40), "one forty in the afternoon"),
        (at(12, 0), "noon"),
        (at(0, 0), "midnight"),
        (at(23, 0), "eleven at night"),
        (at(3, 0), "three at night"),
        (at(21, 59), "nine fifty-nine in the evening"),
    ]
    for when, words in table:
        report.check("%s is said '%s'" % (when.strftime("%H:%M"), words),
                     speech.spoken_time(when) == words,
                     speech.spoken_time(when))

    report.check("a time tomorrow says tomorrow",
                 speech.spoken_when(at(7, 0, 3), now)
                 == "tomorrow at seven in the morning")
    report.check("this week it gives the day",
                 speech.spoken_when(at(18, 0, 5), now)
                 == "on Monday at six in the evening")
    report.check("further out it gives the date",
                 speech.spoken_when(at(18, 0, 20), now)
                 == "on 20 October at six in the evening")

    sl = [(at(18, 0), "ob šestih zvečer"), (at(7, 0), "ob sedmih zjutraj"),
          (at(7, 30), "ob pol osmih zjutraj"),
          (at(15, 5), "ob treh in pet minut popoldne"),
          (at(15, 1), "ob treh in ena minuta popoldne"),
          (at(15, 22), "ob treh in dvaindvajset minut popoldne"),
          (at(12, 0), "opoldne"), (at(0, 0), "ob polnoči"),
          (at(1, 0), "ob eni ponoči")]
    for when, words in sl:
        report.check("Slovenian %s is '%s'" % (when.strftime("%H:%M"), words),
                     speech.spoken_time(when, "sl") == words,
                     speech.spoken_time(when, "sl"))
    report.check("Slovenian gives 'jutri' for tomorrow",
                 speech.spoken_when(at(7, 0, 3), now, "sl")
                 == "jutri ob sedmih zjutraj")

    report.check("numbers read in English",
                 [speech.number_en(n) for n in (0, 7, 13, 20, 21, 99)]
                 == ["zero", "seven", "thirteen", "twenty", "twenty-one",
                     "ninety-nine"])
    report.check("lines read naturally",
                 speech.reminder_line("call mum.") == "Reminder: call mum."
                 and speech.timer_line("", 7200) == "Your two-hour timer "
                 "is done."
                 and speech.timer_line("", 30) == "Your thirty-second "
                 "timer is done.")

    for name, label in (("chime_reminder.wav", "reminder"),
                        ("chime_timer.wav", "timer")):
        path = os.path.join(announce.SOUNDS, name)
        with wave.open(path, "rb") as fh:
            ok = (fh.getframerate() == 16000 and fh.getnchannels() == 1
                  and fh.getsampwidth() == 2
                  and fh.getnframes() / 16000.0 < 1.0)
            seconds = fh.getnframes() / 16000.0
        report.check("the %s chime is 16kHz mono 16-bit and under a second "
                     "(%.2fs)" % (label, seconds), ok)

    from tools import make_chimes

    report.check("the two chimes are different sounds",
                 announce.load_chime("chime_reminder.wav").pcm
                 != announce.load_chime("chime_timer.wav").pcm)
    for name, make in (("chime_reminder.wav", make_chimes.reminder),
                       ("chime_timer.wav", make_chimes.timer)):
        out = os.path.join(root, "regen-" + name)
        make_chimes.write(out, make())
        with open(out, "rb") as a, open(os.path.join(announce.SOUNDS, name),
                                        "rb") as b:
            same = a.read() == b.read()
        report.check("%s is exactly what tools/make_chimes.py makes" % name,
                     same)

    missing = announce.load_chime("not_there.wav")
    report.check("a missing chime is silence, not a crash",
                 missing.pcm == b"" and missing.seconds == 0.0)


class ApiRig:
    """The page's API in front of a SchedRig. The commander runs a tick, which
    is what the main loop would do on its own thread."""

    def __init__(self, root, name, **kw):
        self.rig = SchedRig(root, name, **kw)
        rig = self.rig
        config = UpdateConfig(repo="emulator/trstenko", data_root=rig.data,
                              install_root=rig.data, auto_check=False)
        self.manager = UpdateManager(
            config, FakeReleases(kbps=1e9, clock=rig.clock.mono),
            rig.hal.system, clock=rig.clock.mono,
            on_event=lambda kind, payload: None, on_restart=lambda: None)
        self.auth = Auth(rig.data, clock=rig.clock.wall)
        self.api = UpdateApi(self.manager, rig.device, self.auth,
                             commander=self._pump)
        self.token = None

    def _pump(self, cmd):
        self.rig.device.events.post(cmd)
        self.rig.tick()

    def pair(self):
        status, body = self.api.dispatch("POST", "/api/auth",
                                         {"pin": self.auth.pin})
        self.token = body["token"]
        return self.token

    def call(self, method, path, body=None, query=None, token=True):
        return self.api.dispatch(method, path, body,
                                 self.token if token else None, query)


class Pump:
    """Runs the device's tick on a thread of its own, as the robot's main loop
    does, so a real HTTP request can be answered."""

    def __init__(self, device):
        self.device = device
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            self.device.tick()
            self.device.events.wait(0.005)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(2.0)


def scenario_api_v1(report, root):
    import json as json_mod

    rig = ApiRig(root, "api")
    r = rig.rig
    v1 = ("/api/v1/info", "/api/v1/state", "/api/v1/reminders",
          "/api/v1/settings", "/api/v1/reminders/1/cancel",
          "/api/v1/reminders/1/snooze")
    for path in v1:
        for method in ("GET", "POST", "PATCH"):
            status, body = rig.call(method, path, {}, token=False)
            if status != 401 or not body.get("needs_pin"):
                break
        else:
            continue
        report.check("%s needs a token" % path, False, "%s %s" % (method,
                                                                 status))
        break
    else:
        report.check("every v1 endpoint answers 401 without a token", True)
    status, _ = rig.call("GET", "/api/v1/info", token=False)
    report.check("and a wrong token is no better",
                 rig.api.dispatch("GET", "/api/v1/info", None, "nope")[0]
                 == 401 and status == 401)

    rig.pair()
    status, info = rig.call("GET", "/api/v1/info")
    report.check("info says which API this is and who is answering",
                 status == 200 and info["api_version"] == 1
                 and "reminders" in info["features"]
                 and info["product"]["model"] == "Trstenko"
                 and info["product"]["serial"], str(info)[:120])

    status, state = rig.call("GET", "/api/v1/state")
    report.check("state is everything Home needs in one call",
                 status == 200 and state["device"]["face"]
                 and state["time"]["synced"] is True
                 and state["time"]["timezone"] == TZ
                 and state["reminders"]["next"] is None
                 and state["alerts"] == [] and "state" in state["update"],
                 str(state)[:160])

    # A reminder made through the API, announced on the fake clock.
    status, made = rig.call("POST", "/api/v1/reminders",
                            {"text": "call mum",
                             "local_time": "2026-10-02T18:00"})
    report.check("a reminder is created with a 201 and says when",
                 status == 201 and made["ok"] and made["id"]
                 and made["say"] == "six in the evening"
                 and made["reminder"]["source"] == "web", str(made)[:140])
    status, listing = rig.call("GET", "/api/v1/reminders")
    report.check("it shows in the list",
                 [x["id"] for x in listing["reminders"]] == [made["id"]])
    status, state = rig.call("GET", "/api/v1/state")
    report.check("and as the next reminder on Home",
                 state["reminders"]["next"]["id"] == made["id"])

    r.advance_to(local_epoch(2026, 10, 2, 18, 0))
    r.run(1.5)
    report.check("on the fake clock, at six, it is announced",
                 r.said == ["Reminder: call mum."] and len(r.plays) == 1,
                 str(r.said))
    status, state = rig.call("GET", "/api/v1/state")
    report.check("Home knows it is ringing",
                 state["reminders"]["ringing"] == made["id"]
                 and state["device"]["announcing"]["announcing"])
    r.press()
    status, done = rig.call("GET", "/api/v1/reminders", query={"state":
                                                               "done"})
    report.check("once acknowledged it is under 'done'",
                 [x["id"] for x in done["reminders"]] == [made["id"]])
    status, pending = rig.call("GET", "/api/v1/reminders",
                               query={"state": "pending"})
    report.check("and no longer pending", pending["reminders"] == [])

    status, other = rig.call("POST", "/api/v1/reminders",
                             {"text": "stretch", "in_seconds": 600})
    report.check("a timer-style reminder takes in_seconds",
                 status == 201 and other["reminder"]["kind"] == "timer")
    status, snoozed = rig.call("POST", "/api/v1/reminders/%d/snooze"
                               % other["id"], {"minutes": 15})
    report.check("it can be snoozed", status == 200
                 and snoozed["reminder"]["state"] == "pending")
    status, snoozed = rig.call("POST", "/api/v1/reminders/%d/snooze"
                               % other["id"])
    report.check("snooze with no body uses the default", status == 200)
    status, gone = rig.call("POST", "/api/v1/reminders/%d/cancel"
                            % other["id"])
    report.check("and cancelled", status == 200
                 and gone["reminder"]["state"] == "cancelled")
    status, again = rig.call("POST", "/api/v1/reminders/%d/cancel"
                             % other["id"])
    report.check("cancelling again is a 409 with the reason",
                 status == 409 and "already cancelled" in again["error"],
                 str(again))

    # Refusals, with statuses a page can act on.
    for (method, path, body, query), want, why in (
            (("POST", "/api/v1/reminders", {"text": "a", "local_tim": "x"},
              None), 400, "a misspelt field"),
            (("POST", "/api/v1/reminders", ["a"], None), 400,
             "a body that is not an object"),
            (("POST", "/api/v1/reminders", {"text": "a",
                                            "local_time": "2026-10-02T09:00"},
              None), 400, "a time in the past"),
            (("POST", "/api/v1/reminders", {"text": "x" * 201,
                                            "in_seconds": 5}, None), 400,
             "text that is too long"),
            (("GET", "/api/v1/reminders", None, {"state": "soonish"}), 400,
             "an unknown state filter"),
            (("POST", "/api/v1/reminders/9999/cancel", {}, None), 404,
             "a reminder that is not there"),
            (("POST", "/api/v1/reminders/abc/cancel", {}, None), 404,
             "an id that is not a number"),
            (("POST", "/api/v1/reminders/%d/snooze" % made["id"],
              {"minutes": 0}, None), 400, "a snooze of nothing"),
            (("DELETE", "/api/v1/reminders", None, None), 405,
             "the wrong method"),
            (("POST", "/api/v1/info", {}, None), 405,
             "posting to info"),
            (("GET", "/api/v1/reminders/1/cancel", None, None), 405,
             "getting a cancel"),
            (("GET", "/api/v1/nonsense", None, None), 404,
             "an endpoint that does not exist")):
        status, payload = rig.call(method, path, body, query)
        report.check("%s is a %d" % (why, want), status == want
                     and payload.get("error"), "%s %s" % (status, payload))

    status, payload = rig.call("POST", "/api/v1/reminders",
                               {"text": "a", "in_seconds": 5,
                                "source": "voice"})
    report.check("a client cannot claim to be the voice", status == 400)

    # Settings.
    status, got = rig.call("GET", "/api/v1/settings")
    report.check("settings are readable",
                 status == 200 and got["settings"]["voice"]["volume"] == 0.6
                 and got["settings"]["quiet"]["from"] == "22:00")
    before = json_mod.dumps(got, sort_keys=True)
    status, bad = rig.call("PATCH", "/api/v1/settings",
                           {"voice": {"volume": 9}, "quiet": {"from": "x"},
                            "nonsense": {"a": 1}})
    report.check("a bad settings patch is a 400, listing every problem",
                 status == 400 and all(n in bad["error"] for n in (
                     "voice.volume", "quiet.from", "nonsense")), str(bad))
    status, after = rig.call("GET", "/api/v1/settings")
    report.check("and changed nothing",
                 json_mod.dumps(after, sort_keys=True) == before)
    status, nope = rig.call("PATCH", "/api/v1/settings", ["voice"])
    report.check("a patch that is not an object is a 400", status == 400)
    status, nope = rig.call("PATCH", "/api/v1/settings",
                            {"updates": {"channel": "dev"}})
    report.check("the updater's own settings are refused here",
                 status == 400 and "updates" in nope["error"])
    status, ok = rig.call("PATCH", "/api/v1/settings",
                          {"voice": {"volume": 0.3},
                           "quiet": {"enabled": False}})
    report.check("a good patch is a 200 saying what changed",
                 status == 200 and sorted(ok["changed"])
                 == ["quiet.enabled", "voice.volume"]
                 and ok["settings"]["voice"]["volume"] == 0.3, str(ok)[:100])
    report.check("the speaker followed, through the main thread",
                 abs(r.hal.speaker.get_volume() - 0.3) < 1e-9)
    status, back = rig.call("GET", "/api/v1/settings")
    report.check("and a read shows it", back["settings"]["voice"]["volume"]
                 == 0.3)
    saved = json_mod.load(open(os.path.join(r.data, "settings.json"),
                               encoding="utf-8"))
    report.check("and it is on disk", saved["voice"]["volume"] == 0.3)

    # The main thread too busy to answer.
    rig.api.v1.timeout = 0.05
    rig.api.v1._commander = lambda cmd: None
    start = time.monotonic()
    status, busy = rig.call("GET", "/api/v1/settings")
    report.check("a device that does not answer in time is a 503, not a hang",
                 status == 503 and "busy" in busy["error"]
                 and time.monotonic() - start < 1.0, "%s %s" % (status, busy))
    rig.api.v1._commander = rig._pump
    rig.api.v1.timeout = 2.0

    # A device with no scheduler or settings behind the API.
    bare = Clocked()
    plain = UpdateApi(rig.manager, bare.device, rig.auth,
                      commander=lambda cmd: (bare.device.events.post(cmd),
                                             bare.step(0.1)))
    status, payload = plain.dispatch("GET", "/api/v1/reminders", None,
                                     rig.token, None)
    report.check("with no scheduler it says so, as a 400",
                 status == 400 and "not available" in payload["error"],
                 str(payload))
    status, payload = plain.dispatch("GET", "/api/v1/settings", None,
                                     rig.token, None)
    report.check("and with no settings, a 503", status == 503)
    nodev = UpdateApi(rig.manager, None, rig.auth)
    status, payload = nodev.dispatch("GET", "/api/v1/state", None, rig.token)
    report.check("and with no device at all, a 503",
                 status == 503 and "not ready" in payload["error"])
    status, info = nodev.dispatch("GET", "/api/v1/info", None, rig.token)
    report.check("though info still answers, so an app can check "
                 "compatibility", status == 200)

    # A clock that is not right yet.
    cold = ApiRig(root, "api-cold", synced=False)
    cold.pair()
    status, why = cold.call("POST", "/api/v1/reminders",
                            {"text": "a", "local_time": "2026-10-03T09:00"})
    report.check("a reminder before the clock is known is a 503 that says "
                 "to try again", status == 503
                 and "Try again" in why["error"], str(why))
    status, timer = cold.call("POST", "/api/v1/reminders",
                              {"text": "a", "in_seconds": 30})
    report.check("a timer is fine", status == 201)
    status, state = cold.call("GET", "/api/v1/state")
    report.check("and Home says the clock is not synced",
                 state["time"]["synced"] is False)

    # The limit.
    full = ApiRig(root, "api-full")
    full.pair()
    for n in range(50):
        full.call("POST", "/api/v1/reminders", {"text": "r", "in_seconds": 600})
    status, over = full.call("POST", "/api/v1/reminders",
                             {"text": "r", "in_seconds": 600})
    report.check("the 51st is a 409 with the sentence",
                 status == 409 and "50 reminders" in over["error"],
                 str(over))

    # A key never leaves the device through any of this.
    from firmware.storage.secrets import Secrets

    key = "AIzaSyAPITESTKEYAPITESTKEY00000000zzzz"
    secrets = Secrets.load(os.path.join(r.data, "secrets.json"))
    secrets.set_key("gemini", key)
    r.device.secrets = secrets
    seen = []
    for path in ("/api/v1/info", "/api/v1/state", "/api/v1/settings",
                 "/api/v1/reminders"):
        status, payload = rig.call("GET", path)
        seen.append(json_mod.dumps(payload))
    report.check("no key is in info, state, settings or reminders",
                 all(key not in text for text in seen))
    for r2 in (rig, cold, full):
        r2.rig.close()


def scenario_web_servers(report, root):
    """The same API over real HTTP, from both servers, with the headers."""
    import http.client

    rig = ApiRig(root, "http")
    rig.pair()
    device = rig.rig.device

    needed = {"content-security-policy":
              "default-src 'self'; style-src 'self' 'unsafe-inline'",
              "x-content-type-options": "nosniff",
              "referrer-policy": "no-referrer"}

    def headers_ok(response):
        got = {k.lower(): v for k, v in response.getheaders()}
        return all(got.get(k) == v for k, v in needed.items())

    def exercise(label, request):
        """`request(method, path, body, token)` -> (status, headers_ok, body)."""
        results = {}
        results["page"] = request("GET", "/", None, None)
        results["script"] = request("GET", "/app.js", None, None)
        results["missing"] = request("GET", "/nothing-here", None, None)
        results["unpaired"] = request("GET", "/api/v1/info", None, None)
        results["info"] = request("GET", "/api/v1/info", None, rig.token)
        results["patch"] = request("PATCH", "/api/v1/settings",
                                   {"voice": {"volume": 0.25}}, rig.token)
        results["badpatch"] = request("PATCH", "/api/v1/settings",
                                      {"voice": {"volume": 5}}, rig.token)
        results["post"] = request("POST", "/api/v1/reminders",
                                  {"text": "x", "in_seconds": 120},
                                  rig.token)
        results["query"] = request("GET", "/api/v1/reminders?state=pending",
                                   None, rig.token)
        results["update"] = request("GET", "/api/update", None, rig.token)
        results["junk"] = request("POST", "/api/v1/reminders", "{not json",
                                  rig.token)

        report.check("%s: the page and its script are served" % label,
                     results["page"][0] == 200 and results["script"][0] == 200
                     and b"<script src=\"/app.js\"></script>"
                     in results["page"][2]
                     and b"<script>" not in results["page"][2]
                     and b"fetch(\"/api/update\"" in results["script"][2])
        report.check("%s: an unknown path is a 404" % label,
                     results["missing"][0] == 404)
        report.check("%s: 401 without a token" % label,
                     results["unpaired"][0] == 401)
        report.check("%s: info over HTTP" % label,
                     results["info"][0] == 200
                     and json.loads(results["info"][2])["api_version"] == 1)
        report.check("%s: PATCH settings works, and the bad one is a 400"
                     % label, results["patch"][0] == 200
                     and results["badpatch"][0] == 400,
                     "%s %s" % (results["patch"][0], results["badpatch"][0]))
        report.check("%s: POST creates, and a query string filters"
                     % label, results["post"][0] == 201
                     and results["query"][0] == 200
                     and len(json.loads(results["query"][2])["reminders"])
                     >= 1)
        report.check("%s: the existing update endpoint still works" % label,
                     results["update"][0] == 200
                     and json.loads(results["update"][2])["state"])
        report.check("%s: a body that is not JSON is a 400" % label,
                     results["junk"][0] == 400)
        bad = [name for name, (_, ok, _) in results.items() if not ok]
        report.check("%s: every response carries the security headers "
                     "(page, script, errors, API)" % label, not bad,
                     "missing on: %s" % bad)

    from firmware.webui import serve

    server = serve(rig.api, host="127.0.0.1", port=0)
    port = server.server_address[1]

    def stdlib_request(method, path, body, token):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = body.encode() if isinstance(body, str) \
                else json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, data, headers)
        response = conn.getresponse()
        raw = response.read()
        ok = headers_ok(response)
        conn.close()
        return response.status, ok, raw

    with Pump(device):
        exercise("standard-library server", stdlib_request)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/v1/reminders", b"x" * 70000,
                     {"Content-Length": "70000",
                      "Authorization": "Bearer " + rig.token})
        response = conn.getresponse()
        response.read()
        report.check("standard-library server: an oversized body is a 413, "
                     "before anyone reads it", response.status == 413)
        conn.close()
    server.shutdown()
    server.server_close()

    try:
        from firmware.webui import create_app

        app = create_app(rig.manager, device, rig.auth)
    except ImportError:
        report.check("Flask server (SKIPPED: Flask is not installed here; "
                     "CI installs it)", True)
        rig.rig.close()
        return

    client = app.test_client()

    def flask_request(method, path, body, token):
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        kw = {}
        if body is not None:
            kw["data"] = body if isinstance(body, str) \
                else json.dumps(body)
            kw["content_type"] = "application/json"
        response = client.open(path, method=method, headers=headers, **kw)
        got = {k.lower(): v for k, v in response.headers.items()}
        ok = all(got.get(k) == v for k, v in needed.items())
        return response.status_code, ok, response.get_data()

    with Pump(device):
        exercise("Flask server", flask_request)
        big = client.post("/api/v1/reminders", data=b"x" * 70000,
                          content_type="application/json",
                          headers={"Authorization": "Bearer " + rig.token})
        report.check("Flask server: an oversized body is a 413",
                     big.status_code == 413)
    rig.rig.close()


def main(argv=None):
    from . import assistant_scenarios as asst

    root = tempfile.mkdtemp(prefix="trstenko-headless-")
    report = Report()
    scenarios = [
        ("the provider table", asst.scenario_tables),
        ("what things cost", asst.scenario_prices),
        ("the event queue", scenario_events),
        ("the device on a fake clock", scenario_fake_clock),
        ("frame rates", scenario_frame_rates),
        ("the loop wakes on events", scenario_loop_wakes_on_events),
        ("a step that fails", scenario_step_failures),
        ("commands and the inbox", scenario_commands),
        ("restarting a dead worker", scenario_worker_restarts),
        ("telling systemd, and shutting down", scenario_notify),
        ("logging", scenario_logging),
        ("the v1 API", scenario_api_v1),
        ("the same API over HTTP, from both servers", scenario_web_servers),
        ("timers", scenario_scheduler_timers),
        ("reminders", scenario_scheduler_reminders),
        ("the scheduler's limits and rules", scenario_scheduler_limits),
        ("announcing what is due", scenario_announcements),
        ("when an announcement waits", scenario_announcement_timing),
        ("repeating reminders", scenario_repeats),
        ("boot after being off", scenario_boot_after_off),
        ("housekeeping", scenario_housekeeping_jobs),
        ("telling time in words", scenario_spoken_time),
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
        for rig in list(SchedRig.open_rigs):
            rig.close()
        shutil.rmtree(root, ignore_errors=True)
    return report.done()


if __name__ == "__main__":
    sys.exit(main())

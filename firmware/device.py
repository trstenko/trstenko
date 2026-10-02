"""The device: one loop, one face, one conversation at a time.

    device = Device(hal, pipeline, updater)
    device.run()

Everything that can block -- the model call, a download, speech synthesis --
happens on a thread somewhere below this and is polled here. `tick` draws a
frame and returns, which is what keeps the face at 30Hz while a five-second
round trip is in flight.

This is the conductor. Only this thread changes device state; every other
thread posts an `Event` or sends a `Command` (firmware/events.py) and this one
acts on it during its next tick (plan 01). A tick is a list of steps, and each
runs inside its own `try`: an exception in the scheduler must not stop the
face or the mute switch. A step that fails on every tick makes `healthy()`
false, which is what lets the update trial undo a release that is drawing a
face and doing nothing else.

The face is a consequence, never a command. Nothing calls `set("face_error")`;
things fail, and `_face_for` works out what the device looks like. That is why
muting mid-sentence cannot leave a talking face on screen.
"""

import copy
import logging
import time

from . import events as ev
from . import pipeline as pipeline_mod
from .face import states as state_defs
from .face.controller import FaceController
from .face.renderer import FaceRenderer
from .hal import ButtonDecoder, LONG, POWER, SETUP_COMBO, SHORT, VOL_DOWN, VOL_UP
from .quiet import QuietHours
from .storage.redact import redact
from .storage.settings import SettingsError
from .supervisor import Supervisor

log = logging.getLogger("trstenko.device")

# The three frame rates of plan 01. The idle face only blinks and breathes, so
# drawing it 30 times a second spends heat on nothing; with the screen off
# only events and the scheduler matter, and events end the wait early anyway.
FPS_ACTIVE = 30
FPS_IDLE = 10
FPS_SCREEN_OFF = 2
TARGET_FPS = FPS_ACTIVE
IDLE_FACES = ("face_idle", "face_muted")

VOLUME_STEP = 0.1

# A step that raises on this many ticks in a row is broken rather than
# unlucky. About a second at 30Hz.
FAULT_LIMIT = 30

# The volume buttons change a setting too, but a card should not be written on
# every press. The change is held in memory and written once it has been still
# this long (plan 06, SD-card wear).
SETTINGS_FLUSH_EVERY = 5.0

# How long the error face stays up after a failed turn. Long enough to read as
# an answer to what just happened, short enough that the device does not sulk.
ERROR_HOLD = 4.0


class Device:
    def __init__(self, hal, pipeline=None, updater=None, ssaa=2,
                 on_setup=None, on_log=None, mono=None, wall=None,
                 events=None, screen_sleep_after=None, db=None,
                 settings=None, secrets=None):
        self.hal = hal
        # Two clocks, handed in so a test can drive them. `mono` is what
        # animation, timers and timeouts run on; `wall` is what a reminder
        # "at six" means, and it is not to be trusted before the clock syncs
        # (the Pi has no RTC). The default for `mono` is the HAL's own clock,
        # which is time.monotonic on the board and is what the emulator
        # already replaces, so nothing that exists today changes behaviour.
        self.mono = mono or (lambda: self.hal.now())
        self.wall = wall or time.time

        # Storage, opened by main before the face appears (plan 01, step 2).
        # A device built without them -- the emulator's rigs -- simply has
        # nothing to remember.
        self.db = db
        self.settings = settings
        self.secrets = secrets
        self._settings_dirty = False
        self._settings_listeners = []
        self.quiet = QuietHours()

        # The one inbox. Services are handed this to post to.
        self.events = events or ev.EventQueue()
        self.supervisor = Supervisor(log=lambda line: self.log(line))
        self._handlers = {}
        self._commands = {}
        self.pipeline = pipeline
        self.updater = updater
        self.on_setup = on_setup or (lambda: None)
        self._has_callback = on_log is not None
        self.on_log = on_log or (lambda line: None)
        self._closers = []
        self._closed = False

        self.states = state_defs.build()
        self.renderer = FaceRenderer(ssaa=ssaa)
        self.face = FaceController(self.states, "face_boot",
                                   follow=state_defs.FOLLOW)
        self.buttons = ButtonDecoder()

        self.running = True
        self.turn = None
        self.error_until = 0.0
        self.error_text = ""
        self.setup_mode = False
        self.frames = 0
        self.ticks = 0
        self.draw_ms = 0.0
        self._stale = None

        self._started = self.mono()
        self._was_muted = hal.muted()

        # Screen sleep is off unless the settings turn it on, so a device
        # never goes dark on someone who has not asked for it.
        self.screen_off = False
        self.screen_sleep_after = screen_sleep_after
        self._last_activity = self._started

        self.faults = {}
        self._streak = {}
        self._steps = []
        self._due = {}
        self.add_step("events", self._drain_events)
        self.add_step("input", self._input)
        self.add_step("conversation", self._conversation)
        self.add_step("workers", self.supervisor.tick)
        self.add_step("updater", self._updater)
        self.add_step("settings", self._flush_settings,
                      every=SETTINGS_FLUSH_EVERY)
        self.add_step("screen", self._screen)
        self.add_step("draw", self._draw_frame)

        self.on(ev.SHUTDOWN, lambda event: self.stop())
        self.on(ev.WORKER_DIED,
                lambda event: self.supervisor.died(event.data, self.mono()))
        self.command("set_volume", self._cmd_set_volume)
        self.command("settings.update", self._cmd_settings_update)
        if self.settings is not None:
            self._apply_settings(None)

        if self.updater is not None:
            self.updater.set_busy_hint(lambda: self.turn is not None)
            # If this boot is a freshly installed release, it has to prove
            # itself before the marker clears. See firmware/updates/boot.py.
            if self.updater.begin_trial(self._started):
                self.log("this release is on trial until it comes up")

    # ---- one frame ----

    def add_step(self, name, fn, every=0.0, before="draw"):
        """Put a step in the tick, ahead of the drawing. `every` is a minimum
        number of seconds between runs, for the housekeeping that does not
        need a frame's worth of attention (plan 01, step 6)."""
        entry = (name, fn, every)
        for i, (existing, _, _) in enumerate(self._steps):
            if existing == before:
                self._steps.insert(i, entry)
                break
        else:
            self._steps.append(entry)
        self.faults.setdefault(name, 0)
        self._streak.setdefault(name, 0)

    def tick(self):
        now = self.mono()
        self.ticks += 1
        for name, step, every in self._steps:
            if every and now < self._due.get(name, 0.0):
                continue
            self._due[name] = now + every
            try:
                step(now)
                self._streak[name] = 0
            except Exception:
                self.faults[name] += 1
                self._streak[name] += 1
                log.exception("tick step %s failed", name)

    def run(self):
        while self.running:
            start = time.monotonic()
            self.tick()
            # Wait on the inbox rather than sleeping: a wake word or a web
            # request ends the wait at once, whatever the frame rate.
            self.events.wait(max(0.0, self.frame_interval()
                                 - (time.monotonic() - start)))

    def frame_interval(self):
        """30Hz while anything moves, 10Hz for the settled idle face, 2Hz with
        the screen off."""
        if self.screen_off:
            return 1.0 / FPS_SCREEN_OFF
        now = self.mono()
        moving = (self.turn is not None
                  or now < self.error_until
                  or self.face.progress(now) < 1.0
                  or self.face.current not in IDLE_FACES)
        return 1.0 / (FPS_ACTIVE if moving else FPS_IDLE)

    # ---- events and commands ----

    def on(self, kind, fn):
        """Run `fn(event)` on the main thread whenever `kind` arrives."""
        self._handlers.setdefault(kind, []).append(fn)

    def command(self, name, fn):
        """`fn(args)` runs on the main thread. Return the reply, or raise
        `CommandError` with a sentence for the person who asked."""
        self._commands[name] = fn

    def _drain_events(self, now):
        failed = None
        for item in self.events.drain(ev.DRAIN_LIMIT):
            if isinstance(item, ev.Command):
                self._run_command(item)
                continue
            for fn in self._handlers.get(item.kind, ()):
                try:
                    fn(item)
                except Exception as exc:
                    # The rest of the batch still gets handled.
                    log.exception("handler for %s failed", item.kind)
                    failed = failed or exc
        if failed is not None:
            raise failed

    def _run_command(self, cmd):
        fn = self._commands.get(cmd.name)
        if fn is None:
            cmd.fail("unknown command: %s" % cmd.name)
            return
        try:
            cmd.reply(fn(cmd.args))
        except ev.CommandError as exc:
            cmd.fail(str(exc))
        except Exception:
            # Ours, not the caller's: count it, keep the detail in the log
            # and give the page something it can show.
            self.faults["commands"] = self.faults.get("commands", 0) + 1
            log.exception("command %s failed", cmd.name)
            cmd.fail("the device could not do that")

    def _cmd_set_volume(self, level):
        if isinstance(level, dict):
            level = level.get("level")
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            raise ev.CommandError("volume must be a number from 0 to 1")
        if not 0.0 <= level <= 1.0:
            raise ev.CommandError("volume must be between 0 and 1")
        self._set_volume(float(level))
        return {"volume": self.hal.speaker.get_volume()}

    def _set_volume(self, level):
        level = max(0.0, min(1.0, level))
        self.hal.speaker.set_volume(level)
        if self.settings is not None:
            # What a reboot should come back to. Written later, not now.
            self.settings.data["voice"]["volume"] = round(level, 2)
            self._settings_dirty = True
        self.log("volume %d%%" % round(level * 100))

    # ---- settings ----

    def on_settings(self, fn):
        """Call `fn(changed)` after a settings change, with the dotted names
        of what changed. Only for services that have to react; everything else
        reads `device.settings` when it needs a value."""
        self._settings_listeners.append(fn)

    def _cmd_settings_update(self, patch):
        """Validate, apply in memory, write the file, then tell the services
        that care. In that order: a patch that cannot be written is undone
        rather than left half-true, and nobody is told about a change that did
        not happen."""
        if self.settings is None:
            raise ev.CommandError("settings are not available on this device")
        try:
            clean = self.settings.validate(patch)
        except SettingsError as exc:
            raise ev.CommandError(str(exc))

        before = copy.deepcopy(self.settings.data)
        changed = self.settings.apply(clean)
        if changed:
            try:
                self.settings.save()
            except OSError as exc:
                self.settings.data = before
                log.error("settings could not be written: %s", exc)
                raise ev.CommandError("the settings could not be saved, so "
                                      "nothing was changed")
            self._settings_dirty = False
            self.log("settings changed: %s" % ", ".join(changed))
            self._apply_settings(changed)
        return {"changed": changed, "settings": self.settings.public()}

    def _apply_settings(self, changed):
        """Push settings to the services that hold a copy of them. `changed`
        is None at boot, meaning everything."""
        def touched(prefix):
            return changed is None or any(
                name == prefix or name.startswith(prefix + ".")
                for name in changed)

        if touched("voice.volume"):
            self.hal.speaker.set_volume(self.settings.get("voice", "volume"))
        if touched("quiet"):
            self.quiet = QuietHours.from_settings(self.settings.data["quiet"])
        if changed is not None:
            for fn in self._settings_listeners:
                try:
                    fn(changed)
                except Exception:
                    log.exception("a settings listener failed")

    def _flush_settings(self, now):
        if self._settings_dirty and self.settings is not None:
            self.settings.save()
            self._settings_dirty = False

    # ---- the updater ----

    def _updater(self, now):
        if self.updater is None:
            return
        self.updater.trial_tick(self.healthy(now), now)
        self.updater.tick()

    # ---- the screen ----

    def wake_screen(self, now=None):
        """Anything a person did, or anything that needs their eyes."""
        self._last_activity = self.mono() if now is None else now
        if self.screen_off:
            self.screen_off = False
            self.hal.panel.backlight(1.0)
            # The panel kept whatever it showed; send the whole face again.
            self._stale = (0, 0, self.hal.panel.width, self.hal.panel.height)
            self.log("screen on")

    def sleep_screen(self):
        if not self.screen_off:
            self.screen_off = True
            self.hal.panel.backlight(0.0)
            self.log("screen off")

    def _screen(self, now):
        busy = self.turn is not None or now < self.error_until
        if busy:
            self.wake_screen(now)
        elif (self.screen_sleep_after is not None and not self.screen_off
              and now - self._last_activity >= self.screen_sleep_after):
            self.sleep_screen()

    # ---- input ----

    def _input(self, now):
        for event in self.buttons.feed(self.hal.poll(), now):
            self.wake_screen(now)
            self._button(event, now)

        muted = self.hal.muted()
        if muted != self._was_muted:
            self._was_muted = muted
            self.log("microphone %s" % ("off" if muted else "on"))
            if muted and self.turn is not None:
                # The switch cuts the mic's power. Whatever was being said is
                # gone, and finishing the turn would mean answering silence.
                self.turn.cancel()
                self.turn = None

    def _button(self, event, now):
        if event.name == SETUP_COMBO:
            self.setup_mode = True
            self.log("setup requested")
            self.on_setup()
            return

        if event.name == POWER:
            if event.action == SHORT:
                self.log("shutdown")
                self.stop()
                self.hal.system.shutdown()
            elif event.action == LONG:
                self.log("restart")
                self.stop()
                self.hal.system.reboot()
            return

        if event.action != SHORT:
            return
        if event.name in (VOL_DOWN, VOL_UP):
            step = VOLUME_STEP if event.name == VOL_UP else -VOLUME_STEP
            self._set_volume(self.hal.speaker.get_volume() + step)

    # ---- the conversation ----

    def _conversation(self, now):
        if self.pipeline is None:
            return

        if self.turn is None:
            if self.hal.muted():
                # Drain, do not skip. A detection that was already in flight
                # when the switch went over must die with it -- a device that
                # answers a question it heard while muted breaks the only
                # promise the switch makes.
                self.pipeline.poll(now)
                return
            if now < self.error_until:
                return
            if self.pipeline.poll(now):
                self.log("wake word")
                self.turn = self.pipeline.start_turn(now)
            return

        self.turn.poll(now)
        if not self.turn.finished:
            return

        if self.turn.stage == pipeline_mod.ERROR:
            self.fail(self.turn.error or "something went wrong", now)
        elif self.turn.transcript:
            self.log("%s -> %s" % (self.turn.transcript, self.turn.reply))
        self.turn = None

    def fail(self, text, now=None):
        """Show the error face and say why. The only way into face_error."""
        now = self.mono() if now is None else now
        self.error_text = text
        self.error_until = now + ERROR_HOLD
        self.log("error: %s" % text)

    # ---- output ----

    def _face_for(self, now):
        if self.hal.muted():
            return "face_muted"
        if now < self.error_until:
            return "face_error"
        if self.turn is not None:
            return {
                pipeline_mod.LISTENING: "face_listening",
                pipeline_mod.THINKING: "face_thinking",
                pipeline_mod.SPEAKING: "face_talking",
            }.get(self.turn.stage, "face_idle")
        if now - self._started < self.states["face_boot"].duration:
            return "face_boot"
        return "face_idle"

    def _draw_frame(self, now):
        if self.screen_off:
            return
        self._draw(now)
        self.frames += 1

    def _draw(self, now):
        want = self._face_for(now)
        if want != self.face.current:
            self.face.set(want, now,
                          state_defs.blend_time(self.face.current, want))

        level = self.turn.level() if self.turn is not None else 0.0
        start = time.perf_counter()
        shapes = self.face.sample(now, {"amp": level})
        image, bbox = self.renderer.render(shapes)
        self.draw_ms = (time.perf_counter() - start) * 1000.0

        # Push what is drawn now, plus what was drawn last frame. The panel
        # holds its pixels: a shape that moved or faded leaves its old ones lit
        # unless that area is sent again. Without this, boot's eyes smear a
        # trail on the way up and the mic glyph stays on screen after the mute
        # switch goes back.
        drawn = _clip(bbox, self.face.blit())
        self.hal.panel.push(image, _union(drawn, self._stale))
        self._stale = drawn

    # ---- odds and ends ----

    def healthy(self, now=None):
        """Is this release actually working?

        Deliberately about output rather than about reaching the end of
        `__init__`. A device that imported cleanly and then draws nothing is
        broken in the way that matters, and this is what decides whether an
        update is kept or undone.
        """
        now = self.mono() if now is None else now
        return (self.frames > 30
                and now - self._started >= self.states["face_boot"].duration
                and self.face.current != "face_boot"
                and not self.failing())

    def failing(self):
        """Steps that have raised on every one of their last several runs."""
        return sorted(name for name, n in self._streak.items()
                      if n >= FAULT_LIMIT)

    def log(self, line):
        # Both places: the callback is what the emulator window shows, and the
        # logger is what the journal gets. `via_callback` stops a handler that
        # feeds the same callback from showing the line twice.
        line = redact(line)
        if self._has_callback:
            self.on_log(line)
        log.info("%s", line, extra={"via_callback": self._has_callback})

    def stop(self):
        self.running = False

    def add_closer(self, name, fn):
        """Something to do on the way out, such as flushing and closing the
        database. They run in the order they were added."""
        self._closers.append((name, fn))

    def close(self):
        """The shutdown sequence (plan 01): cancel the turn, stop the audio and
        speech workers, flush and close storage, backlight off, release the
        hardware. Each step is tried even if the one before it failed; a
        device that cannot flush its database should still switch its screen
        off."""
        if self._closed:
            return
        self._closed = True
        self.running = False

        def cancel_turn():
            if self.turn is not None:
                self.turn.cancel()
                self.turn = None

        steps = [("turn", cancel_turn)]
        if self.pipeline is not None:
            steps.append(("pipeline", self.pipeline.close))
        steps.append(("settings", lambda: self._flush_settings(None)))
        steps += self._closers
        steps += [("backlight", lambda: self.hal.panel.backlight(0.0)),
                  ("hal", self.hal.close)]
        for name, fn in steps:
            try:
                fn()
            except Exception:
                log.exception("shutdown step %s failed", name)

    def summary(self):
        """What the web UI shows under "Device"."""
        return {
            "face": self.face.current,
            "muted": self.hal.muted(),
            "mute_is_hardware": self.hal.mute_is_hardware,
            "online": self.hal.system.online(),
            "free_bytes": self.hal.system.free_bytes("/"),
            "volume": self.hal.speaker.get_volume(),
            "uptime": self.hal.system.uptime(),
            "busy": self.turn is not None,
            "setup_mode": self.setup_mode,
            "draw_ms": round(self.draw_ms, 2),
            "screen_off": self.screen_off,
            "failing": self.failing(),
            "workers": self.supervisor.status(),
        }


def _union(a, b):
    """Both may be None. One frame of a bigger push at a transition is the
    price of not leaving pixels behind; it shrinks again immediately."""
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]),
            max(a[2], b[2]), max(a[3], b[3]))


def _clip(bbox, region):
    """Never push outside the state's blit region. The device does not send
    those pixels, so drawing them is a frame spent on nothing."""
    if bbox is None:
        return None
    x0 = max(bbox[0], region[0])
    y0 = max(bbox[1], region[1])
    x1 = min(bbox[2], region[2])
    y1 = min(bbox[3], region[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)

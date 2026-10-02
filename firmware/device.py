"""The device: one loop, one face, one conversation at a time.

    device = Device(hal, pipeline, updater)
    while device.running:
        device.tick()

Everything that can block -- the model call, a download, speech synthesis --
happens on a thread somewhere below this and is polled here. `tick` draws a
frame and returns, which is what keeps the face at 30Hz while a five-second
round trip is in flight.

The face is a consequence, never a command. Nothing calls `set("face_error")`;
things fail, and `_face_for` works out what the device looks like. That is why
muting mid-sentence cannot leave a talking face on screen.
"""

import time

from . import pipeline as pipeline_mod
from .face import states as state_defs
from .face.controller import FaceController
from .face.renderer import FaceRenderer
from .hal import ButtonDecoder, LONG, POWER, SETUP_COMBO, SHORT, VOL_DOWN, VOL_UP

TARGET_FPS = 30
VOLUME_STEP = 0.1

# How long the error face stays up after a failed turn. Long enough to read as
# an answer to what just happened, short enough that the device does not sulk.
ERROR_HOLD = 4.0


class Device:
    def __init__(self, hal, pipeline=None, updater=None, ssaa=2,
                 on_setup=None, on_log=None, mono=None, wall=None):
        self.hal = hal
        # Two clocks, handed in so a test can drive them. `mono` is what
        # animation, timers and timeouts run on; `wall` is what a reminder
        # "at six" means, and it is not to be trusted before the clock syncs
        # (the Pi has no RTC). The default for `mono` is the HAL's own clock,
        # which is time.monotonic on the board and is what the emulator
        # already replaces, so nothing that exists today changes behaviour.
        self.mono = mono or (lambda: self.hal.now())
        self.wall = wall or time.time
        self.pipeline = pipeline
        self.updater = updater
        self.on_setup = on_setup or (lambda: None)
        self.on_log = on_log or (lambda line: None)

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
        self.draw_ms = 0.0
        self._stale = None

        self._started = self.mono()
        self._was_muted = hal.muted()

        if self.updater is not None:
            self.updater.set_busy_hint(lambda: self.turn is not None)
            # If this boot is a freshly installed release, it has to prove
            # itself before the marker clears. See firmware/updates/boot.py.
            if self.updater.begin_trial(self._started):
                self.log("this release is on trial until it comes up")

    # ---- one frame ----

    def tick(self):
        now = self.mono()

        self._events(now)
        self._conversation(now)
        if self.updater is not None:
            self.updater.trial_tick(self.healthy(now), now)
            self.updater.tick()

        self._draw(now)
        self.frames += 1

    def run(self, fps=TARGET_FPS):
        period = 1.0 / fps
        while self.running:
            start = time.monotonic()
            self.tick()
            slack = period - (time.monotonic() - start)
            if slack > 0:
                time.sleep(slack)

    # ---- input ----

    def _events(self, now):
        for event in self.buttons.feed(self.hal.poll(), now):
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
            level = max(0.0, min(1.0, self.hal.speaker.get_volume() + step))
            self.hal.speaker.set_volume(level)
            self.log("volume %d%%" % round(level * 100))

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
                and self.face.current != "face_boot")

    def log(self, line):
        self.on_log(line)

    def stop(self):
        self.running = False

    def close(self):
        self.running = False
        if self.pipeline is not None:
            self.pipeline.close()
        self.hal.close()

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

"""A Trstenko with no parts in it.

Implements `firmware.hal.base` against memory and a Pillow image. The firmware
above it cannot tell the difference, which is the point: the device loop, the
face, the button gestures and the whole update flow can be finished and
debugged before the board arrives.

Where it is deliberately unkind:

- **The panel keeps a framebuffer.** `push` pastes only the bbox it was given,
  exactly like the real one. Anything the firmware draws outside the blit
  region it declared is simply not shown, and the stale pixels stay on screen.
  A clipping bug looks here like it will look on the desk.
- **The mute switch removes the microphone.** It does not set a flag; the mic
  reports itself unavailable and reads return nothing, the way a USB device
  that just lost power does.
- **Free space, network, metered and under-voltage are all yours to set.**
  Every branch in the updater's preflight is reachable from the window.
"""

import time

from PIL import Image

from firmware.hal import base
from firmware.hal.base import BUTTONS, Event

MB = 1024 * 1024
BACKGROUND = (18, 25, 29)


class VirtualPanel(base.Panel):
    """320 x 240 of pixels that persist between frames, like glass does."""

    def __init__(self, spi_hz=32_000_000):
        self.spi_hz = spi_hz
        self.frame = Image.new("RGB", (self.width, self.height), BACKGROUND)
        self.backlight_level = 1.0
        self.last_bbox = None
        self.pushes = 0
        self.bytes_pushed = 0
        self.last_spi_ms = 0.0

    def push(self, image, bbox):
        self.pushes += 1
        if bbox is None:
            self.last_bbox = None
            self.last_spi_ms = 0.0
            return
        self.frame.paste(image.crop(bbox), (bbox[0], bbox[1]))
        self.last_bbox = bbox
        pixels = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        self.bytes_pushed += pixels * 2
        self.last_spi_ms = (pixels * 2 * 8) / self.spi_hz * 1000.0

    def backlight(self, level):
        self.backlight_level = max(0.0, min(1.0, level))

    def close(self):
        self.backlight(0.0)


class VirtualMic(base.Mic):
    """The mic the firmware reads. Where the samples come from -- a slider or
    the computer's own microphone -- is the audio bridge's business, not the
    firmware's, which is the arrangement the robot has with ALSA."""

    def __init__(self):
        self.powered = True
        self._level = 0.0
        self._bridge = None

    def attach(self, bridge):
        self._bridge = bridge

    def set_level(self, value):
        self._level = max(0.0, min(1.0, value))

    def available(self):
        return self.powered

    def read(self, frames):
        if not self.powered:
            return b""
        if self._bridge is not None:
            return self._bridge.take(frames)
        return b"\x00\x00" * int(frames)

    def level(self):
        if not self.powered:
            return 0.0
        if self._bridge is not None:
            return self._bridge.mic_level()
        return self._level


class VirtualSpeaker(base.Speaker):
    """Models how long playback takes, and hands the audio to the bridge in
    case something out there is going to make a noise with it."""

    def __init__(self):
        self._until = 0.0
        self._level = 0.0
        self._volume = 0.6
        self.last_played = 0
        self._bridge = None

    def attach(self, bridge):
        self._bridge = bridge

    def play(self, pcm, rate=22050):
        seconds = len(pcm) / float(rate * 2) if rate else 0.0
        self._until = time.monotonic() + seconds
        self.last_played = len(pcm)
        if self._bridge is not None:
            self._bridge.play(pcm, rate)

    def stop(self):
        self._until = 0.0
        self._level = 0.0
        if self._bridge is not None:
            self._bridge.stop()

    def is_playing(self):
        return time.monotonic() < self._until

    def set_level(self, value):
        """The pipeline's synthetic envelope, used when nothing real is
        playing."""
        self._level = max(0.0, min(1.0, value))

    def synthetic_level(self):
        return self._level if self.is_playing() else 0.0

    def level(self):
        if self._bridge is not None:
            return self._bridge.speaker_level()
        return self.synthetic_level()

    def get_volume(self):
        return self._volume

    def set_volume(self, value):
        self._volume = max(0.0, min(1.0, value))


class VirtualSystem(base.System):
    """A machine whose disk, network and power supply you control."""

    def __init__(self, free_mb=2048, total_mb=14800):
        self.free_mb = free_mb
        self.total_mb = total_mb
        self.is_online = True
        self.is_metered = False
        self.is_undervolt = False
        self.temp_c = 46.0
        self.rebooted = 0
        self.shutdowns = 0
        self._booted = time.monotonic()

    def free_bytes(self, path):
        return int(self.free_mb * MB)

    def total_bytes(self, path):
        return int(self.total_mb * MB)

    def online(self):
        return self.is_online

    def metered(self):
        return self.is_metered

    def undervoltage(self):
        return self.is_undervolt

    def temperature_c(self):
        return self.temp_c

    def uptime(self):
        return time.monotonic() - self._booted

    def reboot(self):
        self.rebooted += 1

    def shutdown(self):
        self.shutdowns += 1


class EmulatedHAL(base.HAL):
    name = "emulated"

    def __init__(self):
        super().__init__(VirtualPanel(), VirtualMic(), VirtualSpeaker(),
                         VirtualSystem(), mute_is_hardware=True)
        self._queue = []
        self.held = {name: False for name in BUTTONS}

    # ---- what the window calls ----

    def press(self, name):
        if self.held.get(name):
            return
        self.held[name] = True
        self._queue.append(Event(name, "press", self.now()))

    def release(self, name):
        if not self.held.get(name):
            return
        self.held[name] = False
        self._queue.append(Event(name, "release", self.now()))

    def set_mute(self, muted):
        """The slide switch. Cutting the supply takes the USB device with it."""
        self.mic.powered = not muted

    # ---- what the firmware calls ----

    def poll(self):
        out, self._queue = self._queue, []
        return out

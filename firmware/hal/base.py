"""What the firmware is allowed to assume about the hardware.

Every backend implements these. `pi.py` drives the real board; the emulator in
`tools/emulator/` implements the same surface against a window on a PC. Nothing
above this layer imports a driver, checks a GPIO number, or knows whether the
panel is real -- which is the only reason the device can be developed before the
parts arrive.

The interfaces are deliberately thin and synchronous. The Pi 3 A+ has 512MB and
one job; an async framework here would buy nothing and cost the frame budget.
"""

import time

# Logical controls, from the top face of the shell.
POWER = "power"
VOL_DOWN = "vol_down"
VOL_UP = "vol_up"

BUTTONS = (POWER, VOL_DOWN, VOL_UP)

# Logical button events, after the decoder in buttons.py has seen the timing.
SHORT = "short"
LONG = "long"
SETUP_COMBO = "setup_combo"


class Event:
    """One thing the hardware did. `name` is a control, `action` what it did."""

    __slots__ = ("name", "action", "at")

    def __init__(self, name, action, at=None):
        self.name = name
        self.action = action
        self.at = time.monotonic() if at is None else at

    def __repr__(self):
        return "Event(%s, %s)" % (self.name, self.action)

    def __eq__(self, other):
        return (isinstance(other, Event) and self.name == other.name
                and self.action == other.action)


class Panel:
    """The 2.4" ILI9341, mounted landscape, so 320 x 240.

    `push` takes an RGB image the size of the whole canvas plus the bounding box
    that actually changed, and sends only that box. The box is the frame rate:
    the full screen is 154KB over SPI, the usual dirty rect is 72KB.
    """

    width = 320
    height = 240

    def push(self, image, bbox):
        raise NotImplementedError

    def backlight(self, level):
        """0.0 to 1.0. Off is a real state: the device sleeps dark."""
        raise NotImplementedError

    def close(self):
        pass


class Mic:
    """The ReSpeaker Lite, over USB audio, 16kHz 16-bit mono.

    It can vanish. The mute switch cuts the board's power, which the Pi sees as
    a USB disconnect, so `available()` is not a formality -- read paths have to
    survive the device going away mid-stream and coming back.
    """

    rate = 16000

    def available(self):
        raise NotImplementedError

    def read(self, frames):
        """Return up to `frames` of 16-bit mono PCM, or b'' if unavailable."""
        raise NotImplementedError

    def level(self):
        """0.0 to 1.0, smoothed. What the listening face animates from."""
        raise NotImplementedError


class Speaker:
    def play(self, pcm, rate=22050):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def is_playing(self):
        raise NotImplementedError

    def level(self):
        """0.0 to 1.0 of what is being played right now. Drives the mouth."""
        raise NotImplementedError

    def get_volume(self):
        raise NotImplementedError

    def set_volume(self, value):
        raise NotImplementedError


class System:
    """The parts of the machine the updater and the shutdown path need."""

    def free_bytes(self, path):
        raise NotImplementedError

    def total_bytes(self, path):
        raise NotImplementedError

    def online(self):
        raise NotImplementedError

    def metered(self):
        """True on a connection the user pays by the megabyte. Never download
        a firmware image over one without asking."""
        return False

    def undervoltage(self):
        """The Pi's own low-voltage flag. Rewriting the root filesystem on a
        board that is already browning out is how a device is bricked."""
        return False

    def temperature_c(self):
        return None

    def uptime(self):
        raise NotImplementedError

    def reboot(self):
        raise NotImplementedError

    def shutdown(self):
        raise NotImplementedError


class HAL:
    """Everything a backend provides, in one object passed to the device."""

    name = "base"

    def __init__(self, panel, mic, speaker, system, mute_is_hardware=True):
        self.panel = panel
        self.mic = mic
        self.speaker = speaker
        self.system = system
        # The mute switch breaks the microphone's supply rather than being read
        # by software. Where that is true, muting cannot be faked or bypassed,
        # which is the whole claim; a backend that cannot do it says so here.
        self.mute_is_hardware = mute_is_hardware

    def now(self):
        return time.monotonic()

    def poll(self):
        """Return the Events since the last call. Never blocks."""
        raise NotImplementedError

    def muted(self):
        """True when the switch has cut the microphone's power."""
        return not self.mic.available()

    def close(self):
        self.panel.close()

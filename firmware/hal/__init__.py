"""Pick a hardware backend.

    from firmware.hal import open_hal
    hal = open_hal()

On the Pi that is the real board. Anywhere else there is no automatic
fallback -- the emulator constructs its own HAL and hands it to the device, so
firmware that reaches for hardware on a PC has a bug rather than a silent
stand-in.
"""

from .base import (BUTTONS, LONG, POWER, SETUP_COMBO, SHORT, VOL_DOWN, VOL_UP,
                   Event, HAL, Mic, Panel, Speaker, System)
from .buttons import ButtonDecoder

__all__ = ["open_hal", "HAL", "Panel", "Mic", "Speaker", "System", "Event",
           "ButtonDecoder", "BUTTONS", "POWER", "VOL_DOWN", "VOL_UP",
           "SHORT", "LONG", "SETUP_COMBO"]


def open_hal():
    from . import pi

    if not pi.available():
        raise RuntimeError(
            "no hardware backend: this is not a Raspberry Pi. "
            "To run the firmware on a PC, use the emulator: "
            "python -m tools.emulator")
    return pi.PiHAL()

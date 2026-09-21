"""Turn raw button edges into the gestures the top face is documented to have.

Both backends report presses and releases and nothing else. The timing lives
here so the Pi and the emulator agree on what a long press is, and so the rules
can be changed in one place.

    power       short -> clean shutdown, long -> restart
    vol_down    short -> volume step down
    vol_up      short -> volume step up
    vol_down + vol_up held together 3s -> raise the setup hotspot, show the QR

The combo is why a short press cannot fire on the way down. Stepping the volume
the instant a finger lands would make every attempt at the setup gesture also
change the volume twice.
"""

from .base import LONG, SETUP_COMBO, SHORT, VOL_DOWN, VOL_UP, Event

LONG_PRESS = 0.8
COMBO_HOLD = 3.0
COMBO = frozenset((VOL_DOWN, VOL_UP))


class ButtonDecoder:
    def __init__(self, long_press=LONG_PRESS, combo_hold=COMBO_HOLD):
        self.long_press = long_press
        self.combo_hold = combo_hold
        self.down = {}
        self.combo_since = None
        self.combo_fired = False
        self._consumed = set()

    def feed(self, raw, now):
        """`raw` is Events with action 'press' or 'release'. Returns the
        logical events, which may be none -- a long press is only known at
        release, and the combo only after three seconds of nothing happening."""
        out = []

        for event in raw:
            if event.action == "press":
                self.down[event.name] = event.at
            elif event.action == "release":
                started = self.down.pop(event.name, None)
                if started is None:
                    continue
                if event.name in self._consumed:
                    self._consumed.discard(event.name)
                    continue
                held = event.at - started
                out.append(Event(event.name,
                                 LONG if held >= self.long_press else SHORT,
                                 event.at))

        out.extend(self._combo(now))
        return out

    def _combo(self, now):
        held = COMBO <= set(self.down)
        if not held:
            self.combo_since = None
            self.combo_fired = False
            return []

        if self.combo_since is None:
            self.combo_since = max(self.down[n] for n in COMBO)

        if self.combo_fired or now - self.combo_since < self.combo_hold:
            return []

        self.combo_fired = True
        # Both buttons are still down. Swallow their releases, or letting go of
        # the setup gesture also steps the volume twice.
        self._consumed |= set(COMBO)
        return [Event(SETUP_COMBO, SETUP_COMBO, now)]

"""Time you control.

A reminder "at six" and a timer "in ten minutes" cannot be tested against a
real clock: the first would need the test to wait until six and the second
would take ten minutes. `FakeClock` is the pair of clocks the firmware takes as
arguments, moved by hand.

The two clocks are separate on purpose, because on the Pi they come apart:

- **monotonic** only ever goes forward, at the rate of real time, and starts
  from nothing at every boot. Timers live here.
- **wall** is the date. With no RTC it is whatever the last shutdown saved
  until NTP syncs, and then it can jump by days in one step. Reminders live
  here, and have to wait for `synced`.

    clock = FakeClock(wall=datetime(2026, 10, 2, 17, 59).timestamp())
    clock.set_synced(False)         # the Pi has just booted
    clock.advance(30)               # both clocks move
    clock.set_wall(...)             # NTP steps the date; monotonic does not
    clock.set_synced(True)
"""


class FakeClock:
    def __init__(self, mono=1000.0, wall=1_790_000_000.0, synced=True):
        self._mono = float(mono)
        self._wall = float(wall)
        self.synced = bool(synced)

    def mono(self):
        return self._mono

    def wall(self):
        return self._wall

    def advance(self, seconds):
        """Real time passing: both clocks move together."""
        if seconds < 0:
            raise ValueError("time does not run backwards; use set_wall to "
                             "model the date being corrected")
        self._mono += seconds
        self._wall += seconds

    def set_wall(self, epoch):
        """The date being stepped, as NTP does on its first sync. Monotonic is
        untouched, which is exactly why timers survive it and reminders do
        not."""
        self._wall = float(epoch)

    def set_synced(self, synced):
        self.synced = bool(synced)

    def is_synced(self):
        """The same question `/run/systemd/timesync/synchronized` answers."""
        return self.synced

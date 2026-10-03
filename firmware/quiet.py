"""Quiet hours: when reminders speak softly.

The default is 22:00 to 07:00. A reminder due inside the window gets a soft
chime and speech at the quiet volume; a timer rings normally, because
somebody set it on purpose (plan 02). This module only answers "is it quiet
now"; what to do about it is the announcer's business.

The window is on the owner's wall clock, so it wraps midnight: 22:00 to 07:00
is the night, not an empty range.
"""


def _minutes(text):
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


class QuietHours:
    def __init__(self, enabled=True, start="22:00", end="07:00", volume=0.4):
        self.enabled = bool(enabled)
        self.start = _minutes(start)
        self.end = _minutes(end)
        self.volume = float(volume)

    @classmethod
    def from_settings(cls, quiet):
        """`quiet` is the settings section, already validated."""
        return cls(quiet["enabled"], quiet["from"], quiet["to"],
                   quiet["volume"])

    def active(self, local):
        """`local` is a `datetime` in the owner's timezone."""
        if not self.enabled or self.start == self.end:
            return False
        now = local.hour * 60 + local.minute
        if self.start < self.end:
            return self.start <= now < self.end
        return now >= self.start or now < self.end

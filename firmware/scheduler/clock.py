"""Time for reminders: the owner's zone, and whether the clock can be believed.

The Pi 3 A+ has no real-time clock. Until NTP has synced, `time.time()` is
whatever the fake-hwclock service saved at the last shutdown, which can be days
out. Timers do not care, because they run on the monotonic clock. A reminder
"at six" does, so it waits for `is_synced()`.

Reminders are stored as local time plus a zone name, never as UTC alone. "Every
day at 7" has to stay at 7 across a daylight-saving change, and an epoch
timestamp cannot keep that promise: the 24 hours between two 7 o'clocks are 25
long on the night the clocks go back.
"""

import datetime
import os
from zoneinfo import ZoneInfo

# systemd-timesyncd creates this when it has set the clock. With Chrony the
# same one-second check works against `chronyc tracking`; the interface below
# is all the scheduler sees.
SYNC_FILE = "/run/systemd/timesync/synchronized"

LOCAL_FORMAT = "%Y-%m-%dT%H:%M"


class FileSyncCheck:
    def __init__(self, path=SYNC_FILE):
        self.path = path

    def is_synced(self):
        return os.path.exists(self.path)


def zone(name):
    """A `ZoneInfo`, or ValueError saying the name is not one this device
    knows."""
    try:
        return ZoneInfo(name)
    except Exception:
        raise ValueError("unknown timezone %r" % (name,))


def parse_local(text):
    """'2026-10-02T18:00' (or with seconds, or with a space) as a naive
    datetime. A time with its own offset is refused: the zone is a separate
    field and two sources of truth is how reminders end up an hour out."""
    if not isinstance(text, str):
        raise ValueError("a time is written like 2026-10-02T18:00")
    try:
        value = datetime.datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError("a time is written like 2026-10-02T18:00")
    if value.tzinfo is not None:
        raise ValueError("give the time without an offset; the timezone is "
                         "its own setting")
    return value.replace(second=0, microsecond=0)


def to_epoch(naive, tzname):
    """When that local time is, in the zone. A time that does not exist (the
    hour skipped when the clocks go forward) lands an hour later, which is
    what a person who asked for 02:30 that night would want; one that happens
    twice means the first."""
    return naive.replace(tzinfo=zone(tzname)).timestamp()


def to_local(epoch, tzname):
    return datetime.datetime.fromtimestamp(epoch, zone(tzname))


def local_iso(naive):
    return naive.strftime(LOCAL_FORMAT)

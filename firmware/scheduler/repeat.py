"""When a repeating reminder fires next.

Daily, weekdays, and named days of the week. That is all, on purpose: full
calendar rules (RRULE) are the kind of feature that takes a week and then
breaks across a time change. "Every second Tuesday" can wait for v2.

The next firing is found by walking local dates and attaching the wall-clock
time to each in the owner's zone, not by adding 24 hours. That is the
difference between "every day at 7" staying at 7 across the last Sunday of
October and drifting to 6 or 8.
"""

import datetime

from . import clock

DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

MESSAGE = ("A repeat can be daily, weekdays, or weekly on named days, such as "
           "weekly:MO,WE.")


def parse(spec):
    """The weekdays (Monday is 0) a repeat covers, or None for no repeat."""
    if spec is None or spec == "":
        return None
    if not isinstance(spec, str):
        raise ValueError(MESSAGE)
    spec = spec.strip().lower()
    if spec == "daily":
        return frozenset(range(7))
    if spec == "weekdays":
        return frozenset(range(5))
    if spec.startswith("weekly:"):
        names = [n.strip().upper() for n in spec[7:].split(",") if n.strip()]
        if names and all(n in DAYS for n in names):
            return frozenset(DAYS.index(n) for n in names)
    raise ValueError(MESSAGE)


def normalise(spec):
    """The spelling that is stored, so 'Weekly:we,mo' and 'weekly:MO,WE' are
    the same repeat."""
    days = parse(spec)
    if days is None:
        return None
    if days == frozenset(range(7)):
        return "daily"
    if days == frozenset(range(5)):
        return "weekdays"
    return "weekly:" + ",".join(DAYS[d] for d in sorted(days))


def next_after(local_time, tzname, spec, after):
    """The first firing strictly after the epoch `after`.

    `local_time` supplies the time of day (its date does not matter, which is
    what lets a device that was off for a weekend skip what it missed instead
    of replaying it). Returns (naive local datetime, epoch).
    """
    days = parse(spec)
    if days is None:
        raise ValueError("this reminder does not repeat")
    at = local_time.time().replace(second=0, microsecond=0)
    day = clock.to_local(after, tzname).date() - datetime.timedelta(days=1)
    for _ in range(16):
        if day.weekday() in days:
            candidate = datetime.datetime.combine(day, at)
            epoch = clock.to_epoch(candidate, tzname)
            if epoch > after:
                return candidate, epoch
        day += datetime.timedelta(days=1)
    raise ValueError(MESSAGE)

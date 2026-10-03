"""The device's own chores, run by the same loop as the reminders.

These are not rows in the jobs table: nothing about them should survive a
reboot, and the owner never sees them. They are a short list of named
intervals checked from `Scheduler.tick`, so there is still no thread and no
lock (plan 02).

What exists in M1 is the clock check, the network check, the temperature
check and the weekly database maintenance. Usage flushing, session expiry and
the budget rollover belong to the assistant and arrive with it.
"""

import datetime
import logging

from .. import events as ev
from . import clock

log = logging.getLogger("trstenko.housekeeping")

# Seconds between runs.
CLOCK_CHECK_UNSYNCED = 1.0
CLOCK_CHECK_SYNCED = 60.0
NET_CHECK = 30.0
THERMAL_CHECK = 10.0

# The wall clock moving by more than this between two checks, against the
# monotonic one, is NTP correcting it or somebody setting it by hand.
CLOCK_JUMP = 5.0

# Plan 02: above 75 the idle frame rate drops to 5Hz; above 80 the face runs
# at 10Hz even during turns. Thresholds, not measurements: whether they are
# right depends on the real case and board. TODO measure on the board with the
# enclosure closed (docs/README.md, open decision 5).
WARM_C = 75.0
HOT_C = 80.0

# Sunday, 04:00 local: the owner is asleep and the device is idle.
MAINTENANCE_WEEKDAY = 6
MAINTENANCE_HOUR = 4

# Plan 05: usage rows older than 13 months are pruned (usage_daily keeps the
# totals).
USAGE_KEPT_SECONDS = 396 * 86400
# Reminders and timers that are over stay for a month, so "did it remind me
# last week?" can still be answered, and then go. One that was missed stays
# three months: it is on the page until its owner dismisses it. Without this
# the table only ever grows (the soak test counted 84 rows after one day).
JOBS_KEPT_SECONDS = 30 * 86400
MISSED_KEPT_SECONDS = 90 * 86400

NORMAL, WARM, HOT = "normal", "warm", "hot"


class _Job:
    def __init__(self, name, fn, every=None, clock_kind="mono",
                 next_fn=None, needs_sync=False):
        self.name = name
        self.fn = fn
        self.every = every
        self.clock_kind = clock_kind
        self.next_fn = next_fn
        self.needs_sync = needs_sync
        self.due = None


class Housekeeping:
    def __init__(self, synced=lambda: True):
        self._jobs = []
        self._synced = synced

    def every(self, name, seconds, fn):
        """Run `fn()` each `seconds`; `seconds` may be a function when the
        interval changes (the clock check slows down once the clock is
        right)."""
        self._jobs.append(_Job(name, fn, every=seconds))

    def at(self, name, next_fn, fn):
        """Run `fn()` at the wall-clock moments `next_fn(after_epoch)` names.
        Waits for the clock to be right."""
        self._jobs.append(_Job(name, fn, clock_kind="wall", next_fn=next_fn,
                               needs_sync=True))

    def tick(self, now_mono, now_wall):
        for job in self._jobs:
            if job.needs_sync and not self._synced():
                continue
            now = now_wall if job.clock_kind == "wall" else now_mono
            if job.due is None:
                job.due = (job.next_fn(now) if job.next_fn
                           else now)
            if now < job.due:
                continue
            try:
                job.fn()
            except Exception:
                # A chore that fails must not stop the others, or the loop.
                log.exception("housekeeping %s failed", job.name)
            if job.next_fn:
                job.due = job.next_fn(now)
            else:
                every = job.every() if callable(job.every) else job.every
                job.due = now + every


def standard(scheduler, system, db, post, owner_tz):
    """Register the M1 chores. `system` is the HAL's, `post` puts an event on
    the main inbox, `owner_tz()` is the owner's timezone name."""
    hk = scheduler.housekeeping

    hk.every("clock_check",
             lambda: CLOCK_CHECK_SYNCED if scheduler.synced
             else CLOCK_CHECK_UNSYNCED,
             scheduler.check_clock)

    state = {"online": None, "level": NORMAL}

    def net_check():
        online = bool(system.online())
        if state["online"] is not None and online != state["online"]:
            post(ev.Event(ev.NETWORK_CHANGED, data=online))
        state["online"] = online

    hk.every("net_check", NET_CHECK, net_check)

    def thermal_check():
        temp = system.temperature_c()
        if temp is None:
            return
        level = HOT if temp > HOT_C else WARM if temp > WARM_C else NORMAL
        if level != state["level"]:
            state["level"] = level
            log.info("CPU temperature %.0f C: %s", temp, level)
            post(ev.Event(ev.HOT, data={"temperature": temp,
                                        "level": level}))

    hk.every("thermal_check", THERMAL_CHECK, thermal_check)

    def next_maintenance(after):
        tz = owner_tz()
        local = clock.to_local(after, tz)
        day = local.date()
        for _ in range(8):
            candidate = datetime.datetime.combine(
                day, datetime.time(MAINTENANCE_HOUR))
            if day.weekday() == MAINTENANCE_WEEKDAY:
                epoch = clock.to_epoch(candidate, tz)
                if epoch > after:
                    return epoch
            day += datetime.timedelta(days=1)
        return after + 7 * 86400

    def maintenance():
        conn = db.conn()
        conn.execute("PRAGMA optimize")
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        cutoff = scheduler.wall() - USAGE_KEPT_SECONDS
        pruned = conn.execute("DELETE FROM usage WHERE at < ?",
                              (cutoff,)).rowcount
        now = scheduler.wall()
        when = "COALESCE(acked_at, fired_at, due_wall, created_at)"
        jobs = conn.execute(
            "DELETE FROM jobs WHERE (state IN ('done', 'cancelled', "
            "'interrupted') AND %s < ?) OR (state = 'missed' AND %s < ?)"
            % (when, when),
            (now - JOBS_KEPT_SECONDS, now - MISSED_KEPT_SECONDS)).rowcount
        log.info("database maintenance done; %d old usage rows and %d "
                 "finished reminders pruned", pruned, jobs)

    hk.at("db_maintenance", next_maintenance, maintenance)
    return state

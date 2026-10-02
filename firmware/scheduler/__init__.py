"""Timers, reminders and repeats: everything that happens because the clock says
so.

    scheduler = Scheduler(db, mono, wall, post, synced=..., tz=...)
    scheduler.boot()
    # every frame, on the main thread:
    scheduler.tick(now_mono, now_wall)

It is a min-heap in the main loop and rows in SQLite, not a thread. A thread
would need locks around the heap, the web page that lists reminders and the
tool that adds them; in the main loop none of that exists, and the loop never
sleeps longer than half a second, so a reminder is at most half a second late.
The table is the truth and the heap is a cache that makes `tick()` one
comparison: it is rebuilt from the table at boot and after any change.

Two clocks, because the Pi has no RTC and its wall clock is wrong until NTP
syncs (see `clock.py`): a timer ("in ten minutes") lives on the monotonic clock
and works at once; a reminder ("at six") lives on the wall clock in the owner's
timezone and waits for the sync, neither firing nor being judged missed.

Everything that changes a job goes through `add`, `cancel` and `snooze`, and
the rules live in `add`, not in whatever called it, so the voice tool, the web
page and a local command can never disagree about what is allowed. Anything
refused raises `SchedulerError`, whose message is a sentence the model or the
page can say as it is.
"""

import heapq
import logging

from .. import events as ev
from ..storage import log_event
from . import clock, housekeeping, repeat, speech

log = logging.getLogger("trstenko.scheduler")

PENDING = "pending"
RINGING = "ringing"
DONE = "done"
MISSED = "missed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

ACTIVE = (PENDING, RINGING)

# Plan 02's limits. Beyond them the tool returns an error the model can say.
MAX_ACTIVE_JOBS = 50
MAX_TIMER_SECONDS = 24 * 3600
MAX_TEXT = 200

SNOOZE_DEFAULT_MINUTES = 5
SNOOZE_MAX_MINUTES = 24 * 60

# A reminder that came due while the device was off is still worth saying if
# it was this recent; older than that it is news nobody wants.
BACKLOG_SECONDS = 6 * 3600


class SchedulerError(Exception):
    """A refusal, in a sentence. `kind` is for the web layer's status code (see
    `events.CommandError`); the sentence is for the person."""

    def __init__(self, message, kind=ev.INVALID):
        super().__init__(message)
        self.kind = kind


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and abs(value) != float("inf"))


class Scheduler:
    def __init__(self, db, mono, wall, post, synced=None, tz=None, lang=None):
        """`post(event)` puts an event on the main inbox. `synced()` says
        whether the wall clock can be believed; `tz()` and `lang()` give the
        owner's timezone and language, read when they are needed so a change
        in settings takes effect without telling this class."""
        self.db = db
        self.mono = mono
        self.wall = wall
        self.post = post
        self._sync = synced or (lambda: True)
        self.tz = tz or (lambda: "Europe/Ljubljana")
        self.lang = lang or (lambda: "en")

        self.synced = bool(self._sync())
        self._offset = wall() - mono()
        self._timers = []
        self._walls = []
        self._boot_pass_done = False
        self._interrupted = []
        self.housekeeping = housekeeping.Housekeeping(lambda: self.synced)

    # ---- boot ----

    def boot(self):
        """What a restart does to the jobs on disk (plan 02).

        A stored timer's monotonic target belonged to the boot that made it,
        and means nothing now. One that a reboot cut off is far more often
        missed than still useful, so it is recorded as interrupted and the
        owner is told once. A job that was ringing when the power went counts
        as due again.
        """
        conn = self.db.conn()
        rows = conn.execute(
            "SELECT * FROM jobs WHERE kind = 'timer' AND state IN "
            "('pending', 'ringing')").fetchall()
        for row in rows:
            conn.execute("UPDATE jobs SET state = ? WHERE id = ?",
                         (INTERRUPTED, row["id"]))
            self._interrupted.append(self._view(dict(row)))
            log_event(conn, "timer_interrupted",
                      "timer %d was interrupted by a restart" % row["id"],
                      at=self.wall())
        conn.execute("UPDATE jobs SET state = ? WHERE state = ?",
                     (PENDING, RINGING))
        self.rebuild()
        if self.synced:
            self._boot_pass()

    def take_interrupted(self):
        """The timers a restart cut off, once, for the announcer."""
        out, self._interrupted = self._interrupted, []
        return out

    def _boot_pass(self):
        """Reminders that came due while the device was off, dealt with once
        the clock can be trusted: recent ones are said together, old ones are
        marked missed without a word, and a repeat skips what it missed and
        works out its next firing instead of firing three times after a
        weekend off."""
        self._boot_pass_done = True
        now = self.wall()
        conn = self.db.conn()
        rows = conn.execute(
            "SELECT * FROM jobs WHERE kind != 'timer' AND state = ? "
            "AND due_wall <= ? ORDER BY due_wall", (PENDING, now)).fetchall()
        recent = []
        for row in rows:
            job = dict(row)
            fresh = now - job["due_wall"] <= BACKLOG_SECONDS
            if fresh:
                recent.append(self._view(job))
                if job["repeat"]:
                    self._advance(job, now)
                else:
                    conn.execute("UPDATE jobs SET state = ?, fired_at = ? "
                                 "WHERE id = ?", (DONE, now, job["id"]))
            else:
                self._miss(job, now, quiet=True)
        self.rebuild()
        if recent:
            self.post(ev.Event(ev.DUE, data={"backlog": recent}))

    # ---- the heap ----

    def rebuild(self):
        """Throw the heaps away and read the table again. Done at boot, on
        `ClockSynced` (the wall clock may just have jumped) and after any
        change that is easier to get right by starting over."""
        self._timers = []
        self._walls = []
        rows = self.db.conn().execute(
            "SELECT id, kind, due_mono, due_wall FROM jobs WHERE state = ?",
            (PENDING,)).fetchall()
        for row in rows:
            if row["kind"] == "timer":
                if row["due_mono"] is not None:
                    heapq.heappush(self._timers, (row["due_mono"], row["id"]))
            elif row["due_wall"] is not None:
                heapq.heappush(self._walls, (row["due_wall"], row["id"]))

    def tick(self, now_mono, now_wall):
        """Every frame. Almost always two comparisons and nothing else."""
        while self._timers and self._timers[0][0] <= now_mono:
            due, job_id = heapq.heappop(self._timers)
            self._fire(job_id, due, now_wall)
        if self.synced:
            while self._walls and self._walls[0][0] <= now_wall:
                due, job_id = heapq.heappop(self._walls)
                self._fire(job_id, due, now_wall)
        self.housekeeping.tick(now_mono, now_wall)

    def _fire(self, job_id, due, now_wall):
        row = self._row(job_id)
        if row is None or row["state"] != PENDING:
            return
        key = row["due_mono"] if row["kind"] == "timer" else row["due_wall"]
        if key != due:
            return  # an entry from before a snooze
        self.db.conn().execute(
            "UPDATE jobs SET state = ?, fired_at = ? WHERE id = ?",
            (RINGING, now_wall, job_id))
        row = self._row(job_id)
        log.info("%s %d is due", row["kind"], job_id)
        self.post(ev.Event(ev.DUE, data={"job": self._view(row)}))

    # ---- the clock ----

    def check_clock(self):
        """The housekeeping job: is the clock right yet, and has it jumped?
        One second until it is, then every minute."""
        synced = bool(self._sync())
        offset = self.wall() - self.mono()
        jumped = abs(offset - self._offset) > housekeeping.CLOCK_JUMP
        self._offset = offset
        if synced and not self.synced:
            self.synced = True
            log.info("the clock is synced")
            self.post(ev.Event(ev.CLOCK_SYNCED))
        elif synced and jumped:
            log.info("the clock jumped; rebuilding")
            self.post(ev.Event(ev.CLOCK_SYNCED, data="jump"))

    def on_clock_synced(self):
        """Main's handler for `ClockSynced`."""
        self.synced = True
        self._offset = self.wall() - self.mono()
        self._retarget_timers()
        self.rebuild()
        if not self._boot_pass_done:
            self._boot_pass()

    def _retarget_timers(self):
        """A timer's wall time is for display only, and was worked out against
        a clock that has just been corrected."""
        now_wall, now_mono = self.wall(), self.mono()
        conn = self.db.conn()
        for row in conn.execute(
                "SELECT id, due_mono FROM jobs WHERE kind = 'timer' AND "
                "state = ?", (PENDING,)).fetchall():
            conn.execute("UPDATE jobs SET due_wall = ? WHERE id = ?",
                         (now_wall + (row["due_mono"] - now_mono), row["id"]))

    # ---- adding, cancelling, snoozing ----

    def add(self, text="", seconds=None, local_time=None, repeat_spec=None,
            tz=None, source="voice"):
        """A timer (`seconds`) or a reminder (`local_time`, optionally
        repeating). Returns the new job as the page and the model see it."""
        text = self._text(text)
        if (seconds is None) == (local_time is None):
            raise SchedulerError(
                "Give either how long the timer is, or the time of the "
                "reminder, and not both.")
        if self._active_count() >= MAX_ACTIVE_JOBS:
            raise SchedulerError(
                "You already have %d reminders; cancel some first."
                % MAX_ACTIVE_JOBS, ev.CONFLICT)

        now_mono, now_wall = self.mono(), self.wall()
        conn = self.db.conn()

        if seconds is not None:
            if repeat_spec:
                raise SchedulerError("A timer cannot repeat. Set a reminder "
                                     "at a time instead.")
            if not _number(seconds) or seconds < 1:
                raise SchedulerError("A timer has to be at least one second "
                                     "long.")
            if seconds > MAX_TIMER_SECONDS:
                raise SchedulerError("Timers can be at most 24 hours.")
            cur = conn.execute(
                "INSERT INTO jobs (kind, text, due_wall, due_mono, state, "
                "created_at, source) VALUES ('timer', ?, ?, ?, ?, ?, ?)",
                (text, now_wall + seconds, now_mono + seconds, PENDING,
                 now_wall, source))
            job_id = cur.lastrowid
            heapq.heappush(self._timers, (now_mono + seconds, job_id))
            log.info("timer %d set for %gs", job_id, seconds)
            return self.get(job_id)

        if not text:
            raise SchedulerError("A reminder needs something to say.")
        if not self.synced:
            # Before the first sync the wall clock is days out, so "is that in
            # the future" cannot be answered. A timer would have worked.
            raise SchedulerError(
                "I do not know the time yet, so I cannot set a reminder. "
                "Try again in a minute.", ev.UNAVAILABLE)
        tz = tz or self.tz()
        try:
            clock.zone(tz)
            naive = clock.parse_local(local_time)
            spec = repeat.normalise(repeat_spec)
        except ValueError as exc:
            raise SchedulerError(str(exc))
        due = clock.to_epoch(naive, tz)
        if due <= now_wall:
            raise SchedulerError("That time has already passed.")
        kind = "repeat" if spec else "reminder"
        cur = conn.execute(
            "INSERT INTO jobs (kind, text, local_time, tz, repeat, due_wall, "
            "state, created_at, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, text, clock.local_iso(naive), tz, spec, due, PENDING,
             now_wall, source))
        job_id = cur.lastrowid
        heapq.heappush(self._walls, (due, job_id))
        log.info("%s %d set for %s", kind, job_id, clock.local_iso(naive))
        return self.get(job_id)

    def cancel(self, job_id):
        row = self._need(job_id)
        if row["state"] not in ACTIVE + (MISSED,):
            raise SchedulerError("Reminder %d is already %s."
                                 % (job_id, row["state"]), ev.CONFLICT)
        self.db.conn().execute("UPDATE jobs SET state = ? WHERE id = ?",
                               (CANCELLED, job_id))
        log.info("%s %d cancelled", row["kind"], job_id)
        return self.get(job_id)

    def snooze(self, job_id, minutes=SNOOZE_DEFAULT_MINUTES):
        """Ring again in `minutes`. A repeating reminder's own schedule is
        untouched: this is one extra firing, and its next regular one is still
        worked out when this one is dealt with."""
        row = self._need(job_id)
        if not _number(minutes) or not 1 <= minutes <= SNOOZE_MAX_MINUTES:
            raise SchedulerError("Snooze for between 1 minute and 24 hours.")
        if row["state"] not in ACTIVE + (MISSED,):
            raise SchedulerError("Reminder %d is already %s."
                                 % (job_id, row["state"]), ev.CONFLICT)
        now_mono, now_wall = self.mono(), self.wall()
        gap = minutes * 60
        self.db.conn().execute(
            "UPDATE jobs SET state = ?, due_wall = ?, due_mono = ? "
            "WHERE id = ?", (PENDING, now_wall + gap, now_mono + gap, job_id))
        if row["kind"] == "timer":
            # A snoozed timer is a new countdown, and "your five-minute timer
            # is done" is the line it should get, not the original length plus
            # however long it rang.
            self.db.conn().execute(
                "UPDATE jobs SET created_at = ? WHERE id = ?",
                (now_wall, job_id))
        row = self._row(job_id)
        if row["kind"] == "timer":
            heapq.heappush(self._timers, (now_mono + gap, job_id))
        else:
            heapq.heappush(self._walls, (now_wall + gap, job_id))
        log.info("%s %d snoozed %g minutes", row["kind"], job_id, minutes)
        return self.get(job_id)

    def list(self, state=None):
        """Jobs as the page shows them, soonest first. `state` filters."""
        conn = self.db.conn()
        if state:
            rows = conn.execute("SELECT * FROM jobs WHERE state = ?",
                                (state,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM jobs").fetchall()
        views = [self._view(dict(r)) for r in rows]
        views.sort(key=lambda v: (v["due_wall"] is None, v["due_wall"] or 0,
                                  v["id"]))
        return views

    def get(self, job_id):
        row = self._row(job_id)
        return self._view(row) if row else None

    def summary(self):
        """For the page's Home screen."""
        conn = self.db.conn()
        pending = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE state IN ('pending','ringing')"
        ).fetchone()[0]
        return {"active": pending, "synced": self.synced}

    # ---- what the announcer calls when it is finished with a job ----

    def complete(self, job_id, acked=True):
        """Dealt with. A one-off is done; a repeat goes back to waiting for its
        next firing."""
        row = self._row(job_id)
        if row is None or row["state"] != RINGING:
            return
        now = self.wall()
        job = dict(row)
        if acked:
            self.db.conn().execute("UPDATE jobs SET acked_at = ? WHERE id = ?",
                                   (now, job_id))
        if job["repeat"]:
            self._advance(job, now)
        else:
            self.db.conn().execute("UPDATE jobs SET state = ? WHERE id = ?",
                                   (DONE, job_id))

    def mark_missed(self, job_id):
        """Nobody acknowledged it. A one-off is missed and stays on the page; a
        repeat notes it and carries on to its next firing."""
        row = self._row(job_id)
        if row is None or row["state"] != RINGING:
            return
        self._miss(dict(row), self.wall())

    def _miss(self, job, now, quiet=False):
        conn = self.db.conn()
        if not quiet:
            log.info("%s %d was missed", job["kind"], job["id"])
        log_event(conn, "reminder_missed",
                  "%s: %s" % (job["kind"], job["text"] or "timer"), at=now)
        if job["repeat"]:
            self._advance(job, now)
        else:
            conn.execute("UPDATE jobs SET state = ? WHERE id = ?",
                         (MISSED, job["id"]))

    def _advance(self, job, now):
        """A repeating job's next firing after now, back to pending."""
        naive, epoch = repeat.next_after(
            clock.parse_local(job["local_time"]), job["tz"], job["repeat"],
            now)
        self.db.conn().execute(
            "UPDATE jobs SET state = ?, local_time = ?, due_wall = ? "
            "WHERE id = ?", (PENDING, clock.local_iso(naive), epoch,
                             job["id"]))
        heapq.heappush(self._walls, (epoch, job["id"]))

    # ---- helpers ----

    def _text(self, text):
        if text is None:
            return ""
        if not isinstance(text, str):
            raise SchedulerError("The text has to be words.")
        text = text.strip()
        if len(text) > MAX_TEXT:
            raise SchedulerError("Keep it under %d characters." % MAX_TEXT)
        if any(ord(c) < 32 for c in text):
            raise SchedulerError("The text has a character I cannot say.")
        return text

    def _active_count(self):
        return self.db.conn().execute(
            "SELECT COUNT(*) FROM jobs WHERE state IN ('pending', 'ringing')"
        ).fetchone()[0]

    def _row(self, job_id):
        return self.db.conn().execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def _need(self, job_id):
        if isinstance(job_id, bool) or not isinstance(job_id, int):
            raise SchedulerError("Which reminder? Give its number.")
        row = self._row(job_id)
        if row is None:
            raise SchedulerError("There is no reminder number %d." % job_id,
                                 ev.NOT_FOUND)
        return row

    def _view(self, job):
        """A job as the page and the model see it. `due_mono` is a number that
        means something only to this boot, so it is not shown; a timer's
        remaining time is."""
        lang = self.lang()
        view = {
            "id": job["id"], "kind": job["kind"], "text": job["text"],
            "state": job["state"], "repeat": job["repeat"],
            "local_time": job["local_time"], "tz": job["tz"],
            "due_wall": job["due_wall"], "created_at": job["created_at"],
            "fired_at": job["fired_at"], "acked_at": job["acked_at"],
            "source": job["source"], "say": "",
        }
        if job["kind"] == "timer":
            if job["state"] == PENDING and job["due_mono"] is not None:
                view["remaining"] = max(0.0, job["due_mono"] - self.mono())
            view["length"] = (job["due_wall"] - job["created_at"]
                              if job["due_wall"] else None)
        elif job["local_time"]:
            try:
                local = clock.parse_local(job["local_time"])
                zone = clock.zone(job["tz"])
                now = clock.to_local(self.wall(), job["tz"])
                view["say"] = speech.spoken_when(
                    local.replace(tzinfo=zone), now, lang)
            except ValueError:
                pass
        return view

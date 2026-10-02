"""What the assistant costs, and when it must stop and when it must wait.

The owner pays the provider directly (plan 05), so the device counts what it
spends, stops at a limit the owner set, and behaves sensibly when a provider
says "too many" or "out of credit". Every request, including the ones that fail
and the one that is cut off, passes through here, because a provider bills what
it generated before we stopped reading and counts a failed request against its
rate limit.

Two decisions about the limits, and why:

- **`allow()` judges the worst case before a request, not the cost after.** The
  last turn before a cap would otherwise go over it. The worst case is the
  estimated input plus the whole reply allowance, which at a tenth of a cent a
  turn costs nothing anyone will notice.
- **The totals live in memory.** `allow()` runs on the worker thread before
  every request and must never wait on SQLite. They are loaded from the usage
  table at boot, kept up to date by `record()`, and the rows are written in
  batches by the main thread (plan 06: nothing on a hot path touches the card).

Cooldowns are shared across turns. After a rate limit the next turn does not try
the provider at all until it is over, and says "busy" at once instead of after
a timeout. After a quota error the wait is long, and a banner is raised for the
page. (Plan 05 says a spend limit lasts until the owner changes something. That
would leave a device silent for good after a top-up nobody told it about, so it
tries again every half hour, and at once if the owner touches the key or the
limits.)

Money is whole millionths of a dollar throughout, so a month of sums does not
drift. Threads: the worker calls `allow`, `begin_turn`, `record`; the main
thread calls `summary`, `flush` and `load`. One lock covers the state.
"""

import json
import logging
import sqlite3
import threading
import time

from ..scheduler import clock as sched_clock
from ..storage import log_event

log = logging.getLogger("trstenko.meter")

# A token is about four characters of text. Only used when the provider sent no
# usage (a cut stream, a provider that never does), and for the worst-case
# estimate before a request.
CHARS_PER_TOKEN = 4
# Google's figure for audio it is sent: 32 tokens a second.
AUDIO_TOKENS_PER_SECOND = 32

# Plan 05: no Retry-After and no reset headers, then 2s, 4s, 8s, up to 60s.
BACKOFF = (2.0, 4.0, 8.0, 16.0, 32.0, 60.0)
# A cooldown a provider asks for is believed, up to an hour; longer than that is
# not a cooldown.
MAX_COOLDOWN = 3600.0
# A 429 that could be the minute limit or the day limit, twice within this long,
# is the day limit (plan 05).
AMBIGUOUS_WINDOW = 60.0
# How often to try again after an out-of-credit error that has no known end.
QUOTA_RECHECK = 1800.0
# One event row per kind and provider per this long, so a stuck loop of 429s is
# one line on the page and not five hundred.
EVENT_EVERY = 60.0

# Plan 05: usage is written in batches, not per request, to spare the card.
FLUSH_EVERY = 30.0
# The rollup waits a minute past midnight, so the last request of the day has
# been flushed before the day is summed.
ROLLUP_AFTER = 60.0

WARN_AT = 0.8
# What a pending buffer may hold when the database will not take it, so a full
# card does not become a full memory.
PENDING_MAX = 2000

# Why a request was not made.
RATE_LIMITED = "rate_limited"
QUOTA = "quota"
DAILY_BUDGET = "daily_budget"
MONTHLY_BUDGET = "monthly_budget"
DAILY_TURNS = "daily_turns"

INSERT_USAGE = (
    "INSERT INTO usage (at, day, turn_id, provider, model, kind, "
    "input_tokens, cached_tokens, output_tokens, reasoning_tokens, "
    "audio_seconds, estimated, cost_usd_micro, latency_ms, status, free_tier) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


class Verdict:
    """Whether a request may be made. `wait` is how long a cooldown has left."""

    __slots__ = ("ok", "reason", "wait")

    def __init__(self, ok=True, reason=None, wait=0.0):
        self.ok = ok
        self.reason = reason
        self.wait = wait

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return "Verdict(%s%s)" % ("ok" if self.ok else self.reason,
                                  " %.0fs" % self.wait if self.wait else "")


def limits_from_settings(settings):
    """The owner's limits, read when asked so a change on the page applies to
    the very next request."""
    def read():
        data = settings.data
        out = dict(data["budget"])
        out["free_tier"] = data["assistant"]["free_tier"]
        return out
    return read


def seconds_until_midnight(wall, tzname):
    """From `wall` to the next midnight in `tzname`."""
    import datetime

    local = sched_clock.to_local(wall, tzname)
    tomorrow = datetime.datetime.combine(
        local.date() + datetime.timedelta(days=1), datetime.time(0))
    return max(1.0, sched_clock.to_epoch(tomorrow, tzname) - wall)


def next_midnight(after, tzname):
    return after + seconds_until_midnight(after, tzname)


def estimate_tokens(chars):
    return max(0, int(chars)) // CHARS_PER_TOKEN


class Meter:
    def __init__(self, prices, limits, tz, mono=time.monotonic,
                 wall=time.time, providers=None):
        self._prices = prices
        self._limits = limits
        self._tz = tz
        self._mono = mono
        self._wall = wall
        self._providers = providers
        self._lock = threading.RLock()

        self._day = self._local_day()
        self._month = self._day[:7]
        self.spent_day = 0
        self.spent_month = 0
        self.turns_day = 0
        self.requests_day = 0
        self.fallbacks_day = 0
        self._warned = set()
        self._reached = set()

        self._pending = []
        self._events = []
        self._cool = {}
        self._banners = {}
        self._unknown = set()
        self._event_at = {}
        self._turn_seen = set()

    # ---- time ----

    def _local_day(self):
        return sched_clock.to_local(self._wall(), self._tz()).date().isoformat()

    def _roll(self):
        """Move to a new day or month when the owner's midnight has passed.
        Cheap enough to run on every call, which is why there is no timer."""
        day = self._local_day()
        if day == self._day:
            return
        if day[:7] != self._month:
            self.spent_month = 0
            self._month = day[:7]
        self._day = day
        self.spent_day = 0
        self.turns_day = 0
        self.requests_day = 0
        self.fallbacks_day = 0
        self._turn_seen = set()
        log.info("a new day: the daily counters start again")

    # ---- loading and writing ----

    def load(self, db):
        """Totals for today and this month from what is already stored."""
        conn = db.conn()
        with self._lock:
            self._roll()
            month = conn.execute(
                "SELECT COALESCE(SUM(cost_usd_micro), 0) FROM usage "
                "WHERE day LIKE ?", (self._month + "%",)).fetchone()[0]
            day = conn.execute(
                "SELECT COALESCE(SUM(cost_usd_micro), 0), COUNT(*), "
                "COUNT(DISTINCT turn_id) FROM usage WHERE day = ?",
                (self._day,)).fetchone()
            self.spent_month = int(month)
            self.spent_day = int(day[0])
            self.requests_day = int(day[1])
            self.turns_day = int(day[2])
            for row in conn.execute("SELECT detail FROM events_log "
                                    "WHERE kind = 'budget_warning'"):
                self._warned.add(row[0])

    def flush(self, db):
        """Write what is waiting. Main thread, from a chore every 30s and when
        the device shuts down. A failure puts it back, bounded."""
        with self._lock:
            rows, self._pending = self._pending, []
            events, self._events = self._events, []
        if not rows and not events:
            return 0
        conn = db.conn()
        try:
            conn.execute("BEGIN")
            conn.executemany(INSERT_USAGE, rows)
            for at, kind, detail in events:
                log_event(conn, kind, detail, at)
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            with self._lock:
                self._pending = (rows + self._pending)[-PENDING_MAX:]
                self._events = (events + self._events)[-PENDING_MAX:]
            log.error("usage could not be written (%s); keeping %d rows",
                      exc, len(rows))
            return 0
        return len(rows)

    def rollup(self, db):
        """Yesterday and earlier into `usage_daily`, which outlives the rows
        (plan 05). Idempotent: run after every midnight, and at boot in case
        the device was off at the time."""
        with self._lock:
            self._roll()
            today = self._day
        conn = db.conn()
        cur = conn.execute(
            "INSERT OR REPLACE INTO usage_daily (day, provider, model, "
            "requests, input_tokens, output_tokens, audio_seconds, "
            "cost_usd_micro) "
            "SELECT day, provider, model, COUNT(*), SUM(input_tokens), "
            "SUM(output_tokens), SUM(audio_seconds), SUM(cost_usd_micro) "
            "FROM usage WHERE day < ? GROUP BY day, provider, model",
            (today,))
        return cur.rowcount

    def register_chores(self, housekeeping, db):
        """Put the meter's two jobs in the scheduler's list: write the usage
        every 30 seconds, and roll yesterday up after midnight. Both run on the
        main thread, which is the one that writes (plan 06)."""
        housekeeping.every("flush_usage", FLUSH_EVERY,
                           lambda: self.flush(db))
        housekeeping.at("usage_rollup",
                        lambda after: next_midnight(after, self._tz())
                        + ROLLUP_AFTER,
                        lambda: self.rollup(db))
        # In case the device was off at the last midnight.
        self.rollup(db)

    def set_prices(self, prices):
        with self._lock:
            self._prices = prices

    # ---- before a request ----

    def estimate_input(self, messages, tools=None, audio_seconds=0.0):
        """A cheap count of what a request will send, for the worst case."""
        chars = len(json.dumps(messages, ensure_ascii=False))
        if tools:
            chars += len(json.dumps(tools, ensure_ascii=False))
        return estimate_tokens(chars) + int(audio_seconds
                                            * AUDIO_TOKENS_PER_SECOND)

    def _cost(self, provider, model, free, **usage):
        """Millionths of a dollar, 0 for a model on the owner's free tier, and
        None for a model with no price."""
        cost = self._prices.cost_micro(provider, model, day=self._day, **usage)
        if cost is None:
            return None
        if free and self._prices.free_model(provider, model):
            return 0
        return cost

    def begin_turn(self, turn_id=None):
        """A turn is about to use the model. Counts it, or refuses because the
        owner's turns for the day are spent. A follow-up is a turn too."""
        with self._lock:
            self._roll()
            cap = self._limits().get("daily_turns")
            if cap is not None and self.turns_day >= cap:
                self._note_reached("turns:%s" % self._day, "daily turn limit")
                return Verdict(False, DAILY_TURNS)
            self.turns_day += 1
            return Verdict()

    def allow(self, provider, model, est_input, max_output,
              audio_seconds=0.0):
        """May this request be made? Checks the provider's cooldown, then the
        owner's money limits against the worst case for this request.
        `audio_seconds` is for a speech-to-text request, which is priced by the
        length of the audio and not by tokens."""
        with self._lock:
            self._roll()
            wait = self._cooldown_left(provider)
            if wait > 0:
                entry = self._cool[provider]
                return Verdict(False, entry["reason"], wait)
            limits = self._limits()
            worst = self._cost(provider, model, limits.get("free_tier"),
                               input_tokens=est_input,
                               output_tokens=max_output,
                               audio_seconds=audio_seconds) or 0
            daily, monthly = limits.get("daily_usd"), limits.get("monthly_usd")
            if daily is not None and self.spent_day + worst > _micro(daily):
                self._note_reached("day:%s" % self._day, "daily spending limit")
                return Verdict(False, DAILY_BUDGET)
            if monthly is not None and (self.spent_month + worst
                                        > _micro(monthly)):
                self._note_reached("month:%s" % self._month,
                                   "monthly spending limit")
                return Verdict(False, MONTHLY_BUDGET)
            return Verdict()

    def take_warning(self):
        """The one warning due, if any, and only once for each period: "you
        have used 80% of this month's budget". The loop says it at the end of
        the next turn (plan 05)."""
        with self._lock:
            self._roll()
            limits = self._limits()
            for kind, spent, cap, key in (
                    ("month", self.spent_month, limits.get("monthly_usd"),
                     "month:%s" % self._month),
                    ("day", self.spent_day, limits.get("daily_usd"),
                     "day:%s" % self._day)):
                if cap is None or key in self._warned:
                    continue
                cap = _micro(cap)
                if cap > 0 and WARN_AT * cap <= spent < cap:
                    self._warned.add(key)
                    self._event("budget_warning", key)
                    return "budget_%s" % kind
            return None

    def _note_reached(self, key, text):
        if key in self._reached:
            return
        self._reached.add(key)
        self._event("budget_reached", text)
        log.warning("%s reached", text)

    # ---- after a request ----

    def record(self, attempt, turn_id, kind="chat", audio_seconds=0.0):
        """Account for one HTTP request. Called with each `Attempt` as it
        happens, from the worker thread. Returns the row, as a dict."""
        with self._lock:
            self._roll()
            limits = self._limits()
            free = bool(limits.get("free_tier"))
            usage = attempt.usage
            estimated = False
            billable = attempt.http_status == 200
            if usage is not None:
                tokens = dict(input_tokens=usage.input_tokens,
                              cached_tokens=usage.cached_tokens,
                              output_tokens=usage.output_tokens)
                reasoning = usage.reasoning_tokens
            elif billable and kind == "chat":
                # The provider never said what it used, or the stream was cut
                # before it could. It billed what it generated until then.
                estimated = True
                tokens = dict(input_tokens=estimate_tokens(
                    attempt.request_chars), cached_tokens=0,
                    output_tokens=estimate_tokens(attempt.received_chars))
                reasoning = 0
            else:
                tokens = dict(input_tokens=0, cached_tokens=0,
                              output_tokens=0)
                reasoning = 0
            audio = float(audio_seconds) if (billable or attempt.status
                                             == "ok") else 0.0
            cost = self._cost(attempt.provider, attempt.model, free,
                              audio_seconds=audio, **tokens)
            if cost is None:
                if (attempt.http_status == 200 or attempt.status == "ok"):
                    self._unknown.add("%s/%s" % (attempt.provider,
                                                 attempt.model))
                cost = 0
            free_flag = int(free and self._prices.free_model(
                attempt.provider, attempt.model))
            at = self._wall()
            row = (at, self._day, str(turn_id), attempt.provider,
                   attempt.model, kind, tokens["input_tokens"],
                   tokens["cached_tokens"], tokens["output_tokens"],
                   reasoning, audio, int(estimated), cost,
                   attempt.latency_ms, attempt.status, free_flag)
            self._pending.append(row)
            if len(self._pending) > PENDING_MAX:
                del self._pending[:len(self._pending) - PENDING_MAX]
            self.spent_day += cost
            self.spent_month += cost
            self.requests_day += 1
            self._after(attempt, free)
            return dict(zip(("at", "day", "turn_id", "provider", "model",
                             "kind", "input_tokens", "cached_tokens",
                             "output_tokens", "reasoning_tokens",
                             "audio_seconds", "estimated", "cost_usd_micro",
                             "latency_ms", "status", "free_tier"), row))

    def _after(self, attempt, free):
        """What an attempt means for the provider's cooldown and the banners."""
        provider = attempt.provider
        now = self._mono()
        entry = self._cool.setdefault(provider, {"until": 0.0, "count": 0,
                                                 "reason": RATE_LIMITED,
                                                 "ambiguous_at": None})
        status = attempt.status
        limits = attempt.ratelimit or {}

        if status == "ok":
            entry["count"] = 0
            self._banners.pop(provider, None)
            if (limits.get("remaining_requests") == 0
                    or limits.get("remaining_tokens") == 0):
                # The next request would be refused. Wait it out here, where
                # a person is not.
                wait = max(limits.get("reset_requests", 0.0),
                           limits.get("reset_tokens", 0.0)) or BACKOFF[0]
                self._cooldown(entry, wait, RATE_LIMITED)
                self._event("rate_limit", "%s: no requests left, waiting "
                            "%.0fs" % (provider, wait), provider)
            return

        if status == "rate_limited":
            if attempt.ambiguous:
                last = entry["ambiguous_at"]
                entry["ambiguous_at"] = now
                if last is not None and now - last <= AMBIGUOUS_WINDOW:
                    # Twice in a minute is not the minute limit.
                    self._quota(provider, entry, free,
                                "%s: the daily limit looks reached" % provider)
                    return
            entry["count"] += 1
            if attempt.retry_after is not None:
                wait = attempt.retry_after
            elif limits.get("reset_requests") or limits.get("reset_tokens"):
                wait = max(limits.get("reset_requests", 0.0),
                           limits.get("reset_tokens", 0.0))
            else:
                wait = BACKOFF[min(entry["count"] - 1, len(BACKOFF) - 1)]
            self._cooldown(entry, wait, RATE_LIMITED)
            self._event("rate_limit", "%s: slowing down for %.0fs"
                        % (provider, min(wait, MAX_COOLDOWN)), provider)
        elif status == "quota":
            self._quota(provider, entry, free,
                        "%s: out of credit or over a limit" % provider)
        elif status == "auth":
            self._banners[provider] = {"kind": "auth", "since": self._wall()}
            self._event("auth", "%s: the key was refused" % provider, provider)

    def _quota(self, provider, entry, free, text):
        wait = QUOTA_RECHECK
        info = (self._providers.get(provider).free_tier
                if self._providers is not None else None)
        if free and info:
            # A free tier's daily quota comes back at a known midnight.
            wait = seconds_until_midnight(
                self._wall(), info.get("resets_tz") or self._tz())
        self._cooldown(entry, wait, QUOTA)
        self._banners[provider] = {"kind": "quota", "since": self._wall(),
                                   "until": self._wall() + wait}
        self._event("quota", text, provider)

    def _cooldown(self, entry, seconds, reason):
        seconds = max(0.0, min(float(seconds), MAX_COOLDOWN
                               if reason == RATE_LIMITED else 86400.0))
        until = self._mono() + seconds
        if until > entry["until"]:
            entry["until"] = until
            entry["reason"] = reason
        log.info("cooling down for %.0fs (%s)", seconds, reason)

    def _cooldown_left(self, provider):
        entry = self._cool.get(provider)
        if entry is None:
            return 0.0
        return max(0.0, entry["until"] - self._mono())

    def _event(self, kind, detail, provider=""):
        """Queue a line for the page's list, at most one a minute for each
        kind and provider."""
        key = (kind, provider, detail if kind.startswith("budget") else "")
        now = self._mono()
        last = self._event_at.get(key)
        if last is not None and now - last < EVENT_EVERY:
            return
        self._event_at[key] = now
        self._events.append((self._wall(), kind, detail))

    def note_fallback(self, from_provider, to_provider, reason):
        """A turn moved to the second key. The page counts these, because a
        fallback every day means the first key's plan is too small."""
        with self._lock:
            self.fallbacks_day += 1
            self._events.append((self._wall(), "fallback",
                                 "%s to %s (%s)" % (from_provider,
                                                    to_provider, reason)))

    def clear(self, provider=None):
        """The owner changed a key or a limit: forget cooldowns and banners and
        try again at once."""
        with self._lock:
            for name in ([provider] if provider else list(self._cool)):
                self._cool.pop(name, None)
                self._banners.pop(name, None)
            self._reached.clear()

    # ---- what the page shows ----

    def cooldown(self, provider):
        with self._lock:
            return self._cooldown_left(provider)

    def summary(self):
        with self._lock:
            self._roll()
            limits = self._limits()

            def part(spent, cap):
                cap_micro = _micro(cap) if cap is not None else None
                return {"spent_usd": spent / 1e6,
                        "limit_usd": cap,
                        "percent": (round(100.0 * spent / cap_micro, 1)
                                    if cap_micro else None)}
            return {
                "month": part(self.spent_month, limits.get("monthly_usd")),
                "day": part(self.spent_day, limits.get("daily_usd")),
                "turns_today": self.turns_day,
                "turn_limit": limits.get("daily_turns"),
                "requests_today": self.requests_day,
                "fallbacks_today": self.fallbacks_day,
                "free_tier": bool(limits.get("free_tier")),
                "usd_to_eur": self._prices.usd_to_eur,
                "cooldowns": {p: round(self._cooldown_left(p), 1)
                              for p in self._cool
                              if self._cooldown_left(p) > 0},
                "banners": [dict(kind=b["kind"], provider=p,
                                 since=b["since"], until=b.get("until"))
                            for p, b in self._banners.items()],
                "unpriced_models": sorted(self._unknown),
            }


def _micro(usd):
    return int(round(float(usd) * 1_000_000))

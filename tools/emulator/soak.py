"""The soak test (plan 08): a simulated day on the device, looking for what
only shows after hours.

    python -m tools.emulator.headless --soak 24h

The device is the real one with the whole assistant on it (the turn loop, the
tools, the meter, the scheduler, the speech process), against the fake
provider. Time is the fake clock, so a day takes a few minutes:

- a turn every two simulated minutes: mostly a spoken answer, sometimes a
  timer, a reminder for later in the day or a memory to save;
- one turn in twenty fails (a server error, a rate limit, a stream cut short);
- the microphone switch is flipped for a few minutes every three hours;
- about half the reminders and timers that go off are acknowledged with a
  button, and the rest are left to be missed.

Once a simulated hour it writes down the process's memory, its thread count,
and the size of every table and queue. At the end it checks:

- **memory**: resident memory after the warm-up hours stays flat;
- **threads**: no more at the end than after warm-up (no thread per turn);
- **rows**: the tables that are meant to be capped are, and the rest grow no
  faster than the work that was done;
- **faults**: no step of the loop ever raised, and no worker was given up on.

It is not part of the ordinary headless run: it takes minutes, and its memory
check depends on the machine. On the board the same test runs in real time for
24 hours before the competition (plan 08).
"""

import os
import random
import sys
import threading
import time

from firmware.hal.base import VOL_UP
from firmware.storage import EVENTS_KEPT

from . import fake_provider as fp

TURN_EVERY = 120.0          # simulated seconds between turns
FAIL_ONE_IN = 20
MUTE_EVERY_HOURS = 3
WARM_UP_HOURS = 2
# What "flat" means for resident memory after warm-up. Python's allocator
# does not hand every freed page back, so a few megabytes of drift is noise;
# a leak of even 1KB a turn would pass this in a day and fail it in a week,
# which is why the per-hour numbers are printed as well.
MEMORY_SLACK = 12 * 1024 * 1024
STEP = 10.0                 # the clock moves in steps this size
SEED = 20261003


def rss_bytes():
    """This process's resident memory, or None where it cannot be read."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),
                                      ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
        return None
    try:
        with open("/proc/self/statm", encoding="ascii") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def parse_duration(text):
    """'24h', '90m' or '2.5h' as simulated hours."""
    text = str(text).strip().lower()
    if text.endswith("m"):
        return float(text[:-1]) / 60.0
    return float(text[:-1] if text.endswith("h") else text)


TABLES = ("jobs", "usage", "usage_daily", "events_log", "turn_log", "memories")


def _sizes(rig):
    conn = rig.device.db.conn()
    out = {name: conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
           for name in TABLES}
    out["active_jobs"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE state IN ('pending', 'ringing')"
    ).fetchone()[0]
    return out


def _script(rng, turn, hour):
    """What the fake model answers for this turn, and what kind it was."""
    if rng.randrange(FAIL_ONE_IN) == 0:
        return rng.choice((
            ("fail", [fp.error(503, "unavailable", "try later"),
                      fp.error(503, "unavailable", "try later")]),
            ("fail", [fp.error(429, "rate_limit_exceeded", "slow down",
                               retry_after=30)]),
            ("fail", [fp.text("This sentence is never finished.", pieces=4, drop_after=1)]),
        ))
    roll = rng.random()
    if roll < 0.15:
        return "timer", [fp.calls(("set_timer", {
            "seconds": rng.choice((60, 180, 300)), "label": "soak"}),
            usage=(900, 20))]
    if roll < 0.20:
        when = "2026-10-02T%02d:%02d" % (17 + hour % 6, rng.randrange(60))
        return "reminder", [fp.calls(("set_reminder", {
            "when": when, "text": "Soak reminder %d." % turn}),
            usage=(900, 25))]
    if roll < 0.25:
        return "memory", [fp.calls(("save_memory", {
            "text": "The user said thing number %d." % turn,
            "category": "fact"}), usage=(900, 20))]
    return "talk", [fp.text("<heard>question %d</heard>That is answer %d. It "
                            "has two sentences." % (turn, turn),
                            usage=(880, 14))]


def run(hours, report, root, say=print):
    """Soak for `hours` of simulated time and report the checks."""
    from .assistant_scenarios import AssistantRig

    rng = random.Random(SEED)
    rig = AssistantRig(root, "soak")
    device, clock, fake = rig.device, rig.clock, rig.fake
    hal = rig.sched.hal
    samples = []
    counts = {"talk": 0, "timer": 0, "reminder": 0, "memory": 0, "fail": 0,
              "unfinished": 0, "acked": 0}
    started = time.monotonic()

    def advance(seconds):
        """Move the clock in steps, giving the device's own thread time to
        notice each one, as a loop that sleeps would."""
        left = seconds
        while left > 0:
            step = min(STEP, left)
            clock.advance(step)
            left -= step
            time.sleep(0.004)

    def sample(hour):
        entry = {"hour": hour, "rss": rss_bytes(),
                 "threads": threading.active_count(),
                 "events": len(device.events),
                 "history": len(rig.assistant.session.start_turn())}
        entry.update(_sizes(rig))
        samples.append(entry)
        say("  hour %2d  rss %s  threads %d  jobs %d (active %d)  usage %d  "
            "events_log %d  turn_log %d  memories %d"
            % (hour, "%.1fMB" % (entry["rss"] / 1048576.0)
               if entry["rss"] else "?", entry["threads"], entry["jobs"],
               entry["active_jobs"], entry["usage"], entry["events_log"],
               entry["turn_log"], entry["memories"]))

    try:
        rig.wait(lambda: device.face.current == "face_idle", 3.0)
        advance(5.0)
        turns = int(hours * 3600 / TURN_EVERY)
        muted_until = -1
        for turn in range(turns):
            hour = int(turn * TURN_EVERY // 3600)
            if turn % int(3600 / TURN_EVERY) == 0:
                sample(hour)
                if hour and hour % MUTE_EVERY_HOURS == 0:
                    hal.set_mute(True)
                    muted_until = turn + 2

            if turn == muted_until:
                hal.set_mute(False)
            if turn >= muted_until:
                # Something going off? Half the time somebody presses a
                # button; otherwise it rings out and is marked missed.
                if device.announcer.current is not None \
                        and rng.random() < 0.5:
                    hal.press(VOL_UP)
                    time.sleep(0.02)
                    hal.release(VOL_UP)
                    counts["acked"] += 1
                    rig.wait(lambda: device.announcer.current is None, 2.0)
                if device.turn is None and device.announcer.current is None:
                    kind, replies = _script(rng, turn, hour)
                    fake.clear()
                    fake.queue(*replies)
                    if rig.wake():
                        rig.hear()
                        if rig.finished(20.0):
                            counts[kind] += 1
                        else:
                            counts["unfinished"] += 1
                            device.turn.cancel()
                # What the harness itself keeps, which is not the device's.
                del rig.stages[:]
                del rig.sched.plays[:]
                del rig.sched.logs[:]
                del rig.audio.captures[:-1]
            advance(TURN_EVERY)
        sample(int(hours))
    finally:
        faults = dict(device.faults)
        failing = device.failing()
        workers = device.supervisor.status()
        rig.close()

    took = time.monotonic() - started
    done = sum(counts[k] for k in ("talk", "timer", "reminder", "memory",
                                   "fail"))
    say("  %d turns in %.0fs: %s" % (done, took, counts))

    report.check("the soak ran its turns: %d of %d, with every kind in it"
                 % (done, turns),
                 done >= turns * 0.85 and counts["unfinished"] <= 2
                 and (hours < WARM_UP_HOURS
                      or all(counts[k] for k in ("talk", "timer", "fail"))),
                 str(counts))
    report.check("no step of the loop raised, in a simulated %g hours" % hours,
                 not any(faults.values()) and not failing,
                 "%s %s" % ({k: v for k, v in faults.items() if v}, failing))
    report.check("no worker was given up on",
                 all(w["state"] != "down" for w in workers.values()),
                 str(workers))

    # A run too short to have warmed up is compared from its first sample.
    warm = [s for s in samples if s["hour"] >= WARM_UP_HOURS] or samples[:1]
    first, last = warm[0], samples[-1]
    if first["rss"] and last["rss"]:
        grew = last["rss"] - first["rss"]
        report.check("memory is flat after warm-up (%+.1fMB from hour %d to "
                     "hour %d)" % (grew / 1048576.0, first["hour"],
                                   last["hour"]),
                     grew < MEMORY_SLACK, "%d bytes" % grew)
    else:
        say("  (memory could not be read on this platform)")
    # Counted for the whole process, so only a full run is judged on it: inside
    # the ordinary suite other scenarios' threads come and go meanwhile.
    report.check("no thread per turn: %d threads after warm-up, %d at the end"
                 % (first["threads"], last["threads"]),
                 hours < WARM_UP_HOURS
                 or last["threads"] <= first["threads"] + 1)
    report.check("the event queue and the conversation history stay short",
                 max(s["events"] for s in samples) < 50
                 and max(s["history"] for s in samples) <= 40,
                 "%s %s" % (max(s["events"] for s in samples),
                            max(s["history"] for s in samples)))
    report.check("the events log is capped at %d rows" % EVENTS_KEPT,
                 max(s["events_log"] for s in samples) <= EVENTS_KEPT + 50,
                 str(max(s["events_log"] for s in samples)))
    report.check("the conversation log stays empty: it is off by default",
                 last["turn_log"] == 0, str(last["turn_log"]))
    report.check("reminders and timers do not pile up as active",
                 max(s["active_jobs"] for s in samples) < 60,
                 str(max(s["active_jobs"] for s in samples)))
    report.check("usage rows grow no faster than requests were made",
                 last["usage"] <= done * 3 + 10, "%d rows for %d turns"
                 % (last["usage"], done))
    return samples

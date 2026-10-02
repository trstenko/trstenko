"""Telling a person that something is due, and not giving up too soon.

When a job comes due the main thread decides *when* to announce it, and this
runs the announcement: a chime, the line spoken, a short window in which the
person can answer, and then either an acknowledgement or another try.

| The device is... | Then |
|---|---|
| idle | announce now |
| in a turn | wait for it to end, then a short pause; never interrupt a sentence |
| muted | announce anyway: the switch cuts the microphone, not the speaker, and someone who muted it still expects the six o'clock reminder |
| in quiet hours | a reminder is soft; a timer rings normally, because somebody set it on purpose |
| installing an update | hold; the job is in SQLite and counts as due again after the restart |

**No model call.** A reminder works with an expired key, no internet and a
spent budget, because what it says is a fixed line with the reminder's own
text in it.

**Speech is a placeholder.** The text goes to `say`, which until Piper arrives
(M2) logs it. The call site is `_speak`; `tts.say` replaces `say` there and
nothing else moves. How long speech takes is estimated, with a constant to be
measured on the board.

**Unacknowledged.** A reminder repeats twice more, a minute apart, then becomes
missed and shows on the page. A timer rings every ten seconds for five minutes,
then stops and becomes missed: a timer nobody stops is a kitchen problem, not
one the device should solve by ringing forever.

Everything here runs on the main thread, from `tick`, on whatever clock it was
given, so a test can ring a timer for five minutes without waiting.
"""

import collections
import logging
import os
import wave

from . import speech

log = logging.getLogger("trstenko.announce")

# Plan 02.
ACK_WINDOW = 8.0
REMINDER_RINGS = 3
REMINDER_GAP = 60.0
TIMER_GAP = 10.0
TIMER_GIVE_UP = 300.0

# After a turn, a breath before a reminder speaks over the end of it.
AFTER_TURN_PAUSE = 1.0
BETWEEN_ANNOUNCEMENTS = 1.0

# How long a spoken line takes, for deciding when the acknowledgement window
# opens. A guess until Piper is on the board.
# TODO measure Piper's real speaking rate on the Pi, in both languages, and
# replace these two numbers with it.
SPEECH_BASE_SECONDS = 0.4
SPEECH_SECONDS_PER_CHAR = 0.065

SOUNDS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))), "assets", "sounds")

CHIME = "chime"
SPEAK = "speak"
WINDOW = "window"
WAIT = "wait"


class Chime:
    def __init__(self, pcm=b"", rate=16000):
        self.pcm = pcm
        self.rate = rate

    @property
    def seconds(self):
        return len(self.pcm) / float(self.rate * 2) if self.rate else 0.0


def load_chime(name, directory=SOUNDS):
    """Decode a chime once, at boot. A missing file is a silent chime and a
    log line, not a device that cannot boot."""
    path = os.path.join(directory, name)
    try:
        with wave.open(path, "rb") as fh:
            if fh.getnchannels() != 1 or fh.getsampwidth() != 2:
                raise ValueError("expected 16-bit mono")
            return Chime(fh.readframes(fh.getnframes()), fh.getframerate())
    except (OSError, ValueError, EOFError, wave.Error) as exc:
        log.warning("chime %s could not be loaded (%s); this reminder will "
                    "be silent until it can", name, exc)
        return Chime()


def estimate_seconds(text):
    return SPEECH_BASE_SECONDS + SPEECH_SECONDS_PER_CHAR * len(text)


class _Active:
    def __init__(self, kind, job=None, jobs=None, text=""):
        self.kind = kind          # 'job' | 'backlog' | 'notice'
        self.job = job
        self.jobs = jobs or []
        self.text = text
        self.phase = None
        self.until = 0.0
        self.rings = 0
        self.first_ring = 0.0
        self.next_ring = 0.0
        self.saved_volume = None


class Announcer:
    def __init__(self, scheduler, speaker, mono, say=None, busy=None,
                 quiet_volume=None, lang=None, chimes=None):
        """`busy()` is true while the main thread says to hold (a turn, an
        update going in). `quiet_volume(kind)` gives the volume to use now for
        a 'reminder' or 'timer', or None for the normal one."""
        self.scheduler = scheduler
        self.speaker = speaker
        self.mono = mono
        self._say = say or (lambda text: log.info("say: %s", text))
        self._busy = busy or (lambda: False)
        self._quiet_volume = quiet_volume or (lambda kind: None)
        self.lang = lang or (lambda: "en")
        self.chimes = chimes or {
            "reminder": load_chime("chime_reminder.wav"),
            "timer": load_chime("chime_timer.wav"),
        }

        self.queue = collections.deque()
        self.current = None
        self._hold_until = 0.0
        self._was_busy = False

    def set_say(self, say):
        """Where spoken lines go. The synthesizer, once there is one."""
        self._say = say

    # ---- what the conductor asks ----

    @property
    def active(self):
        """Is the alert face up? Not during the quiet between repeats."""
        cur = self.current
        return cur is not None and cur.phase in (CHIME, SPEAK, WINDOW)

    @property
    def ringing(self):
        """The id of a job that is waiting to be acknowledged, if any."""
        cur = self.current
        if cur is not None and cur.kind == "job":
            return cur.job["id"]
        return None

    @property
    def in_window(self):
        """Is the device listening for 'stop' or 'snooze' without the wake word
        (plan 03 uses this to run only the local command matcher)?"""
        cur = self.current
        return cur is not None and cur.phase == WINDOW

    def summary(self):
        return {"announcing": self.active, "ringing": self.ringing,
                "queued": len(self.queue)}

    # ---- being told ----

    def enqueue(self, data):
        """A `Due` event's payload: one job, or the ones missed while off."""
        if "backlog" in data:
            self.queue.append(("backlog", data["backlog"]))
        else:
            self.queue.append(("job", data["job"]))

    def enqueue_interrupted(self, jobs):
        """Timers a restart cut off, said once at the next idle moment."""
        if jobs:
            labels = [j["text"] for j in jobs]
            self.queue.append(("notice", speech.interrupted_line(
                labels, self.lang())))

    def acknowledge(self):
        """A button, or 'stop' in the window. Returns whether there was
        anything to acknowledge."""
        cur = self.current
        if cur is None:
            return False
        if cur.kind == "job":
            self.scheduler.complete(cur.job["id"], acked=True)
        self.speaker.stop()
        self._finish()
        return True

    def snooze(self, minutes=None):
        cur = self.current
        if cur is None or cur.kind != "job":
            return False
        if minutes is None:
            self.scheduler.snooze(cur.job["id"])
        else:
            self.scheduler.snooze(cur.job["id"], minutes)
        self.speaker.stop()
        self._finish()
        return True

    def job_cancelled(self, job_id):
        """The owner cancelled it from the page while it was ringing or
        waiting."""
        cur = self.current
        if cur is not None and cur.kind == "job" and cur.job["id"] == job_id:
            self.speaker.stop()
            self._finish()
        self.queue = collections.deque(
            item for item in self.queue
            if not (item[0] == "job" and item[1]["id"] == job_id))

    # ---- the clock ----

    def tick(self, now):
        busy = bool(self._busy())
        if self._was_busy and not busy:
            self._hold_until = max(self._hold_until, now + AFTER_TURN_PAUSE)
        self._was_busy = busy

        cur = self.current
        if cur is None:
            if self.queue and not busy and now >= self._hold_until:
                self._start(self.queue.popleft(), now)
            return

        if cur.kind == "job":
            self._tick_job(cur, now, busy)
        else:
            self._tick_once(cur, now)

    # ---- one job ----

    def _start(self, item, now):
        kind, payload = item
        if kind == "job":
            cur = _Active("job", job=payload)
            self._open(cur, "timer" if payload["kind"] == "timer"
                       else "reminder")
            cur.first_ring = now
            self.current = cur
            self._ring(cur, now)
        elif kind == "backlog":
            texts = [j["text"] for j in payload]
            cur = _Active("backlog", jobs=payload,
                          text=speech.backlog_line(texts, self.lang()))
            self._open(cur, "reminder")
            self.current = cur
            self._begin_chime(cur, now, "reminder")
        else:
            cur = _Active("notice", text=payload)
            self._open(cur, "notice")
            self.current = cur
            self._begin_speech(cur, now)

    def _open(self, cur, volume_kind):
        """Take the speaker's volume for the length of the announcement."""
        cur.saved_volume = self.speaker.get_volume()
        quiet = self._quiet_volume(volume_kind)
        if quiet is not None:
            self.speaker.set_volume(quiet)

    def _ring(self, cur, now):
        job = cur.job
        timer = job["kind"] == "timer"
        cur.rings += 1
        gap = TIMER_GAP if timer else REMINDER_GAP
        cur.next_ring = now + gap
        self._begin_chime(cur, now, "timer" if timer else "reminder")

    def _begin_chime(self, cur, now, which):
        chime = self.chimes.get(which) or Chime()
        if chime.pcm:
            self.speaker.play(chime.pcm, chime.rate)
        cur.phase = CHIME
        cur.until = now + chime.seconds

    def _begin_speech(self, cur, now):
        text = self._line(cur)
        cur.phase = SPEAK
        if text:
            self._speak(text)
            cur.until = now + estimate_seconds(text)
        else:
            cur.until = now

    def _line(self, cur):
        if cur.kind != "job":
            return cur.text
        job = cur.job
        if job["kind"] == "timer":
            # Said on the first ring only. Thirty repetitions of a sentence
            # is not an alarm, it is a punishment.
            if cur.rings > 1:
                return ""
            return speech.timer_line(job["text"], job.get("length") or 0,
                                     self.lang())
        return speech.reminder_line(job["text"], self.lang())

    def _speak(self, text):
        """Where `tts.say` goes."""
        self._say(text)

    def _tick_job(self, cur, now, busy):
        if cur.phase == CHIME and now >= cur.until:
            self._begin_speech(cur, now)
        if cur.phase == SPEAK and now >= cur.until:
            cur.phase = WINDOW
            cur.until = min(now + ACK_WINDOW, cur.next_ring)
        if cur.phase == WINDOW and now >= cur.until:
            cur.phase = WAIT

        if cur.phase != WAIT:
            return

        timer = cur.job["kind"] == "timer"
        if timer:
            if now - cur.first_ring >= TIMER_GIVE_UP:
                self._give_up(cur)
                return
        elif cur.rings >= REMINDER_RINGS:
            self._give_up(cur)
            return

        if now >= cur.next_ring and not busy and now >= self._hold_until:
            self._ring(cur, now)

    def _give_up(self, cur):
        self.scheduler.mark_missed(cur.job["id"])
        self._finish()

    # ---- the one-shot announcements ----

    def _tick_once(self, cur, now):
        if cur.phase == CHIME and now >= cur.until:
            self._begin_speech(cur, now)
        if cur.phase == SPEAK and now >= cur.until:
            self._finish()

    def _finish(self):
        cur = self.current
        self.current = None
        if cur is not None and cur.saved_volume is not None:
            self.speaker.set_volume(cur.saved_volume)
        self._hold_until = max(self._hold_until,
                               self.mono() + BETWEEN_ANNOUNCEMENTS)

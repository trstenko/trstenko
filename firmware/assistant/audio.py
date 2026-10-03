"""The ear: one thread that listens for the wake word and records what follows.

It reads the microphone in 10 millisecond chunks (160 frames, what the wake-word
detectors expect) and is the only thing that touches the mic. It does not decide
anything about the device: it posts events (`WakeWord`, `SpeechEnded`, `MicLost`,
`MicBack`) and main decides what they mean (plan 01, plan 03).

**The ring.** Every chunk goes into a two second ring, so when the wake word
fires the first words the person said are not lost: some of them were spoken
while the detector was still making up its mind. The ring is what the recording
starts from. The VAD is *not* run while waiting, which saves CPU, and is not
shown the pre-roll: the wake word itself would count as speech, and a person who
says only "Trstenko" would be answered.

**Ending a recording.** Any of: 0.7 seconds of silence after at least 0.6 seconds
of speech; 12 seconds in all; or 5 seconds with no speech at all, which is a false
wake and ends quietly with no audio, so nothing is sent to a provider. Counted
as `false_wakes`.

**Armed, not recording, after a wake.** The thread posts `WakeWord` and waits up
to a second for main to say `begin_capture`, because main may refuse (a
reminder is speaking, the device is muted). Without that handshake the thread
would record whenever it heard its name, and answers could not be undone.

**Muted.** The switch cuts the microphone's power, which the board sees as a USB
disconnect. The thread posts `MicLost` once and polls `available()` twice a
second until it is back (`MicBack`). Nothing is read while it is gone, so a
detection that was in flight when the switch went over cannot complete.

**While the device speaks.** By default the detector is paused from the start of
speech until 300ms after it, because without echo cancellation the device would
hear itself say a word like its name (`audio.barge_in`, false until the echo
test on the board passes). The ring is cleared when listening resumes, so the
device's own voice is never the front of the next recording.

`step()` handles one chunk and is what the scenarios drive; `run()` is the
thread. `close()` stops it. A thread that dies posts `WorkerDied` so the
supervisor can bring it back.
"""

import collections
import logging
import threading
import time

from .. import events as ev

log = logging.getLogger("trstenko.audio")

RATE = 16000
CHUNK_FRAMES = 160
SAMPLE_BYTES = 2
CHUNK_SECONDS = CHUNK_FRAMES / float(RATE)

# Plan 03 and Finding 13.
RING_SECONDS = 2.0
MIN_SPEECH = 0.6
SILENCE_HOLD = 0.7
MAX_UTTERANCE = 12.0
NO_SPEECH = 5.0
FOLLOW_UP = 5.0
# How long to wait for main to take up a wake word before going back to
# listening for the next one.
ARMED_FOR = 1.0
# After the device stops speaking, a breath before the detector is let loose.
AFTER_SPEECH = 0.3
MUTED_POLL = 0.5
IDLE_SLEEP = 0.005

WAITING, ARMED, CAPTURING, PAUSED, LOST = (
    "waiting", "armed", "capturing", "paused", "lost")


class AudioIn:
    def __init__(self, mic, post, detector, vad, mono=time.monotonic,
                 speaking=lambda: False, barge_in=lambda: False,
                 sleep=time.sleep):
        self.mic = mic
        self.post = post
        self.detector = detector
        self.vad = vad
        self._mono = mono
        self._sleep = sleep
        self._speaking = speaking
        self._barge_in = barge_in

        self.state = WAITING
        self.false_wakes = 0
        self.wakes = 0
        self.captures = 0
        self._ring = bytearray()
        self._ring_max = int(RING_SECONDS * RATE) * SAMPLE_BYTES
        self._armed_until = 0.0
        self._resume_at = 0.0
        self._next_poll = 0.0
        self._want = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._cap = None
        self.detections_run = 0

    # ---- called by main ----

    def begin_capture(self, turn_id, followup=False):
        """Main has taken up the wake word (or opened a follow-up window): start
        recording for `turn_id`. A follow-up has no wake word and no pre-roll,
        and waits for speech for `FOLLOW_UP` seconds."""
        with self._lock:
            self._want = (turn_id, bool(followup))

    def abort(self):
        """The turn was cancelled (mute, a button, shutdown): drop whatever is
        being recorded and go back to waiting, saying nothing."""
        with self._lock:
            self._want = None
            self._cap = None
            if self.state in (ARMED, CAPTURING):
                self.state = WAITING
                self._reset_listening()

    # ---- the thread ----

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audio-in",
                                        daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not \
                threading.current_thread():
            self._thread.join(2.0)

    def _run(self):
        try:
            while not self._stop.is_set():
                if not self.step():
                    self._sleep(IDLE_SLEEP)
        except Exception:
            if not self._stop.is_set():
                log.exception("the audio thread stopped")
        finally:
            if not self._stop.is_set():
                self.post(ev.Event(ev.WORKER_DIED, data="audio"))

    # ---- one chunk ----

    def step(self):
        """Handle at most one chunk. Returns True if there was audio."""
        now = self._mono()
        if self.state == LOST:
            if now < self._next_poll:
                return False
            self._next_poll = now + MUTED_POLL
            if not self._available():
                return False
            self._recover()
            return False

        if not self._available():
            self._lose(now)
            return False

        try:
            chunk = self.mic.read(CHUNK_FRAMES)
        except Exception:
            log.exception("the microphone could not be read")
            self._lose(now)
            return False
        if not chunk:
            return False

        self._take_request(now)
        if self.state == CAPTURING:
            self._capture(chunk, now)
            return True

        self._ring_add(chunk)
        if self.state == ARMED:
            if now >= self._armed_until:
                self.state = WAITING
            return True
        if self._paused(now):
            return True
        self.detections_run += 1
        if self.detector.process(chunk):
            self.wakes += 1
            self.state = ARMED
            self._armed_until = now + ARMED_FOR
            self.post(ev.Event(ev.WAKE_WORD, data={"at": now}))
        return True

    def _available(self):
        try:
            return bool(self.mic.available())
        except Exception:
            return False

    def _lose(self, now):
        if self.state != LOST:
            self.state = LOST
            self._cap = None
            self._next_poll = now + MUTED_POLL
            self.post(ev.Event(ev.MIC_LOST))

    def _recover(self):
        self.state = WAITING
        self._reset_listening()
        self.post(ev.Event(ev.MIC_BACK))

    def _reset_listening(self):
        self._ring = bytearray()
        try:
            self.vad.reset()
        except Exception:
            log.exception("the VAD would not reset")

    def _ring_add(self, chunk):
        self._ring += chunk
        extra = len(self._ring) - self._ring_max
        if extra > 0:
            del self._ring[:extra]

    def _paused(self, now):
        """Half-duplex: the detector sleeps while the device speaks, and for a
        moment after, unless barge-in is on."""
        if self._barge_in():
            return False
        if self._speaking():
            self.state = PAUSED
            self._resume_at = now + AFTER_SPEECH
            return True
        if self.state == PAUSED:
            if now < self._resume_at:
                return True
            self.state = WAITING
            # What is in the ring is the device's own voice.
            self._ring = bytearray()
        return False

    # ---- recording ----

    def _take_request(self, now):
        with self._lock:
            want, self._want = self._want, None
        if want is None or self.state not in (ARMED, WAITING, PAUSED):
            return
        turn_id, followup = want
        if followup:
            pre = b""
            wait = FOLLOW_UP
        else:
            pre = bytes(self._ring)
            wait = NO_SPEECH
        self._cap = {"id": turn_id, "pcm": bytearray(pre), "speech": 0.0,
                     "silence": 0.0, "elapsed": 0.0, "wait": wait,
                     "started": False}
        try:
            self.vad.reset()
        except Exception:
            log.exception("the VAD would not reset")
        self.state = CAPTURING
        self.captures += 1

    def _capture(self, chunk, now):
        cap = self._cap
        if cap is None:
            self.state = WAITING
            return
        cap["pcm"] += chunk
        span = len(chunk) / float(RATE * SAMPLE_BYTES)
        cap["elapsed"] += span
        try:
            talking = self.vad.speech(chunk)
        except Exception:
            log.exception("the VAD failed")
            talking = False
        if talking:
            cap["speech"] += span
            cap["silence"] = 0.0
            if not cap["started"]:
                cap["started"] = True
                self.post(ev.Event(ev.SPEECH_STARTED,
                                   data={"turn_id": cap["id"]}))
        elif cap["started"]:
            cap["silence"] += span

        if cap["speech"] >= MIN_SPEECH and cap["silence"] >= SILENCE_HOLD:
            return self._finish("speech")
        if cap["elapsed"] >= MAX_UTTERANCE:
            return self._finish("max")
        if cap["speech"] < MIN_SPEECH and cap["elapsed"] >= cap["wait"]:
            return self._finish("silence")

    def _finish(self, reason):
        cap, self._cap = self._cap, None
        self.state = WAITING
        pcm = bytes(cap["pcm"])
        if reason == "silence":
            # A false wake, or a follow-up nobody took: nothing worth sending.
            self.false_wakes += 1
            pcm = b""
        self.post(ev.Event(ev.SPEECH_ENDED, data={
            "turn_id": cap["id"], "pcm": pcm, "reason": reason}))
        self._ring = bytearray()
        self._resume_at = self._mono() + AFTER_SPEECH

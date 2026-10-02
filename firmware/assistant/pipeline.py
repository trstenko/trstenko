"""The voice loop as the device sees it: the real `Pipeline` and `Turn`.

`firmware/pipeline.py` fixed the shape long ago, so the face and the state
machine could be finished against a scripted turn before any key existed. This is
the real thing behind the same shape, and adds nothing to it: `poll` is true on a
wake word, `start_turn` returns a `Turn` the device polls once a frame, and the
turn's `stage` and `level` are what the face is drawn from.

Everything here runs on the main thread. The audio thread, the worker and the
speech thread post events; the handlers below turn them into stage changes, and
the turn is the one place that knows what stage a conversation is in. Every event
that belongs to a turn carries its id, and an event from a turn that is no longer
current is ignored (plan 03), which is what stops a late sentence from a cancelled
turn being spoken.

A conversation is a wake word, a recording, a reply and, if the reply ended in a
question, a follow-up recording five seconds long with no wake word: the same
`Turn`, another *exchange* (`t5.1`, `t5.2`). The events are matched by exchange.

The stages and what moves them:

    LISTENING   until SpeechEnded                    (or a watchdog)
    THINKING    until the first audio, or TurnDone/TurnFailed
    SPEAKING    until SpeakingDone, then the follow-up window or DONE
    ERROR       TurnFailed, a speech or worker death, a watchdog

The watchdogs exist because a thread can die without saying so. A turn must end
somehow, and "the face thinks forever" is the failure that looks like a broken
product.

While a reminder is going off its acknowledgement window is listened to without a
wake word (`tick`): "stop" and "snooze for ten minutes" go to the worker as an
`ack` job, which tries only the local commands and never starts a conversation.
"""

import itertools
import logging

from .. import events as ev
from .. import pipeline as base

log = logging.getLogger("trstenko.assistant")

# A recording is capped at 12s and the model has 8s; these are the margins past
# which a silent thread is assumed dead.
LISTEN_WATCHDOG = 20.0
THINK_WATCHDOG = 14.0
SPEAK_WATCHDOG = 12.0


class AssistantTurn(base.Turn):
    def __init__(self, pipeline, number, now):
        super().__init__()
        self.pipeline = pipeline
        self.id = "t%d" % number
        self.exchange = 1
        self.sentences = []
        self.cancelled = False
        self._follow = False
        self._worker_done = False
        self._started_speaking = False
        self._unspoken = False
        self._entered = now
        self.stage = base.LISTENING
        pipeline.audio.begin_capture(self.xid)

    @property
    def xid(self):
        return "%s.%d" % (self.id, self.exchange)

    # ---- the device asks ----

    def poll(self, now):
        if self.finished:
            return
        age = now - self._entered
        if self.stage == base.LISTENING and age > LISTEN_WATCHDOG:
            log.error("no recording came back from the audio thread")
            self._end(base.ERROR, "the microphone stopped answering", True)
        elif self.stage == base.THINKING and age > THINK_WATCHDOG:
            log.error("the assistant did not answer in time")
            self._end(base.ERROR, "that took too long", True)
        elif (self.stage == base.THINKING and self.sentences
              and age > SPEAK_WATCHDOG and not self._started_speaking):
            log.error("nothing was spoken")
            self._end(base.ERROR, "the voice did not start", True)

    def level(self):
        hal = self.pipeline.hal
        if self.stage == base.LISTENING:
            return hal.mic.level()
        if self.stage == base.SPEAKING:
            return hal.speaker.level()
        return 0.0

    def cancel(self):
        """The user muted, pressed a button, or walked away."""
        if self.finished:
            return
        self.cancelled = True
        self.pipeline.worker.cancel(self.xid)
        self.pipeline.tts.cancel()
        self.pipeline.audio.abort()
        self.stage = base.DONE

    # ---- what the pipeline tells it ----

    def _end(self, stage, error="", stop_everything=False):
        if stop_everything:
            self.pipeline.worker.cancel(self.xid)
            self.pipeline.tts.cancel()
            self.pipeline.audio.abort()
        self.stage = stage
        self.error = error

    def _enter(self, stage, now=None):
        self.stage = stage
        self._entered = self.pipeline.mono() if now is None else now

    def speech_ended(self, data):
        if data.get("reason") == "silence" or not data.get("pcm"):
            self.stage = base.DONE          # a false wake, or nobody answered
            return
        self._enter(base.THINKING)
        from .worker import Job
        if not self.pipeline.worker.submit(Job(self.xid, data["pcm"])):
            self._end(base.ERROR, "the assistant is busy", True)

    def sentence(self, text):
        self.sentences.append(text)
        self.reply = " ".join(self.sentences)
        if not self.pipeline.tts.say(self.xid, text):
            # No voice, or Piper is down. The reply exists and nobody can hear
            # it, which the face says (plan 03: error face only).
            self._unspoken = True
            log.warning("a sentence could not be spoken: the voice is down")

    def worker_done(self, spoke, follow_up):
        self._worker_done = True
        self._follow = bool(follow_up)
        if spoke and self.sentences and not self._unspoken:
            self.pipeline.tts.finish(self.xid)
        elif self._unspoken:
            self._end(base.ERROR, "the voice is not working")
        else:
            self.stage = base.DONE

    def failed(self, kind):
        # The line that explains it was posted just before this and is being
        # spoken; the face carries on in the error state meanwhile.
        self.pipeline.tts.finish(self.xid)
        self._end(base.ERROR, "the assistant could not answer (%s)" % kind)

    def speaking_started(self):
        if self.stage == base.THINKING:
            self._started_speaking = True
            self._enter(base.SPEAKING)

    def speaking_done(self):
        if self._follow:
            # The reply asked something. Listen for the answer without the wake
            # word, in the same conversation.
            self.exchange += 1
            self.sentences = []
            self._follow = False
            self._worker_done = False
            self._started_speaking = False
            self._enter(base.LISTENING)
            self.pipeline.audio.begin_capture(self.xid, followup=True)
        else:
            self.stage = base.DONE


class AssistantPipeline(base.Pipeline):
    def __init__(self, hal, audio, worker, tts, mono):
        self.hal = hal
        self.audio = audio
        self.worker = worker
        self.tts = tts
        self.mono = mono
        self.woke = False
        self.turn = None
        self._numbers = itertools.count(1)
        self._acks = itertools.count(1)
        self._ack_armed = None
        self.device = None
        self.wakes = 0

    # ---- the device's interface ----

    def poll(self, now):
        woke, self.woke = self.woke, False
        return woke

    def start_turn(self, now):
        self.turn = AssistantTurn(self, next(self._numbers), now)
        return self.turn

    def close(self):
        pass

    def trigger(self):
        """A wake word without the word: a button in the emulator's window."""
        self.woke = True

    # ---- the events ----

    def attach(self, device):
        """Register for everything the other threads post, and a step for the
        acknowledgement window."""
        self.device = device
        on = device.on
        on(ev.WAKE_WORD, self._on_wake)
        on(ev.SPEECH_ENDED, self._on_speech_ended)
        on(ev.SENTENCE, self._on_sentence)
        on(ev.TURN_DONE, self._on_turn_done)
        on(ev.TURN_FAILED, self._on_turn_failed)
        on(ev.SPEAKING_STARTED, self._on_speaking_started)
        on(ev.SPEAKING_DONE, self._on_speaking_done)
        on(ev.TTS_DIED, self._on_tts_died)
        on(ev.MIC_LOST, lambda e: device.log("microphone lost"))
        on(ev.MIC_BACK, lambda e: device.log("microphone back"))
        device.add_step("assistant", self.tick)

    def _mine(self, event):
        turn = self.turn
        if turn is None or turn.finished:
            return None
        data = event.data or {}
        return turn if data.get("turn_id") == turn.xid else None

    def _on_wake(self, event):
        self.wakes += 1
        if self.turn is not None and not self.turn.finished:
            return            # a conversation is already going
        self.woke = True

    def _on_speech_ended(self, event):
        data = event.data or {}
        turn_id = str(data.get("turn_id", ""))
        if turn_id.startswith("ack-"):
            if data.get("reason") != "silence" and data.get("pcm"):
                from .worker import Job
                self.worker.submit(Job(turn_id, data["pcm"], "ack"))
            return
        turn = self._mine(event)
        if turn is not None:
            turn.speech_ended(data)

    def _on_sentence(self, event):
        data = event.data or {}
        turn = self._mine(event)
        if turn is not None:
            turn.sentence(data.get("text", ""))
        elif str(data.get("turn_id", "")).startswith("ack-"):
            self.tts.say_line(data.get("text", ""))

    def _on_turn_done(self, event):
        turn = self._mine(event)
        if turn is not None:
            data = event.data
            turn.worker_done(data.get("spoke", 0), data.get("follow_up"))

    def _on_turn_failed(self, event):
        turn = self._mine(event)
        if turn is not None:
            turn.failed((event.data or {}).get("kind", "failed"))

    def _on_speaking_started(self, event):
        turn = self._mine(event)
        if turn is not None:
            turn.speaking_started()

    def _on_speaking_done(self, event):
        turn = self._mine(event)
        if turn is not None:
            turn.speaking_done()

    def _on_tts_died(self, event):
        reason = (event.data or {}).get("reason", "")
        self.device.log("the voice stopped: %s" % reason)
        turn = self.turn
        if turn is not None and not turn.finished and turn.sentences:
            turn._end(base.ERROR, "the voice stopped", False)

    # ---- the acknowledgement window ----

    def tick(self, now):
        """Listen without a wake word while something is going off."""
        announcer = getattr(self.device, "announcer", None)
        if announcer is None or not announcer.in_window:
            if self._ack_armed is not None:
                # The window closed (the next ring, or the end): stop
                # listening for "stop" so the wake word is not held back, and
                # so the next window can open.
                self._ack_armed = None
                if self.turn is None or self.turn.finished:
                    self.audio.abort()
            return
        # One capture per window. A timer rings every ten seconds, and each
        # ring opens a new window; keying on the job alone left every ring
        # after the first impossible to stop by voice.
        cur = announcer.current
        ringing = announcer.ringing
        key = (ringing if ringing is not None else -1,
               cur.rings if cur is not None else 0)
        if self._ack_armed == key:
            return
        self._ack_armed = key
        self.audio.begin_capture("ack-%d" % next(self._acks), followup=True)

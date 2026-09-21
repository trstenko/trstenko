"""A conversation with nobody, on a timer -- or with you, through the mic.

Stands in for the Phase 2 pipeline (Porcupine, cloud STT, the model call,
Piper) so the device loop and the face can be driven end to end. Durations are
the ones the research expects on the real board, so the emulator shows the
latency the device will have rather than an instant reply.

With the audio bridge on a real microphone it stops being a timer:

- Listening ends when you stop talking, not when a clock runs out.
- The face animates from the level of the room.
- The reply is what you just said, played back out of the speakers.

That last one is a parrot standing in for TTS, and it is the most useful thing
the emulator does with audio: it proves the whole path -- microphone in, 16kHz
mono chunks through the HAL, buffered across a turn, back out through the
speaker the firmware thinks it has -- with nothing faked in the middle except
the words.
"""

import math
import random

from firmware import pipeline as base

from .audio import RATE, SAMPLE_BYTES

# Cloud STT round trip, then the model, per docs/research/software-stack.md.
STT_SECONDS = (0.6, 1.2)
THINK_SECONDS = (1.4, 3.0)
UTTERANCE_SECONDS = (1.8, 3.2)
REPLY_SECONDS = (2.0, 4.5)

# End-of-utterance, when there is a real microphone. Speech has gaps in it, so
# a hold much under half a second cuts people off mid-sentence.
SILENCE_LEVEL = 0.06
SILENCE_HOLD = 0.7
MIN_UTTERANCE = 0.6
MAX_UTTERANCE = 12.0

REPLIES = [
    ("what's the time", "It's twenty past four."),
    ("remind me to call mum at six", "I'll remind you at six."),
    ("what did I ask you yesterday", "You asked me to save the wifi password."),
    ("turn the volume down", "Done."),
]


class ScriptedTurn(base.Turn):
    def __init__(self, now, speaker, fail_at=None, rng=None, bridge=None):
        super().__init__()
        self.rng = rng or random.Random()
        self.speaker = speaker
        self.fail_at = fail_at
        self.bridge = bridge
        self.live = bool(bridge is not None and bridge.live_input)

        self.started = now
        self.t0 = now
        self._silent_since = None
        self._captured = b""

        self.transcript, self.reply = self.rng.choice(REPLIES)
        self._listen_for = self._pick(UTTERANCE_SECONDS)
        self._think_for = self._pick(STT_SECONDS) + self._pick(THINK_SECONDS)
        self._speak_for = self._pick(REPLY_SECONDS)
        self._amp = 0.0

        if self.live:
            self.bridge.start_recording()

    def _pick(self, span):
        return self.rng.uniform(*span)

    # ---- stages ----

    def poll(self, now):
        if self.finished:
            return
        elapsed = now - self.t0

        if self.stage == base.LISTENING:
            self._listening(now, elapsed)
        elif self.stage == base.THINKING:
            self._thinking(now, elapsed)
        elif self.stage == base.SPEAKING:
            self._speaking(now, elapsed)

    def _listening(self, now, elapsed):
        self._amp = (self.bridge.mic_level() if self.live
                     else _speech(now - self.started))

        if not self.live:
            if elapsed >= self._listen_for:
                self._done_listening(now)
            return

        # Real audio: wait for the room to go quiet rather than for a clock.
        if self._amp >= SILENCE_LEVEL:
            self._silent_since = None
        elif self._silent_since is None:
            self._silent_since = now

        quiet = (self._silent_since is not None
                 and now - self._silent_since >= SILENCE_HOLD)
        if elapsed >= MAX_UTTERANCE or (elapsed >= MIN_UTTERANCE and quiet):
            self._done_listening(now)

    def _done_listening(self, now):
        """The utterance is over. Stop recording here rather than when the
        reply starts, or the thinking seconds end up on the end of it -- STT
        would be sent the pause as well as the question."""
        if self.live:
            self._captured = self.bridge.stop_recording()
        self._enter(base.THINKING, now)

    def _thinking(self, now, elapsed):
        self._amp = 0.0
        if self.fail_at == base.THINKING:
            self.stage = base.ERROR
            self.error = "the model did not answer"
            return
        if elapsed >= self._think_for:
            self._speak(now)

    def _speak(self, now):
        self._enter(base.SPEAKING, now)

        pcm = self._captured
        if pcm:
            # The parrot: say back what was said. Piper goes here later, and
            # nothing else about this turn changes when it does.
            self.transcript = "(%0.1fs of audio)" % (
                len(pcm) / float(RATE * SAMPLE_BYTES))
            self.reply = "played back what it heard"
            self._speak_for = len(pcm) / float(RATE * SAMPLE_BYTES)
            self.speaker.play(pcm, RATE)
        else:
            self.speaker.play(b"\x00" * int(22050 * 2 * self._speak_for))

    def _speaking(self, now, elapsed):
        if self.live:
            # What is actually leaving the speakers, so the mouth is driven by
            # the audio rather than by an envelope that guesses at it.
            self._amp = self.bridge.speaker_level()
        else:
            self._amp = _speech(now - self.started, rate=7.5)
            self.speaker.set_level(self._amp)

        if elapsed >= self._speak_for:
            self.speaker.stop()
            self.stage = base.DONE
            self._amp = 0.0

    def _enter(self, stage, now):
        self.stage = stage
        self.t0 = now

    # ---- what the device asks ----

    def level(self):
        return self._amp

    def cancel(self):
        self.speaker.stop()
        if self.live:
            self.bridge.stop_recording()
        super().cancel()


class ScriptedPipeline(base.Pipeline):
    """Fires when the window says so. Nothing here listens for a wake word --
    that is Phase 2, and a timer that woke the device on its own would make
    every other behaviour hard to watch."""

    def __init__(self, speaker, seed=None, bridge=None):
        self.speaker = speaker
        self.rng = random.Random(seed)
        self.bridge = bridge
        self.triggered = False
        self.fail_next = None

    def trigger(self):
        self.triggered = True

    def poll(self, now):
        fired, self.triggered = self.triggered, False
        return fired

    def start_turn(self, now):
        fail_at, self.fail_next = self.fail_next, None
        return ScriptedTurn(now, self.speaker, fail_at=fail_at, rng=self.rng,
                            bridge=self.bridge)


def _speech(t, rate=9.0):
    """A syllable rate inside a phrase envelope. Crude, but it has the gaps
    and the peaks that make the face read as speech rather than as a meter."""
    phrase = 0.5 + 0.5 * math.sin(t * 0.9)
    syllable = abs(math.sin(t * rate)) * abs(math.sin(t * rate * 0.41))
    return max(0.0, min(1.0, phrase * syllable * 1.4))

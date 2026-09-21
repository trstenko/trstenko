"""The voice loop, as the device sees it.

Wake word, speech to text, the model call, tools, and text to speech are not
written yet -- they are Phase 2 in `docs/prototype-plan.md`. What is fixed is
the shape they have to present to the device, and that is here, so the face and
the state machine can be finished and tested against the emulator's scripted
pipeline before a single API key exists.

The contract is polled, not callback-driven, and never blocks. The device loop
runs at 30Hz to draw a face; a `requests.post` to a model provider on that
thread drops two seconds of frames. A real implementation does its work on a
thread and reports progress through `stage` and `level`.
"""

WAITING = "waiting"
LISTENING = "listening"
THINKING = "thinking"
SPEAKING = "speaking"
DONE = "done"
ERROR = "error"


class Turn:
    """One exchange: from the wake word to the end of the spoken reply."""

    def __init__(self):
        self.stage = LISTENING
        self.transcript = ""
        self.reply = ""
        self.error = ""

    @property
    def finished(self):
        return self.stage in (DONE, ERROR)

    def poll(self, now):
        """Advance. Called once a frame; must return promptly."""
        raise NotImplementedError

    def level(self):
        """0.0 to 1.0 for the face: mic level while listening, playback
        amplitude while speaking. What makes the face look like it is hearing
        and talking rather than idling at a different size."""
        return 0.0

    def cancel(self):
        """The user muted, pressed a button, or walked away."""
        self.stage = DONE


class Pipeline:
    """Waits for the wake word, then hands back a Turn."""

    def poll(self, now):
        """Return True on the frame the wake word fires."""
        raise NotImplementedError

    def start_turn(self, now):
        raise NotImplementedError

    def close(self):
        pass

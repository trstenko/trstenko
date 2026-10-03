"""How every other thread talks to the main one.

Only the main thread changes device state (plan 01). Audio, the assistant
worker, speech synthesis and the web server each run somewhere else, and each
reports by posting an `Event` here, or asks for something with a `Command` and
waits for the answer. The main thread drains this once a frame.

Why not `queue.Queue`: the loop has to sleep until the next frame *or* until
something arrives, whichever is first, and `Queue.get` takes the item off to
tell you it is there. A deque and a condition variable give a wait that does
not consume anything, so the loop can wake the moment a wake word or a web
request lands and still decide for itself how many to handle this frame.
Without that, either the loop polls at a fixed rate (a button press is a frame
late at 2Hz) or it spins.

This module is also the one list of event kinds. Services in different threads
agree on a name here instead of on a string they each spelled themselves.
"""

import collections
import threading
import time

# ---- audio-in thread ----
WAKE_WORD = "WakeWord"
SPEECH_STARTED = "SpeechStarted"
SPEECH_ENDED = "SpeechEnded"            # data: the PCM of the utterance
MIC_LOST = "MicLost"
MIC_BACK = "MicBack"

# ---- assistant worker ----
TURN_PROGRESS = "TurnProgress"          # data: the stage
SENTENCE = "Sentence"                   # data: one sentence to speak
TURN_DONE = "TurnDone"
TURN_FAILED = "TurnFailed"              # data: the kind of failure

# ---- text to speech ----
SPEAKING_STARTED = "SpeakingStarted"
SPEAKING_DONE = "SpeakingDone"
TTS_DIED = "TtsDied"
# Polled from the speaker, never queued: ten levels a second would be most of
# the traffic and the face only wants the latest one. Named here so nobody
# invents a second spelling for it.
SPEAKING_LEVEL = "SpeakingLevel"

# ---- the scheduler, run by main itself ----
DUE = "Due"                             # data: the job

# ---- system ----
CLOCK_SYNCED = "ClockSynced"
NETWORK_CHANGED = "NetworkChanged"      # data: online or not
LOW_VOLTAGE = "LowVoltage"
HOT = "Hot"                             # data: the temperature in degrees C
SHUTDOWN = "Shutdown"
# Posted from the `finally` of a worker thread, so main can restart it.
WORKER_DIED = "WorkerDied"              # data: the worker's name
# The setup worker finished a slow step (firmware/netsetup.py).
NET_SETUP = "NetSetup"                  # data: what happened, and its result

KINDS = frozenset((
    WAKE_WORD, SPEECH_STARTED, SPEECH_ENDED, MIC_LOST, MIC_BACK,
    TURN_PROGRESS, SENTENCE, TURN_DONE, TURN_FAILED,
    SPEAKING_STARTED, SPEAKING_DONE, TTS_DIED, SPEAKING_LEVEL,
    DUE,
    CLOCK_SYNCED, NETWORK_CHANGED, LOW_VOLTAGE, HOT, SHUTDOWN, WORKER_DIED,
    NET_SETUP,
))

# How long a web handler waits for the main thread before telling the page the
# device is busy (plan 01).
COMMAND_TIMEOUT = 2.0

# What one tick will handle. A burst must not starve the drawing.
DRAIN_LIMIT = 50


class Event:
    """Something that happened. Small, and immutable by convention.

    Events from a turn carry its `turn_id` in `data`, so main can throw away
    the late ones from a turn that was cancelled (plan 03).
    """

    __slots__ = ("kind", "at", "data")

    def __init__(self, kind, at=None, data=None):
        self.kind = kind
        self.at = time.monotonic() if at is None else at
        self.data = data

    def __repr__(self):
        return "Event(%s)" % self.kind


# Why a command was refused, so the web layer can pick a status code without
# reading the sentence: a bad request, something that is not there, something
# the current state forbids, or something that cannot be done just now.
INVALID = "invalid"
NOT_FOUND = "not_found"
CONFLICT = "conflict"
UNAVAILABLE = "unavailable"


class CommandError(Exception):
    """The main thread looked at the command and refused it. The message is
    written for a person, because the page shows it as it is."""

    def __init__(self, message, kind=INVALID):
        super().__init__(message)
        self.kind = kind


class CommandTimeout(Exception):
    """Main did not answer in time. To a web handler this is a 503: the device
    is busy, not broken."""


class Command:
    """A request to change something, with a slot for the answer.

    The web server needs "set the volume, then show me the new volume". It
    posts one of these and blocks on `wait`; main runs it during a tick and
    fills in `reply` or `fail`.
    """

    __slots__ = ("name", "args", "result", "error", "kind", "_done")

    def __init__(self, name, args=None):
        self.name = name
        self.args = args
        self.result = None
        self.error = None
        self.kind = INVALID
        self._done = threading.Event()

    def reply(self, result=None):
        self.result = result
        self._done.set()

    def fail(self, message, kind=INVALID):
        self.error = str(message)
        self.kind = kind
        self._done.set()

    @property
    def answered(self):
        return self._done.is_set()

    def wait(self, timeout=COMMAND_TIMEOUT):
        if not self._done.wait(timeout):
            raise CommandTimeout(self.name)
        if self.error is not None:
            raise CommandError(self.error, self.kind)
        return self.result

    def __repr__(self):
        return "Command(%s)" % self.name


class EventQueue:
    """Events and commands for the main thread, with a wait that can be woken."""

    def __init__(self):
        self._items = collections.deque()
        self._cv = threading.Condition()

    def post(self, item):
        """Any thread. Takes an `Event` or a `Command`."""
        with self._cv:
            self._items.append(item)
            self._cv.notify()

    def wait(self, timeout):
        """Main thread only. Returns when there is something to handle, or
        after `timeout`; either way it takes nothing off the queue."""
        with self._cv:
            if not self._items and timeout > 0:
                self._cv.wait(timeout)

    def drain(self, limit=DRAIN_LIMIT):
        """Main thread only. At most `limit` items, oldest first; the rest wait
        for the next frame."""
        with self._cv:
            count = min(limit, len(self._items))
            return [self._items.popleft() for _ in range(count)]

    def __len__(self):
        with self._cv:
            return len(self._items)

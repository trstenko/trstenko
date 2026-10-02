"""The conversation so far, kept in RAM.

A session is a burst of turns: each starts within five minutes of the last
(plan 03, Finding 5). After that the conversation starts again from nothing, and
anything worth keeping a day later is in the memories, not in here. Nothing
here is written to disk, so a reboot is a fresh start and no transcript outlives
the device being switched off.

The history is **append-only within a session**. Editing or reordering an earlier
message breaks the provider's prefix cache (xAI's docs say so in as many words),
and trimming is done at the session boundary instead: after eight exchanges, or
when the text has grown past a size cap, the next turn starts a new session.

What is kept is the conversation as the provider saw it, including the model's
tool calls and the tool results, because a follow-up that refers to "that
reminder" needs them. What is *not* kept is audio: a user message that was audio
is replaced by its transcript when the turn ends, since half a megabyte of
base64 in the history would be sent again on every later request.
"""

import time
import uuid

# Plan 03 and llm-workflow.md, Finding 5.
EXPIRY = 5 * 60.0
MAX_EXCHANGES = 8
# A backstop. Tool results are cut at 1,000 characters and replies at a few
# hundred, so eight exchanges stay well under this; it is here for the day one
# does not.
MAX_CHARS = 32_000


def _size(message):
    content = message.get("content")
    size = len(content) if isinstance(content, str) else 0
    for call in message.get("tool_calls") or ():
        size += len(str(call.get("function", {}).get("arguments", "")))
    return size


class Session:
    def __init__(self, mono=time.monotonic, expiry=EXPIRY,
                 max_exchanges=MAX_EXCHANGES, max_chars=MAX_CHARS):
        self._mono = mono
        self.expiry = expiry
        self.max_exchanges = max_exchanges
        self.max_chars = max_chars
        self._history = []
        self._exchanges = 0
        self._last = None
        self.id = ""
        self.reset()

    def reset(self):
        self._history = []
        self._exchanges = 0
        self._last = None
        # Also what a provider that caches by conversation (xAI) is told.
        self.id = uuid.uuid4().hex[:12]

    @property
    def exchanges(self):
        return self._exchanges

    @property
    def active(self):
        return (self._last is not None
                and self._mono() - self._last <= self.expiry)

    def start_turn(self):
        """The history to send with the next request. Starts a new session
        first if the last one has run out of time or room."""
        if self._last is not None and not self.active:
            self.reset()
        elif (self._exchanges >= self.max_exchanges
              or sum(_size(m) for m in self._history) > self.max_chars):
            self.reset()
        return list(self._history)

    def end_turn(self, user_text, produced):
        """Keep a finished exchange: what the user said, as text, and the
        messages the model and the tools produced."""
        self._history.append({"role": "user", "content": user_text})
        self._history.extend(produced)
        self._exchanges += 1
        self._last = self._mono()

    def touch(self):
        """A turn that ended with nothing to keep still counts as activity, so
        a follow-up window does not find the session expired."""
        self._last = self._mono()

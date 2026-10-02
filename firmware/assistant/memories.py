"""What Trstenko remembers about its owner, kept on the device.

A memory is one short sentence the owner asked it to keep ("My sister is called
Ana."), in the `memories` table (plan 06). They go into the prompt on every
request while they fit (`for_prompt`), so the model can use what it knows
without being asked and without a round trip, and `search` finds the rest once
there are more than fit.

This module is where the hygiene lives, because a memory is text that anyone in
the room can cause to be written and that is then fed back to a model on every
later turn. That makes it an injection channel, and the defences are layered:

- **One line, plain text, short.** No newlines or control characters, so a
  memory cannot pose as a new line of the prompt, and a length cap, so it
  cannot carry a script.
- **Nothing key-shaped.** A password said aloud must not be written down and
  then sent to a provider on every request (`storage.redact`).
- **A limit on how many**, with a sentence the owner hears when it is reached,
  rather than a memory that grows without end and a prompt that grows with it.
- **Duplicates are not stored twice.** "Remember my sister is Ana" said three
  times is one memory.
- And, in `prompt.py`, **they are presented as data, in quotes, and never as
  instructions.** Nothing here can make that true on its own.

Who writes: the main thread, through a `Command` (plan 06, single writer), for
saving and forgetting. The worker thread reads them for the prompt and for
search, and writes only `last_used_at`, at most once an hour per memory, to
spare the card. Each thread uses its own SQLite connection (`Database.conn`).
"""

import re
import sqlite3
import time
import unicodedata

from ..storage.redact import redact

CATEGORIES = ("person", "preference", "fact", "other")
DEFAULT_CATEGORY = "fact"

# A few dozen short sentences is a lot to say to a desk robot. The limit is
# there to stop a runaway, not to be met.
MAX_MEMORIES = 200
MAX_CHARS = 200
MIN_CHARS = 3
# Plan 06: last_used_at is written at most this often per memory.
TOUCH_EVERY = 3600.0
# What a memory costs in the prompt besides its words: the bullet, the quotes
# and the newline.
LINE_OVERHEAD_TOKENS = 3
CHARS_PER_TOKEN = 4

SEARCH_LIMIT = 5

# Reasons a memory is refused, for the caller to turn into a sentence.
EMPTY, TOO_LONG, BAD_TEXT, SECRET, FULL, NOT_FOUND = (
    "empty", "too_long", "bad_text", "secret", "full", "not_found")


class MemoryRefused(Exception):
    """`code` is one of the constants above; the message is for a person."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _fold(text):
    """Lower case, accents off, punctuation gone: what two spellings of the
    same sentence have in common."""
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def clean(text):
    """The text as it will be stored, or `MemoryRefused`."""
    if not isinstance(text, str):
        raise MemoryRefused(BAD_TEXT, "A memory has to be words.")
    # One memory is one line: a newline or a tab becomes a space, so a memory
    # cannot start what looks like a new line of the prompt.
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = " ".join(text.split())
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise MemoryRefused(BAD_TEXT, "A memory cannot contain control "
                            "characters.")
    if len(text) < MIN_CHARS:
        raise MemoryRefused(EMPTY, "There is nothing to remember in that.")
    if len(text) > MAX_CHARS:
        raise MemoryRefused(TOO_LONG, "Keep a memory under %d characters."
                            % MAX_CHARS)
    if redact(text) != text:
        raise MemoryRefused(SECRET, "That looks like a password or a key, "
                            "which is not kept.")
    return text


def _row(row):
    return {"id": row["id"], "text": row["text"], "category": row["category"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
            "pinned": bool(row["pinned"]), "source": row["source"]}


class MemoryStore:
    def __init__(self, db, wall=time.time, max_count=MAX_MEMORIES):
        self.db = db
        self.wall = wall
        self.max_count = max_count

    def _conn(self):
        return self.db.conn()

    # ---- writing (main thread) ----

    def add(self, text, category=DEFAULT_CATEGORY, source="voice",
            pinned=False):
        """Keep a memory. Returns it, with `already` true when the owner had
        said it before and nothing was stored."""
        text = clean(text)
        if category not in CATEGORIES:
            category = DEFAULT_CATEGORY
        conn = self._conn()
        folded = _fold(text)
        rows = conn.execute("SELECT * FROM memories").fetchall()
        for row in rows:
            if _fold(row["text"]) == folded:
                out = _row(row)
                out["already"] = True
                return out
        if len(rows) >= self.max_count:
            raise MemoryRefused(FULL, "There are already %d memories; forget "
                                "some first." % self.max_count)
        cur = conn.execute(
            "INSERT INTO memories (text, category, created_at, pinned, "
            "source) VALUES (?, ?, ?, ?, ?)",
            (text, category, self.wall(), int(bool(pinned)), source))
        out = _row(conn.execute("SELECT * FROM memories WHERE id = ?",
                                (cur.lastrowid,)).fetchone())
        out["already"] = False
        return out

    def delete(self, memory_id):
        row = self.get(memory_id)
        if row is None:
            raise MemoryRefused(NOT_FOUND, "There is no memory number %s."
                                % memory_id)
        self._conn().execute("DELETE FROM memories WHERE id = ?",
                             (row["id"],))
        return row

    def pin(self, memory_id, pinned=True):
        row = self.get(memory_id)
        if row is None:
            raise MemoryRefused(NOT_FOUND, "There is no memory number %s."
                                % memory_id)
        self._conn().execute("UPDATE memories SET pinned = ? WHERE id = ?",
                             (int(bool(pinned)), row["id"]))
        return self.get(memory_id)

    def touch(self, ids):
        """Note that these were used. Any thread; once an hour at most for
        each, so a card is not written for every answer."""
        ids = [i for i in ids if isinstance(i, int)]
        if not ids:
            return
        now = self.wall()
        try:
            self._conn().executemany(
                "UPDATE memories SET last_used_at = ? WHERE id = ? AND "
                "(last_used_at IS NULL OR last_used_at < ?)",
                [(now, i, now - TOUCH_EVERY) for i in ids])
        except sqlite3.Error:
            # Bookkeeping, never worth failing a turn for.
            pass

    # ---- reading (any thread) ----

    def get(self, memory_id):
        if isinstance(memory_id, bool) or not isinstance(memory_id, int):
            return None
        row = self._conn().execute("SELECT * FROM memories WHERE id = ?",
                                   (memory_id,)).fetchone()
        return _row(row) if row else None

    def count(self):
        return self._conn().execute(
            "SELECT COUNT(*) FROM memories").fetchone()[0]

    def list(self, limit=100, offset=0):
        rows = self._conn().execute(
            "SELECT * FROM memories ORDER BY pinned DESC, created_at DESC, "
            "id DESC LIMIT ? OFFSET ?", (int(limit), int(offset))).fetchall()
        return [_row(r) for r in rows]

    def search(self, query, limit=SEARCH_LIMIT):
        """The memories that share words with `query`, best first. A word
        counts when it appears inside one of the memory's words, so "sister"
        finds "sisters", and accents do not matter."""
        words = [w for w in _fold(query or "").split() if len(w) >= 2]
        rows = self._conn().execute(
            "SELECT * FROM memories ORDER BY created_at DESC, id DESC"
        ).fetchall()
        scored = []
        for row in rows:
            haystack = _fold(row["text"] + " " + row["category"])
            score = sum(1 for w in words if w in haystack)
            if score or not words:
                scored.append((score, row))
        scored.sort(key=lambda pair: -pair[0])
        return [_row(row) for _, row in scored[:int(limit)]]

    def for_prompt(self, token_budget):
        """The memories to put in the prompt: pinned first, then newest, until
        the budget is spent (plan 06: `memories_in_prompt_tokens`). Says
        whether there were more than fit, so the prompt can mention search."""
        if token_budget <= 0:
            return {"items": [], "more": self.count() > 0}
        rows = self._conn().execute(
            "SELECT * FROM memories ORDER BY pinned DESC, created_at DESC, "
            "id DESC").fetchall()
        items, used = [], 0
        for row in rows:
            cost = len(row["text"]) // CHARS_PER_TOKEN + LINE_OVERHEAD_TOKENS
            if used + cost > token_budget:
                return {"items": items, "more": True}
            used += cost
            items.append(_row(row))
        return {"items": items, "more": False}

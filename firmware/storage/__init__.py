"""What the device keeps: one SQLite file, a settings file and a secrets file.

    db = Database(os.path.join(data_root, "trstenko.db"))
    migrate(db)
    db.conn().execute(...)

SQLite is opened in WAL mode with `synchronous=NORMAL`. That is safe against
the application crashing, and on a power cut it can lose the last few
transactions but cannot corrupt the file -- the right trade for a card that
wears out (plan 06). `busy_timeout` covers the moments two threads want to
write at once.

Each thread that needs the database gets a connection of its own and keeps it.
Sharing one connection across threads is what `check_same_thread` exists to
stop, and wrapping it in a lock would put the web server in the way of the
frame loop.
"""

import os
import sqlite3
import threading
import time

from .redact import RedactFilter
from .redact import redact as _redact

DB_NAME = "trstenko.db"
BUSY_TIMEOUT_MS = 2000

# `events_log` is what the page shows the owner: errors, rate limits,
# fallbacks, budget warnings. The journal is for developers and lives in RAM.
EVENTS_KEPT = 500

__all__ = ["Database", "open_db", "log_event", "RedactFilter", "DB_NAME",
           "EVENTS_KEPT"]


def open_db(path):
    """A connection configured for the calling thread. In autocommit mode: the
    callers that need a transaction say BEGIN, which makes it visible in the
    code instead of being something the driver does behind it."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000.0,
                           isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS)
    return conn


class Database:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all = []
        self.closed = False

    def conn(self):
        """This thread's connection, opened on first use."""
        if self.closed:
            raise sqlite3.ProgrammingError("the database is closed")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = open_db(self.path)
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    def checkpoint(self):
        self.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self):
        """Fold the WAL back into the file, then close every connection. With
        the WAL left behind, a copy of just `trstenko.db` would be missing
        the latest writes."""
        if self.closed:
            return
        try:
            self.checkpoint()
        except sqlite3.Error:
            pass
        self.closed = True
        with self._lock:
            conns, self._all = self._all, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()


def log_event(conn, kind, detail="", at=None):
    """Something the owner should be able to see on the page, kept to the last
    `EVENTS_KEPT`. The detail is redacted here, not trusted to the caller."""
    at = time.time() if at is None else at
    conn.execute("INSERT INTO events_log (at, kind, detail) VALUES (?, ?, ?)",
                 (at, kind, _redact(str(detail))))
    conn.execute(
        "DELETE FROM events_log WHERE id <= "
        "(SELECT id FROM events_log ORDER BY id DESC LIMIT 1 OFFSET ?)",
        (EVENTS_KEPT,))

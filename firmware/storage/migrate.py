"""Bringing the database up to the schema this release expects.

Migrations are numbered `.sql` files in `migrations/`, applied in order inside
one transaction: either all the pending ones land and the version moves, or
none do. A power cut or an error half-way leaves the database as it was.

The rules that make this safe across the update system's automatic rollbacks
(`updates/boot.py`):

1. **Migrations only add.** If release 0.6 migrates the database and then rolls
   back to 0.5, 0.5 has to open it. `tools/check_layout.py` refuses a migration
   containing DROP or RENAME.
2. **A database newer than this release is left alone.** That is the rolled-back
   case, and with rule 1 it works: the old code does not know the new tables
   and does not need to.
3. **Back up first.** The copy goes to `backups/trstenko-v<schema>.db`, the
   last three kept. It is there for the day a migration has to destroy
   something, which is not today. It is taken with SQLite's own backup call,
   because copying the file of a WAL database gives you a torn copy.
"""

import logging
import os
import re
import sqlite3

log = logging.getLogger("trstenko.storage")

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")
KEEP_BACKUPS = 3

_NAME = re.compile(r"^(\d+)_.*\.sql$")


class MigrationError(Exception):
    pass


def available(migrations_dir=None):
    """[(version, path)], oldest first."""
    directory = migrations_dir or MIGRATIONS_DIR
    found = []
    for name in sorted(os.listdir(directory)):
        match = _NAME.match(name)
        if match:
            found.append((int(match.group(1)), os.path.join(directory, name)))
    found.sort()
    return found


def current_version(conn):
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0] or 0)


def latest(migrations_dir=None):
    migrations = available(migrations_dir)
    return migrations[-1][0] if migrations else 0


def migrate(db, backups_dir=None, migrations_dir=None):
    """Returns (version_before, version_after). `db` is a `Database`."""
    conn = db.conn()
    before = current_version(conn)
    pending = [(v, p) for v, p in available(migrations_dir) if v > before]

    if not pending:
        newest = latest(migrations_dir)
        if before > newest:
            log.info("database is at schema %d, newer than this release "
                     "(%d); leaving it alone", before, newest)
        return before, before

    if before > 0:
        backups_dir = backups_dir or os.path.join(
            os.path.dirname(db.path), "backups")
        _backup(conn, backups_dir, before)

    path = ""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for version, path in pending:
            with open(path, "r", encoding="utf-8") as fh:
                for statement in _statements(fh.read()):
                    conn.execute(statement)
            _record(conn, version)
        conn.execute("COMMIT")
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise MigrationError("migration %s failed: %s" % (
            os.path.basename(path), exc)) from exc

    after = pending[-1][0]
    log.info("database migrated from schema %d to %d", before, after)
    return before, after


def _record(conn, version):
    # schema_version is created by the first migration, so it may not exist
    # until that file has run.
    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]:
        conn.execute("UPDATE schema_version SET version = ?", (version,))
    else:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)",
                     (version,))


def _statements(script):
    """Split a script into statements without ending the transaction, which
    `executescript` would do. SQLite's own `complete_statement` knows about
    semicolons inside triggers and strings."""
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement and not _only_comments(statement):
                yield statement
    if buffer.strip() and not _only_comments(buffer):
        yield buffer.strip()


def _only_comments(text):
    return all(not line.strip() or line.strip().startswith("--")
               for line in text.splitlines())


def _backup(conn, backups_dir, schema_version):
    os.makedirs(backups_dir, exist_ok=True)
    target = os.path.join(backups_dir, "trstenko-v%d.db" % schema_version)
    temp = target + ".tmp"
    if os.path.exists(temp):
        os.remove(temp)
    copy = sqlite3.connect(temp)
    try:
        conn.backup(copy)
    finally:
        copy.close()
    os.replace(temp, target)

    # Newest schema first. Going by version rather than file time keeps the
    # choice the same on a card whose clock was wrong when it was written.
    copies = sorted(
        ((int(m.group(1)), os.path.join(backups_dir, m.group(0)))
         for m in (re.match(r"^trstenko-v(\d+)\.db$", n)
                   for n in os.listdir(backups_dir)) if m),
        reverse=True)
    for _, stale in copies[KEEP_BACKUPS:]:
        try:
            os.remove(stale)
        except OSError:
            pass
    return target

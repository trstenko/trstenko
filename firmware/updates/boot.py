"""Which release is running, and how a bad one gets undone.

The install itself is a symlink swap (see `installer.py`). This is the half
that matters afterwards: a new version has to prove it works, and if it does
not, the device has to go back on its own. A desk assistant that needs a
laptop and an SD card reader to recover is dead where it stands.

## The marker

Swapping `current` also writes `pending-confirm.json`:

    {"new": "0.4.0", "previous": "0.3.0", "at": 1770000000,
     "attempts": 0, "max_attempts": 3}

While that file exists, the new release is on trial. Two things can clear it:

- The new version reaches a working state and calls `confirm()`. The marker
  goes, the old release is pruned, and that is the end of it.
- It does not, and `rollback()` points `current` back at `previous`.

## Two nets, because they catch different failures

**A deadline inside the app.** The new version confirms once it has drawn
frames and settled on a face. If it is running but wedged -- a config it
cannot parse, a web server that never binds -- nothing calls `confirm()`, the
deadline passes, and it rolls itself back.

That does not help when the new version dies before any of its code runs, on a
syntax error or a missing import. Nothing of the new release executes, so the
new release cannot be the thing that recovers.

**So the second net runs outside it.** `bootcheck` runs before the app on
every boot, counts the attempt, and rolls back once the count passes
`max_attempts`. It is installed as a copy at `bin/bootcheck.py`, outside
`releases/`, so the file that performs the recovery is never the file being
replaced.

## Symlinks, and Windows

`current` is a symlink on the device. Windows needs a privilege for those, and
the emulator runs there, so there is a fallback: a one-line pointer file. The
device always gets the symlink -- systemd's `ExecStart` is a path, not a
lookup -- and the fallback exists so the whole flow can be tested on a laptop.
"""

import json
import os
import shutil
import time

MARKER = "pending-confirm.json"
CURRENT = "current"
POINTER = "current.path"
RELEASES = "releases"

MAX_ATTEMPTS = 3

# How long a new release has to prove itself before it rolls itself back.
# Long enough for a cold boot on a Pi 3 A+ with a face to draw and a web
# server to bind; short enough that nobody is standing there wondering.
CONFIRM_SECONDS = 120.0

KEEP_RELEASES = 2


class Layout:
    """Where an installed Trstenko lives.

        /opt/trstenko/releases/0.4.0/     unpacked release
        /opt/trstenko/current -> releases/0.4.0
        /opt/trstenko/bin/bootcheck.py    the recovery script, never swapped
        /opt/trstenko/pending-confirm.json

    Settings and the database are deliberately not here. They live under the
    data root, so rolling a release back does not roll back what the user
    told the device.
    """

    def __init__(self, root):
        self.root = root

    @property
    def releases(self):
        return os.path.join(self.root, RELEASES)

    @property
    def current(self):
        return os.path.join(self.root, CURRENT)

    @property
    def pointer(self):
        return os.path.join(self.root, POINTER)

    @property
    def marker(self):
        return os.path.join(self.root, MARKER)

    @property
    def bin_dir(self):
        return os.path.join(self.root, "bin")

    def release_dir(self, version):
        return os.path.join(self.releases, str(version))

    def installed(self):
        """Every release on disk, newest name last. Unsorted by version on
        purpose -- these are directory names, and the caller knows which one
        is current."""
        try:
            return sorted(os.listdir(self.releases))
        except OSError:
            return []

    # ---- the pointer ----

    def read_current(self):
        """The release `current` points at, or None."""
        try:
            target = os.readlink(self.current)
            return os.path.basename(target.rstrip("/\\"))
        except (OSError, ValueError, AttributeError):
            pass
        try:
            with open(self.pointer, "r", encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            return None

    def set_current(self, version):
        """Point at a release. Atomic where it can be.

        A symlink swapped through `rename` is the one operation here that
        cannot be interrupted half-done, which is why the whole install is
        arranged to end with it.
        """
        target = os.path.join(RELEASES, str(version))
        temp = self.current + ".swap"
        try:
            _unlink(temp)
            os.symlink(target, temp, target_is_directory=True)
            os.replace(temp, self.current)
            _unlink(self.pointer)
            return "symlink"
        except (OSError, NotImplementedError, AttributeError):
            _unlink(temp)

        _write_atomic(self.pointer, str(version))
        return "pointer"


class Pending:
    __slots__ = ("new", "previous", "at", "attempts", "max_attempts")

    def __init__(self, new, previous, at=None, attempts=0,
                 max_attempts=MAX_ATTEMPTS):
        self.new = str(new)
        self.previous = str(previous) if previous else ""
        self.at = at if at is not None else time.time()
        self.attempts = int(attempts)
        self.max_attempts = int(max_attempts)

    def as_dict(self):
        return {"new": self.new, "previous": self.previous, "at": self.at,
                "attempts": self.attempts, "max_attempts": self.max_attempts}

    @property
    def recoverable(self):
        return bool(self.previous)


class BootGuard:
    """Arms the trial, counts the attempts, and undoes the install."""

    def __init__(self, root, clock=time.time, log=None):
        self.layout = Layout(root)
        self.clock = clock
        self.log = log or (lambda line: None)
        self._deadline = None

    # ---- the marker ----

    def read(self):
        try:
            with open(self.layout.marker, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return Pending(**data)
        except (OSError, ValueError, TypeError):
            return None

    def arm(self, new, previous, max_attempts=MAX_ATTEMPTS):
        pending = Pending(new, previous, at=self.clock(),
                          max_attempts=max_attempts)
        _write_atomic(self.layout.marker,
                      json.dumps(pending.as_dict(), indent=1))
        return pending

    def note_attempt(self):
        """Called once per boot, before the app. Returns the marker after
        counting this boot, or None if nothing is on trial."""
        pending = self.read()
        if pending is None:
            return None
        pending.attempts += 1
        _write_atomic(self.layout.marker,
                      json.dumps(pending.as_dict(), indent=1))
        return pending

    # ---- the two outcomes ----

    def start_trial(self, now):
        """The app calls this at startup. Sets the deadline it has to beat."""
        if self.read() is None:
            self._deadline = None
            return False
        self._deadline = now + CONFIRM_SECONDS
        return True

    def on_trial(self):
        return self._deadline is not None

    def overdue(self, now):
        return self._deadline is not None and now >= self._deadline

    def confirm(self):
        """This release works. Clear the marker and prune the old ones."""
        pending = self.read()
        if pending is None:
            return False
        _unlink(self.layout.marker)
        self._deadline = None
        self.log("update: %s confirmed" % pending.new)
        self.prune()
        return True

    def rollback(self, reason=""):
        """Go back. Returns the version now pointed at, or None if there is
        nothing to go back to -- which is the case for a first install, and
        is why `arm` records the previous version rather than assuming one."""
        pending = self.read()
        if pending is None or not pending.recoverable:
            return None
        if not os.path.isdir(self.layout.release_dir(pending.previous)):
            self.log("update: cannot roll back, %s is gone" % pending.previous)
            return None

        self.layout.set_current(pending.previous)
        _unlink(self.layout.marker)
        self._deadline = None
        self.log("update: rolled back to %s%s"
                 % (pending.previous, (" (%s)" % reason) if reason else ""))
        return pending.previous

    def prune(self, keep=KEEP_RELEASES):
        """Keep the running release and the one before it. The previous one
        stays because it is what a rollback needs; anything older is a copy
        of the firmware taking up an SD card."""
        current = self.layout.read_current()
        names = [n for n in self.layout.installed() if n != current]
        for name in names[:-(keep - 1)] if keep > 1 else names:
            shutil.rmtree(self.layout.release_dir(name), ignore_errors=True)
            self.log("update: removed old release %s" % name)


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)


def _unlink(path):
    try:
        os.remove(path)
    except (OSError, PermissionError):
        try:
            os.rmdir(path)      # a directory symlink on Windows
        except OSError:
            pass

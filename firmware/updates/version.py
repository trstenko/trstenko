"""Versions, and the one the running firmware is.

Semantic versioning, `MAJOR.MINOR.PATCH` with an optional `-pre.N` suffix. The
release tag may carry a leading `v`; the tag and the version are not the same
string and comparing them as strings is how a device decides 0.10.0 is older
than 0.9.0.
"""

import os
import re

VERSION_FILE = os.path.join(os.path.dirname(__file__), "..", "VERSION")

_PATTERN = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?(?:\+[0-9A-Za-z.\-]+)?$")


class Version:
    __slots__ = ("major", "minor", "patch", "pre", "raw")

    def __init__(self, major, minor, patch, pre=None, raw=None):
        self.major = major
        self.minor = minor
        self.patch = patch
        self.pre = pre or None
        self.raw = raw or str(self)

    def __str__(self):
        base = "%d.%d.%d" % (self.major, self.minor, self.patch)
        return base + ("-" + self.pre if self.pre else "")

    def __repr__(self):
        return "Version(%s)" % self

    @property
    def prerelease(self):
        return self.pre is not None

    def _key(self):
        # A prerelease sorts below the release it leads to: 1.2.0-rc.1 < 1.2.0.
        if self.pre is None:
            return (self.major, self.minor, self.patch, 1, ())
        return (self.major, self.minor, self.patch, 0, _pre_key(self.pre))

    def __eq__(self, other):
        return isinstance(other, Version) and self._key() == other._key()

    def __hash__(self):
        return hash(self._key())

    def __lt__(self, other):
        return self._key() < other._key()

    def __le__(self, other):
        return self._key() <= other._key()

    def __gt__(self, other):
        return other < self

    def __ge__(self, other):
        return other <= self


def _pre_key(pre):
    """Dotted prerelease identifiers: numbers below strings, numbers numeric."""
    out = []
    for part in pre.split("."):
        if part.isdigit():
            out.append((0, int(part), ""))
        else:
            out.append((1, 0, part))
    return tuple(out)


def parse(text):
    """Return a Version, or None if the string is not one.

    Returning None rather than raising is deliberate: a repo will eventually
    get a tag like `hardware-rev-b`, and the updater has to skip it rather than
    crash the device that reads the release list.
    """
    if not text:
        return None
    m = _PATTERN.match(text.strip())
    if not m:
        return None
    return Version(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                   m.group(4), text.strip())


def current():
    """The version of the firmware that is running."""
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as fh:
            found = parse(fh.read())
    except OSError:
        found = None
    # An unreadable VERSION means every release looks newer, and the device
    # would offer an update forever. 0.0.0 is at least honest and comparable.
    return found or Version(0, 0, 0, raw="0.0.0")

"""Can this device take this update right now?

Run before an update is offered and again immediately before it is applied.
The gap between those two moments is minutes to days, and the SD card can fill
up in between.

A check is either blocking or advisory. Blocking stops the update, including
an urgent one -- urgency is a reason to skip asking the user, never a reason to
write firmware onto a full card or a browning-out board. Advisory checks are
shown and moved past.
"""

SPACE_MULTIPLIER = 3.0
"""Bundle, unpacked tree, and the current install kept intact for rollback."""

HEADROOM_BYTES = 256 * 1024 * 1024
"""On top of that. A root filesystem with nothing spare stops logging, and a
device that cannot log is a device that cannot explain a failed update."""

MAX_TEMP_C = 80.0


class Check:
    __slots__ = ("name", "ok", "detail", "blocking")

    def __init__(self, name, ok, detail="", blocking=True):
        self.name = name
        self.ok = bool(ok)
        self.detail = detail
        self.blocking = blocking

    def __repr__(self):
        return "Check(%s, %s)" % (self.name, "ok" if self.ok else "FAILED")

    def as_dict(self):
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "blocking": self.blocking}


class Result:
    def __init__(self, checks):
        self.checks = checks

    def __iter__(self):
        return iter(self.checks)

    @property
    def ok(self):
        return all(c.ok for c in self.checks if c.blocking)

    @property
    def blockers(self):
        return [c for c in self.checks if c.blocking and not c.ok]

    @property
    def warnings(self):
        return [c for c in self.checks if not c.blocking and not c.ok]

    def reason(self):
        """One line for the face and the web UI."""
        if self.ok:
            return ""
        return "; ".join(c.detail or c.name for c in self.blockers)

    def as_dict(self):
        return {"ok": self.ok, "reason": self.reason(),
                "checks": [c.as_dict() for c in self.checks]}


def required_bytes(size_bytes, multiplier=SPACE_MULTIPLIER,
                   headroom=HEADROOM_BYTES):
    return int(size_bytes * multiplier) + headroom


DOWNLOAD = "download"
INSTALL = "install"


def run(system, release, install_root, busy=False, allow_metered=False,
        stage=DOWNLOAD, keys=None, require_signature=False):
    """`system` is a `firmware.hal.System`. `busy` is the device's own answer
    to "am I mid-conversation" -- interrupting a turn is worse than waiting.

    `stage` is which question is being asked. Before downloading, a working
    connection is the whole point. Before installing, the bundle is already
    on the card and the network is irrelevant -- refusing to install because
    the wifi dropped in between would strand a device with a verified update
    sitting on its disk.
    """
    from . import version as version_mod

    installing = stage == INSTALL
    checks = []

    need = required_bytes(release.size_bytes)
    try:
        free = system.free_bytes(install_root)
    except OSError as exc:
        checks.append(Check("storage", False, "cannot read free space: %s" % exc))
    else:
        checks.append(Check(
            "storage", free >= need,
            "needs %s free, has %s" % (_mb(need), _mb(free))))

    if not installing:
        online = system.online()
        checks.append(Check(
            "network", online,
            "no network connection" if not online else "connected"))

        metered = system.metered()
        checks.append(Check(
            "metered", allow_metered or not metered,
            "connection is metered; %s would be billed by the megabyte"
            % _mb(release.size_bytes)))

    under = system.undervoltage()
    checks.append(Check(
        "power", not under,
        "the board reports under-voltage; fix the supply before flashing"))

    temp = system.temperature_c()
    checks.append(Check(
        "temperature", temp is None or temp < MAX_TEMP_C,
        "%.0f C" % temp if temp is not None else "unknown",
        blocking=False))

    # Advisory before a download, which is background work. Blocking before
    # an install, which unpacks an archive and builds a virtualenv -- on a Pi
    # 3 A+ that is minutes of CPU, and doing it under someone's question is
    # how a demo stutters.
    checks.append(Check(
        "idle", not busy, "the device is in the middle of something",
        blocking=installing))

    running = version_mod.current()
    if release.min_version is not None:
        ok = running >= release.min_version
        checks.append(Check(
            "min_version", ok,
            "this release needs %s or newer installed first; running %s"
            % (release.min_version, running)))

    checks.append(Check(
        "newer", release.version > running,
        "release %s is not newer than the installed %s"
        % (release.version, running)))

    # Blocking once signatures are: a signed manifest without a sha256 vouches
    # for no bundle in particular.
    checks.append(Check(
        "checksum_published", bool(release.sha256),
        "the release publishes no sha256, so the download cannot be verified",
        blocking=require_signature))

    # A checksum says the bundle arrived intact; a signature says who made
    # it. Blocking only once the device has a key to check against -- see
    # `require_signature` in config.py for why that is derived and not a
    # switch.
    signed = release.check_signature(keys or [])
    checks.append(Check(
        "signature",
        signed or not require_signature,
        release.signature_note if not signed
        else "signed by %s" % release.key_id,
        blocking=require_signature))

    return Result(checks)


def _mb(value):
    return "%.0f MB" % (value / (1024.0 * 1024.0))

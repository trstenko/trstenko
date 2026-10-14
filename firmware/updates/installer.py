"""Download, verify, and apply.

An update is a directory swap:

    releases/0.4.0.tmp/     unpacked here, verified here
    releases/0.4.0/         renamed into place once it is complete
    current -> releases/0.4.0

Every step before the last one is reversible by deleting a directory, and the
last one is a symlink rename, which either happened or did not. There is no
moment where the device is half-updated, which is the property worth having:
this thing sits on a desk and gets unplugged mid-sentence.

Afterwards the new release is on trial until it says it works -- `boot.py`
holds that half, including what happens when it never does.

## THE HONEST LIMIT: this updates the app, not the operating system

What a release can change: everything under `firmware/`, the face data, and
the Python packages in its own virtualenv, built offline from wheels carried
in the bundle.

What it cannot change: the kernel, anything installed with apt, the system
Python, ALSA or NetworkManager config, the display driver's system bits, or
anything else that lives outside `/opt/trstenko`. A release needing a newer
Piper binary, a newer glibc, or a kernel fix is not shippable this way.

That case is handled rather than ignored. The manifest carries
`requires_base`, the device knows its own base image version, and a bundle
asking for a newer base is **refused with an explanation** instead of being
half-applied. The recovery is reflashing the SD card, which for a handful of
units is a real answer and for a few hundred is not.

Growing past that means A/B root partitions -- RAUC or SWUpdate, the rootfs
duplicated, the bootloader's `tryboot` wired up, and the image laid out for it
before the first card is ever written. That is the Gen-2 job, and doing it
early would have meant deciding the SD layout before the enclosure was.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

from .boot import BootGuard, Layout
from .releases import ReleaseError

CHUNK = 64 * 1024

MANIFEST = "manifest.json"
BASE_FILE = "base.json"

# What this firmware's install layout understands. A bundle built for a later
# one is refused rather than guessed at.
LAYOUT_VERSION = 1
DEFAULT_BASE = 1


class StagingError(Exception):
    """The bundle did not survive the trip, or is not what it claims."""


class ApplyError(Exception):
    """The bundle is fine; this device cannot take it."""


class Staged:
    """A downloaded, verified bundle waiting to be applied."""

    def __init__(self, path, version, sha256, size_bytes):
        self.path = path
        self.version = version
        self.sha256 = sha256
        self.size_bytes = size_bytes

    def as_dict(self):
        return {"path": self.path, "version": str(self.version),
                "sha256": self.sha256, "size_bytes": self.size_bytes}


class Applied:
    """The result of a successful apply. The device still has to restart."""

    def __init__(self, version, previous, pointer, needs_restart=True):
        self.version = str(version)
        self.previous = str(previous or "")
        self.pointer = pointer
        self.needs_restart = needs_restart

    def as_dict(self):
        return {"version": self.version, "previous": self.previous,
                "pointer": self.pointer, "needs_restart": self.needs_restart}


def base_version(root):
    """The SD image this device was flashed from.

    Written once when the card is made. A device with no file is assumed to
    be base 1: refusing every update because a marker is missing would be a
    worse failure than accepting one that might not fit.
    """
    try:
        with open(os.path.join(root, BASE_FILE), "r", encoding="utf-8") as fh:
            return int(json.load(fh).get("base", DEFAULT_BASE))
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_BASE


class Installer:
    def __init__(self, source, staging_dir, install_root=None, log=None,
                 venv_builder=None, runner=None):
        self.source = source
        self.staging_dir = staging_dir
        self.install_root = install_root or os.path.dirname(
            staging_dir.rstrip("/\\"))
        self.layout = Layout(self.install_root)
        self.log = log or (lambda line: None)
        self.guard = BootGuard(self.install_root, log=self.log)
        # Both injected so the emulator and the tests can watch them being
        # called without building a virtualenv or restarting anything.
        self.venv_builder = venv_builder or build_venv
        self.runner = runner or subprocess.run

    # ---- download ----

    def download(self, release, progress=None):
        """Stream the bundle into staging and verify it. Raises on any
        failure, leaving nothing behind."""
        os.makedirs(self.staging_dir, exist_ok=True)
        self.clear()

        target = os.path.join(
            self.staging_dir, "trstenko-firmware-%s%s" % (
                release.version, _suffix(release.bundle_url)))

        handle, temp_path = tempfile.mkstemp(dir=self.staging_dir,
                                             suffix=".part")
        try:
            with os.fdopen(handle, "wb") as fh:
                written = self.source.fetch(release.bundle_url, fh,
                                            progress=progress)
        except (ReleaseError, OSError):
            _unlink(temp_path)
            raise

        try:
            if release.size_bytes and written != release.size_bytes:
                raise StagingError(
                    "downloaded %d bytes, the release says %d"
                    % (written, release.size_bytes))

            digest = sha256_of(temp_path)
            if release.sha256 and digest != release.sha256:
                raise StagingError(
                    "checksum mismatch: got %s, expected %s"
                    % (digest[:16], release.sha256[:16]))
        except Exception:
            _unlink(temp_path)
            raise

        os.replace(temp_path, target)
        return Staged(target, release.version, digest, written)

    # ---- apply ----

    def apply(self, staged):
        """Unpack, build, swap, and arm the rollback.

        Returns an `Applied`. It does not restart the device: the caller owns
        that, because it is the thing that knows whether the device is in the
        middle of a sentence.
        """
        version = str(staged.version)
        release_dir = self.layout.release_dir(version)
        work = release_dir + ".tmp"

        previous = self.layout.read_current()
        if previous == version:
            raise ApplyError("%s is already the running release" % version)

        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(self.layout.releases, exist_ok=True)

        try:
            manifest = self._unpack(staged.path, work)
            self._check_manifest(manifest, version)
            self._build(work, manifest)
            _fsync_dir(work)
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise

        # Complete on disk before it is visible under its real name. A power
        # cut up to here leaves a .tmp directory and nothing else.
        shutil.rmtree(release_dir, ignore_errors=True)
        os.replace(work, release_dir)
        _fsync_dir(self.layout.releases)

        self._install_bootcheck(release_dir)

        # Armed before the swap, not after: the marker is what recovers the
        # device, and a power cut between the two would otherwise leave a new
        # release running with nothing watching it.
        self.guard.arm(version, previous)
        pointer = self.layout.set_current(version)

        self.log("update: %s installed, %s on trial"
                 % (version, "rollback armed" if previous
                    else "no previous release to roll back to"))
        return Applied(version, previous, pointer)

    def restart(self, service="trstenko.service"):
        """Hand over to the new release. systemd starts it again, bootcheck
        runs first, and the new version has `CONFIRM_SECONDS` to prove
        itself."""
        self.runner(["systemctl", "restart", service], check=False)

    # ---- the pieces ----

    def _unpack(self, bundle, work):
        os.makedirs(work, exist_ok=True)
        try:
            if bundle.endswith(".zip"):
                with zipfile.ZipFile(bundle) as archive:
                    _check_members(archive.namelist())
                    archive.extractall(work)
            else:
                with tarfile.open(bundle) as archive:
                    _check_members(archive.getnames())
                    _check_tar_entries(archive.getmembers())
                    _extract(archive, work)
        except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
            raise StagingError("cannot unpack the bundle: %s" % exc)

        path = os.path.join(work, MANIFEST)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError) as exc:
            raise StagingError("the bundle has no usable %s: %s"
                               % (MANIFEST, exc))
        if not isinstance(manifest, dict):
            raise StagingError("%s is not an object" % MANIFEST)
        return manifest

    def _check_manifest(self, manifest, version):
        if str(manifest.get("version", version)) != version:
            raise StagingError(
                "the bundle contains %s but the release says %s"
                % (manifest.get("version"), version))

        layout = int(manifest.get("layout", LAYOUT_VERSION))
        if layout > LAYOUT_VERSION:
            raise ApplyError(
                "this release uses install layout %d and the running "
                "firmware understands %d" % (layout, LAYOUT_VERSION))

        needs = int(manifest.get("requires_base", 0))
        have = base_version(self.install_root)
        if needs > have:
            # The honest limit, enforced. See the module docstring.
            raise ApplyError(
                "this release needs base image %d and the card was flashed "
                "with %d. Updates replace the application, not the operating "
                "system, so this one needs the SD card reflashed rather than "
                "an update." % (needs, have))

    def _build(self, work, manifest):
        """Build the release's virtualenv from wheels carried in the bundle.

        Offline on purpose. An install that reaches PyPI can fail halfway
        because a network dropped, at the exact moment the device is least
        able to explain itself -- and it makes the result depend on what the
        internet looked like that afternoon.
        """
        wheels = os.path.join(work, "wheels")
        requirements = os.path.join(work, "requirements.txt")
        if not (os.path.isdir(wheels) and os.path.exists(requirements)):
            self.log("update: no wheels in the bundle, using the system "
                     "interpreter")
            return None
        return self.venv_builder(work, wheels, requirements, self.runner,
                                 self.log)

    def _install_bootcheck(self, release_dir):
        """Copy the recovery script somewhere the next update will not touch.

        It has to keep working when the release it came from is the broken
        one, so it cannot live inside `releases/`.

        On the device `bin/` is root's and read-only to this service
        (`deploy/trstenko.service`): the boot units run what is in it, so
        whatever can rewrite it chooses what runs at boot. There the copy is
        refused, the card keeps the script it was made with, and changing it
        is a new base image. Elsewhere (the emulator, a bench Pi set up by
        hand) the copy goes ahead.
        """
        # bootcheck.py and the boot guard it uses. Both, because the copy
        # runs as a script with no package around it.
        here = os.path.dirname(os.path.abspath(__file__))
        packaged = os.path.join(release_dir, "firmware", "updates")
        try:
            os.makedirs(self.layout.bin_dir, exist_ok=True)
            for name in ("bootcheck.py", "boot.py"):
                source = os.path.join(packaged, name)
                if not os.path.exists(source):
                    source = os.path.join(here, name)
                shutil.copyfile(source,
                                os.path.join(self.layout.bin_dir, name))
        except OSError as exc:
            self.log("update: bootcheck.py left as the card has it (%s)"
                     % exc)

    # ---- staging housekeeping ----

    def clear(self):
        if not os.path.isdir(self.staging_dir):
            return
        for name in os.listdir(self.staging_dir):
            _unlink(os.path.join(self.staging_dir, name))

    def staged_bytes(self):
        if not os.path.isdir(self.staging_dir):
            return 0
        return sum(
            os.path.getsize(os.path.join(self.staging_dir, n))
            for n in os.listdir(self.staging_dir)
            if os.path.isfile(os.path.join(self.staging_dir, n)))


def build_venv(work, wheels, requirements, runner, log):
    """`python -m venv` plus an offline pip install. Minutes on a Pi 3 A+."""
    import venv

    target = os.path.join(work, "venv")
    venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(target)
    python = os.path.join(target, "Scripts" if os.name == "nt" else "bin",
                          "python" + (".exe" if os.name == "nt" else ""))
    result = runner([python, "-m", "pip", "install", "--no-index",
                     "--find-links", wheels, "-r", requirements],
                    capture_output=True, text=True)
    if getattr(result, "returncode", 0) != 0:
        raise StagingError("building the release's virtualenv failed: %s"
                           % (getattr(result, "stderr", "") or "")[-400:])
    log("update: virtualenv built from the bundle's own wheels")
    return target


def sha256_of(path, chunk=CHUNK):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _check_members(names):
    """Refuse a bundle that writes outside the directory it is unpacked into.

    A signed bundle from your own release page should never contain `../`.
    One that does is either broken or hostile, and the difference does not
    matter at the point where it is about to overwrite /etc.
    """
    for name in names:
        if name.startswith("/") or name.startswith("\\") or ".." in name.replace("\\", "/").split("/"):
            raise StagingError("the bundle tries to write outside its "
                               "directory: %r" % name)


def _check_tar_entries(members):
    """Refuse what a name check cannot see: a link whose target leaves the
    directory, and anything that is not a plain file, a folder or a link.
    `tarfile`'s `data` filter does this on a Python that has it; this is the
    same rule for one that does not, checked before anything is written."""
    for member in members:
        if member.issym() or member.islnk():
            target = member.linkname.replace("\\", "/")
            if member.issym():
                # Relative to the link's own folder.
                base = os.path.dirname(member.name.replace("\\", "/"))
                target = os.path.normpath(os.path.join(base, target))
            else:
                target = os.path.normpath(target)
            if target.startswith("/") or target == ".." \
                    or target.startswith("../"):
                raise StagingError("the bundle links outside its directory: "
                                   "%r -> %r" % (member.name,
                                                 member.linkname))
        elif not (member.isfile() or member.isdir()):
            raise StagingError("the bundle holds something that is not a "
                               "file or a folder: %r" % member.name)


def _extract(archive, work):
    # Python 3.12+ can enforce this itself; older ones get the manual check
    # above and nothing worse than a normal extract.
    if hasattr(tarfile, "data_filter"):
        archive.extractall(work, filter="data")
    else:
        archive.extractall(work)


def _fsync_dir(path):
    """Make the rename survive a power cut. A no-op on Windows, where the
    emulator runs and where it does not matter."""
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _suffix(url):
    for ext in (".tar.gz", ".tar.xz", ".zip"):
        if url.lower().endswith(ext):
            return ext
    return ".bundle"


def _unlink(path):
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.remove(path)
    except OSError:
        pass


__all__ = ["Installer", "Staged", "Applied", "StagingError", "ApplyError",
           "sha256_of", "base_version", "build_venv"]

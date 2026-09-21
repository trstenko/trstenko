"""A release feed with no GitHub behind it.

The update flow is the part of this firmware most likely to be wrong and least
likely to be exercised -- testing it for real means cutting a release, waiting
for a device to notice, and having no way to replay it. This serves whatever
release you ask for, downloads at whatever speed you ask for, and can be told
to fail.

It implements `firmware.updates.ReleaseSource`, so the manager cannot tell it
from the real one. Point the emulator at a real repo with `--repo owner/name`
and the same manager talks to GitHub instead.
"""

import hashlib
import io
import os
import tempfile
import time

from firmware.updates import Release, ReleaseError, ReleaseSource
from firmware.updates.version import parse

from . import bundle as bundle_mod

DEFAULT_SUMMARIES = {
    "urgent": "Fixes a crash that leaves the microphone recording after a "
              "failed turn",
    "recommended": "Faster wake word, and the face no longer stutters during "
                   "long replies",
    "optional": "Two new faces and a Slovenian voice for Piper",
}


class FakeReleases(ReleaseSource):
    def __init__(self, version="0.4.0", urgency="optional", size_mb=18.0,
                 kbps=2400.0, clock=time.monotonic, real_bundle=False,
                 work_dir=None):
        self.version = version
        self.urgency = urgency
        self.size_mb = size_mb
        self.kbps = kbps
        self.clock = clock

        # A real tar.gz of this checkout's firmware, so `install` has
        # something to unpack. Off by default: most scenarios only exercise
        # the download, and building the archive costs a second.
        self.real_bundle = real_bundle
        self.work_dir = work_dir or tempfile.mkdtemp(prefix="trstenko-feed-")
        self.requires_base = 0
        self.broken_release = False
        # A throwaway signing key, made once per run. The emulator hands the
        # public half to the updater, so the signed path is the default one
        # here rather than an option nobody switches on.
        self.seed, self.public_key = _keypair()
        self.sign_releases = True
        self.break_signature = False

        self.offline = False
        self.fail_download = False
        self.corrupt = False
        self.min_version = None
        self.checks = 0
        self.downloads = 0
        self._cache = {}

    # ---- what the manager sees ----

    def _bundle(self):
        """Build the archive once per (version, shape) and remember it."""
        key = (self.version, self.requires_base, self.broken_release,
               int(self.size_mb * 1024 * 1024))
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        path = os.path.join(
            self.work_dir, "trstenko-firmware-%s-%s.tar.gz"
            % (self.version, "broken" if self.broken_release else "ok"))
        bundle_mod.build(path, self.version,
                         requires_base=self.requires_base,
                         pad_to=int(self.size_mb * 1024 * 1024),
                         broken=self.broken_release)
        with open(path, "rb") as fh:
            payload = fh.read()
        self._cache[key] = payload
        return payload

    def payload(self):
        if self.real_bundle:
            return self._bundle()
        return _payload(int(self.size_mb * 1024 * 1024))

    def manifest(self, size, digest):
        from tools.release import sign_manifest

        manifest = {
            "version": self.version,
            "urgency": self.urgency,
            "summary": DEFAULT_SUMMARIES.get(self.urgency, ""),
            "requires_base": self.requires_base,
            "requires_reboot": True,
            "size_bytes": size,
            "sha256": digest,
        }
        if self.sign_releases:
            sign_manifest(manifest, self.seed)
            if self.break_signature:
                # Signed, then altered. The signature still looks like one
                # and no longer covers what is in front of it, which is the
                # case a bare checksum cannot tell from a good release.
                manifest["urgency"] = "urgent"
                manifest["summary"] = "tampered with after signing"
        return manifest

    def build(self):
        payload = self.payload()
        size = len(payload)
        digest = hashlib.sha256(payload).hexdigest()
        manifest = self.manifest(size, digest)
        return Release(
            version=parse(self.version),
            urgency=self.urgency,
            summary=DEFAULT_SUMMARIES.get(self.urgency, ""),
            notes="Emulated release. Nothing here was built by anyone.",
            url="https://example.invalid/releases/%s" % self.version,
            bundle_url="emulator://trstenko-firmware-%s.tar.gz" % self.version,
            size_bytes=size,
            sha256=digest,
            min_version=parse(self.min_version or ""),
            requires_reboot=True,
            published="",
            prerelease=False,
            manifest=manifest,
        )

    def latest(self, channel="stable"):
        self.checks += 1
        if self.offline:
            raise ReleaseError("cannot reach GitHub: the emulator is offline")
        if not self.version:
            return None
        return self.build()

    def fetch(self, url, into, progress=None, chunk=64 * 1024):
        self.downloads += 1
        if self.offline:
            raise ReleaseError("download failed: the emulator is offline")

        payload = self.payload()
        size = len(payload)
        if self.corrupt:
            # One flipped byte. The checksum has to catch this; if it does not,
            # the device would install a broken image and find out at boot.
            payload = b"\x01" + payload[1:]

        source = io.BytesIO(payload)
        written = 0
        rate = max(1.0, self.kbps) * 1024.0
        started = self.clock()

        while True:
            block = source.read(chunk)
            if not block:
                break
            if self.fail_download and written >= size // 3:
                raise ReleaseError("download failed: connection reset")
            into.write(block)
            written += len(block)
            if progress is not None:
                progress(written, size)
            # Pace it. An instant download means the progress bar, the cancel
            # button and the "do not interrupt a turn" rule never get tested.
            behind = written / rate - (self.clock() - started)
            if behind > 0:
                time.sleep(min(behind, 0.25))

        return written


def _keypair():
    """Imported late: `tools.release` pulls in the bundle builder, and this
    module is imported by things that only want the feed."""
    from tools.release import keypair

    return keypair()


def _payload(size):
    """Deterministic filler, so the checksum is stable across calls."""
    block = hashlib.sha256(b"trstenko").digest() * 512  # 16 KiB
    return (block * (size // len(block) + 1))[:size]

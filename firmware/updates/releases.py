"""Where updates come from: GitHub releases.

A release is a candidate when it has a parseable semver tag, is not a draft,
matches the configured channel, and carries a firmware bundle asset. Anything
else in the repo's release list is ignored rather than treated as an error --
the repo has releases for the enclosure and the docs too.

## How a release says how urgent it is

Preferred: attach `update.json` to the release.

    {
      "version": "0.4.1",
      "urgency": "urgent",
      "summary": "Fixes a crash that leaves the mic hot after a failed turn",
      "min_version": "0.3.0",
      "requires_reboot": true,
      "sha256": "…",
      "size_bytes": 8412300
    }

Fallback: a line in the release body, `Urgency: urgent`. That exists because
the person cutting a release at 1am will forget the asset, and the default when
nothing says otherwise is `optional` -- the safe direction, since an optional
update waits for consent and an urgent one does not.
"""

import json
import re
import urllib.error
import urllib.request

from . import version as version_mod
from .config import PRERELEASE_CHANNELS

API = "https://api.github.com"
USER_AGENT = "trstenko-updater"
MANIFEST_ASSET = "update.json"
BUNDLE_SUFFIXES = (".tar.gz", ".tar.xz", ".zip")

URGENT = "urgent"
RECOMMENDED = "recommended"
OPTIONAL = "optional"
URGENCIES = (URGENT, RECOMMENDED, OPTIONAL)

_BODY_URGENCY = re.compile(r"^\s*urgency\s*:\s*(\w+)\s*$",
                           re.IGNORECASE | re.MULTILINE)


class ReleaseError(Exception):
    """The release feed could not be read or made sense of."""


class Release:
    """One candidate update, as the rest of the updater sees it."""

    def __init__(self, version, urgency=OPTIONAL, summary="", notes="",
                 url="", bundle_url="", size_bytes=0, sha256="",
                 min_version=None, requires_reboot=True, published="",
                 prerelease=False, manifest=None):
        self.version = version
        self.urgency = urgency if urgency in URGENCIES else OPTIONAL
        self.summary = summary
        self.notes = notes
        self.url = url
        self.bundle_url = bundle_url
        self.size_bytes = int(size_bytes or 0)
        self.sha256 = (sha256 or "").lower()
        self.min_version = min_version
        self.requires_reboot = bool(requires_reboot)
        self.published = published
        self.prerelease = prerelease
        # The manifest as published, kept whole: the signature covers these
        # exact fields, so checking it means checking what arrived rather
        # than what this class chose to keep.
        self.manifest = manifest or {}
        self.signed = False
        self.key_id = ""
        self.signature_note = "not checked yet"

    def __repr__(self):
        return "Release(%s, %s)" % (self.version, self.urgency)

    @property
    def urgent(self):
        return self.urgency == URGENT

    def as_dict(self):
        return {
            "version": str(self.version),
            "urgency": self.urgency,
            "summary": self.summary,
            "notes": self.notes,
            "url": self.url,
            "size_bytes": self.size_bytes,
            "requires_reboot": self.requires_reboot,
            "published": self.published,
            "prerelease": self.prerelease,
            "min_version": str(self.min_version) if self.min_version else None,
            "signed": self.signed,
            "key_id": self.key_id,
            "signature_note": self.signature_note,
        }

    def check_signature(self, keys):
        """Record who signed this, if anyone. Returns whether it verified."""
        from . import signing

        self.signed, self.key_id, self.signature_note = signing.check(
            self.manifest, keys)
        if not self.signed:
            return False
        # The signature covers the manifest, not the GitHub release around
        # it. Without these, an old signed manifest and its bundle could be
        # republished under a new tag, or the unsigned release body could
        # make a signed release urgent.
        if str(self.manifest.get("version", "")) != str(self.version):
            self.signed = False
            self.signature_note = (
                "the signed manifest is for %s, not %s"
                % (self.manifest.get("version") or "no version", self.version))
            return False
        urgency = str(self.manifest.get("urgency") or OPTIONAL).lower()
        self.urgency = urgency if urgency in URGENCIES else OPTIONAL
        self.signature_note = "signed by %s" % self.key_id
        return True


class ReleaseSource:
    """Implement this to feed the updater from somewhere else. The emulator
    does exactly that, which is how the whole flow is testable without cutting
    a real release."""

    def latest(self, channel="stable"):
        raise NotImplementedError

    def fetch(self, url, into, progress=None, chunk=64 * 1024):
        raise NotImplementedError


class GitHubReleases(ReleaseSource):
    def __init__(self, repo, token=None, timeout=15, opener=None):
        self.repo = repo
        self.token = token
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    # ---- reading the feed ----

    def _get(self, url, accept="application/vnd.github+json"):
        request = urllib.request.Request(url, headers={
            "Accept": accept,
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if self.token:
            request.add_header("Authorization", "Bearer %s" % self.token)
        try:
            with self._open(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and "rate limit" in str(exc.reason).lower():
                raise ReleaseError("GitHub rate limit reached") from exc
            raise ReleaseError("GitHub returned %s" % exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ReleaseError("cannot reach GitHub: %s" % exc) from exc

    def releases(self, limit=20):
        url = "%s/repos/%s/releases?per_page=%d" % (API, self.repo, limit)
        try:
            data = json.loads(self._get(url))
        except ValueError as exc:
            raise ReleaseError("release feed is not JSON") from exc
        if not isinstance(data, list):
            raise ReleaseError("release feed is not a list")
        return data

    def latest(self, channel="stable"):
        """Newest candidate on the channel, or None. Not GitHub's own
        `/latest`, which ignores prereleases and, more importantly, means
        "most recently published" rather than "highest version"."""
        best = None
        for raw in self.releases():
            release = self._parse(raw)
            if release is None:
                continue
            if release.prerelease and channel not in PRERELEASE_CHANNELS:
                continue
            if best is None or release.version > best.version:
                best = release
        return best

    def _parse(self, raw):
        if raw.get("draft"):
            return None
        parsed = version_mod.parse(raw.get("tag_name", ""))
        if parsed is None:
            return None

        bundle = _pick_bundle(raw.get("assets") or [])
        if bundle is None:
            return None

        manifest = self._manifest(raw.get("assets") or [])
        body = raw.get("body") or ""

        urgency = manifest.get("urgency") or _urgency_from_body(body)
        min_version = version_mod.parse(manifest.get("min_version") or "")

        return Release(
            version=parsed,
            urgency=(urgency or OPTIONAL).lower(),
            summary=manifest.get("summary") or _first_line(body),
            notes=manifest.get("notes") or body,
            url=raw.get("html_url", ""),
            bundle_url=bundle.get("browser_download_url", ""),
            size_bytes=manifest.get("size_bytes") or bundle.get("size", 0),
            sha256=manifest.get("sha256", ""),
            min_version=min_version,
            requires_reboot=manifest.get("requires_reboot", True),
            published=raw.get("published_at", ""),
            prerelease=bool(raw.get("prerelease")),
            manifest=manifest,
        )

    def _manifest(self, assets):
        for asset in assets:
            if asset.get("name") != MANIFEST_ASSET:
                continue
            try:
                doc = json.loads(self._get(asset["browser_download_url"],
                                           accept="application/octet-stream"))
            except (ReleaseError, ValueError, KeyError):
                # A malformed manifest must not hide the release. The body
                # fallback and the default urgency still apply.
                return {}
            return doc if isinstance(doc, dict) else {}
        return {}

    # ---- downloading ----

    def fetch(self, url, into, progress=None, chunk=64 * 1024):
        """Stream a bundle to an open binary file. Returns the byte count.

        Streamed rather than read whole: the board has 512MB and the bundle is
        tens of megabytes, and `response.read()` would hold all of it plus the
        copy being written.
        """
        request = urllib.request.Request(url, headers={
            "Accept": "application/octet-stream",
            "User-Agent": USER_AGENT,
        })
        if self.token:
            request.add_header("Authorization", "Bearer %s" % self.token)

        written = 0
        try:
            with self._open(request, timeout=self.timeout) as response:
                total = int(response.headers.get("Content-Length") or 0)
                while True:
                    block = response.read(chunk)
                    if not block:
                        break
                    into.write(block)
                    written += len(block)
                    if progress is not None:
                        progress(written, total)
        except (urllib.error.URLError, OSError) as exc:
            raise ReleaseError("download failed: %s" % exc) from exc
        return written


def _pick_bundle(assets):
    for asset in assets:
        name = (asset.get("name") or "").lower()
        if name.startswith("trstenko-firmware") and name.endswith(
                BUNDLE_SUFFIXES):
            return asset
    return None


def _urgency_from_body(body):
    match = _BODY_URGENCY.search(body or "")
    if not match:
        return OPTIONAL
    found = match.group(1).lower()
    return found if found in URGENCIES else OPTIONAL


def _first_line(body):
    for line in (body or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line and not _BODY_URGENCY.match(line):
            return line
    return ""

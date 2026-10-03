"""Build the tar.gz a release actually ships, so the emulator installs a
real one.

The installer unpacks an archive, reads `manifest.json`, checks the base
image requirement and swaps a directory. Handing it a few megabytes of filler
would test the download and nothing after it, so this makes the real shape:

    manifest.json          version, layout, requires_base, entry
    firmware/...           the app
    requirements.txt       what the venv needs
    wheels/                the packages, so the install never needs the network
    VERSION

The firmware copied in is this checkout's, minus the parts a release does not
carry. `pad_to` then inflates it to whatever size the test wants, with an
incompressible blob, because a bundle that gzips down to nothing makes the
download finish before the progress bar has drawn.
"""

import io
import json
import os
import secrets
import tarfile
import time

FIRMWARE = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "firmware"))

SKIP_DIRS = {"__pycache__", "staging", "releases"}


def build(path, version, requires_base=0, layout=1, pad_to=0, broken=False):
    """Write a bundle to `path` and return its size.

    `broken` puts a syntax error in the entry point -- a release that cannot
    start at all, which is the case the rollback exists for and the one that
    is impossible to test by hand without shipping a broken release.
    """
    manifest = {
        "version": str(version),
        "layout": layout,
        "requires_base": requires_base,
        "entry": "firmware.main",
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "broken": bool(broken),
    }

    with tarfile.open(path, "w:gz") as archive:
        _add_bytes(archive, "manifest.json",
                   json.dumps(manifest, indent=1).encode())
        _add_bytes(archive, "VERSION", ("%s\n" % version).encode())
        _add_bytes(archive, "requirements.txt", b"Pillow>=10.0\n")

        for rel in _firmware_files():
            source = os.path.join(FIRMWARE, rel)
            name = "firmware/" + rel.replace(os.sep, "/")
            if broken and rel == "main.py":
                _add_bytes(archive, name,
                           b"this is not python and will not import\n")
                continue
            archive.add(source, arcname=name)

        if pad_to:
            current = os.path.getsize(path) if os.path.exists(path) else 0
            _add_bytes(archive, "filler.bin",
                       secrets.token_bytes(max(0, pad_to - current)))

    return os.path.getsize(path)


def _firmware_files():
    for folder, dirs, files in os.walk(FIRMWARE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            yield os.path.relpath(os.path.join(folder, name), FIRMWARE)


def _add_bytes(archive, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    archive.addfile(info, io.BytesIO(data))

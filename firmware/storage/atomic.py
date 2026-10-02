"""Writing a file so a power cut leaves the old one or the new one, never half.

Write beside it, flush it to the card, then `os.replace` over the original.
`replace` is atomic on every filesystem the device or a PC uses. The `fsync` is
what `updates/manager._save_state` skips and what matters here: without it the
rename can reach the card before the data does, and a power cut then leaves a
whole file with nothing in it.
"""

import json
import os


def write_json(path, data, mode=None):
    """`mode` is applied at creation, so a secrets file is never readable by
    anyone else even for a moment."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp = path + ".tmp"

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(temp, flags, 0o666 if mode is None else mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    if mode is not None:
        try:
            # The mode in `os.open` is filtered by the umask and ignored by
            # some filesystems; say it again.
            os.chmod(temp, mode)
        except OSError:
            pass

    os.replace(temp, path)
    _sync_directory(directory)


def _sync_directory(directory):
    """So the rename itself survives. Not possible on Windows, and not needed
    there for anything this device does."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)

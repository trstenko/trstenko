"""Run before the app, every boot. Undo an update that cannot start.

    python3 bootcheck.py /opt/trstenko

systemd runs this from `/opt/trstenko/bin/bootcheck.py`, which is a copy kept
outside `releases/`. That placement is the whole point: this is what recovers
a device whose new release will not start, so it cannot be a file inside the
release being tested.

It counts one attempt per boot. Past `max_attempts`, it points `current` back
at the previous release and clears the marker, and the app that starts a
moment later is the old one.

Deliberately small and standard-library only. It has to run on a device where
the new firmware's imports are exactly what is broken -- so it imports none of
them, and the copy is executed as a script rather than as part of the
package.
"""

import os
import sys

if __package__ in (None, ""):
    # Running as the standalone copy at bin/bootcheck.py.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from boot import BootGuard          # noqa: F401  (a sibling copy)
    except ImportError:
        BootGuard = None
else:
    from .boot import BootGuard

DEFAULT_ROOT = os.environ.get("TRSTENKO_ROOT", "/opt/trstenko")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    root = argv[0] if argv else DEFAULT_ROOT

    if BootGuard is None:
        print("bootcheck: cannot import the boot guard; leaving %s alone"
              % root, file=sys.stderr)
        return 0

    guard = BootGuard(root, log=lambda line: print(line, file=sys.stderr))
    pending = guard.note_attempt()
    if pending is None:
        return 0

    if pending.attempts > pending.max_attempts:
        went_back = guard.rollback(
            "%d boots without confirmation" % (pending.attempts - 1))
        if went_back is None:
            print("bootcheck: %s failed to start and there is nothing to go "
                  "back to" % pending.new, file=sys.stderr)
            return 1
        return 0

    print("bootcheck: %s on trial, attempt %d of %d"
          % (pending.new, pending.attempts, pending.max_attempts),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

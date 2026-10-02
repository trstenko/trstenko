"""Entry point on the device.

    python -m firmware.main

This is what the systemd unit runs. It opens the hardware, builds the updater
and the web UI, and hands the loop to `Device`. On a PC it refuses and points
at the emulator, because a firmware that quietly substitutes fake hardware is
a firmware that gets shipped untested.

## Boot order

The face has to appear before anything slow loads, and `Device.healthy` treats
drawn frames as proof a release works. Plan 01 has the full table; this is as
much of it as exists so far:

1. Logging. Before anything that can fail, so the failure is in the journal.
2. Open the hardware. Without it there is nothing to do.
3. Settings and secrets, then the database and its migrations. A migration
   that fails is not caught here: the transaction has rolled back, the old
   schema is intact, and a release that cannot start is exactly what the
   update system's rollback is for.
4. Build the device; `face_boot` starts drawing on the first tick.
5. Start the web server, which never blocks the loop.
6. Tell systemd `READY=1`, then run.

The voice pipeline is not wired in yet (Phase 2). The device runs without one:
it boots, shows a face, respects the mute switch and the buttons, and serves
the update page. That is deliberately the order -- everything here works before
the first API key exists.

## Shutdown

SIGTERM (systemd stop, a reboot) posts `Shutdown` rather than doing the work
in the signal handler: the loop finishes its frame, leaves `run`, and the
`finally` below runs the same close path as the power button. Before this, a
stop from systemd killed the process mid-write.
"""

import argparse
import logging
import os
import signal
import sys
import threading

from . import events, logs, sdnotify
from .device import Device
from .hal import open_hal
from .storage import DB_NAME, Database
from .storage import migrate as migrate_mod
from .storage import secrets as secrets_mod
from .storage import settings as settings_mod
from .updates import GitHubReleases, UpdateConfig, UpdateManager
from .webui import Auth, UpdateApi, serve

log = logging.getLogger("trstenko.main")

# How often systemd is told the loop is turning. The unit allows 20s of
# silence, so this leaves the loop room for a slow frame or two.
WATCHDOG_EVERY = 1.0


def build(hal, config=None, notifier=None, on_log=None):
    config = config or UpdateConfig.load()
    source = GitHubReleases(config.repo, config.token)
    updater = UpdateManager(
        config, source, hal.system,
        on_event=lambda kind, payload: log.info("update: %s", kind))
    settings = settings_mod.Settings.load(
        settings_mod.default_path(config.data_root))
    secrets = secrets_mod.Secrets.load(
        secrets_mod.default_path(config.data_root))
    db = Database(os.path.join(config.data_root, DB_NAME))
    migrate_mod.migrate(db)

    device = Device(hal, pipeline=None, updater=updater, on_log=on_log,
                    db=db, settings=settings, secrets=secrets)
    device.add_closer("storage", db.close)
    if notifier is not None:
        # A step of the main loop, so a stalled loop stops pinging.
        device.add_step("watchdog", lambda now: notifier.watchdog(),
                        every=WATCHDOG_EVERY)
    return device, updater


def terminate_handler(device):
    """What SIGTERM does. Separate from `signal.signal` so a test can call it."""
    def on_term(signum, frame):
        log.info("SIGTERM: shutting down")
        device.events.post(events.Event(events.SHUTDOWN))
    return on_term


def run(device, notifier):
    """Run until told to stop, then leave things the way plan 01 says."""
    notifier.ready()
    try:
        device.run()
    except KeyboardInterrupt:
        pass
    finally:
        notifier.stopping()
        device.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-web", action="store_true")
    args = ap.parse_args(argv)

    logs.setup()

    try:
        hal = open_hal()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2

    notifier = sdnotify.Notifier()
    device, updater = build(hal, notifier=notifier)

    # The page is reachable by anyone on the same Wi-Fi, so it is locked. The
    # PIN belongs on the device's own screen next to the setup QR code; until
    # that screen exists, the journal is where to find it.
    auth = Auth(updater.config.data_root,
                log=logging.getLogger("trstenko.auth").info)
    log.info("pairing PIN: %s", auth.pin)

    server = None
    if not args.no_web:
        # Flask on the device when it is installed; the standard library is
        # the fallback, so a broken virtualenv still leaves a way in.
        try:
            from .webui import create_app

            app = create_app(updater, device, auth)

            threading.Thread(
                target=lambda: app.run(host="0.0.0.0", port=args.port),
                daemon=True).start()
        except ImportError:
            server = serve(UpdateApi(updater, device, auth),
                           port=args.port)

    signal.signal(signal.SIGTERM, terminate_handler(device))
    try:
        run(device, notifier)
    finally:
        if server is not None:
            server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

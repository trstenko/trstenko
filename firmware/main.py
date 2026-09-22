"""Entry point on the device.

    python -m firmware.main

This is what the systemd unit runs. It opens the hardware, builds the updater
and the web UI, and hands the loop to `Device`. On a PC it refuses and points
at the emulator, because a firmware that quietly substitutes fake hardware is
a firmware that gets shipped untested.

The voice pipeline is not wired in yet (Phase 2). The device runs without one:
it boots, shows a face, respects the mute switch and the buttons, and serves
the update page. That is deliberately the order -- everything here works before
the first API key exists.
"""

import argparse
import sys

from .device import Device
from .hal import open_hal
from .updates import GitHubReleases, UpdateConfig, UpdateManager
from .webui import Auth, UpdateApi, serve


def build(hal, config=None, log=print):
    config = config or UpdateConfig.load()
    source = GitHubReleases(config.repo, config.token)
    updater = UpdateManager(
        config, source, hal.system,
        on_event=lambda kind, payload: log("update: %s" % kind))
    device = Device(hal, pipeline=None, updater=updater, on_log=log)
    return device, updater


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-web", action="store_true")
    args = ap.parse_args(argv)

    try:
        hal = open_hal()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2

    device, updater = build(hal)

    # The page is reachable by anyone on the same Wi-Fi, so it is locked. The
    # PIN belongs on the device's own screen next to the setup QR code; until
    # that screen exists, the journal is where to find it.
    auth = Auth(updater.config.data_root, log=print)
    print("pairing PIN: %s" % auth.pin)

    server = None
    if not args.no_web:
        # Flask on the device when it is installed; the standard library is
        # the fallback, so a broken virtualenv still leaves a way in.
        try:
            from .webui import create_app

            app = create_app(updater, device, auth)
            import threading

            threading.Thread(
                target=lambda: app.run(host="0.0.0.0", port=args.port),
                daemon=True).start()
        except ImportError:
            server = serve(UpdateApi(updater, device, auth),
                           port=args.port)

    try:
        device.run()
    except KeyboardInterrupt:
        pass
    finally:
        device.close()
        if server is not None:
            server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

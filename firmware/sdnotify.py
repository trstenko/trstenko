"""Telling systemd that the device is alive.

With `Type=notify` and `WatchdogSec=20` in the unit, systemd waits for
`READY=1` before it calls the service started, and restarts it if
`WATCHDOG=1` stops arriving. That is the only thing that notices a process
which is alive and stuck: `Restart=always` handles one that has died.

The ping comes from the main loop's own tick, on purpose. A thread that pings
by itself keeps a frozen device looking healthy, which is the failure this
exists to catch.

This is the whole protocol -- a datagram to the socket named in
`$NOTIFY_SOCKET` -- so there is no dependency. With the variable unset (a PC,
the emulator, a shell) every call does nothing, which is what lets the same
code run everywhere.
"""

import logging
import os
import socket

log = logging.getLogger("trstenko.sdnotify")


class Notifier:
    def __init__(self, address=None, send=None):
        """`address` defaults to `$NOTIFY_SOCKET`. `send(payload)` replaces the
        socket, for a test on a machine whose Python has no unix sockets."""
        if address is None:
            address = os.environ.get("NOTIFY_SOCKET")
        self.address = address or None
        self._send = send
        self._complained = False
        self.sent = 0

    @property
    def enabled(self):
        return bool(self.address) and (
            self._send is not None or hasattr(socket, "AF_UNIX"))

    def ready(self):
        return self._notify(b"READY=1")

    def watchdog(self):
        return self._notify(b"WATCHDOG=1")

    def stopping(self):
        return self._notify(b"STOPPING=1")

    def _notify(self, payload):
        if not self.enabled:
            return False
        try:
            if self._send is not None:
                self._send(payload)
            else:
                self._send_socket(payload)
        except OSError as exc:
            # Never let a missed ping take the loop down with it. Say so once;
            # if they really are not arriving, systemd will say the rest.
            if not self._complained:
                self._complained = True
                log.warning("could not reach systemd: %s", exc)
            return False
        self.sent += 1
        return True

    def _send_socket(self, payload):
        address = self.address
        if address.startswith("@"):
            # An abstract socket: systemd writes it with an @, the kernel
            # wants a leading NUL.
            address = "\0" + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(payload, address)

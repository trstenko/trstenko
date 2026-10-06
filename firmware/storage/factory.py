"""Factory reset: the device as it left the box.

For a device that is being handed to somebody else, or that its owner wants
emptied. It removes (plan 06):

- the database: memories, reminders, usage, the logs, and its backups;
- the secrets file: every API key;
- the tools the owner installed, and what they kept (plan 10);
- the settings, except the update channel, which describes the device and not
  its owner;
- the pairing PIN, which is replaced by a new one;
- the home Wi-Fi, so the device comes back up in setup mode, with the setup
  card on its screen showing the new PIN.

Voices and the installed releases stay: they are the product, not the owner's.

**It happens on the way out, not in the command.** The command only marks the
reset and stops the loop. The wiping is the last thing the shutdown sequence
does, after the turn is cancelled, the workers have stopped and the database is
closed: a file cannot be removed from under a thread that is writing it, and a
half-wiped device that keeps running is worse than either state. Then the
device restarts.

The page asks for the PIN again before it sends the command (plan 07).
"""

import logging
import os
import shutil

from .. import events as ev
from . import DB_NAME
from . import settings as settings_mod

log = logging.getLogger("trstenko.factory")


def wipe(data_root, db=None, settings=None, secrets=None, new_pin=None,
         network=None):
    """Remove the owner's data under `data_root`. Every step is tried even if
    the one before it failed. Returns the names of the steps that failed."""
    failed = []

    def step(name, fn):
        try:
            fn()
        except Exception:
            log.exception("factory reset: %s failed", name)
            failed.append(name)

    def database():
        if db is not None:
            db.close()
        path = os.path.join(data_root, DB_NAME)
        # The WAL and its index hold the latest writes, so they go too.
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass
        shutil.rmtree(os.path.join(data_root, "backups"), ignore_errors=True)

    def keys():
        if secrets is not None:
            secrets.clear()

    def the_settings():
        path = settings.path if settings is not None \
            else settings_mod.default_path(data_root)
        fresh = settings_mod.Settings(path)
        if settings is not None:
            fresh.data["updates"] = dict(settings.data.get("updates") or {})
        fresh.save()

    def installed_tools():
        from ..assistant.extensions import store

        store.wipe(data_root)

    step("database", database)
    step("keys", keys)
    step("tools", installed_tools)
    step("settings", the_settings)
    if new_pin is not None:
        step("pin", new_pin)
    if network is not None:
        step("wifi", network.forget)
    return failed


def wire(device, data_root, new_pin=None, network=None):
    """Give `device` the `factory.reset` command. Call it after everything
    else is wired: its closer has to run last."""
    state = {"pending": False}

    def reset(args):
        if device.turn is not None:
            device.turn.cancel()
            device.turn = None
        state["pending"] = True
        device.log("factory reset requested; restarting")
        device.stop()
        return {"ok": True, "restarting": True}

    def on_close():
        if not state["pending"]:
            return
        failed = wipe(data_root, db=device.db, settings=device.settings,
                      secrets=device.secrets, new_pin=new_pin,
                      network=network)
        log.info("factory reset done%s", " (failed: %s)" % ", ".join(failed)
                 if failed else "")
        device.hal.system.reboot()

    device.command("factory.reset", reset)
    device.add_closer("factory", on_close)
    return state

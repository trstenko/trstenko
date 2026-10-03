"""Scenarios for setup mode: the hotspot, the card on the screen, joining Wi-Fi.

The device is the real one and so is its setup worker thread; only the Wi-Fi is
fake (`network.FakeNetwork`). The worker really runs on its own thread, so a
scenario waits for it with `settle`, which keeps ticking the device.
"""

import json
import os
import time

from firmware import netsetup
from firmware.face import setupcard
from firmware.hal.base import VOL_DOWN, VOL_UP
from firmware.webui import Auth, UpdateApi

from .network import FakeNetwork

SERIAL = "TRS-1-ABCD1234"


class SetupRig:
    def __init__(self, root, name, saved=True):
        from .headless import Rig

        self.rig = Rig(os.path.join(root, name))
        self.device = self.rig.device
        self.net = FakeNetwork(saved=saved)
        self.auth = Auth(os.path.join(root, name), clock=lambda: self.rig.t)
        self.setup = netsetup.wire(
            self.device, self.net, serial=SERIAL, pin=lambda: self.auth.pin,
            new_pin=self.auth.reset_pin)
        self.api = UpdateApi(self.rig.updater, self.device, self.auth,
                             commander=self._pump)
        self.token = self.api.dispatch(
            "POST", "/api/auth", {"pin": self.auth.pin})[1]["token"]

    def _pump(self, cmd):
        self.device.events.post(cmd)
        self.rig.step(0.05)

    def call(self, method, path, body=None):
        return self.api.dispatch(method, path, body, self.token)

    def settle(self, condition, seconds=3.0):
        """Tick until `condition()` holds; the worker needs real time."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.rig.step(0.05)
            if condition():
                return True
            time.sleep(0.005)
        return False

    def hold_both(self):
        hal = self.rig.hal
        hal.press(VOL_DOWN)
        hal.press(VOL_UP)
        self.rig.step(3.5)
        hal.release(VOL_DOWN)
        hal.release(VOL_UP)
        self.rig.step(0.2)

    def close(self):
        self.setup.close()


def _has(image, colour):
    return any(pixel == colour for pixel in image.getdata())


def scenario_setup_mode(report, root):
    """The two-button gesture: a new PIN, a scan, a hotspot with a random
    password, the card, a join that fails and one that works."""
    r = SetupRig(root, "setupmode")
    try:
        setup, net = r.setup, r.net
        r.rig.step(4.0)
        r.settle(lambda: ("saved",) in net.calls)
        r.rig.step(0.2)
        report.check("a device that knows a Wi-Fi boots to the face, not setup",
                     setup.state == netsetup.OFF and net.hotspot is None
                     and r.device.face.current == "face_idle")
        status, out = r.call("POST", "/api/v1/setup/join",
                             {"ssid": "Home", "password": "password1"})
        report.check("joining outside setup is a 409", status == 409, str(out))

        old_pin = r.auth.pin
        r.hold_both()
        ok = r.settle(lambda: setup.state == netsetup.WAITING)
        report.check("holding both volume buttons starts setup", ok,
                     setup.state)
        report.check("the scan comes before the hotspot",
                     net.calls[-2:] == [("scan",), ("hotspot", "Trstenko-1234")],
                     str(net.calls))
        password = net.hotspot[1]
        report.check("the hotspot is named after the serial and has a random "
                     "ten-character password",
                     net.hotspot[0] == "Trstenko-1234" and len(password) == 10
                     and password.isalnum())
        report.check("the gesture makes a new pairing PIN, and the old token "
                     "stops working",
                     r.auth.pin != old_pin
                     and r.call("GET", "/api/v1/setup")[0] == 401)
        r.token = r.api.dispatch("POST", "/api/auth",
                                 {"pin": r.auth.pin})[1]["token"]

        card = setup.card()
        report.check("the card carries the Wi-Fi code, the address and the PIN",
                     card["mode"] == "setup"
                     and card["qr"] == "WIFI:T:WPA;S:Trstenko-1234;P:%s;;"
                     % password and card["url"] == "http://10.42.0.1"
                     and card["pin"] == r.auth.pin)
        pushes = r.rig.hal.panel.pushes
        r.rig.step(1.0)
        frame = r.rig.hal.panel.frame
        report.check("the screen shows the card, sent once and not every frame",
                     _has(frame, setupcard.TEAL)
                     and r.rig.hal.panel.pushes - pushes <= 1,
                     "%d pushes" % (r.rig.hal.panel.pushes - pushes))

        status, out = r.call("GET", "/api/v1/setup")
        text = json.dumps(out) + json.dumps(r.call("GET", "/api/v1/state")[1])
        report.check("the page sees the state and the networks, strongest "
                     "first", status == 200 and out["state"] == "waiting"
                     and [n["ssid"] for n in out["networks"]]
                     == ["Home", "Sosedov WiFi", "Cafe: guests"]
                     and out["hotspot"] == "Trstenko-1234", str(out)[:200])
        report.check("the hotspot password and the PIN are in no answer and "
                     "no log line",
                     password not in text and r.auth.pin not in text
                     and not any(password in line for line in r.rig.logs))

        for body, why in (({"ssid": "", "password": "password1"}, "no name"),
                          ({"ssid": "Home", "password": "short"},
                           "a password too short to be one"),
                          ({"ssid": "x" * 40, "password": "password1"},
                           "a name too long to be one")):
            status, out = r.call("POST", "/api/v1/setup/join", body)
            report.check("a join with %s is a 400" % why,
                         status == 400 and out["error"], str(out))

        net.fail_join = True
        status, out = r.call("POST", "/api/v1/setup/join",
                             {"ssid": "Home", "password": "wrong-password"})
        report.check("a join is accepted at once and done in the background",
                     status == 200 and out["state"] == "joining")
        ok = r.settle(lambda: setup.state == netsetup.WAITING)
        report.check("a join that fails brings the same hotspot back, with "
                     "the reason for the page",
                     ok and net.hotspot == ("Trstenko-1234", password)
                     and "Couldn't join Home" in setup.status()["error"],
                     "%s %s" % (setup.state, setup.error))
        report.check("the Wi-Fi password is in no log line",
                     not any("wrong-password" in line for line in r.rig.logs))

        before = len(net.calls)
        status, out = r.call("POST", "/api/v1/setup/scan")
        ok = r.settle(lambda: not setup.status()["scanning"])
        report.check("scan again drops the hotspot, scans, and raises it",
                     status == 200 and ok and net.calls[before:]
                     == [("hotspot_down",), ("scan",),
                         ("hotspot", "Trstenko-1234")], str(net.calls[before:]))

        net.fail_join = False
        status, out = r.call("POST", "/api/v1/setup/join",
                             {"ssid": "Home", "password": "right-password"})
        ok = r.settle(lambda: setup.state == netsetup.JOINED)
        card = setup.card()
        report.check("a join that works takes the hotspot down and shows "
                     "where the device now is",
                     ok and net.hotspot is None and net.joined == "Home"
                     and card["mode"] == "online"
                     and card["address"] == "192.168.1.57"
                     and card["url"] == "http://trstenko.local", str(card))
        r.rig.step(netsetup.JOINED_HOLD + 2.0)
        pushes = r.rig.hal.panel.pushes
        r.rig.step(1.0)
        report.check("two minutes later the face is back",
                     setup.state == netsetup.OFF and setup.card() is None
                     and r.rig.hal.panel.pushes > pushes
                     and not _has(r.rig.hal.panel.frame, setupcard.PAPER))

        r.hold_both()
        r.settle(lambda: setup.state == netsetup.WAITING)
        report.check("the next setup has a different hotspot password",
                     net.hotspot is not None and net.hotspot[1] != password)
    finally:
        r.close()


def scenario_setup_new_device(report, root):
    """A device with no Wi-Fi saved starts setup by itself, stays healthy
    behind the card, and gives up after fifteen minutes."""
    r = SetupRig(root, "setupnew", saved=False)
    try:
        setup, net = r.setup, r.net
        ok = r.settle(lambda: setup.state == netsetup.WAITING)
        report.check("with no Wi-Fi saved, setup starts at boot", ok,
                     setup.state)
        r.rig.step(6.0)
        report.check("the device behind the card still counts as working, so "
                     "a first release is not rolled back for it",
                     r.device.healthy() and setup.card() is not None)
        state = r.call("GET", "/api/v1/state")[1]
        report.check("Home is told setup is on",
                     state["device"]["setup"]["state"] == "waiting")
        r.rig.step(netsetup.SETUP_TIMEOUT)
        ok = r.settle(lambda: net.hotspot is None)
        report.check("after fifteen minutes with nobody, the hotspot goes "
                     "down and the face returns",
                     ok and setup.state == netsetup.OFF, setup.state)

        # A fault in the network layer must not leave the card up for ever.
        net.start_hotspot = lambda ssid, password: 1 / 0
        setup.begin()
        ok = r.settle(lambda: setup.state == netsetup.OFF)
        report.check("a fault while starting ends setup instead of hanging it",
                     ok and r.device.supervisor.status()["network"]["state"]
                     == "running", setup.state)
    finally:
        r.close()


def scenario_setup_parts(report, root):
    """The pieces with no device in them: nmcli's output, the QR text, the
    card."""
    listed = netsetup.parse_scan(
        "Home:82:WPA2\nHome:40:WPA2\n:55:WPA2\nCafe\\: guests:31:\n"
        "Open:12:--\nbroken line\n")
    report.check("a scan list keeps the strongest of each name, drops hidden "
                 "ones, and reads an escaped colon",
                 listed == [{"ssid": "Home", "signal": 82, "secure": True},
                            {"ssid": "Cafe: guests", "signal": 31,
                             "secure": False},
                            {"ssid": "Open", "signal": 12, "secure": False}],
                 str(listed))
    report.check("the Wi-Fi code escapes what would break it",
                 netsetup.wifi_qr('a;b', 'p:"q') ==
                 'WIFI:T:WPA;S:a\\;b;P:p\\:\\"q;;')
    seen = {netsetup.new_password() for _ in range(50)}
    report.check("hotspot passwords are random and easy to type",
                 len(seen) == 50 and all(
                     len(p) == 10 and not set(p) & set("0O1lI") for p in seen))

    card = {"mode": "setup", "ssid": "Trstenko-1234", "password": "abcdefgh23",
            "url": "http://10.42.0.1", "pin": "123456", "qr": "WIFI:x"}
    grid = [[(x + y) % 2 == 0 for x in range(25)] for y in range(25)]
    with_code = setupcard.draw(card, matrix=lambda text: grid)
    plain = setupcard.draw(card, matrix=lambda text: None)
    report.check("the card is the size of the panel, with the code on it",
                 with_code.size == (320, 240)
                 and _has(with_code, setupcard.PAPER)
                 and with_code.getpixel((160, 20)) != setupcard.DARK)
    report.check("without the qrcode package it shows the hotspot as text "
                 "rather than nothing",
                 plain.size == (320, 240) and plain.tobytes()
                 != with_code.tobytes() and _has(plain, setupcard.TEAL))
    for mode in ("starting", "joining", "online"):
        image = setupcard.draw({"mode": mode, "ssid": "Home", "lang": "sl",
                                "url": "http://trstenko.local",
                                "address": "192.168.1.57", "pin": "123456"})
        report.check("the %s card draws" % mode, image.size == (320, 240)
                     and image.getbbox() is not None)
    same = setupcard.cached(dict(card))
    report.check("an unchanged card is not drawn again",
                 setupcard.cached(dict(card)) is same
                 and setupcard.cached(None) is None)


def scenario_factory_reset(report, root):
    """Factory reset: asks for the PIN, wipes on the way out, restarts, and
    the device that comes back is a new one in setup mode."""
    from firmware.storage import DB_NAME, factory
    from firmware.storage import settings as settings_mod
    from firmware.storage.secrets import Secrets, default_path

    from .headless import SchedRig

    key = "sk-test-factory-reset-key-000111222333"
    rig = SchedRig(root, "factory")
    data = rig.data
    device = rig.device
    secrets = Secrets.load(default_path(data))
    secrets.set_key("openai", key)
    device.secrets = secrets
    net = FakeNetwork(saved=True)
    auth = Auth(data, clock=rig.clock.wall)
    netsetup.wire(device, net, serial=SERIAL, pin=lambda: auth.pin,
                  new_pin=auth.reset_pin, check=False)
    factory.wire(device, data, new_pin=auth.reset_pin, network=net)

    def pump(cmd):
        device.events.post(cmd)
        rig.tick()
    api = UpdateApi(None, device, auth, commander=pump)
    token = api.dispatch("POST", "/api/auth", {"pin": auth.pin})[1]["token"]

    def call(path, body=None, method="POST"):
        return api.dispatch(method, path, body, token)

    call("/api/v1/reminders", {"text": "tea", "in_seconds": 600})
    call("/api/v1/settings", {"owner": {"name": "Maj"}}, "PATCH")
    rig.settings.data["updates"]["channel"] = "beta"
    rig.settings.save()
    rig.db.conn().execute(
        "INSERT INTO memories (text, created_at, source) VALUES "
        "('likes tea', 1, 'web')")
    os.makedirs(os.path.join(data, "backups"), exist_ok=True)
    open(os.path.join(data, "backups", "trstenko-v1.db"), "wb").close()
    db_path = os.path.join(data, DB_NAME)
    old_pin = auth.pin

    status, out = call("/api/v1/factory-reset", {})
    report.check("a factory reset without the PIN is refused",
                 status == 403 and out.get("needs_pin"), str(out))
    status, out = call("/api/v1/factory-reset", {"pin": "000000"
                                                 if old_pin != "000000"
                                                 else "111111"})
    report.check("and with the wrong PIN", status == 403 and device.running)
    status, out = call("/api/v1/factory-reset", None, "GET")
    report.check("and it only answers a POST", status == 405)
    report.check("nothing has been touched by the refusals",
                 os.path.exists(db_path) and secrets.has_key("openai")
                 and rig.hal.system.rebooted == 0)

    status, out = call("/api/v1/factory-reset", {"pin": old_pin})
    report.check("with the PIN it is accepted, and the loop is told to stop",
                 status == 200 and out["restarting"] and not device.running,
                 str(out))
    report.check("the wiping waits for the shutdown: the files are still "
                 "there while workers could be writing them",
                 os.path.exists(db_path))
    device.close()

    left = sorted(os.listdir(data))
    with open(settings_mod.default_path(data), encoding="utf-8") as fh:
        saved = json.load(fh)
    report.check("the database, its WAL and its backups are gone",
                 not any(n.startswith(DB_NAME) for n in left)
                 and "backups" not in left, str(left))
    report.check("every key is gone, from the file and from memory",
                 not os.path.exists(default_path(data))
                 and not secrets.has_key("openai"))
    report.check("the settings are the defaults again, but the update "
                 "channel is kept",
                 saved["owner"]["name"] == ""
                 and saved["updates"]["channel"] == "beta", str(saved)[:120])
    report.check("there is a new PIN and the old token is dead",
                 auth.pin != old_pin and not auth.check(token))
    report.check("the Wi-Fi is forgotten and the device restarts",
                 ("forget",) in net.calls and not net.is_saved
                 and rig.hal.system.rebooted == 1, str(net.calls))
    text = "\n".join(rig.logs)
    report.check("the key is in no log line", key not in text)
    if rig in SchedRig.open_rigs:
        SchedRig.open_rigs.remove(rig)

    # What comes back up: a new device, with nothing in it, asking for Wi-Fi.
    again = SchedRig(root, "factory")
    try:
        setup = netsetup.wire(again.device, net, serial=SERIAL,
                              pin=lambda: auth.pin, new_pin=auth.reset_pin)
        end = time.monotonic() + 3.0
        while time.monotonic() < end and setup.state != netsetup.WAITING:
            again.tick()
            time.sleep(0.005)
        memories = again.db.conn().execute(
            "SELECT COUNT(*) FROM memories").fetchone()[0]
        report.check("after the restart it is an empty device in setup mode, "
                     "showing the new PIN",
                     setup.state == netsetup.WAITING and memories == 0
                     and again.scheduler.list() == []
                     and setup.card()["pin"] == auth.pin, setup.state)
        setup.close()
    finally:
        again.close()

    # An ordinary shutdown wipes nothing.
    plain = SchedRig(root, "factoryplain")
    factory.wire(plain.device, plain.data, network=FakeNetwork())
    plain.device.close()
    report.check("a device that was not reset keeps everything on shutdown",
                 os.path.exists(os.path.join(plain.data, DB_NAME))
                 and plain.hal.system.rebooted == 0)
    plain.close()

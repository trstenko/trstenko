"""Setup mode: the device's own hotspot, and joining the owner's Wi-Fi.

A new device has no way in. It knows no Wi-Fi, so no phone can reach its page,
so nobody can tell it a Wi-Fi. Setup mode breaks the circle (plan 07): the
device raises a hotspot of its own, shows a QR code for it on the screen next
to the pairing PIN, and the page asks which network to join.

    off -> starting -> waiting -> joining -> joined -> off
                          ^          |
                          +-- the join failed, the hotspot is back

**When it starts.** At boot with no home Wi-Fi saved, and when both volume
buttons are held (`SETUP_COMBO`). The buttons also make a new pairing PIN, so a
device handed to someone else stops answering to whoever paired with it before.

**Nothing here blocks the main loop.** `nmcli` takes seconds: a scan, raising
the hotspot, and up to half a minute to join. All of that runs on one
long-lived worker thread, which posts a `NetSetup` event when it is done. The
state lives on the main thread and only changes there (plan 01).

**The scan comes first.** The Pi's one radio is unreliable at scanning while it
is also an access point, so the list is taken before the hotspot goes up and
the page shows that saved list. "Scan again" drops the hotspot for a moment.

**No default credentials.** The hotspot's password is random and new every
time setup starts, which EN 18031 requires. It is shown on the device's screen
and nowhere else: not in the log, not in the page's API.

**The address and the PIN, on request.** The page asks for the pairing PIN on
every new visit, and only the screen may show it. A long press of a volume
button puts the device's address and its PIN on the screen for half a minute.
Looking the address up is an `nmcli` call, so that too goes through the worker.

**Passwords are not kept here.** The home Wi-Fi password goes from the page to
`nmcli`, which stores it in NetworkManager's own files. It is never logged and
never sent back.

The real commands are in `NmcliNetwork`. The emulator and the tests replace it
with a fake that records what was asked of it (`tools/emulator/network.py`).
"""

import logging
import queue
import secrets
import threading

from . import events as ev

log = logging.getLogger("trstenko.netsetup")

OFF = "off"
STARTING = "starting"
WAITING = "waiting"
JOINING = "joining"
JOINED = "joined"

# NetworkManager's shared mode gives the device this address (plan 07).
HOTSPOT_URL = "http://10.42.0.1"
HOME_URL = "http://trstenko.local"

# Plan 07: with nobody connected, setup gives up and goes back to the face.
SETUP_TIMEOUT = 15 * 60.0
# How long the address stays on the screen after joining.
JOINED_HOLD = 120.0
# How long the address and the PIN stay up after a long press. Long enough to
# walk to a laptop and type six digits.
INFO_HOLD = 30.0
JOIN_TIMEOUT = 30.0
COMMAND_TIMEOUT = 20.0

PASSWORD_LENGTH = 10
# No 0/O or 1/l/I: the owner may have to type it when the camera will not
# read the code.
PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"

MAX_NETWORKS = 20
MAX_SSID = 32            # bytes, by the Wi-Fi standard
MAX_WIFI_PASSWORD = 63
MIN_WIFI_PASSWORD = 8
WORKER = "network"

# What the device says aloud during setup, as well as showing it. Slovenian is
# unreviewed by a native speaker, like the rest of the device's Slovenian.
LINES = {
    "en": {"joining": "Connecting to {ssid}.",
           "joined": "I'm online.",
           "join_failed": "I couldn't join {ssid}. Check the password and "
                          "try again."},
    "sl": {"joining": "Povezujem se z omre\u017ejem {ssid}.",
           "joined": "Povezan sem.",
           "join_failed": "Z omre\u017ejem {ssid} se nisem mogel povezati. "
                          "Preverite geslo in poskusite znova."},
}
POLL = 0.5               # how often the idle worker looks at its stop flag


class NetworkError(Exception):
    """The network would not do it. The message is for the owner."""


class Network:
    """What setup needs from the machine's Wi-Fi. Every call may block and is
    only ever made from the worker thread."""

    def saved(self):
        """Is a home Wi-Fi already known? False means a new device."""
        raise NotImplementedError

    def scan(self):
        """[{"ssid", "signal" (0-100), "secure" (bool)}], strongest first."""
        raise NotImplementedError

    def start_hotspot(self, ssid, password):
        raise NotImplementedError

    def stop_hotspot(self):
        raise NotImplementedError

    def join(self, ssid, password):
        """Join and remember it. Raises `NetworkError` if it will not."""
        raise NotImplementedError

    def address(self):
        """The device's address on the network it is on, or None."""
        raise NotImplementedError

    def forget(self):
        """Forget every saved Wi-Fi. A factory reset calls this, from the
        shutdown path, so the device comes back up in setup mode."""
        raise NotImplementedError


class NmcliNetwork(Network):
    """NetworkManager, which Raspberry Pi OS Bookworm uses, through `nmcli`.

    The service runs sandboxed, so NetworkManager has to be told this user may
    manage connections: `firmware/deploy/50-trstenko-network.rules`.

    TODO on the board: none of this has run on a Pi. Check the hotspot comes up
    on `wlan0` with the address in `HOTSPOT_URL`, that a phone joins it from
    the QR code, how long a failed join really takes, and that stopping the
    hotspot lets the saved home Wi-Fi reconnect by itself.
    """

    HOTSPOT_NAME = "trstenko-setup"

    def __init__(self, interface="wlan0", nmcli="nmcli"):
        self.interface = interface
        self.nmcli = nmcli

    def _exec(self, args, timeout):
        """Runs nmcli. Returns the finished process; logs nothing, so it is
        safe for commands that carry a password."""
        import subprocess

        try:
            return subprocess.run((self.nmcli,) + tuple(args),
                                  capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            raise NetworkError("The Wi-Fi did not answer in time.")
        except OSError as exc:
            raise NetworkError("The Wi-Fi could not be reached (%s)."
                               % type(exc).__name__)

    def _run(self, *args, timeout=COMMAND_TIMEOUT):
        """A command with no password in it: nmcli's own words go to the log
        when it fails."""
        done = self._exec(args, timeout)
        if done.returncode != 0:
            log.warning("nmcli %s failed: %s", args[0] if args else "",
                        (done.stderr or "").strip()[:200])
            raise NetworkError("The Wi-Fi refused that.")
        return done.stdout

    def _run_secret(self, args, timeout=COMMAND_TIMEOUT):
        """A command that carries a password: the log says only that it
        failed, never the command or nmcli's output."""
        done = self._exec(args, timeout)
        if done.returncode != 0:
            log.warning("nmcli failed on a command that carried a password; "
                        "its output is not logged")
            raise NetworkError("The Wi-Fi refused that.")
        return done.stdout

    def saved(self):
        out = self._run("-t", "-f", "TYPE,NAME", "connection", "show")
        return any(line.startswith("802-11-wireless:")
                   and not line.endswith(":" + self.HOTSPOT_NAME)
                   for line in out.splitlines())

    def scan(self):
        out = self._run("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi",
                        "list", "ifname", self.interface, "--rescan", "yes")
        return parse_scan(out)

    def start_hotspot(self, ssid, password):
        self._run_secret(("device", "wifi", "hotspot", "ifname",
                          self.interface, "con-name", self.HOTSPOT_NAME,
                          "ssid", ssid, "password", password))

    def stop_hotspot(self):
        try:
            self._run("connection", "down", self.HOTSPOT_NAME)
        except NetworkError:
            pass        # it was not up
        try:
            # Deleted, not kept: its password must not outlive this setup.
            self._run("connection", "delete", self.HOTSPOT_NAME)
        except NetworkError:
            pass

    def join(self, ssid, password):
        args = ["device", "wifi", "connect", ssid, "ifname", self.interface]
        if password:
            args += ["password", password]
        try:
            self._run_secret(args, timeout=JOIN_TIMEOUT)
        except NetworkError:
            raise NetworkError("Couldn't join %s: wrong password or out of "
                               "range." % ssid)

    def forget(self):
        out = self._run("-t", "-f", "TYPE,UUID", "connection", "show")
        for line in out.splitlines():
            kind, _, uuid = line.partition(":")
            if kind == "802-11-wireless" and uuid:
                self._run("connection", "delete", "uuid", uuid)

    def address(self):
        try:
            out = self._run("-t", "-f", "IP4.ADDRESS", "device", "show",
                            self.interface)
        except NetworkError:
            return None
        for line in out.splitlines():
            value = line.partition(":")[2].split("/")[0].strip()
            if value:
                return value
        return None


def parse_scan(text):
    """`nmcli -t -f SSID,SIGNAL,SECURITY` into the list `Network.scan` returns.
    Terse mode escapes a colon inside a name as `\\:`. Hidden networks (no
    name) are left out, and a name seen on several access points is listed
    once, at its strongest."""
    best = {}
    for line in text.splitlines():
        fields, current, escaped = [], "", False
        for ch in line:
            if escaped:
                current += ch
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == ":":
                fields.append(current)
                current = ""
            else:
                current += ch
        fields.append(current)
        if len(fields) < 3 or not fields[0]:
            continue
        try:
            signal = max(0, min(100, int(fields[1])))
        except ValueError:
            signal = 0
        secure = fields[2].strip() not in ("", "--")
        found = best.get(fields[0])
        if found is None or signal > found["signal"]:
            best[fields[0]] = {"ssid": fields[0], "signal": signal,
                               "secure": secure}
    return sorted(best.values(),
                  key=lambda n: (-n["signal"], n["ssid"]))[:MAX_NETWORKS]


def new_password():
    return "".join(secrets.choice(PASSWORD_ALPHABET)
                   for _ in range(PASSWORD_LENGTH))


def wifi_qr(ssid, password):
    """The text a phone's camera joins a network from. `\\ ; , : "` in a name
    or a password are escaped with a backslash."""
    def esc(value):
        return "".join("\\" + c if c in '\\;,:"' else c for c in value)
    return "WIFI:T:WPA;S:%s;P:%s;;" % (esc(ssid), esc(password))


class NetSetup:
    """The state of setup mode, on the main thread, and the worker that does
    the slow parts. Build it with `wire`."""

    def __init__(self, network, post, mono, serial="", pin=None, new_pin=None,
                 on_log=None, password=new_password, show_pin=None,
                 say=None):
        self.network = network
        self.post = post
        self.mono = mono
        self.serial = serial
        self._pin = pin or (lambda: "")
        self._new_pin = new_pin
        # The owner may keep the PIN off the screen (settings, security).
        self._show_pin = show_pin or (lambda: True)
        # `say(key, ssid=...)` speaks one of LINES. The screen says the same
        # things, so a device with no voice loses nothing.
        self._say = say or (lambda key, **kw: None)
        self.on_log = on_log or (lambda line: None)
        self._password = password

        self.state = OFF
        self.ssid = ""
        self.password = ""
        self.networks = []
        self.error = ""
        self.joining = ""           # the network being joined, or just joined
        self.address = None
        self._deadline = 0.0
        self._scanning = False
        self._info_until = 0.0

        self._jobs = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._thread = None

    # ---- the worker thread ----

    def start(self):
        """Begin the worker. The supervisor calls this again if it dies."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._work, name=WORKER,
                                        daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)

    def _work(self):
        try:
            while not self._stop.is_set():
                try:
                    job = self._jobs.get(timeout=POLL)
                except queue.Empty:
                    continue
                self._do(*job)
        finally:
            if not self._stop.is_set():
                self.post(ev.Event(ev.WORKER_DIED, data=WORKER))

    def _tell(self, what, **data):
        data["what"] = what
        self.post(ev.Event(ev.NET_SETUP, data=data))

    def _do(self, what, *args):
        net = self.network
        try:
            if what == "check":
                self._tell("checked", saved=bool(net.saved()))
            elif what == "open":
                ssid, password = args
                networks = self._scan()
                net.start_hotspot(ssid, password)
                self._tell("opened", networks=networks)
            elif what == "scan":
                ssid, password = args
                net.stop_hotspot()
                networks = self._scan()
                net.start_hotspot(ssid, password)
                self._tell("scanned", networks=networks)
            elif what == "join":
                self._join(*args)
            elif what == "close":
                net.stop_hotspot()
                self._tell("closed")
            elif what == "address":
                self._tell("address", address=net.address())
        except NetworkError as exc:
            self._tell("failed", during=what, error=str(exc))
        except Exception:
            # Ours, not the owner's. The detail goes to the log; the screen
            # must not be left on a card nothing will ever take down.
            log.exception("setup step %s failed", what)
            self._tell("failed", during=what,
                       error="Setup stopped because of a fault.")

    def _scan(self):
        try:
            return self.network.scan()
        except NetworkError:
            # A hotspot with an empty list is still a way in: the owner can
            # scan again from the page.
            return []

    def _join(self, ssid, password, hot_ssid, hot_password):
        net = self.network
        net.stop_hotspot()
        try:
            net.join(ssid, password)
        except NetworkError as exc:
            # Back to where the phone can reach the device and read why.
            try:
                net.start_hotspot(hot_ssid, hot_password)
            except NetworkError:
                log.warning("the hotspot did not come back after a failed "
                            "join")
            self._tell("join_failed", error=str(exc))
            return
        self._tell("joined", address=net.address())

    def _queue(self, *job):
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            raise ev.CommandError("Setup is busy. Try again in a moment.",
                                  ev.UNAVAILABLE)

    # ---- the main thread ----

    @property
    def active(self):
        return self.state != OFF

    def check_saved(self):
        """At boot: ask whether a home Wi-Fi is known. Setup starts by itself
        when the answer is no."""
        self._queue("check")

    def begin(self, now=None, reset_pin=False):
        """Start setup mode. Does nothing if it is already running."""
        if self.state not in (OFF, JOINED):
            return False
        now = self.mono() if now is None else now
        if reset_pin and self._new_pin is not None:
            self._new_pin()
        self.ssid = "Trstenko-%s" % (self.serial[-4:] or "0000")
        self.password = self._password()
        self.error = ""
        self.joining = ""
        self.address = None
        self.networks = []
        self._queue("open", self.ssid, self.password)
        self.state = STARTING
        self._deadline = now + SETUP_TIMEOUT
        self.on_log("setup: starting the hotspot %s" % self.ssid)
        return True

    def scan(self):
        if self.state != WAITING or self._scanning:
            raise ev.CommandError("Setup is not ready for that yet.",
                                  ev.CONFLICT)
        self._queue("scan", self.ssid, self.password)
        self._scanning = True
        return self.status()

    def join(self, ssid, password, now=None):
        if self.state != WAITING or self._scanning:
            raise ev.CommandError("Setup is not ready for that yet.",
                                  ev.CONFLICT)
        if not isinstance(ssid, str) or not ssid \
                or len(ssid.encode("utf-8")) > MAX_SSID:
            raise ev.CommandError("Choose a network to join.")
        password = "" if password is None else password
        if not isinstance(password, str) or len(password) > MAX_WIFI_PASSWORD \
                or (password and len(password) < MIN_WIFI_PASSWORD):
            raise ev.CommandError("A Wi-Fi password is 8 to 63 characters.")
        self._queue("join", ssid, password, self.ssid, self.password)
        self.state = JOINING
        self.joining = ssid
        self.error = ""
        self._deadline = (self.mono() if now is None else now) + SETUP_TIMEOUT
        self.on_log("setup: joining a network")
        self._say("joining", ssid=ssid)
        return self.status()

    def show_info(self):
        """Put the address and the PIN on the screen for a while. During setup
        the card already shows the PIN, so there is nothing to do."""
        if self.state != OFF:
            return False
        try:
            self._queue("address")
        except ev.CommandError:
            return False
        return True

    def stop(self):
        """Give up: hotspot down, back to the face."""
        if self.state in (STARTING, WAITING):
            self._queue("close")
        self._off()

    def _off(self):
        self.state = OFF
        self.password = ""
        self._scanning = False

    def handle(self, event):
        """A `NetSetup` event from the worker."""
        data = event.data or {}
        what = data.get("what")
        now = self.mono()
        if what == "checked":
            if not data.get("saved") and self.state == OFF:
                self.on_log("setup: no Wi-Fi is saved")
                self.begin(now)
        elif what == "opened" and self.state == STARTING:
            self.networks = data.get("networks") or []
            self.state = WAITING
            self._deadline = now + SETUP_TIMEOUT
            self.on_log("setup: the hotspot is up, %d networks in range"
                        % len(self.networks))
        elif what == "scanned":
            self._scanning = False
            if self.state == WAITING:
                self.networks = data.get("networks") or []
        elif what == "joined" and self.state == JOINING:
            self.address = data.get("address")
            self.state = JOINED
            self.password = ""
            self._deadline = now + JOINED_HOLD
            self.on_log("setup: online at %s" % (self.address or "?"))
            self._say("joined", ssid=self.joining)
        elif what == "join_failed" and self.state == JOINING:
            self.error = data.get("error") or "Couldn't join that network."
            self.state = WAITING
            self._deadline = now + SETUP_TIMEOUT
            self.on_log("setup: the join failed")
            self._say("join_failed", ssid=self.joining)
        elif what == "address":
            if self.state == OFF:
                self.address = data.get("address")
                self._info_until = now + INFO_HOLD
        elif what == "failed":
            self._scanning = False
            self.on_log("setup: %s failed: %s" % (data.get("during"),
                                                  data.get("error")))
            if data.get("during") == "scan" and self.state == WAITING:
                self.error = data.get("error") or ""
            elif data.get("during") == "address":
                # No address to show, but the PIN is still worth showing.
                if self.state == OFF:
                    self.address = None
                    self._info_until = now + INFO_HOLD
            elif data.get("during") != "check":
                self._off()

    def tick(self, now):
        if self.state in (WAITING, STARTING) and now >= self._deadline:
            self.on_log("setup: nobody came; back to the face")
            self.stop()
        elif self.state == JOINED and now >= self._deadline:
            self._off()

    # ---- what others see ----

    def card(self):
        """What the screen shows instead of the face, or None for the face."""
        if self.state == STARTING:
            return {"mode": "starting"}
        if self.state == WAITING:
            return {"mode": "setup", "ssid": self.ssid,
                    "password": self.password, "url": HOTSPOT_URL,
                    "pin": self._pin(), "qr": wifi_qr(self.ssid,
                                                      self.password)}
        if self.state == JOINING:
            return {"mode": "joining", "ssid": self.joining}
        if self.state == JOINED:
            return {"mode": "online", "url": HOME_URL,
                    "address": self.address, "pin": self._shown_pin()}
        if self.mono() < self._info_until:
            return {"mode": "info", "url": HOME_URL,
                    "address": self.address, "pin": self._shown_pin()}
        return None

    def _shown_pin(self):
        """The PIN for a card, or None when the owner has hidden it. The setup
        card does not ask: it always shows the PIN (see `card`)."""
        return self._pin() if self._show_pin() else None

    def status(self):
        """For the page. Never a password, and never the PIN."""
        return {"state": self.state, "hotspot": self.ssid if self.active
                else "", "networks": list(self.networks),
                "scanning": self._scanning, "joining": self.joining,
                "error": self.error, "address": self.address,
                "home_url": HOME_URL}


def wire(device, network, serial="", pin=None, new_pin=None, check=True):
    """Attach setup mode to `device`: the worker, the step, the commands the
    page uses, the screen card and the two-button gesture. `pin` returns the
    pairing PIN for the card and `new_pin` makes a fresh one."""
    from .face import setupcard

    def show_pin():
        if device.settings is None:
            return True
        return bool(device.settings.get("security", "show_pin"))

    def say(key, **words):
        # Through the announcer, as a one-off notice: it waits for a quiet
        # moment and uses whatever voice the device has.
        if device.announcer is None:
            return
        lines = LINES.get(device.language(), LINES["en"])
        device.announcer.queue.append(("notice", lines[key].format(**words)))

    setup = NetSetup(network, device.events.post, device.mono, serial=serial,
                     pin=pin, new_pin=new_pin, on_log=device.log,
                     show_pin=show_pin, say=say)
    device.netsetup = setup
    device.on(ev.NET_SETUP, setup.handle)
    device.add_step("netsetup", setup.tick, every=1.0)
    device.add_closer("netsetup", setup.close, first=True)
    device.on_setup = lambda: setup.begin(reset_pin=True)
    device.on_info = setup.show_info
    def card():
        shown = setup.card()
        if shown is not None:
            shown["lang"] = device.language()
        return setupcard.cached(shown)

    device.set_card(card)
    device.status_extra["setup"] = lambda: {"state": setup.state}

    def need_dict(args):
        if not isinstance(args, dict):
            raise ev.CommandError("send the network as an object")
        return args

    device.command("setup.status", lambda args: setup.status())
    device.command("setup.scan", lambda args: setup.scan())
    device.command("setup.join", lambda args: setup.join(
        need_dict(args).get("ssid"), need_dict(args).get("password")))
    device.supervisor.add(WORKER, setup.start, device.mono())
    if check:
        setup.check_saved()
    return setup

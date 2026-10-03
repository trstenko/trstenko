"""A Wi-Fi you control, for setup mode in the emulator and the scenarios.

It records what was asked of it, in order, so a scenario can show that the
scan came before the hotspot and that a failed join brought the hotspot back.
`fail_join` makes the next join fail the way a wrong password does. Nothing
here touches the PC's own network.
"""

from firmware.netsetup import Network, NetworkError

NEARBY = (
    {"ssid": "Home", "signal": 82, "secure": True},
    {"ssid": "Sosedov WiFi", "signal": 47, "secure": True},
    {"ssid": "Cafe: guests", "signal": 31, "secure": False},
)


class FakeNetwork(Network):
    def __init__(self, saved=True, nearby=NEARBY):
        self.is_saved = saved
        self.nearby = [dict(n) for n in nearby]
        self.fail_join = False
        self.calls = []             # ("scan",), ("hotspot", ssid), ...
        self.hotspot = None         # (ssid, password) while it is up
        self.joined = None
        self.passwords = []         # every hotspot password it was given

    def saved(self):
        self.calls.append(("saved",))
        return self.is_saved

    def scan(self):
        self.calls.append(("scan",))
        return [dict(n) for n in self.nearby]

    def start_hotspot(self, ssid, password):
        self.calls.append(("hotspot", ssid))
        self.passwords.append(password)
        self.hotspot = (ssid, password)

    def stop_hotspot(self):
        self.calls.append(("hotspot_down",))
        self.hotspot = None

    def join(self, ssid, password):
        self.calls.append(("join", ssid))
        if self.fail_join:
            raise NetworkError("Couldn't join %s: wrong password or out of "
                               "range." % ssid)
        self.joined = ssid
        self.is_saved = True

    def address(self):
        return "192.168.1.57" if self.joined else None

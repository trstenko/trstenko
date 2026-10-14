# Putting Trstenko on a card

First install is by hand, once per device. After that the device updates
itself — see [../updates/README.md](../updates/README.md).

## Layout

```
/opt/trstenko/                 replaced by updates
  releases/0.4.0/              one unpacked release, with its own venv
  current -> releases/0.4.0    what systemd runs
  bin/bootcheck.py             the rollback, kept out of releases/
  base.json                    which SD image this card was written from
  pending-confirm.json         present only while a release is on trial

/var/lib/trstenko/             never touched by an update
  settings.json                API keys, provider, channel
  trstenko.db                  memory and reminders
  updates/state.json           deferrals, skipped versions
```

The split is the point. A rollback returns the code and leaves everything the
user told the device alone.

## First install

```sh
sudo mkdir -p /opt/trstenko/releases/0.0.0 /opt/trstenko/bin /var/lib/trstenko
sudo cp -r firmware /opt/trstenko/releases/0.0.0/
sudo cp firmware/updates/bootcheck.py firmware/updates/boot.py /opt/trstenko/bin/
sudo ln -sfn releases/0.0.0 /opt/trstenko/current

sudo python3 -m venv /opt/trstenko/releases/0.0.0/venv
sudo /opt/trstenko/releases/0.0.0/venv/bin/pip install \
    -r /opt/trstenko/releases/0.0.0/firmware/requirements-pi.txt

echo '{"base": 1}' | sudo tee /opt/trstenko/base.json

# The app's own user. It owns its two directories, except bin/, which only
# root may change (the boot units run what is in it).
sudo useradd --system --home-dir /var/lib/trstenko --shell /usr/sbin/nologin \
    --groups audio,gpio,spi,video trstenko
sudo chown -R trstenko:trstenko /opt/trstenko /var/lib/trstenko
sudo chown -R root:root /opt/trstenko/bin
sudo chmod 0755 /opt/trstenko/bin
sudo chmod 0700 /var/lib/trstenko

sudo cp firmware/deploy/*.service firmware/deploy/*.socket /etc/systemd/system/
# Lets the service manage Wi-Fi for setup mode (firmware/netsetup.py), restart
# itself after an update, and reboot or power off from the button.
sudo cp firmware/deploy/50-trstenko-network.rules /etc/polkit-1/rules.d/
# Makes a phone open the setup page by itself after joining the hotspot.
sudo mkdir -p /etc/NetworkManager/dnsmasq-shared.d
sudo cp firmware/deploy/trstenko-captive.conf /etc/NetworkManager/dnsmasq-shared.d/
sudo systemctl enable --now trstenko-tool.socket
sudo systemctl enable --now trstenko-bootcheck.service trstenko.service
```

`base.json` is the version of the SD image, not of the firmware. Bump it when
you build a card with a newer kernel, a newer Python, or new apt packages, and
set `requires_base` in any release that depends on them. A release asking for
a base newer than the card has is refused with an explanation instead of being
half-applied — see the limit below.

## Watching it

```sh
journalctl -u trstenko -f
journalctl -u trstenko-bootcheck -b        # what happened at the last boot
cat /opt/trstenko/pending-confirm.json     # only exists during a trial
readlink /opt/trstenko/current             # the running release
```

## The units

| | |
|---|---|
| `trstenko-bootcheck.service` | Runs **before** the app, every boot. Counts the attempt, rolls back past the limit. Uses the system Python and the copy in `bin/`, because it has to work when the release is what is broken. |
| `trstenko.service` | The app. `ExecStart` goes through `current`, so a rollback takes effect on the next start with nothing to reconfigure. |
| `trstenko-rollback.service` | `OnFailure` for the app: five crashes in a minute and this rolls back and restarts, without waiting for a reboot nobody is going to perform. |
| `trstenko-tool.socket`, `trstenko-tool@.service` | Third-party tools (plan 10). Each call is one connection to the socket and runs in a unit of its own: a throwaway user, no network, nothing readable but Python, the firmware's code and the installed tools, and caps on memory, processes and time. |

All of them run as the `trstenko` user or a throwaway one, never as root. The
one exception is the rollback unit's final `systemctl restart`, a fixed
command marked `+` in the unit.

TODO on the board: start a tool call and check `systemd-analyze security
trstenko-tool@…`; check the app can drive the display, the sound card, the
buttons and Wi-Fi as `trstenko`; check an update restarts it and the power
button reboots it.

## What updates cannot do

Updates replace `/opt/trstenko`. They do not touch the kernel, apt packages,
the system Python, ALSA or NetworkManager configuration, or anything else on
the card.

A release that needs any of those must set `requires_base` above what the card
carries. The device then refuses it and says the card needs reflashing, which
for the handful of units this project builds is an honest answer. It stops
being one somewhere past fifty devices in other people's hands, and the fix at
that point is A/B root partitions (RAUC or SWUpdate) — which has to be decided
before the cards are written, not after.

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

sudo cp firmware/deploy/*.service /etc/systemd/system/
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

## The three units

| | |
|---|---|
| `trstenko-bootcheck.service` | Runs **before** the app, every boot. Counts the attempt, rolls back past the limit. Uses the system Python and the copy in `bin/`, because it has to work when the release is what is broken. |
| `trstenko.service` | The app. `ExecStart` goes through `current`, so a rollback takes effect on the next start with nothing to reconfigure. |
| `trstenko-rollback.service` | `OnFailure` for the app: five crashes in a minute and this rolls back and restarts, without waiting for a reboot nobody is going to perform. |

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

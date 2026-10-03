# Trstenko

Code for the Trstenko product (Moje podjetje project).

Everything that is not code lives one level up, outside version control:
**`../docs/`** for the plan, the costing and the order, **`../cad/`** for the
enclosure and **`../assets/`** for the brand and the renders. Start at
`../docs/README.md`.

## The one rule about layout

**`firmware/` is what gets copied onto the robot. Nothing else is.**

If a module needs a window, a keyboard, an After Effects export, or a folder
that only exists on a development machine, it goes in `tools/`. That includes
things that look like firmware: the face preview draws the real face with the
real renderer and still belongs in `tools/`, because it needs tkinter and the
robot has no display server.

The rule is checked rather than remembered:

    python -m tools.check_layout

It fails if anything under `firmware/` imports `tools`, imports tkinter,
reads a path inside `docs/`, or imports a third-party package at module level
where a late import would let the device boot without it.

```
firmware/            on the robot
  main.py            systemd runs this
  device.py          the loop: events in, a face out
  pipeline.py        the interface the voice loop will implement
  hal/               base.py is the contract, pi.py is the board
  face/              the face, plus data/faces.json baked from the AE export
  updates/           checking, preflight, download, verify, install, roll back
                     signing.py verifies who made a release (Ed25519)
  deploy/            systemd units and the first-install steps
  webui/             the page the device serves over its own hotspot
  VERSION            what the updater compares releases against

tools/               never on the robot
  emulator/          a Trstenko made of nothing
  face/              the AE importer, the bake step, the desktop preview
  release.py         keygen, build a bundle, sign it
  check_layout.py    enforces the rule above
```

## Run it without any hardware

    pip install -r tools/requirements.txt
    python -m tools.emulator

The emulator runs the real `Device`, the real face and the real update manager
against emulated hardware, and gives you the things you otherwise need a
soldered board to reach: three buttons with real press timing, the mute switch,
an SD card you can fill up, a network you can drop, and a release feed you can
publish to. It also serves the device's own update page at
<http://127.0.0.1:8080>.

Audio can be real. Point it at this computer's microphone and speakers and a
turn records what you say, ends when you stop talking, and plays it back — a
parrot standing in for TTS, at the robot's own 16kHz mono format, through the
same HAL the Pi uses. Needs `sounddevice`; without it the level slider keeps
working. See `tools/emulator/audio.py`.

For the version with no window, which is also the test suite:

    python -m tools.emulator.headless

Twenty-one scenarios, a hundred-odd assertions: boot, a full conversation, a turn that
fails, the mute switch pulled mid-sentence, the setup gesture, an optional
update, an urgent one, a full card, a dropped connection, a corrupted bundle,
what actually reaches the panel, whether anything was left lit on it, the
audio path end to end against a canned microphone, and the whole install
path — apply, restart, confirm, and both ways a bad release gets rolled back.
No sound card and no Pi needed. Run it after touching `device.py`, the
renderer, or anything under `updates/`.

## Run it on the robot

    pip install -r firmware/requirements-pi.txt
    python -m firmware.main

Refuses to start anywhere that is not a Raspberry Pi, and says to use the
emulator instead. There is no automatic fake-hardware fallback: firmware that
silently substitutes a stub is firmware that ships untested.

## The face

`firmware/face/` draws it; `tools/face/preview.py` shows it on a PC at the
panel's real 320 × 240 with the SPI cost of every frame. See
[firmware/face/README.md](firmware/face/README.md).

The animation comes out of After Effects. `tools/face/bake.py` reads the
export from `../assets/brand/face-states/ae/faces.json`, converts AE's
(speed, influence) eases into bezier handles, and writes
`firmware/face/data/faces.json`. Re-export from AE, then:

    python -m tools.face.bake            # rewrite the baked data
    python -m tools.face.bake --check    # exit 1 if it is stale

The robot reads only the baked file. That is the whole reason the conversion
lives in `tools/`.

## Publishing an update

    git tag -a v0.4.1 -m "Faster wake word" && git push --tags

GitHub Actions builds the bundle, signs it, and publishes the release; the
devices do the rest. A tag with a suffix — `v0.5.0-beta.1` — is a prerelease
and only reaches devices set to the dev channel. Needs the
`TRSTENKO_SIGNING_KEY` secret, and `firmware/VERSION` has to match the tag.
Details in [firmware/updates/README.md](firmware/updates/README.md).

## Updates

The device checks GitHub releases and decides whether to ask or to tell:
optional updates wait for the user, urgent ones announce themselves and go
ahead. Either way one yes covers the whole thing: download, unpack, swap the
symlink, restart. If the new release does not come up, the device goes back to
the previous one on its own, without a laptop or a card reader.

The checks are not skipped for urgent releases, and neither the download nor
the install starts while someone is mid-conversation.

**It updates the app, not the operating system.** A release needing a new
kernel, a new apt package or a newer Python is refused with a message saying
the card needs reflashing, rather than half-applied. Details and the reasoning
in [firmware/updates/README.md](firmware/updates/README.md); the first-install
steps and the systemd units in [firmware/deploy/](firmware/deploy/).

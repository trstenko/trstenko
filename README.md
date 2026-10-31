# Trstenko

Trstenko is a voice assistant that sits on your desk: a 10 cm cube with a small screen for a
face, a microphone, a speaker and a switch that physically cuts the microphone's power. Say
"Trstenko", ask something, and it answers out loud. It sets reminders and timers, remembers what
you tell it to, and changes its own volume. It speaks English and Slovenian.

It is built by four students for the Slovenian *Moje podjetje* competition (April 2027).

## How it works

```
wake word ("Trstenko")  ->  record until you stop talking  ->  speech to text
   ->  a language model, which can call the device's tools (reminders, timers, memory, volume)
   ->  Piper text to speech on the device  ->  the face animates the whole time
```

- **You bring the AI.** The owner pastes in their own API key for Google Gemini, OpenAI, Grok,
  Groq, or Ollama running on their own PC. Trstenko has no server of its own; your questions go
  only to the provider you picked.
- **The wake word and the speech output run on the device.** Nothing leaves it until it hears its
  name, and the mute switch takes the microphone away in hardware, not in software.
- **It is set up from a phone.** A new device opens its own Wi-Fi hotspot and shows a QR code; its
  settings page is served by the device itself.
- **It updates itself.** It checks this repo's GitHub releases, asks before installing (or, for an
  urgent fix, tells you and goes ahead), and rolls back on its own if a new version does not start.
- **It can take third-party tools.** A `.trsttool` file adds a new ability. The device limits
  what a tool is given, but it cannot yet guarantee a tool stays inside those limits, so install
  one only if you trust whoever wrote it.

The hardware is a Raspberry Pi 3 A+ (512 MB of RAM, which is the limit everything is designed
around), a 2.4" 320 × 240 screen, a ReSpeaker Lite USB microphone and a small speaker, in a
3D-printed shell. **The hardware has not been built yet.** Everything here runs and is tested in
an emulator on a normal PC.

## What is in this repo

```
firmware/        what runs on the device, and nothing else
  main.py        the entry point systemd starts
  device.py      the main loop: events in, a face out
  assistant/     wake word to spoken reply: providers, speech to text, tools, text to speech
  scheduler/     reminders and timers
  face/          draws the animated face (data baked from After Effects)
  webui/         the settings page the device serves to your phone
  updates/       checking, downloading, verifying and installing releases, and rolling back
  hal/           the hardware layer: base.py is the contract, pi.py is the real board
  storage/       the SQLite database and its migrations
  deploy/        systemd units and first-install steps

tools/           for development; never copied onto the device
  emulator/      a Trstenko in a window, and the test suite
  eval/          scores each AI provider on a fixed set of questions
  toolmaker/     a page for writing and testing third-party tools
  face/          imports the After Effects animation and bakes it for the device
  release.py     builds and signs a release
  wakeword/      trains the "Trstenko" wake-word model (Docker)
```

The split is enforced: `python -m tools.check_layout` fails if anything in `firmware/` imports
from `tools/`, needs a display server, or imports a package at load time that would stop the
device from booting without it.

## Try it without the hardware

```
pip install -r tools/requirements.txt
python -m tools.emulator
```

A window with the face and the device's buttons, mute switch, network and SD card, all of which
you can break on purpose. Its settings page is at <http://127.0.0.1:8080>; paste an API key there
to talk to a real model. It can use your PC's microphone and speakers. Details in
[tools/README.md](tools/README.md).

The tests are the same emulator without a window, on a clock the tests control:

```
python -m tools.emulator.headless
```

CI runs this, the layout check and a release build on every pull request.

## On the device

```
pip install -r firmware/requirements-pi.txt
python -m firmware.main
```

It refuses to start on anything that is not a Raspberry Pi. There is no fallback to fake
hardware, so firmware that passes on the board was tested on the board. First-install steps are
in [firmware/deploy/](firmware/deploy/).

## Releases

```
git tag -a v0.4.1 -m "Faster wake word" && git push --tags
```

GitHub Actions builds the bundle, signs it and publishes the release; devices find it from there.
A tag with a suffix (`v0.5.0-beta.1`) only reaches devices on the dev channel. `firmware/VERSION`
has to match the tag. The update only replaces the app, never the operating system: a release
that needs a newer system is refused with a message to reflash the card. Details in
[firmware/updates/README.md](firmware/updates/README.md).

## More

- [tools/README.md](tools/README.md): the emulator, the tool creator, the face pipeline, releases
- [firmware/face/README.md](firmware/face/README.md): how the face is drawn
- [firmware/updates/README.md](firmware/updates/README.md): how updates are checked and installed
- [SECURITY.md](SECURITY.md): how to report a security problem;
  [SECURITY-REVIEW-2026-10.md](SECURITY-REVIEW-2026-10.md) is the October 2026 review

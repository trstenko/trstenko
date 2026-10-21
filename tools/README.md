# tools

Development only. None of this is copied onto the robot, and
`python -m tools.check_layout` fails the build if anything under `firmware/`
starts depending on it.

| | |
|---|---|
| `emulator/` | A Trstenko with no parts in it. Runs the real firmware. |
| `face/ae_import.py` | Reads the After Effects export and rebuilds its curves. |
| `face/bake.py` | Writes `firmware/face/data/faces.json` from that. |
| `face/preview.py` | The face on a PC, with per-frame SPI cost. |
| `check_layout.py` | Enforces the firmware/tools split. |
| `wakeword/` | Trains the "Trstenko" micro-wake-word model that ships on a sold unit. Docker, not pip -- see its own README. |

    pip install -r tools/requirements.txt

tkinter is needed by the emulator and the preview. It ships with the
python.org and Windows builds; on Debian it is `apt install python3-tk`.

## The emulator

    python -m tools.emulator

A window with the panel on the left at 2× nearest-neighbour, and on the right
everything the physical device has that a PC does not:

- **Three buttons** that report press and release separately, so a long press
  and the hold-both-volume-buttons setup gesture behave as they will on the
  desk. Click and hold, do not click.
- **The mute switch.** It does not set a flag. It takes the microphone away,
  the way cutting the ReSpeaker Lite's 5V does, and the firmware has to cope
  with the device vanishing.
- **Free space, network, metered, under-voltage.** Every branch of the
  updater's preflight is reachable from the window.
- **A release feed.** Pick a version, a size, an urgency, and whether the
  download should be corrupt or drop half-way. `--repo owner/name` points the
  same manager at real GitHub instead.
- **Audio in and out**, real if you want it. See below.
- **A release you can install.** The emulated feed builds an actual tar.gz of
  this checkout, so `install` unpacks something real, swaps the symlink and
  restarts into it. Tick *ship a release that cannot start* to watch the
  rollback happen.

The panel keeps a framebuffer and pastes only the bbox the firmware pushed, so
anything drawn outside the declared blit region does not appear — the same
failure the real panel has, visible before the panel exists. Turn the region
overlay on to see the declared region (grey) against the dirty rect (orange).

The device's own update page is served at <http://127.0.0.1:8080>: the same
HTML, the same endpoints and the same `UpdateManager` the Pi runs.

Keys: `W` wake word, `M` mute, `N` network, `U` check for updates, `Esc` quit.

Useful flags:

    python -m tools.emulator --urgency urgent --grace 20
    python -m tools.emulator --size-mb 400 --free-mb 256    # no room
    python -m tools.emulator --kbps 200                     # slow download
    python -m tools.emulator --repo trstenko/trstenko     # the real feed

### Without a window

    python -m tools.emulator.headless

The same rig on a clock the test controls, so a three-second button hold costs
no real time. Twelve scenarios, no sound card needed, and it exits non-zero when one of them does
something other than what it says. This is the test suite; run it after
touching `firmware/device.py`, the renderer, or anything in `firmware/updates/`.

### Audio

Point the input at this computer's microphone and the output at its speakers,
and the emulator stops pretending. A turn then:

- captures at the robot's own format — 16kHz mono int16 in 160-frame chunks,
  which is what micro-wake-word takes and nothing else,
- animates `face_listening` from the level of the room,
- ends the utterance when you stop talking rather than when a clock runs out,
- and plays back what it heard.

That last one is a parrot standing in for Piper, and it is the point of the
whole section: microphone in, through the ring buffer, through the same
`Mic`/`Speaker` interfaces the Pi will use, back out. Only the words are
faked. When a real STT and TTS land, nothing in `firmware/` changes.

Capture runs on PortAudio's thread into a bounded ring buffer and the device
loop takes whatever has arrived, because waiting even 10ms for audio on the
thread that draws the face costs a third of the frame budget. Utterances get
250ms of pre-roll, so the first word is not clipped by the wake word firing
late, and no more than that — the ring holds two seconds and sending all of it
to STT would delay every turn.

The level slider does two jobs: it *is* the level when nothing real is
plugged in, and it is an input trim when something is. The trim earns its
place — desk microphones differ by more than 20dB, and a headset boom mic in
a quiet room reads near zero at the gain a laptop array needs. It moves the
samples, not just the meter, so what you see is what gets recorded and played
back.

**When you hear nothing**, in order:

1. **Play a test tone** (`T`). If you hear it, the output is fine and the
   problem is on the way in.
2. Check the line under the meters. A sound card delivering nothing says so:
   *nothing is arriving from this device (peak 0.1% of full scale)*.
3. **Pick a different input** in the dropdown. A machine with a headset, a
   webcam and onboard audio has three candidates and Windows picks one of
   them for you.
4. Switch the input to **a wav file, on a loop**. With no `--wav` path it
   synthesises an utterance, so the whole path can be exercised on a machine
   whose microphone does not work at all.

Needs `sounddevice` (`pip install sounddevice`, already in
`tools/requirements.txt`). Without it the live backends refuse with that line
and the slider keeps working.

**What a PC cannot show you:** the echo problem. On the robot, playback goes
back out through the mic board so the XU316 can subtract it, which is what
stops the device waking itself up. A PC has no equivalent, so here the
emulator will happily hear its own output. That test needs hardware — Phase 1,
step 11.

## The tool creator

    python -m tools.toolmaker

Opens a page in the browser for writing a third-party tool: a `.trsttool`
file the device can be given later (plan 10). Your tools are kept as plain
folders in `~/trstenko-tools` (`--dir` to change it), so you can also edit
them in your own editor.

A new tool opens as a working weather tool with seven tests. You change three
files:

- `tool.json`: who the tool is, which hosts it may reach, and the description
  and arguments the model is given.
- `tool.py`: the code. The docstring at the top says what `ctx` offers.
- `tests.py`: your tests. The internet is a dict of answers you write down.

**Run tests** runs your tests and then seven checks every tool gets (no
internet, an error page, an answer that is not JSON, an empty answer, wrong
arguments). **Try it** calls the tool once and shows the exact text the model
would read. **Export** writes the `.trsttool`.

The tool runs here the way it will on a device: in a process of its own that
may not open sockets, start programs or touch files, with the internet reached
only through the hosts it listed. A device cannot install one yet; that page
is the next step of plan 10.

## Cutting a release

    python -m tools.release keygen --out release-key.json
    python -m tools.release trust  --key release-key.json
    python -m tools.release build  --version 0.4.1 --key release-key.json \
        --urgency recommended --summary "Faster wake word"

`build` writes the bundle and a signed `update.json`; attach both to a GitHub
release tagged `v0.4.1`. `trust` adds the public key to the firmware, which
is also what makes signatures mandatory — there is no separate switch.

Keep the private key wherever the team keeps its other secrets. Not the repo.

## The face pipeline

    python -m tools.face.preview        # look at it
    python -m tools.face.bake           # AE export -> firmware/face/data/
    python -m tools.face.bake --check   # is the baked file stale?

`ae_import.py` is the part that has to stay out of the firmware: it reads
`../assets/brand/face-states/ae/faces.json`, which is not on the robot,
and converts AE's (speed, influence) keyframe eases into cubic bezier handles.
Doing that once on a build machine is cheaper and less fragile than shipping
the converter and hoping the export is there.

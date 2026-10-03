# Updates

The device watches its GitHub releases and decides, per release, whether to
ask the user or to tell them. The page it serves over its own hotspot is the
whole interface; there is no app and no account.

## Who decides

| Urgency | What the device does |
|---|---|
| `optional` | Says one is available. Nothing downloads until the user presses the button. They can defer it, or skip that version permanently. |
| `recommended` | Same as optional. The word only changes what the page says. |
| `urgent` | Says it is going to update, counts down, then downloads, installs and restarts on its own. "Install now" skips the wait. It can be held for a few hours; it cannot be skipped. |

A yes means the whole thing. Pressing **Install 0.4.0** downloads, unpacks and
restarts into it — there is no second button between the download finishing
and the install starting, because nobody who pressed the first one meant
"fetch it and ask me again". An urgent release reaches the same place with
nobody present, which is the only thing marking one urgent is for.

Urgency changes who gets asked. It never changes what gets checked — an
urgent release still fails preflight on a full SD card or a board that is
browning out, and a failed preflight stops it dead, unattended or not.

It also waits its turn. Neither the download nor the install starts while the
device is mid-conversation: the download competes with the STT and model calls
for one wifi radio, and unpacking plus building a virtualenv is minutes of CPU
on a Pi 3 A+. Both pick up the moment the turn ends.

The asymmetry is deliberate. Urgent is for a release that fixes something
about privacy or safety on a device with a microphone in it, and a user who
never opens the page still gets that fix. Everything else waits for a yes,
because a desk assistant that reboots itself to add a face is a nuisance.

## How a release says how urgent it is

Attach `update.json` to the GitHub release:

```json
{
  "version": "0.4.1",
  "urgency": "urgent",
  "summary": "Fixes a crash that leaves the microphone recording after a failed turn",
  "min_version": "0.3.0",
  "requires_reboot": true,
  "sha256": "…",
  "size_bytes": 8412300
}
```

Whoever cuts the release at 1am will forget the asset, so there is a fallback:
a line in the release body reading `Urgency: urgent`. When neither exists the
release is `optional`, which is the safe direction to be wrong in.

The firmware bundle itself is the release asset named
`trstenko-firmware-<version>.tar.gz`. A release with no such asset is not a
firmware release and is skipped — the same repo also tags enclosure revisions
and document updates.

`min_version` refuses a hop that is too long, for when an update needs a
migration that only a recent version knows how to run.

## Who made this release

A sha256 proves the bundle arrived intact. It does not prove who wrote it —
it came from the same place the bundle did, so anyone able to answer as
GitHub, or with write access to the repo, can publish a matching checksum for
whatever they are handing over. On a device with a microphone in it, that is
the difference worth caring about.

So `update.json` is signed with Ed25519 and the public key ships in the
firmware. The signature covers the manifest, the manifest carries the
bundle's sha256, and the bundle is whatever hashes to that — break the chain
anywhere and it stops matching.

```sh
python -m tools.release keygen --out release-key.json   # once, ever
python -m tools.release trust  --key release-key.json   # once, ever
python -m tools.release build  --version 0.4.1 --key release-key.json
```

The private key never goes near a robot and never goes in the repo.

**Adding a key is what turns enforcement on.** There is no separate switch,
because a switch is a thing somebody forgets: `require_signature` is derived
from whether any key is configured. While the list is empty — which is where
this project is today — releases install unverified, the status says
`signed: false`, and the page says so in amber. The emulator generates a
throwaway key at startup and signs with it, so the path being exercised in
development is the signed one.

Verification is RFC 8032 arithmetic written out in `signing.py` rather than a
dependency: `cryptography` would drag a build toolchain into the Pi image for
one signature per update check. It costs a few hundred milliseconds on a Pi 3
A+, on a worker thread, against a loop that otherwise does nothing for six
hours. It is checked against the RFC's own test vectors.

## What is checked before anything is downloaded

`preflight.py`, run three times: when the release is found, again when the
download starts, and again before anything is written — that offer may be a
day old, and the card can fill up in between.

The third run asks a different question. The bundle is already on the card by
then, so the network and metered checks are dropped: refusing to install
because the wifi dropped after the download would strand a device with a
verified update sitting on its disk. Two checks get stricter instead — free
space, because unpacking needs it, and being mid-conversation, which is only
advisory before a download and blocking before an install.

- **Storage.** Three times the bundle plus 256MB: the download, the unpacked
  tree, and the current install kept intact for rollback. The headroom is so
  the device can still write a log while it fails.
- **Network**, and whether it is **metered** — never pull 20MB over a
  connection billed by the megabyte without being told to.
- **Under-voltage.** The Pi's own flag. Rewriting a root filesystem on a board
  that is already browning out is how a device becomes a brick.
- **Version.** Newer than what is running, and at least `min_version`.
- **The signature**, blocking as soon as the device has a key to check
  against, advisory when it does not.
- Advisory, shown but not blocking: temperature, whether the device is
  mid-conversation, whether the release published a checksum at all.

Then the bundle is streamed to `staging/` — streamed, because 512MB of RAM
does not have room for a copy of the image plus the copy being written — its
length and sha256 checked against the manifest, and only then is it staged.

## How an update is applied

A release is a directory, and installing one is a symlink swap.

```
/opt/trstenko/releases/0.4.0/     unpacked, with its own virtualenv
/opt/trstenko/current -> releases/0.4.0
/var/lib/trstenko/                settings and the database, never touched
```

1. Unpack into `releases/0.4.0.tmp` and read `manifest.json`.
2. Build the release's virtualenv from wheels carried **inside the bundle**,
   with `pip --no-index`. An install that reaches PyPI can die halfway when a
   network drops, and it makes the result depend on what the internet looked
   like that afternoon.
3. `rename()` the finished directory into place. A power cut before this
   leaves a `.tmp` directory and nothing else.
4. Write `pending-confirm.json`, then swap the `current` symlink. Armed
   before the swap, never after: the marker is what recovers the device.
5. Restart — but not mid-sentence. The manager waits until the device is not
   in the middle of a turn.

Settings live under a different root on purpose, so going back a release does
not go back on what the user told the device.

## What happens when a release does not work

The new one is on trial until it proves otherwise, and there are two nets
because there are two different failures.

**It starts but never becomes a working device** — a config it cannot parse, a
web server that never binds. Nothing calls `confirm()`, the deadline passes,
and it rolls itself back. "Working" means drawn frames and a face that is past
boot, not "reached the end of `__init__`".

**It dies before any of its code runs** — a syntax error, a missing import.
Nothing of the new release executes, so the new release cannot be what
recovers. `bootcheck` runs *before* the app on every boot, counts the attempt,
and rolls back once the count passes three. It is a copy at `bin/bootcheck.py`,
outside `releases/`, precisely so the file doing the recovery is never the file
being replaced. `OnFailure=` on the systemd unit catches the crash-loop case
without waiting for a reboot nobody will perform.

Either way the device comes back on the previous release by itself. That is
the whole reason the previous one is kept rather than deleted.

## THE LIMIT: this updates the app, not the operating system

An update replaces `/opt/trstenko`. It does not touch the kernel, apt
packages, the system Python, ALSA or NetworkManager config, or anything else
on the card.

So a release that needs a newer Piper binary, a newer glibc, or a kernel fix
**cannot ship this way**. That case is refused rather than half-applied: the
manifest carries `requires_base`, the card carries its image version in
`base.json`, and a bundle asking for more than the card has is rejected with a
message saying the card needs reflashing.

For the handful of units this project builds, reflashing is an honest answer.
It stops being one somewhere past fifty devices in other people's hands. The
fix at that point is A/B root partitions (RAUC or SWUpdate), which duplicates
the rootfs, needs the bootloader's `tryboot` wired up, and has to be decided
**before the cards are written** — which is exactly why it is not being done
now, with the enclosure still unfinished.

## What is not built yet

Worth knowing before this gets trusted with anything:

- **No key has been generated for this project yet.** Signing is built and
  enforced (see below), but `trusted_keys.json` is still empty, so releases
  install unverified and every screen that mentions one says so. Running
  `python -m tools.release keygen` and `trust` once closes this.
- **`firmware/hal/pi.py` has never run on hardware.** The parts are not
  built. Pin numbers, the ALSA device names and the SPI wiring are from the
  research, not from a board that worked. Everything above the HAL is
  exercised by the emulator; the HAL itself is Phase 1's first job.
- **No delta updates.** Every release downloads whole. At ~20MB and a home
  connection that is fine, and it would not be on a metered link — which is
  why a metered connection blocks the download rather than being a warning.
- **The virtualenv step is untested against a real wheel set.** The code
  path runs and is exercised with a stub; no bundle with actual aarch64
  wheels has been built yet.
- **A release is trusted to be honest about `requires_base`.** Nothing
  verifies that a bundle claiming base 1 really runs on base 1. Getting that
  wrong looks like an update that installs and then rolls itself back.

## Talking to it

The page calls these; so can curl.

    GET  /api/update            state, release, preflight results, device
    POST /api/update/check
    POST /api/update/approve
    POST /api/update/defer      {"hours": 24}
    POST /api/update/skip
    POST /api/update/cancel
    POST /api/update/install    unpack it, swap to it, restart
    POST /api/update/revert     go back to the release before this one

## Publishing one

`.github/workflows/release.yml` does it. Two ways in:

```sh
# stable, asks the user
git tag -a v0.4.1 -m "Faster wake word" && git push --tags

# dev channel: a prerelease, ignored by devices on stable
git tag -a v0.5.0-beta.1 -m "Trying the new wake word" && git push --tags
```

An `Urgency: urgent` line in the tag message is honoured. For anything
urgent, prefer running the workflow by hand from the Actions tab — marking a
release urgent means devices install it unattended, which deserves a button
somebody presses on purpose rather than a line in a tag message.

The workflow refuses to publish when `firmware/VERSION` disagrees with the
tag. That is not pedantry: the device reads that file to know what it is
running, so a mismatch makes every device install the release and then offer
it again, forever.

`TRSTENKO_SIGNING_KEY` (the contents of `release-key.json`) has to be a
repository secret. Without it the workflow stops rather than publishing
something no device can verify. If the committed key list is empty it
publishes with a warning, because that is this project's current state.

**Channels.** A GitHub prerelease is the dev channel. A device set to
`stable` never sees one; a device set to `dev` (or `beta` — both work) takes
the highest version of either. Set it in `settings.json` under the data
root, or from the update page.

## Trying it

    python -m tools.emulator --urgency urgent --grace 20
    python -m tools.emulator --size-mb 400 --free-mb 256
    python -m tools.emulator --kbps 150

The emulator's feed implements `ReleaseSource`, so the manager cannot tell it
from GitHub. `--repo owner/name` points it at the real thing.

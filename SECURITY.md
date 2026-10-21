# Reporting a problem with Trstenko

Trstenko is a desk assistant with a microphone in it. If you have found a way
to listen through one, take one over, or put software on one that its owner
did not ask for, please tell us before you tell anyone else.

**Where:** open a private advisory at
<https://github.com/trstenko/trstenko/security/advisories>. That reaches us
without the report being public first.

**What helps:** the firmware version (it is on the device's own page, under
"This device"), what the device was doing, and enough detail to reproduce it.
A proof of concept is welcome and never required.

**What to expect:** an acknowledgement within a week, and an honest answer
about whether and when it gets fixed. We are a student company, not a
security team — we would rather say "this will take a month" than go quiet.

A fix ships as a release marked `urgent`, which devices install on their own
without waiting to be asked.

## Support period

Security updates are published until the date in
[`firmware/product.py`](firmware/product.py) (`SUPPORT_UNTIL`), shown on
every device's own page. When that changes, it changes there first.

## What is already in place

- **Updates are signed.** Ed25519 over the release manifest, verified on the
  device against a key in the firmware; the manifest carries the bundle's
  sha256, so the signature covers the whole release. See
  [`firmware/updates/signing.py`](firmware/updates/signing.py).
- **A bad update undoes itself.** A release that does not come up is rolled
  back by the device, either on a deadline or by `bootcheck` on the next
  boot. See [`firmware/updates/README.md`](firmware/updates/README.md).
- **The device's web page is locked** to a per-device pairing PIN, with
  attempts throttled. There is no default or shared credential. See
  [`firmware/webui/auth.py`](firmware/webui/auth.py).
- **The microphone has a hardware switch** that cuts its power, so muted is
  electrically muted rather than a promise made in software.
- **Each release carries an SBOM** (`sbom.json`), so "does this ship the
  broken version of X" is a question with a filed answer.

## Known gaps, stated rather than hidden

The October 2026 review adds more, with what was fixed and what is still
open: [`SECURITY-REVIEW-2026-10.md`](SECURITY-REVIEW-2026-10.md).

- **No key is trusted yet.** Signing is built and enforced the moment a key
  is added, but the shipped list is still empty, so releases install
  unverified and the device says so on its own page. This closes with one
  run of `python -m tools.release keygen` and `trust`.
- **The page is served over plain HTTP.** It is reachable only on the
  device's own access point or the local network, and a self-signed
  certificate would train people to click through warnings. Revisit if the
  device is ever reachable from further away.
- **Settings, including API keys, are stored unencrypted** on the SD card,
  readable by anyone holding the card. Encrypting them at rest needs a key
  the device can retrieve without a human, which on this hardware means
  storing that key next to the data it protects.
- **`firmware/hal/pi.py` has never run on hardware.** It is the layer
  closest to the device and the least exercised.

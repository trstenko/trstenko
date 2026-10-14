# Security review, October 2026

A review of everything under `firmware/` plus the developer servers in
`tools/`, at commit `9348228`. Done by reading the code, then checking the
findings that could be checked against the code itself: the emulator's
scenarios, and small scripts against `Auth`, the two web servers and the
updater. Nothing was tried against hardware, since there is none in reach and
`firmware/hal/pi.py` has never run on any.

`SECURITY.md` already lists four known gaps (no trusted key, plain HTTP,
unencrypted settings, untested HAL). They are not repeated below unless
something new was found about them.

## Summary

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | The firmware, and every third-party tool it starts, runs as root | High | Open: deploy change |
| 2 | The main service can rewrite the script that boots as unconfined root | High | Open: deploy change |
| 3 | Third-party tools are not isolated by the OS, only by an in-process hook | High | Open: design change |
| 4 | With no trusted key, an `urgent` release installs itself without a yes | High | Open, see below |
| 5 | A signed manifest was not tied to the release it came with | Medium | **Fixed** |
| 6 | Any web page the owner opens could hold the PIN lockout shut | Medium | **Fixed** |
| 7 | Anyone on the LAN can still hold the PIN lockout shut | Low | Open: accepted trade-off |
| 8 | No `Host` check outside setup mode (DNS rebinding) | Low | Open |
| 9 | The PIN lockout checked and updated without a lock | Low | **Fixed** |
| 10 | Static files: the folder check was a bare string prefix | Low | **Fixed** |
| 11 | A tool's process has no memory or CPU limits | Low | Open |
| 12 | Tar bundles are not checked for links on an older Python | Low | Open |
| 13 | The owner's Ollama host can be any address | Info | Open |
| 14 | The wakeword dashboard trusts any request from localhost | Low (dev tool) | Open |

## Findings

### 1. The firmware runs as root — High

None of the units in `firmware/deploy/` sets `User=`, so `trstenko.service`
runs as root. `50-trstenko-network.rules` says as much ("only needed once the
unit runs as its own user"). The hardening in the unit (`ProtectSystem=strict`
and the rest) limits what root can *write*, but root can still *read*
everything, including the Wi-Fi passwords NetworkManager keeps in
`/etc/NetworkManager/system-connections/`, and the unit has no
`CapabilityBoundingSet`, so it keeps every capability.

Everything the service starts inherits that: Piper, `nmcli`, and every
third-party tool (finding 3).

**Fix:** a `trstenko` system user with `User=trstenko`,
`CapabilityBoundingSet=` (empty), `AmbientCapabilities=` and
`SupplementaryGroups=audio gpio spi`; the polkit rule already exists for the
Wi-Fi part. The board TODO in the polkit file is the remaining step.

### 2. The main service can rewrite what boots as unconfined root — High

`trstenko-bootcheck.service` and `trstenko-rollback.service` run
`/usr/bin/python3 /opt/trstenko/bin/bootcheck.py` as root with **no**
sandboxing. `/opt/trstenko` is in the main service's `ReadWritePaths`, and the
installer itself writes `bin/bootcheck.py` (`Installer._install_bootcheck`).
So anything that gains code execution inside the sandboxed main service, for
instance through finding 3, can replace `bootcheck.py` and is running as
unconfined root on the next boot, persistently. The sandbox on the main unit
does not hold.

**Fix, in order of effort:**
- Give both boot units the same hardening as the main one; they only need
  `ReadWritePaths=/opt/trstenko`.
- Have bootcheck run from a path the main service cannot write (for example
  `/usr/local/lib/trstenko/`, root-owned, written only when the card is made),
  and mark `/opt/trstenko/bin` read-only for the main unit
  (`ReadOnlyPaths=/opt/trstenko/bin`). Updating bootcheck then becomes a
  base-image change, which matches how rarely it should change.

### 3. Third-party tools are not isolated by the OS — High

`runner.py` says its audit hook "is a tripwire ... not the wall. The wall is
the operating system on the device, which runs this process as a user with no
network and nothing to read (plan 10)". That wall is not built:
`host.run()` starts the runner with `sys.executable` as the same user as the
firmware (root, per finding 1), with full network access and the whole
filesystem readable. The environment is emptied, which is good, and that is
the only OS-level separation.

The audit hook is not a boundary on its own. Tool code runs in the same
interpreter as the hook, and the hook's decisions depend on state that code
can reach and change, so a tool written to get past it will. Several
operations that matter are also not on the blocked list at all.

So, in practice, a `.trsttool` the owner installs has the same power as the
firmware: it can read `secrets.json` (the provider API keys), the pairing PIN
and the memories database, talk to the network directly instead of through
`ctx.http`, and write under `/opt/trstenko` (finding 2).

What limits this today: installing needs a paired session **and** the PIN
again, and the owner is shown the manifest first. So this is a "malicious or
compromised tool file" risk, not a remote one. That is still the exact case
the host allowlist and the consent screen exist for, and the consent screen
currently promises more than the device enforces.

**Fix:** make the wall real before tools are offered to anyone.
- Start each call in its own transient unit, for example a
  `trstenko-tool@.service` template started through a small polkit rule, with
  `DynamicUser=yes`, `PrivateNetwork=yes` (the host does the HTTP),
  `ProtectSystem=strict`, `ProtectHome=yes`, `TemporaryFileSystem=/var/lib /opt`
  plus `BindReadOnlyPaths=` for the tool folder and the Python runtime only,
  `MemoryMax=`, `CPUQuota=`, `TasksMax=`, `RestrictAddressFamilies=AF_UNIX`
  and a `SystemCallFilter=@system-service`. `bubblewrap` gets the same result
  without systemd. Note the main unit's `RestrictNamespaces=yes` rules out
  creating namespaces from inside it, which is why the work is best handed to
  systemd.
- Until then, change the install screen's wording so it does not claim
  isolation the device does not have, or keep tool install behind a
  developer switch.

### 4. With no trusted key, an urgent release installs itself — High

Already listed as a known gap ("no key is trusted yet"), but one consequence
is not: a release marked `urgent` is downloaded, installed and restarted into
after a five-minute grace period **without anyone saying yes**
(`manager._on_checked`, `SCHEDULED`). With no key configured, the only thing
standing between "someone got write access to the GitHub repo or account" and
"code running as root on every device" is that five-minute notice. If the
release ships without `update.json`, there is not even a sha256 to check: the
urgency comes from the unsigned release body.

**Fix:** generate and trust the release key before any unit leaves the
building (`python -m tools.release keygen` and `trust`, keep the private key
off GitHub). Until a key is configured, treat every release as needing
consent, whatever it says about urgency. That is a two-line change in
`_on_checked`, left for you to decide because it changes product behaviour.

### 5. A signed manifest was not tied to its release — Medium, fixed

The signature covers `update.json`, but the updater took the version from the
GitHub tag and, when the manifest had no `urgency`, the urgency from the
unsigned release body. Once a key is trusted, someone with repo write access
could attach an old signed `update.json` and its bundle to a new, higher tag.
The bundle's inner `manifest.json` version check would usually stop that, but
the signature, which is the control meant for this threat, did not. A signed
manifest without a `sha256` was also accepted, since that check was advisory.

**Fixed in this change:**
- `Release.check_signature` now refuses a signed manifest whose `version` is
  not the release's version, and once a release verifies, its urgency comes
  from the signed manifest only.
- `checksum_published` is blocking whenever signatures are required.
- New check in `scenario_signing`: "a signed manifest for another version is
  refused".

### 6. Any web page could hold the PIN lockout shut — Medium, fixed

`/api/auth` needs no token, and both servers parsed a body as JSON whatever
its `Content-Type`. A `text/plain` POST is a "simple" request that browsers
send cross-origin with no preflight. So any web page the owner had open could
POST to `http://trstenko.local/api/auth` in a loop and keep the device locked
(up to ten minutes at a time, renewed with each guess). A POST with no PIN at
all counted as a wrong guess too.

**Fixed in this change:**
- Both servers refuse a non-empty body not declared `application/json` with
  415 (`headers.json_body_ok`). Browsers only send that type cross-origin
  after a preflight, and neither server answers preflights. The page already
  sends the header.
- A pairing request with no PIN is a 400 and does not count towards the
  lockout.
- New checks in `scenario_pairing` and `scenario_web_servers` (both servers).

Chrome's Private Network Access rules would block some of this on their own,
but not in every browser and not from every origin.

### 7. Anyone on the LAN can still hold the lockout shut — Low, open

The lockout is one counter for the whole device, so anybody on the same
network can keep sending wrong PINs and the owner cannot pair until they
stop. That is the price of a global counter, which is what makes a
six-digit PIN safe against guessing, and the owner can still use the device
by voice. If it becomes a problem: count per client address as well as
globally, or let a press of the device's button clear the lockout.

### 8. No `Host` check outside setup mode — Low, open

Outside setup the servers answer any `Host` header, so a DNS-rebinding page
can reach the API from the owner's browser under its own origin. Everything
except `/api/auth` needs a token that origin does not have, so today this
only adds another way to reach `/api/auth` (finding 7). It is cheap to close:
answer 421 for any `Host` that is not one of `captive.OWN_HOSTS`, the device's
own IP addresses or its mDNS name.

### 9. The lockout checked and updated without a lock — Low, fixed

`Auth.verify` and `Auth.confirm` read "am I locked", compared, then counted
the failure, while the servers answer each request on its own thread. A burst
of guesses could all pass the "not locked" read before the first failure was
counted. Under CPython's GIL the window turned out to be very small: a
200-thread burst got the correct four comparisons in every run. With a
slower comparison step in the way (simulated with a 1 ms delay) the same
burst got 42 guesses compared. The window is real, so it is closed:
both methods now hold a `threading.Lock`.

### 10. The static-file folder check was a bare prefix — Low, fixed

`server.py` and `app.py` checked `target.startswith(STATIC)`, which a sibling
folder named `static-something` also passes. No such folder exists, so this
could not be reached, but it is now `STATIC + os.sep`. Flask's
`send_from_directory` has its own check as well.

### 11. A tool's process has no resource limits — Low, open

A tool call is killed after eight seconds, but nothing caps its memory or the
processes it starts. Eight seconds is enough to push a 512 MB board into the
OOM killer, which may pick the firmware. This goes away with the unit in
finding 3 (`MemoryMax=`, `TasksMax=`). Without it, `preexec_fn` with
`resource.setrlimit(RLIMIT_AS, ...)` is the quick version.

### 12. Tar bundles on an older Python — Low, open

`installer._check_members` checks member *names* for `..` and absolute paths
and relies on `tarfile`'s `data` filter for the rest. Without that filter
(Pythons before 3.12 that did not get the backport), a symlink member
pointing outside the folder is not refused. Only reachable with a bundle that
already passed the updater's checks, so it matters only with finding 4 open.
Refuse `issym()`/`islnk()` members that point outside the folder in
`_check_members`, or require a Python with `tarfile.data_filter`.

### 13. The Ollama host can be any address — Info

`keys.set` for Ollama takes any host, and the device will then send requests
there. Only a paired owner can set it, and pointing it at their own LAN is
the point, so this is noted rather than flagged.

### 14. The wakeword dashboard trusts any request from localhost — Low, dev tool

`tools/wakeword/ui/server.py` skips its token for any caller whose address is
`127.0.0.1`, and checks neither `Host` nor `Origin`. A web page open in the
developer's browser on the same machine can therefore post to it (once the
ad-hoc certificate has been accepted, which using the dashboard requires), or
reach it through DNS rebinding, and start the training jobs or touch the
recordings. The jobs are a fixed list, so this is nuisance rather than code
execution. `tools/toolmaker` already does it right: check the `Host` header
and require the per-run token from everyone, local or not.

## Checked and found sound

- **No shell anywhere.** `nmcli`, `systemctl` and the tool runner are started
  with argument lists; SSIDs and passwords cannot inject commands.
- **The page is XSS-resistant by construction.** No `innerHTML`, `eval` or
  inline script in `firmware/webui/static/`; the CSP forbids inline scripts
  and every third-party origin.
- **Tokens** are 256-bit, compared through a dict lookup on a random key,
  expire after a day, and die on restart. PIN comparisons are constant-time.
- **Request size** is limited before the token is checked, on both servers;
  `.trsttool` files are size-checked both packed and unpacked (no zip bomb).
- **`ctx.http`** enforces HTTPS, port 443, the declared host list, no user
  info in the URL, and checks the *connected* peer address is public, which
  closes the "public name that resolves to the router" trick. Redirects are
  not followed.
- **Secrets** are write-only through the API, kept out of `settings.get`, and
  masked out of logs by value and by shape. The `updates` section of the
  settings (including the repo the device trusts) cannot be changed through
  `PATCH /api/v1/settings`.
- **Ed25519 verification** rejects `s >= L` and off-curve points, and covers
  the manifest that carries the bundle's hash.
- **The tool creator** (`tools/toolmaker`) binds to localhost, checks the
  `Host` header and requires a per-run token, so a web page cannot drive it.
- **Destructive actions** (factory reset, privacy delete, tool install, PIN
  change) ask for the PIN again under the same lockout.

## What was changed

| File | Change |
|------|--------|
| `firmware/webui/auth.py` | Lock around the lockout check and update (9) |
| `firmware/webui/headers.py` | `json_body_ok`: a body must be declared JSON (6) |
| `firmware/webui/server.py`, `app.py` | 415 for undeclared bodies (6); folder check with a separator (10) |
| `firmware/webui/api.py` | A pairing request with no PIN is a 400, not a wrong guess (6) |
| `firmware/updates/releases.py` | A signed manifest must name this release's version; urgency only from signed fields (5) |
| `firmware/updates/preflight.py` | A missing sha256 blocks when signatures are required (5) |
| `tools/emulator/` | Scenarios for 5 and 6; each failed before the fix and passes after |

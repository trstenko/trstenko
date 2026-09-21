"""Who made this release.

A sha256 proves the bundle arrived intact. It does not prove who wrote it,
because it came from the same place the bundle did -- anyone able to answer
as GitHub, or who gets write access to the repo, can publish a checksum that
matches whatever they are handing over. On a device with a microphone in it
that is the difference worth caring about.

So the release manifest is signed, and the public key ships in the firmware.

    update.json         the manifest, with "signature" and "key_id"
      -> signature covers the manifest with those two fields removed
      -> the manifest carries the bundle's sha256
      -> the bundle is whatever hashes to that

One signature therefore covers the whole release: break the chain anywhere
and the hash stops matching.

## Ed25519, written out here

No dependency. `cryptography` would do this in C and pulls in a build
toolchain the Pi image does not otherwise need; this is the RFC 8032
reference algorithm in about a hundred lines of integer arithmetic.

It is slow -- two scalar multiplications, a few hundred milliseconds on a Pi
3 A+. That is once per update check, on a worker thread, against a loop that
does nothing else for six hours at a time. Nothing here handles a secret, so
it does not need to be constant time: the key, the signature and the message
are all public.

Signing lives in `tools/release.py`, not here. The robot verifies; it never
signs.

## Keys

`trusted_keys.json` next to this file lists the public keys, hex-encoded. A
release must be signed by one of them.

**With no keys listed, updates are allowed and marked unsigned.** That is
this project's current state and it is visible rather than silent: the status
says `signed: false`, the page says so, and the emulator's feed signs by
default so the signed path is the one being exercised. Add a key and
signatures become mandatory in the same move -- there is no separate switch
to forget.
"""

import hashlib
import json
import os

KEYS_FILE = "trusted_keys.json"

P = 2 ** 255 - 19
Q = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, P - 2, P) % P
_I = pow(2, (P - 1) // 4, P)


def _inv(x):
    return pow(x, P - 2, P)


def _recover_x(y, sign):
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P != 0:
        x = (x * _I) % P
    if x % 2 != sign:
        x = P - x
    return x


def _add(a, b):
    x1, y1 = a
    x2, y2 = b
    k = _D * x1 * x2 * y1 * y2
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + k)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - k)
    return (x3 % P, y3 % P)


def _mul(point, e):
    """Double-and-add. Iterative, because a recursive version is 253 frames
    deep and this runs on a board with 512MB."""
    result = (0, 1)
    addend = point
    while e > 0:
        if e & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        e >>= 1
    return result


_BY = 4 * _inv(5) % P
_B = (_recover_x(_BY, 0), _BY)


def _decode_point(data):
    if len(data) != 32:
        raise ValueError("a point is 32 bytes")
    value = int.from_bytes(data, "little")
    y = value & ((1 << 255) - 1)
    x = _recover_x(y, value >> 255)
    point = (x, y)
    if not _on_curve(point):
        raise ValueError("point is not on the curve")
    return point


def _on_curve(point):
    x, y = point
    return (-x * x + y * y - 1 - _D * x * x * y * y) % P == 0


def encode_point(point):
    x, y = point
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def verify(message, signature, public_key):
    """True when `signature` is this key's signature over `message`.

    Returns False rather than raising on a malformed signature or key: the
    input comes off the network, and every way it can be wrong means the same
    thing to the caller.
    """
    try:
        if len(signature) != 64 or len(public_key) != 32:
            return False
        r = _decode_point(signature[:32])
        a = _decode_point(public_key)
        s = int.from_bytes(signature[32:], "little")
        if s >= Q:
            return False
        digest = hashlib.sha512(
            signature[:32] + public_key + message).digest()
        h = int.from_bytes(digest, "little") % Q
        return _mul(_B, s) == _add(r, _mul(a, h))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- manifests


SIGNATURE_FIELDS = ("signature", "key_id")


def canonical(manifest):
    """The exact bytes that get signed.

    Sorted keys, no incidental whitespace, and the signature fields removed.
    Both sides have to agree byte for byte, so the rule is written once and
    used by the signer and the verifier alike.
    """
    body = {k: v for k, v in manifest.items() if k not in SIGNATURE_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def key_id(public_key):
    """Short name for a key, so a device can say which one it wanted."""
    return hashlib.sha256(public_key).hexdigest()[:16]


def load_keys(*paths):
    """Trusted public keys, as bytes. Missing or broken files contribute
    nothing rather than raising -- a device with an unreadable key file has
    no trusted keys, which is a state the caller already handles."""
    found = []
    seen = set()
    for path in paths:
        if not path:
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        for entry in (data.get("keys") if isinstance(data, dict) else data) or []:
            text = entry.get("key") if isinstance(entry, dict) else entry
            try:
                raw = bytes.fromhex(str(text))
            except ValueError:
                continue
            if len(raw) == 32 and raw not in seen:
                seen.add(raw)
                found.append(raw)
    return found


def default_key_paths(install_root=None):
    """The firmware's own keys, then the image's.

    Two places on purpose. The first ships with the release and can be
    rotated by a release signed with the outgoing key. The second is written
    when the card is made, so a device can be tied to its owner's key without
    rebuilding the firmware.
    """
    paths = [os.path.join(os.path.dirname(__file__), KEYS_FILE)]
    if install_root:
        paths.append(os.path.join(install_root, "trusted-keys.json"))
    return paths


def check(manifest, keys):
    """(ok, key_id, reason) for a manifest against a list of public keys.

    With no keys, nothing can be verified and nothing is claimed: ok is
    False and the reason says so. The caller decides whether that blocks the
    update -- see `require_signature` in config.py.
    """
    if not keys:
        return False, "", "no trusted keys are configured"
    signature = manifest.get("signature") or ""
    if not signature:
        return False, "", "the release is not signed"
    try:
        raw = bytes.fromhex(signature)
    except (ValueError, TypeError):
        return False, "", "the signature is not hex"

    message = canonical(manifest)
    for key in keys:
        if verify(message, raw, key):
            return True, key_id(key), ""
    return False, "", ("signed with a key this device does not trust "
                       "(wanted one of: %s)"
                       % ", ".join(key_id(k) for k in keys))

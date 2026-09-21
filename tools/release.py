"""Cut a release: make a keypair, build a bundle, sign the manifest.

    python -m tools.release keygen --out release-key.json
    python -m tools.release build --version 0.4.1 --out dist/
    python -m tools.release sign  --key release-key.json dist/update.json
    python -m tools.release trust --key release-key.json

Then attach `dist/trstenko-firmware-0.4.1.tar.gz` and `dist/update.json` to
a GitHub release tagged `v0.4.1`. The device does the rest.

The private key never goes near the robot and never goes in the repo. Keep it
where the team keeps its other secrets; losing it means minting a new one and
shipping it in a release signed with the old one, which is why `trust` takes
a list rather than a single key.

Signing lives here rather than in `firmware/` because the device only ever
verifies. The curve arithmetic is shared -- imported from
`firmware.updates.signing`, so the two sides cannot drift apart.
"""

import argparse
import hashlib
import json
import os
import secrets
import sys

from firmware.updates import signing
from tools.emulator import bundle as bundle_mod


# ------------------------------------------------------------------ keys


def keypair(seed=None):
    """(seed, public key). The seed is the private half; 32 bytes."""
    seed = seed or secrets.token_bytes(32)
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] &= 63
    h[31] |= 64
    a = int.from_bytes(h[:32], "little")
    public = signing.encode_point(signing._mul(signing._B, a))
    return seed, public


def sign(message, seed):
    h = hashlib.sha512(seed).digest()
    prefix = bytearray(h[:32])
    prefix[0] &= 248
    prefix[31] &= 63
    prefix[31] |= 64
    a = int.from_bytes(prefix, "little")
    public = signing.encode_point(signing._mul(signing._B, a))

    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(),
                       "little") % signing.Q
    big_r = signing.encode_point(signing._mul(signing._B, r))
    k = int.from_bytes(hashlib.sha512(big_r + public + message).digest(),
                       "little") % signing.Q
    s = (r + k * a) % signing.Q
    return big_r + s.to_bytes(32, "little")


def sign_manifest(manifest, seed):
    """Add `signature` and `key_id` to a manifest, in place."""
    _, public = keypair(seed)
    manifest.pop("signature", None)
    manifest.pop("key_id", None)
    manifest["key_id"] = signing.key_id(public)
    manifest["signature"] = sign(signing.canonical(manifest), seed).hex()
    return manifest


# -------------------------------------------------------------- commands


def cmd_keygen(args):
    seed, public = keypair()
    document = {"private": seed.hex(), "public": public.hex(),
                "key_id": signing.key_id(public)}
    if os.path.exists(args.out) and not args.force:
        print("%s exists; pass --force to overwrite it (and make sure you "
              "have the old one)" % args.out, file=sys.stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=1)
    try:
        os.chmod(args.out, 0o600)
    except OSError:
        pass
    print("key %s written to %s" % (document["key_id"], args.out))
    print("public key: %s" % document["public"])
    print("\nAdd it to the devices with:\n"
          "    python -m tools.release trust --key %s" % args.out)
    return 0


def cmd_trust(args):
    """Put a public key in the firmware's trusted list.

    From the moment a key is listed, an unsigned release stops being
    installable -- there is no separate switch, because a switch is a thing
    somebody forgets to turn on.
    """
    path = os.path.join(os.path.dirname(signing.__file__), signing.KEYS_FILE)
    keys = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            keys = json.load(fh).get("keys", [])

    for key_file in args.key:
        with open(key_file, "r", encoding="utf-8") as fh:
            document = json.load(fh)
        public = document["public"]
        if any(entry.get("key") == public for entry in keys):
            continue
        keys.append({"key": public, "key_id": document.get("key_id", ""),
                     "note": args.note or os.path.basename(key_file)})

    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"keys": keys}, fh, indent=1)
    print("%d trusted key(s) in %s" % (len(keys), path))
    if keys:
        print("signatures are now required: an unsigned release will be "
              "refused by any device running this firmware")
    return 0


def cmd_build(args):
    _check_version_file(args.version, args.force)
    os.makedirs(args.out, exist_ok=True)
    name = "trstenko-firmware-%s.tar.gz" % args.version
    path = os.path.join(args.out, name)
    size = bundle_mod.build(path, args.version,
                            requires_base=args.requires_base)
    digest = _sha256(path)

    manifest = {
        "version": args.version,
        "urgency": args.urgency,
        "summary": args.summary or "",
        "requires_base": args.requires_base,
        "requires_reboot": True,
        "size_bytes": size,
        "sha256": digest,
    }
    if args.min_version:
        manifest["min_version"] = args.min_version

    if args.key:
        with open(args.key, "r", encoding="utf-8") as fh:
            seed = bytes.fromhex(json.load(fh)["private"])
        sign_manifest(manifest, seed)

    manifest_path = os.path.join(args.out, "update.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)

    print("%s  (%.1f MB)" % (path, size / 1048576.0))
    print("%s  %s" % (manifest_path,
                      "signed by %s" % manifest["key_id"] if args.key
                      else "UNSIGNED — pass --key to sign it"))
    print("\nAttach both to a GitHub release tagged v%s." % args.version)
    return 0


def cmd_sign(args):
    with open(args.key, "r", encoding="utf-8") as fh:
        seed = bytes.fromhex(json.load(fh)["private"])
    with open(args.manifest, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    sign_manifest(manifest, seed)
    with open(args.manifest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)

    keys = signing.load_keys(*signing.default_key_paths())
    ok, used, reason = signing.check(manifest, keys)
    print("signed %s with %s" % (args.manifest, manifest["key_id"]))
    if ok:
        print("a device running this firmware would accept it (key %s)" % used)
    elif not keys:
        print("this firmware trusts no keys yet, so a device would install "
              "this without checking the signature at all.\n"
              "    python -m tools.release trust --key %s" % args.key)
    else:
        print("a device running this firmware would REFUSE it: %s" % reason)
    return 0


def _check_version_file(version, force=False):
    """`firmware/VERSION` has to match the release being built.

    The device reads that file to decide what it is running. Ship 0.4.1 with
    0.4.0 inside it and the updater offers 0.4.1 forever, installing it over
    and over -- which looks like a broken update system rather than a
    forgotten one-line commit.
    """
    from firmware.updates import version as version_mod

    running = str(version_mod.current())
    if running == str(version).lstrip("v"):
        return
    message = ("firmware/VERSION says %s but this release is %s. Commit the "
               "bump first." % (running, version))
    if force:
        print("warning: " + message, file=sys.stderr)
        return
    raise SystemExit(message)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    subs = ap.add_subparsers(dest="command", required=True)

    gen = subs.add_parser("keygen", help="make a signing keypair")
    gen.add_argument("--out", default="release-key.json")
    gen.add_argument("--force", action="store_true")
    gen.set_defaults(func=cmd_keygen)

    trust = subs.add_parser("trust", help="trust a public key on devices")
    trust.add_argument("--key", action="append", required=True)
    trust.add_argument("--note", default="")
    trust.set_defaults(func=cmd_trust)

    build = subs.add_parser("build", help="build a bundle and its manifest")
    build.add_argument("--version", required=True)
    build.add_argument("--out", default="dist")
    build.add_argument("--urgency", default="optional",
                       choices=("optional", "recommended", "urgent"))
    build.add_argument("--summary", default="")
    build.add_argument("--requires-base", type=int, default=1)
    build.add_argument("--min-version", default="")
    build.add_argument("--key", help="sign it with this private key")
    build.add_argument("--force", action="store_true",
                       help="build even if firmware/VERSION disagrees")
    build.set_defaults(func=cmd_build)

    sign_cmd = subs.add_parser("sign", help="sign an existing manifest")
    sign_cmd.add_argument("manifest")
    sign_cmd.add_argument("--key", required=True)
    sign_cmd.set_defaults(func=cmd_sign)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

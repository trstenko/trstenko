"""Bake the After Effects export into the data file the firmware ships.

    python -m tools.face.bake            # docs/ export -> firmware/face/data/faces.json
    python -m tools.face.bake --check    # non-zero exit if the bake is stale

Run it after every re-export from After Effects. The robot reads only the
output; `docs/` and this directory are not installed on it.
"""

import argparse
import datetime
import json
import os
import sys

from firmware.face import facedata
from tools.face import ae_import

OUT_PATH = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "firmware", "face", "data",
    "faces.json",
))


def _rows(track):
    out = []
    for key in track.keys:
        handles = list(key.out) if key.out else None
        out.append([round(key.t, 6), round(key.v, 6), handles,
                    1 if key.hold else 0])
    return out


def bake(src=None):
    states, raw = ae_import.load(src)

    baked = {}
    for name, state in sorted(states.items()):
        tracks = {}
        for (shape, prop), track in sorted(state.tracks.items()):
            tracks["%s.%s" % (shape, prop)] = _rows(track)

        baked[name] = {
            "duration": state.duration,
            "loop": state.loop,
            "note": state.note,
            "shapes": [
                {
                    "name": s.name,
                    "x": s.rest["x"], "y": s.rest["y"],
                    "w": s.rest["w"], "h": s.rest["h"], "r": s.rest["r"],
                    "color": list(s.color) if s.color else None,
                }
                for s in state.shapes
            ],
            "tracks": tracks,
        }

    return {
        "schema": facedata.SCHEMA,
        "baked": datetime.datetime.now(datetime.UTC)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "project": raw.get("project", ""),
            "exported": raw.get("exported", ""),
            "schema": raw.get("schema"),
        },
        "states": baked,
    }


def _serialise(doc):
    return json.dumps(doc, indent=1, sort_keys=False) + "\n"


def _without_timestamp(text):
    """Compare bakes by content. The timestamp always differs."""
    doc = json.loads(text)
    doc.pop("baked", None)
    return json.dumps(doc, sort_keys=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", help="faces.json from the AE exporter")
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the baked file is missing or stale")
    args = ap.parse_args(argv)

    source = args.src or ae_import.DEFAULT_PATH
    if not os.path.exists(source):
        # CI, and any checkout without the docs/ tree beside it. The bake is
        # committed, so a missing export means "cannot check", not "stale".
        print("no AE export at %s; nothing to compare the bake against"
              % source)
        return 0

    text = _serialise(bake(args.src))

    if args.check:
        if not os.path.exists(args.out):
            print("stale: %s does not exist" % args.out)
            return 1
        with open(args.out, "r", encoding="utf-8") as fh:
            current = fh.read()
        if _without_timestamp(current) != _without_timestamp(text):
            print("stale: %s does not match the AE export" % args.out)
            return 1
        print("up to date: %s" % args.out)
        return 0

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)

    states = json.loads(text)["states"]
    keyed = sum(len(s["tracks"]) for s in states.values())
    print("baked %d states, %d tracks -> %s" % (len(states), keyed, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

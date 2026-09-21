"""Load the baked face data that ships with the firmware.

`data/faces.json` here is not the After Effects export. It is written by
`tools/face/bake.py`, which reads the AE export out of `docs/` and resolves
every keyframe ease into plain cubic bezier handles. The conversion from AE's
(speed, influence) pairs happens on the build machine, once, so the device
never carries that code and never needs `docs/` to exist.

Bake before you ship:

    python -m tools.face.bake
"""

import json
import os

from .timeline import Key, Shape, State, Track

SCHEMA = 1

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "data", "faces.json")


def _track(rows):
    """[[t, v, handles_or_null, hold], ...] -> Track."""
    return Track([
        Key(row[0], row[1], row[2], hold=bool(row[3]))
        for row in rows
    ])


def load(path=None):
    """Return {name: State}. Raises OSError if the bake is missing."""
    path = path or DEFAULT_PATH
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    if data.get("schema") != SCHEMA:
        raise ValueError("faces.json schema %r, expected %r"
                         % (data.get("schema"), SCHEMA))

    states = {}
    for name, spec in data["states"].items():
        shapes = [
            Shape(s["name"], s["x"], s["y"], s["w"], s["h"], s["r"],
                  tuple(s["color"]) if s.get("color") else None)
            for s in spec["shapes"]
        ]
        tracks = {}
        for key, rows in spec.get("tracks", {}).items():
            shape_name, prop = key.rsplit(".", 1)
            tracks[(shape_name, prop)] = _track(rows)

        states[name] = State(
            name, spec["duration"], shapes, tracks,
            loop=spec.get("loop", True), note=spec.get("note", ""),
            source="ae" if tracks else "ae-rest",
        )

    return states, data

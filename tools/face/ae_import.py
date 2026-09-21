"""Read faces.json (written by 'Trstenko - Export Faces.jsx') into States.

Build-machine only. Nothing rendered in After Effects reaches the device, and
neither does this module: `tools/face/bake.py` runs it once and writes the
resolved curves to `firmware/face/data/faces.json`, which is what the robot
loads. The ease conversion below is the reason for that split -- it needs the
AE export, which lives in `docs/`, and `docs/` is not on the robot.
"""

import json
import math
import os

from firmware.face import easing
from firmware.face.timeline import Key, Shape, State, Track

# AE property path -> (our property name per dimension, is the speed a magnitude)
TRANSFORM = {
    "Transform/Position": (("x", "y"), True),
    "Transform/Scale": (("sx", "sy"), False),
    "Transform/Rotation": (("rot",), False),
    "Transform/Opacity": (("opacity",), False),
}

SCALED_BY_100 = ("sx", "sy")

# code/tools/face/ -> Trstenko/docs/assets/brand/face-states/ae/faces.json
DEFAULT_PATH = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..",
    "docs", "assets", "brand", "face-states", "ae", "faces.json",
))


def _ease_at(key, side, dim):
    eases = key.get(side)
    if not eases:
        return 0.0, 16.6667
    e = eases[dim] if dim < len(eases) else eases[0]
    return e.get("speed", 0.0), e.get("influence", 16.6667)


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _build_track(aekeys, dim, spatial, divisor):
    built = []
    for i, k in enumerate(aekeys):
        va = _as_list(k["v"])[dim]
        if i == len(aekeys) - 1:
            built.append(Key(k["t"], va / divisor, "linear"))
            break

        nxt = aekeys[i + 1]
        dt = nxt["t"] - k["t"]
        if spatial:
            # AE reports spatial speed along the motion path, not per axis.
            a, b = _as_list(k["v"]), _as_list(nxt["v"])
            dv = math.sqrt(sum((b[j] - a[j]) ** 2 for j in range(min(len(a), len(b), 3))))
        else:
            dv = _as_list(nxt["v"])[dim] - va

        s_out, i_out = _ease_at(k, "i_out", dim)
        s_in, i_in = _ease_at(nxt, "i_in", dim)
        handles = easing.ae_handles(dt, dv, s_out, i_out, s_in, i_in)

        is_hold = k.get("out") == "hold"
        built.append(Key(k["t"], va / divisor, handles, hold=is_hold))

    return Track(built)


def _layer_to_shape(layer):
    rest = {"w": 70.0, "h": 92.0, "r": 27.0, "x": 160.0, "y": 120.0}
    color = None

    for p in layer["properties"]:
        path = p["path"]
        if "keys" in p:
            continue
        v = p["value"]
        if path.endswith("Size"):
            rest["w"], rest["h"] = float(v[0]), float(v[1])
        elif path.endswith("Roundness"):
            rest["r"] = float(v)
        elif path.endswith("Color"):
            color = tuple(int(round(c * 255)) for c in v[:3])
        elif path == "Transform/Position":
            rest["x"], rest["y"] = float(v[0]), float(v[1])

    return Shape(layer["name"], rest["x"], rest["y"],
                 rest["w"], rest["h"], rest["r"], color)


def _layer_tracks(layer):
    tracks = {}
    for p in layer["properties"]:
        if "keys" not in p:
            continue
        mapping = TRANSFORM.get(p["path"])
        if mapping is None:
            continue
        names, spatial = mapping
        for dim, name in enumerate(names):
            divisor = 100.0 if name in SCALED_BY_100 else 1.0
            tracks[(layer["name"], name)] = _build_track(
                p["keys"], dim, spatial, divisor
            )
    return tracks


def load(path=None):
    """Return {comp_name: State}. Comps with no animated property still load,
    so their rest pose is available even if nobody has keyed them yet."""
    path = path or DEFAULT_PATH
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    states = {}
    for comp in data["comps"]:
        shapes = []
        tracks = {}
        # AE layer 1 is the topmost; draw back to front.
        for layer in sorted(comp["layers"], key=lambda l: -l["index"]):
            if not layer.get("enabled", True) or layer.get("guide"):
                continue
            shapes.append(_layer_to_shape(layer))
            tracks.update(_layer_tracks(layer))

        loop = any(m["name"] == "loop" for m in comp.get("markers", []))
        states[comp["name"]] = State(
            comp["name"], comp["duration"], shapes, tracks,
            loop=loop, note=comp.get("note", ""),
            source="ae" if tracks else "ae-rest",
        )

    return states, data


def animated_props(state):
    return set(state.tracks.keys())

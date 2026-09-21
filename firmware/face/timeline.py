"""Keyframe tracks and the sampler.

A Track is one animated scalar on one shape -- `("eye_l", "sy")`. Everything the
device animates reduces to scalars, which is why a 320x240 face costs four
rounded rects and no frame buffer.
"""

from . import easing


class Key:
    __slots__ = ("t", "v", "out", "hold")

    def __init__(self, t, v, out="ease", hold=False):
        self.t = float(t)
        self.v = float(v)
        self.out = easing.resolve(out)
        self.hold = hold


class Track:
    """Keys on one scalar, sampled by time. Keys must be sorted."""

    def __init__(self, keys):
        self.keys = sorted(keys, key=lambda k: k.t)

    def sample(self, t):
        keys = self.keys
        if not keys:
            return 0.0
        if t <= keys[0].t:
            return keys[0].v
        if t >= keys[-1].t:
            return keys[-1].v

        lo = 0
        hi = len(keys) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if keys[mid].t <= t:
                lo = mid
            else:
                hi = mid

        a, b = keys[lo], keys[hi]
        if a.hold or b.t == a.t:
            return a.v
        p = (t - a.t) / (b.t - a.t)
        return a.v + (b.v - a.v) * easing.ease(a.out, p)


class Shape:
    """Rest pose of one rounded rect. Tracks move it away from this."""

    DEFAULTS = {"sx": 1.0, "sy": 1.0, "rot": 0.0, "opacity": 100.0}

    def __init__(self, name, x, y, w, h, r, color=None):
        self.name = name
        self.rest = {
            "x": float(x), "y": float(y),
            "w": float(w), "h": float(h), "r": float(r),
            "sx": 1.0, "sy": 1.0, "rot": 0.0, "opacity": 100.0,
        }
        self.color = color


class State:
    """One face state: shapes, tracks, duration, and how time wraps."""

    def __init__(self, name, duration, shapes, tracks=None, loop=True,
                 note="", runtime=None, source="code"):
        self.name = name
        self.duration = float(duration)
        self.shapes = shapes
        self.tracks = tracks or {}
        self.loop = loop
        self.note = note
        self.runtime = runtime
        self.source = source

    def local_time(self, t):
        if self.loop and self.duration > 0:
            return t % self.duration
        return min(t, self.duration)

    def sample(self, t, params=None):
        """Return a list of resolved shape dicts for time `t`."""
        lt = self.local_time(t)
        out = []
        for shape in self.shapes:
            cur = dict(shape.rest)
            for prop in cur:
                track = self.tracks.get((shape.name, prop))
                if track is not None:
                    cur[prop] = track.sample(lt)
            cur["name"] = shape.name
            cur["color"] = shape.color
            out.append(cur)

        if self.runtime is not None:
            self.runtime(out, lt, params or {})
        return out

    def merge_tracks(self, other, prefer_other=True):
        """Fold another track dict in. Used to let AE keys win over code ones."""
        for key, track in other.items():
            if prefer_other or key not in self.tracks:
                self.tracks[key] = track


def keys(*pairs, **kw):
    """keys((0, 1.0), (0.5, 0.2, "ease_in"), (0.8, 1.0)) -> Track"""
    default_ease = kw.get("ease", "ease")
    built = []
    for pair in pairs:
        t, v = pair[0], pair[1]
        out = pair[2] if len(pair) > 2 else default_ease
        built.append(Key(t, v, out, hold=(out == "hold")))
    return Track(built)

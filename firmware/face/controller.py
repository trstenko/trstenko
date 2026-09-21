"""Holds which state the face is in, and eases between them.

Cutting between states is what a state machine does; it is not what a face
does. Switching straight from idle to listening pops the eyes to a new size in
one frame, which reads as a glitch rather than as the device waking up. This
blends the outgoing and incoming poses over a fifth of a second.

Shapes are matched by name, so eyes tween into eyes. A shape only one side has
-- the mic glyph, the error brows -- fades instead. Colour is interpolated too,
which is what carries teal into amber when something breaks.
"""

from . import easing
from .timeline import Shape

BLEND = 0.22
BLEND_EASE = easing.NAMED["ease"]

LERPED = ("x", "y", "w", "h", "r", "sx", "sy", "opacity")


def _lerp(a, b, m):
    return a + (b - a) * m


def _lerp_angle(a, b, m):
    """Shortest way round, so a brow at 18 deg never spins the long way to 0."""
    d = (b - a + 180.0) % 360.0 - 180.0
    return a + d * m


def _lerp_color(a, b, m):
    a = a or (27, 127, 155)
    b = b or (27, 127, 155)
    return tuple(int(round(_lerp(a[i], b[i], m))) for i in range(3))


def _blend(a, b, m):
    """One shape's worth. Either side may be None, meaning it fades."""
    if a is None and b is None:
        return None
    if a is None:
        out = dict(b)
        out["opacity"] = b["opacity"] * m
        return out
    if b is None:
        out = dict(a)
        out["opacity"] = a["opacity"] * (1.0 - m)
        return out

    out = dict(b)
    for k in LERPED:
        out[k] = _lerp(a[k], b[k], m)
    out["rot"] = _lerp_angle(a["rot"], b["rot"], m)
    out["color"] = _lerp_color(a.get("color"), b.get("color"), m)
    return out


class FaceController:
    """Drive this from the interaction loop: `set(name)` on every state change,
    `sample(now)` once per frame."""

    def __init__(self, states, current="face_idle", blend=BLEND, follow=None):
        self.states = states
        self.blend = blend
        self.follow = follow or {}

        self.blend_len = blend
        self.current = current
        self.started = 0.0
        self.prev = None
        self.prev_started = 0.0
        self.blend_from = None

    # ---- control ----

    def set(self, name, now, blend=None):
        """Begin easing to `name`. Setting the state it is already in is a
        no-op, so the interaction loop can call this every frame if it wants."""
        if name == self.current or name not in self.states:
            return
        self.prev = self.current
        self.prev_started = self.started
        self.blend_from = now
        self.blend_len = self.blend if blend is None else blend
        self.current = name
        self.started = now

    def progress(self, now):
        """0 while blending, 1 once settled."""
        if self.blend_from is None:
            return 1.0
        if self.blend_len <= 0:
            return 1.0
        return min(1.0, (now - self.blend_from) / self.blend_len)

    def blit(self):
        """Union of both regions while blending, since both are on screen."""
        cur = self.states[self.current].blit
        if self.prev is None:
            return cur
        old = self.states[self.prev].blit
        return (min(cur[0], old[0]), min(cur[1], old[1]),
                max(cur[2], old[2]), max(cur[3], old[3]))

    # ---- frame ----

    def _auto_advance(self, now):
        """A one-shot that has played out hands over to its follow-up, so boot
        lands in idle and the wake acknowledgement lands in listening without
        the caller having to time it."""
        state = self.states[self.current]
        if state.loop:
            return
        nxt = self.follow.get(self.current)
        if nxt and now - self.started >= state.duration:
            self.set(nxt, now)

    def sample(self, now, params=None):
        self._auto_advance(now)

        state = self.states[self.current]
        shapes = state.sample(now - self.started, params)

        p = self.progress(now)
        if p >= 1.0 or self.prev is None:
            self.prev = None
            self.blend_from = None
            return shapes

        m = easing.ease(BLEND_EASE, p)
        old = self.states[self.prev]
        # The outgoing state keeps running during the blend. Freezing it makes
        # the handover look like a dissolve between two photographs.
        old_shapes = {s["name"]: s for s in old.sample(now - self.prev_started,
                                                       params)}
        new_shapes = {s["name"]: s for s in shapes}

        order = [s["name"] for s in shapes]
        order += [n for n in old_shapes if n not in new_shapes]

        out = []
        for name in order:
            blended = _blend(old_shapes.get(name), new_shapes.get(name), m)
            if blended is not None:
                out.append(blended)
        return out

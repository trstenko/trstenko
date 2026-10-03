"""Cubic bezier easing, and the conversion from After Effects' temporal ease.

AE stores a keyframe ease as (speed, influence) rather than as bezier handles.
Speed is in value units per second, influence is a percentage of the segment
duration. `ae_handles` turns a pair of those into the normalized handles the
tween engine uses, which is what lets a curve drawn in the graph editor survive
the trip to the device unchanged.
"""

NAMED = {
    "linear": (0.0, 0.0, 1.0, 1.0),
    "ease": (0.42, 0.0, 0.58, 1.0),
    "ease_in": (0.42, 0.0, 1.0, 1.0),
    "ease_out": (0.0, 0.0, 0.58, 1.0),
    "snap": (0.9, 0.0, 1.0, 1.0),
    "back_out": (0.34, 1.56, 0.64, 1.0),
    "hold": None,
}


def _cubic(a, b, u):
    """Bezier with implicit endpoints at 0 and 1."""
    v = 1.0 - u
    return 3.0 * v * v * u * a + 3.0 * v * u * u * b + u * u * u


def _solve(x1, x2, p):
    """Find u where the curve's x equals p. Newton, bisection as backstop."""
    u = p
    for _ in range(8):
        err = _cubic(x1, x2, u) - p
        if abs(err) < 1e-6:
            return u
        v = 1.0 - u
        d = 3.0 * v * v * x1 + 6.0 * v * u * (x2 - x1) + 3.0 * u * u * (1.0 - x2)
        if abs(d) < 1e-6:
            break
        u -= err / d

    lo, hi = 0.0, 1.0
    for _ in range(24):
        u = 0.5 * (lo + hi)
        if _cubic(x1, x2, u) < p:
            lo = u
        else:
            hi = u
    return u


def ease(handles, p):
    """Map linear progress 0..1 through the curve. May exceed 1 for overshoot."""
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    x1, y1, x2, y2 = handles
    return _cubic(y1, y2, _solve(x1, x2, p))


def ae_handles(dt, dv, speed_out, inf_out, speed_in, inf_in):
    """AE (speed, influence) pair -> normalized bezier handles.

    `dv` is the value change across the segment; for a spatial property pass the
    euclidean length of the move, because AE reports spatial speed as a
    magnitude along the motion path rather than per axis.

    A segment with no value change has no meaningful curve, so it falls back to
    linear -- the value is constant either way.
    """
    if dt <= 0 or dv == 0:
        return NAMED["linear"]

    x1 = max(0.0, min(1.0, inf_out / 100.0))
    x2 = max(0.0, min(1.0, 1.0 - inf_in / 100.0))
    y1 = (speed_out * dt * x1) / dv
    y2 = 1.0 - (speed_in * dt * (inf_in / 100.0)) / dv
    return (x1, y1, x2, y2)


def resolve(spec):
    """Accept a name, an explicit 4-tuple, or None (meaning linear)."""
    if spec is None:
        return NAMED["linear"]
    if isinstance(spec, str):
        return NAMED.get(spec, NAMED["ease"])
    return tuple(spec)

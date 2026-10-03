"""The idle acts and the alert face.

An idle face that only blinks and breathes is alive for a minute and furniture
after an hour. An act is a few seconds of the face doing something -- a glance,
a yawn, a nap -- played once, every several minutes, and handed back to
`face_idle` by the controller. Each one starts and ends on the idle pose, so
the blend in and out has nothing to hide.

`face_alert` is the face for a ringing reminder: like the muted face it borrows
a symbol people already know, a bell. `face_timer` is a timer going off, with
no symbol: the face itself calls out.

Everything here must stay inside the blit region (x 48-272, y 32-192): what
leaves it is not sent to the panel.
"""

import math

from .states import CX, EYE_L, EYE_R, EYES
from .timeline import keys

EYE_Y = 106.0
EYE_W, EYE_H, EYE_R_ = 70.0, 92.0, 27.0
# Idle holds back from full brightness; a livelier act goes to 100.
DIM = 88.0

IDLE_EYES = [("eye_l", EYE_L, EYE_Y, EYE_W, EYE_H, EYE_R_),
             ("eye_r", EYE_R, EYE_Y, EYE_W, EYE_H, EYE_R_)]
REST_X = {"eye_l": EYE_L, "eye_r": EYE_R}


def _both(tracks, prop, *pairs):
    for eye in EYES:
        tracks[(eye, prop)] = keys(*pairs)


def _shift(tracks, *pairs):
    """The same sideways move on both eyes, given as offsets from rest."""
    for eye in EYES:
        tracks[(eye, "x")] = keys(
            *[(p[0], REST_X[eye] + p[1]) + tuple(p[2:]) for p in pairs])


def _blink(t0, base=1.0, close=0.06, open_=0.09):
    return [(t0, base, "ease_in"), (t0 + close, 0.06, "ease_out"),
            (t0 + close + open_, base, "ease")]


# ---- idle acts ----

def _look():
    """A glance to one side, then the other. The eye on the side it looks
    toward narrows, which is what a turning head does and what keeps the pair
    inside the blit region."""
    d = 3.4
    t = {}
    _shift(t, (0.0, 0), (0.3, 0), (0.6, -12), (1.3, -12), (1.75, 12), (2.45, 12),
           (2.8, 0), (d, 0))
    t[("eye_l", "sx")] = keys((0.0, 1.0), (0.3, 1.0), (0.6, 0.84), (1.3, 0.84),
                              (1.75, 1.0), (2.45, 1.0), (d, 1.0))
    t[("eye_r", "sx")] = keys((0.0, 1.0), (1.3, 1.0), (1.75, 0.84), (2.45, 0.84),
                              (2.8, 1.0), (d, 1.0))
    _both(t, "y", (0.0, EYE_Y), (0.3, EYE_Y), (0.6, 104.0), (1.3, 104.0),
          (1.52, 106.5), (1.75, 104.0), (2.45, 104.0), (2.8, EYE_Y), (d, EYE_Y))
    # People blink as their gaze moves; without it the eyes slide like a slider.
    _both(t, "sy", (0.0, 1.0), *_blink(1.42), *_blink(2.95), (d, 1.0))
    _both(t, "opacity", (0.0, DIM), (d, DIM))
    return d, IDLE_EYES, t


def _curious():
    """A tilt of the head: one eye up and larger, the other down and smaller."""
    d = 3.0
    t = {}
    for eye, big, y in (("eye_l", 1.13, 101.0), ("eye_r", 0.8, 111.0)):
        t[(eye, "sy")] = keys(
            (0.0, 1.0), (0.25, 1.0), (0.6, big), *_blink(1.3, big),
            (2.0, big), (2.4, 1.0), *_blink(2.55), (d, 1.0))
        t[(eye, "y")] = keys((0.0, EYE_Y), (0.25, EYE_Y), (0.6, y), (2.0, y),
                             (2.4, EYE_Y), (d, EYE_Y))
        t[(eye, "rot")] = keys((0.0, 0.0), (0.25, 0.0), (0.6, 6.0), (2.0, 6.0),
                               (2.4, 0.0), (d, 0.0))
    _both(t, "opacity", (0.0, DIM), (0.6, 100.0), (2.0, 100.0), (2.4, DIM),
          (d, DIM))
    return d, IDLE_EYES, t


def _sleepy():
    """Nodding off twice and shaking it away."""
    d = 5.0
    t = {}
    _both(t, "sy", (0.0, 1.0), (0.4, 1.0, "ease_in"), (1.7, 0.38, "ease_out"),
          (1.85, 1.06), (2.05, 1.0), (2.3, 1.0, "ease_in"),
          (3.4, 0.22), (3.75, 0.22, "ease_out"), (3.9, 1.1), (4.1, 1.0),
          *_blink(4.72), (d, 1.0))
    _both(t, "y", (0.0, EYE_Y), (0.4, EYE_Y, "ease_in"), (1.7, 116.0, "ease_out"),
          (1.85, 103.0), (2.05, EYE_Y), (2.3, EYE_Y, "ease_in"),
          (3.4, 121.0), (3.75, 122.0, "ease_out"), (3.9, 101.0), (4.1, EYE_Y),
          (d, EYE_Y))
    _both(t, "opacity", (0.0, DIM), (0.4, DIM), (1.7, 70.0), (1.85, DIM),
          (2.3, DIM), (3.4, 62.0), (3.75, 62.0), (3.9, 100.0), (4.6, DIM),
          (d, DIM))
    _shift(t, (0.0, 0), (4.1, 0), (4.2, -4), (4.32, 4), (4.44, -3), (4.56, 2),
           (4.66, 0), (d, 0))
    return d, IDLE_EYES, t


def _yawn():
    """Eyes squeezed shut, and the only time the idle face has a mouth."""
    d = 4.0
    t = {}
    _both(t, "sy", (0.0, 1.0), (0.3, 1.0, "ease_in"), (0.9, 0.12), (2.5, 0.1),
          (2.9, 1.0), *_blink(3.3), (d, 1.0))
    _both(t, "sx", (0.0, 1.0), (0.3, 1.0), (0.9, 1.08), (2.5, 1.08), (2.9, 1.0),
          (d, 1.0))
    _both(t, "y", (0.0, EYE_Y), (0.3, EYE_Y), (0.9, 92.0), (2.1, 89.0),
          (2.5, 90.0), (2.9, EYE_Y), (d, EYE_Y))
    _both(t, "opacity", (0.0, DIM), (d, DIM))
    t[("mouth", "opacity")] = keys((0.0, 0.0), (0.45, 0.0), (0.7, DIM),
                                   (2.55, DIM), (2.85, 0.0), (d, 0.0))
    t[("mouth", "sy")] = keys((0.0, 0.2), (0.45, 0.2), (1.3, 1.1), (2.1, 1.15),
                              (2.7, 0.2), (d, 0.2))
    t[("mouth", "sx")] = keys((0.0, 0.9), (0.45, 0.9), (1.3, 1.0), (2.1, 0.92),
                              (2.7, 0.7), (d, 0.7))
    t[("mouth", "y")] = keys((0.0, 146.0), (0.45, 146.0), (1.3, 152.0),
                             (2.1, 153.0), (2.7, 146.0), (d, 146.0))
    shapes = IDLE_EYES + [("mouth", CX, 146.0, 52.0, 48.0, 24.0)]
    return d, shapes, t


def _happy():
    """Two hops and a squint. Squash on the ground, stretch in the air."""
    d = 2.6
    t = {}
    _both(t, "y", (0.0, EYE_Y), (0.2, EYE_Y, "ease_in"), (0.3, 110.0, "ease_out"),
          (0.52, 90.0, "ease_in"), (0.74, 108.0, "ease_out"),
          (0.86, 110.0, "ease_out"), (1.06, 94.0, "ease_in"),
          (1.26, 108.0, "ease_out"), (1.45, EYE_Y), (d, EYE_Y))
    _both(t, "sy", (0.0, 1.0), (0.2, 1.0), (0.3, 0.82), (0.52, 1.1),
          (0.74, 0.8), (0.86, 0.8), (1.06, 1.08), (1.26, 0.8), (1.45, 0.62),
          (2.0, 0.62), (2.25, 1.0), (d, 1.0))
    _both(t, "sx", (0.0, 1.0), (0.2, 1.0), (0.3, 1.08), (0.52, 0.96),
          (0.74, 1.1), (1.06, 0.96), (1.26, 1.1), (2.0, 1.1), (2.25, 1.0),
          (d, 1.0))
    _both(t, "opacity", (0.0, DIM), (0.3, 100.0), (2.0, 100.0), (2.4, DIM),
          (d, DIM))
    return d, IDLE_EYES, t


def _doze():
    """A short nap: eyes shut and sunk, three bubbles drifting up, then a start
    and a quick look both ways to see who noticed."""
    d = 9.0
    t = {}
    _both(t, "sy", (0.0, 1.0), (0.5, 1.0, "ease_in"), (2.0, 0.1),
          (6.4, 0.1, "ease_out"), (6.55, 1.18), (6.8, 1.0), *_blink(8.1),
          (d, 1.0))
    _both(t, "y", (0.0, EYE_Y), (0.5, EYE_Y), (2.0, 118.0), (3.5, 121.0),
          (5.0, 118.0), (6.4, 121.0, "ease_out"), (6.55, 100.0), (6.8, EYE_Y),
          (d, EYE_Y))
    _both(t, "opacity", (0.0, DIM), (0.5, DIM), (2.0, 55.0), (6.4, 55.0),
          (6.55, 100.0), (7.6, 100.0), (8.0, DIM), (d, DIM))
    _both(t, "sx", (0.0, 1.0), (6.9, 1.0), (7.05, 0.9), (7.75, 0.9),
          (7.95, 1.0), (d, 1.0))
    _shift(t, (0.0, 0), (6.9, 0), (7.05, -8), (7.3, -8), (7.5, 8), (7.75, 8),
           (7.95, 0), (d, 0))

    shapes = list(IDLE_EYES)
    for i, start in enumerate((2.4, 3.6, 4.8)):
        name = "zz_%d" % (i + 1)
        end = start + 1.5
        shapes.append((name, 236.0, 70.0, 18.0, 18.0, 9.0))
        t[(name, "x")] = keys((0.0, 236.0), (start, 236.0), (end, 257.0),
                              (d, 257.0))
        t[(name, "y")] = keys((0.0, 70.0), (start, 70.0, "ease_out"),
                              (end, 47.0), (d, 47.0))
        t[(name, "sx")] = keys((0.0, 0.45), (start, 0.45), (end, 1.0), (d, 1.0))
        t[(name, "sy")] = keys((0.0, 0.45), (start, 0.45), (end, 1.0), (d, 1.0))
        t[(name, "opacity")] = keys((0.0, 0.0), (start, 0.0),
                                    (start + 0.35, 85.0), (end - 0.5, 85.0),
                                    (end, 0.0), (d, 0.0))
    return d, shapes, t


def _roll():
    """Eyes up and once round. A little impatient, a little bored."""
    d = 2.6
    t = {}
    begin, turn = 0.5, 1.3
    xs = [(0.0, 0.0), (0.25, 0.0), (begin, 0.0)]
    ys = [(0.0, EYE_Y), (0.25, EYE_Y), (begin, 97.0, "linear")]
    steps = 14
    for i in range(1, steps + 1):
        a = 2.0 * math.pi * i / steps
        at = begin + turn * i / steps
        xs.append((at, 8.0 * math.sin(a), "linear"))
        ys.append((at, EYE_Y - 9.0 * math.cos(a), "linear"))
    xs += [(begin + turn + 0.25, 0.0), (d, 0.0)]
    ys += [(begin + turn + 0.25, EYE_Y), (d, EYE_Y)]
    _shift(t, *xs)
    _both(t, "y", *ys)
    _both(t, "sx", (0.0, 1.0), (0.25, 1.0), (begin, 0.9),
          (begin + turn, 0.9), (begin + turn + 0.25, 1.0), (d, 1.0))
    _both(t, "sy", (0.0, 1.0), (0.25, 1.0), (begin, 0.92),
          (begin + turn, 0.92), (begin + turn + 0.25, 1.0), *_blink(2.2),
          (d, 1.0))
    _both(t, "opacity", (0.0, DIM), (d, DIM))
    return d, IDLE_EYES, t


IDLE_BUILDERS = {
    "face_idle_look": _look,
    "face_idle_curious": _curious,
    "face_idle_sleepy": _sleepy,
    "face_idle_yawn": _yawn,
    "face_idle_happy": _happy,
    "face_idle_doze": _doze,
    "face_idle_roll": _roll,
}


# ---- the alert face ----

BELL_PIVOT = (CX, 106.0)
# name, distance below the pivot, w, h, r, how far it swings against the bell
BELL_PARTS = (
    ("bell_knob", 2.0, 12.0, 12.0, 6.0, 1.0),
    ("bell_body", 32.0, 44.0, 60.0, 22.0, 1.0),
    ("bell_rim", 58.0, 70.0, 12.0, 6.0, 1.0),
    # The clapper trails the bell and swings wider: that is what makes it ring.
    ("bell_clap", 71.0, 14.0, 14.0, 7.0, 1.5),
)
BELL_SWING = 17.0          # degrees
BELL_BEATS = 3.5           # swings a second
RING, FADE = 0.7, 1.1      # full swing until RING, still by FADE


def _bell_angle(at):
    if at <= 0.0 or at >= FADE:
        return 0.0
    env = 1.0 if at < RING else (FADE - at) / (FADE - RING)
    return BELL_SWING * env * math.sin(2.0 * math.pi * BELL_BEATS * at)


def alert_bell():
    """Small bright eyes over a bell that rings for a second and rests for a
    second. A bell is the one picture everybody already reads as "reminder"."""
    d = 2.0
    t = {}
    _both(t, "sy", (0.0, 0.5), (0.12, 0.58), (0.9, 0.58), (1.15, 0.5),
          *_blink(1.5, 0.5), (d, 0.5))
    _both(t, "y", (0.0, 74.0), (0.12, 71.0), (0.9, 71.0), (1.15, 74.0),
          (d, 74.0))
    _both(t, "opacity", (0.0, 100.0), (d, 100.0))

    px, py = BELL_PIVOT
    frames = int(FADE * 30) + 1
    for name, dist, _w, _h, _r, gain in BELL_PARTS:
        lag = 0.04 if gain != 1.0 else 0.0
        xs, ys, rots = [], [], []
        for i in range(frames + 1):
            at = i / 30.0
            deg = _bell_angle(at - lag) * gain
            rad = math.radians(deg)
            xs.append((at, px + dist * math.sin(rad), "linear"))
            ys.append((at, py + dist * math.cos(rad), "linear"))
            rots.append((at, deg, "linear"))
        t[(name, "x")] = keys(*xs, (d, px))
        t[(name, "y")] = keys(*ys, (d, py + dist))
        t[(name, "rot")] = keys(*rots, (d, 0.0))

    shapes = [("eye_l", EYE_L, 74.0, EYE_W, EYE_H, EYE_R_),
              ("eye_r", EYE_R, 74.0, EYE_W, EYE_H, EYE_R_)]
    shapes += [(name, px, py + dist, w, h, r)
               for name, dist, w, h, r, _gain in BELL_PARTS]
    return d, shapes, t


def alert_hey():
    """No symbol: two hops and a round mouth calling out, then a breath."""
    d = 1.6
    t = {}
    _both(t, "y", (0.0, 86.0, "ease_out"), (0.12, 78.0, "ease_in"),
          (0.26, 88.0, "ease_out"), (0.32, 86.0, "ease_out"),
          (0.44, 78.0, "ease_in"), (0.58, 88.0, "ease_out"), (0.68, 86.0),
          (d, 86.0))
    _both(t, "sy", (0.0, 1.0), (0.12, 1.06), (0.26, 0.88), (0.32, 0.95),
          (0.44, 1.06), (0.58, 0.88), (0.68, 1.0), *_blink(1.15), (d, 1.0))
    _both(t, "opacity", (0.0, 100.0), (d, 100.0))
    for prop in ("sx", "sy"):
        t[("mouth", prop)] = keys(
            (0.0, 1.0, "ease_out"), (0.12, 1.4), (0.26, 1.0),
            (0.32, 1.0, "ease_out"), (0.44, 1.4), (0.58, 1.0), (0.8, 0.8),
            (d, 1.0))
    t[("mouth", "opacity")] = keys((0.0, 100.0), (d, 100.0))
    shapes = [("eye_l", EYE_L, 86.0, EYE_W, 80.0, EYE_R_),
              ("eye_r", EYE_R, 86.0, EYE_W, 80.0, EYE_R_),
              ("mouth", CX, 160.0, 34.0, 34.0, 17.0)]
    return d, shapes, t


# A reminder has something to tell you, so it gets the symbol. A timer only
# has to be noticed.
ALERT_BUILDERS = {
    "face_alert": alert_bell,
    "face_timer": alert_hey,
}

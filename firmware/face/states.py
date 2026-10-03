"""The face states, animated in code.

`face_boot` was animated in After Effects and arrives through `facedata`, which
reads the file `tools/face/bake.py` writes. The rest are keyed here, in the same
vocabulary the AE comps use: position, scale, rotation and opacity on named
rounded rects.

AE wins wherever it has keys. A property animated in the comp is never
overwritten by anything in this file, so re-exporting faces.json takes over a
state one property at a time -- animate `face_idle`'s scale in AE and the blink
here stops applying, while the breathing on Y keeps working until that is keyed
too. `PREFER_CODE` lists the states that invert that rule.
"""

from .timeline import Shape, State, keys

TEAL = (27, 127, 155)
DARK = (18, 25, 29)

W, H = 320, 240
CX = 160
SEP = 68
EYE_L, EYE_R = CX - SEP, CX + SEP

# The blit region. Motion outside this is clipped on the device.
DIRTY_RECT = (48, 32, 272, 192)
FULL_SCREEN = (0, 0, W, H)

# face_boot rises from below the screen, so it needs the whole panel. It costs
# 38ms a frame instead of 11, which is affordable exactly once: nothing else is
# running during boot, and it plays at whatever rate it plays at.
BLIT_REGION = {"face_boot": FULL_SCREEN}


def blit_region(name):
    return BLIT_REGION.get(name, DIRTY_RECT)

# Fallback rest poses, matching 'Trstenko - Build Face Comps.jsx'. Only used
# when the baked data is missing; normally the rest pose comes from the export.
REST = {
    "face_idle": (6.0, True, [
        ("eye_l", EYE_L, 106, 70, 92, 27), ("eye_r", EYE_R, 106, 70, 92, 27)]),
    "face_listening": (3.0, True, [
        ("eye_l", EYE_L, 106, 76, 124, 29), ("eye_r", EYE_R, 106, 76, 124, 29)]),
    "face_thinking": (2.0, True, [
        ("dot_1", 98, 115, 35, 35, 15), ("dot_2", 160, 115, 35, 35, 15),
        ("dot_3", 222, 115, 35, 35, 15)]),
    "face_talking": (3.0, True, [
        ("eye_l", EYE_L, 77, 70, 79, 27), ("eye_r", EYE_R, 77, 70, 79, 27),
        ("mouth", CX, 162, 106, 41, 20.5)]),
    "face_wake": (1.0, False, [
        ("eye_l", EYE_L, 106, 70, 92, 27), ("eye_r", EYE_R, 106, 70, 92, 27)]),
    "face_muted": (3.0, True, [
        ("eye_l", EYE_L, 106, 70, 92, 27), ("eye_r", EYE_R, 106, 70, 92, 27),
        ("mouth", CX, 162, 106, 12, 6)]),
    "face_boot": (2.5, False, [
        ("eye_l", EYE_L, 106, 70, 92, 27), ("eye_r", EYE_R, 106, 106, 92, 27)]),
    "face_error": (3.0, True, [
        ("eye_l", EYE_L, 106, 70, 92, 27), ("eye_r", EYE_R, 106, 70, 92, 27),
        ("mouth", CX, 162, 106, 41, 20.5)]),
}

EYES = ("eye_l", "eye_r")


def _blink(t0, close=0.06, open_=0.09):
    """One blink as (time, value, ease) triples on scale Y."""
    return [
        (t0, 1.0, "ease_in"),
        (t0 + close, 0.06, "ease_out"),
        (t0 + close + open_, 1.0, "ease"),
    ]


def _blink_squash(t0, close=0.06, open_=0.09):
    """Eyes widen slightly as they close. Without this a blink reads as a glitch."""
    return [
        (t0, 1.0, "ease_in"),
        (t0 + close, 1.05, "ease_out"),
        (t0 + close + open_, 1.0, "ease"),
    ]


def _idle():
    blinks = [1.4, 3.6, 3.88]          # single, then a double
    sy = [(0.0, 1.0)]
    sx = [(0.0, 1.0)]
    for t0 in blinks:
        sy += _blink(t0)
        sx += _blink_squash(t0)
    sy.append((6.0, 1.0))
    sx.append((6.0, 1.0))

    tracks = {}
    for eye in EYES:
        tracks[(eye, "sy")] = keys(*sy)
        tracks[(eye, "sx")] = keys(*sx)
        # Breathing. Small enough to be felt rather than seen.
        tracks[(eye, "y")] = keys(
            (0.0, 106.0), (1.5, 107.2), (3.0, 106.0), (4.5, 107.2), (6.0, 106.0))
        # Idle holds back, so waking is a step up in brightness as well as size.
        tracks[(eye, "opacity")] = keys((0.0, 88.0), (6.0, 88.0))
    return tracks


def _listening_runtime(shapes, t, params):
    """Eye height follows the mic level.

    Dilated eyes alone did not read as listening -- they are idle, larger.
    Reacting to the room does, for the same reason the talking mouth works: the
    face is visibly responding to something the user is doing, and no amount of
    self-contained idle motion can stand in for that."""
    amp = max(0.0, min(1.0, float(params.get("amp", 0.0))))
    top, bottom = DIRTY_RECT[1] + 3, DIRTY_RECT[3] - 3
    for s in shapes:
        if not s["name"].startswith("eye"):
            continue
        s["sy"] *= 1.0 + 0.22 * amp
        s["sx"] *= 1.0 + 0.05 * amp
        # A loud room must not push the eyes outside the blit region, where
        # the device would simply not send them.
        limit = min(s["y"] - top, bottom - s["y"])
        if s["h"] * s["sy"] / 2.0 > limit:
            s["sy"] = 2.0 * limit / s["h"]


def _listening():
    tracks = {}
    for eye in EYES:
        # A wider pulse than idle's breathing, so the state still reads in
        # silence, and full brightness against idle's 88.
        # Resting below full height leaves the mic reaction somewhere to go.
        # At 1.0 the eyes already fill the blit region and can only shrink.
        tracks[(eye, "sx")] = keys((0.0, 1.0), (1.4, 1.04), (2.8, 1.0), (3.0, 1.0))
        tracks[(eye, "sy")] = keys((0.0, 0.88), (1.4, 0.95), (2.8, 0.88), (3.0, 0.88))
        tracks[(eye, "y")] = keys((0.0, 108.0), (1.4, 106.0), (2.8, 108.0), (3.0, 108.0))
        tracks[(eye, "opacity")] = keys((0.0, 100.0), (3.0, 100.0))
    return tracks


# Thinking keeps the eyes and demotes the dots to a caption underneath. The
# dots-only version was the odd one out in the set -- every other state is a
# face, so for two to four seconds mid-conversation the face simply vanished.
THINKING_DOTS = ((134.0, 0.0), (160.0, 0.14), (186.0, 0.28))
DOT_SCALE = 0.46          # the comp's dots are 35px; 16px reads as a caption
DOT_Y = 170.0


def _thinking():
    tracks = {}
    for dot, (x, off) in zip(("dot_1", "dot_2", "dot_3"), THINKING_DOTS):
        up, down = off + 0.16, off + 0.36
        tracks[(dot, "x")] = keys((0.0, x), (2.0, x))
        y = [(0.0, DOT_Y)]
        if off > 0:
            y.append((off, DOT_Y))
        y += [(up, DOT_Y - 9.0, "ease_out"), (down, DOT_Y, "ease_in"),
              (2.0, DOT_Y)]
        tracks[(dot, "y")] = keys(*y)
        # Squash on the landing, recovered before the next dot lifts.
        tracks[(dot, "sy")] = keys(
            (0.0, DOT_SCALE), (down, DOT_SCALE, "ease_out"),
            (down + 0.06, DOT_SCALE * 0.76, "ease_out"),
            (down + 0.16, DOT_SCALE), (2.0, DOT_SCALE))
        tracks[(dot, "sx")] = keys(
            (0.0, DOT_SCALE), (down, DOT_SCALE, "ease_out"),
            (down + 0.06, DOT_SCALE * 1.18, "ease_out"),
            (down + 0.16, DOT_SCALE), (2.0, DOT_SCALE))

    # Eyes drift up and to one side, then back: looking away while working it
    # out. Lidded slightly, because a wide eye reads as listening.
    for eye, rest_x in (("eye_l", EYE_L), ("eye_r", EYE_R)):
        tracks[(eye, "x")] = keys(
            (0.0, rest_x), (0.9, rest_x + 7.0), (1.7, rest_x), (2.0, rest_x))
        tracks[(eye, "y")] = keys(
            (0.0, 96.0), (0.9, 91.0), (1.7, 96.0), (2.0, 96.0))
        tracks[(eye, "sy")] = keys(
            (0.0, 0.84), (0.9, 0.78), (1.7, 0.84), (2.0, 0.84))
        tracks[(eye, "opacity")] = keys((0.0, 92.0), (2.0, 92.0))
    return tracks


MOUTH_CLOSED, MOUTH_OPEN = 10.0, 41.0


def _talking_runtime(shapes, t, params):
    """Mouth height follows TTS output amplitude, so lip-sync is real rather
    than a keyed rhythm that drifts out of step with the voice."""
    amp = max(0.0, min(1.0, float(params.get("amp", 0.0))))
    for s in shapes:
        if s["name"] != "mouth":
            continue
        h = MOUTH_CLOSED + (MOUTH_OPEN - MOUTH_CLOSED) * amp
        s["h"] = h
        s["r"] = min(s["r"], h / 2.0)
        # Opening drops the lower lip further than it lifts the upper one.
        s["y"] = 162.0 + (h - MOUTH_CLOSED) * 0.12


def _talking():
    tracks = {}
    for eye in EYES:
        tracks[(eye, "sy")] = keys(
            (0.0, 1.0), *_blink(1.6), (3.0, 1.0))
        tracks[(eye, "sx")] = keys(
            (0.0, 1.0), *_blink_squash(1.6), (3.0, 1.0))
    return tracks


def _wake():
    """Idle -> listening. Anticipation, overshoot, settle. One second, and it is
    the only thing between the wake word firing and the user believing it."""
    tracks = {}
    for eye in EYES:
        tracks[(eye, "sy")] = keys(
            (0.0, 1.0, "ease_in"), (0.08, 0.80, "ease_out"), (0.30, 1.46, "ease"),
            (0.50, 1.30, "ease"), (0.68, 1.362, "ease"), (0.84, 1.348),
            (1.0, 1.348))
        tracks[(eye, "sx")] = keys(
            (0.0, 1.0, "ease_in"), (0.08, 1.12, "ease_out"), (0.30, 1.05, "ease"),
            (0.52, 1.095, "ease"), (0.84, 1.0857), (1.0, 1.0857))
        tracks[(eye, "y")] = keys(
            (0.0, 106.0), (0.08, 109.0, "ease_out"), (0.30, 104.5, "ease"),
            (0.60, 106.0), (1.0, 106.0))
    return tracks


def _muted():
    """Closed eyes and a microphone with a slash through it.

    A lidded, dimmed face was not enough: it reads as asleep or idle, and
    nothing about it says the microphone specifically is off. Most people have
    never seen this device before and will not learn a private visual language
    from it, so this borrows the one symbol they already know from every call
    they have ever been on.

    The eyes stay, closed and dim, so the device still looks present rather than
    switched off -- it is listening to nothing, not dead."""
    tracks = {}
    for eye in EYES:
        tracks[(eye, "sy")] = keys((0.0, 0.085), (3.0, 0.085))
        tracks[(eye, "y")] = keys((0.0, 80.0), (3.0, 80.0))
        tracks[(eye, "opacity")] = keys((0.0, 46.0), (1.5, 36.0), (3.0, 46.0))
    tracks[("mouth", "opacity")] = keys((0.0, 0.0), (3.0, 0.0))

    # The glyph itself holds still and holds full brightness. It is the one
    # thing on screen that has to be read, not felt.
    for part in ("mic_body", "mic_stem", "mic_base", "mic_slash_gap",
                 "mic_slash"):
        tracks[(part, "opacity")] = keys((0.0, 100.0), (3.0, 100.0))
    return tracks


# A slow sway, not a judder. The judder read as a klaxon for a state that means
# "try again in a minute".
SWAY = ((0.0, 0.0), (0.25, 3.0), (0.65, -3.0), (1.0, 1.2), (1.3, 0.0))

# One slow dim per cycle. Two hard flashes to 25% were the aggressive part.
ERROR_PULSE = ((0.0, 100.0), (1.2, 100.0), (1.65, 58.0), (2.15, 100.0),
               (3.0, 100.0))


def _error():
    """No wifi, or the LLM call failed.

    This one overrides its AE keys (see PREFER_CODE), because the comp animates
    a 0.7s entrance that re-drops the eyes on every loop.

    It has to be legible as "something is wrong" without behaving like an
    alarm, and without blaming the user. Brows down toward the middle is a
    scowl and reads as angry at whoever is standing there; brows up toward the
    middle is the apologetic version, which is the honest one. The device broke,
    not the person.

    So: eyes tilted the sorry way, a slow droop, one dim per cycle, a small
    mouth, amber instead of teal.
    """
    tracks = {}
    # Brows tilted inner-end-high: the apologetic shape. Mirroring it puts them
    # down toward the middle, which is a scowl aimed at whoever is standing
    # there -- the device broke, not the person.
    for eye, brow, rest_x in (("eye_l", "brow_l", EYE_L),
                              ("eye_r", "brow_r", EYE_R)):
        tracks[(eye, "x")] = keys(
            *[(t, rest_x + dx) for t, dx in SWAY], (3.0, rest_x))
        tracks[(eye, "sy")] = keys((0.0, 0.72), (3.0, 0.72))
        tracks[(eye, "sx")] = keys((0.0, 0.92), (3.0, 0.92))
        # A slow droop over the cycle. Sorry, not cross.
        tracks[(eye, "y")] = keys((0.0, 122.0), (1.5, 126.0), (3.0, 122.0))
        tracks[(eye, "opacity")] = keys(*ERROR_PULSE)

        tracks[(brow, "x")] = keys(
            *[(t, rest_x + dx) for t, dx in SWAY], (3.0, rest_x))
        # Sitting just above the eye, not floating clear of it. With a gap
        # the brows read as antennae rather than as an expression.
        tracks[(brow, "y")] = keys((0.0, 72.0), (1.5, 76.0), (3.0, 72.0))
        tracks[(brow, "opacity")] = keys(*ERROR_PULSE)

    tracks[("mouth", "x")] = keys(
        *[(t, CX + dx * 0.5) for t, dx in SWAY], (3.0, CX))
    tracks[("mouth", "sx")] = keys((0.0, 0.42), (3.0, 0.42))
    tracks[("mouth", "sy")] = keys((0.0, 0.34), (3.0, 0.34))
    tracks[("mouth", "y")] = keys(
        (0.0, 174.0), (1.5, 177.0), (3.0, 174.0))
    tracks[("mouth", "opacity")] = keys(*ERROR_PULSE)
    return tracks


# Shapes the AE comps do not have. The comp builder made one layer per shape the
# round-1 design called for; these are the ones the design grew afterwards.
EXTRA_SHAPES = {
    # A microphone with a slash: capsule, stem, base, and the negation across
    # it. Four rounded rects, same as everything else on this screen.
    "face_muted": [
        ("mic_body", CX, 126.0, 28.0, 52.0, 14.0, 0.0),
        ("mic_stem", CX, 163.0, 8.0, 18.0, 4.0, 0.0),
        ("mic_base", CX, 176.0, 40.0, 8.0, 4.0, 0.0),
        # Everything on this screen is one colour, so a slash laid straight
        # over the mic merges with it into a blob. This dark bar is drawn
        # first and knocks a gap out of the glyph, exactly as a two-tone icon
        # would. It is the slash plus 8px on every side.
        ("mic_slash_gap", CX, 138.0, 112.0, 19.0, 9.5, -45.0, DARK),
        ("mic_slash", CX, 138.0, 104.0, 11.0, 5.5, -45.0),
    ],
    # Thinking has only dots in the comp; it needs a face like every other
    # state.
    "face_thinking": [
        ("eye_l", EYE_L, 96.0, 70.0, 80.0, 27.0, 0.0),
        ("eye_r", EYE_R, 96.0, 70.0, 80.0, 27.0, 0.0),
    ],
    # Error needs brows as their own shapes. Tilting the eye itself only works
    # when the eye is a flat bar; at anything like a normal height a 10-degree
    # rotation reads as a skewed square, not as an expression.
    "face_error": [
        ("brow_l", EYE_L, 72.0, 64.0, 14.0, 7.0, 18.0),
        ("brow_r", EYE_R, 72.0, 64.0, 14.0, 7.0, -18.0),
    ],
}


CODE_TRACKS = {
    "face_idle": _idle,
    "face_listening": _listening,
    "face_thinking": _thinking,
    "face_talking": _talking,
    "face_wake": _wake,
    "face_muted": _muted,
    "face_error": _error,
}

RUNTIME = {
    "face_talking": _talking_runtime,
    "face_listening": _listening_runtime,
}

# Normally AE keys win per property. These states are the exception, because the
# code version is the one that is finished. Drop a name from this set and the
# comp takes over again -- the keys are still in the bake either way.
PREFER_CODE = {"face_error"}

# Amber. The brand teal #1B7F9B is HSL(193, 70%, 36%); this is the same
# saturation and lightness with the hue rotated to 38, so it reads as a sibling
# of the brand colour and carries the same weight on a dark screen. Terracotta
# #B8442A was tried first and clashed: it is both hotter and lighter than
# anything else the device ever shows.
ERROR_AMBER = (155, 108, 27)

COLOR_OVERRIDE = {
    "face_error": ERROR_AMBER,
}

ORDER = ["face_idle", "face_listening", "face_thinking", "face_talking",
         "face_wake", "face_muted", "face_boot", "face_error"]

# What the idle face does now and then, built in `acts.py`. Each plays once
# and hands back to face_idle. The device picks one every several minutes.
IDLE_ACTS = ("face_idle_look", "face_idle_curious", "face_idle_sleepy",
             "face_idle_yawn", "face_idle_happy", "face_idle_wink",
             "face_idle_doze", "face_idle_roll", "face_idle_perk")

# Where a one-shot goes when it finishes. The controller follows these on its
# own, so the interaction loop never has to time the handover.
FOLLOW = {
    "face_boot": "face_idle",
    "face_wake": "face_listening",
}
FOLLOW.update({name: "face_idle" for name in IDLE_ACTS})

# Most transitions take the default. These do not: wake already ends on the
# listening pose, so a long blend there just smears an arrival that has
# already happened.
BLEND_OVERRIDE = {
    ("face_wake", "face_listening"): 0.08,
    ("face_boot", "face_idle"): 0.35,
}


def blend_time(prev, nxt, default=0.22):
    return BLEND_OVERRIDE.get((prev, nxt), default)


def _fallback_states():
    out = {}
    for name, (dur, loop, shapes) in REST.items():
        out[name] = State(
            name, dur,
            [Shape(s[0], s[1], s[2], s[3], s[4], s[5], TEAL) for s in shapes],
            {}, loop=loop, source="fallback")
    return out


def build(data_path=None):
    """Rest poses and AE keys from the baked data, code tracks filling the gaps.

    `data/faces.json` is written by `tools/face/bake.py` from the After Effects
    export. If it is missing or unreadable the fallback poses in `REST` are used
    instead, so the device still shows a face rather than a black screen.
    """
    try:
        from .facedata import load
        states, _ = load(data_path)
        missing = [n for n in REST if n not in states]
        if missing:
            fallback = _fallback_states()
            for name in missing:
                states[name] = fallback[name]
    except (OSError, ValueError, KeyError):
        states = _fallback_states()

    # Imported here: acts.py takes its geometry from this module.
    from . import acts
    for name in IDLE_ACTS:
        duration, shapes, tracks = acts.IDLE_BUILDERS[name]()
        states[name] = State(
            name, duration, [Shape(*spec[:6]) for spec in shapes], tracks,
            loop=False, source="code")

    for name, state in states.items():
        builder = CODE_TRACKS.get(name)
        if builder is not None:
            code_wins = name in PREFER_CODE
            had_ae = state.source == "ae"
            state.merge_tracks(builder(), prefer_other=code_wins)
            if had_ae:
                state.source = "code*" if code_wins else "ae+code"
            else:
                state.source = "code"

        for spec in EXTRA_SHAPES.get(name, ()):
            if any(sh.name == spec[0] for sh in state.shapes):
                continue
            shape = Shape(spec[0], spec[1], spec[2], spec[3], spec[4], spec[5],
                          spec[7] if len(spec) > 7 else None)
            shape.rest["rot"] = spec[6]
            state.shapes.append(shape)

        state.runtime = RUNTIME.get(name)
        state.blit = blit_region(name)
        override = COLOR_OVERRIDE.get(name)
        for shape in state.shapes:
            if shape.name.endswith("_gap"):
                shape.color = DARK
            elif override is not None:
                shape.color = override
            elif shape.color is None:
                shape.color = TEAL

    return states

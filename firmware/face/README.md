# Face renderer

Draws the animated face for the 2.4" ILI9341 panel. Four rounded rects on a dark
320 × 240 canvas, sampled from keyframe tracks and drawn with Pillow — no
pre-rendered frames, no video, nothing to decode.

## Transitions

`FaceController` is what the interaction loop talks to. Call `set(name, now)` on
every state change and `sample(now, params)` once per frame; it eases between
poses over 0.22s instead of cutting.

Cutting is what a state machine does, not what a face does — switching straight
from idle to listening pops the eyes to a new size in one frame, which reads as
a dropped frame rather than as the device waking up.

Shapes blend by name, so eyes tween into eyes. A shape only one side has — the
mic glyph, the error brows — fades instead. Colour interpolates too, which is
what carries teal into amber when the wifi drops. The outgoing state keeps
running during the blend; freezing it makes the handover look like a dissolve
between two photographs.

Two tables in `states.py` tune it:

- `FOLLOW` — where a one-shot goes when it finishes. Boot lands in idle, the
  wake acknowledgement lands in listening, and the caller never times it.
- `BLEND_OVERRIDE` — per-pair durations. `wake → listening` gets 0.08s because
  wake already ends on the listening pose, so a long blend there smears an
  arrival that has already happened.

Press `B` in the preview to turn blending off and see the cut version.

## Run the preview

The preview is a development tool and lives in `tools/`, because it needs
tkinter and the robot has no display server. From `code/`:

    pip install -r tools/requirements.txt
    python -m tools.face.preview

A window at the panel's real 320 × 240, scaled 3× with nearest-neighbour so you
see the actual pixel grid. Keys `1`-`8` pick a state, `D` runs the full
interaction loop, space pauses, Esc quits.

The readout on the right gives draw time, the dirty rect, and what pushing that
rect costs over SPI at 32 MHz. Those two numbers added together are the frame
budget on the Pi; the PC draw time is optimistic, the SPI number is not.

It warns when a frame's bounding box leaves the blit region (x 48–272,
y 32–192), because on the device that part is simply not sent.

## Where the animation comes from

| | Source |
|---|---|
| `face_boot` | After Effects |
| `face_error` | `states.py` — overrides the comp, see below |
| everything else | `states.py` |

`tools/face/ae_import.py` reads `docs/assets/brand/face-states/ae/faces.json`
and rebuilds AE's curves — the (speed, influence) ease on each keyframe
converts to cubic bezier handles, so a curve shaped in the graph editor arrives
intact. That conversion runs on a build machine, not on the robot:
`python -m tools.face.bake` writes the resolved curves to `data/faces.json`,
which is the only face data the device ships with. Re-export from AE, re-bake,
or the device keeps animating the old curves.

**AE wins per property.** Keying `face_idle`'s scale in AE stops the blink in
`states.py` from applying, while the breathing on Y keeps working until that is
keyed too. So the AE comps can take over one state, or one property, at a time.
Nothing has to be ported all at once.

The exception is `PREFER_CODE` in `states.py`, currently `{"face_error"}`, where
the code version wins instead. Drop a name from that set and the comp takes over
again; the keys stay in the bake either way.

Rest poses always come from the bake. `states.py` carries a copy as `REST`, so
a device whose `data/faces.json` is missing or corrupt shows a face rather than
a black screen.

## Files

| | |
|---|---|
| `easing.py` | Cubic bezier, and the AE (speed, influence) → handles conversion |
| `controller.py` | Which state the face is in, and the ease between states |
| `timeline.py` | `Key`, `Track`, `Shape`, `State`, and the sampler |
| `facedata.py` | `data/faces.json` → states |
| `states.py` | The code-animated states, and the merge with AE |
| `renderer.py` | Shapes → image, plus `to_rgb565` and `spi_cost_ms` |
| `tools/face/ae_import.py` | The AE export → states, on a build machine |
| `tools/face/bake.py` | Writes `data/faces.json` |
| `tools/face/preview.py` | The desktop window |

## The two states that react

`face_talking` and `face_listening` both take a live level through `sample`:

    state.sample(t, {"amp": 0.0 .. 1.0})

On the device that is TTS output amplitude for talking, and mic input level for
listening. The preview synthesises an envelope so you can see them move.

Talking drives mouth height. Listening drives eye height, which is what makes it
read as listening at all — dilated eyes on their own are just idle, larger. A
face visibly responding to the room says "I can hear you" in a way no amount of
self-contained idle motion does.

Listening rests at 0.88 scale rather than 1.0 so the reaction has somewhere to
go, and clamps itself to the blit region, since a loud room would otherwise push
the eyes off the part of the screen that gets sent.

## Muted

Lidding and dimming the face was not enough. It read as asleep, and nothing
about it said the microphone specifically was off. Nobody meeting this device
will learn a private visual language from it, so `face_muted` shows the symbol
they already know from every call they have been on: a microphone with a slash
through it, at full brightness, holding still.

The eyes stay, closed and dim, so the device still looks present. It is
listening to nothing, not switched off.

## Thinking

The dots-only version was the odd one out — every other state is a face, so for
the two to four seconds an LLM round-trip takes, the face vanished. Now the eyes
stay and drift up and away, lidded slightly, while the dots shrink to a caption
underneath. Lidded matters: a wide eye reads as listening.

## Shapes the comps do not have

`EXTRA_SHAPES` in `states.py` adds rounded rects that the AE comps were never
built with. The comp builder made one layer per shape the round-1 design called
for; these are what the design grew afterwards. They animate through the same
tracks as everything else, so a comp can be rebuilt to include them later
without changing any of this code.

- **`face_muted`** gets a microphone glyph: capsule, stem, base, slash.
- **`face_thinking`** gets eyes.
- **`face_error`** gets brows.

The mic needs one trick. Everything on this screen is a single colour, so a
slash laid over the mic merges with it into a blob. `mic_slash_gap` is a bar in
the background colour, 8px larger on every side, drawn just before the slash —
it knocks a gap out of the glyph the way a two-tone icon would. Any shape whose
name ends in `_gap` is forced to the background colour and skips
`COLOR_OVERRIDE`.

## Error

`face_error` is the only state not drawn in the brand teal. It uses amber
`#9B6C1B`, which is `#1B7F9B` with the hue rotated to 38° and the saturation and
lightness left alone — a sibling of the brand colour, carrying the same weight
on a dark screen. Terracotta `#B8442A` was tried first and clashed: hotter and
lighter than anything else the device shows.

The expression is apologetic, not cross. Brows tilt inner-end-high and sit on
the eyes; the mirror of that tilt is a scowl aimed at whoever is standing there,
which blames the user for a broken wifi connection. They are separate shapes
because rotating the eye itself only reads as an expression when the eye is a
flat bar — at any normal height a 10° rotation just looks like a skewed square.

The motion is restrained: a slow sway, one dim to 58% per cycle, a slow droop.
An earlier version shook hard and double-flashed to 25%, which read as a klaxon
for a state that means "try again in a minute".

## The blit region

Most states blit `DIRTY_RECT` (x 48–272, y 32–192), 72 KB against 154 KB for the
full screen. `BLIT_REGION` overrides it per state: `face_boot` gets the whole
panel, because its eyes rise from below the screen and would otherwise be
clipped on the way up. That costs 38 ms a frame instead of 11, which is
affordable exactly once — nothing else runs during boot.

The preview draws the current state's region and warns when a frame leaves it.

## Known rough edges

`ssaa=2` in `FaceRenderer` supersamples for antialiasing, since Pillow's
`rounded_rectangle` is aliased and jagged eyelids show at this size. It costs 4×
the pixel work. Measure on the Pi before assuming it fits; `ssaa=1` is the
fallback.

The dirty rect it reports is padded by `RESAMPLE_PAD` because of that
supersampling. LANCZOS is not a box filter — it carries a shape's edge a pixel
or two further than the shape itself, and a bbox that covered only the shape
left a faint outline on the panel every time something moved. The panel is
only sent the bbox, so nothing repainted it. Two pixels on every side of a
rect that is already 100 wide is a rounding error in SPI cost.

Spatial tangents are not exported, so an AE motion path that curves arrives as a
straight line between its keys. None of the current comps use one.

While blending into `face_muted`, `mic_slash_gap` is a dark bar at partial
opacity, so for about a fifth of a second it darkens whatever is behind it
rather than cutting a clean hole. It reads as the slash sweeping in. Alpha over
a single-colour screen cannot do better without a second draw pass.

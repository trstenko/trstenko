"""Generate the two alert chimes the scheduler plays.

    python -m tools.make_chimes

Writes `firmware/assets/sounds/chime_reminder.wav` and `chime_timer.wav`:
16kHz, 16-bit, mono (the speaker's own format, so nothing resamples them on the
device), each under a second. They are synthesised here, from sine waves, so
there is no audio to license and no download to trust. Run it again and the
files come out byte for byte the same.

The two are meant to be told apart without looking. A reminder is two soft
notes, rising; a timer is three short, higher beeps, which reads as "something
needs doing now". Neither is shrill: the first thing a sleeping owner hears at
night should not be a fright.
"""

import math
import os
import struct
import sys
import wave

RATE = 16000
PEAK = 0.55
SOUNDS = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "firmware", "assets", "sounds")


def tone(freq, seconds, harmonic=0.25, attack=0.008, decay=6.0):
    """A bell-ish note: a fundamental and a quieter octave, a quick attack so
    it does not click, and an exponential fade."""
    count = int(RATE * seconds)
    out = []
    for i in range(count):
        t = i / RATE
        wave_ = (math.sin(2 * math.pi * freq * t)
                 + harmonic * math.sin(2 * math.pi * 2 * freq * t))
        envelope = min(1.0, t / attack) * math.exp(-decay * t)
        out.append(wave_ * envelope)
    return out


def silence(seconds):
    return [0.0] * int(RATE * seconds)


def reminder():
    # E5 then A5: a rising fourth, unhurried.
    return tone(659.25, 0.34) + silence(0.02) + tone(880.0, 0.42, decay=5.0)


def timer():
    beep = tone(1174.66, 0.12, harmonic=0.1, decay=8.0)
    gap = silence(0.07)
    return beep + gap + beep + gap + beep


def write(path, samples):
    top = max(abs(s) for s in samples) or 1.0
    scale = PEAK * 32767 / top
    frames = b"".join(struct.pack("<h", int(round(s * scale)))
                      for s in samples)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(RATE)
        fh.writeframes(frames)
    return len(samples) / RATE


def main(argv=None):
    for name, make in (("chime_reminder.wav", reminder),
                       ("chime_timer.wav", timer)):
        path = os.path.join(SOUNDS, name)
        seconds = write(path, make())
        print("%s: %.2fs" % (os.path.relpath(path), seconds))
    return 0


if __name__ == "__main__":
    sys.exit(main())

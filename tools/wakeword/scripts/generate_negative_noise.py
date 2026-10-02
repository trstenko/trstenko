"""Procedural ambient-noise beds for negative training data.

Nothing here is downloaded, so nothing here has a license question -- see
LICENSES.md for why that matters (the reference recipe's real-recording
alternatives are non-commercial or unknown-license). This is a synthetic
stand-in, not a replacement for the real-room recordings the README asks
the team to add later.

Five families, cycled: white/pink/brown noise (spectral tilt covers
"hiss"-like sources a real room also produces), a low-frequency hum with
harmonics (fans, fridges, appliance motors), and amplitude-modulated noise
(footsteps, distant traffic, intermittent household sound).
"""

import os
import wave

import numpy as np

RATE = 16000
CLIP_SECONDS = 8
N_CLIPS_PER_FAMILY = int(os.environ.get("N_NOISE_CLIPS_PER_FAMILY", "150"))
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "negative_noise")


def _write_wav(path, samples):
    pcm = np.clip(samples * 32767, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(RATE)
        fh.writeframes(pcm.tobytes())


def _normalize(x, peak=0.7):
    m = np.max(np.abs(x))
    if m < 1e-9:
        return x
    return x * (peak / m)


def white_noise(n, rng):
    return rng.normal(0, 1, n)


def pink_noise(n, rng):
    # Voss-McCartney approximation: sum of octave-spaced random walks.
    n_rows = 12
    array = rng.normal(0, 1, (n_rows, n))
    for i in range(n_rows):
        stride = 2 ** i
        array[i] = np.repeat(array[i, ::stride], stride)[:n]
    return array.sum(axis=0)


def brown_noise(n, rng):
    steps = rng.normal(0, 1, n)
    walk = np.cumsum(steps)
    return walk - np.mean(walk)


def hum(n, rng):
    t = np.arange(n) / RATE
    base_freq = rng.uniform(45, 120)  # fans/appliance motors, not mains-locked
    signal = np.zeros(n)
    for harmonic in (1, 2, 3):
        amp = 1.0 / harmonic
        signal += amp * np.sin(2 * np.pi * base_freq * harmonic * t + rng.uniform(0, 2 * np.pi))
    signal += 0.05 * rng.normal(0, 1, n)
    return signal


def modulated_noise(n, rng):
    carrier = white_noise(n, rng)
    mod_rate = rng.uniform(0.1, 2.0)
    t = np.arange(n) / RATE
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * mod_rate * t + rng.uniform(0, 2 * np.pi))
    envelope = envelope ** rng.uniform(1.0, 4.0)
    return carrier * envelope


FAMILIES = {
    "white": white_noise,
    "pink": pink_noise,
    "brown": brown_noise,
    "hum": hum,
    "modulated": modulated_noise,
}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rng = np.random.default_rng(20260926)
    n_samples = CLIP_SECONDS * RATE

    total = 0
    for name, fn in FAMILIES.items():
        for i in range(N_CLIPS_PER_FAMILY):
            clip = _normalize(fn(n_samples, rng), peak=rng.uniform(0.2, 0.7))
            _write_wav(os.path.join(OUT_DIR, "%s_%03d.wav" % (name, i)), clip)
            total += 1

    print("wrote %d synthetic negative-noise clips to %s" % (total, OUT_DIR))


if __name__ == "__main__":
    main()

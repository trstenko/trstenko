"""Run the exported .tflite model against a single WAV clip and print
per-step trigger probability.

    docker compose run --rm train python scripts/try_clip.py data/try_clips/whatever.wav

This is the closest thing to "try the model" available on this machine.
`pymicro_wakeword` -- the engine `firmware/wakeword/micro_wakeword.py`
actually loads on the robot -- ships a bundled aarch64 Linux binary and
cannot run on this Windows PC or in either x86_64 Docker image here; there
is no live-mic test until real Pi hardware exists. This instead reuses
`microwakeword.inference.Model`, the plain-TensorFlow-Lite wrapper the
upstream project's own `model_train_eval` evaluation already runs on this
machine to produce the frr/faph numbers -- same model file, same math, just
against one clip you choose instead of the whole test set.
"""

import os
import sys
import wave

import numpy as np

from microwakeword.inference import Model

MODEL_PATH = "/work/export/trstenko.tflite"
STEP_MS = 10


def load_wav(path):
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != 16000 or w.getsampwidth() != 2:
            raise ValueError(
                "expected mono 16kHz 16-bit WAV, got %d ch / %d Hz / %d-bit"
                % (w.getnchannels(), w.getframerate(), w.getsampwidth() * 8))
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16)


def main():
    if len(sys.argv) != 2:
        print("usage: try_clip.py <path-to-16khz-mono-wav>")
        sys.exit(1)

    cutoff = float(os.environ.get("PROBABILITY_CUTOFF", "0.2"))
    samples = load_wav(sys.argv[1])
    model = Model(MODEL_PATH)
    predictions = model.predict_clip(samples, step_ms=STEP_MS)

    triggered_at = []
    for i, score in enumerate(predictions):
        t = i * STEP_MS / 1000.0
        marker = "  <-- TRIGGER" if score >= cutoff else ""
        print("t=%5.2fs  score=%.3f%s" % (t, score, marker))
        if score >= cutoff:
            triggered_at.append(t)

    print()
    if triggered_at:
        print("Triggered at: %s (cutoff=%.2f)"
              % (", ".join("%.2fs" % t for t in triggered_at), cutoff))
    else:
        print("Never crossed cutoff %.2f -- would not have fired." % cutoff)


if __name__ == "__main__":
    main()

"""Copies the trained streaming tflite + writes the JSON manifest
`pymicro_wakeword.MicroWakeWord.from_config` expects, into
`firmware/wakeword/models/` (bind-mounted as /work/export). This is the one
artifact from this whole pipeline that the robot actually loads.
"""

import json
import os
import shutil

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
EXPORT_DIR = os.path.join(os.path.dirname(__file__), "..", "export")

SOURCE_TFLITE = os.path.join(
    DATA, "trained_models", "trstenko", "tflite_stream_state_internal_quant",
    "stream_state_internal_quant.tflite")

# Starting point, not tuned -- lower this if the model under-triggers in
# testing, raise it if it false-accepts. See train.sh's printed evaluation
# and README.md "Honest gaps."
PROBABILITY_CUTOFF = float(os.environ.get("PROBABILITY_CUTOFF", "0.85"))
SLIDING_WINDOW_SIZE = int(os.environ.get("SLIDING_WINDOW_SIZE", "5"))


def main():
    if not os.path.exists(SOURCE_TFLITE):
        raise SystemExit(
            "no trained model at %s -- run scripts/train.sh first"
            % SOURCE_TFLITE)

    os.makedirs(EXPORT_DIR, exist_ok=True)
    shutil.copyfile(SOURCE_TFLITE, os.path.join(EXPORT_DIR, "trstenko.tflite"))

    manifest = {
        "type": "micro",
        "wake_word": "Trstenko",
        "author": "Trstenko (Moje podjetje student company)",
        "model": "trstenko.tflite",
        "trained_languages": ["en", "sl"],
        "version": 1,
        "micro": {
            "probability_cutoff": PROBABILITY_CUTOFF,
            "feature_step_size": 10,
            "sliding_window_size": SLIDING_WINDOW_SIZE,
        },
    }
    with open(os.path.join(EXPORT_DIR, "trstenko.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    print("exported firmware/wakeword/models/trstenko.tflite + .json")


if __name__ == "__main__":
    main()

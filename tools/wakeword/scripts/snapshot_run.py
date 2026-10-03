"""Copies the config and evaluation results of the run that just finished
into `data/runs/<timestamp>/`, so past runs stay comparable instead of being
overwritten by the next `train.sh`. Called automatically at the end of
train.sh; the training UI reads this directory for run history.
"""

import datetime
import json
import os
import shutil

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
TRAIN_DIR = os.path.join(DATA, "trained_models", "trstenko")
RUNS_DIR = os.path.join(DATA, "runs")

ROC_PATH = os.path.join(
    TRAIN_DIR, "tflite_stream_state_internal_quant", "tflite_streaming_roc.txt")
CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "training_parameters.yaml")


def parse_roc(path):
    """Each line: 'Cutoff 0.50: frr=0.3000; faph=nan'."""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("Cutoff"):
                continue
            try:
                cutoff_part, rest = line.split(":", 1)
                cutoff = float(cutoff_part.replace("Cutoff", "").strip())
                frr_part, faph_part = rest.split(";")
                frr = float(frr_part.split("=")[1].strip())
                faph_str = faph_part.split("=")[1].strip()
                faph = None if faph_str == "nan" else float(faph_str)
                rows.append({"cutoff": cutoff, "frr": frr, "faph": faph})
            except (ValueError, IndexError):
                continue
    return rows


def main():
    if not os.path.exists(ROC_PATH):
        raise SystemExit(
            "no evaluation output at %s -- did train.sh finish?" % ROC_PATH)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(RUNS_DIR, stamp)
    os.makedirs(run_dir, exist_ok=True)

    shutil.copyfile(ROC_PATH, os.path.join(run_dir, "tflite_streaming_roc.txt"))
    if os.path.exists(CONFIG_PATH):
        shutil.copyfile(CONFIG_PATH, os.path.join(run_dir, "training_parameters.yaml"))

    rows = parse_roc(ROC_PATH)
    measured = [r for r in rows if r["faph"] is not None]
    summary = {
        "timestamp": stamp,
        "rows": rows,
        "ambient_measured": bool(measured),
    }
    if measured:
        viable = [r for r in measured if r["faph"] <= 2.0]
        best = min(viable, key=lambda r: r["frr"]) if viable else \
            min(measured, key=lambda r: r["faph"])
        summary["recommended_cutoff"] = best["cutoff"]
        summary["recommended_frr"] = best["frr"]
        summary["recommended_faph"] = best["faph"]

    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    print("snapshotted run to data/runs/%s" % stamp)
    if measured:
        print("recommended cutoff: %.2f (frr=%.4f, faph=%.3f)" % (
            summary["recommended_cutoff"], summary["recommended_frr"],
            summary["recommended_faph"]))
    else:
        print("WARNING: every faph is nan -- ambient eval set is still not "
              "populated for this run. See TRAINING_GUIDE.md.")


if __name__ == "__main__":
    main()

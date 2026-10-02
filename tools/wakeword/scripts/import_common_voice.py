"""DO NOT RUN THIS AGAINST REAL DATA YET -- see LICENSES.md's "Explicitly
rejected" table. As of October 2025, Mozilla Data Collective is the *only*
way to download Common Voice, and its Terms of Use say plainly: "Commercial
use of datasets is prohibited without prior written consent." The audio
itself is CC0, but that doesn't matter when the access terms you have to
agree to on the way to getting it separately block commercial use, and this
pipeline trains a model for a product that gets sold. This script exists
and works correctly (tested against a synthetic corpus), but is blocked
until someone actually gets Mozilla's written consent -- their own terms
say that's requestable.

Imports real human speech (never saying "Trstenko") from Mozilla Common
Voice as a negative-speech category, alongside the synthetic TTS negatives
in negative_speech_en/sl. Real speech is a stronger negative than more
synthetic voices for the same purpose -- *if* the access-terms problem
above gets resolved.

Common Voice isn't something this script downloads for you: getting a
corpus out of Mozilla Data Collective requires agreeing to their terms
yourself (there's no way around a human clicking "I agree" there, and this
repo shouldn't try to script around it, especially not for a source that's
currently blocked anyway). What this script does is take whatever you
already extracted and turn it into training data:

    1. Get written commercial-use consent from Mozilla first (see above).
    2. Go to https://mozilladatacollective.com (the old
       commonvoice.mozilla.org/datasets page is retired), pick a language
       (this project cares about "en" and "sl"), and download a corpus.
    3. Extract it somewhere. You'll have a folder with validated.tsv,
       clips/, and a few other .tsv files.
    4. docker compose run --rm generate python scripts/import_common_voice.py \\
           --corpus-dir /path/to/extracted/cv-corpus-XX.0-YYYY-MM-DD/en \\
           --lang en

Only pulls from validated.tsv (Common Voice's own community-QA'd subset,
not the raw unvalidated firehose), and skips any sentence that contains the
wake word itself, so as not to quietly slip an actual "Trstenko" into the
negative set with the wrong label.
"""

import argparse
import csv
import os
import subprocess

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
RATE = 16000
WAKE_WORD = "trstenko"


def convert_to_wav(src_path, dst_path):
    """mp3 (Common Voice's distribution format) -> mono/16kHz/16-bit WAV."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", src_path,
        "-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16",
        dst_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0 and os.path.getsize(dst_path) > 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", required=True,
                         help="Extracted Common Voice release dir (contains validated.tsv, clips/)")
    parser.add_argument("--lang", required=True,
                         help="Short tag for the output dir, e.g. en or sl")
    parser.add_argument("--limit", type=int, default=300,
                         help="Max clips to import (default 300 -- this is meant to add "
                              "diversity alongside the synthetic negatives, not dominate them)")
    parser.add_argument(
        "--i-have-mozilla-written-consent", action="store_true",
        help="Required. See this script's module docstring and LICENSES.md's "
             "\"Explicitly rejected\" table -- Mozilla Data Collective's Terms "
             "of Use currently block commercial use of this data without it.",
    )
    args = parser.parse_args()
    if not args.i_have_mozilla_written_consent:
        raise SystemExit(
            "Blocked: Mozilla Data Collective's Terms of Use say commercial "
            "use of Common Voice datasets requires their prior written "
            "consent (see LICENSES.md). Get that first, then re-run with "
            "--i-have-mozilla-written-consent."
        )

    tsv_path = os.path.join(args.corpus_dir, "validated.tsv")
    clips_dir = os.path.join(args.corpus_dir, "clips")
    if not os.path.isfile(tsv_path) or not os.path.isdir(clips_dir):
        raise SystemExit(
            "%s doesn't look like an extracted Common Voice corpus "
            "(expected validated.tsv and clips/ inside it)" % args.corpus_dir
        )

    out_dir = os.path.join(DATA, "negative_speech_cv_%s" % args.lang)
    os.makedirs(out_dir, exist_ok=True)

    imported = 0
    skipped_wake_word = 0
    failed = 0
    with open(tsv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            if imported >= args.limit:
                break
            sentence = (row.get("sentence") or "").lower()
            if WAKE_WORD in sentence:
                skipped_wake_word += 1
                continue

            clip_path = os.path.join(clips_dir, row["path"])
            if not os.path.isfile(clip_path):
                continue

            out_path = os.path.join(out_dir, "cv_%s_%04d.wav" % (args.lang, imported))
            if convert_to_wav(clip_path, out_path):
                imported += 1
            else:
                failed += 1

    print(
        "imported %d clips to %s (skipped %d containing the wake word, "
        "%d failed to convert)" % (imported, out_dir, skipped_wake_word, failed)
    )
    print(
        "next: add a `features_dir: data/features/negative_speech_cv_%s` entry "
        "to config/training_parameters.yaml (commented-out template already "
        "there), then re-run build_features.py" % args.lang
    )


if __name__ == "__main__":
    main()

"""Wav files -> augmented spectrogram features, per category.

Mirrors microWakeWord's own `basic_training_notebook.ipynb` (Clips ->
Augmentation -> SpectrogramGeneration -> RaggedMmap), except every path fed
to `background_paths`/`impulse_paths` here is one of our own commercially-
clean sources (see LICENSES.md), and RIR augmentation is disabled -- no
impulse-response dataset in this pipeline has a confirmed-permissive
license. Run inside the `train` container.
"""

import os

from microwakeword.audio.augmentation import Augmentation
from microwakeword.audio.clips import Clips
from microwakeword.audio.spectrograms import SpectrogramGeneration
from mmap_ninja.ragged import RaggedMmap

DATA = os.path.join(os.path.dirname(__file__), "..", "data")

# name -> (input wav dir, truth)
CATEGORIES = {
    "positive_en": (os.path.join(DATA, "positive_en"), True),
    "positive_sl": (os.path.join(DATA, "positive_sl"), True),
    "negative_speech_en": (os.path.join(DATA, "negative_speech_en"), False),
    "negative_speech_sl": (os.path.join(DATA, "negative_speech_sl"), False),
    "negative_noise": (os.path.join(DATA, "negative_noise"), False),
}

BACKGROUND_PATHS = [os.path.join(DATA, "negative_noise")]
# Real recordings, once the team has them (see README "Honest gaps" and
# TRAINING_GUIDE.md). Auto-detected: drop wavs in these folders and the next
# build_features.py run picks them up, no code change needed. The yaml still
# needs the matching `features` entry uncommented -- see
# config/training_parameters.yaml -- this only builds the feature dir, it
# doesn't make training_parameters.yaml read it.
REAL_POSITIVE = os.path.join(DATA, "positive_real")
if os.path.isdir(REAL_POSITIVE) and os.listdir(REAL_POSITIVE):
    CATEGORIES["positive_real"] = (REAL_POSITIVE, True)

REAL_BACKGROUND = os.path.join(DATA, "negative_real")
if os.path.isdir(REAL_BACKGROUND) and os.listdir(REAL_BACKGROUND):
    BACKGROUND_PATHS.append(REAL_BACKGROUND)
    CATEGORIES["negative_real"] = (REAL_BACKGROUND, False)

# External real-audio sources (see scripts/import_common_voice.py,
# scripts/import_freesound.py, LICENSES.md) -- same auto-detect pattern as
# positive_real/negative_real above: nothing here until an import script has
# actually been run, and the yaml still needs the matching entry uncommented.
for _lang in ("en", "sl"):
    _cv_dir = os.path.join(DATA, "negative_speech_cv_%s" % _lang)
    if os.path.isdir(_cv_dir) and os.listdir(_cv_dir):
        CATEGORIES["negative_speech_cv_%s" % _lang] = (_cv_dir, False)

REAL_AMBIENT_FREESOUND = os.path.join(DATA, "negative_ambient_freesound")
if os.path.isdir(REAL_AMBIENT_FREESOUND) and any(
    f.endswith(".wav") for f in os.listdir(REAL_AMBIENT_FREESOUND)
):
    BACKGROUND_PATHS.append(REAL_AMBIENT_FREESOUND)
    CATEGORIES["negative_ambient_freesound"] = (REAL_AMBIENT_FREESOUND, False)

# Categories long/continuous enough to measure false-accepts-per-hour against
# (model_train_eval streams straight through each stored track and counts
# re-triggers -- short single-word utterances don't mean anything here).
# Without this, `test_tflite_streaming_quantized`'s faph comes back `nan`:
# it divides false accepts by the ambient set's total duration, which is
# zero when testing_ambient/validation_ambient are never populated.
AMBIENT_CATEGORIES = {"negative_noise", "negative_real", "negative_ambient_freesound"}


def build_ambient(name, clips, out_dir):
    """Whole, unaugmented spectrograms for the false-accept-per-hour test.
    `model_train_eval` streams straight through each track counting
    re-triggers and divides by total duration -- so this wants raw ambient
    audio, not the short randomly-cropped windows the other splits use."""
    plain = SpectrogramGeneration(clips=clips, augmenter=None, step_ms=10)

    for split, split_name in (("testing_ambient", "test"),
                              ("validation_ambient", "validation")):
        split_dir = os.path.join(out_dir, split)
        os.makedirs(split_dir, exist_ok=True)
        RaggedMmap.from_generator(
            out_dir=os.path.join(split_dir, "wakeword_mmap"),
            sample_generator=plain.spectrogram_generator(
                split=split_name, repeat=1),
            batch_size=100,
            verbose=True,
        )
        print("%s/%s done" % (name, split))


def build_one(name, input_dir, out_root):
    # Ambient categories get a much bigger test+validation slice: faph is a
    # count of false accepts divided by ambient *hours*, so a thin slice (the
    # default 10%) makes the rate swing wildly on a single false accept --
    # confirmed on run 2 (2026-09-26), where testing_ambient worked out to
    # ~10 minutes and the whole ROC curve collapsed to 2 points (AUC 0.0,
    # "0 or ~6/hour" with nothing in between). This doesn't cost augmentation
    # variety: `background_paths` reads the whole directory regardless of
    # split, only this category's own direct pos/neg training examples
    # shrink, and negative_speech_* already covers that role.
    base_split_count = 0.3 if name in AMBIENT_CATEGORIES else 0.1

    # Clips() first carves out a `2 * split_count` fraction as test+validation,
    # then splits *that* 50/50 -- with too few files the inner split lands on
    # a single sample and datasets.train_test_split refuses to split 1 sample
    # into two non-empty sets. Real recordings start out this small (a
    # handful of takes), so fall back to an absolute held-out count (an int
    # instead of a float makes HF's train_test_split treat it as a literal
    # count, not a fraction) once the fractional split would go degenerate.
    # Below 3 clips there's no split that leaves anything for training.
    n_files = len([f for f in os.listdir(input_dir) if f.endswith(".wav")])
    if n_files < 3:
        print("skipping %s: only %d clip(s) -- need at least 3 before a "
              "train/validation/test split is possible" % (name, n_files))
        return
    if 2 * base_split_count * n_files >= 2:
        split_count = base_split_count
    else:
        split_count = 1

    clips = Clips(
        input_directory=input_dir,
        file_pattern="*.wav",
        max_clip_duration_s=None,
        remove_silence=False,
        random_split_seed=10,
        split_count=split_count,
    )
    augmenter = Augmentation(
        augmentation_duration_s=3.2,
        augmentation_probabilities={
            "SevenBandParametricEQ": 0.1,
            "TanhDistortion": 0.1,
            "PitchShift": 0.1,
            "BandStopFilter": 0.1,
            "AddColorNoise": 0.1,
            "AddBackgroundNoise": 0.75,
            "Gain": 1.0,
            "RIR": 0.0,  # no license-clean impulse-response set; see README
        },
        impulse_paths=[],
        background_paths=BACKGROUND_PATHS,
        background_min_snr_db=-5,
        background_max_snr_db=10,
        min_jitter_s=0.195,
        max_jitter_s=0.205,
    )

    out_dir = os.path.join(out_root, name)
    os.makedirs(out_dir, exist_ok=True)

    for split, split_name, repetition, slide_frames in (
        ("training", "train", 2, 10),
        ("validation", "validation", 1, 10),
        ("testing", "test", 1, 1),
    ):
        split_dir = os.path.join(out_dir, split)
        os.makedirs(split_dir, exist_ok=True)

        spectrograms = SpectrogramGeneration(
            clips=clips,
            augmenter=augmenter,
            slide_frames=slide_frames,
            step_ms=10,
        )
        RaggedMmap.from_generator(
            out_dir=os.path.join(split_dir, "wakeword_mmap"),
            sample_generator=spectrograms.spectrogram_generator(
                split=split_name, repeat=repetition),
            batch_size=100,
            verbose=True,
        )
        print("%s/%s done" % (name, split))

    if name in AMBIENT_CATEGORIES:
        build_ambient(name, clips, out_dir)


def main():
    out_root = os.path.join(DATA, "features")
    for name, (input_dir, _truth) in CATEGORIES.items():
        if not os.path.isdir(input_dir) or not os.listdir(input_dir):
            print("skipping %s: %s is empty (run the generate scripts first)"
                  % (name, input_dir))
            continue
        print("== building features for %s ==" % name)
        build_one(name, input_dir, out_root)


if __name__ == "__main__":
    main()

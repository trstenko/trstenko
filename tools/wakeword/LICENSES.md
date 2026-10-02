# Licensing audit -- wake word training pipeline

Researched 2026-09-26. Every component that touches the training data or the
shipped artifact, with its actual license, checked against source (not
assumed from a project's README tagline).

## What ships on the robot

| Component | License | Obligation |
|---|---|---|
| `pymicro-wakeword` (runtime, `firmware/wakeword/micro_wakeword.py`) | Apache-2.0 | Keep the `LICENSE` notice if redistributing source; no per-unit cost, no copyleft. |
| `microwakeword`'s bundled `libtensorflowlite_c` (inside `pymicro-wakeword`) | Apache-2.0 (TensorFlow) | Same as above. |
| The trained `trstenko.tflite` + manifest | Ours -- see training data below | Only as restricted as the data it was trained on. |

**Porcupine (`pvporcupine`, `firmware/wakeword/porcupine.py`) does not ship.**
Its free tier is personal/non-commercial only (Picovoice's own FAQ: "no
dedicated free or paid plans for personal or non-commercial use" for anyone
else). Config-gated to the competition build; see `docs/research/
software-stack.md` for the full pricing research.

## Training-time only (never distributed to a customer)

GPL code that only ever runs inside `docker compose run generate`, on a dev
machine, producing `.wav` files as output, is not a distribution of that GPL
code to anyone -- there is nothing to comply with beyond "don't ship it,"
which the Docker split (see `README.md`) enforces structurally.

| Component | License | Role |
|---|---|---|
| `piper-sample-generator` | MIT | The generation CLI/tooling. |
| `piper` (a.k.a. `piper1-gpl`, `piper-tts` on PyPI) | **GPL-3.0-or-later** | `piper-sample-generator` imports it for phonemization (`piper.phonemize_espeak`) and for `.onnx`-voice synthesis (`PiperVoice`). Pulled in by `Dockerfile.generate`; never installed in `Dockerfile.train` or anywhere in `firmware/`. |
| `torch` / `torchaudio` | BSD-3-Clause | Runs the LibriTTS-R generator checkpoint. |
| `microwakeword` (training framework) | Apache-2.0 | `Dockerfile.train`. |
| `tensorflow` (training only, via `tensorflow/tensorflow:2.18.0-gpu`) | Apache-2.0 | Same. |

**Separate finding, not blocking this task but flagged because it changes an
existing decision**: `docs/research/software-stack.md` calls Piper TTS "Apache-2.0" for the
robot's *spoken output*, based on the original `rhasspy/piper` project. That
repo is now archived; the actively maintained fork (`OHF-Voice/piper1-gpl`,
what `pip install piper-tts` now installs) **relicensed to GPL-3.0-or-later**
at some point after the original MIT release (last MIT tag: `2023.11.14-2`,
over two years stale). This doesn't block selling the device -- Piper runs
as a separate subprocess the firmware invokes and reads stdout from, not
code linked into the firmware, so GPL's copyleft doesn't reach across that
boundary (the standard "mere aggregation" argument, same as any product that
shells out to `ffmpeg`). It does mean: keep invoking it as a separate
process, never `import piper` from `firmware/`; include its license text and
a link to its source with the device documentation; and because Raspberry Pi
OS is a general-purpose Linux a buyer can already reinstall onto, GPLv3's
anti-tivoization clause (installation information) is satisfied without extra
work. Worth a line in `docs/research/software-stack.md` under TTS; not
re-litigated here since it's a separate concern from the wake word.

## Training data

| Source | License | Used for | Why it's clean |
|---|---|---|---|
| LibriTTS-R generator checkpoint (`en_US-libritts_r-medium.pt`, via `rhasspy/piper-checkpoints`, MIT repo) | Trained on LibriTTS-R, **CC BY 4.0** | Positive + negative-speech synthesis, English | CC BY permits commercial use; requires attribution, which is recorded here: *"Contains material derived from LibriTTS-R (Koizumi et al.), CC BY 4.0."* Carry that line into the shipped device's third-party notices before sale. |
| `sl_SI-artur-medium` Piper voice (`rhasspy/piper-voices`) | MIT | Positive + negative-speech synthesis, Slovenian | Explicit MIT on the HF repo. |
| Procedural noise (`scripts/generate_negative_noise.py`) | N/A -- generated, not downloaded | Negative background | Nothing to license; it's synthesized from `numpy`. |
| (Recommended, not yet collected) team's own recordings of "Trstenko" and of the real deployment environment | Team-owned | Positive + negative background | Own copyright, zero restriction, and better data than any of the above. |
| Freesound.org, CC0-filtered (`scripts/import_freesound.py`) | **CC0 per clip**, filtered at query time via the API's own `license` field -- never a dataset-level tag | Negative background (real ambient recordings) | Every Freesound upload carries its own contributor-chosen license (CC0, CC BY, CC BY-NC, all mixed on the same site) -- this only ever downloads clips the API itself reports as CC0, checked per clip, which is the opposite failure mode from the rejected `AudioSet`/`fma_xsmall` mirrors below (a re-packager's blanket tag standing in for licenses the re-packager doesn't actually hold). Freesound's own FAQ explicitly endorses this: CC0 sounds may be sold outright, and their AI-training guidance says commercial training is fine as long as non-CC0 sounds are excluded, which the API filter already does. `SOURCES.txt` written alongside the clips logs id/author/license/url per download for an audit trail, even though CC0 itself requires no attribution. One live caveat: the API's own terms of service say "commercial use requires negotiation" for the *API access itself* -- read as being about heavy-volume commercial services built on the API, not a one-time pull of a few dozen clips for local training data, but worth re-confirming with Freesound directly before scaling this pull up a lot. |

### Explicitly rejected -- do not reintroduce

The micro-wake-word project's own `basic_training_notebook.ipynb` downloads
all of the below and says outright: *"the data downloaded here has a mixture
of different licenses and usage restrictions... any custom models trained
with this data should be considered as appropriate for non-commercial
personal use only."* Confirmed independently per-source:

| Source | Checked license | Verdict |
|---|---|---|
| `kahrendt/microwakeword` (the pre-made negative spectrogram features: `dinner_party`, `no_speech`, `speech`, etc.) | **CC BY-NC-4.0**, stated on the HF dataset page | Blocks commercial use outright. This is the one a from-the-docs implementation would reach for first, since it's the "just download features, skip the audio pipeline" shortcut -- don't. |
| `agkphysics/AudioSet` mirror | Tagged `cc-by-4.0` by the uploader, but re-hosts decoded audio extracted from YouTube videos that each carry their own (often "standard YouTube license", i.e. all-rights-reserved) license | The uploader can't grant rights they don't hold over the underlying audio; the notebook's own disclaimer exists because of exactly this dataset. |
| `mchl914/fma_xsmall` mirror | Tagged `mit` by the uploader | Underlying Free Music Archive tracks are individually licensed by their artists (CC BY, CC BY-NC, CC BY-SA, CC0, mixed) -- an uploader's own MIT tag on a repackaging script doesn't relicense the songs inside it. |
| `davidscripka/MIT_environmental_impulse_responses` | `license:unknown` on HF | Not necessarily restrictive, but not confirmed permissive either -- treated as unusable until someone finds the actual terms. This is why RIR augmentation is off; see `README.md`. |
| Mozilla Common Voice, current distribution (`scripts/import_common_voice.py` -- **do not use until this changes**) | Audio itself is CC0, but as of October 2025 the *only* distribution channel (Mozilla retired the old direct-download page and their own official Hugging Face org in favor of it) is Mozilla Data Collective, whose platform Terms of Use state plainly: **"Commercial use of datasets is prohibited without prior written consent."** | Same failure mode as `AudioSet` above, one layer up: a permissive license on the *content* doesn't help when the *access terms you have to agree to in order to get it* separately block commercial use. Blocks this outright until someone actually gets Mozilla's written consent (their terms explicitly allow requesting it) -- the import script exists and works, but is not cleared to run against real data yet. First noted as clean, then corrected same-day once MDC's actual Terms of Use got read instead of just the CC0 tag on the content -- left in here specifically so nobody re-does that same research and reaches the same wrong first conclusion. |

If someone reintroduces `pip install`-ing any of the above into this
pipeline later "to improve the model," they've reintroduced the exact
problem this task exists to remove. Point them at this file.

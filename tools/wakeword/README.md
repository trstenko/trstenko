# wakeword

Trains the "Trstenko" model for **micro-wake-word** (Apache-2.0, free per
unit) -- the engine `firmware/wakeword/micro_wakeword.py` loads on any unit
that gets sold. Porcupine stays the competition-demo engine; see
`docs/research/software-stack.md` for why the two can't swap roles.

This is a dev tool. Nothing here is copied onto the robot (`tools/
check_layout.py` enforces that); it produces one artifact the robot does
load: a `.tflite` file plus a small JSON manifest, dropped in
`firmware/wakeword/models/`.

**Read `LICENSES.md` before changing what feeds the training data.** The
short version: the recipe in micro-wake-word's own example notebook pulls in
AudioSet and FMA mirrors that are non-commercial or unknown-license, and the
project's own pre-packaged negative-feature dataset
(`kahrendt/microwakeword` on Hugging Face) is **CC BY-NC 4.0** -- explicitly
not for a product that gets sold. Everything under `scripts/` here uses a
different, commercially-clean set of sources instead. Don't add the notebook's
downloads back in without re-reading that file.

## Why Docker

Training needs TensorFlow >= 2.18, and this machine only has Python 3.14
installed, which current TensorFlow does not support. Sample generation needs
PyTorch plus `piper` (the GPL-3.0 TTS engine) for phonemization -- a
dependency that must never reach `requirements.txt`/`requirements-pi.txt`,
because those install on the robot. Two containers keep both facts true
without hand-managing a Python environment:

- **`generate`** -- PyTorch, `piper-sample-generator`, `piper` (GPL-3.0).
  Turns text into wake-word and negative-speech `.wav` files. Never runs on
  the robot; its GPL dependency never gets distributed to anyone.
- **`train`** -- the official `tensorflow/tensorflow:2.18.0-gpu` image, plus
  `microwakeword`. Turns `.wav` files into spectrogram features, trains, and
  exports the quantized streaming `.tflite`.

Both need the host's NVIDIA GPU (`--gpus all`); confirmed working via Docker
Desktop's WSL2 GPU passthrough on this machine (RTX 4070 SUPER).

## Running it

Either the commands below directly, or `python -m tools.wakeword.ui` for a
local dashboard that runs the same commands from a browser with live logs
and run history -- see `ui/README.md`. `TRAINING_GUIDE.md` covers what the
numbers mean either way.

    docker compose build
    docker compose run --rm generate bash scripts/generate_positive.sh
    docker compose run --rm generate bash scripts/generate_negative_speech.sh
    docker compose run --rm train python scripts/generate_negative_noise.py
    docker compose run --rm train python scripts/build_features.py
    docker compose run --rm train bash scripts/train.sh
    docker compose run --rm train python scripts/export_model_manifest.py

Once exported, try it against any WAV clip (drop one under `data/`, e.g.
`data/try_clips/`, since that's the folder actually reachable inside the
container):

    docker compose run --rm train python scripts/try_clip.py data/try_clips/whatever.wav

Prints a per-10ms-step trigger probability and flags every step past cutoff.
This is not a live-mic test -- `pymicro_wakeword`, the engine
`firmware/wakeword/micro_wakeword.py` actually loads, ships a bundled aarch64
Linux binary and only runs on the real Pi. `try_clip.py` reuses the plain
TensorFlow Lite path `model_train_eval` already runs on this machine to
produce the frr/faph numbers, just against one clip you pick.

`tools/emulator`'s "WAKE WORD TEST" section wraps this same script behind a
"record 2s and test" button, using whichever audio input is selected under
its own AUDIO section (pick "this computer's microphone" for a real test) --
same non-live-mic caveat applies.

## Getting real recordings at scale

Two ways real recordings reach `data/positive_real` / `data/negative_real`:

- The dashboard's own "Record from phone" panel (same-wifi only, see above).
- **`voice-collector`** (sibling repo, `../voice-collector/`) -- a
  public-internet-facing site for collecting recordings from anyone with the
  link, with an admin review queue (approve/deny), optional invite codes,
  and a "download zip" export shaped `positive/*.wav` / `negative/*.wav`.
  Drop that zip into this dashboard's "Import training zip" panel and it
  unpacks straight into `data/positive_real` / `data/negative_real`,
  continuing the same `real_NNNN.wav` numbering the phone-recording panel
  uses, so recordings from both sources coexist without colliding. The zip's
  `manifest.csv` (pseudonymous contributor ids, no names) is read on import
  and each clip's origin is appended to `data/real_import_map.csv`, so if a
  contributor later withdraws consent (the admin lists them under
  Contributors), their clips can be found and deleted from the local
  training set. See
  `../voice-collector/README.md` for deploying it.

These are recordings of the wake word and of your own room -- the
irreplaceable kind. For *volume* of generic real-world negative background
beyond what the team personally records, `scripts/import_freesound.py`
(real ambient recordings, CC0-filtered per clip via the API's own license
field) pulls from a source vetted the same way as everything else in
`LICENSES.md` -- see the script's own docstring for the one manual step
that can't be automated around (creating a Freesound API key). Auto-detected
by `build_features.py` the same way `positive_real`/`negative_real` are,
once run.

`scripts/import_common_voice.py` also exists (real human speech, en + sl)
but is **currently blocked** -- see `LICENSES.md`'s "Explicitly rejected"
table. The audio is CC0, but as of October 2025 the only way to download it
is through Mozilla Data Collective, whose Terms of Use prohibit commercial
use without Mozilla's prior written consent. The script refuses to run
without a `--i-have-mozilla-written-consent` flag until that's actually
obtained.

`data/` (bind-mounted into both containers) accumulates the intermediate
`.wav`s, the mmap feature sets, and `trained_models/`. It is large (multiple
GB, thousands of files) and gitignored -- nothing under it belongs in
version control. The one output that does leave this directory is the final
export, copied to `firmware/wakeword/models/trstenko.tflite` + `.json`.

**If this repo lives under a synced folder (OneDrive, Dropbox, etc.), move
`data/` out of it.** Confirmed on this project: thousands of small files
totaling several GB was enough to crash Explorer while the sync client tried
to keep up. Set `WAKEWORD_DATA_DIR` in a `.env` file next to
`docker-compose.yml` (gitignored, machine-specific) to point at a local,
unsynced path instead -- `docker-compose.yml` and `ui/server.py` both read
it, falling back to `./data` if it's absent.

## What's actually in the training data

- **Positive ("Trstenko")**: the LibriTTS-R multi-speaker generator (904
  English speakers, phonetic diversity) plus the single Slovenian Piper
  voice (`sl_SI-artur-medium`), so the model has heard both an anglicized
  and a native-Slovenian pronunciation. Team members should also record a
  few dozen real sayings once hardware exists -- synthetic-only positives are
  the biggest gap in this recipe; see "Honest gaps" below.
- **Negative speech**: the same two engines, reciting ordinary sentences and
  words that sound similar to "Trstenko" (see `scripts/wordlists/`), so the
  model learns those aren't a match rather than just learning "any voice
  triggers it."
- **Negative background**: procedurally generated noise beds
  (`scripts/generate_negative_noise.py` -- filtered white/pink noise, hums,
  amplitude-modulated variants) with zero licensing question because nothing
  is downloaded. This is a synthetic stand-in for real ambient recordings,
  not a replacement for them.

## Honest gaps (same tone as the rest of this repo's research docs)

- **No real-room negative recordings.** The single highest-value thing to
  add before trusting this model is 30-60 minutes of the actual desk/kitchen
  environment the device sits in -- TV, conversation, appliances, silence.
  It costs nothing (the team's own recording, their own copyright) and fixes
  false-positive behaviour synthetic noise can't. Record with the ReSpeaker
  Lite once it's wired up, save as 16kHz mono WAV under
  `data/negative_real/`, and add a `features` entry for it in
  `config/training_parameters.yaml` before the next training run.
- **No real human sayings of "Trstenko."** Same argument, positive side.
  Ten team members saying it five times each, in the room it'll sit in, is a
  much stronger positive set than more synthetic samples would be.
- **No RIR (room echo) augmentation.** The MIT RIR dataset the reference
  notebook uses has an **unknown** license on Hugging Face; skipped rather
  than guessed at. `AddBackgroundNoise` and `Gain` still run; only the
  simulated-room-echo augmentation is off. Worth revisiting with a
  synthetically generated RIR set (`pyroomacoustics`, BSD-licensed, computes
  impulse responses instead of recording them) if false negatives in
  reverberant rooms turn out to matter.
- **Training a *good* model is iterative -- confirmed, not hypothetical.**
  The first real run (2026-09-26, 10000 steps, data as described above)
  finished and exported cleanly, but its own streaming-quantized evaluation
  says it isn't ready: at the exported cutoff (0.5) the false-*rejection*
  rate is **30%** -- it would miss roughly one in three real "Trstenko"s.
  Lower cutoffs reject less (14% at 0.10) at an unknown cost in false
  accepts, because...
- **...the false-accept-per-hour metric came back `nan` on this run.**
  `model_train_eval`'s ambient-false-positive count needs a features
  category built specifically as one continuous negative stream, split into
  overlapping windows (`truncation_strategy: split`, `sampling_weight: 0`,
  same shape as the reference notebook's `dinner_party_eval`). Nothing in
  `config/training_parameters.yaml` is set up that way yet -- `negative_noise`
  is sampled as ordinary training/validation data instead. Add one before the
  next run, or the cutoff is being chosen blind on false accepts.
  **Concretely, next steps in order**: (1) add an ambient-eval feature
  category as above so cutoff selection has real faph numbers to read, (2)
  increase `positive_class_weight` and/or lower `negative_class_weight` in
  `training_parameters.yaml` -- a 30% miss rate against a 20:1 negative
  weighting suggests the loss is currently biased hard toward never false-
  accepting, at the cost of recall, (3) add the real recordings this file
  already asks for (team sayings + real-room ambience), which push the
  false-accept measurement onto real data instead of only synthetic.

## Licensing summary

See `LICENSES.md` for the full chain with sources. Nothing that ends up
distributed on the robot (the `.tflite` + JSON manifest, or the
`pymicro-wakeword` runtime that loads them) carries a restriction that
blocks selling the device.

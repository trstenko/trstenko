# Training a production "Trstenko" model

What "good enough to ship" actually means, how to read the numbers this
pipeline prints, and what to change between runs. Written after the first
real run (2026-09-26) rather than from the framework's docs alone -- the
numbers below are what this project's own first attempt produced, not
hypothetical examples.

Read `README.md` first for how to run each step and `LICENSES.md` for why
the data sources are what they are. This file is about judgment calls once
the pipeline is running: what to look at, what to change, when to stop.

## The two numbers that matter

Every run ends by testing the exported model and printing a sweep like:

```
INFO:absl:Cutoff 0.50: frr=0.3000; faph=nan
```

- **frr (false rejection rate)**: of real "Trstenko"s, what fraction the
  model misses at this cutoff. Lower is better. This is annoyance -- a
  device that doesn't hear its own name.
- **faph (false accepts per hour)**: on continuous non-wake-word audio, how
  many times per hour the model fires anyway. Lower is better. This is
  worse than annoyance -- the device replies to nothing, out loud, unprompted.

They trade off against each other via the `probability_cutoff` in
`firmware/wakeword/models/trstenko.json` -- raise it, faph drops and frr
rises; lower it, the reverse. **You get to pick a point on that curve, not
a single "accuracy" number.** `export_model_manifest.py`'s
`PROBABILITY_CUTOFF` env var is that choice.

**Target, per microWakeWord's own convention** (`test.py`'s default
`max_faph=2.0`, which is also roughly the openWakeWord/Home-Assistant-voice
community's working assumption for "doesn't annoy anyone in a normal room"):
**faph <= 2, frr as low as possible at that faph.** A device sitting on a
desk for 8 waking hours should false-trigger at most once or twice a day,
full stop -- that number is the actual product requirement, not the frr one.
Once faph <= 2 is hit, frr is what you tune for demo-quality responsiveness.

**A third number, `AUC`, printed once at the top of the same file**: area
under the frr-vs-faph curve, so **lower is better here too** -- it's an area
of *error*, not a correlation-style score where higher wins. `AUC 0.00000`
with `frr=0.0000` at every cutoff is the good case (zero error area found),
not a bug -- confirmed by hand-checking run 3 below, where it briefly looked
alarming until the math was worked through.

## Reading `tflite_streaming_roc.txt`

`data/trained_models/trstenko/tflite_stream_state_internal_quant/
tflite_streaming_roc.txt` has the full cutoff sweep from the last run, not
just what scrolled past in the log. Pick `probability_cutoff` from *this*
file: find the highest faph <= 2 (lowest cutoff satisfying it), read off its
frr, and use that cutoff. If every row says `faph=nan`, the ambient eval set
wasn't built -- see "Fixed 2026-09-26" below before trusting any cutoff
choice from that run.

## What the first real run actually said (2026-09-26)

10000 steps, `positive_class_weight=1`, `negative_class_weight=20`, data as
described in `README.md`. Result: **frr=0.30 at cutoff 0.50**, and every
`faph` value was `nan`.

Two separate problems, not one:

1. **The ambient (faph) measurement was never wired up.** `model_train_eval`
   computes false-accepts-per-hour from `testing_ambient`/`validation_ambient`
   folders that `build_features.py` never created -- it built the same
   `training`/`validation`/`testing` split for every category, including the
   background-noise one. Division by a zero-duration ambient set is `nan`,
   not zero.

   **Fixed 2026-09-26**: `build_features.py` now also builds
   `testing_ambient`/`validation_ambient` for any category listed in
   `AMBIENT_CATEGORIES` (currently `negative_noise`, plus `negative_real`
   once that directory has anything in it) -- whole, *unaugmented*
   spectrograms, because the ambient test is supposed to measure what the
   model does on raw background audio, not on the augmented training
   distribution. `model_train_eval` scans every configured `features_dir`
   for those two folder names regardless of that entry's `sampling_weight`,
   so no `training_parameters.yaml` change was needed -- just re-run
   `build_features.py` before the next `train.sh`.

2. **30% frr is plausibly a class-weight problem, not a data problem.**
   `negative_class_weight: 20` against `positive_class_weight: 1` tells the
   loss "a missed wake word costs 1, a false accept costs 20" -- a deliberate
   bias toward never false-accepting, at recall's expense. That bias made
   sense as a default when there was no faph measurement to check it against
   (better to err toward silence than toward nagging). Now that faph is
   measurable, the honest move is to **lower `negative_class_weight`
   (try 5-8) and re-run**, then look at whether faph is still <= 2. If it's
   comfortably under, the weighting was too conservative and recall was
   paid for nothing; if it jumps well past 2, the original weighting was
   closer to correct and the real fix is more/better negative data instead.

## What runs 2 and 3 found

**Run 2** (`negative_class_weight` 20 -> 8, ambient eval wired up): the
weighting theory from run 1 was right. `frr=0.0000` and `faph=0.000` at
cutoff 0.03 -- both numbers essentially perfect on the held-out synthetic
set. But the ROC file had exactly 2 rows (`AUC 0.00000`, one point at
faph=0, one at faph=2), which is the "ambient set too short" problem
described under "Knobs" above: ~10 minutes of ambient test audio gives almost
no resolution between cutoffs.

**Run 3** (same weights, negative_noise regenerated at 2x the clip count,
`AMBIENT_CATEGORIES` split bumped to 30%): ~1 hour of ambient test audio per
split instead of ~10 minutes. Result: 3 distinct rows instead of 2 --
`faph` = 0.000 / 1.022 / 2.000 across cutoffs 0.03 / 0.02 / 0.01, `frr=0`
at all three. Meaningfully better resolution, still on the thin side for a
production sign-off (see the ambient-duration knob above).

**The honest read of "frr=0 at faph=0," twice in a row**: this says the
pipeline and the eval math both work correctly now, and that the model
separates *this specific synthetic distribution* essentially perfectly. It
is not evidence the model works on a real human voice through a real
microphone in a real room -- Piper-synthesized speech and procedurally
generated noise are more mutually distinguishable than the real thing will
be. Treat "run 3 was flawless" as "the harness is trustworthy, time to feed
it real data" rather than "done." The production checklist at the bottom of
this file says the same thing structurally; this is the concrete run that
prompted writing it that way.

## Knobs, in the order to reach for them

1. **`negative_class_weight` / `positive_class_weight`** in
   `training_parameters.yaml`. Cheapest thing to change -- no new data, no
   rebuild, just re-run `train.sh`. Start here after every features rebuild.
2. **More positive data, especially real recordings.** Synthetic-only
   positives (this pipeline's current state) cap how well the model
   generalizes to an actual human voice, actual room acoustics, actual mic.
   Ten team members saying "Trstenko" five times each, recorded once
   hardware exists, dropped into `data/positive_real/` and added as a
   `CATEGORIES` entry in `build_features.py`, is worth more than doubling
   the synthetic sample count.
3. **Real ambient recordings**, same argument, negative side. Drop 30-60
   minutes of actual desk/kitchen audio into `data/negative_real/` --
   `build_features.py` already picks it up automatically (see "What's
   actually in the training data" in `README.md`) and treats it as an
   ambient category too.
4. **More/harder negative speech.** If a specific false-accept keeps
   showing up once you have real ambient data (a phrase, a name, a TV ad),
   add it to `scripts/wordlists/negative_en.txt` or `negative_sl.txt` and
   regenerate -- this is the "hard negative mining" the microWakeWord docs
   gesture at, made concrete.
5. **Ambient test set duration**, if faph looks unstable rather than wrong.
   Confirmed on run 2 (2026-09-26): with the default 10%-to-eval split,
   `testing_ambient` worked out to about 10 minutes of audio, and the whole
   ROC sweep collapsed to 2 points (0 false accepts, or ~6/hour -- nothing in
   between). faph is a count of false accepts divided by ambient *hours*;
   too short a test set means every individual false accept swings the
   estimate by a large multiple. `build_features.py` now reserves 30% of
   `AMBIENT_CATEGORIES` clips for test+validation instead of the default 10%
   specifically because of this -- it costs nothing in augmentation variety
   (`background_paths` reads the whole directory regardless of split; only
   this category's own direct training examples shrink, and
   `negative_speech_*` already covers "this word is not the wake word").
   Run 3 (same weights as run 2, just this fix + a bigger negative_noise
   pool) resolved into 3 distinct points instead of 2 -- meaningfully more
   signal, though still on the thin side; a production run should budget for
   multiple hours of ambient audio if faph precision right at the cutoff
   boundary matters.
6. **`training_steps`, `learning_rates`.** Last, not first -- this dataset
   is small enough (a few thousand clips) that 10000 steps at 0.001 is
   already past the point of the loss curve flattening in the first run.
   More steps on the same data mostly risks overfitting a synthetic
   distribution harder, not generalizing better.

## Between runs: what to actually do

```
docker compose run --rm generate bash scripts/generate_positive.sh       # if you changed positive data
docker compose run --rm generate bash scripts/generate_negative_speech.sh # if you changed the wordlists
docker compose run --rm train python scripts/generate_negative_noise.py  # if you changed the noise families
docker compose run --rm train python scripts/build_features.py          # always, after any data change
docker compose run --rm train bash scripts/train.sh                     # always
docker compose run --rm -e PROBABILITY_CUTOFF=<chosen> train python scripts/export_model_manifest.py
```

`scripts/` and `config/` are bind-mounted (not baked into the image), so
editing `training_parameters.yaml` or any script takes effect on the next
`docker compose run` with no rebuild. Only a `Dockerfile.*` change needs
`docker compose build`.

Keep old runs before overwriting them: `data/trained_models/trstenko/` gets
replaced by the next `train.sh`. Copy `tflite_streaming_roc.txt` (and note
the yaml settings used) somewhere before the next run if you want to compare
-- or use the training UI (below), which does this for you.

## The training UI

`ui/` is a small local dashboard (`python -m tools.wakeword.ui`, from the
`code/` directory, on the host -- not in Docker) that:

- shows dataset counts per category (positive/negative wav counts, whether
  ambient categories have data),
- lists past runs with their final frr/faph table and the hyperparameters
  used, read from `data/runs/<timestamp>/`,
- launches a new run (`build_features.py` -> `train.sh` ->
  `export_model_manifest.py`) with hyperparameters entered in the page,
  streaming the live log,
- lets you set the export cutoff by clicking a point on the frr/faph curve
  instead of re-reading the sweep table by eye.

See `ui/README.md` for what it does and doesn't do -- it shells out to the
same `docker compose run` commands above, it doesn't replace understanding
them.

## Production checklist, before this ships in a sold unit

- [ ] `testing_ambient`/`validation_ambient` populated, faph is a real
      number (not `nan`), and faph <= 2 at the chosen cutoff.
- [ ] At least some positive data is real human recordings, not only
      synthetic voices.
- [ ] At least some negative data is a real recording of the deployment
      environment (desk/kitchen), not only procedural noise.
- [ ] The exported `probability_cutoff` was chosen by reading
      `tflite_streaming_roc.txt` for the actual run being shipped, not
      copied from an earlier run's config.
- [ ] The model has been tested on the actual Pi + ReSpeaker Lite hardware,
      not only against held-out synthetic clips -- a TFLite model that
      scores well on its own test set can still behave differently against
      a real mic's noise floor and frequency response.
- [ ] `firmware/wakeword/` config for the release build selects
      `micro_wakeword`, not `porcupine`.

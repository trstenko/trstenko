`trstenko.tflite` + `trstenko.json` land here from
`tools/wakeword/scripts/export_model_manifest.py`. They are the one artifact
the whole training pipeline in `tools/wakeword/` produces for the robot to
actually load -- everything else in that directory (Docker images, raw
`.wav`s, intermediate mmap features) stays out of the repo.

Empty until the first training run finishes. `MicroWakeWordDetector` (see
`../micro_wakeword.py`) raises if it isn't here rather than silently doing
nothing -- a device that can't hear its name should say so.

## Hey Jarvis, the fallback

`hey_jarvis.tflite` + `hey_jarvis.json` are the stock micro-wake-word model by
Kevin Ahrendt, copied unchanged from
[esphome/micro-wake-word-models](https://github.com/esphome/micro-wake-word-models)
(`models/v2/`, commit 40ff33f), which is Apache-2.0. `vad.tflite` comes from
the same repository. The owner picks it under Device > Listening
(`audio.wake_word`) if the Trstenko model does not hear well enough.
English only; trained by its author, not by `tools/wakeword/`.

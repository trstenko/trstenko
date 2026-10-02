`trstenko.tflite` + `trstenko.json` land here from
`tools/wakeword/scripts/export_model_manifest.py`. They are the one artifact
the whole training pipeline in `tools/wakeword/` produces for the robot to
actually load -- everything else in that directory (Docker images, raw
`.wav`s, intermediate mmap features) stays out of the repo.

Empty until the first training run finishes. `MicroWakeWordDetector` (see
`../micro_wakeword.py`) raises if it isn't here rather than silently doing
nothing -- a device that can't hear its name should say so.

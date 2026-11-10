"""micro-wake-word: Apache-2.0, free per unit, the device's only wake word
engine. `tools/wakeword/` trains the Trstenko model it loads; the Hey Jarvis
fallback is a stock model (see `models/README.md`).

`pymicro-wakeword` ships its own bundled `libtensorflowlite_c` (aarch64,
glibc >=2.35 -- Bookworm 64-bit satisfies that) so there is nothing to
compile on the Pi.
"""

from . import base


class MicroWakeWordDetector(base.WakeWordDetector):
    def __init__(self, model_path, probability_cutoff=None):
        from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures

        self._features = MicroWakeWordFeatures()
        self._model = MicroWakeWord.from_config(model_path)
        if probability_cutoff is not None:
            self._model.probability_cutoff = probability_cutoff

    def process(self, chunk):
        triggered = False
        for features in self._features.process_streaming(chunk):
            if self._model.process_streaming(features):
                triggered = True
                # Without this the sliding window stays above cutoff for the
                # rest of the utterance and `process()` fires on every frame
                # until the speaker stops talking, not once.
                self._model.reset()
        return triggered

    def close(self):
        self._model.close()
        self._features.reset()

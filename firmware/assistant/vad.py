"""Is anyone speaking? The question that decides when a turn has ended.

A turn ends when the person stops talking, and the first way to ask that is
loudness: below a level for 0.7 seconds. In a quiet room it works. A fan, a
television or a second voice either cut people off or never end the turn
(`docs/research/llm-workflow.md`, Finding 13). So the decided answer is
micro-wake-word's own VAD model, run through the `pymicro-wakeword` package the
wake word already uses: no second runtime, 34KB, under a megabyte of memory, and
it separates speech from room noise cleanly on the clips we have. Its default
cutoff of 0.5 misses speech from across a room, so ours starts at 0.3
(`audio.vad_cutoff`), to be tuned on the board.

The energy detector stays as the fallback, and the fallback is loud about being
one: if the model or its package is missing, or fails once, the device logs it
and carries on with loudness for the rest of the run rather than ending every
turn at the 12 second cap or not hearing the person at all.

Both answer the same question of a 10 millisecond chunk: `speech(chunk)`.
"""

import array
import logging
import math
import os

log = logging.getLogger("trstenko.vad")

# What counts as full scale for the loudness meters, shared with the emulator's
# own (`tools/emulator/audio.py`): speech at a desk peaks well below int16 max.
FULL_SCALE = 8000.0
# The loudness above which a chunk is speech, as a fraction of that scale. This
# is the emulator's SILENCE_LEVEL, which has been used against real microphones.
ENERGY_LEVEL = 0.06

MODELS = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "wakeword", "models")


def level(chunk):
    """Loudness of 16-bit mono PCM, 0.0 to 1.0."""
    samples = array.array("h")
    samples.frombytes(chunk[:len(chunk) - len(chunk) % 2])
    if not samples:
        return 0.0
    total = 0
    for s in samples:
        total += s * s
    return min(1.0, math.sqrt(total / len(samples)) / FULL_SCALE)


class EnergyVad:
    name = "energy"

    def __init__(self, threshold=ENERGY_LEVEL):
        self.threshold = threshold

    def speech(self, chunk):
        return level(chunk) >= self.threshold

    def reset(self):
        pass

    def set_cutoff(self, value):
        pass


class MicroVad:
    """micro-wake-word's VAD model. Raises on construction if it cannot run, so
    `open_vad` can fall back instead of failing later."""

    name = "micro"
    # The model answers once per feature step, a few times per 10ms chunk or
    # not at all. The latest few answers are what "now" means.
    WINDOW = 8

    def __init__(self, config_path, cutoff=0.3):
        from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures

        self._features = MicroWakeWordFeatures()
        self._model = MicroWakeWord.from_config(config_path)
        self.cutoff = cutoff
        self._recent = []

    def speech(self, chunk):
        for features in self._features.process_streaming(chunk):
            prob = self._model.process_streaming_prob(features)
            if prob is not None:
                self._recent.append(float(prob))
        del self._recent[:-self.WINDOW]
        return bool(self._recent) and max(self._recent) >= self.cutoff

    def reset(self):
        self._recent = []
        self._model.reset()
        self._features.reset()

    def set_cutoff(self, value):
        self.cutoff = float(value)

    def close(self):
        self._model.close()


class SafeVad:
    """Uses the model, and drops to loudness for good the first time it fails.
    One bad frame must not end every later turn at the cap."""

    def __init__(self, primary, fallback):
        self._primary = primary
        self._fallback = fallback
        self.failed = False

    @property
    def name(self):
        return self._fallback.name if self.failed else self._primary.name

    def speech(self, chunk):
        if not self.failed:
            try:
                return self._primary.speech(chunk)
            except Exception:
                log.exception("the VAD failed; using loudness from now on")
                self.failed = True
        return self._fallback.speech(chunk)

    def reset(self):
        try:
            if not self.failed:
                self._primary.reset()
        except Exception:
            self.failed = True
        self._fallback.reset()

    def set_cutoff(self, value):
        self._primary.set_cutoff(value)


def open_vad(cutoff=0.3, directory=MODELS):
    """The best VAD available, never None."""
    energy = EnergyVad()
    config = os.path.join(directory, "vad.json")
    try:
        if not os.path.exists(config):
            raise FileNotFoundError(config)
        micro = MicroVad(config, cutoff)
    except Exception as exc:
        log.warning("the VAD model is not available (%s); using loudness",
                    exc)
        return energy
    log.info("using the micro-wake-word VAD, cutoff %.2f", cutoff)
    return SafeVad(micro, energy)

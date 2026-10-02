"""One engine, swappable by config, because the two available engines are
licensed for different situations rather than different quality.

`docs/research/software-stack.md` has the full story. The short version: the
Picovoice Console can turn "Trstenko" into a working Porcupine model in
under a minute, but Porcupine's free tier is personal/non-commercial only --
fine for the competition demo, not for a unit that gets sold. micro-wake-word
is Apache-2.0 and free to ship, but the model has to be trained
(`tools/wakeword/`), and training a good one takes real effort. Both consume
the same 16kHz 16-bit mono stream from `hal.Mic`, so the interface below is
the entire cost of keeping that decision reversible.
"""


class WakeWordDetector:
    """Consumes audio a chunk at a time; says when it heard its name."""

    def process(self, chunk):
        """`chunk` is 16-bit mono PCM at 16kHz, any length -- the same 160-
        frame reads `hal.Mic` already produces elsewhere in the pipeline.
        Each engine buffers internally to whatever window it actually wants,
        so nothing above this layer needs to know that size.
        Returns True on the frame the wake word finishes triggering."""
        raise NotImplementedError

    def close(self):
        pass


def open_detector(config):
    """`config` is the `[wakeword]` section of the on-device settings: at
    least `engine` ("porcupine" or "micro_wakeword") plus whatever that
    engine needs. Raises if the engine's package or model file is missing,
    rather than silently falling back -- a device that can't hear its name
    should say so, not idle quietly."""
    engine = config["engine"]
    if engine == "porcupine":
        from .porcupine import PorcupineDetector
        return PorcupineDetector(
            access_key=config["access_key"],
            keyword_path=config["keyword_path"])
    if engine == "micro_wakeword":
        from .micro_wakeword import MicroWakeWordDetector
        return MicroWakeWordDetector(
            model_path=config["model_path"],
            probability_cutoff=config.get("probability_cutoff", 0.5))
    raise ValueError("unknown wake word engine: %r" % (engine,))

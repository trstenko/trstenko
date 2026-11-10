"""The wake word engine, behind one small interface.

There is one engine: micro-wake-word (Apache-2.0, free per unit), loading
either the trained Trstenko model or the stock Hey Jarvis fallback. Porcupine
was dropped on 2026-11-10: its free tier is non-commercial and the units are
sold. The interface stays, so another engine can be added without touching
the audio path; it consumes the 16kHz 16-bit mono stream from `hal.Mic`.
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
    least `engine` ("micro_wakeword") plus whatever that
    engine needs. Raises if the engine's package or model file is missing,
    rather than silently falling back -- a device that can't hear its name
    should say so, not idle quietly."""
    engine = config["engine"]
    if engine == "micro_wakeword":
        from .micro_wakeword import MicroWakeWordDetector
        return MicroWakeWordDetector(
            model_path=config["model_path"],
            # No cutoff means the model's own, from its manifest. A default
            # here used to turn "normal" sensitivity into 0.5.
            probability_cutoff=config.get("probability_cutoff"))
    raise ValueError("unknown wake word engine: %r" % (engine,))

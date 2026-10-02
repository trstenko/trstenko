"""The other end of the pipe: runs Piper, one sentence per line, framed.

    python piper_worker.py MODEL CONFIG LENGTH_SCALE SPEAKER

It is a script and not part of the firmware's own process on purpose. Piper
takes about 200MB of the board's 512MB (`llm-workflow.md`, Finding 14), pulls
in onnxruntime, and has been seen to die with an illegal instruction on some
Cortex-A53 builds. In its own process, a crash costs the voice, which the
supervisor brings back, and not the face, the reminders and the mute switch.
It also means Piper can live in its own environment, with its own pinned
onnxruntime, whatever Python the firmware runs on.

The protocol is length-prefixed, because Piper's raw output has no marks between
sentences and guessing the end from a gap in the stream would put a delay on
every sentence:

    on start:   b"TRPR" + uint32 sample rate        (the model is loaded)
    per line:   uint32 length + that many bytes of 16-bit mono PCM, any number
                of times, then uint32 0              (the sentence is done)

An empty line is answered with the 0 alone. Standard library until Piper is
imported, and Piper is imported only here.
"""

import struct
import sys


def main(argv):
    model, config = argv[1], argv[2]
    length_scale = float(argv[3]) if len(argv) > 3 else 1.0
    speaker = int(argv[4]) if len(argv) > 4 and argv[4] != "-" else None

    from piper import PiperVoice, SynthesisConfig

    voice = PiperVoice.load(model, config_path=config)
    syn = SynthesisConfig(length_scale=length_scale, speaker_id=speaker)
    out = sys.stdout.buffer
    out.write(b"TRPR" + struct.pack("<I", voice.config.sample_rate))
    out.flush()
    for raw in sys.stdin.buffer:
        text = raw.decode("utf-8", "replace").strip()
        if text:
            for chunk in voice.synthesize(text, syn_config=syn):
                data = chunk.audio_int16_bytes
                out.write(struct.pack("<I", len(data)) + data)
        out.write(struct.pack("<I", 0))
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

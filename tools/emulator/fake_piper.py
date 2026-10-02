"""A stand-in for `firmware/assistant/piper_worker.py`, with no model.

Speaks the same pipe protocol: a handshake with the sample rate, then for each
line a frame of PCM whose length depends on the text (about 60ms a character, so
a longer sentence takes longer to play, as it would), then a zero. It can be told
what to do through its arguments, so a scenario can make it slow, make it die
mid-sentence, or refuse to start:

    fake_piper.py MODEL CONFIG LENGTH_SCALE SPEAKER [delay=SECONDS]
                  [die_after=N] [die_on=TEXT] [no_handshake] [chunks=N]

The audio is a quiet tone, so it is audible if played, and its length is exact.
"""

import math
import struct
import sys
import time

RATE = 22050
SECONDS_PER_CHAR = 0.06


def tone(seconds):
    n = int(RATE * seconds)
    return b"".join(struct.pack("<h", int(3000 * math.sin(i * 0.05)))
                    for i in range(n))


def main(argv):
    opts = {}
    for arg in argv[5:]:
        key, _, value = arg.partition("=")
        opts[key] = value
    out = sys.stdout.buffer
    if "no_handshake" in opts:
        time.sleep(30)
        return 0
    if "die_at_start" in opts:
        return 3
    out.write(b"TRPR" + struct.pack("<I", RATE))
    out.flush()
    lines = 0
    for raw in sys.stdin.buffer:
        text = raw.decode("utf-8", "replace").strip()
        lines += 1
        if "die_after" in opts and lines > int(opts["die_after"]):
            return 5
        if "die_on" in opts and opts["die_on"] in text:
            out.write(struct.pack("<I", 1000) + b"\x00" * 500)   # cut short
            out.flush()
            return 6
        if "delay" in opts:
            time.sleep(float(opts["delay"]))
        if text:
            pcm = tone(max(0.1, len(text) * SECONDS_PER_CHAR))
            chunks = max(1, int(opts.get("chunks", "1")))
            step = len(pcm) // chunks // 2 * 2
            for i in range(chunks):
                part = pcm[i * step:(i + 1) * step] if i < chunks - 1 \
                    else pcm[i * step:]
                out.write(struct.pack("<I", len(part)) + part)
        out.write(struct.pack("<I", 0))
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

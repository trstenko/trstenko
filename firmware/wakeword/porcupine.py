"""Picovoice Porcupine: under 4% CPU, a working "Trstenko" model in about a
minute from the Picovoice Console -- and, per its free-tier terms, licensed
for personal/non-commercial use only. Competition demo and dev machines,
never a unit that gets sold. See `docs/research/software-stack.md`.
"""

import array

from . import base


class PorcupineDetector(base.WakeWordDetector):
    def __init__(self, access_key, keyword_path):
        import pvporcupine

        self._porcupine = pvporcupine.create(
            access_key=access_key, keyword_paths=[keyword_path])
        self._frame_length = self._porcupine.frame_length
        self._buffer = array.array("h")

    def process(self, chunk):
        self._buffer.frombytes(chunk)
        triggered = False
        n = self._frame_length
        while len(self._buffer) >= n:
            frame = self._buffer[:n]
            del self._buffer[:n]
            if self._porcupine.process(frame) >= 0:
                triggered = True
        return triggered

    def close(self):
        self._porcupine.delete()

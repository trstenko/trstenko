"""Speaking: sentences in, sound out, through one long-lived Piper process.

The model's reply arrives a sentence at a time (`speech_text`), and each goes to
`say()` the moment it is complete. What happens then:

    say(turn, text) -> synth thread -> Piper -> PCM -> play thread -> speaker

Why it is built this way, from `docs/research/llm-workflow.md`, Finding 14:

- **One process, loaded once.** Loading the model takes 2 to 3 seconds even on a
  desktop; a process per reply would add that to every turn. And never two at a
  time: Piper is about 200MB of a 512MB board, so switching voice stops one
  process before it starts the next.
- **Piper speaks a line at a time, all at once.** It does not stream within a
  sentence, so the length of the *first* sentence sets the time to first sound.
  That is why the prompt asks for a short first sentence, and why the splitter
  hands sentences on as soon as they are whole.
- **Synthesis and playback overlap.** While one sentence plays the next is
  being made, so the gaps between sentences are not the synthesis time. The
  playback side waits for the speaker to finish one before it starts the next,
  because the board's speaker replaces whatever is playing.
- **Piper's own process.** A crash costs the voice and not the face; the
  supervisor restarts it. The framed pipe (`piper_worker.py`) means the end of a
  sentence is stated, not guessed.

Cancelling (the mute switch, a button, a new turn) bumps an epoch. Everything
queued under an older epoch is dropped, the speaker stops, and the sentence Piper
is part-way through is read to its end and thrown away: it cannot be interrupted,
and leaving it unread would put its audio at the front of the next sentence.

The three threads are one worker to the supervisor. If any of them dies the
others are stopped, `TtsDied` is posted, and `WorkerDied` follows from the
`finally`, so main can bring the lot back.

Threads post events and never touch device state (plan 01). `SpeakingStarted`
comes with the first audio of a turn; `SpeakingDone` when the last sentence has
finished playing and the turn has said it will send no more (`finish`).
"""

import itertools
import logging
import os
import queue
import struct
import subprocess
import sys
import threading
import time

from .. import events as ev

log = logging.getLogger("trstenko.tts")

WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "piper_worker.py")
HANDSHAKE = b"TRPR"

# Loading a medium voice takes 2 to 3 seconds on a desktop and is slower on the
# board, which has not been measured.
# TODO measure Piper's load time and real-time factor on the Pi 3 A+ and set
# these from it (plan 08, first-boot checklist item 2).
START_TIMEOUT = 30.0
# Longest to wait for one sentence. A sentence that takes this long is a Piper
# that has hung.
SYNTH_TIMEOUT = 20.0
# Frames bigger than this are not audio.
MAX_FRAME = 1 << 22
POLL = 0.02


class TtsUnavailable(Exception):
    pass


class PiperSynthesizer:
    """See the module docstring. `speaker` is the HAL's, `post` puts an event on
    the main inbox."""

    def __init__(self, speaker, post, command=None, script=None, extra=(),
                 mono=time.monotonic):
        self.speaker = speaker
        self.post = post
        self.command = list(command or
                            [os.environ.get("TRSTENKO_PIPER") or sys.executable])
        self.script = script or WORKER_SCRIPT
        self.extra = list(extra)
        self._mono = mono
        self.voice = None
        self.directory = ""
        self.sample_rate = 22050
        self.state = "stopped"          # stopped, starting, ready, down
        self.restarts = 0
        self._epoch = 0
        self._turn_ids = itertools.count(1)
        self._jobs = queue.Queue()
        self._play = queue.Queue()
        self._frames = queue.Queue()
        self._proc = None
        self._closing = threading.Event()
        self._dead = threading.Event()
        self._threads = []
        self._lock = threading.Lock()
        self.started_speaking = set()

    # ---- life ----

    def configure(self, voice, directory):
        """Which voice, and where its files are. Takes effect at the next
        start, or at once through `set_voice`."""
        self.voice = voice
        self.directory = directory

    def start(self):
        """Begin, or begin again after a death. Returns at once; the model
        loads on the synth thread."""
        if self.voice is None:
            raise TtsUnavailable("no voice is installed")
        self._closing.clear()
        self._dead.clear()
        self._drain(self._jobs)
        self._drain(self._play)
        self.state = "starting"
        self._threads = [
            threading.Thread(target=self._guard, args=(self._synth_loop,),
                             name="tts-synth", daemon=True),
            threading.Thread(target=self._guard, args=(self._play_loop,),
                             name="tts-play", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def close(self):
        """Stop everything and wait for it. Used at shutdown."""
        self._closing.set()
        self._epoch += 1
        try:
            self.speaker.stop()
        except Exception:
            pass
        self._jobs.put(("stop",))
        self._play.put(("stop",))
        self._kill()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(3.0)
        self.state = "stopped"

    def set_voice(self, voice, directory=None):
        """Change voice. The old process is stopped before the new one starts,
        after whatever is already queued has been made."""
        if directory is not None:
            self.directory = directory
        self.voice = voice
        self._jobs.put(("voice", voice))

    def status(self):
        return {"state": self.state, "voice": self.voice.id if self.voice
                else None, "restarts": self.restarts}

    # ---- using it ----

    def new_turn_id(self):
        return "say-%d" % next(self._turn_ids)

    def say(self, turn_id, text):
        """Queue a sentence. False if there is no voice to say it with."""
        if self.state in ("down", "stopped") or not text.strip():
            return False
        self._jobs.put(("say", self._epoch, turn_id, text.strip()))
        return True

    def finish(self, turn_id):
        """No more sentences are coming for this turn: `SpeakingDone` follows
        the last one."""
        self._jobs.put(("end", self._epoch, turn_id))

    def say_line(self, text):
        """One whole utterance, as a reminder is: say it and finish."""
        turn_id = self.new_turn_id()
        ok = self.say(turn_id, text)
        self.finish(turn_id)
        return turn_id if ok else None

    def cancel(self):
        """Stop speaking and forget what was queued."""
        self._epoch += 1
        self._drain(self._jobs, keep=("voice", "stop"))
        self._drain(self._play, keep=("stop",))
        try:
            self.speaker.stop()
        except Exception:
            log.exception("the speaker would not stop")

    def level(self):
        return self.speaker.level()

    @staticmethod
    def _drain(q, keep=()):
        kept = []
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            if item and item[0] in keep:
                kept.append(item)
        for item in kept:
            q.put(item)

    # ---- the threads ----

    def _guard(self, fn):
        """Run a thread body, and when it ends for any reason but being told to,
        take the others down and tell main."""
        try:
            fn()
        except Exception as exc:
            if not self._closing.is_set():
                log.error("speech stopped: %s", exc)
                self._die(str(exc) or type(exc).__name__)
        else:
            if not self._closing.is_set() and not self._dead.is_set():
                self._die("the speech thread ended")

    def _die(self, reason):
        with self._lock:
            if self._dead.is_set() or self._closing.is_set():
                return
            self._dead.set()
            self.state = "down"
        self._kill()
        self._jobs.put(("stop",))
        self._play.put(("stop",))
        self.post(ev.Event(ev.TTS_DIED, data={"reason": reason}))
        self.post(ev.Event(ev.WORKER_DIED, data="tts"))
        self.restarts += 1

    def _spawn(self, voice):
        model, config = voice.files(self.directory)
        speaker = "-" if voice.speaker is None else str(voice.speaker)
        argv = (self.command + [self.script, model, config,
                                "%g" % voice.length_scale, speaker]
                + self.extra)
        log.info("starting Piper with voice %s", voice.id)
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)
        self._proc = proc
        frames = queue.Queue()
        self._frames = frames
        threading.Thread(target=self._read, args=(proc, frames),
                         name="tts-read", daemon=True).start()
        try:
            kind, value = frames.get(timeout=START_TIMEOUT)
        except queue.Empty:
            raise TtsUnavailable("Piper did not start in time")
        if kind != "rate":
            raise TtsUnavailable("Piper did not start")
        self.sample_rate = value
        self.state = "ready"
        return proc

    @staticmethod
    def _read(proc, frames):
        """Turn Piper's byte stream into ("rate", n), ("data", bytes),
        ("end", None) and finally ("eof", None)."""
        def exactly(n):
            buf = b""
            while len(buf) < n:
                part = proc.stdout.read(n - len(buf))
                if not part:
                    return None
                buf += part
            return buf
        try:
            head = exactly(8)
            if head is None or head[:4] != HANDSHAKE:
                frames.put(("eof", None))
                return
            frames.put(("rate", struct.unpack("<I", head[4:])[0]))
            while True:
                size = exactly(4)
                if size is None:
                    break
                n = struct.unpack("<I", size)[0]
                if n == 0:
                    frames.put(("end", None))
                    continue
                if n > MAX_FRAME:
                    break
                data = exactly(n)
                if data is None:
                    break
                frames.put(("data", data))
        except (OSError, ValueError):
            pass
        frames.put(("eof", None))

    def _kill(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(2.0)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass

    def _synth_loop(self):
        current = self.voice
        proc = self._spawn(current)
        while True:
            job = self._jobs.get()
            if job[0] == "stop":
                return
            if job[0] == "voice":
                if job[1].id != current.id:
                    # One Piper at a time: the old one goes before the new
                    # one is started.
                    self._kill()
                    current = job[1]
                    proc = self._spawn(current)
                continue
            if job[0] == "end":
                if job[1] == self._epoch:
                    self._play.put(("end", job[1], job[2]))
                continue
            _, epoch, turn_id, text = job
            if epoch != self._epoch:
                continue
            try:
                proc.stdin.write(text.replace("\n", " ").encode("utf-8")
                                 + b"\n")
                proc.stdin.flush()
            except OSError:
                raise TtsUnavailable("Piper stopped listening")
            parts = []
            while True:
                try:
                    kind, value = self._frames.get(timeout=SYNTH_TIMEOUT)
                except queue.Empty:
                    raise TtsUnavailable("Piper stopped answering")
                if kind == "data":
                    parts.append(value)
                elif kind == "end":
                    break
                elif kind == "eof":
                    raise TtsUnavailable("Piper stopped")
            if epoch == self._epoch and parts:
                self._play.put(("pcm", epoch, turn_id, b"".join(parts),
                                self.sample_rate))

    def _play_loop(self):
        while True:
            item = self._play.get()
            if item[0] == "stop":
                return
            if item[1] != self._epoch:
                continue
            if item[0] == "end":
                self.started_speaking.discard(item[2])
                self.post(ev.Event(ev.SPEAKING_DONE,
                                   data={"turn_id": item[2]}))
                continue
            _, epoch, turn_id, pcm, rate = item
            if turn_id not in self.started_speaking:
                self.started_speaking.add(turn_id)
                self.post(ev.Event(ev.SPEAKING_STARTED,
                                   data={"turn_id": turn_id}))
            self.speaker.play(pcm, rate)
            while (self.speaker.is_playing() and epoch == self._epoch
                   and not self._closing.is_set()):
                time.sleep(POLL)


"""Real audio in and out for the emulator, on the robot's terms.

The robot's audio path is fixed by the research and the parts:

    ReSpeaker Lite (XMOS XU316, USB audio)
      -> ALSA capture, 16kHz 16-bit mono, 10ms chunks
      -> wake word (Porcupine now, micro-wake-word when units are sold)
      -> cloud STT -> model -> Piper
      -> playback back out through the ReSpeaker Lite, so its echo canceller
         has a reference of what the device is saying

This module is the same seam on a PC. `SystemInput` opens the computer's
microphone at exactly the format the robot will use -- 16kHz, mono, signed
16-bit, 160-frame chunks -- and `SystemOutput` plays PCM back through its
speakers. `firmware.hal.base.Mic` and `Speaker` are the interfaces on the
other side, and they do not change when a backend is swapped.

The format is not a detail. micro-wake-word takes 16kHz mono int16 in 10ms
chunks and nothing else; opening at a PC's native 44.1kHz stereo float32 runs
fine, detects nothing, and looks like a bad model. So everything above this
module gets 16kHz mono int16 whatever the sound card is doing.

Getting that is not always a matter of asking. Plenty of Windows devices
refuse 16kHz outright -- WASAPI shared mode in particular runs at whatever
the mixer is set to, and PortAudio answers with `Invalid sample rate
[PaErrorCode -9997]`. So the stream is opened at a rate the device will
accept and converted here, and the label says which rate it settled on.

Capture runs on PortAudio's own thread into a ring buffer, and `read` takes
whatever has arrived. Nothing blocks: the device loop draws a face at 30Hz,
and waiting even 10ms for audio on that thread is a third of the frame budget.

The ring buffer is bounded and drops the oldest audio when it overflows. A
device that has stopped reading is not helped by a growing backlog of audio it
will never catch up with, and on a 512MB board that backlog is the thing that
kills it.

## What a PC still cannot show you

The echo problem. On the robot, playback goes back out through the mic board
so the XU316 can subtract it, which is what stops the device waking itself up.
A PC has no equivalent path, so the emulator will happily hear its own output
and the real behaviour is whatever the hardware does. That test is Phase 1,
step 11, and it needs the parts.

`sounddevice` (PortAudio) is an optional dependency. Without it the system
backends refuse with an install line and the slider keeps working.
"""

import array
import collections
import math
import os
import threading

RATE = 16000
CHUNK_MS = 10
CHUNK_FRAMES = RATE * CHUNK_MS // 1000
SAMPLE_BYTES = 2

# Two seconds of 16kHz mono. Long enough that a busy frame or a slow update
# check cannot lose audio, short enough that a stalled reader does not build a
# backlog it can never use.
RING_SECONDS = 2.0

# How much of the room from before the wake word to keep at the front of an
# utterance. Some is wanted: people start the sentence before the detector
# fires, and without it the first word is clipped. All of it is not -- the
# ring holds two seconds, and prepending that delays the turn and hands STT
# audio nobody addressed to it.
PREROLL_SECONDS = 0.25

# What counts as full scale for the level meters. Speech at a normal distance
# from a desk mic peaks well below int16 max, and scaling to 32767 gives a
# meter that never moves and a listening face that never reacts.
FULL_SCALE = 8000.0

MANUAL = "manual"
SYSTEM = "system"
FILE = "file"


class NotBuilt(NotImplementedError):
    """A backend the emulator offers but cannot run here."""


def _sounddevice():
    try:
        import sounddevice
    except Exception as exc:  # ImportError, or PortAudio missing
        raise NotBuilt(
            "PC audio needs sounddevice, which is not installed or cannot "
            "find PortAudio: %s. Install it with `pip install sounddevice` "
            "(it is in tools/requirements.txt). The level slider keeps "
            "working without it." % exc)
    return sounddevice


def rms(pcm):
    """Level of signed 16-bit little-endian PCM, 0.0 to 1.0.

    `audioop` would have done this in C, and it was removed in Python 3.13.
    Over a 160-sample chunk the pure-Python version costs microseconds.
    """
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if not samples:
        return 0.0
    total = 0
    for s in samples:
        total += s * s
    return min(1.0, math.sqrt(total / len(samples)) / FULL_SCALE)


def gain(pcm, factor):
    """Scale PCM by `factor`, clipping rather than wrapping.

    Wrapping is what happens when int16 overflows, and it does not sound like
    loud audio -- it sounds like a broken speaker.
    """
    if 0.999 <= factor <= 1.001 or not pcm:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    for i, value in enumerate(samples):
        samples[i] = max(-32768, min(32767, int(value * factor)))
    return samples.tobytes()


def peak(pcm):
    """Loudest sample, 0.0 to 1.0. Peak rather than RMS, because the question
    a quiet input asks is "is anything arriving at all"."""
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if not samples:
        return 0.0
    return max(abs(v) for v in samples) / 32768.0


def devices(kind):
    """[(index, name)] for "input" or "output". Empty when PortAudio is not
    installed, which the window renders as "no devices found"."""
    try:
        sd = _sounddevice()
    except NotBuilt:
        return []
    key = "max_%s_channels" % kind
    out = []
    try:
        for index, info in enumerate(sd.query_devices()):
            if info.get(key, 0) > 0:
                out.append((index, info["name"]))
    except Exception:
        return []
    return out


def tone(seconds=0.5, hz=440.0, level=0.25, rate=RATE):
    """A sine, for proving the output path without involving the input."""
    count = int(rate * seconds)
    samples = array.array("h", (
        int(level * 32767 * math.sin(2.0 * math.pi * hz * i / rate))
        for i in range(count)))
    # Fade the ends, or the discontinuity clicks.
    edge = max(1, int(rate * 0.01))
    for i in range(edge):
        factor = i / edge
        samples[i] = int(samples[i] * factor)
        samples[-1 - i] = int(samples[-1 - i] * factor)
    return samples.tobytes()


# ---------------------------------------------------------------- inputs


class Input:
    name = "input"
    label = "input"
    # True when the samples are a real room rather than a stand-in. It
    # decides whether the device gets end-of-utterance detection and a
    # playback reply, so a test can set it without opening a sound card.
    live = False

    def start(self):
        pass

    def stop(self):
        pass

    def read(self, frames=CHUNK_FRAMES):
        """16-bit mono little-endian PCM, at most `frames` of it."""
        raise NotImplementedError

    def level(self):
        raise NotImplementedError

    def detail(self):
        return ""


class ManualInput(Input):
    """The slider. No audio hardware is touched."""

    name = MANUAL
    label = "slider — no audio hardware"

    def __init__(self):
        self._level = 0.0

    def set_level(self, value):
        self._level = max(0.0, min(1.0, float(value)))

    def read(self, frames=CHUNK_FRAMES):
        return b"\x00\x00" * int(frames)

    def level(self):
        return self._level


class SystemInput(Input):
    """This computer's microphone, opened at the robot's format."""

    name = SYSTEM
    label = "this computer's microphone"
    live = True

    # Desk microphones differ by more than 20dB, and a headset boom mic in a
    # quiet room reads near zero at the gain a laptop's array needs. Without a
    # trim the listening face never moves and it looks like a broken meter.
    DEFAULT_GAIN = 8.0

    def __init__(self, device=None):
        self.device = device
        self.gain = self.DEFAULT_GAIN
        self.native_rate = RATE
        self.channels = 1
        self._peak = 0.0
        self._quiet_for = 0
        self._stream = None
        self._ring = collections.deque(
            maxlen=int(RATE * RING_SECONDS / CHUNK_FRAMES))
        self._lock = threading.Lock()
        self._level = 0.0
        self._dropped = 0
        self._error = ""

    def start(self):
        sd = _sounddevice()
        last = None
        for rate, channels in _candidates(sd, self.device, "input"):
            try:
                stream = sd.RawInputStream(
                    samplerate=rate, channels=channels, dtype="int16",
                    blocksize=int(rate * CHUNK_MS / 1000),
                    device=self.device, callback=self._on_audio)
                stream.start()
            except Exception as exc:
                last = exc
                continue
            self._stream = stream
            self.native_rate = int(rate)
            self.channels = channels
            self.label = "this computer's microphone%s" % (
                "" if rate == RATE and channels == 1
                else " (%g kHz%s, converted)" % (
                    rate / 1000.0, " stereo" if channels > 1 else ""))
            return

        self._stream = None
        raise NotBuilt(
            "cannot open this microphone at any usable rate: %s. Pick a "
            "different input, or use the wav-file input instead." % last)

    def _on_audio(self, indata, frames, time_info, status):
        """PortAudio's thread. Copy and leave: anything slow here shows up as
        dropped input, and the level is wanted by the UI thread anyway."""
        if status:
            self._error = str(status)
        chunk = _to_mono_16k(bytes(indata), self.native_rate, self.channels)
        loud = peak(chunk)
        with self._lock:
            if len(self._ring) == self._ring.maxlen:
                self._dropped += 1
            self._ring.append(chunk)
            self._level = rms(chunk)
            self._peak = max(self._peak * 0.995, loud)
            # Counted before the trim: this is about whether the sound card
            # is delivering anything, not about how loud it ends up.
            self._quiet_for = (self._quiet_for + 1) if loud < 0.002 else 0

    def read(self, frames=CHUNK_FRAMES):
        want = int(frames) * SAMPLE_BYTES
        out = bytearray()
        with self._lock:
            while self._ring and len(out) < want:
                out += self._ring.popleft()
        # The trim applies to the samples, not only to the meter. Anything
        # else and the meter says one thing while the recording -- and the
        # playback made from it -- is another.
        return gain(bytes(out[:want]), self.gain)

    def level(self):
        with self._lock:
            return min(1.0, self._level * self.gain)

    @property
    def silent(self):
        """Roughly two seconds of a sound card delivering nothing."""
        with self._lock:
            return self._quiet_for > 200

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        self._stream = None

    def detail(self):
        if self.silent:
            return ("nothing is arriving from this device (peak %.1f%% of "
                    "full scale) — wrong input, or its Windows level is at "
                    "zero" % (self._peak * 100.0))
        if self._dropped:
            return "%d chunks dropped" % self._dropped
        return self._error


class FileInput(Input):
    """A wav file on a loop, in place of a microphone.

    The point is repeatability: the same utterance every run, so a wake-word
    change is measurable rather than a matter of saying it again. It is also
    the answer when the machine has no working microphone -- the emulator
    still exercises the whole path.

    With no file it synthesises one, so there is always something to hear.
    """

    name = FILE
    label = "a wav file, on a loop"
    live = True

    def __init__(self, path=None, gap_seconds=1.2):
        self.path = path
        self.gap_seconds = gap_seconds
        self.gain = 1.0
        self._pcm = b""
        self._at = 0

    def start(self):
        self._pcm = (_read_wav(self.path) if self.path
                     else synthetic_utterance())
        self._pcm += b"\x00\x00" * int(RATE * self.gap_seconds)
        self._at = 0
        self.label = ("%s, on a loop" % os.path.basename(self.path)
                      if self.path else "a synthesised utterance, on a loop")

    def read(self, frames=CHUNK_FRAMES):
        """Hand out at most one frame's worth at a time.

        A file can produce unlimited audio on demand, and a reader that
        drains as fast as it likes would race through the loop. Real audio
        arrives at one second per second, so this does too.
        """
        want = min(int(frames), CHUNK_FRAMES * 4) * SAMPLE_BYTES
        if not self._pcm:
            return b""
        out = bytearray()
        while len(out) < want:
            chunk = self._pcm[self._at:self._at + want - len(out)]
            if not chunk:
                self._at = 0
                continue
            out += chunk
            self._at += len(chunk)
        return gain(bytes(out), self.gain)

    def level(self):
        window = self._pcm[max(0, self._at - CHUNK_FRAMES * SAMPLE_BYTES):
                           self._at]
        return min(1.0, rms(window) * self.gain)


def synthetic_utterance(seconds=1.6, rate=RATE):
    """Something that moves like speech: a carrier chopped into syllables.

    Not words, and not trying to be. It has the gaps and the peaks that make
    end-of-utterance detection and the talking face behave the way they will
    with a voice, and unlike a microphone it is there on every machine.
    """
    count = int(rate * seconds)
    samples = array.array("h", bytes(2 * count))
    for i in range(count):
        t = i / rate
        syllable = abs(math.sin(2.0 * math.pi * 3.1 * t))
        carrier = (math.sin(2.0 * math.pi * 170.0 * t)
                   + 0.5 * math.sin(2.0 * math.pi * 340.0 * t)
                   + 0.25 * math.sin(2.0 * math.pi * 900.0 * t))
        samples[i] = int(max(-1.0, min(1.0, carrier / 1.75))
                         * syllable * 0.4 * 32767)
    return samples.tobytes()


def _read_wav(path):
    """Any 16-bit wav, as 16kHz mono int16.

    Done by hand because `audioop`, which did exactly this in C, was removed
    in Python 3.13. A few seconds of audio converted once at startup does not
    need to be fast.
    """
    import wave

    try:
        with wave.open(path, "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            raw = wav.readframes(wav.getnframes())
    except (OSError, wave.Error) as exc:
        raise NotBuilt("cannot read %s: %s" % (path, exc))

    if width != 2:
        raise NotBuilt(
            "%s is %d-bit; this wants 16-bit PCM. Convert it with "
            "`ffmpeg -i in.wav -acodec pcm_s16le -ar 16000 -ac 1 out.wav`."
            % (path, width * 8))

    samples = array.array("h")
    samples.frombytes(raw[:len(raw) - len(raw) % 2])

    if channels > 1:
        samples = array.array("h", [
            sum(samples[i:i + channels]) // channels
            for i in range(0, len(samples) - channels + 1, channels)])

    if rate != RATE:
        samples = _resample(samples, rate, RATE)
    return samples.tobytes()


def _resample(samples, src, dst):
    """Linear interpolation, no anti-aliasing filter.

    Good enough for feeding a test utterance in. It is not good enough to
    judge wake-word accuracy from -- for that, record at 16kHz to begin with
    and skip this path entirely.
    """
    if src == dst or not samples:
        return samples
    ratio = float(src) / dst
    count = int(len(samples) / ratio)
    out = array.array("h", bytes(2 * count))
    last = len(samples) - 1
    for i in range(count):
        position = i * ratio
        index = int(position)
        frac = position - index
        a = samples[index]
        b = samples[index + 1] if index < last else a
        out[i] = int(a + (b - a) * frac)
    return out


# --------------------------------------------------------------- outputs


class Output:
    name = "output"
    label = "output"

    def play(self, pcm, rate=RATE):
        raise NotImplementedError

    def stop(self):
        pass

    def level(self):
        return 0.0

    def is_playing(self):
        return False

    def detail(self):
        return ""


class SilentOutput(Output):
    """Nothing leaves the machine. `VirtualSpeaker` still models how long the
    audio would have taken, so the talking face lasts the right time."""

    name = MANUAL
    label = "silent — length only"

    def __init__(self):
        self.bytes_played = 0

    def play(self, pcm, rate=RATE):
        self.bytes_played += len(pcm)


class SystemOutput(Output):
    """This computer's speakers.

    Playback runs on its own thread, writing 10ms at a time and publishing the
    amplitude of each chunk as it goes. That amplitude is what the talking
    face animates from, so the mouth is driven by the audio actually leaving
    the machine rather than by a synthetic envelope -- the same relationship
    it will have to Piper's output on the robot.
    """

    name = SYSTEM
    label = "this computer's speakers"

    def __init__(self, device=None):
        self.device = device
        self.native_rate = None
        self._thread = None
        self._stop = threading.Event()
        self._level = 0.0
        self._volume = 1.0
        self._error = ""

    def set_volume(self, value):
        self._volume = max(0.0, min(1.0, value))

    def play(self, pcm, rate=RATE):
        sd = _sounddevice()
        self.stop()
        if not pcm:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(sd, gain(pcm, self._volume), rate),
            daemon=True)
        self._thread.start()

    def _run(self, sd, pcm, rate):
        """Open at a rate the card accepts, resampling if it is not ours.

        Same problem as capture: a device that will not do 16kHz is common,
        and the answer is to convert rather than to give up.
        """
        try:
            stream, opened = _open_output(sd, self.device, rate)
        except Exception as exc:
            self._error = "cannot open the speakers: %s" % exc
            return

        if opened != rate:
            samples = array.array("h")
            samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
            pcm = _resample(samples, rate, opened).tobytes()
            self.native_rate = opened

        block = max(1, opened // 100) * SAMPLE_BYTES  # 10ms
        try:
            with stream as out:
                for i in range(0, len(pcm), block):
                    if self._stop.is_set():
                        break
                    chunk = pcm[i:i + block]
                    self._level = rms(chunk)
                    out.write(chunk)
        except Exception as exc:
            self._error = str(exc)
        finally:
            self._level = 0.0

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.5)
        self._thread = None
        self._level = 0.0

    def is_playing(self):
        return self._thread is not None and self._thread.is_alive()

    def level(self):
        return self._level

    def detail(self):
        return self._error


INPUTS = (ManualInput, SystemInput, FileInput)
OUTPUTS = (SilentOutput, SystemOutput)

BUILT = {ManualInput, SystemInput, FileInput, SilentOutput, SystemOutput}


class AudioBridge:
    """Holds the chosen input and output, and hands the emulated mic and
    speaker their levels once a frame.

    Nothing here reaches into the firmware. It drives `VirtualMic` and
    `VirtualSpeaker`, which are what the firmware already talks to, so the
    device cannot tell a real microphone from the slider -- which is the
    point of testing against it.
    """

    def __init__(self, mic, speaker):
        self.mic = mic
        self.speaker = speaker
        self.manual = ManualInput()
        self.input = self.manual
        self.output = SilentOutput()
        self.frames_captured = 0

        # What the mic heard during this turn, for the parrot reply below.
        self.recording = bytearray()
        self._recording = False
        # Audio pumped out of the ring and not yet read by the firmware. Held
        # for it rather than drained twice, and capped: a device that has
        # stopped reading gains nothing from a growing backlog.
        self._pending = bytearray()

        self.mic.attach(self)
        self.speaker.attach(self)

    # ---- choosing ----

    def select_input(self, name, device=None, path=None):
        chosen = _find(INPUTS, name)
        if chosen is ManualInput:
            instance = self.manual
        elif chosen is SystemInput:
            instance = chosen(device=device)
        else:
            instance = chosen(path=path)
        instance.start()  # raises NotBuilt before anything is swapped in
        if self.input is not instance:
            self.input.stop()
        self.input = instance
        return instance

    def select_output(self, name, device=None):
        chosen = _find(OUTPUTS, name)
        if chosen is SilentOutput:
            instance = SilentOutput()
        else:
            instance = chosen(device=device)
            _sounddevice()  # refuse now, not at the first spoken reply
        self.output.stop()
        self.output = instance
        return instance

    def set_input_gain(self, value):
        """Only means anything to a real microphone; the slider is the level
        itself when there is not one."""
        if isinstance(self.input, SystemInput):
            self.input.gain = max(0.1, float(value))

    @property
    def live_input(self):
        """True when real audio is coming in, which is what decides whether
        the device gets a scripted envelope or the room."""
        return self.input.live

    # ---- capture ----

    def start_recording(self):
        self._drain()
        keep = int(RATE * PREROLL_SECONDS) * SAMPLE_BYTES
        self.recording = bytearray(self._pending[-keep:])
        del self._pending[:]
        self._recording = True

    def _drain(self):
        """Empty the backend's ring into the pending buffer.

        Bounded by what the ring can hold. An input that always has more to
        give would otherwise spin here forever, on the thread that draws the
        face -- and "always has more" is exactly what a backend looks like
        when its own buffering is wrong.
        """
        for _ in range(int(RING_SECONDS * RATE / (CHUNK_FRAMES * 16)) + 2):
            pcm = self.input.read(CHUNK_FRAMES * 16)
            if not pcm:
                break
            self._pending += pcm

    def stop_recording(self):
        self._recording = False
        return bytes(self.recording)

    def pump(self):
        """Once a frame: move audio and level from the backend into the mic
        the firmware reads.

        The mute switch cuts the ReSpeaker's power on the robot, so a muted
        emulator must produce nothing at all -- not silence that still counts
        as a working device.
        """
        if not self.mic.powered:
            return
        # Take everything waiting, not a fixed 40ms. A frame that ran long
        # -- a redraw, an update check -- otherwise leaves a backlog the next
        # frames never catch up with, and the ring drops the oldest audio.
        pcm = self.input.read(int(RATE * RING_SECONDS))
        if not pcm:
            return
        self.frames_captured += len(pcm) // SAMPLE_BYTES
        if self._recording:
            self.recording += pcm
        self._pending += pcm
        overflow = len(self._pending) - int(RATE * SAMPLE_BYTES)
        if overflow > 0:
            del self._pending[:overflow]

    def take(self, frames):
        """What `VirtualMic.read` hands the firmware."""
        want = int(frames) * SAMPLE_BYTES
        out = bytes(self._pending[:want])
        del self._pending[:want]
        return out

    def test_tone(self):
        """Play a tone straight out, bypassing the device entirely.

        The first question when nothing is audible is which half is broken.
        This answers it: if the tone plays, the output is fine and the
        problem is upstream.
        """
        pcm = tone()
        if isinstance(self.output, SystemOutput):
            self.output.set_volume(self.speaker.get_volume())
        self.output.play(pcm, RATE)
        return len(pcm) / float(RATE * SAMPLE_BYTES)

    def play(self, pcm, rate=RATE):
        """Called by `VirtualSpeaker`, which the firmware called."""
        if isinstance(self.output, SystemOutput):
            self.output.set_volume(self.speaker.get_volume())
        self.output.play(pcm, rate)

    def stop(self):
        self.output.stop()

    # ---- what the firmware sees ----

    def mic_level(self):
        return self.input.level() if self.mic.powered else 0.0

    def speaker_level(self):
        """Real playback amplitude when audio is leaving the machine, and the
        pipeline's synthetic envelope when it is not."""
        if isinstance(self.output, SystemOutput):
            return self.output.level()
        return self.speaker.synthetic_level()

    def status(self):
        return {
            "input": self.input.label,
            "output": self.output.label,
            "rate": RATE,
            "chunk_frames": CHUNK_FRAMES,
            "captured_seconds": self.frames_captured / float(RATE),
            "recorded_seconds": len(self.recording) / float(RATE * 2),
            "mic_level": self.mic_level(),
            "speaker_level": self.speaker_level(),
            "playing": self.output.is_playing() or self.speaker.is_playing(),
            "live_input": self.live_input,
            "detail": self.input.detail() or self.output.detail(),
            "gain": getattr(self.input, "gain", None),
            "silent_input": bool(getattr(self.input, "silent", False)),
        }

    def close(self):
        self.input.stop()
        self.output.stop()


def _candidates(sd, device, kind):
    """Rates and channel counts to try, best first.

    Ours first, because a card that will do 16kHz mono needs no conversion
    and no guessing. Then the device's own default, which is the one it is
    guaranteed to accept, then the usual suspects.
    """
    native = None
    try:
        info = sd.query_devices(device if device is not None else None, kind)
        native = int(info.get("default_samplerate") or 0) or None
        max_channels = int(info.get("max_%s_channels" % kind, 1)) or 1
    except Exception:
        max_channels = 2

    rates = [RATE]
    if native:
        rates.append(native)
    rates += [48000, 44100, 32000, 22050]

    out = []
    for rate in rates:
        for channels in (1, 2):
            if channels <= max_channels and (rate, channels) not in out:
                out.append((rate, channels))
    return out


def _open_output(sd, device, rate):
    """(stream, rate it actually opened at)."""
    last = None
    for candidate in [rate, None, 48000, 44100]:
        if candidate is None:
            try:
                info = sd.query_devices(
                    device if device is not None else None, "output")
                candidate = int(info.get("default_samplerate") or 0)
            except Exception:
                continue
        if not candidate:
            continue
        try:
            return (sd.RawOutputStream(samplerate=candidate, channels=1,
                                       dtype="int16", device=device),
                    candidate)
        except Exception as exc:
            last = exc
    raise last or RuntimeError("no usable output rate")


def _to_mono_16k(pcm, rate, channels):
    """Whatever the card gave us, as 16kHz mono int16.

    Cheap on purpose: this runs on PortAudio's callback thread, where
    anything slow shows up as dropped input rather than as a slow function.
    A 10ms block is a few hundred samples.
    """
    if rate == RATE and channels == 1:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if channels > 1:
        samples = array.array("h", [
            sum(samples[i:i + channels]) // channels
            for i in range(0, len(samples) - channels + 1, channels)])
    if rate != RATE:
        samples = _resample(samples, rate, RATE)
    return samples.tobytes()


def _find(options, name):
    for option in options:
        if option.name == name:
            return option
    raise KeyError(name)

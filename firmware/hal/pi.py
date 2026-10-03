"""The real board: Pi 3 A+, ILI9341 over SPI, ReSpeaker Lite over USB audio.

Every driver import happens inside the function that needs it. Importing this
module on a PC has to work -- `firmware.hal` asks it whether it can run before
falling back, and the emulator imports the package too.

Nothing here is verified against hardware yet; the parts are not built. The
shape is fixed by the research (`docs/research/software-stack.md`) and the pin
assignment by the wiring, and the emulator exercises the layer above it. Treat
the bodies as the first thing to test in Phase 1, not as working code.
"""

import shutil
import subprocess
import time

from . import base
from .base import BUTTONS, POWER, VOL_DOWN, VOL_UP, Event

# BCM numbering, per the wiring in docs/research/parts-and-pricing.md.
PINS = {POWER: 17, VOL_DOWN: 27, VOL_UP: 22}

SPI_HZ = 32_000_000


def available():
    """True on the device. The model string is the honest test -- a PC can
    have RPi.GPIO installed, and a Pi 5 is not this board."""
    try:
        with open("/proc/device-tree/model", "r", encoding="utf-8") as fh:
            return "Raspberry Pi" in fh.read()
    except OSError:
        return False


class PiPanel(base.Panel):
    """ILI9341, rotated to landscape.

    The panel is 240 x 320 native and sits on its side in the shell, so
    rotation 90 and a 320 x 240 canvas. Getting this wrong draws a correct face
    in the wrong orientation, which looks like a broken renderer.
    """

    def __init__(self, rotation=90, spi_hz=SPI_HZ):
        import board
        import digitalio
        from adafruit_rgb_display import ili9341

        spi = board.SPI()
        self._display = ili9341.ILI9341(
            spi,
            cs=digitalio.DigitalInOut(board.CE0),
            dc=digitalio.DigitalInOut(board.D25),
            rst=digitalio.DigitalInOut(board.D24),
            baudrate=spi_hz,
            rotation=rotation,
        )
        self._backlight = digitalio.DigitalInOut(board.D18)
        self._backlight.switch_to_output(value=True)

    def push(self, image, bbox):
        if bbox is None:
            return
        x0, y0, x1, y1 = bbox
        # The library takes the window and the crop separately; sending the
        # whole canvas every frame is 154KB and would halve the frame rate.
        self._display.image(image.crop(bbox), x=x0, y=y0)

    def backlight(self, level):
        # No PWM on this pin in the current wiring, so brightness is on or off.
        # Dimming the face is done by the renderer instead.
        self._backlight.value = level > 0.0

    def close(self):
        self.backlight(0.0)


class PiMic(base.Mic):
    """ALSA capture from the ReSpeaker Lite.

    The mute switch cuts the board's 5V, so the USB device disappears. Reopen
    on every read that finds it gone rather than holding a dead handle.
    """

    def __init__(self, device="plughw:CARD=Lite,DEV=0", chunk=160):
        self.device = device
        self.chunk = chunk
        self._stream = None
        self._level = 0.0

    def _open(self):
        if self._stream is not None:
            return self._stream
        try:
            import alsaaudio
        except ImportError:
            return None
        try:
            self._stream = alsaaudio.PCM(
                alsaaudio.PCM_CAPTURE, alsaaudio.PCM_NONBLOCK,
                device=self.device, channels=1, rate=self.rate,
                format=alsaaudio.PCM_FORMAT_S16_LE, periodsize=self.chunk)
        except Exception:
            self._stream = None
        return self._stream

    def available(self):
        return self._open() is not None

    def read(self, frames):
        stream = self._open()
        if stream is None:
            return b""
        try:
            _, data = stream.read()
        except Exception:
            # Disconnected mid-stream: drop the handle so the next call reopens.
            self._stream = None
            return b""
        if data:
            self._level = _rms16(data)
        return data

    def level(self):
        return self._level


class PiSpeaker(base.Speaker):
    """Playback through the ReSpeaker Lite's output.

    Routing playback through the mic board rather than the Pi's own audio is
    what gives the XU316's echo canceller a reference signal. Send it anywhere
    else and the AEC silently does nothing -- see the echo test in Phase 1.
    """

    def __init__(self, device="plughw:CARD=Lite,DEV=0"):
        self.device = device
        self._proc = None
        self._volume = 0.6
        self._level = 0.0

    def play(self, pcm, rate=22050):
        self.stop()
        self._proc = subprocess.Popen(
            ["aplay", "-q", "-D", self.device, "-f", "S16_LE",
             "-r", str(rate), "-c", "1", "-"],
            stdin=subprocess.PIPE)
        self._proc.stdin.write(pcm)
        self._proc.stdin.close()

    def stop(self):
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None
        self._level = 0.0

    def is_playing(self):
        return self._proc is not None and self._proc.poll() is None

    def level(self):
        return self._level if self.is_playing() else 0.0

    def get_volume(self):
        return self._volume

    def set_volume(self, value):
        self._volume = max(0.0, min(1.0, value))
        subprocess.run(
            ["amixer", "-q", "sset", "PCM", "%d%%" % int(self._volume * 100)],
            check=False)


class PiSystem(base.System):
    def free_bytes(self, path):
        return shutil.disk_usage(path).free

    def total_bytes(self, path):
        return shutil.disk_usage(path).total

    def online(self):
        try:
            out = subprocess.run(["nmcli", "-t", "-f", "STATE", "general"],
                                 capture_output=True, text=True, timeout=4)
            return out.stdout.strip().startswith("connected")
        except (OSError, subprocess.SubprocessError):
            return False

    def metered(self):
        try:
            out = subprocess.run(["nmcli", "-t", "-f", "METERED", "general"],
                                 capture_output=True, text=True, timeout=4)
            return out.stdout.strip().startswith("yes")
        except (OSError, subprocess.SubprocessError):
            return False

    def undervoltage(self):
        try:
            out = subprocess.run(["vcgencmd", "get_throttled"],
                                 capture_output=True, text=True, timeout=4)
            flags = int(out.stdout.strip().split("=")[1], 16)
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return False
        # Bit 0: under-voltage right now. Bit 16: it happened since boot.
        return bool(flags & 0x1) or bool(flags & 0x10000)

    def temperature_c(self):
        try:
            with open("/sys/class/thermal/thermal_zone0/temp",
                      encoding="utf-8") as fh:
                return int(fh.read().strip()) / 1000.0
        except (OSError, ValueError):
            return None

    def uptime(self):
        try:
            with open("/proc/uptime", encoding="utf-8") as fh:
                return float(fh.read().split()[0])
        except (OSError, ValueError):
            return time.monotonic()

    def reboot(self):
        subprocess.run(["systemctl", "reboot"], check=False)

    def shutdown(self):
        subprocess.run(["systemctl", "poweroff"], check=False)


class PiHAL(base.HAL):
    name = "pi"

    def __init__(self):
        super().__init__(PiPanel(), PiMic(), PiSpeaker(), PiSystem(),
                         mute_is_hardware=True)
        import RPi.GPIO as GPIO

        self._gpio = GPIO
        GPIO.setmode(GPIO.BCM)
        self._state = {}
        for name, pin in PINS.items():
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self._state[name] = False

    def poll(self):
        """Read the pins and report edges. Buttons are active-low with a
        pull-up, so a press reads 0.

        Polling rather than GPIO edge callbacks: the loop already runs at 30Hz
        for the face, a 33ms sample is its own debounce, and callbacks would
        deliver button events on a second thread for no gain."""
        now = time.monotonic()
        events = []
        for name in BUTTONS:
            pressed = not self._gpio.input(PINS[name])
            if pressed != self._state[name]:
                self._state[name] = pressed
                events.append(Event(name, "press" if pressed else "release",
                                    now))
        return events

    def close(self):
        super().close()
        self.speaker.stop()
        self._gpio.cleanup()


def _rms16(pcm):
    """Rough level from signed 16-bit little-endian PCM, 0.0 to 1.0."""
    import array

    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if not samples:
        return 0.0
    total = sum(s * s for s in samples)
    return min(1.0, (total / len(samples)) ** 0.5 / 8000.0)


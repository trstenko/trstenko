"""The emulator window: a Trstenko on your desk, made of nothing.

    python -m tools.emulator

Runs the real firmware -- `firmware.device.Device`, the real face, the real
update manager -- against emulated hardware, and puts the controls that only
exist on the physical device (three buttons, a mute switch, an SD card that
can fill up, a network that can drop) where you can reach them.

Audio can be real: point the input at this computer's microphone and the
output at its speakers, and a turn records what you say and plays it back.
That is a parrot standing in for TTS, and it exercises the whole path at the
robot's own format -- 16kHz mono, 160-frame chunks, through the same HAL the
Pi will use.

The update page the device serves is served here too, at http://127.0.0.1:8080.
It is the same page, the same endpoints and the same manager the Pi runs.
"""

import argparse
import time
import tkinter as tk
import webbrowser
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

from firmware.device import Device
from firmware.hal.base import POWER, VOL_DOWN, VOL_UP
from firmware.updates import GitHubReleases, UpdateConfig, UpdateManager
from firmware.webui import Auth, UpdateApi, serve

from . import audio as audio_mod
from .fake_releases import FakeReleases
from .hal import MB, EmulatedHAL
from .pipeline import ScriptedPipeline

SCALE = 2
FPS = 30

BG = "#0b0f12"
SURFACE = "#151d22"
LINE = "#243137"
INK = "#c8d4d8"
DIM = "#6b7d85"
TEAL = "#1b7f9b"
AMBER = "#9b6c1b"

MONO = ("Consolas", 9)
MONO_BOLD = ("Consolas", 9, "bold")


class Emulator(tk.Tk):
    def __init__(self, args):
        super().__init__()
        self.title("Trstenko — emulator")
        self.configure(bg=BG)

        self.hal = EmulatedHAL()
        self.hal.system.free_mb = args.free_mb
        self.wav_path = args.wav
        self.audio = audio_mod.AudioBridge(self.hal.mic, self.hal.speaker)
        self.pipeline = ScriptedPipeline(self.hal.speaker, seed=args.seed,
                                         bridge=self.audio)

        self.releases = FakeReleases(version=args.release,
                                     urgency=args.urgency,
                                     size_mb=args.size_mb,
                                     kbps=args.kbps,
                                     real_bundle=True)
        source = self.releases
        if args.repo:
            source = GitHubReleases(args.repo)

        # Trust the emulated feed's throwaway key, so signatures are
        # required here even though the shipped key list is still empty.
        config = UpdateConfig(repo=args.repo or "majcek210/trstenko (emulated)",
                              data_root=args.data_root,
                              install_root=args.data_root,
                              extra_keys=([self.releases.public_key]
                                          if not args.repo else []),
                              check_interval_hours=args.check_hours,
                              auto_check=False,
                              urgent_grace_seconds=args.grace)
        self.config = config
        self.updater = UpdateManager(config, source, self.hal.system,
                                     on_event=self._update_event,
                                     on_restart=self._simulate_restart)
        self.restarts = 0
        self.pending_restart = False
        self._last_log = None
        self._last_log_count = 0

        self.device = Device(self.hal, self.pipeline, self.updater,
                             on_log=self.log, on_setup=self._setup)

        # Locked like the real one. The device shows its PIN on the panel
        # next to the setup QR code; here the log is the panel.
        self.auth = Auth(args.data_root, log=self.log)
        self.api = UpdateApi(self.updater, self.device, self.auth)
        self.server = serve(self.api, host="127.0.0.1", port=args.port)
        self.url = "http://127.0.0.1:%d/" % args.port

        self.show_regions = tk.BooleanVar(value=True)
        self.muted = tk.BooleanVar(value=False)
        self.online = tk.BooleanVar(value=True)
        self.metered = tk.BooleanVar(value=False)
        self.undervolt = tk.BooleanVar(value=False)
        self.free_mb = tk.IntVar(value=args.free_mb)
        self.urgency = tk.StringVar(value=args.urgency)
        self.release_version = tk.StringVar(value=args.release)
        self.size_mb = tk.DoubleVar(value=args.size_mb)
        self.corrupt = tk.BooleanVar(value=False)
        self.drop = tk.BooleanVar(value=False)
        self.broken_release = tk.BooleanVar(value=False)
        self.tamper = tk.BooleanVar(value=False)
        self.auto_restart = tk.BooleanVar(value=True)
        self.fail_turn = tk.BooleanVar(value=False)
        self.audio_in = tk.StringVar(value=audio_mod.MANUAL)
        self.audio_out = tk.StringVar(value=audio_mod.MANUAL)
        self.mic_gain = tk.DoubleVar(value=0.0)
        self.in_device = tk.StringVar(value="default")
        self.out_device = tk.StringVar(value="default")

        self._build()
        self._keys()
        self.protocol("WM_DELETE_WINDOW", self.quit_all)

        self.log("emulator up — update page at %s" % self.url)
        self.log("pairing PIN for the page: %s" % self.auth.pin)
        self._tick()

    # ---- layout ----

    def _build(self):
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(padx=12, pady=12, fill="both", expand=True)

        left = tk.Frame(wrap, bg=BG)
        left.grid(row=0, column=0, sticky="n")

        self.canvas = tk.Canvas(left, width=320 * SCALE, height=240 * SCALE,
                                highlightthickness=1, highlightbackground=LINE,
                                bd=0)
        self.canvas.pack()
        self.image_id = self.canvas.create_image(0, 0, anchor="nw")

        self.stats = tk.Label(left, text="", bg=BG, fg=DIM, justify="left",
                              anchor="w", font=MONO)
        self.stats.pack(anchor="w", pady=(8, 0))

        # Two columns of controls rather than one. The audio and release
        # sections grew past the height of a 1080p screen, and a window that
        # does not fit is a window with hidden buttons.
        right = tk.Frame(wrap, bg=BG)
        right.grid(row=0, column=1, sticky="n", padx=(14, 0))

        column_a = tk.Frame(right, bg=BG)
        column_a.grid(row=0, column=0, sticky="n")
        column_b = tk.Frame(right, bg=BG)
        column_b.grid(row=0, column=1, sticky="n", padx=(16, 0))

        self._top_face(column_a)
        self._conversation(column_a)
        self._audio(column_a)
        self._machine(column_b)
        self._updates(column_b)

        # The log lives under the panel rather than across the bottom: the
        # left column has 300px of slack and the right one has none, and the
        # window has to fit on a 1080p laptop.
        self.logbox = tk.Text(left, height=13, width=1, bg=SURFACE, fg=INK,
                              bd=0, font=MONO, insertbackground=INK,
                              highlightthickness=1, highlightbackground=LINE,
                              wrap="word")
        self.logbox.pack(fill="both", expand=True, pady=(10, 0))
        self.logbox.configure(state="disabled")
        left.grid(sticky="nsew")
        wrap.grid_rowconfigure(0, weight=1)

    def _section(self, parent, title):
        tk.Label(parent, text=title, bg=BG, fg=DIM, font=MONO_BOLD).pack(
            anchor="w", pady=(12, 4))
        frame = tk.Frame(parent, bg=BG)
        frame.pack(anchor="w", fill="x")
        return frame

    def _top_face(self, parent):
        frame = self._section(parent, "TOP FACE")
        row = tk.Frame(frame, bg=BG)
        row.pack(anchor="w")
        for label, name in (("power", POWER), ("vol −", VOL_DOWN),
                            ("vol +", VOL_UP)):
            b = tk.Button(row, text=label, width=8, relief="flat", bd=0,
                          bg=SURFACE, fg=INK, activebackground=TEAL,
                          activeforeground="#fff", font=MONO)
            # Press and release are separate events, because a long press and
            # the two-button setup gesture are both timing, not clicks.
            b.bind("<ButtonPress-1>", lambda e, n=name: self.hal.press(n))
            b.bind("<ButtonRelease-1>", lambda e, n=name: self.hal.release(n))
            b.pack(side="left", padx=(0, 6))

        self._check(frame, "mute switch — cuts the mic's power",
                    self.muted, self._apply_mute)
        tk.Label(frame, text="hold vol − and vol + together for 3s: setup",
                 bg=BG, fg=DIM, font=MONO).pack(anchor="w")

    def _conversation(self, parent):
        frame = self._section(parent, "CONVERSATION")
        tk.Button(frame, text="W   say the wake word", anchor="w", width=34,
                  relief="flat", bd=0, bg=TEAL, fg="#fff", font=MONO_BOLD,
                  command=self.wake).pack(anchor="w")
        self._check(frame, "make the next turn fail", self.fail_turn,
                    self._apply_fail)

    def _audio(self, parent):
        """Mic in, speakers out, at the format the robot will use. Picking a
        backend that cannot run here says why and leaves the slider in
        charge, rather than looking selected and doing nothing."""
        frame = self._section(parent, "AUDIO")

        tk.Label(frame, text="in", bg=BG, fg=DIM, font=MONO).pack(anchor="w")
        for backend in audio_mod.INPUTS:
            tk.Radiobutton(frame, text=_backend_label(backend),
                           value=backend.name, variable=self.audio_in,
                           bg=BG, fg=INK, selectcolor=SURFACE,
                           activebackground=BG, activeforeground=INK,
                           font=MONO, command=self._apply_audio_in,
                           anchor="w").pack(anchor="w")

        self.in_picker = self._device_picker(
            frame, self.in_device, "input", self._apply_audio_in)

        row = tk.Frame(frame, bg=BG)
        row.pack(anchor="w", fill="x")
        self.gain_label = tk.Label(row, text="level", bg=BG, fg=DIM,
                                   font=MONO, width=9, anchor="w")
        self.gain_label.pack(side="left")
        self.gain_scale = tk.Scale(
            row, from_=0.0, to=1.0, resolution=0.01, orient="horizontal",
            variable=self.mic_gain, bg=BG, fg=INK, troughcolor=SURFACE,
            highlightthickness=0, bd=0, length=240, font=("Consolas", 8))
        self.gain_scale.pack(side="left")

        self.meters = tk.Canvas(frame, width=330, height=26, bg=BG,
                                highlightthickness=0, bd=0)
        self.meters.pack(anchor="w")

        self.audio_hint = tk.Label(
            frame, text="", bg=BG, fg=DIM, justify="left", anchor="w",
            font=MONO, wraplength=330)
        self.audio_hint.pack(anchor="w")

        tk.Label(frame, text="out", bg=BG, fg=DIM, font=MONO).pack(
            anchor="w", pady=(6, 0))
        for backend in audio_mod.OUTPUTS:
            tk.Radiobutton(frame, text=_backend_label(backend),
                           value=backend.name, variable=self.audio_out,
                           bg=BG, fg=INK, selectcolor=SURFACE,
                           activebackground=BG, activeforeground=INK,
                           font=MONO, command=self._apply_audio_out,
                           anchor="w").pack(anchor="w")

        self.out_picker = self._device_picker(
            frame, self.out_device, "output", self._apply_audio_out)

        tk.Button(frame, text="T   play a test tone", anchor="w", width=34,
                  relief="flat", bd=0, bg=SURFACE, fg=INK, font=MONO,
                  command=self.test_tone).pack(anchor="w", pady=(4, 0))

        self.audio_line = tk.Label(frame, text="", bg=BG, fg=DIM,
                                   justify="left", anchor="w", font=MONO,
                                   wraplength=330)
        self.audio_line.pack(anchor="w")

    def _machine(self, parent):
        frame = self._section(parent, "MACHINE")
        row = tk.Frame(frame, bg=BG)
        row.pack(anchor="w", fill="x")
        tk.Label(row, text="free", bg=BG, fg=DIM, font=MONO).pack(side="left")
        tk.Scale(row, from_=0, to=8192, resolution=64, orient="horizontal",
                 variable=self.free_mb, bg=BG, fg=INK, troughcolor=SURFACE,
                 highlightthickness=0, bd=0, length=250, font=("Consolas", 8),
                 command=self._apply_free).pack(side="left")
        tk.Label(row, text="MB", bg=BG, fg=DIM, font=MONO).pack(side="left")

        self._check(frame, "network up", self.online, self._apply_system)
        self._check(frame, "connection is metered", self.metered,
                    self._apply_system)
        self._check(frame, "board reports under-voltage", self.undervolt,
                    self._apply_system)

    def _updates(self, parent):
        frame = self._section(parent, "RELEASE FEED")

        row = tk.Frame(frame, bg=BG)
        row.pack(anchor="w", fill="x")
        tk.Label(row, text="version", bg=BG, fg=DIM, font=MONO).pack(
            side="left")
        tk.Entry(row, textvariable=self.release_version, width=9, bd=0,
                 bg=SURFACE, fg=INK, insertbackground=INK, font=MONO).pack(
            side="left", padx=6)
        tk.Label(row, text="size", bg=BG, fg=DIM, font=MONO).pack(side="left")
        tk.Entry(row, textvariable=self.size_mb, width=6, bd=0, bg=SURFACE,
                 fg=INK, insertbackground=INK, font=MONO).pack(side="left",
                                                               padx=6)
        tk.Label(row, text="MB", bg=BG, fg=DIM, font=MONO).pack(side="left")

        urgency = tk.Frame(frame, bg=BG)
        urgency.pack(anchor="w", pady=(4, 0))
        for value in ("optional", "recommended", "urgent"):
            tk.Radiobutton(urgency, text=value, value=value,
                           variable=self.urgency, bg=BG, fg=INK,
                           selectcolor=SURFACE, activebackground=BG,
                           activeforeground=INK, font=MONO,
                           command=self._apply_release).pack(side="left")

        self._check(frame, "corrupt the download", self.corrupt,
                    self._apply_release)
        self._check(frame, "ship a release that cannot start",
                    self.broken_release, self._apply_release)
        self._check(frame, "tamper with it after signing", self.tamper,
                    self._apply_release)
        self._check(frame, "drop the connection part-way", self.drop,
                    self._apply_release)

        buttons = tk.Frame(frame, bg=BG)
        buttons.pack(anchor="w", pady=(6, 0))
        for label, command in (("check now", self.check_now),
                               ("approve", self.updater.approve),
                               ("install", self.install_now),
                               ("restart", self.restart_now),
                               ("defer", self.updater.defer),
                               ("cancel", self.updater.cancel)):
            tk.Button(buttons, text=label, relief="flat", bd=0, bg=SURFACE,
                      fg=INK, activebackground=TEAL, activeforeground="#fff",
                      font=MONO, command=command).pack(side="left",
                                                       padx=(0, 6))

        self._check(frame, "restart into it by itself", self.auto_restart,
                    lambda: None)

        tk.Button(frame, text="open the update page", anchor="w", width=34,
                  relief="flat", bd=0, bg=SURFACE, fg=INK, font=MONO,
                  command=lambda: webbrowser.open(self.url)).pack(
            anchor="w", pady=(6, 0))

        self.update_line = tk.Label(frame, text="", bg=BG, fg=INK,
                                    justify="left", anchor="w", font=MONO,
                                    wraplength=330)
        self.update_line.pack(anchor="w", pady=(6, 0))

        self._check(parent, "show the blit region and the dirty rect",
                    self.show_regions, lambda: None)

    def _device_picker(self, parent, variable, kind, command):
        """Which sound card. Without this, "I hear nothing" has no next step
        on a machine with a headset, a webcam mic and onboard audio all
        fighting to be the default."""
        names = ["default"] + ["%d: %s" % (i, n)
                               for i, n in audio_mod.devices(kind)]
        box = ttk.Combobox(parent, values=names, textvariable=variable,
                           state="readonly", width=38, font=("Consolas", 8))
        box.pack(anchor="w", pady=(2, 2))
        box.bind("<<ComboboxSelected>>", lambda e: command())
        return box

    def _check(self, parent, text, var, command):
        tk.Checkbutton(parent, text=text, variable=var, bg=BG, fg=INK,
                       selectcolor=SURFACE, activebackground=BG,
                       activeforeground=INK, font=MONO,
                       command=command).pack(anchor="w")

    def _keys(self):
        self.bind("w", lambda e: self.wake())
        self.bind("W", lambda e: self.wake())
        self.bind("m", lambda e: self._toggle(self.muted, self._apply_mute))
        self.bind("n", lambda e: self._toggle(self.online, self._apply_system))
        self.bind("u", lambda e: self.check_now())
        for k in "tT":
            self.bind(k, lambda e: self.test_tone())
        self.bind("<Escape>", lambda e: self.quit_all())

    @staticmethod
    def _toggle(var, after):
        var.set(not var.get())
        after()

    # ---- controls ----

    def wake(self):
        if self.hal.muted():
            self.log("wake word ignored: the mic is switched off")
            return
        self.pipeline.trigger()

    def check_now(self):
        self.updater.check(force=True)

    def install_now(self):
        if not self.updater.install():
            self.log("install: nothing is staged to install")

    def _simulate_restart(self):
        """`systemctl restart`, without a systemd.

        The device object is replaced the way the process would be, so the
        replacement reads the marker off disk and works out for itself that
        it is on trial -- the same code path the Pi runs.
        """
        self.restarts += 1
        if not self.auto_restart.get():
            self.pending_restart = True
            self.log("restart: held. Press restart to bring the new release "
                     "up.")
            return
        self.pending_restart = False
        self.after(400, self._bring_up)

    def restart_now(self):
        if not self.pending_restart and self.updater.state not in (
                "applied", "restarting"):
            self.log("restart: nothing is waiting to start")
            return
        self.pending_restart = False
        self._bring_up()

    def _bring_up(self):
        """Start the firmware again against the same install root."""
        broken = self.broken_release.get()
        self.updater = UpdateManager(
            self.config, self.releases, self.hal.system,
            on_event=self._update_event, on_restart=self._simulate_restart)
        self.device = Device(self.hal, self.pipeline, self.updater,
                             on_log=self.log, on_setup=self._setup)
        if broken:
            # A release that draws a face and never becomes a working device.
            # The one the rollback exists for.
            self.device.healthy = lambda now=None: False
            self.log("the new release came up wrong; it has two minutes to "
                     "prove otherwise")
        self.api.manager = self.updater
        self.api.device = self.device
        self.log("restarted into %s"
                 % (self.updater.guard.layout.read_current() or "the "
                    "checked-out firmware"))

    def _apply_mute(self):
        self.hal.set_mute(self.muted.get())

    def _apply_system(self):
        self.hal.system.is_online = self.online.get()
        self.hal.system.is_metered = self.metered.get()
        self.hal.system.is_undervolt = self.undervolt.get()
        self.releases.offline = not self.online.get()

    def _apply_free(self, _=None):
        self.hal.system.free_mb = self.free_mb.get()

    def _apply_audio_in(self):
        self._select_audio(
            lambda name: self.audio.select_input(
                name, device=_device_index(self.in_device.get()),
                path=self.wav_path),
            self.audio_in, self.audio.input.name)
        if self.audio.live_input and self.mic_gain.get() < 0.2:
            self.mic_gain.set(0.25)   # ~8x, the default trim

    def test_tone(self):
        """Half the answer to "I hear nothing": if this plays, the output is
        fine and the problem is on the way in."""
        try:
            seconds = self.audio.test_tone()
        except audio_mod.NotBuilt as exc:
            self.log("audio: %s" % exc)
            return
        self.log("audio: %.1fs test tone at volume %d%%"
                 % (seconds, round(self.hal.speaker.get_volume() * 100)))

    def _apply_audio_out(self):
        self._select_audio(
            lambda name: self.audio.select_output(
                name, device=_device_index(self.out_device.get())),
            self.audio_out, self.audio.output.name)

    def _select_audio(self, select, var, current):
        """Offer it, then say honestly why it did not happen. Reverting the
        radio matters: a control that looks selected and does nothing is worse
        than one that refuses."""
        try:
            chosen = select(var.get())
        except audio_mod.NotBuilt as exc:
            self.log("audio: %s" % exc)
            var.set(current)
            return
        self.log("audio: %s" % chosen.label)

    def _apply_fail(self):
        from firmware.pipeline import THINKING

        self.pipeline.fail_next = THINKING if self.fail_turn.get() else None

    def _apply_release(self):
        self.releases.urgency = self.urgency.get()
        self.releases.version = self.release_version.get().strip()
        try:
            self.releases.size_mb = float(self.size_mb.get())
        except (tk.TclError, ValueError):
            pass
        self.releases.corrupt = self.corrupt.get()
        self.releases.fail_download = self.drop.get()
        self.releases.broken_release = self.broken_release.get()
        self.releases.break_signature = self.tamper.get()

    def _setup(self):
        self.log("setup mode: the device would raise its hotspot and show a "
                 "QR code (not built yet)")

    def _update_event(self, kind, payload):
        self.log("update: %s %s" % (kind, _brief(payload)))

    # ---- the loop ----

    def _tick(self):
        self._apply_release()
        # One slider, two jobs: the level itself when nothing real is plugged
        # in, and an input trim when something is.
        knob = self.mic_gain.get()
        if self.audio.live_input:
            self.audio.set_input_gain(1.0 + knob * 31.0)
            self.gain_label.configure(text="gain x%.0f" % (1.0 + knob * 31.0))
        else:
            self.audio.manual.set_level(knob)
            self.gain_label.configure(text="level")
        self.audio.pump()
        try:
            self.device.tick()
        except Exception as exc:  # keep the window alive to show the log
            self.log("!! device.tick raised %s: %s" % (type(exc).__name__, exc))

        self._paint()
        self._readouts()
        self.after(int(1000 / FPS), self._tick)

    def _paint(self):
        panel = self.hal.panel
        frame = panel.frame
        if panel.backlight_level <= 0.0:
            frame = Image.new("RGB", frame.size, (0, 0, 0))

        big = frame.resize((320 * SCALE, 240 * SCALE), Image.NEAREST)
        if self.show_regions.get():
            draw = ImageDraw.Draw(big)
            _rect(draw, self.device.face.blit(), (60, 80, 90))
            _rect(draw, panel.last_bbox, (200, 90, 60))

        self.photo = ImageTk.PhotoImage(big)
        self.canvas.itemconfigure(self.image_id, image=self.photo)

    def _readouts(self):
        panel = self.hal.panel
        budget = self.device.draw_ms + panel.last_spi_ms
        turn = self.device.turn

        self.stats.configure(text=(
            "face     %-16s %s\n"
            "turn     %-16s level %.2f\n"
            "draw     %5.2f ms   spi %5.2f ms   ceiling %4.1f fps\n"
            "pushed   %s over SPI in %d frames"
        ) % (
            self.device.face.current,
            "muted" if self.hal.muted() else "mic live",
            turn.stage if turn else "idle",
            turn.level() if turn else 0.0,
            self.device.draw_ms, panel.last_spi_ms,
            (1000.0 / budget) if budget else 0.0,
            _mb(panel.bytes_pushed), panel.pushes,
        ))

        self._audio_readout()

        status = self.updater.status()
        line = "state    %s" % status["state"]
        if status["release"]:
            line += "   %s (%s)" % (status["release"]["version"],
                                    status["release"]["urgency"])
        if status["state"] == "downloading":
            line += "   %3.0f%%" % (status["progress"] * 100)
        if status["seconds_until_auto"] is not None:
            line += "   installs in %ds" % round(status["seconds_until_auto"])
        if status["on_trial"]:
            line += "   on trial, %s -> %s" % (status["on_trial"]["previous"]
                                               or "nothing",
                                               status["on_trial"]["new"])
        if status["release"]:
            line += "\n%s" % status["release"]["signature_note"]
        if status["running_release"]:
            line += "\nrunning  releases/%s   base v%s" % (
                status["running_release"], status["base"])
        if status["error"]:
            line += "\n%s" % status["error"]
        self.update_line.configure(
            text=line, fg=AMBER if status["error"] else INK)

    def _audio_readout(self):
        status = self.audio.status()
        self._meter(0, "mic", status["mic_level"], TEAL)
        self._meter(13, "out", status["speaker_level"],
                    TEAL if status["playing"] else LINE)
        line = "%d Hz mono, %d-frame chunks — captured %.1fs" % (
            status["rate"], status["chunk_frames"],
            status["captured_seconds"])
        if status["detail"]:
            line += "\n%s" % status["detail"]
        self.audio_line.configure(
            text=line,
            fg=AMBER if (status["detail"] or status["silent_input"]) else DIM)

        if status["silent_input"]:
            hint = ("pick another input above, or try the test tone to check "
                    "the output on its own")
        elif status["live_input"]:
            hint = "speak after the wake word — it plays back what it heard"
        else:
            hint = "the slider drives the listening face"
        self.audio_hint.configure(text=hint)

    def _meter(self, y, label, value, colour):
        canvas = self.meters
        canvas.delete(label)
        canvas.create_text(2, y + 6, text=label, anchor="w", fill=DIM,
                           font=("Consolas", 8), tags=label)
        x0, width = 34, 290
        canvas.create_rectangle(x0, y + 2, x0 + width, y + 10, outline=LINE,
                                tags=label)
        if value > 0:
            canvas.create_rectangle(x0, y + 2, x0 + width * min(1.0, value),
                                    y + 10, outline="", fill=colour,
                                    tags=label)

    def log(self, line):
        """Append, collapsing a repeat into a count.

        One failure that repeats on every click filled the pane with the same
        sentence and pushed everything useful off the top.
        """
        stamp = time.strftime("%H:%M:%S")
        if not hasattr(self, "logbox"):
            # Something logged while the window was still being built. The
            # pairing PIN does this when it cannot be written to disk.
            print("%s  %s" % (stamp, line))
            return
        self.logbox.configure(state="normal")
        if line == self._last_log:
            self._last_log_count += 1
            self.logbox.delete("end-2l linestart", "end-1c")
            self.logbox.insert("end", "%s  %s   (x%d)\n"
                               % (stamp, line, self._last_log_count))
        else:
            self._last_log = line
            self._last_log_count = 1
            self.logbox.insert("end", "%s  %s\n" % (stamp, line))
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def quit_all(self):
        self.audio.close()
        self.device.close()
        self.server.shutdown()
        self.destroy()


def _device_index(label):
    """"12: Microphone (...)" -> 12, and "default" -> None."""
    if not label or label == "default":
        return None
    head = label.split(":", 1)[0].strip()
    return int(head) if head.isdigit() else None


def _backend_label(backend):
    suffix = "" if backend in audio_mod.BUILT else "   (not built)"
    return "%s%s" % (backend.label, suffix)


def _rect(draw, box, color):
    if not box:
        return
    x0, x1 = sorted((box[0] * SCALE, box[2] * SCALE))
    y0, y1 = sorted((box[1] * SCALE, box[3] * SCALE))
    if x1 <= x0 or y1 <= y0:
        return
    draw.rectangle([x0, y0, x1 - 1, y1 - 1], outline=color, width=1)


def _mb(value):
    return "%.1f MB" % (value / float(MB))


def _brief(payload):
    if not isinstance(payload, dict):
        return ""
    for key in ("error", "reason", "line", "version", "installed"):
        if payload.get(key):
            return str(payload[key])
    release = payload.get("release")
    if isinstance(release, dict):
        return "%s (%s)" % (release.get("version"), release.get("urgency"))
    return ""


def parse_args(argv=None):
    import os
    import tempfile

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080,
                    help="where to serve the device's own update page")
    ap.add_argument("--repo", default="",
                    help="talk to a real GitHub repo instead of the fake feed")
    ap.add_argument("--release", default="0.4.0",
                    help="version the fake feed offers")
    ap.add_argument("--urgency", default="optional",
                    choices=("optional", "recommended", "urgent"))
    ap.add_argument("--size-mb", type=float, default=18.0)
    ap.add_argument("--kbps", type=float, default=2400.0,
                    help="download speed for the fake feed")
    ap.add_argument("--grace", type=float, default=20.0,
                    help="seconds an urgent update waits before installing")
    ap.add_argument("--check-hours", type=float, default=6.0)
    ap.add_argument("--free-mb", type=int, default=2048)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--wav", default=None,
                    help="a 16-bit wav to feed in as the microphone; with "
                         "no path the file input synthesises an utterance")
    ap.add_argument("--data-root",
                    default=os.path.join(tempfile.gettempdir(),
                                         "trstenko-emulator"))
    return ap.parse_args(argv)


def main(argv=None):
    Emulator(parse_args(argv)).mainloop()
    return 0

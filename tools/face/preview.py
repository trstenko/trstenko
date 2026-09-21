"""Desktop preview for the face states.

Runs the same sampler, controller and renderer the device runs, at the panel's
real 320x240, scaled up with nearest-neighbour so what you see is the actual
pixel grid. Reports draw time and the SPI cost of the dirty rect, which together
are what set the frame rate on the Pi.

    python -m tools.face.preview

Keys: 1-8 pick a state, space pauses, R restarts, D runs the demo loop,
B toggles the blend so you can see what cutting between states looks like.
"""

import math
import time
import tkinter as tk

from PIL import Image, ImageDraw, ImageTk

from firmware.face import states as state_defs
from firmware.face.controller import FaceController
from firmware.face.renderer import FaceRenderer, spi_cost_ms

SCALE = 3
TARGET_FPS = 30

# boot -> idle -> wake -> listening -> thinking -> talking -> idle, the loop
# from prototype-plan.md step 16. The one-shots hand over on their own, so the
# holds here only matter for the looping states.
DEMO = [
    ("face_boot", 2.5), ("face_idle", 2.6), ("face_wake", 1.0),
    ("face_listening", 2.6), ("face_thinking", 2.4), ("face_talking", 3.4),
    ("face_idle", 3.0),
]


class Preview(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Trstenko — face preview (320 × 240)")
        self.configure(bg="#0b0f12")
        self.resizable(False, False)

        self.states = state_defs.build()
        self.renderer = FaceRenderer(ssaa=2)
        self.ctrl = FaceController(
            self.states, "face_idle", follow=state_defs.FOLLOW)

        self.clock = time.perf_counter()
        self.paused = False
        self.pause_at = 0.0
        self.demo_idx = None
        self.demo_t0 = 0.0
        self.draw_ms = 0.0

        self.show_guide = tk.BooleanVar(value=True)
        self.auto_amp = tk.BooleanVar(value=True)
        self.blending = tk.BooleanVar(value=True)
        self.amp = tk.DoubleVar(value=0.0)

        self._build_ui()
        self._bind_keys()
        self._highlight()
        self._tick()

    # ---- ui ----

    def _build_ui(self):
        wrap = tk.Frame(self, bg="#0b0f12")
        wrap.pack(padx=14, pady=14)

        self.canvas = tk.Canvas(
            wrap, width=320 * SCALE, height=240 * SCALE,
            highlightthickness=1, highlightbackground="#243036", bd=0)
        self.canvas.grid(row=0, column=0, rowspan=2)
        self.image_id = self.canvas.create_image(0, 0, anchor="nw")

        side = tk.Frame(wrap, bg="#0b0f12")
        side.grid(row=0, column=1, sticky="n", padx=(14, 0))

        tk.Label(side, text="STATE", bg="#0b0f12", fg="#6b7d85",
                 font=("Consolas", 9, "bold")).pack(anchor="w", pady=(0, 4))

        self.buttons = {}
        for i, name in enumerate(state_defs.ORDER):
            st = self.states.get(name)
            b = tk.Button(
                side, text="%d  %-15s %s" % (i + 1, name[5:],
                                             st.source if st else "?"),
                anchor="w", width=26, relief="flat", bd=0,
                bg="#151d22", fg="#c8d4d8", activebackground="#1B7F9B",
                activeforeground="#ffffff", font=("Consolas", 9),
                command=lambda n=name: self.select(n))
            b.pack(fill="x", pady=1)
            self.buttons[name] = b

        tk.Button(side, text="D  demo loop", anchor="w", width=26, relief="flat",
                  bd=0, bg="#1B7F9B", fg="#ffffff", font=("Consolas", 9, "bold"),
                  command=self.start_demo).pack(fill="x", pady=(8, 0))

        tk.Label(side, text="LEVEL  (mic / TTS)", bg="#0b0f12", fg="#6b7d85",
                 font=("Consolas", 9, "bold")).pack(anchor="w", pady=(14, 2))
        tk.Scale(side, from_=0.0, to=1.0, resolution=0.01, orient="horizontal",
                 variable=self.amp, bg="#0b0f12", fg="#c8d4d8", length=210,
                 troughcolor="#151d22", highlightthickness=0, bd=0,
                 font=("Consolas", 8)).pack(anchor="w")

        for text, var in (("simulate level", self.auto_amp),
                          ("B  ease between states", self.blending),
                          ("show blit region", self.show_guide)):
            tk.Checkbutton(side, text=text, variable=var,
                           bg="#0b0f12", fg="#c8d4d8", selectcolor="#151d22",
                           activebackground="#0b0f12",
                           activeforeground="#c8d4d8",
                           font=("Consolas", 9)).pack(anchor="w")

        self.stats = tk.Label(
            side, text="", bg="#0b0f12", fg="#8fa3aa", justify="left",
            anchor="w", font=("Consolas", 9))
        self.stats.pack(anchor="w", pady=(14, 0))

        self.note = tk.Label(
            wrap, text="", bg="#0b0f12", fg="#6b7d85", wraplength=320 * SCALE,
            justify="left", anchor="w", font=("Consolas", 9))
        self.note.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _bind_keys(self):
        for i, name in enumerate(state_defs.ORDER):
            self.bind(str(i + 1), lambda e, n=name: self.select(n))
        self.bind("<space>", lambda e: self.toggle_pause())
        for k in "rR":
            self.bind(k, lambda e: self.select(self.ctrl.current, force=True))
        for k in "dD":
            self.bind(k, lambda e: self.start_demo())
        for k in "bB":
            self.bind(k, lambda e: self.blending.set(not self.blending.get()))
        self.bind("<Escape>", lambda e: self.destroy())

    # ---- control ----

    def _now(self):
        return self.pause_at if self.paused else time.perf_counter()

    def select(self, name, force=False, quiet=False):
        if not quiet:
            self.demo_idx = None
        now = self._now()
        blend = state_defs.blend_time(self.ctrl.current, name)
        if force:
            self.ctrl.current = None
        self.ctrl.set(name, now, 0.0 if not self.blending.get() else blend)
        self._highlight()

    def start_demo(self):
        self.select(DEMO[0][0], quiet=True)
        self.demo_idx = 0
        self.demo_t0 = self._now()

    def _highlight(self):
        cur = self.ctrl.current
        for n, b in self.buttons.items():
            on = (n == cur)
            b.configure(bg="#1B7F9B" if on else "#151d22",
                        fg="#ffffff" if on else "#c8d4d8")

    def toggle_pause(self):
        if not self.paused:
            self.pause_at = time.perf_counter()
        else:
            # Shift every clock forward, so unpausing does not jump the
            # timeline by however long the window sat paused.
            gap = time.perf_counter() - self.pause_at
            self.ctrl.started += gap
            self.ctrl.prev_started += gap
            if self.ctrl.blend_from is not None:
                self.ctrl.blend_from += gap
            self.demo_t0 += gap
        self.paused = not self.paused

    # ---- frame ----

    def _advance_demo(self, now):
        if self.demo_idx is None:
            return
        name, hold = DEMO[self.demo_idx]
        # A one-shot hands itself over; follow it rather than fighting it.
        if name in state_defs.FOLLOW and self.ctrl.current != name:
            self.demo_idx = (self.demo_idx + 1) % len(DEMO)
            self.demo_t0 = now
            return
        if now - self.demo_t0 >= hold:
            self.demo_idx = (self.demo_idx + 1) % len(DEMO)
            self.demo_t0 = now
            self.select(DEMO[self.demo_idx][0], quiet=True)

    def _amplitude(self, t):
        if not self.auto_amp.get():
            return self.amp.get()
        # Crude speech envelope: a syllable rate with gaps between phrases.
        phrase = 0.5 + 0.5 * math.sin(t * 0.9)
        syllable = abs(math.sin(t * 9.0)) * abs(math.sin(t * 3.7))
        return max(0.0, min(1.0, phrase * syllable * 1.4))

    def _tick(self):
        now = self._now()
        before = self.ctrl.current
        self._advance_demo(now)

        params = {"amp": self._amplitude(now - self.clock)}
        if self.auto_amp.get():
            self.amp.set(round(params["amp"], 2))

        start = time.perf_counter()
        shapes = self.ctrl.sample(now, params)
        img, bbox = self.renderer.render(shapes)
        self.draw_ms = (time.perf_counter() - start) * 1000.0

        if self.ctrl.current != before:
            self._highlight()

        big = img.resize((320 * SCALE, 240 * SCALE), Image.NEAREST)
        if self.show_guide.get():
            big = self._overlay(big, bbox)

        self.photo = ImageTk.PhotoImage(big)
        self.canvas.itemconfigure(self.image_id, image=self.photo)
        self._update_stats(now, bbox)

        self.after(int(1000 / TARGET_FPS), self._tick)

    def _overlay(self, big, bbox):
        d = ImageDraw.Draw(big)
        self._rect(d, self.ctrl.blit(), (60, 80, 90))
        self._rect(d, bbox, (200, 90, 60))
        return big

    @staticmethod
    def _rect(d, box, color):
        """Normalise rather than trust the caller. An empty or reversed box is
        a ValueError out of Pillow, and in a draw loop that means a dead
        window."""
        if not box:
            return
        x0, x1 = sorted((box[0] * SCALE, box[2] * SCALE))
        y0, y1 = sorted((box[1] * SCALE, box[3] * SCALE))
        if x1 <= x0 or y1 <= y0:
            return
        d.rectangle([x0, y0, x1, y1], outline=color, width=1)

    def _update_stats(self, now, bbox):
        state = self.states[self.ctrl.current]
        spi = spi_cost_ms(bbox)
        full = spi_cost_ms((0, 0, 320, 240))

        if bbox:
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            region = "%d x %d  (%.0f KB)" % (w, h, w * h * 2 / 1024.0)
        else:
            region = "nothing drawn"

        budget = self.draw_ms + spi
        fps = 1000.0 / budget if budget > 0 else 0.0

        p = self.ctrl.progress(now)
        if self.ctrl.prev is not None and p < 1.0:
            blend = "%s -> %s  %3.0f%%" % (
                self.ctrl.prev[5:], self.ctrl.current[5:], p * 100)
        else:
            blend = "settled"

        outside = ""
        if bbox:
            gx0, gy0, gx1, gy1 = self.ctrl.blit()
            if bbox[0] < gx0 or bbox[1] < gy0 or bbox[2] > gx1 or bbox[3] > gy1:
                outside = "\n!! outside the blit region — clipped on device"

        self.stats.configure(text=(
            "t        %5.2f / %.2fs %s\n"
            "blend    %s\n"
            "dirty    %s\n"
            "draw     %5.2f ms  (PC, ssaa 2)\n"
            "spi      %5.2f ms  @32MHz  (full: %.1f)\n"
            "ceiling  %5.1f fps%s"
        ) % (state.local_time(now - self.ctrl.started), state.duration,
             "loop" if state.loop else "one-shot", blend, region,
             self.draw_ms, spi, full, fps, outside))

        self.note.configure(
            text="%s — %s" % (state.name, state.note or "no note"))


def main():
    Preview().mainloop()


if __name__ == "__main__":
    main()

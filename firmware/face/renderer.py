"""Draws a sampled face to a 320x240 image.

Every shape is a rounded rect drawn into a small RGBA tile and composited, which
keeps rotation and opacity on one code path and means a frame costs a few
hundred pixels of work rather than a full canvas per layer.

`ssaa` trades CPU for antialiasing. Pillow's rounded_rectangle is aliased, and
on a 320x240 panel jagged eyelids are visible from across a desk. Drop it to 1
on the Pi if the frame budget gets tight.
"""

import math

from PIL import Image, ImageDraw

DARK = (18, 25, 29)

# How far past the supersampled bbox the downscale filter can reach, in final
# pixels. Two covers ssaa 2 with room to spare and costs a border of pixels on
# a rect that is already ~100 wide.
RESAMPLE_PAD = 2


class FaceRenderer:
    def __init__(self, width=320, height=240, bg=DARK, ssaa=2):
        self.width = width
        self.height = height
        self.bg = bg
        self.ssaa = max(1, int(ssaa))

    def render(self, shapes):
        """Return (RGB image, dirty bbox) for one sampled frame.

        The bbox is the union of everything drawn. It is what the device pushes
        over SPI, so it is also the number that decides the frame rate.
        """
        s = self.ssaa
        canvas = Image.new("RGB", (self.width * s, self.height * s), self.bg)
        bbox = None

        for shape in shapes:
            box = self._draw(canvas, shape, s)
            if box is None:
                continue
            bbox = box if bbox is None else (
                min(bbox[0], box[0]), min(bbox[1], box[1]),
                max(bbox[2], box[2]), max(bbox[3], box[3]),
            )

        if s > 1:
            canvas = canvas.resize((self.width, self.height), Image.LANCZOS)
            if bbox is not None:
                # Round outwards, then pad. LANCZOS is not a box filter: it
                # carries a shape's energy a pixel or two past its own edge,
                # so a bbox that only covers the shape leaves a faint outline
                # behind when the shape moves away -- and since the panel is
                # only sent the bbox, that outline stays lit.
                bbox = (int(math.floor(bbox[0] / s)) - RESAMPLE_PAD,
                        int(math.floor(bbox[1] / s)) - RESAMPLE_PAD,
                        int(math.ceil(bbox[2] / s)) + RESAMPLE_PAD,
                        int(math.ceil(bbox[3] / s)) + RESAMPLE_PAD)

        if bbox is not None:
            bbox = (max(0, bbox[0]), max(0, bbox[1]),
                    min(self.width, bbox[2]), min(self.height, bbox[3]))
            # A shape entirely off-canvas -- boot's eyes start at y 301 -- clamps
            # to an empty or inverted box. Nothing to blit.
            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                bbox = None

        return canvas, bbox

    def _draw(self, canvas, shape, s):
        alpha = shape.get("opacity", 100.0) / 100.0
        if alpha <= 0.002:
            return None

        w = shape["w"] * shape.get("sx", 1.0) * s
        h = shape["h"] * shape.get("sy", 1.0) * s
        if w < 1.0 or h < 1.0:
            return None

        # AE scales the corner with the shape; clamp so it stays drawable.
        r = shape["r"] * min(shape.get("sx", 1.0), shape.get("sy", 1.0)) * s
        r = max(0.0, min(r, min(w, h) / 2.0))

        tw, th = int(math.ceil(w)) + 2, int(math.ceil(h)) + 2
        tile = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        color = tuple(shape.get("color") or (27, 127, 155))
        ImageDraw.Draw(tile).rounded_rectangle(
            [1, 1, 1 + w, 1 + h], radius=r,
            fill=color + (int(round(255 * alpha)),),
        )

        rot = shape.get("rot", 0.0) % 360.0
        if rot:
            tile = tile.rotate(rot, expand=True, resample=Image.BILINEAR)

        cx, cy = shape["x"] * s, shape["y"] * s
        left = int(round(cx - tile.width / 2.0))
        top = int(round(cy - tile.height / 2.0))
        canvas.paste(tile, (left, top), tile)
        return (left, top, left + tile.width, top + tile.height)


def to_rgb565(img):
    """Pack an RGB image the way the ILI9341 wants it: big-endian 5-6-5."""
    out = bytearray(img.width * img.height * 2)
    i = 0
    for r, g, b in img.getdata():
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out[i] = v >> 8
        out[i + 1] = v & 0xFF
        i += 2
    return bytes(out)


def spi_cost_ms(bbox, hz=32_000_000):
    """How long pushing that bbox takes over SPI at RGB565, ignoring overhead."""
    if bbox is None:
        return 0.0
    w, h = max(0, bbox[2] - bbox[0]), max(0, bbox[3] - bbox[1])
    return (w * h * 2 * 8) / hz * 1000.0

"""The setup card: what the screen shows instead of the face during setup.

The panel is 320 x 240. While the hotspot is up it shows a QR code a phone's
camera joins the hotspot from, and under it the address to open and the pairing
PIN (plan 07). While the device joins the home Wi-Fi it says so, and once it is
online it shows where to find it for a couple of minutes.

The card is a still picture, so it is drawn once and kept: `cached` hands back
the same image until what it says changes, and the device only sends the panel
a new one then.

The QR code needs the `qrcode` package, which is installed on the board
(`requirements-pi.txt`). Without it the card shows the hotspot's name and
password as text instead, and says so in the log: a card with no way in would
be worse than a plain one.
"""

import logging

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("trstenko.setupcard")

WIDTH, HEIGHT = 320, 240
DARK = (18, 25, 29)
PAPER = (255, 253, 248)
TEAL = (27, 127, 155)
DIM = (163, 176, 181)

QR_SIZE = 168            # leaves two lines of text under it
QR_TOP = 10
# TODO on the board: read these sizes on the real panel from a metre away.
TEXT_SIZE = 20
SMALL_SIZE = 15

# The few words on the card. Slovenian is unreviewed by a native speaker, like
# the rest of the device's Slovenian.
WORDS = {
    "en": {"wifi": "Wi-Fi", "password": "Password", "joining": "Connecting to",
           "online": "Online", "starting": "Starting setup",
           "info": "Open in a browser", "offline": "Not on Wi-Fi"},
    "sl": {"wifi": "Wi-Fi", "password": "Geslo", "joining": "Povezujem z",
           "online": "Povezano", "starting": "Začenjam nastavitev",
           "info": "Odprite v brskalniku", "offline": "Ni povezave z Wi-Fi"},
}

_warned = False


def _font(size):
    try:
        return ImageFont.load_default(size)
    except TypeError:
        # Pillow before 10.1 has one size of built-in font.
        return ImageFont.load_default()


def _centre(draw, y, text, font, fill=PAPER):
    width = draw.textlength(text, font=font)
    draw.text(((WIDTH - width) / 2, y), text, font=font, fill=fill)


def qr_matrix(text):
    """The code as rows of booleans, or None when `qrcode` is not installed."""
    global _warned
    try:
        import qrcode
    except ImportError:
        if not _warned:
            _warned = True
            log.warning("qrcode is not installed: the setup card shows the "
                        "hotspot as text")
        return None
    code = qrcode.QRCode(border=2,
                         error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(text)
    code.make(fit=True)
    return code.get_matrix()


def draw(card, matrix=qr_matrix):
    """An RGB image of `card` (from `NetSetup.card`), the size of the panel."""
    image = Image.new("RGB", (WIDTH, HEIGHT), DARK)
    pen = ImageDraw.Draw(image)
    big, small = _font(TEXT_SIZE), _font(SMALL_SIZE)
    mode = card.get("mode")
    words = WORDS.get(card.get("lang"), WORDS["en"])

    if mode == "setup":
        rows = matrix(card["qr"])
        if rows:
            cell = max(1, QR_SIZE // len(rows))
            size = cell * len(rows)
            left = (WIDTH - size) // 2
            pen.rectangle((left, QR_TOP, left + size - 1, QR_TOP + size - 1),
                          fill=PAPER)
            for y, row in enumerate(rows):
                for x, on in enumerate(row):
                    if on:
                        pen.rectangle(
                            (left + x * cell, QR_TOP + y * cell,
                             left + (x + 1) * cell - 1,
                             QR_TOP + (y + 1) * cell - 1), fill=DARK)
        else:
            _centre(pen, 30, words["wifi"], small, DIM)
            _centre(pen, 52, card["ssid"], big)
            _centre(pen, 96, words["password"], small, DIM)
            _centre(pen, 118, card["password"], big)
        _centre(pen, 186, card["url"].replace("http://", ""), big)
        _centre(pen, 212, "PIN %s" % card["pin"], big, TEAL)
    elif mode == "joining":
        _centre(pen, 86, words["joining"], small, DIM)
        _centre(pen, 112, card["ssid"][:24], big)
    elif mode == "info" and not card.get("address"):
        _centre(pen, 80, words["offline"], big)
        _centre(pen, 150, "PIN %s" % card["pin"], big, TEAL)
    elif mode in ("online", "info"):
        _centre(pen, 46, words[mode], big if mode == "online" else small,
                TEAL if mode == "online" else DIM)
        _centre(pen, 92, card["url"].replace("http://", ""), big)
        if card.get("address"):
            _centre(pen, 122, card["address"], small, DIM)
        _centre(pen, 168, "PIN %s" % card["pin"], big)
    else:
        _centre(pen, 106, words["starting"], big)
    return image


_last = (None, None)


def cached(card):
    """The image for `card`, drawn only when the card has changed. None for no
    card, which means the face."""
    global _last
    if card is None:
        _last = (None, None)
        return None
    key = tuple(sorted(card.items()))
    if _last[0] != key:
        _last = (key, draw(card))
    return _last[1]

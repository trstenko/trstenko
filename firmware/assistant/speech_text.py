"""Turning what the model writes into what a speech engine should be given.

Three jobs, all because the model's text is read aloud by Piper:

- **Cut it into sentences as it streams.** Piper produces nothing until it has a
  whole line, so the time to the first sound is the time to the end of the first
  sentence. The splitter hands each sentence on the moment it is complete, and
  is careful about the places a full stop is not an end: "Dr. Novak", "3.5",
  "e.g.". It would rather wait a few characters than cut a sentence in the
  wrong place, because a wrong cut is heard.
- **Clean it.** The prompt says plain spoken language, and models ignore that
  often enough to matter: markdown, links, emoji and bullet characters are
  removed, and the symbols a synthesiser reads badly ("°C", "%", "km") are
  said in words.
- **Keep it short.** A voice device that reads out an essay sounds broken, so
  after 60 words the rest is dropped and the device says "That's the short
  version." (plan 03).

`HeardFilter` is for Gemini, whose audio goes into the chat request itself. The
model is asked to begin with `<heard>what it heard</heard>`, which gives the
device the transcript with no second request (plan 03, Finding 6). The filter
takes that tag out of the stream before anything is spoken, and keeps what was
inside it.
"""

import re
import unicodedata

# Plan 03: after this many words, stop queuing sentences.
MAX_SPOKEN_WORDS = 60

# Words that end in a full stop without ending a sentence. Short on purpose: a
# word that often does end one ("etc.", "no.") is left out, since splitting after
# it is harmless and not splitting delays the speech.
ABBREVIATIONS = {
    "en": frozenset(("mr", "mrs", "ms", "dr", "prof", "st", "vs", "jr", "sr",
                     "mt", "e.g", "i.e", "a.m", "p.m")),
    "sl": frozenset(("npr", "dr", "tj", "oz", "sv", "gl", "ul", "ing", "mag",
                     "prof", "št")),
}
ALL_ABBREVIATIONS = ABBREVIATIONS["en"] | ABBREVIATIONS["sl"]

_BOUNDARY = re.compile(
    r"""([.!?…]+["')\]”’»*_]*)(\s+)|(\s*\n\s*)""")
_LAST_WORD = re.compile(r"(\S+)$")


class SentenceSplitter:
    """Feed it text as it arrives; it returns the sentences that are complete.

        for sentence in splitter.feed(delta):
            speak(sentence)
        tail = splitter.flush()      # at the end of the stream
    """

    def __init__(self):
        self._buf = ""

    def feed(self, text):
        self._buf += text
        out = []
        while True:
            end = self._boundary()
            if end is None:
                return out
            sentence, self._buf = self._buf[:end].strip(), \
                self._buf[end:].lstrip()
            if sentence:
                out.append(sentence)

    def flush(self):
        """What is left when the stream is over."""
        rest, self._buf = self._buf.strip(), ""
        return rest

    def _boundary(self):
        for match in _BOUNDARY.finditer(self._buf):
            if match.group(3) is not None:
                if self._buf[:match.start()].strip():
                    return match.end()
                continue
            if match.group(1).startswith(".") and self._not_an_end(match):
                continue
            return match.end()
        return None

    def _not_an_end(self, match):
        """Is this full stop part of an abbreviation or a list number?"""
        before = self._buf[:match.start(1)]
        found = _LAST_WORD.search(before)
        if not found:
            return False
        word = found.group(1).lstrip("([\"'“‘«").lower()
        if word in ALL_ABBREVIATIONS:
            return True
        # "1. First" at the start of a line is a list marker.
        if word.isdigit() and len(word) <= 2:
            start = found.start(1)
            return start == 0 or before[:start].rstrip(" \t").endswith("\n") \
                or not before[:start].strip()
        return False


_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_BULLET = re.compile(r"(?m)^\s*(?:[-+*•·▪●◦]|\d{1,2}[.)])\s+")
_MARKDOWN = re.compile(r"[*_#~|`]+|(?m:^\s*>+\s?)")
_SPACES = re.compile(r"[ \t\r\f\v]+")

_UNITS = {
    "en": (("°C", " degrees Celsius"), ("°F", " degrees Fahrenheit"),
           ("°", " degrees"), ("%", " percent"), ("&", " and "),
           ("€", " euros")),
    "sl": (("°C", " stopinj Celzija"), ("°F", " stopinj Fahrenheita"),
           ("°", " stopinj"), ("%", " odstotkov"), ("&", " in "),
           ("€", " evrov")),
}
_AFTER_NUMBER = {
    "en": (("km/h", "kilometres per hour"), ("km", "kilometres"),
           ("kg", "kilograms"), ("cm", "centimetres"), ("mm", "millimetres"),
           ("ml", "millilitres")),
    "sl": (("km/h", "kilometrov na uro"), ("km", "kilometrov"),
           ("kg", "kilogramov"), ("cm", "centimetrov"),
           ("mm", "milimetrov"), ("ml", "mililitrov")),
}


def clean_for_speech(text, lang="en"):
    """Plain words for a synthesiser. Returns '' when nothing speakable is
    left."""
    lang = lang if lang in _UNITS else "en"
    text = _CODE_BLOCK.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _BULLET.sub("", text)
    for symbol, words in _UNITS[lang]:
        text = text.replace(symbol, words)
    if lang == "en":
        text = re.sub(r"\$\s?(\d[\d,.]*)", r"\1 dollars", text)
    for unit, words in _AFTER_NUMBER[lang]:
        text = re.sub(r"(?<=\d)\s?%s\b" % re.escape(unit), " " + words, text)
    text = _MARKDOWN.sub(" ", text)
    # Emoji and other symbols. Done after the units, since the degree sign is
    # one.
    text = "".join(c for c in text
                   if unicodedata.category(c) not in ("So", "Sk", "Cs", "Co")
                   and c not in "‍️︎")
    text = _SPACES.sub(" ", text)
    # What a removed symbol leaves behind: "hot 😀, very" is "hot , very".
    return re.sub(r"\s+([,.;:!?])", r"\1", text).strip()


class SpokenCap:
    """Stops a reply that has gone on too long, at a sentence end."""

    def __init__(self, limit=MAX_SPOKEN_WORDS):
        self.limit = limit
        self.words = 0
        self.full = False
        self.dropped = False

    def take(self, sentence):
        """The sentence if it may be spoken, else None. Once the limit is
        passed everything after it is dropped, and `dropped` says there was
        something to drop, which is when "that's the short version" is true."""
        if self.full:
            self.dropped = True
            return None
        self.words += len(sentence.split())
        if self.words >= self.limit:
            self.full = True
        return sentence


class HeardFilter:
    """Takes the `<heard>...</heard>` tag off the front of a stream.

    Without it the transcript would be read aloud before the answer. If the
    model begins with anything else, the text passes through untouched and
    there is simply no transcript.
    """

    OPEN = "<heard>"
    CLOSE = "</heard>"
    MAX_INSIDE = 600

    def __init__(self, enabled=True):
        self._state = "start" if enabled else "pass"
        self._buf = ""
        self.transcript = ""

    @property
    def waiting(self):
        return self._state != "pass"

    def feed(self, text):
        """The part of `text` that may be spoken."""
        if self._state == "pass":
            return text
        self._buf += text
        if self._state == "start":
            stripped = self._buf.lstrip()
            if stripped.startswith(self.OPEN):
                self._buf = stripped[len(self.OPEN):]
                self._state = "inside"
            elif self.OPEN.startswith(stripped):
                return ""            # could still turn into the tag
            else:
                self._state = "pass"
                out, self._buf = self._buf, ""
                return out
        if self._state == "inside":
            end = self._buf.find(self.CLOSE)
            if end >= 0:
                self.transcript = self._buf[:end].strip()
                rest = self._buf[end + len(self.CLOSE):].lstrip()
                self._buf = ""
                self._state = "pass"
                return rest
            if len(self._buf) > self.MAX_INSIDE:
                # A tag that never closes is not a transcript. Say nothing of
                # it rather than read out a paragraph of its contents.
                self.transcript = self._buf[:self.MAX_INSIDE].strip()
                self._buf = ""
                self._state = "pass"
        return ""

    def finish(self):
        """At the end of the stream: whatever was held back."""
        out = ""
        if self._state == "start":
            out = self._buf
        elif self._state == "inside":
            self.transcript = self._buf.strip()[:self.MAX_INSIDE]
        self._buf = ""
        self._state = "pass"
        return out

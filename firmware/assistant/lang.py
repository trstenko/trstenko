"""Everything the device says that the model did not write.

Confirmations ("Timer set for ten minutes."), the sentence for each way a turn
can fail, "That's the short version." The model's own replies are in whatever
language the person spoke (the prompt says so); these are the lines the device
itself speaks, and they have to exist in every language it is sold in or a
Slovenian owner hears English the first time the network drops.

They are plain JSON, one file per language, flat, with `{placeholders}`. The
rules, checked in CI (`check()`): every key in English exists in every other
language, so nothing falls back silently, and no
language has a key English does not, and no language uses a placeholder English
does not supply (it may leave one out: a Slovenian number has case endings that
a developer is better off not guessing).

**The Slovenian is written by a developer and has not been read by a native
speaker.** It is deliberately plain, and avoids numbers with their case
endings. It wants a read-through before the Slovenian voice ships (M5).
"""

import json
import logging
import os
import re

log = logging.getLogger("trstenko.lang")

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lang")
LANGS = ("en", "sl")
DEFAULT = "en"

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_cache = {}


def _load(lang):
    path = os.path.join(HERE, "%s.json" % lang)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("%s.json must be an object" % lang)
    return data


class Strings:
    """The lines for one language. Missing ones come from English, so a line
    added in English first is spoken in English rather than not at all; CI
    keeps that from lasting."""

    def __init__(self, lang):
        self.lang = lang if lang in LANGS else DEFAULT
        table = dict(_load(DEFAULT))
        if self.lang != DEFAULT:
            table.update(_load(self.lang))
        self._table = table

    def has(self, key):
        return key in self._table

    def __call__(self, key, **values):
        text = self._table.get(key)
        if text is None:
            log.error("no line called %r", key)
            return ""
        try:
            return text.format(**values)
        except (KeyError, IndexError, ValueError):
            log.error("line %r was given the wrong values", key)
            return _PLACEHOLDER.sub("", text).strip()


def strings(lang):
    """Cached; the files are read once."""
    lang = lang if lang in LANGS else DEFAULT
    if lang not in _cache:
        _cache[lang] = Strings(lang)
    return _cache[lang]


def check():
    """Problems with the language files, for CI."""
    problems = []
    try:
        base = _load(DEFAULT)
    except (OSError, ValueError) as exc:
        return ["lang/en.json: %s" % exc]
    for lang in LANGS:
        if lang == DEFAULT:
            continue
        try:
            other = _load(lang)
        except (OSError, ValueError) as exc:
            problems.append("lang/%s.json: %s" % (lang, exc))
            continue
        for key in sorted(set(base) - set(other)):
            problems.append("lang/%s.json has no %r, which English has"
                            % (lang, key))
        for key in sorted(set(other) - set(base)):
            problems.append("lang/%s.json has %r, which English does not"
                            % (lang, key))
        for key in sorted(set(base) & set(other)):
            invented = (set(_PLACEHOLDER.findall(other[key]))
                        - set(_PLACEHOLDER.findall(base[key])))
            if invented:
                problems.append("lang/%s.json %r uses {%s}, which English "
                                "does not give it" % (lang, key,
                                                      ", ".join(sorted(
                                                          invented))))
    for lang in LANGS:
        try:
            for key, text in _load(lang).items():
                if not isinstance(text, str) or not text.strip():
                    problems.append("lang/%s.json %r must be text" % (lang,
                                                                       key))
        except (OSError, ValueError):
            pass
    return problems

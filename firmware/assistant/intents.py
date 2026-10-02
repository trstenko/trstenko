"""The commands that need no model: answered from the transcript alone.

A handful of whole-sentence patterns run on the transcript before the model is
asked (`docs/research/llm-workflow.md`, Finding 10). A match skips the model
round trip, which is the slowest step we control, and its cost, on the commands
people say most. The rules that keep it from being worse than nothing:

- **Whole sentences, strictly.** "Set a timer for ten minutes" matches; "set a
  timer for ten minutes and remind me about the bins" does not, and goes to the
  model. A matcher that guesses is worse than no matcher.
- **Same code path as the model.** A match runs the registry's own tool, so a
  timer set by pattern and one set by the model are the same timer, with the
  same checks and the same confirmation.
- **Per language.** English and Slovenian each have a list. Numbers are words or
  digits up to 99 (59 in Slovenian, whose compound tens are not all written).
- **The alert window is separate.** "Stop" and "snooze" mean something only
  while a reminder is going off (plan 02); at any other time "stop" ends the turn
  quietly.

Local commands still work with the budget spent or the provider down, as long as
the words reach us. On Gemini, where hearing and thinking are one request, that
means they work only while that request does.
"""

import re

from ..scheduler import clock as sched_clock
from ..scheduler import speech

# Intent names.
VOLUME, STOP, TIME, TIMER, ACK, SNOOZE = (
    "volume", "stop", "time", "timer", "ack", "snooze")

SNOOZE_DEFAULT_MINUTES = 5


def _numbers():
    words = {}
    for n in range(1, 100):
        words[speech.number_en(n)] = n
        words[speech.number_en(n).replace("-", " ")] = n
    sl = {}
    for n in range(1, 60):
        sl[speech.number_sl(n)] = n
    for n, word in ((60, "šestdeset"), (70, "sedemdeset"), (80, "osemdeset"),
                    (90, "devetdeset")):
        sl[word] = n
    return words, sl


_EN_NUMBERS, _SL_NUMBERS = _numbers()


def parse_number(text, lang="en"):
    """'ten', 'twenty five', '25' as an int, or None."""
    text = text.strip().lower()
    if text.isdigit() and len(text) <= 4:
        return int(text)
    table = _SL_NUMBERS if lang == "sl" else _EN_NUMBERS
    return table.get(text)


_FILLER = re.compile(r"\b(?:please|hey|ok trstenko|trstenko|prosim|hej)\b")
_PUNCT = re.compile(r"[^\w\s%]", re.UNICODE)


def normalise(text, lang="en"):
    text = text.lower().replace("’", "'")
    text = _PUNCT.sub(" ", text.replace("'", ""))
    text = _FILLER.sub(" ", text)
    return " ".join(text.split())


_N = r"(?P<n>[\w ]+?)"
_EN_UNIT = {"second": 1, "seconds": 1, "minute": 60, "minutes": 60,
            "hour": 3600, "hours": 3600}
_SL_UNIT = {"sekundo": 1, "sekundi": 1, "sekunde": 1, "sekund": 1,
            "minuto": 60, "minuti": 60, "minute": 60, "minut": 60,
            "uro": 3600, "uri": 3600, "ure": 3600, "ur": 3600}

_PATTERNS = {
    "en": {
        VOLUME: [
            (r"(?:turn )?(?:the )?volume (?P<d>up|down)", None),
            (r"(?P<d>louder|quieter)", None),
            (r"turn it (?P<d>up|down)", None),
            (r"(?:set )?(?:the )?volume (?:to )?%s(?: percent)?" % _N, "n"),
        ],
        STOP: [(r"(?:stop|cancel|never ?mind|nothing|forget it)", None)],
        TIME: [(r"what is the time", None), (r"what time is it", None),
               (r"whats the time", None)],
        TIMER: [
            (r"(?:set )?(?:a )?timer (?:for )?%s (?P<u>seconds?|minutes?|"
             r"hours?)" % _N, "n"),
            (r"(?:set )?(?:a )?%s (?P<u>second|minute|hour) timer" % _N, "n"),
        ],
        ACK: [(r"(?:ok|okay|stop|thanks|thank you|got it|i heard you)",
               None)],
        SNOOZE: [(r"snooze(?: for %s (?P<u>minutes?|hours?))?" % _N, "n")],
    },
    "sl": {
        VOLUME: [
            (r"(?P<d>glasneje|tišje|tisje)", None),
            (r"(?:povečaj|povecaj) glasnost", "up"),
            (r"(?:zmanjšaj|zmanjsaj) glasnost", "down"),
            (r"glasnost na %s(?: odstotkov| odstotka| odstotek| %%)?" % _N,
             "n"),
        ],
        STOP: [(r"(?:ustavi|prekliči|prekliči|preklici|nič|nic|pozabi)",
                None)],
        TIME: [(r"koliko je ura", None), (r"koliko je ure", None)],
        TIMER: [(r"nastavi časovnik na %s (?P<u>\w+)" % _N, "n"),
                (r"časovnik na %s (?P<u>\w+)" % _N, "n"),
                (r"nastavi casovnik na %s (?P<u>\w+)" % _N, "n")],
        ACK: [(r"(?:v redu|ustavi|hvala|razumem)", None)],
        SNOOZE: [(r"odloži(?: za %s (?P<u>\w+))?" % _N, "n"),
                 (r"odlozi(?: za %s (?P<u>\w+))?" % _N, "n")],
    },
}
_COMPILED = {
    lang: {name: [(re.compile("^" + p + "$"), kind) for p, kind in rows]
           for name, rows in groups.items()}
    for lang, groups in _PATTERNS.items()}


class Intent:
    """What was asked. `tool` and `args` are for the registry; the rest are
    handled by the worker."""

    __slots__ = ("name", "tool", "args")

    def __init__(self, name, tool=None, args=None):
        self.name = name
        self.tool = tool
        self.args = args or {}

    def __repr__(self):
        return "Intent(%s %s)" % (self.name, self.args)


def match(text, lang="en", alert_window=False):
    """The `Intent` this whole sentence is, or None to let the model have it."""
    lang = lang if lang in _COMPILED else "en"
    text = normalise(text, lang)
    if not text:
        return None
    groups = _COMPILED[lang]
    order = (VOLUME, TIMER, TIME)
    if alert_window:
        # While something is going off, "stop" means that, and is tried first.
        order = (SNOOZE, ACK) + order
    order += (STOP,)
    for name in order:
        for pattern, kind in groups[name]:
            found = pattern.match(text)
            if not found:
                continue
            intent = _build(name, found, kind, lang)
            if intent is not None:
                return intent
    return None


def _build(name, found, kind, lang):
    groups = found.groupdict()
    if name == VOLUME:
        if kind == "n":
            n = parse_number(groups["n"], lang)
            if n is None or not 0 <= n <= 100:
                return None
            return Intent(name, "set_volume", {"level": n})
        direction = kind or groups.get("d")
        direction = {"louder": "up", "quieter": "down", "glasneje": "up",
                     "tišje": "down", "tisje": "down"}.get(direction,
                                                             direction)
        return Intent(name, "set_volume", {"change": direction})
    if name == TIMER:
        n = parse_number(groups["n"], lang)
        units = _SL_UNIT if lang == "sl" else _EN_UNIT
        unit = units.get(groups.get("u", ""))
        if n is None or unit is None or n < 1:
            return None
        return Intent(name, "set_timer", {"seconds": n * unit})
    if name == SNOOZE:
        if groups.get("n") is None:
            return Intent(name, None, {"minutes": SNOOZE_DEFAULT_MINUTES})
        n = parse_number(groups["n"], lang)
        unit = 60
        if groups.get("u", "").startswith(("hour", "ur")):
            unit = 3600
        if n is None or n < 1:
            return None
        return Intent(name, None, {"minutes": n * unit // 60})
    return Intent(name)


def clock_line(wall, tz, synced, strings, lang="en"):
    """What it says for 'what time is it', or the reason it cannot."""
    if not synced:
        return strings("time_unknown")
    local = sched_clock.to_local(wall, tz)
    return strings("time_is", time=speech.spoken_time(local, lang))


def run(intent, registry, tool_ctx, strings, wall, tz, synced=True,
        lang="en"):
    """Do it, the same way the model would, and say what to speak, or '' for
    nothing. Returns (spoken, ok)."""
    if intent.name == STOP:
        return "", True
    if intent.name == TIME:
        return clock_line(wall(), tz(), synced, strings, lang), True
    if intent.name in (ACK, SNOOZE):
        try:
            if intent.name == ACK:
                tool_ctx.command("announce.ack")
                return "", True
            tool_ctx.command("announce.snooze", intent.args)
            return strings("snoozed"), True
        except Exception as exc:
            return "", False
    result = registry.run(intent.tool, dict(intent.args), tool_ctx)
    if result.ok:
        return result.say, True
    return strings("err_generic"), False

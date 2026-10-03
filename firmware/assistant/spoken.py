"""Numbers and lengths of time as a person says them, for the lines the device
speaks itself.

A Slovenian number takes an ending that depends on what it counts and on the
case. These lengths are only ever said after "za" ("nastavljen za deset
minut"), so the accusative is the one form built here: one is "eno minuto",
two "dve minuti", three and four "tri minute", five and up "pet minut".
"""

from ..scheduler import speech


def duration(seconds, lang="en"):
    """'ten minutes', 'one hour and thirty minutes', 'forty-five seconds', or
    None for a language this does not cover."""
    if lang not in ("en", "sl"):
        return None
    seconds = max(1, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if lang == "sl":
        parts = []
        if hours:
            parts.append(_count_sl(hours, _HOUR_SL))
        if minutes:
            parts.append(_count_sl(minutes, _MINUTE_SL))
        if secs and not hours:
            parts.append(_count_sl(secs, _SECOND_SL))
        return " in ".join(parts)
    parts = []
    if hours:
        parts.append("%s hour%s" % (speech.number_en(hours),
                                    "" if hours == 1 else "s"))
    if minutes:
        parts.append("%s minute%s" % (speech.number_en(minutes),
                                      "" if minutes == 1 else "s"))
    if secs and not hours:
        parts.append("%s second%s" % (speech.number_en(secs),
                                      "" if secs == 1 else "s"))
    return " and ".join(parts)


# Accusative, as after "za": one, two, three or four, five and more.
_HOUR_SL = ("uro", "uri", "ure", "ur")
_MINUTE_SL = ("minuto", "minuti", "minute", "minut")
_SECOND_SL = ("sekundo", "sekundi", "sekunde", "sekund")


def _count_sl(n, forms):
    """'eno minuto', 'dve minuti', 'tri minute', 'pet minut', 'enaindvajset
    minut'. The noun follows the last two digits of the number: 1, 2, 3 or 4,
    else the plural genitive. All three nouns are feminine."""
    last = n % 100
    if last == 1:
        return "eno " + forms[0]
    if last == 2:
        return "dve " + forms[1]
    word = speech.number_sl(n)
    return "%s %s" % (word, forms[2] if last in (3, 4) else forms[3])


def with_preposition(phrase, lang="en"):
    """The scheduler's phrase for a time reads "six in the evening" for today
    and "tomorrow at six in the evening" for another day. A sentence wants "at"
    in front of the first and nothing in front of the second. Slovenian carries
    its own "ob", but the line reads "nastavljen za ...", and "za ob šestih"
    is not Slovenian: a time with no day in front gets "danes"."""
    if not phrase:
        return phrase
    if lang == "sl":
        if phrase.startswith(("ob ", "opoldne", "opolnoči")):
            return "danes " + phrase
        if phrase.startswith("v "):
            # "v ponedeljek ob devetih" -> "za ponedeljek ob devetih"
            return phrase[2:]
        return phrase
    if lang != "en":
        return phrase
    if phrase.startswith(("tomorrow", "on ")):
        return phrase
    return "at " + phrase

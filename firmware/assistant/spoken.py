"""Numbers and lengths of time as a person says them, for the lines the device
speaks itself. English only for lengths: a Slovenian number takes a case ending
that depends on what it counts, and a wrong ending is worse than a line that
leaves the number out (the Slovenian lines do).
"""

from ..scheduler import speech


def duration(seconds, lang="en"):
    """'ten minutes', 'one hour and thirty minutes', 'forty-five seconds', or
    None for a language this does not cover."""
    if lang != "en":
        return None
    seconds = max(1, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
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


def with_preposition(phrase, lang="en"):
    """The scheduler's phrase for a time reads "six in the evening" for today
    and "tomorrow at six in the evening" for another day. A sentence wants "at"
    in front of the first and nothing in front of the second. Slovenian carries
    its own "ob"."""
    if lang != "en" or not phrase:
        return phrase
    if phrase.startswith(("tomorrow", "on ")):
        return phrase
    return "at " + phrase

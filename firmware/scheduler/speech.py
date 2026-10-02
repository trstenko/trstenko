"""What the device says about time, in words, in English and Slovenian.

A reminder is confirmed aloud ("OK, tomorrow at seven in the morning") and read
out when it is due. Both need a time spoken the way a person says it, not
"07:00" run through a speech synthesiser.

The English is checked by the tests. **The Slovenian is hand-written and not
yet reviewed by a native speaker**: the locative hour forms and the numerals
are standard, but the choices of phrasing (when to say "pol", how to say
minutes) are mine. It wants a read-through before the Slovenian voice ships
(milestone M5), and it is written as plain tables so that review is easy.
"""

# ---- English ----

_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven",
          "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
          "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]

_MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
_DAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
            "Sunday"]


def number_en(n):
    """0 to 99 in words; anything bigger is left as digits, which a
    synthesiser reads correctly anyway."""
    n = int(n)
    if n < 0 or n > 99:
        return str(n)
    if n < 20:
        return _UNITS[n]
    tens, unit = divmod(n, 10)
    return _TENS[tens] + ("-" + _UNITS[unit] if unit else "")


def _period_en(hour):
    if 5 <= hour < 12:
        return "in the morning"
    if 12 <= hour < 18:
        return "in the afternoon"
    if 18 <= hour < 22:
        return "in the evening"
    return "at night"


def _time_en(dt):
    if dt.hour == 0 and dt.minute == 0:
        return "midnight"
    if dt.hour == 12 and dt.minute == 0:
        return "noon"
    hour = dt.hour % 12 or 12
    nxt = (dt.hour + 1) % 12 or 12
    m = dt.minute
    if m == 0:
        words = number_en(hour)
    elif m == 15:
        words = "quarter past " + number_en(hour)
    elif m == 30:
        words = "half past " + number_en(hour)
    elif m == 45:
        words = "quarter to " + number_en(nxt)
    elif m < 10:
        words = "%s oh %s" % (number_en(hour), number_en(m))
    else:
        words = "%s %s" % (number_en(hour), number_en(m))
    return "%s %s" % (words, _period_en(dt.hour))


def _day_en(local, now):
    days = (local.date() - now.date()).days
    if days == 0:
        return ""
    if days == 1:
        return "tomorrow at "
    if 1 < days < 7:
        return "on %s at " % _DAYS_EN[local.weekday()]
    return "on %d %s at " % (local.day, _MONTHS_EN[local.month - 1])


# ---- Slovenian ----

# "ob šestih": after "ob" the hour is in the locative.
_HOUR_LOC = ["dvanajstih", "eni", "dveh", "treh", "štirih", "petih", "šestih",
             "sedmih", "osmih", "devetih", "desetih", "enajstih"]
# "pol sedmih": half past six is "half of the seventh", genitive.
_HOUR_GEN = ["dvanajstih", "ene", "dveh", "treh", "štirih", "petih", "šestih",
             "sedmih", "osmih", "devetih", "desetih", "enajstih"]

_UNITS_SL = ["", "ena", "dve", "tri", "štiri", "pet", "šest", "sedem",
             "osem", "devet"]
_TEENS_SL = ["deset", "enajst", "dvanajst", "trinajst", "štirinajst",
             "petnajst", "šestnajst", "sedemnajst", "osemnajst", "devetnajst"]
_TENS_SL = ["", "", "dvajset", "trideset", "štirideset", "petdeset"]

_MONTHS_SL = ["januarja", "februarja", "marca", "aprila", "maja", "junija",
              "julija", "avgusta", "septembra", "oktobra", "novembra",
              "decembra"]
_DAYS_SL = ["v ponedeljek", "v torek", "v sredo", "v četrtek", "v petek",
            "v soboto", "v nedeljo"]


def number_sl(n):
    """1 to 59, which is what minutes need. Past twenty the unit comes first:
    enaindvajset, dvaindvajset."""
    n = int(n)
    if n < 1 or n > 59:
        return str(n)
    if n < 10:
        return _UNITS_SL[n]
    if n < 20:
        return _TEENS_SL[n - 10]
    tens, unit = divmod(n, 10)
    if not unit:
        return _TENS_SL[tens]
    unit_word = {1: "enain", 2: "dvain", 3: "triin", 4: "štiriin"}.get(
        unit, _UNITS_SL[unit] + "in")
    return unit_word + _TENS_SL[tens]


def _minutes_sl(n):
    noun = ("minuta" if n == 1 else "minuti" if n == 2
            else "minute" if n in (3, 4) else "minut")
    return "%s %s" % (number_sl(n), noun)


def _period_sl(hour):
    if 5 <= hour < 10:
        return "zjutraj"
    if 10 <= hour < 12:
        return "dopoldne"
    if 12 <= hour < 18:
        return "popoldne"
    if 18 <= hour < 22:
        return "zvečer"
    return "ponoči"


def _time_sl(dt):
    if dt.hour == 0 and dt.minute == 0:
        return "ob polnoči"
    if dt.hour == 12 and dt.minute == 0:
        return "opoldne"
    h12 = dt.hour % 12
    nxt = (dt.hour + 1) % 12
    m = dt.minute
    if m == 0:
        words = "ob %s" % _HOUR_LOC[h12]
    elif m == 30:
        words = "ob pol %s" % _HOUR_GEN[nxt]
    else:
        words = "ob %s in %s" % (_HOUR_LOC[h12], _minutes_sl(m))
    return "%s %s" % (words, _period_sl(dt.hour))


def _day_sl(local, now):
    days = (local.date() - now.date()).days
    if days == 0:
        return ""
    if days == 1:
        return "jutri "
    if days == 2:
        return "pojutrišnjem "
    if 2 < days < 7:
        return _DAYS_SL[local.weekday()] + " "
    return "%d. %s " % (local.day, _MONTHS_SL[local.month - 1])


# ---- both ----


def spoken_time(local, lang="en"):
    """'six in the evening', 'ob šestih zvečer'. `local` is a datetime in the
    owner's zone."""
    return _time_sl(local) if lang == "sl" else _time_en(local)


def spoken_when(local, now, lang="en"):
    """The day too, when it is not today: 'tomorrow at seven in the morning'.
    Both datetimes are in the owner's zone."""
    if lang == "sl":
        return _day_sl(local, now) + _time_sl(local)
    return _day_en(local, now) + _time_en(local)


def duration_words(seconds, lang="en"):
    """'ten-minute' for a timer of 600 seconds, or None when the length is not
    a whole number of one unit and the generic line reads better."""
    seconds = int(round(seconds))
    if lang == "sl":
        return None
    if seconds >= 3600 and seconds % 3600 == 0:
        return "%s-hour" % number_en(seconds // 3600)
    if seconds >= 60 and seconds % 60 == 0:
        return "%s-minute" % number_en(seconds // 60)
    if 0 < seconds < 60:
        return "%s-second" % number_en(seconds)
    # 95 seconds is "a minute and a half", and a timer line that tries to say
    # so is longer than the timer. The plain line does.
    return None


# ---- the lines it says ----


def reminder_line(text, lang="en"):
    return ("Opomnik: %s." if lang == "sl" else "Reminder: %s.") % text.rstrip(
        ".!? ")


def timer_line(text, seconds, lang="en"):
    if lang == "sl":
        return ("Časovnik za %s je potekel." % text) if text else \
            "Časovnik je potekel."
    if text:
        return "The %s timer is done." % text
    length = duration_words(seconds, lang)
    if length:
        return "Your %s timer is done." % length
    return "Your timer is done."


def backlog_line(texts, lang="en"):
    """'While I was off, you had two reminders: call mum, and take the bins
    out.'"""
    texts = [t.rstrip(".!? ") for t in texts]
    if lang == "sl":
        return "Ko me ni bilo, ste imeli opomnike: %s." % ", ".join(texts)
    if len(texts) == 1:
        return "While I was off, you had a reminder: %s." % texts[0]
    count = number_en(len(texts))
    body = ", ".join(texts[:-1]) + ", and " + texts[-1] \
        if len(texts) > 2 else "%s, and %s" % (texts[0], texts[1])
    return "While I was off, you had %s reminders: %s." % (count, body)


def interrupted_line(labels, lang="en"):
    """Timers a restart cut off. `labels` are their texts, '' when unlabelled."""
    if lang == "sl":
        return "Časovnik, ki ste ga nastavili, je prekinil ponovni zagon."
    if len(labels) == 1:
        if labels[0]:
            return ("The timer you set for the %s was interrupted by a "
                    "restart." % labels[0])
        return "A timer you set was interrupted by a restart."
    return "Some timers you set were interrupted by a restart."

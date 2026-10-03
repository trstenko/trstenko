"""Catching a promise with nothing behind it.

A model sometimes answers "I'll set that" or "Zbudil te bom" and calls no tool.
Nothing was set, and the owner finds out when the timer never rings. The first
evaluation saw it three times in forty English sentences, and in Slovenian on
every repeat of some (plan 08). A prompt can make it rarer; it cannot make it
impossible.

So the turn loop checks what the model is about to say. While no tool has
succeeded in the turn, a sentence that sounds like "it is done" or "I will do
it" is held back instead of spoken (`looks_like_promise`). If the step ends
with a tool call, the sentence was true and is spoken. If it ends with none,
the model is told once that nothing happened and asked to call the tool; if it
still does not, the device says plainly that it could not do it.

The patterns are about the device's own jobs only: setting, reminding,
timing, waking, remembering, cancelling, changing the volume. "I will" on its
own is not one. They err towards holding a sentence back for a moment over
letting a false "done" out: a held sentence that turns out to be true is only
late.

Slovenian patterns were written with the phrases the evaluation recorded
("To bom uredil", "Zbudil te bom", "To si bom zapomnil"); like the rest of the
device's Slovenian they want a native speaker's eye.
"""

import re

_PATTERNS = {
    "en": (
        # "I'll set that", "I will remind you", "I'm going to wake you"
        r"\bI(?:'ll| will|'m going to| am going to) (?:set|start|remind|"
        r"remember|save|note|cancel|delete|forget|wake|time|turn|change|"
        r"lower|raise|do that|do it|take care|sort)\b",
        # "I've set", "I have saved"
        r"\bI(?:'ve| have) (?:set|started|saved|noted|cancelled|canceled|"
        r"deleted|forgotten|turned|changed|lowered|raised|added|scheduled)\b",
        # "Setting that timer now", "Starting a timer"
        r"\b(?:setting|starting|saving|cancelling|canceling|scheduling) "
        r"(?:that|the|a|an|your|it)\b",
        # "Timer set", "Reminder is set", "Volume is now"
        r"\b(?:timer|reminder|alarm)s? (?:is |has been |are |was )?"
        r"(?:set|started|saved|cancelled|canceled|scheduled|added)\b",
        r"\b(?:will do|consider it done)\b",
        # "Done." on its own, or "Okay, done."
        r"^(?:all |okay,? |ok,? )?done\b",
        r"\bI(?:'ll| will) (?:wake|remind) you\b",
        # A bare yes. "Okay." as the whole answer to "wake me in an hour" is
        # a promise too; as the answer to "thanks" it is not, which is why
        # the model gets a second go before anything is said about failing.
        r"^(?:ok(?:ay)?|sure|alright|all right|certainly|of course|got it|"
        r"right|i will)[.!]?$",
    ),
    "sl": (
        # "nastavil sem", "nastavljen", "nastavljam", "bom nastavil"
        r"\bnastav(?:il|ila|ljen|ljena|ljeno|ljam|im)\b",
        # "opomnil te bom", "te bom opomnil", "opomnim te"
        r"\b(?:opomnil|opomnila|opomnim)\b",
        # "zbudil te bom", "budim te", "zbudim te"
        r"\b(?:zbudil|zbudila|zbudim|budim)\b",
        # "zapomnil sem si", "to si bom zapomnil", "shranjeno", "zapisano"
        r"\b(?:zapomnil|zapomnila|shranil|shranila|shranjen[oa]?|zapisan[oa]?)"
        r"\b",
        # "preklical", "izbrisan", "odstranjen"
        r"\b(?:preklical|preklicala|preklican[oa]?|izbrisal|izbrisala|"
        r"izbrisan[oa]?|odstranjen[oa]?)\b",
        # "to bom uredil", "to bo urejeno", "urejeno"
        r"\b(?:uredil|uredila|urejeno)\b",
        r"\b(?:znižal|zvišal|povečal|zmanjšal)a?\b",
        # "Bom.", "Bom, takoj uredim.", "Prav.", "V redu." (seen live for
        # "zbudi me čez uro in pol"): a yes with nothing behind it.
        r"^bom\b",
        r"\b(?:uredim|poskrbim|naredim)\b",
        r"^(?:prav|velja|v redu|seveda|okej|ok|dogovorjeno)[.!]?$",
    ),
}

_COMPILED = {lang: tuple(re.compile(p, re.IGNORECASE) for p in patterns)
             for lang, patterns in _PATTERNS.items()}

# What the model is told when it promised and called nothing. English, like
# the rest of the prompt; it answers the user in their language.
NUDGE = ("You said you would do that, or that it is done, but you called no "
         "tool, so nothing has happened. Call the tool now. If something you "
         "need is missing, ask for it. If you cannot do it, say so plainly. "
         "If the user did not ask you to do anything, just answer them.")


def looks_like_promise(sentence, lang="en"):
    """Does this sentence claim one of the device's jobs is done or will be?
    A question never does: "Shall I set a timer?" promises nothing."""
    text = (sentence or "").strip()
    if not text or text.rstrip("\"')”’ ").endswith("?"):
        return False
    # Both languages are tried: a model sometimes answers in the other one.
    order = (lang, "en") if lang in _COMPILED and lang != "en" else ("en",)
    return any(p.search(text) for name in order for p in _COMPILED[name])

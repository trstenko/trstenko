"""Blanking API keys out of anything that is about to be written down.

The rule is that a key never appears in a log line, an event, an API response
or a tool result (plan 06). Code that handles keys is careful, and this is for
the day somebody is not: an exception message that quotes a request header, a
debug line that prints a config.

Two ways to recognise a key. By shape, for the providers whose keys have one
(`sk-...`, `AIza...`, `xai-...`, `gsk_...`), which catches a key this process
never stored. And by value, for every key `Secrets` has actually loaded, which
catches a provider whose keys have no recognisable shape. The second one is why
this module holds a registry: the log handlers are set up long before the
secrets are read, and neither knows about the other.
"""

import logging
import re

MASK = "[key hidden]"

# Long enough that ordinary text ("sk-learn", "gsk_") is left alone, short
# enough that no real key slips under it.
_SHAPES = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{16,}"
    r"|AIza[0-9A-Za-z_\-]{20,}"
    r"|xai-[A-Za-z0-9]{16,}"
    r"|gsk_[A-Za-z0-9]{16,})")

_known = set()


def remember(value):
    """A key the process now holds. Anything this short is not a key and would
    blank half the log."""
    if value and len(value) >= 8:
        _known.add(value)


def forget(value):
    _known.discard(value)


def redact(text):
    if not isinstance(text, str):
        return text
    for value in sorted(_known, key=len, reverse=True):
        if value in text:
            text = text.replace(value, MASK)
    return _SHAPES.sub(MASK, text)


class RedactFilter(logging.Filter):
    """Goes on the handlers, not the logger: a filter on a logger does not see
    the records its children emit."""

    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = redact(
                logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True

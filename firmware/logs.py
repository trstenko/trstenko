"""Where the firmware's log lines go.

Every module logs through the standard library under `trstenko.<module>`, and
this sets up two places for those lines to land:

- **stdout**, which systemd sends to the journal. The journal is held in RAM
  (plan 06), so INFO is fine to be generous with and the SD card is not
  touched. There is no timestamp: journald adds its own.
- **a callback**, which is what `Device(on_log=...)` always was. The emulator
  shows these lines in its window, and it should see the scheduler's and the
  storage layer's too, not only the device's.

INFO is one line per thing that happened: a wake word, a reminder firing, a
setting changing, an error class. DEBUG is off unless `TRSTENKO_LOG=debug`.
Audio, API keys and the words of a conversation are never logged (plan 01);
the filter that blanks anything shaped like a key is attached to these
handlers, because a filter on a logger does not see its children's records.
"""

import logging
import os
import sys

ROOT = "trstenko"
FORMAT = "%(levelname)s %(name)s: %(message)s"

# Without a handler, the standard library prints warnings to stderr as a last
# resort, bare. A library that logs should not do that behind the caller's back.
logging.getLogger(ROOT).addHandler(logging.NullHandler())


class CallbackHandler(logging.Handler):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self._trstenko = True

    def emit(self, record):
        # `Device.log` has already handed its own lines to the callback.
        if getattr(record, "via_callback", False):
            return
        try:
            line = record.getMessage()
            name = record.name.split(".")[-1]
            if name != "device":
                line = "%s: %s" % (name, line)
            self.callback(line)
        except Exception:
            self.handleError(record)


def setup(stream=None, level=None, callback=None, filters=None):
    """Install the handlers, replacing any this module installed before, so a
    process that restarts its device in place does not log everything twice.
    `stream=False` means no stdout handler. Every handler gets the filter that
    blanks API keys unless `filters` says otherwise."""
    logger = logging.getLogger(ROOT)
    teardown()
    if filters is None:
        from .storage.redact import RedactFilter

        filters = (RedactFilter(),)

    if level is None:
        level = logging.DEBUG if os.environ.get(
            "TRSTENKO_LOG", "").lower() == "debug" else logging.INFO
    logger.setLevel(level)

    handlers = []
    if stream is not False:
        out = logging.StreamHandler(stream or sys.stdout)
        out.setFormatter(logging.Formatter(FORMAT))
        out._trstenko = True
        handlers.append(out)
    if callback is not None:
        handlers.append(CallbackHandler(callback))

    for handler in handlers:
        for flt in filters:
            handler.addFilter(flt)
        logger.addHandler(handler)
    return logger


def teardown():
    logger = logging.getLogger(ROOT)
    for handler in list(logger.handlers):
        if getattr(handler, "_trstenko", False):
            logger.removeHandler(handler)

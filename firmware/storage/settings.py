"""The owner's settings: defaults, validation, and an atomic write.

`settings.json` already exists -- `updates.config.UpdateConfig.load` reads its
"updates" section -- and this extends the same file with the sections of plan
06. Two things follow from sharing it:

- **What this does not understand, it keeps.** A device that was rolled back
  from a release with a newer setting must not lose that setting by saving the
  older release's idea of the file. Unknown sections and keys are carried
  through untouched, and the "updates" section is carried through verbatim.
- **Every value has a default, and a broken file is not an error.** A settings
  file truncated by a power cut gives the defaults, and says so in the log,
  rather than a device that will not boot.

Changes come in as a patch, `{"voice": {"volume": 0.4}}`. The patch is checked
as a whole before anything changes, so a request with one bad value changes
nothing, and every complaint comes back at once in words the page can show.
Only the main thread applies a patch (plan 01).
"""

import copy
import json
import logging
import os
import re

from . import atomic

log = logging.getLogger("trstenko.settings")

SETTINGS_FILE = "settings.json"

PROVIDERS = ("gemini", "openai", "xai", "groq", "ollama")

DEFAULTS = {
    "updates": {"channel": "stable"},
    "owner": {"name": "", "language": "en", "timezone": "Europe/Ljubljana",
              "location": ""},
    "assistant": {"provider": "gemini", "model": "", "fallback_provider": "",
                  # Who turns speech into text when the chosen provider cannot
                  # (Ollama, Grok): empty means the first provider with a key
                  # that can (plan 04).
                  "stt_provider": "",
                  # Whether the key is on a provider's free tier. The device
                  # cannot tell from the key, and a wrong guess is a budget
                  # that either stops a free user or never stops a paying one,
                  # so it is the owner's say-so and defaults to counting the
                  # cost (plan 05).
                  "free_tier": False,
                  "conversation_log": False, "memories_in_prompt_tokens": 400},
    "voice": {"piper_voice": "en_US-ljspeech-medium", "volume": 0.6,
              "barge_in": False},
    "audio": {"vad_cutoff": 0.3, "wake_sensitivity": "normal"},
    "quiet": {"enabled": True, "from": "22:00", "to": "07:00", "volume": 0.4},
    "screen": {"sleep_after_min": 10, "night_off": True},
    "budget": {"monthly_usd": 5.0, "daily_usd": None, "daily_turns": None,
               "max_output_tokens": 300, "max_tool_steps": 4},
    # Whether the screen may show the pairing PIN on a long volume press and
    # after joining Wi-Fi. An owner who chose their own PIN can turn it off.
    # The setup card always shows it: setup makes a new random PIN, and a PIN
    # nobody can read is a device nobody can pair with.
    "security": {"show_pin": True},
    "prices_override": {},
}

# Sections a patch may not touch. The update channel has its own page and its
# own rules (`updates/`), and changing it here would only change the file, not
# the running updater, which reads it once at boot.
READ_ONLY = ("updates",)

_CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class SettingsError(ValueError):
    """The patch was refused. The message is for the person who sent it."""


# ---- one rule per setting ----
#
# (kind, argument...). Kept as data so the validation, the defaults and the
# page's idea of what is allowed cannot drift apart.

RULES = {
    ("owner", "name"): ("text", 40),
    ("owner", "language"): ("choice", ("en", "sl")),
    ("owner", "timezone"): ("timezone",),
    ("owner", "location"): ("text", 80),

    ("assistant", "provider"): ("choice", PROVIDERS),
    ("assistant", "model"): ("text", 80),
    ("assistant", "fallback_provider"): ("choice", ("",) + PROVIDERS),
    ("assistant", "stt_provider"): ("choice", ("",) + PROVIDERS),
    ("assistant", "free_tier"): ("bool",),
    ("assistant", "conversation_log"): ("bool",),
    ("assistant", "memories_in_prompt_tokens"): ("int", 0, 2000),

    ("voice", "piper_voice"): ("text", 80),
    ("voice", "volume"): ("float", 0.0, 1.0),
    ("voice", "barge_in"): ("bool",),

    ("audio", "vad_cutoff"): ("float", 0.05, 0.95),
    ("audio", "wake_sensitivity"): ("choice", ("low", "normal", "high")),

    ("quiet", "enabled"): ("bool",),
    ("quiet", "from"): ("clock",),
    ("quiet", "to"): ("clock",),
    ("quiet", "volume"): ("float", 0.0, 1.0),

    ("security", "show_pin"): ("bool",),

    ("screen", "sleep_after_min"): ("int", 1, 1440),
    ("screen", "night_off"): ("bool",),

    ("budget", "monthly_usd"): ("float?", 0.0, 100000.0),
    ("budget", "daily_usd"): ("float?", 0.0, 100000.0),
    ("budget", "daily_turns"): ("int?", 1, 1000000),
    ("budget", "max_output_tokens"): ("int", 16, 4096),
    ("budget", "max_tool_steps"): ("int", 1, 8),
}


def known_timezone(name):
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return True
    except Exception:
        return False


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and abs(value) != float("inf"))


def check(path, rule, value):
    """Returns (clean_value, None) or (None, complaint)."""
    kind = rule[0]
    nullable = kind.endswith("?")
    kind = kind.rstrip("?")

    if value is None and nullable:
        return None, None

    if kind == "bool":
        if isinstance(value, bool):
            return value, None
        return None, "%s must be true or false" % path

    if kind == "text":
        if not isinstance(value, str):
            return None, "%s must be text" % path
        value = value.strip()
        if len(value) > rule[1]:
            return None, "%s must be at most %d characters" % (path, rule[1])
        if any(ord(c) < 32 for c in value):
            return None, "%s must not contain control characters" % path
        return value, None

    if kind == "choice":
        if value in rule[1]:
            return value, None
        options = ", ".join(repr(o) for o in rule[1] if o != "")
        return None, "%s must be one of %s" % (path, options)

    if kind == "clock":
        if isinstance(value, str) and _CLOCK.match(value):
            return value, None
        return None, "%s must be a time like 22:00" % path

    if kind == "timezone":
        if isinstance(value, str) and known_timezone(value):
            return value, None
        return None, "%s is not a timezone this device knows" % path

    if kind in ("int", "float"):
        if not _number(value) or (kind == "int" and int(value) != value):
            what = "a whole number" if kind == "int" else "a number"
            return None, "%s must be %s%s" % (
                path, what, " or empty" if nullable else "")
        low, high = rule[1], rule[2]
        if not low <= value <= high:
            return None, "%s must be between %s and %s" % (path, low, high)
        return (int(value) if kind == "int" else float(value)), None

    raise AssertionError("no such rule: %s" % kind)


def _check_prices(value):
    """`{"openai/gpt-5-mini": {"in": 0.3}}`: per-million-token prices the owner
    wants used instead of the shipped ones (plan 05)."""
    if not isinstance(value, dict):
        return None, "prices_override must be a set of model prices"
    clean = {}
    for model, prices in value.items():
        if not isinstance(model, str) or not isinstance(prices, dict):
            return None, "prices_override must map a model to its prices"
        row = {}
        for field, amount in prices.items():
            if not isinstance(field, str) or not _number(amount) \
                    or amount < 0:
                return None, ("prices_override %s: %s must be a price, "
                              "zero or more" % (model, field))
            row[field] = float(amount)
        clean[model] = row
    return clean, None


class Settings:
    def __init__(self, path):
        self.path = path
        self.data = copy.deepcopy(DEFAULTS)
        # What the file had that we have no rule for, and its "updates"
        # section, carried through on save.
        self._extra = {}
        self.repaired = []

    # ---- reading ----

    @classmethod
    def load(cls, path):
        """Never raises. A missing file is a new device; an unreadable one is
        defaults, with the reason in `repaired` and the log."""
        self = cls(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return self
        except (OSError, ValueError) as exc:
            self.repaired.append("settings file unreadable (%s); using the "
                                 "defaults" % exc)
            log.warning(self.repaired[-1])
            return self
        if not isinstance(raw, dict):
            self.repaired.append("settings file is not an object; using the "
                                 "defaults")
            log.warning(self.repaired[-1])
            return self

        for section, body in raw.items():
            if section == "updates":
                if isinstance(body, dict):
                    self.data["updates"] = copy.deepcopy(body)
                continue
            if section == "prices_override":
                clean, why = _check_prices(body)
                if why:
                    self.repaired.append("ignored %s" % why)
                else:
                    self.data["prices_override"] = clean
                continue
            if section not in DEFAULTS or not isinstance(body, dict):
                self._extra[section] = body
                continue
            for key, value in body.items():
                rule = RULES.get((section, key))
                if rule is None:
                    self._extra.setdefault(section, {})[key] = value
                    continue
                if value == DEFAULTS[section][key]:
                    # A default is valid by definition. This matters for the
                    # timezone: a PC with no tz database cannot check any name,
                    # and must still be able to read back the file it wrote.
                    continue
                clean, why = check("%s.%s" % (section, key), rule, value)
                if why:
                    self.repaired.append("ignored %s" % why)
                else:
                    self.data[section][key] = clean
        for line in self.repaired:
            log.warning(line)
        return self

    def get(self, section, key):
        return self.data[section][key]

    def public(self):
        """Everything the page may see. There are no secrets in this file,
        which is the point of keeping them in another."""
        return copy.deepcopy(self.data)

    # ---- changing ----

    def validate(self, patch):
        """The cleaned patch, or SettingsError listing everything wrong with
        it. Changes nothing."""
        if not isinstance(patch, dict) or not patch:
            raise SettingsError("send the settings to change, such as "
                                '{"voice": {"volume": 0.4}}')
        problems = []
        clean = {}
        for section, body in patch.items():
            if section in READ_ONLY:
                problems.append("%s settings cannot be changed here" % section)
                continue
            if section == "prices_override":
                value, why = _check_prices(body)
                if why:
                    problems.append(why)
                else:
                    clean[section] = value
                continue
            if section not in DEFAULTS:
                problems.append("unknown setting: %s" % section)
                continue
            if not isinstance(body, dict):
                problems.append("%s must be a set of settings" % section)
                continue
            for key, value in body.items():
                rule = RULES.get((section, key))
                if rule is None:
                    problems.append("unknown setting: %s.%s" % (section, key))
                    continue
                value, why = check("%s.%s" % (section, key), rule, value)
                if why:
                    problems.append(why)
                else:
                    clean.setdefault(section, {})[key] = value
        if problems:
            raise SettingsError("; ".join(problems))
        return clean

    def apply(self, clean):
        """Apply a patch `validate` returned. Returns the dotted names that
        actually changed, so the caller tells only the services that care."""
        changed = []
        for section, body in clean.items():
            if section == "prices_override":
                if self.data[section] != body:
                    self.data[section] = body
                    changed.append(section)
                continue
            for key, value in body.items():
                if self.data[section][key] != value:
                    self.data[section][key] = value
                    changed.append("%s.%s" % (section, key))
        return changed

    # ---- writing ----

    def save(self):
        out = copy.deepcopy(self._extra)
        for section, body in self.data.items():
            if isinstance(body, dict) and isinstance(out.get(section), dict):
                merged = dict(out[section])
                merged.update(body)
                out[section] = merged
            else:
                out[section] = copy.deepcopy(body)
        atomic.write_json(self.path, out)


def default_path(data_root):
    return os.path.join(data_root, SETTINGS_FILE)

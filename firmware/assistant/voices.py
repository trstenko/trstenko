"""The voices Trstenko can speak with, as data.

There will be more than one voice of Trstenko, in more than one language and
eventually in more than one character, so a voice is a row in `voices.json` and
never a constant. Nothing outside this module and `tts.py` knows that the engine
is Piper: a voice names its `engine`, its model files, its sample rate, its
speed, and the licence and credit that go with it. Adding a voice is a row here
and the files in the voices folder.

The setting `voice.piper_voice` holds a voice's id. (The name is the plan's and
stays, so a release that is rolled back still reads it.) An id that is not in
the registry, or whose model files are missing, falls back to the default for the
language, with a log line, and never to silence: a device that cannot find its
chosen voice should still be able to say so.

`ships` is false for a voice that must not be in a unit that is sold (a research
licence, for instance). The registry will not choose one as a default, and the
about page lists `credits()`, which is the attribution some licences require.

The files are large (about 63MB for a medium English voice) and live outside the
code: in the voices folder, `TRSTENKO_VOICES` or `<data root>/voices`. They are
fetched by `tools/voices/`, never committed.
"""

import json
import logging
import os

log = logging.getLogger("trstenko.voices")

HERE = os.path.dirname(os.path.abspath(__file__))
VOICES_FILE = os.path.join(HERE, "voices.json")

ENGINES = ("piper",)
LANGUAGES = ("en", "sl")
FIELDS = {"label", "languages", "engine", "model", "config", "sample_rate",
          "speaker", "speed", "licence", "credit", "ships", "default_for"}
# Speed is a multiplier on the speaking rate; Piper wants its inverse.
MIN_SPEED, MAX_SPEED = 0.5, 2.0


class VoiceError(ValueError):
    pass


class Voice:
    def __init__(self, voice_id, row):
        self.id = voice_id
        self.label = row["label"]
        self.languages = tuple(row["languages"])
        self.engine = row["engine"]
        self.model = row["model"]
        self.config = row["config"]
        self.sample_rate = row["sample_rate"]
        self.speaker = row["speaker"]
        self.speed = float(row["speed"])
        self.licence = row["licence"]
        self.credit = row["credit"]
        self.ships = row["ships"]
        self.default_for = tuple(row["default_for"])

    @property
    def length_scale(self):
        return 1.0 / self.speed

    def files(self, directory):
        return (os.path.join(directory, self.model),
                os.path.join(directory, self.config))

    def present(self, directory):
        return all(os.path.isfile(p) for p in self.files(directory))


def check(rows):
    """Every problem with a registry, as sentences; empty if it is sound."""
    if not isinstance(rows, dict) or not rows:
        return ["the voice registry must be an object with a row each"]
    problems = []
    defaults = {}
    for vid, row in rows.items():
        where = "voice %s" % vid
        if not isinstance(row, dict):
            problems.append("%s: must be an object" % where)
            continue
        extra, missing = set(row) - FIELDS, FIELDS - set(row)
        if extra:
            problems.append("%s: unknown field(s) %s" % (where, sorted(extra)))
        if missing:
            problems.append("%s: missing field(s) %s"
                            % (where, sorted(missing)))
            continue
        if row["engine"] not in ENGINES:
            problems.append("%s: engine must be one of %s" % (where, ENGINES))
        if (not isinstance(row["languages"], list) or not row["languages"]
                or not set(row["languages"]) <= set(LANGUAGES)):
            problems.append("%s: languages must be from %s" % (where,
                                                              LANGUAGES))
        for field in ("model", "config", "label", "licence"):
            if not isinstance(row[field], str) or not row[field].strip():
                problems.append("%s: %s must be text" % (where, field))
        for field in ("model", "config"):
            value = row[field]
            if isinstance(value, str) and (os.path.basename(value) != value
                                           or value.startswith(".")):
                problems.append("%s: %s must be a plain file name" % (where,
                                                                      field))
        rate = row["sample_rate"]
        if isinstance(rate, bool) or not isinstance(rate, int) \
                or not 8000 <= rate <= 48000:
            problems.append("%s: sample_rate must be a rate in Hz" % where)
        speed = row["speed"]
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) \
                or not MIN_SPEED <= speed <= MAX_SPEED:
            problems.append("%s: speed must be between %s and %s"
                            % (where, MIN_SPEED, MAX_SPEED))
        if row["speaker"] is not None and (
                isinstance(row["speaker"], bool)
                or not isinstance(row["speaker"], int)):
            problems.append("%s: speaker must be a number or null" % where)
        if not isinstance(row["ships"], bool):
            problems.append("%s: ships must be true or false" % where)
        if not isinstance(row["credit"], str):
            problems.append("%s: credit must be text" % where)
        if (not isinstance(row["default_for"], list)
                or not set(row["default_for"]) <= set(row["languages"] or [])):
            problems.append("%s: default_for must be among its languages"
                            % where)
        else:
            for lang in row["default_for"]:
                if not row["ships"]:
                    problems.append("%s: a voice that does not ship cannot "
                                    "be the default for %s" % (where, lang))
                if lang in defaults:
                    problems.append("%s and %s are both the default for %s"
                                    % (defaults[lang], vid, lang))
                defaults[lang] = vid
    for lang in LANGUAGES:
        if lang not in defaults:
            problems.append("no voice is the default for %s" % lang)
    return problems


class VoiceRegistry:
    def __init__(self, rows):
        problems = check(rows)
        if problems:
            raise VoiceError("; ".join(problems))
        self._voices = {vid: Voice(vid, row) for vid, row in rows.items()}

    @classmethod
    def load(cls, path=None):
        try:
            with open(path or VOICES_FILE, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
        except (OSError, ValueError) as exc:
            raise VoiceError("voices.json could not be read: %s" % exc)
        return cls(rows)

    def ids(self):
        return list(self._voices)

    def get(self, voice_id):
        return self._voices.get(voice_id)

    def default_for(self, language):
        for voice in self._voices.values():
            if language in voice.default_for:
                return voice
        return None

    def for_language(self, language):
        return [v for v in self._voices.values() if language in v.languages]

    def choose(self, voice_id, language, directory):
        """The voice to use, and a note when it is not the one asked for. Never
        None while any voice's files are present; None only when there is
        nothing to speak with at all."""
        voice = self.get(voice_id)
        note = ""
        if voice is None:
            note = "voice %r is not in the registry" % (voice_id,)
        elif language not in voice.languages:
            note = "voice %s does not speak %s" % (voice.id, language)
        elif not voice.present(directory):
            note = "the files for voice %s are missing" % voice.id
        else:
            return voice, ""
        fallback = self.default_for(language)
        if fallback is not None and fallback.present(directory):
            log.warning("%s; using %s", note, fallback.id)
            return fallback, note
        for other in self._voices.values():
            if language in other.languages and other.present(directory):
                log.warning("%s; using %s", note, other.id)
                return other, note
        log.error("%s, and no voice for %s is installed", note, language)
        return None, note

    def credits(self):
        """The attribution the licences ask for, for the about page."""
        return [{"voice": v.id, "credit": v.credit, "licence": v.licence}
                for v in self._voices.values() if v.credit]


def voices_dir(data_root):
    return os.environ.get("TRSTENKO_VOICES") or os.path.join(data_root,
                                                             "voices")

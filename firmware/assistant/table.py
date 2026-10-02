"""The provider table and the price table, loaded and checked.

Providers rename their models every few months and change what a request may
carry. A release is the only way to fix that on a device that has already been
sold, so everything the model client needs to know about a provider is a row in
`providers.json`, and what a model costs is a row in `prices.json`. The client
(`provider.py`) has no `if provider == "gemini"` in it, and adding a provider is
a row here and a row in the evaluation (plan 04).

These files are read once, at boot, and a broken one must fail loudly and early:
a typo in `drop_params` that survives to the first request is a 400 from a
provider on the owner's own bill. So every field the client reads is checked
here, unknown fields are refused (they are always typos in a file that ships
with the firmware), and the same checks run in CI (`tools/check_layout.py`) so
the mistake is found before a release is cut rather than after.

Money is handled in whole millionths of a dollar (plan 05), so sums over a month
do not drift. The prices are per million tokens, which makes the price of a
token, in millionths of a dollar, the same number: no division on the way.
"""

import copy
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PROVIDERS_FILE = os.path.join(HERE, "providers.json")
PRICES_FILE = os.path.join(HERE, "prices.json")

SCHEMA_FLAVOURS = ("strict", "plain")
# How token counts come back from a streamed request: Gemini always sends them
# in the last chunk, OpenAI and its kin only when asked, and a provider that
# never does leaves the meter to estimate.
STREAM_USAGE = ("always_in_last_chunk", "stream_options", "none")
AUTH = ("bearer", "none")
# The two spellings of "how long may the reply be". A row carries exactly one,
# and the owner's `budget.max_output_tokens` replaces its value.
TOKEN_PARAMS = ("max_tokens", "max_completion_tokens")
LANGUAGES = ("en", "sl")

PROVIDER_FIELDS = {
    "label", "base_url", "allow_http", "auth", "schema_flavour",
    "audio_in_chat", "default_model", "models", "params", "drop_params",
    "stream_usage", "headers", "stt", "free_tier", "languages",
    "key_help_url", "verified",
}
REQUIRED_FIELDS = PROVIDER_FIELDS - {"allow_http"}

# A host the owner typed on the setup page, going into a URL. Letters, digits,
# dots and hyphens: nothing that can turn it into a path, a query, a user or a
# different host. The port is the provider's own, in the table's URL.
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
HEADER_PLACEHOLDERS = {"session_id"}

PRICE_FIELDS = {"in", "cached", "out", "per_minute", "per_hour"}
TOKEN_PRICE_FIELDS = {"in", "cached", "out"}
AUDIO_PRICE_FIELDS = {"per_minute", "per_hour"}


class TableError(ValueError):
    """A table is wrong, or a request asked for something it does not hold. The
    message names the provider and the field."""


# ---- providers ----

def _text(value):
    return isinstance(value, str) and value.strip() != ""


def check_provider(name, row):
    """Every problem with one provider's row, as sentences; empty if it is
    sound."""
    where = "provider %s" % name
    if not isinstance(row, dict):
        return ["%s: must be an object" % where]
    problems = []

    unknown = sorted(set(row) - PROVIDER_FIELDS)
    if unknown:
        problems.append("%s: unknown field(s) %s" % (where, ", ".join(unknown)))
    missing = sorted(REQUIRED_FIELDS - set(row))
    if missing:
        problems.append("%s: missing field(s) %s" % (where, ", ".join(missing)))
        return problems

    def bad(field, why):
        problems.append("%s: %s %s" % (where, field, why))

    if not _text(row["label"]):
        bad("label", "must be text")

    local = row.get("allow_http", False)
    if not isinstance(local, bool):
        bad("allow_http", "must be true or false")
        local = False
    url = row["base_url"]
    if not _text(url) or re.search(r"[\s?#]", url):
        bad("base_url", "must be a URL with no spaces, query or fragment")
    elif url.startswith("https://"):
        if "{" in url:
            bad("base_url", "may only hold {host} on a local provider")
    elif url.startswith("http://") and local:
        if set(_PLACEHOLDER.findall(url)) - {"host"}:
            bad("base_url", "may only hold {host}")
    else:
        bad("base_url", "must be https:// (http:// only with allow_http, "
                        "for a provider on the owner's own network)")

    if row["auth"] not in AUTH:
        bad("auth", "must be one of %s" % ", ".join(AUTH))
    elif row["auth"] == "none" and not local:
        bad("auth", "can only be none on a local provider: a key-less "
                    "request to a public service is a mistake")
    if row["schema_flavour"] not in SCHEMA_FLAVOURS:
        bad("schema_flavour", "must be one of %s" % ", ".join(SCHEMA_FLAVOURS))
    if not isinstance(row["audio_in_chat"], bool):
        bad("audio_in_chat", "must be true or false")
    if row["stream_usage"] not in STREAM_USAGE:
        bad("stream_usage", "must be one of %s" % ", ".join(STREAM_USAGE))

    models = row["models"]
    if (not isinstance(models, list) or not models
            or not all(_text(m) for m in models)
            or len(set(models)) != len(models)):
        bad("models", "must be a list of distinct model names")
    elif row["default_model"] not in models:
        bad("default_model", "must be one of its models")

    params = row["params"]
    if not isinstance(params, dict):
        bad("params", "must be an object")
        params = {}
    else:
        limits = [k for k in TOKEN_PARAMS if k in params]
        if len(limits) != 1:
            bad("params", "must hold exactly one of %s"
                % " and ".join(TOKEN_PARAMS))
        elif (isinstance(params[limits[0]], bool)
              or not isinstance(params[limits[0]], int)
              or params[limits[0]] < 1):
            bad("params", "%s must be a whole number above zero" % limits[0])
        if not all(isinstance(k, str) for k in params):
            bad("params", "names must be text")

    drop = row["drop_params"]
    if not isinstance(drop, list) or not all(_text(d) for d in drop):
        bad("drop_params", "must be a list of parameter names")
    else:
        clash = sorted(set(drop) & set(params))
        if clash:
            bad("drop_params", "also sent in params: %s" % ", ".join(clash))

    headers = row["headers"]
    if not isinstance(headers, dict) or not all(
            _text(k) and isinstance(v, str) for k, v in headers.items()):
        bad("headers", "must map header names to text")
    else:
        for value in headers.values():
            unknown_ph = set(_PLACEHOLDER.findall(value)) - HEADER_PLACEHOLDERS
            if unknown_ph:
                bad("headers", "unknown placeholder %s"
                    % ", ".join(sorted(unknown_ph)))

    stt = row["stt"]
    if stt is not None:
        if (not isinstance(stt, dict) or set(stt) != {"path", "model"}
                or not _text(stt.get("path")) or not stt["path"].startswith("/")
                or not _text(stt.get("model"))):
            bad("stt", "must be null or an object with a path starting with "
                       "/ and a model")

    free = row["free_tier"]
    if free is not None:
        if (not isinstance(free, dict) or set(free) != {"note", "quota_url"}
                or not _text(free.get("note"))
                or not str(free.get("quota_url", "")).startswith("https://")):
            bad("free_tier", "must be null or a note and an https quota_url")

    langs = row["languages"]
    if (not isinstance(langs, list) or "en" not in langs
            or not set(langs) <= set(LANGUAGES)):
        bad("languages", "must be a list of %s that includes en"
            % ", ".join(LANGUAGES))
    if not str(row["key_help_url"]).startswith("https://"):
        bad("key_help_url", "must be an https URL")
    verified = row["verified"]
    if verified is not None and not (isinstance(verified, str)
                                     and _DAY.match(verified)):
        bad("verified", "must be null or the day it was last checked live")
    return problems


class Provider:
    """One row of the table, with the questions the client asks of it."""

    def __init__(self, name, row):
        self.name = name
        self.label = row["label"]
        self.audio_in_chat = row["audio_in_chat"]
        self.default_model = row["default_model"]
        self.models = tuple(row["models"])
        self.schema_flavour = row["schema_flavour"]
        self.stream_usage = row["stream_usage"]
        self.languages = tuple(row["languages"])
        self.key_help_url = row["key_help_url"]
        self.free_tier = copy.deepcopy(row["free_tier"])
        self.stt = copy.deepcopy(row["stt"])
        self.verified = row["verified"]
        self.local = bool(row.get("allow_http", False))
        self.needs_key = row["auth"] == "bearer"
        self._base_url = row["base_url"]
        self._params = copy.deepcopy(row["params"])
        self._drop = frozenset(row["drop_params"])
        self._headers = dict(row["headers"])
        self.token_param = next(k for k in TOKEN_PARAMS if k in self._params)

    @property
    def drop_params(self):
        return self._drop

    def base_url(self, host=""):
        """The URL requests go to. A local provider's address is the owner's
        own, so it is checked here, on its way into a URL."""
        if "{host}" not in self._base_url:
            return self._base_url.rstrip("/")
        host = (host or "").strip()
        if not host:
            raise TableError("%s needs the address of the computer it runs "
                             "on" % self.label)
        if not _HOST.match(host):
            raise TableError("%s: that is not an address" % self.label)
        return self._base_url.replace("{host}", host).rstrip("/")

    def params(self, max_output_tokens=None):
        """What goes into every request besides the messages and tools, with
        the owner's reply-length cap in place of the table's default."""
        params = copy.deepcopy(self._params)
        if max_output_tokens is not None:
            params[self.token_param] = int(max_output_tokens)
        return params

    def headers(self, session_id=""):
        """Extra headers the provider wants, with placeholders filled. Never
        the credential: `provider.py` builds that itself, so it exists in one
        place only."""
        return {k: v.replace("{session_id}", session_id)
                for k, v in self._headers.items()}

    def speaks(self, language):
        return language in self.languages

    def model(self, chosen=""):
        """The owner's model if they chose one, else the table's default."""
        return chosen or self.default_model


class Providers:
    def __init__(self, rows):
        self._rows = {name: Provider(name, row) for name, row in rows.items()}

    def names(self):
        return list(self._rows)

    def __contains__(self, name):
        return name in self._rows

    def get(self, name):
        try:
            return self._rows[name]
        except KeyError:
            raise TableError("there is no provider called %r" % (name,))

    def can_transcribe(self):
        """Providers that can turn audio into text, on their own or inside the
        chat request. An Ollama owner needs one of these as a second key."""
        return [p.name for p in self._rows.values()
                if p.audio_in_chat or p.stt]


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise TableError("%s could not be read: %s"
                         % (os.path.basename(path), exc))


def check_providers(rows, known=None):
    """All the problems with a whole table. `known` is the provider names the
    settings accept; the two lists must be the same, or a choice the page
    offers has no row, or a row can never be chosen."""
    if not isinstance(rows, dict) or not rows:
        return ["the provider table must be an object with a row each"]
    problems = []
    for name, row in rows.items():
        problems += check_provider(name, row)
    if known is not None:
        for name in sorted(set(known) - set(rows)):
            problems.append("provider %s: the settings offer it but the "
                            "table has no row" % name)
        for name in sorted(set(rows) - set(known)):
            problems.append("provider %s: in the table but the settings "
                            "cannot choose it" % name)
    return problems


def load_providers(path=None, known=None):
    """The shipped table, or `path`. Raises `TableError` naming every problem."""
    rows = _read(path or PROVIDERS_FILE)
    if known is None and path is None:
        from ..storage.settings import PROVIDERS
        known = PROVIDERS
    problems = check_providers(rows, known)
    if problems:
        raise TableError("; ".join(problems))
    return Providers(rows)


# ---- prices ----

def _price_problems(where, row, extra=()):
    problems = []
    if not isinstance(row, dict):
        return ["%s: must be an object" % where]
    unknown = sorted(set(row) - PRICE_FIELDS - set(extra))
    if unknown:
        problems.append("%s: unknown price(s) %s" % (where, ", ".join(unknown)))
    for field in PRICE_FIELDS & set(row):
        value = row[field]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or value != value or value < 0):
            problems.append("%s: %s must be a price, zero or more"
                            % (where, field))
    return problems


def check_prices(data):
    if not isinstance(data, dict):
        return ["the price table must be an object"]
    problems = []
    if not _DAY.match(str(data.get("updated", ""))):
        problems.append("prices: updated must be a day like 2026-10-02")
    rate = data.get("usd_to_eur")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0:
        problems.append("prices: usd_to_eur must be a number above zero")
    models = data.get("models")
    if not isinstance(models, dict) or not models:
        return problems + ["prices: models must hold a row per model"]

    for key, row in models.items():
        where = "price %s" % key
        if "/" not in key or key.startswith("/") or key.endswith("/"):
            problems.append("%s: the name must be provider/model" % where)
        problems += _price_problems(where, row, ("free_tier", "changes"))
        if not isinstance(row, dict):
            continue
        has_tokens = bool(TOKEN_PRICE_FIELDS & set(row))
        has_audio = bool(AUDIO_PRICE_FIELDS & set(row))
        if has_tokens == has_audio:
            problems.append("%s: needs token prices (in, out) or an audio "
                            "price (per_minute, per_hour), not both or "
                            "neither" % where)
        if has_tokens and not {"in", "out"} <= set(row):
            problems.append("%s: token prices need both in and out" % where)
        if "free_tier" in row and not isinstance(row["free_tier"], bool):
            problems.append("%s: free_tier must be true or false" % where)
        changes = row.get("changes", {})
        if not isinstance(changes, dict):
            problems.append("%s: changes must map a day to new prices" % where)
            continue
        for day, new in changes.items():
            if not _DAY.match(day):
                problems.append("%s: change date %r is not a day" % (where, day))
            problems += _price_problems("%s change %s" % (where, day), new)
    return problems


def cross_check(providers, prices):
    """Every model the owner can pick without typing it has a price, and so
    does every speech-to-text model. A model with no price is a budget that
    cannot be enforced in money (plan 05), which is fine for one the owner
    typed in and wrong for one we ship. Local providers cost nothing."""
    problems = []
    for name in providers.names():
        provider = providers.get(name)
        if provider.local:
            continue
        wanted = list(provider.models)
        if provider.stt:
            wanted.append(provider.stt["model"])
        for model in wanted:
            if not prices.known(name, model):
                problems.append("provider %s: model %s has no price"
                                % (name, model))
    return problems


class Prices:
    """What things cost, with the owner's overrides and announced changes."""

    def __init__(self, data, overrides=None):
        self.updated = data["updated"]
        self.usd_to_eur = float(data["usd_to_eur"])
        self._models = copy.deepcopy(data["models"])
        self._overrides = copy.deepcopy(overrides or {})

    def with_overrides(self, overrides):
        """The same table with a different set of owner overrides, as when the
        owner changes a price on the page."""
        clone = Prices.__new__(Prices)
        clone.updated = self.updated
        clone.usd_to_eur = self.usd_to_eur
        clone._models = self._models
        clone._overrides = copy.deepcopy(overrides or {})
        return clone

    def known(self, provider, model):
        key = "%s/%s" % (provider, model)
        return key in self._models or key in self._overrides

    def row(self, provider, model, day=None):
        """The prices in force on `day` (`'2026-10-02'`), or None if the model
        is not priced. Changes already announced apply from their day; with no
        `day` none of them do. An owner override beats both, field by field."""
        key = "%s/%s" % (provider, model)
        shipped = self._models.get(key)
        override = self._overrides.get(key)
        if shipped is None and override is None:
            return None
        row = {}
        if shipped is not None:
            row = {k: v for k, v in shipped.items() if k != "changes"}
            if day:
                for when in sorted(shipped.get("changes", {})):
                    if when <= day:
                        row.update(shipped["changes"][when])
        if override:
            row.update(override)
        return row

    def free_model(self, provider, model):
        """Does this model have a free tier at all? Whether the owner is on it
        is their say-so, in the settings."""
        row = self.row(provider, model)
        return bool(row and row.get("free_tier"))

    def cost_micro(self, provider, model, input_tokens=0, cached_tokens=0,
                   output_tokens=0, audio_seconds=0.0, day=None):
        """Millionths of a dollar, rounded to whole ones, or None when the
        model has no price.

        `input_tokens` includes the cached ones and `output_tokens` includes
        the reasoning ones, as the providers report them (plan 05): the cached
        are charged at their own rate and only the rest at the input rate.
        """
        row = self.row(provider, model, day)
        if row is None:
            return None
        total = 0.0
        if "per_minute" in row or "per_hour" in row:
            if "per_minute" in row:
                total += audio_seconds / 60.0 * row["per_minute"] * 1e6
            else:
                total += audio_seconds / 3600.0 * row["per_hour"] * 1e6
        else:
            cached = max(0, min(int(cached_tokens), int(input_tokens)))
            fresh = max(0, int(input_tokens)) - cached
            total += fresh * row.get("in", 0.0)
            total += cached * row.get("cached", row.get("in", 0.0))
            total += max(0, int(output_tokens)) * row.get("out", 0.0)
        return int(total + 0.5)


def load_prices(path=None, overrides=None):
    data = _read(path or PRICES_FILE)
    problems = check_prices(data)
    if problems:
        raise TableError("; ".join(problems))
    return Prices(data, overrides)

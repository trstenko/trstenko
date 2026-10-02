"""API keys, kept apart from everything else.

They live in `secrets.json`, mode 0600, so that a settings export, a support
log or a screenshot of the settings page never contains one. The file is not
encrypted: on a Pi there is nowhere to keep the encryption key that is safer
than the file itself, and encrypting it would be theatre. What protects a key
is the file mode, the service user, and a factory reset deleting it.

**Write-only.** The page can set a key and can be told that one is set and what
its last four characters are. Nothing hands the key back. `key()` exists for the
one module that has to put it in a request header, and for nothing else.

This module never puts a key in an error message either: a complaint about a
key that quotes the key is a leak.

This file shares its name with the standard library's `secrets`. That is the
name the plan gave it and it is harmless inside the package, since imports here
are absolute.
"""

import json
import logging
import os

from . import atomic
from . import redact
from .settings import PROVIDERS

log = logging.getLogger("trstenko.secrets")

SECRETS_FILE = "secrets.json"

# Providers that take a key. Ollama takes a host instead.
KEYED = tuple(p for p in PROVIDERS if p != "ollama")

MIN_KEY = 8
MAX_KEY = 512


class SecretsError(ValueError):
    pass


class Secrets:
    def __init__(self, path):
        self.path = path
        self._keys = {}
        self.ollama_host = ""
        self._checked = {}

    @classmethod
    def load(cls, path):
        """Never raises: a missing or unreadable file is a device with no keys
        yet, which is a state the page already knows how to show."""
        self = cls(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return self
        except (OSError, ValueError) as exc:
            log.warning("secrets file unreadable (%s); no keys loaded", exc)
            return self
        if not isinstance(raw, dict):
            return self
        keys = raw.get("keys")
        for provider, value in (keys.items() if isinstance(keys, dict)
                                else ()):
            if provider in KEYED and isinstance(value, str) and value:
                self._keys[provider] = value
                redact.remember(value)
        host = raw.get("ollama_host")
        if isinstance(host, str):
            self.ollama_host = host
        checked = raw.get("checked")
        if isinstance(checked, dict):
            self._checked = {k: v for k, v in checked.items()
                             if k in KEYED and isinstance(v, str)}
        return self

    # ---- keys ----

    def set_key(self, provider, key):
        """Store a key, or remove it with an empty one. Returns the same status
        the page gets: whether it is set, and its last four characters."""
        if provider not in KEYED:
            raise SecretsError("there is no key to set for %r" % (provider,))
        if not isinstance(key, str):
            raise SecretsError("the key must be text")
        key = key.strip()

        old = self._keys.pop(provider, None)
        if old:
            redact.forget(old)
        self._checked.pop(provider, None)

        if key:
            if not MIN_KEY <= len(key) <= MAX_KEY:
                self._restore(provider, old)
                raise SecretsError("that does not look like an API key")
            if any(c.isspace() or ord(c) < 32 for c in key):
                self._restore(provider, old)
                raise SecretsError("an API key has no spaces or line breaks "
                                   "in it")
            self._keys[provider] = key
            redact.remember(key)
        self.save()
        return self.status()[provider]

    def _restore(self, provider, old):
        if old:
            self._keys[provider] = old
            redact.remember(old)

    def key(self, provider):
        """For the code that builds a request header. Not for the page, a log,
        an event or a tool result."""
        return self._keys.get(provider, "")

    def has_key(self, provider):
        return bool(self._keys.get(provider))

    def last4(self, provider):
        key = self._keys.get(provider, "")
        # A short key would be mostly on show.
        return key[-4:] if len(key) >= 12 else ""

    def mark_checked(self, provider, when):
        """Remember that a test request with this key worked, as a timestamp
        the page can show."""
        if provider in self._keys:
            self._checked[provider] = str(when)
            self.save()

    def set_ollama_host(self, host):
        host = (host or "").strip()
        if host and (len(host) > 255 or not all(
                c.isalnum() or c in ".:-_" for c in host)):
            raise SecretsError("that is not a host name or address")
        self.ollama_host = host
        self.save()

    def status(self):
        """What the page may know: never a key."""
        out = {p: {"set": self.has_key(p), "last4": self.last4(p),
                   "checked_ok": self._checked.get(p)} for p in KEYED}
        out["ollama"] = {"set": bool(self.ollama_host),
                         "host": self.ollama_host}
        return out

    def clear(self):
        """Factory reset."""
        for value in self._keys.values():
            redact.forget(value)
        self._keys = {}
        self._checked = {}
        self.ollama_host = ""
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass

    # ---- writing ----

    def save(self):
        atomic.write_json(self.path, {
            "keys": dict(self._keys),
            "ollama_host": self.ollama_host,
            "checked": dict(self._checked),
        }, mode=0o600)


def default_path(data_root):
    return os.path.join(data_root, SECRETS_FILE)

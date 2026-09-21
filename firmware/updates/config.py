"""Update settings, and where the updater keeps its files.

Defaults are what an unconfigured device does. The web UI writes the rest into
`settings.json` under the data root, so a user can move to the beta channel or
turn automatic checking off without editing code.
"""

import json
import os

DEFAULT_DATA_ROOT = os.environ.get("TRSTENKO_DATA", "/var/lib/trstenko")
# Releases live apart from settings on purpose: rolling a release back
# must not roll back what the user told the device.
DEFAULT_INSTALL_ROOT = os.environ.get("TRSTENKO_ROOT", "/opt/trstenko")
DEFAULT_REPO = "majcek210/trstenko"

# "dev" and "beta" are the same thing: releases marked prerelease on GitHub.
# Both spellings are accepted because the workflow says dev and the research
# said beta, and a device that silently falls back to stable because someone
# typed the other one is a bad afternoon.
PRERELEASE_CHANNELS = ("beta", "dev")
CHANNELS = ("stable",) + PRERELEASE_CHANNELS


class UpdateConfig:
    def __init__(self, repo=DEFAULT_REPO, channel="stable", data_root=None,
                 install_root=None, check_interval_hours=6.0,
                 auto_check=True, allow_metered=False,
                 urgent_grace_seconds=300.0, defer_hours=24.0,
                 urgent_max_defer_hours=12.0, token=None,
                 extra_keys=None):
        self.repo = repo
        self.channel = channel if channel in CHANNELS else "stable"
        self.data_root = data_root or DEFAULT_DATA_ROOT
        self.install_root = install_root or DEFAULT_INSTALL_ROOT
        self.check_interval_hours = float(check_interval_hours)
        self.auto_check = bool(auto_check)
        self.allow_metered = bool(allow_metered)
        # How long an urgent update shows its notice before going ahead. Long
        # enough to read and to hit "not now", short enough that the fix
        # actually lands.
        self.urgent_grace_seconds = float(urgent_grace_seconds)
        self.defer_hours = float(defer_hours)
        # An urgent update can be pushed back, but not indefinitely. Otherwise
        # "urgent" means nothing and the device stays broken.
        self.urgent_max_defer_hours = float(urgent_max_defer_hours)
        self.token = token
        # Keys handed in rather than read off disk. The emulator uses this to
        # exercise the signed path with a throwaway key; the device does not.
        self.extra_keys = list(extra_keys or [])

    def trusted_keys(self):
        """Every public key allowed to sign a release for this device."""
        from . import signing

        return signing.load_keys(
            *signing.default_key_paths(self.install_root)) + self.extra_keys

    @property
    def require_signature(self):
        """True as soon as there is a key to check against.

        Deliberately derived rather than configurable. A separate "verify
        signatures" switch is a thing somebody leaves off, and a device with
        a trusted key that ignores signatures is worse than one with neither.
        """
        return bool(self.trusted_keys())

    @property
    def staging_dir(self):
        return os.path.join(self.data_root, "updates", "staging")

    @property
    def state_path(self):
        return os.path.join(self.data_root, "updates", "state.json")

    def as_dict(self):
        return {
            "repo": self.repo,
            "install_root": self.install_root,
            "channel": self.channel,
            "check_interval_hours": self.check_interval_hours,
            "auto_check": self.auto_check,
            "allow_metered": self.allow_metered,
            "urgent_grace_seconds": self.urgent_grace_seconds,
            "defer_hours": self.defer_hours,
            "urgent_max_defer_hours": self.urgent_max_defer_hours,
        }

    @classmethod
    def load(cls, path=None, data_root=None):
        """Read `settings.json` if it exists. A missing or broken file gives
        defaults rather than an exception -- a device that will not boot
        because its settings file got truncated is worse than one on the
        stable channel."""
        data_root = data_root or DEFAULT_DATA_ROOT
        path = path or os.path.join(data_root, "settings.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh).get("updates", {})
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("data_root", data_root)
        allowed = {k: v for k, v in raw.items()
                   if k in cls.__init__.__code__.co_varnames}
        return cls(**allowed)

"""Checking for, and eventually applying, firmware updates.

    from firmware.updates import UpdateManager, UpdateConfig, GitHubReleases

    config = UpdateConfig.load()
    manager = UpdateManager(config, GitHubReleases(config.repo, config.token),
                            hal.system)

The device's web UI drives it: `check()` on demand, `tick()` on the loop,
`approve()` / `defer()` / `skip()` / `install()` from the page.

Applying an update swaps a release directory and arms a rollback. What it
cannot do is change the operating system -- the limit is spelled out at the
top of `installer.py` and in this package's README.
"""

from .config import UpdateConfig
from .boot import BootGuard, Layout
from .installer import ApplyError, Installer, StagingError, base_version
from .manager import (APPLIED, AWAITING_CONSENT, BLOCKED, CHECKING, DEFERRED,
                      DOWNLOADING, FAILED, IDLE, INSTALLING, ON_TRIAL,
                      RESTARTING, SCHEDULED, STAGED, UP_TO_DATE,
                      UpdateManager)
from .releases import (GitHubReleases, OPTIONAL, RECOMMENDED, URGENT, Release,
                       ReleaseError, ReleaseSource)
from .version import Version, current, parse

__all__ = ["UpdateConfig", "UpdateManager", "GitHubReleases", "Release",
           "ReleaseSource", "ReleaseError", "Version", "current", "parse",
           "URGENT", "RECOMMENDED", "OPTIONAL",
           "BootGuard", "Layout", "Installer", "ApplyError", "StagingError",
           "base_version",
           "IDLE", "CHECKING", "UP_TO_DATE", "AWAITING_CONSENT", "SCHEDULED",
           "DOWNLOADING", "STAGED", "INSTALLING", "APPLIED", "ON_TRIAL",
           "RESTARTING",
           "BLOCKED", "FAILED", "DEFERRED"]

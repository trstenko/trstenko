"""Building the scheduler for a device.

The scheduler, the announcer and the chores need the device's clocks, its inbox,
its speaker and its settings, and the device needs them back. This is the one
place that joins them, so `main.py` on the robot and the emulator's rigs build
exactly the same thing.
"""

from . import housekeeping
from .announce import Announcer
from .clock import FileSyncCheck
from . import Scheduler


def wire(device, synced=None, say=None):
    """Attach a scheduler to `device`, which needs its database open.

    `synced` answers "can the wall clock be believed"; the default is the real
    file systemd-timesyncd creates, and the emulator passes `FakeClock`'s.
    `say` is where spoken lines go until Piper exists.
    """
    if device.db is None:
        raise ValueError("the scheduler keeps its jobs in the database; "
                         "open one first")
    scheduler = Scheduler(
        device.db, device.mono, device.wall, device.events.post,
        synced=synced or FileSyncCheck().is_synced,
        tz=device.timezone, lang=device.language)
    housekeeping.standard(scheduler, device.hal.system, device.db,
                          device.events.post, device.timezone)
    announcer = Announcer(
        scheduler, device.hal.speaker, device.mono, say=say,
        busy=device.hold_announcements, quiet_volume=device.quiet_volume,
        lang=device.language)
    scheduler.boot()
    device.attach_scheduler(scheduler, announcer)
    return scheduler, announcer

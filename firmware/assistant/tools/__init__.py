"""The eight tools of v1 (`docs/research/llm-workflow.md`, Finding 3), in the
order they are always sent.

A short list helps small models most, and fewer tools means fewer wrong picks and
fewer input tokens on every request. OpenAI's own guidance is under twenty; there
are eight, and `build()` is the only place the list is made.
"""

from . import device, memory, reminders
from .registry import Registry, ToolContext, ToolError, tool

__all__ = ["build", "Registry", "ToolContext", "ToolError", "tool"]


def build():
    return Registry([
        memory.save_memory,
        memory.search_memory,
        memory.forget_memory,
        reminders.set_timer,
        reminders.set_reminder,
        reminders.list_reminders,
        reminders.cancel_reminder,
        device.set_volume,
    ])

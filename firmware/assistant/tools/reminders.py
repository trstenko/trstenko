"""Timers and reminders, set by voice.

The model never schedules anything itself. These tools send the same commands
the web page does, to the scheduler on the main thread (plan 02), which does the
validating in one place. A reminder is a fixed line the device speaks when it is
due, with no model call, so one set today still fires with an expired key.

`set_timer` and `set_reminder` are the likeliest pair for a model to confuse, so
each description says when to use the other. The evaluation (plan 08) will say
whether they should be one tool; until then they are two.
"""

from .. import spoken
from .registry import ToolError, tool

MAX_LISTED = 10


@tool(
    name="set_timer",
    description=(
        "Start a countdown, as in 'in ten minutes' or 'set a timer'. For "
        "'remind me to ...' use set_reminder."),
    params={
        "seconds": {"type": "integer", "min": 1, "max": 24 * 3600,
                    "description": "How long, in seconds, at most 24 hours."},
        "label": {"type": "string", "optional": True, "max_length": 60,
                  "description": "What it is for, such as 'pasta'."},
    },
    final=True,
)
def set_timer(ctx, seconds, label):
    result = ctx.command("reminders.add", {
        "seconds": seconds, "text": label or "", "source": "voice"})
    duration = spoken.duration(seconds, ctx.lang)
    key = "timer_set" if duration else "timer_set_generic"
    return {"id": result["id"],
            "say": ctx.strings(key, duration=duration)}


@tool(
    name="set_reminder",
    description=(
        "Remind the user of something at a local date and time, as in "
        "'remind me to ... at six'. For 'in N minutes' with no task, use "
        "set_timer."),
    params={
        "when": {"type": "string", "max_length": 25,
                 "description": "Local time as 2026-10-02T18:00, worked out "
                                "from the current time you were given."},
        "text": {"type": "string", "max_length": 200,
                 "description": "What to say when it is time, as a short "
                                "sentence."},
    },
    final=True,
)
def set_reminder(ctx, when, text):
    try:
        result = ctx.command("reminders.add", {
            "local_time": when, "text": text, "source": "voice"})
    except ToolError as exc:
        # Say what time it is, so the model can fix the call instead of
        # guessing again (llm-workflow.md, Finding 12).
        exc.extra["now"] = ctx.now_local()
        raise
    return {"id": result["id"], "say": ctx.strings(
        "reminder_set", when=spoken.with_preposition(
            result["reminder"]["say"], ctx.lang))}


@tool(
    name="list_reminders",
    description=(
        "List the pending reminders and timers, with their numbers. Use "
        "before cancelling one, or when asked what is coming up."),
)
def list_reminders(ctx):
    result = ctx.command("reminders.list", {"state": "pending"})
    jobs = result["reminders"]
    shown = []
    for job in jobs[:MAX_LISTED]:
        item = {"id": job["id"], "text": job["text"] or "timer"}
        if job["kind"] == "timer":
            item["seconds_left"] = int(round(job.get("remaining") or 0))
        else:
            item["when"] = spoken.with_preposition(job["say"], ctx.lang)
            if job["repeat"]:
                item["repeats"] = job["repeat"]
        shown.append(item)
    out = {"count": len(jobs), "reminders": shown}
    if len(jobs) > MAX_LISTED:
        out["more"] = len(jobs) - MAX_LISTED
    return out


@tool(
    name="cancel_reminder",
    description=(
        "Cancel one reminder or timer by its number. Use list_reminders first "
        "if you do not know the number."),
    params={
        "reminder_id": {"type": "integer", "min": 1,
                        "description": "The number from list_reminders."},
    },
)
def cancel_reminder(ctx, reminder_id):
    result = ctx.command("reminders.cancel", {"id": reminder_id})
    return {"cancelled": result["reminder"]["text"] or "timer"}

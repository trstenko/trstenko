"""The one thing the model may do to the device itself: change the volume.

Deliberately not tools: mute (the switch cuts the microphone's power and
software cannot press it), shutdown, restart and updates (they are on the
buttons and the page; a model must not be able to reboot the device because
someone on the radio said "restart"), and anything that touches the network or
the keys. What a tool can reach is exactly what is listed in this package.
"""

from .registry import ToolError, tool


@tool(
    name="set_volume",
    description=(
        "Change the speaker volume. Give a level from 0 to 100, or up or "
        "down for a step."),
    params={
        "level": {"type": "integer", "optional": True, "min": 0, "max": 100,
                  "description": "The volume in percent."},
        "change": {"type": "string", "optional": True,
                   "enum": ["up", "down"],
                   "description": "A step louder or quieter."},
    },
    final=True,
)
def set_volume(ctx, level, change):
    if level is None and change is None:
        raise ToolError("give a level from 0 to 100, or up or down")
    if level is not None:
        result = ctx.command("set_volume", {"level": level / 100.0})
    else:
        result = ctx.command("volume.change", {"direction": change})
    percent = int(round(result["volume"] * 100))
    return {"volume": percent,
            "say": ctx.strings("volume_set", percent=percent)}

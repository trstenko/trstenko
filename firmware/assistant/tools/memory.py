"""Remembering, recalling and forgetting: the assistant's own memory.

`save_memory` and `forget_memory` change what the device keeps, so they go to the
main thread as `Command`s (single writer, plan 01). `search_memory` only reads,
and reads on the worker's own database connection.

`forget_memory` is destructive, so it asks first. A guest who says "forget
everything" gets a question back instead of a wiped memory, and the answer has
to come in a later turn: the registry will not let the model confirm its own
question.

`attach` registers the commands on the device. The same commands will serve the
web page's memory list (plan 07), so the page and the voice go through one code
path.
"""

from .. import memories as mem
from .registry import ToolError, tool

LIST_LIMIT = 100


@tool(
    name="save_memory",
    description=(
        "Remember a lasting fact about the user. Use when they say "
        "'remember ...' or tell you something about themselves; one fact per "
        "call."),
    params={
        "text": {"type": "string", "max_length": mem.MAX_CHARS,
                 "description": "The fact as a full sentence, such as 'The "
                                "user's sister is called Ana.'"},
        "category": {"type": "string", "enum": list(mem.CATEGORIES),
                     "description": "person, preference, fact or other."},
    },
    final=True,
)
def save_memory(ctx, text, category):
    result = ctx.command("memories.save", {"text": text,
                                           "category": category,
                                           "source": "voice"})
    if not result.get("ok"):
        raise ToolError(result.get("error", "that could not be saved"),
                        code=result.get("code"))
    key = "memory_known" if result.get("already") else "memory_saved"
    return {"id": result["memory"]["id"], "say": ctx.strings(key)}


@tool(
    name="search_memory",
    description=(
        "Look through the saved memories for words. Use when asked about "
        "something about the user that is not in the notes you were given."),
    params={
        "query": {"type": "string", "max_length": 100,
                  "description": "A few words to look for."},
    },
)
def search_memory(ctx, query):
    found = ctx.memories.search(query)
    ctx.memories.touch([m["id"] for m in found])
    return {"found": [{"id": m["id"], "text": m["text"]} for m in found]}


def _preview_forget(ctx, memory_id):
    memory = ctx.memories.get(memory_id)
    if memory is None:
        raise ToolError("there is no memory number %d" % memory_id)
    return {"memory": memory["text"]}


@tool(
    name="forget_memory",
    description=(
        "Delete one saved memory by its number. It asks the user to confirm "
        "first; do not call it again until they have said yes."),
    params={
        "memory_id": {"type": "integer", "min": 1,
                      "description": "The number of the memory, as given in "
                                     "the notes or by search_memory."},
    },
    destructive=True,
    preview=_preview_forget,
)
def forget_memory(ctx, memory_id):
    result = ctx.command("memories.forget", {"id": memory_id})
    if not result.get("ok"):
        raise ToolError(result.get("error", "that could not be forgotten"))
    return {"forgot": result["memory"]["text"], "say": ctx.strings(
        "memory_forgot")}


def attach(device, store):
    """The commands behind these tools and the page, on the main thread."""
    from ... import events as ev

    def need_dict(args):
        if not isinstance(args, dict):
            raise ev.CommandError("send the memory as an object")
        return args

    def save(args):
        args = need_dict(args)
        try:
            row = store.add(args.get("text"),
                            args.get("category") or mem.DEFAULT_CATEGORY,
                            source=args.get("source") or "voice",
                            pinned=bool(args.get("pinned")))
        except mem.MemoryRefused as exc:
            return {"ok": False, "code": exc.code, "error": str(exc)}
        return {"ok": True, "memory": row, "already": row["already"]}

    def forget(args):
        args = need_dict(args)
        try:
            row = store.delete(args.get("id"))
        except mem.MemoryRefused as exc:
            return {"ok": False, "code": exc.code, "error": str(exc)}
        return {"ok": True, "memory": row}

    def pin(args):
        args = need_dict(args)
        try:
            row = store.pin(args.get("id"), args.get("pinned", True))
        except mem.MemoryRefused as exc:
            raise ev.CommandError(str(exc), ev.NOT_FOUND)
        return {"ok": True, "memory": row}

    def listing(args):
        return {"memories": store.list(LIST_LIMIT), "count": store.count(),
                "limit": store.max_count}

    device.command("memories.save", save)
    device.command("memories.forget", forget)
    device.command("memories.pin", pin)
    device.command("memories.list", listing)

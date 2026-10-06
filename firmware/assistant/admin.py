"""What the page asks of the assistant: keys, usage history, privacy.

All of these are `Command`s, answered on the main thread like every other
change (plan 01), so the page and the voice cannot make conflicting ones.

**Keys are write-only.** `keys.set` takes a key and answers with whether one is
set and its last four characters; nothing here ever returns a key. A changed key
makes the meter forget what it learned from the old one (a banner, a cooldown)
and the stored test result is dropped, because it described the old key.

**Testing a key never blocks.** `keys.test` queues one tiny request on the
assistant worker and answers at once; the page asks `keys.status` until the
result is `ok` or `failed`. A slow provider is the worker's problem, not the
device's. Only one test per provider runs at a time.

**Privacy.** `privacy.export` is everything the device holds about the owner,
as one JSON document: the settings, the memories, the reminders and the usage
per day, and the installed tools with what each has kept. It has no keys in
it, ever. `privacy.delete` removes memories, the
conversation log, the usage history, or all three; the web layer asks for the
PIN again before it sends the command (plan 07).
"""

from .. import events as ev
from ..storage.secrets import KEYED, SecretsError

HISTORY_DAYS = (7, 30, 90)
DELETABLE = ("memories", "log", "usage", "all")


def _need_dict(args):
    if not isinstance(args, dict):
        raise ev.CommandError("expected an object", ev.INVALID)
    return args


def _need_provider(args, assistant, allow_ollama=False):
    name = _need_dict(args).get("provider")
    known = set(KEYED) | ({"ollama"} if allow_ollama else set())
    if name not in known:
        raise ev.CommandError("there is no provider called %r"
                              % (str(name)[:30],), ev.NOT_FOUND)
    return name


def attach(device, assistant):
    """Register the commands on `device` for the assistant `assistant`."""
    secrets = device.secrets

    def keys_status(args):
        if secrets is None:
            raise ev.CommandError("keys are not available on this device",
                                  ev.UNAVAILABLE)
        out = secrets.status()
        tests = assistant.worker.key_tests if assistant.worker else {}
        for name in KEYED:
            out[name]["test"] = dict(tests.get(name) or {"state": "none"})
        out["selected"] = device.settings.data["assistant"]["provider"]
        out["providers"] = [
            {"id": n, "label": p.label, "models": list(p.models),
             "default_model": p.default_model, "needs_key": p.needs_key,
             "key_help_url": p.key_help_url, "languages": list(p.languages),
             "free_tier": (p.free_tier or {}).get("note"),
             "verified": p.verified}
            for n in assistant.providers.names()
            for p in [assistant.providers.get(n)]]
        return out

    def keys_set(args):
        if secrets is None:
            raise ev.CommandError("keys are not available on this device",
                                  ev.UNAVAILABLE)
        name = _need_provider(args, assistant, allow_ollama=True)
        try:
            if name == "ollama":
                secrets.set_ollama_host(args.get("host", ""))
                result = secrets.status()["ollama"]
            else:
                result = secrets.set_key(name, args.get("key", ""))
        except SecretsError as exc:
            raise ev.CommandError(str(exc), ev.INVALID)
        if assistant.worker is not None:
            assistant.worker.key_tests.pop(name, None)
        assistant.keys_changed(name if name != "ollama" else None)
        return result

    def keys_test(args):
        name = _need_provider(args, assistant)
        if secrets is None or not secrets.has_key(name):
            raise ev.CommandError("Add a key for %s first." % name,
                                  ev.INVALID)
        running = (assistant.worker.key_tests.get(name) or {}).get("state")
        if running == "running":
            return {"state": "running"}
        assistant.worker.test_key(name)
        return dict(assistant.worker.key_tests[name])

    def usage_history(args):
        days = _need_dict(args or {}).get("days", 30)
        if days not in HISTORY_DAYS:
            raise ev.CommandError("days must be one of %s"
                                  % ", ".join(map(str, HISTORY_DAYS)),
                                  ev.INVALID)
        return {"days": _history(device, assistant, days),
                "summary": assistant.meter.summary()}

    def export(args):
        return _export(device, assistant)

    def delete(args):
        what = _need_dict(args).get("what")
        if what not in DELETABLE:
            raise ev.CommandError("what must be one of %s"
                                  % ", ".join(DELETABLE), ev.INVALID)
        return _delete(device, assistant, what)

    device.command("keys.status", keys_status)
    device.command("keys.set", keys_set)
    device.command("keys.test", keys_test)
    device.command("usage.history", usage_history)
    device.command("privacy.export", export)
    device.command("privacy.delete", delete)


def _history(device, assistant, days):
    """One row per day, oldest first, today last and live. Days before today
    come from `usage_daily` (which outlives the rows); today comes from
    `usage`, which is not rolled up yet."""
    conn = device.db.conn()
    today = assistant.meter.today()
    assistant.meter.flush(device.db)
    rows = {}
    for day, requests, tin, tout, cost in conn.execute(
            "SELECT day, SUM(requests), SUM(input_tokens), "
            "SUM(output_tokens), SUM(cost_usd_micro) FROM usage_daily "
            "WHERE day < ? AND day >= date(?, ?) GROUP BY day",
            (today, today, "-%d days" % days)):
        rows[day] = (requests, tin, tout, cost)
    for day, requests, tin, tout, cost in conn.execute(
            "SELECT day, COUNT(*), SUM(input_tokens), SUM(output_tokens), "
            "SUM(cost_usd_micro) FROM usage WHERE day >= ? GROUP BY day",
            (today,)):
        rows[day] = (requests, tin, tout, cost)
    return [{"day": day, "requests": r or 0, "input_tokens": i or 0,
             "output_tokens": o or 0, "cost_usd": (c or 0) / 1e6}
            for day, (r, i, o, c) in sorted(rows.items())]


def _export(device, assistant):
    conn = device.db.conn()
    memories = [dict(zip(("id", "text", "category", "created_at", "pinned",
                          "source"), row))
                for row in conn.execute(
                    "SELECT id, text, category, created_at, pinned, source "
                    "FROM memories ORDER BY id")]
    usage = [dict(zip(("day", "provider", "model", "requests",
                       "input_tokens", "output_tokens", "cost_usd_micro"),
                      row))
             for row in conn.execute(
                 "SELECT day, provider, model, requests, input_tokens, "
                 "output_tokens, cost_usd_micro FROM usage_daily "
                 "ORDER BY day")]
    reminders = device.scheduler.list(None) if device.scheduler else []
    return {"exported_at": device.wall(),
            "settings": device.settings.public(),
            "memories": memories, "reminders": reminders,
            "usage_daily": usage,
            "tools": assistant.tools.exported() if assistant.tools else [],
            "note": "API keys are never part of an export."}


def _delete(device, assistant, what):
    conn = device.db.conn()
    done = {}
    if what in ("memories", "all"):
        done["memories"] = conn.execute("DELETE FROM memories").rowcount
    if what in ("log", "all"):
        done["log"] = (conn.execute("DELETE FROM turn_log").rowcount
                       + conn.execute("DELETE FROM events_log").rowcount)
    if what in ("usage", "all"):
        assistant.meter.flush(device.db)
        done["usage"] = (conn.execute("DELETE FROM usage").rowcount
                         + conn.execute("DELETE FROM usage_daily").rowcount)
        assistant.meter.reset_totals()
    return {"deleted": done}

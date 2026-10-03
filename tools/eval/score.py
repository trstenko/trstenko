"""Scoring one evaluation case. No device and no network in here: it takes what
happened in a turn and what the case expected, and says which checks passed.

A case is one line of `cases_<lang>.jsonl`:

    {"say": "remind me to call mum at six",
     "now": "2026-10-02T15:10", "tz": "Europe/Ljubljana",
     "expect_tool": "set_reminder",
     "expect_args": {"when": "2026-10-02T18:00", "text~": "mum"},
     "max_words": 15}

What a case may expect:

- `expect_tool`: the tool that must be called. A list means any one of them.
  `null` means no tool at all. Leave the key out to not check.
- `also_ok`: tools that may be called as well without it counting as an extra
  (`list_reminders` before a `cancel_reminder`, say).
- `expect_args`: arguments of the expected tool's first call. `"key"` must be
  equal; `"key~"` must contain the text, whatever the case of the letters.
- `max_words`: the most words the whole spoken reply may have.
- `expect_question`: true if the reply has to end by asking something.
- `say`/`say_not` checks: `reply_matches` and `reply_not` are regular
  expressions the spoken reply must, or must not, contain.
- `local`: true if the device should handle it itself, with no model request.

What a case may set up: `name` (the owner's), `memories` (a list of facts
already saved), `reminders` (a list of `{"text", "when"}` already set) and
`lang`.

An `Outcome` is what the runner saw. A turn that failed (no key, a timeout, a
refusal) fails the `turn` check and nothing else is judged.
"""

import json
import re

CASE_KEYS = frozenset((
    "say", "now", "tz", "lang", "name", "memories", "reminders", "note",
    "expect_tool", "also_ok", "expect_args", "max_words", "expect_question",
    "reply_matches", "reply_not", "local",
))


class Outcome:
    def __init__(self, calls=(), reply="", error="", local=False,
                 latency=None, seconds=0.0, input_tokens=0, output_tokens=0,
                 cost_usd=0.0, requests=0):
        self.calls = list(calls)        # [(name, args dict or None)], in order
        self.reply = reply              # everything spoken, as one string
        self.error = error              # why the turn failed, or ""
        self.local = local              # handled with no model request
        self.latency = latency          # seconds to the first sentence
        self.seconds = seconds
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.requests = requests


def load(path):
    """The cases in a `.jsonl` file. Raises `ValueError` naming the line that
    is wrong: a case with a misspelt key would otherwise pass by checking
    nothing."""
    cases = []
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                case = json.loads(line)
            except ValueError as exc:
                raise ValueError("%s line %d is not JSON: %s"
                                 % (path, number, exc))
            if not isinstance(case, dict) or not case.get("say"):
                raise ValueError("%s line %d needs a \"say\"" % (path, number))
            unknown = sorted(set(case) - CASE_KEYS)
            if unknown:
                raise ValueError("%s line %d: unknown key %s"
                                 % (path, number, unknown[0]))
            case["line"] = number
            cases.append(case)
    return cases


def words(text):
    return len(re.findall(r"\w+(?:['’-]\w+)*", text or ""))


_TIME = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d)(?::00(?:\.0+)?)?$")


def _same(got, want):
    if isinstance(want, str) and isinstance(got, str):
        got, want = got.strip().lower(), want.strip().lower()
        # 18:00 and 18:00:00 are the same minute; models write both.
        a, b = _TIME.match(got.upper()), _TIME.match(want.upper())
        if a and b:
            return a.group(1) == b.group(1)
        return got == want
    return got == want


def score(case, outcome):
    """[(check, passed, detail)] for one case. Only the checks the case asks
    for are in the list, so a report can count them."""
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), "" if ok else detail))

    check("turn", not outcome.error, outcome.error)
    if outcome.error:
        return checks

    names = [name for name, _ in outcome.calls]
    if "local" in case:
        check("local", outcome.local == bool(case["local"]),
              "handled %s" % ("by the device" if outcome.local
                              else "by the model"))

    if "expect_tool" in case:
        want = case["expect_tool"]
        allowed = set(case.get("also_ok") or ())
        wanted = set([want] if isinstance(want, str) else want or ())
        if not wanted:
            # No tool, except the ones the case allows (a memory search).
            others = [n for n in names if n not in allowed]
            check("tool", not others, "called %s" % ", ".join(others))
        else:
            check("tool", bool(wanted & set(names)),
                  "called %s" % (", ".join(names) or "nothing"))
            extra = [n for n in names if n not in wanted | allowed]
            check("no extra tool", not extra, "also called %s"
                  % ", ".join(extra))
            repeated = [n for n in wanted if names.count(n) > 1]
            check("called once", not repeated, "%s called %d times"
                  % (repeated[0], names.count(repeated[0]))
                  if repeated else "")

        if case.get("expect_args") and wanted & set(names):
            args = next(a for n, a in outcome.calls if n in wanted) or {}
            wrong = []
            for key, value in case["expect_args"].items():
                if key.endswith("~"):
                    got = args.get(key[:-1])
                    if str(value).lower() not in str(got or "").lower():
                        wrong.append("%s=%r lacks %r" % (key[:-1], got, value))
                elif not _same(args.get(key), value):
                    wrong.append("%s=%r, wanted %r" % (key, args.get(key),
                                                       value))
            check("arguments", not wrong, "; ".join(wrong))

    if "max_words" in case:
        count = words(outcome.reply)
        check("length", count <= case["max_words"],
              "%d words, at most %d" % (count, case["max_words"]))
    if case.get("expect_question"):
        check("asks back", outcome.reply.rstrip("\"')”’ ")
              .endswith("?"), "said: %s" % outcome.reply[:80])
    if case.get("reply_matches"):
        check("says", re.search(case["reply_matches"], outcome.reply, re.I),
              "said: %s" % outcome.reply[:80])
    if case.get("reply_not"):
        check("does not say", not re.search(case["reply_not"], outcome.reply,
                                            re.I),
              "said: %s" % outcome.reply[:80])
    return checks


def passed(checks):
    return all(ok for _, ok, _ in checks)


def report(provider, model, rows, when=""):
    """The results as Markdown. `rows` is [(case, outcome, checks)]."""
    total = len(rows)
    good = sum(1 for _, _, checks in rows if passed(checks))
    by_check = {}
    for _, _, checks in rows:
        for name, ok, _ in checks:
            seen = by_check.setdefault(name, [0, 0])
            seen[0] += 1 if ok else 0
            seen[1] += 1
    latencies = sorted(o.latency for _, o, _ in rows if o.latency is not None)
    cost = sum(o.cost_usd for _, o, _ in rows)
    requests = sum(o.requests for _, o, _ in rows)

    def middle(values):
        return values[len(values) // 2] if values else None

    lines = ["# Evaluation: %s, %s" % (provider, model), ""]
    if when:
        lines += ["Run %s." % when, ""]
    lines += ["**%d of %d cases passed.**" % (good, total), "",
              "| Check | Passed |", "|---|---|"]
    lines += ["| %s | %d of %d |" % (name, ok, of)
              for name, (ok, of) in sorted(by_check.items())]
    lines += [""]
    if latencies:
        lines.append("Time to the first sentence: median %.1fs, slowest %.1fs."
                     % (middle(latencies), latencies[-1]))
    lines.append("Requests: %d. Cost by the device's own meter: $%.4f."
                 % (requests, cost))
    failed = [(c, o, checks) for c, o, checks in rows if not passed(checks)]
    if failed:
        lines += ["", "## What failed", ""]
        for case, outcome, checks in failed:
            lines.append("- **\"%s\"** (line %d)" % (case["say"],
                                                     case.get("line", 0)))
            for name, ok, detail in checks:
                if not ok:
                    lines.append("  - %s: %s" % (name, detail))
    lines += ["", "## Every case", "",
              "| Line | Said | Tools | Reply | Result |", "|---|---|---|---|---|"]
    for case, outcome, checks in rows:
        tools = ", ".join(n for n, _ in outcome.calls) or (
            "(device)" if outcome.local else "")
        reply = (outcome.reply or outcome.error).replace("|", "/")[:70]
        lines.append("| %d | %s | %s | %s | %s |" % (
            case.get("line", 0), case["say"].replace("|", "/"), tools, reply,
            "ok" if passed(checks) else "FAILED"))
    return "\n".join(lines) + "\n"

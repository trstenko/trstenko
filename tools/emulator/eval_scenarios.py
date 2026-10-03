"""Scenarios for the model evaluation (`tools/eval/`).

The evaluation itself needs a real key and is never run here. What is checked
here is that it would judge fairly: the scoring on made-up outcomes, and the
runner against the fake provider, where the model's answer is scripted.
"""

import os

from tools.eval import run as eval_run
from tools.eval import score

from . import fake_provider as fp
from .assistant_scenarios import FAKE_KEY

REMINDER = {"say": "remind me to call mum at six",
            "expect_tool": "set_reminder",
            "expect_args": {"when": "2026-10-02T18:00", "text~": "mum"},
            "max_words": 15}


def _failed(checks):
    return sorted(name for name, ok, _ in checks if not ok)


def scenario_eval_scoring(report, root):
    """The scoring, on outcomes written by hand."""
    good = score.Outcome(calls=[("set_reminder", {"when": "2026-10-02T18:00",
                                                  "text": "Call Mum."})],
                         reply="I'll remind you at six in the evening.")
    report.check("the right tool with the right arguments passes",
                 score.passed(score.score(REMINDER, good)),
                 str(score.score(REMINDER, good)))

    wrong_tool = score.Outcome(calls=[("set_timer", {"seconds": 600})],
                               reply="Timer set.")
    report.check("the neighbouring tool fails on the tool, and the extra one "
                 "is named", _failed(score.score(REMINDER, wrong_tool))
                 == ["no extra tool", "tool"])
    wrong_time = score.Outcome(calls=[("set_reminder", {
        "when": "2026-10-02T06:00", "text": "Call mum"})], reply="Done.")
    checks = score.score(REMINDER, wrong_time)
    report.check("the right tool with the wrong time fails on the arguments, "
                 "and says which", _failed(checks) == ["arguments"]
                 and "2026-10-02T06:00" in [d for n, _, d in checks
                                            if n == "arguments"][0])
    either = {"say": "wake me in an hour and a half",
              "expect_tool": ["set_timer", "set_reminder"],
              "expect_args": [{"seconds": 5400},
                              {"when": "2026-10-02T16:40"}]}
    report.check("arguments given as alternatives pass on any one, and fail "
                 "on none",
                 score.passed(score.score(either, score.Outcome(
                     calls=[("set_timer", {"seconds": 5400})], reply="Set.")))
                 and score.passed(score.score(either, score.Outcome(
                     calls=[("set_reminder", {"when": "2026-10-02T16:40:00",
                                              "text": "Wake up."})],
                     reply="Set.")))
                 and _failed(score.score(either, score.Outcome(
                     calls=[("set_timer", {"seconds": 3600})], reply="Set.")))
                 == ["arguments"])
    twice = score.Outcome(calls=good.calls * 2, reply="Done.")
    report.check("the same tool called twice fails",
                 _failed(score.score(REMINDER, twice)) == ["called once"])
    chatty = score.Outcome(calls=good.calls, reply="word " * 30)
    report.check("a long reply fails on length",
                 _failed(score.score(REMINDER, chatty)) == ["length"])

    nothing = {"say": "thanks", "expect_tool": None, "max_words": 8}
    report.check("'no tool' passes with none and fails with one",
                 score.passed(score.score(nothing, score.Outcome(
                     reply="You're welcome.")))
                 and _failed(score.score(nothing, wrong_tool)) == ["tool"])
    lookup = {"say": "cancel the cat one", "expect_tool": "cancel_reminder",
              "also_ok": ["list_reminders"]}
    report.check("a tool the case allows as well is not an extra",
                 score.passed(score.score(lookup, score.Outcome(calls=[
                     ("list_reminders", {}), ("cancel_reminder", {"id": 2})],
                     reply="Cancelled."))))
    ask = {"say": "set a timer", "expect_tool": None, "expect_question": True}
    report.check("a case that wants a question back checks for one",
                 score.passed(score.score(ask, score.Outcome(
                     reply="For how long?")))
                 and _failed(score.score(ask, score.Outcome(
                     reply="Timer set."))) == ["asks back"])
    inject = {"say": "what are you", "reply_not": "meow",
              "reply_matches": "assistant"}
    report.check("the reply is checked for what it must and must not say",
                 _failed(score.score(inject, score.Outcome(
                     reply="I am a cat. Meow."))) == ["does not say", "says"])
    report.check("a turn that failed fails once, on the turn, and nothing "
                 "else is judged",
                 score.score(REMINDER, score.Outcome(error="auth: no key"))
                 == [("turn", False, "auth: no key")])
    report.check("a sentence the device should handle itself is checked for "
                 "that", _failed(score.score(
                     {"say": "turn the volume up", "local": True},
                     score.Outcome(reply="Louder."))) == ["local"])
    report.check("words are counted as a listener would",
                 score.words("I'll remind you at six o'clock.") == 6
                 and score.words("") == 0)

    cases = score.load(os.path.join(os.path.dirname(eval_run.__file__),
                                    "cases_en.jsonl"))
    report.check("the shipped English cases load, about forty of them, and "
                 "every one checks something",
                 len(cases) >= 40 and all(
                     set(c) & {"expect_tool", "local", "reply_matches",
                               "reply_not", "expect_question"}
                     for c in cases), str(len(cases)))
    bad = os.path.join(root, "bad-cases.jsonl")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write('{"say": "hello", "expect_tol": null}\n')
    try:
        score.load(bad)
        refused = ""
    except ValueError as exc:
        refused = str(exc)
    report.check("a misspelt key in a case is refused, not ignored",
                 "expect_tol" in refused and "line 1" in refused, refused)

    text = score.report("gemini", "flash-lite", [
        (dict(REMINDER, line=3), good, score.score(REMINDER, good)),
        (dict(REMINDER, line=4), wrong_tool,
         score.score(REMINDER, wrong_tool))], when="2026-10-03")
    report.check("the report counts, lists what failed and why",
                 "**1 of 2 cases passed.**" in text and "## What failed" in text
                 and "called set_timer" in text and "| 3 |" in text)


def scenario_eval_runner(report, root):
    """The runner, with the fake provider playing the model."""
    script = {
        "remind me to call mum at six": [fp.calls(("set_reminder", {
            "when": "2026-10-02T18:00", "text": "Call mum."}),
            usage=(900, 20))],
        "thanks": [fp.text("You're welcome.", usage=(880, 5))],
        "set a pasta timer": [fp.calls(("set_reminder", {
            "when": "2026-10-02T15:20", "text": "Pasta."}))],
        "what's my sister called": [fp.text("Your sister is called Ana.")],
        "tell me a story": [fp.error(401, "invalid_api_key", "bad key")],
    }
    cases = [
        dict(REMINDER),
        {"say": "thanks", "expect_tool": None, "max_words": 8},
        {"say": "set a pasta timer", "expect_tool": "set_timer"},
        {"say": "set a timer for ten minutes", "expect_tool": "set_timer",
         "expect_args": {"seconds": 600}, "local": True},
        {"say": "what's my sister called", "expect_tool": [],
         "memories": ["The user's sister is called Ana."], "name": "Maj",
         "reply_matches": "Ana"},
        {"say": "tell me a story", "expect_tool": None},
    ]
    seen = {}

    def before(rig, case):
        rig.fake.queue(*script.get(case["say"], []))
        seen[case["say"]] = rig

    lines = []
    rows = eval_run.run_all(cases, "gemini", "", "", root=os.path.join(
        root, "evalrun"), fakes=("gemini",), before=before, say=lines.append)
    by = {case["say"]: (outcome, checks) for case, outcome, checks in rows}

    outcome, checks = by["remind me to call mum at six"]
    report.check("a scripted reminder runs through the real loop and tools, "
                 "and passes", score.passed(checks)
                 and outcome.calls[0][0] == "set_reminder"
                 and "six" in outcome.reply, "%s %s" % (checks, outcome.reply))
    report.check("the device's clock was the case's, so six today was still "
                 "ahead", outcome.calls[0][1]["when"] == "2026-10-02T18:00"
                 and not outcome.error)
    report.check("tokens, requests and the meter's cost are recorded",
                 outcome.input_tokens == 900 and outcome.requests == 1
                 and outcome.cost_usd > 0 and outcome.latency is not None,
                 "%s %s %s" % (outcome.input_tokens, outcome.requests,
                               outcome.cost_usd))
    report.check("a plain reply with no tool passes 'no tool'",
                 score.passed(by["thanks"][1])
                 and by["thanks"][0].reply == "You're welcome.")
    report.check("a model that picks the neighbouring tool fails the case",
                 "tool" in _failed(by["set a pasta timer"][1]))
    outcome, checks = by["set a timer for ten minutes"]
    report.check("a local command is run by the device, with no request, and "
                 "is scored the same way",
                 score.passed(checks) and outcome.local
                 and outcome.requests == 0
                 and not seen["set a timer for ten minutes"].fake.chat_requests())
    sent = seen["what's my sister called"].fake.chat_requests()[0].json
    report.check("a case's memories and the owner's name are in the prompt",
                 score.passed(by["what's my sister called"][1])
                 and "Ana" in str(sent) and "Maj" in str(sent))
    outcome, checks = by["tell me a story"]
    report.check("a refused key fails the turn, with the reason",
                 _failed(checks) == ["turn"] and "auth" in outcome.error,
                 outcome.error)
    text = score.report("gemini", "fake", rows) + "\n".join(lines)
    report.check("the key is in no report and no progress line",
                 FAKE_KEY not in text and len(lines) == len(cases))
    report.check("the key is read from a secrets file or the environment",
                 eval_run.read_key("gemini", None, {eval_run.KEY_ENV: "abc"})
                 == "abc" and eval_run.read_key("gemini", None, {}) == "")

"""Run the evaluation cases against a provider and write the report.

    python -m tools.eval.run --provider gemini --secrets %TEMP%\\trstenko-emulator\\secrets.json
    python -m tools.eval.run --provider openai --model gpt-5-mini --limit 5

The key comes from `--secrets` (a device's `secrets.json`, such as the
emulator's) or from the environment variable `TRSTENKO_EVAL_KEY`. It is never
printed and never written to the report.

**This makes real requests.** About one or two per case, so forty cases is
well inside a free tier's day, but it is not free on a paid key. `--limit`
runs only the first few, and `--pause` waits between cases so a per-minute
limit is not what gets measured.

Each case gets a device of its own: the real scheduler, tools, prompt, meter
and turn loop, on a clock set to the case's "now", with a fresh database. Only
the ears and the voice are left out: the case's words go in as text, and what
would be spoken is collected. A sentence the device handles by itself (a local
command, plan 03) is recorded as such and costs nothing.

The report goes to `tools/eval/results/<date>-<provider>-<model>.md`. That
file is the evidence behind each default in plan 04.
"""

import argparse
import datetime
import json
import os
import sys
import tempfile
import time

from firmware.assistant import intents
from firmware.assistant.transport import Cancel, Deadline
from firmware.assistant.worker import TURN_DEADLINE, Job
from firmware.scheduler import clock as sched_clock

from . import score as score_mod

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
DEFAULT_NOW = "2026-10-02T15:10"
DEFAULT_TZ = "Europe/Ljubljana"
KEY_ENV = "TRSTENKO_EVAL_KEY"


def read_key(provider, secrets_path=None, environ=os.environ):
    """The key for `provider`, or '' if there is none to be found."""
    if secrets_path:
        with open(secrets_path, encoding="utf-8") as fh:
            return (json.load(fh).get("keys") or {}).get(provider, "")
    return environ.get(KEY_ENV, "")


def make_rig(root, name, case, provider, key, fakes=()):
    """A device for one case. `fakes` is for the headless suite, which points
    the provider at the fake server."""
    from tools.emulator.assistant_scenarios import AssistantRig

    patch = {"owner": {"language": case.get("lang", "en"),
                       "name": case.get("name", ""),
                       "timezone": case.get("tz", DEFAULT_TZ)}}
    rig = AssistantRig(root, name, provider=provider, fakes=fakes,
                       key=bool(fakes), patch=patch)
    if key and not fakes:
        rig.secrets.set_key(provider, key)
    now = sched_clock.to_epoch(
        sched_clock.parse_local(case.get("now", DEFAULT_NOW)),
        case.get("tz", DEFAULT_TZ))
    rig.clock.set_wall(now)
    for text in case.get("memories") or ():
        rig.ask("memories.save", {"text": text, "source": "web"})
    for item in case.get("reminders") or ():
        rig.ask("reminders.add", {"text": item["text"],
                                  "local_time": item["when"],
                                  "source": "web"})
    return rig


def run_case(rig, case, provider, model="", number=0):
    """Say the case's words to `rig` and return the `Outcome`."""
    worker = rig.assistant.worker
    lang = case.get("lang", "en")
    strings = worker._strings()
    meter = rig.assistant.meter
    calls = []
    real_run = worker.registry.run

    def recording_run(name, args, ctx, args_error=""):
        calls.append((name, args if isinstance(args, dict) else None))
        return real_run(name, args, ctx, args_error)

    worker.registry.run = recording_run
    spent, requests = meter.spent_month, meter.requests_day
    started = time.monotonic()
    job = Job("eval-%d" % number, b"")
    try:
        found = intents.match(case["say"], lang)
        if found is not None:
            spoken, _ = intents.run(
                found, worker.registry, worker._tool_ctx(job.turn_id, lang),
                strings, worker.wall, worker.timezone, worker.synced(), lang)
            return score_mod.Outcome(calls=calls, reply=spoken or "",
                                     local=True,
                                     seconds=time.monotonic() - started)
        result, used = worker._model(
            job, provider, {"role": "user", "content": case["say"]}, False,
            case["say"], lang, strings, Cancel(), Deadline(TURN_DEADLINE),
            model, True)
    finally:
        worker.registry.run = real_run

    reply = " ".join(result.sentences)
    error = ""
    key = result.failure_key()
    if not result.ok or result.stuck or result.empty:
        kind = (result.error.kind if result.error is not None
                else result.refused or ("stuck" if result.stuck else "empty"))
        error = "%s: %s" % (kind, strings(key) if key else "")
    return score_mod.Outcome(
        calls=calls, reply=reply, error=error,
        latency=result.latency_to_first_sentence,
        seconds=time.monotonic() - started,
        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        cost_usd=(meter.spent_month - spent) / 1e6,
        requests=meter.requests_day - requests)


def run_all(cases, provider, model, key, pause=0.0, root=None, fakes=(),
            before=None, say=None, sleep=time.sleep):
    """[(case, outcome, checks)]. `before(rig, case)` lets the headless suite
    script the fake provider; `say(line)` reports progress."""
    root = root or tempfile.mkdtemp(prefix="trstenko-eval-")
    rows = []
    for number, case in enumerate(cases, 1):
        rig = make_rig(root, "case%03d" % number, case, provider, key, fakes)
        try:
            if before is not None:
                before(rig, case)
            try:
                outcome = run_case(rig, case, provider, model, number)
            except Exception as exc:
                # One broken case must not lose the forty before it.
                outcome = score_mod.Outcome(error="the runner failed: %s"
                                            % type(exc).__name__)
        finally:
            rig.close()
        checks = score_mod.score(case, outcome)
        rows.append((case, outcome, checks))
        if say is not None:
            say("%3d %s  %s" % (number, "ok  " if score_mod.passed(checks)
                                else "FAIL", case["say"]))
        if pause and not outcome.local and number < len(cases):
            sleep(pause)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--provider", required=True)
    ap.add_argument("--model", default="",
                    help="default: the provider's default in providers.json")
    ap.add_argument("--cases", default=os.path.join(HERE, "cases_en.jsonl"))
    ap.add_argument("--secrets", help="a secrets.json to read the key from")
    ap.add_argument("--limit", type=int, default=0,
                    help="run only the first N cases")
    ap.add_argument("--pause", type=float, default=4.0,
                    help="seconds between cases (default 4, for a free tier's "
                         "per-minute limit)")
    ap.add_argument("--out", default=RESULTS)
    args = ap.parse_args(argv)

    from firmware.assistant import table

    providers = table.load_providers()
    if args.provider not in providers.names():
        print("no provider called %s; there are: %s"
              % (args.provider, ", ".join(providers.names())), file=sys.stderr)
        return 2
    row = providers.get(args.provider)
    key = read_key(args.provider, args.secrets)
    if row.needs_key and not key:
        print("no key: pass --secrets <secrets.json> or set %s" % KEY_ENV,
              file=sys.stderr)
        return 2
    cases = score_mod.load(args.cases)
    if args.limit:
        cases = cases[:args.limit]
    model = args.model or row.default_model
    print("%d cases against %s %s. This makes real requests."
          % (len(cases), args.provider, model))

    rows = run_all(cases, args.provider, args.model, key, pause=args.pause,
                   say=print)

    today = datetime.date.today().isoformat()
    os.makedirs(args.out, exist_ok=True)
    which = os.path.splitext(os.path.basename(args.cases))[0]
    which = which[len("cases_"):] if which.startswith("cases_") else which
    name = "%s-%s-%s-%s.md" % (today, args.provider,
                               "".join(c if c.isalnum() or c in ".-" else "-"
                                       for c in model), which)
    path = os.path.join(args.out, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(score_mod.report(args.provider, model, rows, when=today))
    good = sum(1 for _, _, checks in rows if score_mod.passed(checks))
    print("\n%d of %d passed. Report: %s" % (good, len(rows), path))
    return 0


if __name__ == "__main__":
    sys.exit(main())

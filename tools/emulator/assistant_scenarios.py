"""Headless scenarios for the assistant (milestone M2).

They live here rather than in `headless.py`, which is already long, and are run
by it: `python -m tools.emulator.headless` is still the one command. Each takes
`(report, root)` like the others, and none needs a sound card, a display, a
network, a key or a Piper model.
"""

import copy
import json

from firmware.assistant import table


def _raises(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except Exception as exc:
        return exc
    return None


def scenario_tables(report, root):
    from firmware.storage.settings import PROVIDERS

    providers = table.load_providers()
    prices = table.load_prices()
    report.check("the shipped provider table loads",
                 providers.names() == list(PROVIDERS), str(providers.names()))
    report.check("and every model and STT model we ship has a price",
                 table.cross_check(providers, prices) == [],
                 str(table.cross_check(providers, prices)))

    with open(table.PROVIDERS_FILE, encoding="utf-8") as fh:
        shipped = json.load(fh)

    def broken(name, change):
        rows = copy.deepcopy(shipped)
        change(rows[name])
        return table.check_providers(rows)

    def says(problems, word):
        return any(word in p for p in problems)

    report.check("a missing field is named",
                 says(broken("gemini", lambda r: r.pop("schema_flavour")),
                      "schema_flavour"))
    report.check("an unknown field is a typo, and is refused",
                 says(broken("gemini", lambda r: r.update(drop_param=[])),
                      "drop_param"))
    report.check("plain http to a public host is refused",
                 says(broken("openai", lambda r: r.update(
                     base_url="http://api.openai.com/v1")), "https://"))
    report.check("a key-less public provider is refused",
                 says(broken("openai", lambda r: r.update(auth="none")),
                      "local provider"))
    report.check("a default model that is not in the list is refused",
                 says(broken("gemini", lambda r: r.update(
                     default_model="gemini-9")), "default_model"))
    report.check("a row with two reply-length parameters is refused",
                 says(broken("openai", lambda r: r["params"].update(
                     max_tokens=300)), "exactly one"))
    report.check("a row with none is refused",
                 says(broken("openai", lambda r: r["params"].pop(
                     "max_completion_tokens")), "exactly one"))
    report.check("a parameter that is both sent and dropped is refused",
                 says(broken("gemini", lambda r: r["drop_params"].append(
                     "reasoning_effort")), "also sent"))
    report.check("an unknown header placeholder is refused",
                 says(broken("xai", lambda r: r["headers"].update(
                     x="{password}")), "placeholder"))
    report.check("a query string in the base URL is refused",
                 says(broken("openai", lambda r: r.update(
                     base_url="https://api.openai.com/v1?key=1")), "base_url"))
    report.check("an STT path that is not absolute is refused",
                 says(broken("openai", lambda r: r["stt"].update(
                     path="audio")), "stt"))
    report.check("a table that omits a provider the settings offer is refused",
                 says(table.check_providers(
                     {k: v for k, v in shipped.items() if k != "groq"},
                     PROVIDERS), "groq"))
    report.check("and one with a row the settings cannot choose is refused",
                 says(table.check_providers(
                     dict(shipped, extra=shipped["groq"]), PROVIDERS),
                      "extra"))
    err = _raises(table.load_providers, path=root + "/missing.json")
    report.check("a missing table file says which file",
                 isinstance(err, table.TableError)
                 and "missing.json" in str(err), repr(err))

    gem = providers.get("gemini")
    report.check("the owner's reply cap replaces the table's default",
                 gem.params(120)["max_tokens"] == 120
                 and gem.params()["max_tokens"] == 300
                 and providers.get("openai").token_param
                 == "max_completion_tokens")
    report.check("params come back as a copy nobody can change the table with",
                 gem.params().update(x=1) is None
                 and "x" not in gem.params())
    report.check("a model the owner did not choose is the table's default",
                 gem.model("") == gem.default_model
                 and gem.model("custom") == "custom")
    report.check("session placeholders fill in the headers, and no more",
                 providers.get("xai").headers("abc")
                 == {"x-grok-conv-id": "abc"}
                 and gem.headers() == {})
    report.check("Gemini hears Slovenian and OpenAI does not yet",
                 gem.speaks("sl") and not providers.get("openai").speaks("sl"))
    report.check("Gemini needs no separate STT; OpenAI and Groq have one",
                 providers.can_transcribe() == ["gemini", "openai", "groq"],
                 str(providers.can_transcribe()))

    ollama = providers.get("ollama")
    report.check("a local provider's address goes into its URL",
                 ollama.base_url("192.168.1.5")
                 == "http://192.168.1.5:11434/v1"
                 and ollama.base_url("pc.local")
                 == "http://pc.local:11434/v1")
    for host, why in (("", "empty"), ("evil.com/steal", "a path"),
                      ("a b", "a space"), ("x?y=1", "a query"),
                      ("user@host", "a user"), ("-bad", "a leading hyphen"),
                      ("pc:9999", "a port")):
        err = _raises(ollama.base_url, host)
        report.check("an address that is %s is refused" % why,
                     isinstance(err, table.TableError), repr(err))
    report.check("a public provider ignores any host",
                 gem.base_url("anything")
                 == "https://generativelanguage.googleapis.com/v1beta/openai")
    report.check("no provider's headers carry a credential",
                 all("authorization" not in k.lower() and "key" not in k.lower()
                     for name in providers.names()
                     for k in providers.get(name).headers("s")))


def scenario_prices(report, root):
    prices = table.load_prices()

    cost = prices.cost_micro("gemini", "gemini-3.8-flash", input_tokens=1000,
                             cached_tokens=200, output_tokens=100)
    report.check("cached tokens are charged at their own rate, the rest at "
                 "the input rate", cost == 990, str(cost))
    report.check("a price change applies from its day, not before",
                 prices.cost_micro("gemini", "gemini-3.8-flash",
                                   input_tokens=1000, cached_tokens=200,
                                   output_tokens=100, day="2026-12-31") == 990
                 and prices.cost_micro("gemini", "gemini-3.8-flash",
                                       input_tokens=1000, cached_tokens=200,
                                       output_tokens=100,
                                       day="2027-01-01") == 1980)
    report.check("with no day, announced changes are not applied",
                 prices.cost_micro("gemini", "gemini-3.8-flash",
                                   input_tokens=1000, cached_tokens=200,
                                   output_tokens=100) == 990)
    report.check("a model that never cached is charged the input rate for "
                 "cached tokens too",
                 prices.cost_micro("xai", "grok-4.20-0309-non-reasoning",
                                   input_tokens=1000, cached_tokens=1000)
                 == 1250)
    report.check("more cached than input is clamped, not negative",
                 prices.cost_micro("openai", "gpt-5-mini", input_tokens=10,
                                   cached_tokens=999) >= 0)
    report.check("a typical turn costs about a tenth of a cent on gpt-5-mini "
                 "(plan 05)",
                 900 <= prices.cost_micro("openai", "gpt-5-mini",
                                          input_tokens=3000,
                                          output_tokens=160) <= 1300,
                 str(prices.cost_micro("openai", "gpt-5-mini",
                                       input_tokens=3000, output_tokens=160)))
    report.check("per-minute STT is priced from the seconds",
                 prices.cost_micro("openai", "gpt-4o-mini-transcribe",
                                   audio_seconds=60) == 3000
                 and prices.cost_micro("openai", "gpt-4o-mini-transcribe",
                                       audio_seconds=5) == 250)
    report.check("per-hour STT is priced from the seconds",
                 prices.cost_micro("groq", "whisper-large-v3-turbo",
                                   audio_seconds=3600) == 40000)
    report.check("a model with no price is unknown, not free",
                 prices.cost_micro("openai", "gpt-9") is None
                 and not prices.known("openai", "gpt-9"))
    report.check("a free tier is a fact about the model",
                 prices.free_model("gemini", "gemini-3.5-flash-lite")
                 and not prices.free_model("openai", "gpt-5-mini"))

    mine = prices.with_overrides({"openai/gpt-5-mini": {"in": 1.0},
                                  "openai/gpt-9": {"in": 2.0, "out": 4.0}})
    report.check("an owner's override beats the shipped price, field by field",
                 mine.row("openai", "gpt-5-mini")["in"] == 1.0
                 and mine.row("openai", "gpt-5-mini")["out"] == 2.0)
    report.check("and prices a model we do not ship",
                 mine.cost_micro("openai", "gpt-9", input_tokens=10,
                                 output_tokens=10) == 60)
    report.check("overrides do not change the table they came from",
                 prices.row("openai", "gpt-5-mini")["in"] == 0.25)

    bad = {"updated": "yesterday", "usd_to_eur": 0,
           "models": {"x": {"in": 1.0, "out": -2},
                      "a/b": {"in": 1.0, "per_minute": 1.0},
                      "a/c": {"in": 1.0}}}
    problems = table.check_prices(bad)
    report.check("a bad price table says what is wrong with each row",
                 any("updated" in p for p in problems)
                 and any("usd_to_eur" in p for p in problems)
                 and any("provider/model" in p for p in problems)
                 and any("out must be a price" in p for p in problems)
                 and any("not both" in p for p in problems)
                 and any("both in and out" in p for p in problems),
                 "; ".join(problems))

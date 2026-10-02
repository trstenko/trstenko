"""Headless scenarios for the assistant (milestone M2).

They live here rather than in `headless.py`, which is already long, and are run
by it: `python -m tools.emulator.headless` is still the one command. Each takes
`(report, root)` like the others, and none needs a sound card, a display, a
network, a key or a Piper model.
"""

import calendar
import copy
import json
import logging
import threading
import time

from firmware.assistant import provider as prov
from firmware.assistant import table
from firmware.assistant import transport

from . import fake_provider as fp

FAKE_KEY = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE12345x9Qk"


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


# ---- the model client, over a real socket, against a fake provider ----

class Fake:
    """A fake provider, a table row pointing at it, and a client on that row.
    Closing it stops the server, and nothing outlives the scenario."""

    open_fakes = []

    def __init__(self, name="gemini", mutate=None, key=FAKE_KEY,
                 **transport_kw):
        with open(table.PROVIDERS_FILE, encoding="utf-8") as fh:
            row = json.load(fh)[name]
        self.server = fp.FakeProvider()
        row["base_url"] = self.server.start()
        row["allow_http"] = True
        if mutate:
            mutate(row)
        self.provider = table.Provider(name, row)
        self.key = key
        self.attempts = []
        self.deltas = []
        self.transport = transport.Transport(**transport_kw)
        self.client = prov.ChatClient(
            self.provider, lambda: self.key, transport=self.transport,
            user_agent="Trstenko/test")
        Fake.open_fakes.append(self)

    def chat(self, seconds=8.0, messages=None, tools=None, cancel=None,
             on_text=True, **kw):
        """The reply, or the `ProviderError` it raised."""
        self.deltas = []
        callback = ((lambda t: self.deltas.append((time.monotonic(), t)))
                    if on_text is True else on_text)
        try:
            return self.client.chat(
                self.provider.default_model,
                messages or [{"role": "user", "content": "hi"}], tools=tools,
                deadline=transport.Deadline(seconds), cancel=cancel,
                on_text=callback, on_attempt=self.attempts.append, **kw)
        except transport.ProviderError as err:
            return err

    def __getattr__(self, name):
        return getattr(self.server, name)

    def close(self):
        self.client.close()
        self.server.stop()
        if self in Fake.open_fakes:
            Fake.open_fakes.remove(self)


class capture_logs:
    """The provider's own log lines, so a scenario can read what a run said."""

    def __init__(self, name="trstenko.provider"):
        self.lines = []
        self._logger = logging.getLogger(name)
        outer = self

        class Handler(logging.Handler):
            def emit(self, record):
                outer.lines.append(record.getMessage())

        self._handler = Handler()

    def __enter__(self):
        self._logger.addHandler(self._handler)
        self._level = self._logger.level
        self._logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._level)


TOOLS = [{"type": "function", "function": {
    "name": "set_timer", "description": "d",
    "parameters": {"type": "object", "properties": {}}}}]


def scenario_provider_replies(report, root):
    fake = Fake()
    try:
        fake.queue(fp.text("Hello there, I am Trstenko.", usage=(120, 6),
                           pieces=4, gap=0.06))
        with capture_logs() as logs:
            reply = fake.chat(tools=TOOLS)
        report.check("a streamed reply comes back whole",
                     isinstance(reply, prov.ModelReply)
                     and reply.text == "Hello there, I am Trstenko.",
                     repr(getattr(reply, "text", reply)))
        report.check("the text arrives in pieces, as it is streamed",
                     len(fake.deltas) >= 3
                     and "".join(t for _, t in fake.deltas) == reply.text,
                     str(fake.deltas))
        report.check("the first piece comes before the stream has finished",
                     fake.deltas[-1][0] - fake.deltas[0][0] >= 0.1)
        report.check("usage in the last chunk is read",
                     reply.usage.input_tokens == 120
                     and reply.usage.output_tokens == 6
                     and not reply.usage.estimated, repr(reply.usage))
        report.check("the assistant message is ready to go back into the "
                     "conversation as it is",
                     reply.message == {"role": "assistant",
                                       "content": reply.text}
                     and reply.finish_reason == "stop")
        report.check("every attempt is reported, with its usage",
                     len(fake.attempts) == 1
                     and fake.attempts[0].status == "ok"
                     and fake.attempts[0].usage.input_tokens == 120
                     and fake.attempts[0].latency_ms >= 100
                     and fake.attempts[0].provider == "gemini")

        sent = fake.chat_requests()[0]
        body = sent.json
        report.check("the key is in the Authorization header",
                     sent.headers.get("authorization") == "Bearer " + FAKE_KEY)
        report.check("and the request says what it is",
                     sent.headers.get("content-type") == "application/json"
                     and sent.headers.get("user-agent") == "Trstenko/test"
                     and sent.path == "/v1/chat/completions", sent.path)
        report.check("the key is nowhere in the body",
                     FAKE_KEY.encode() not in sent.raw)
        report.check("the table's parameters go in, and its drops stay out",
                     body["stream"] is True and body["max_tokens"] == 300
                     and body["reasoning_effort"] == "minimal"
                     and "stream_options" not in body
                     and body["tools"] == TOOLS
                     and body["model"] == "gemini-3.5-flash-lite", str(body))

        report.check("nothing we logged contains the key",
                     all(FAKE_KEY not in line for line in logs.lines)
                     and any("provider=gemini" in l for l in logs.lines),
                     str(logs.lines))
        report.check("and the log line has no words from the conversation",
                     all("Trstenko." not in l and "hi" not in l.split()
                         for l in logs.lines))

        fake.queue(fp.text("ok", usage=(10, 1)))
        fake.chat(max_output=77)
        report.check("the owner's reply cap replaces the table's default",
                     fake.chat_requests()[-1].json["max_tokens"] == 77)

        fake.queue(fp.text("No tools offered."))
        fake.chat()
        report.check("no tools offered means no tools sent",
                     "tools" not in fake.chat_requests()[-1].json)

        fake.queue(fp.text("cached", usage=(120, 6, 100, 3),
                           usage_style="with_finish"))
        reply = fake.chat()
        report.check("usage with the finish chunk, cached and reasoning "
                     "tokens included",
                     reply.usage.cached_tokens == 100
                     and reply.usage.reasoning_tokens == 3
                     and reply.usage.input_tokens == 120, repr(reply.usage))

        fake.queue(fp.text("no usage at all"))
        reply = fake.chat()
        report.check("a provider that sends no usage gives none, for the "
                     "meter to estimate",
                     reply.usage is None and fake.attempts[-1].usage is None)

        fake.queue(fp.whole("Answered whole.", usage=(5, 3)))
        reply = fake.chat()
        report.check("a provider that ignores `stream` is still understood",
                     reply.text == "Answered whole."
                     and reply.usage.output_tokens == 3, repr(reply))

        junk = ["garbage"] * 3
        report.check("usage with the wrong types reads as zeros, not a crash",
                     prov.Usage.parse({"prompt_tokens": "x",
                                       "completion_tokens": None,
                                       "prompt_tokens_details": junk}
                                      ).input_tokens == 0
                     and prov.Usage.parse("nope") is None)
    finally:
        fake.close()

    fake = Fake("openai")
    try:
        fake.queue(fp.text("Hi.", usage=(50, 2)))
        fake.chat()
        body = fake.chat_requests()[0].json
        report.check("OpenAI's rows send max_completion_tokens, and ask for "
                     "usage in the stream",
                     body["max_completion_tokens"] == 300
                     and "max_tokens" not in body
                     and body["stream_options"] == {"include_usage": True},
                     str(body))
    finally:
        fake.close()

    fake = Fake("xai")
    try:
        fake.queue(fp.text("Hi."))
        fake.chat(session_id="sess-42")
        headers = fake.chat_requests()[0].headers
        report.check("a header from the table gets the session id, and the "
                     "key is still sent once",
                     headers.get("x-grok-conv-id") == "sess-42"
                     and headers.get("authorization") == "Bearer " + FAKE_KEY)
    finally:
        fake.close()

    fake = Fake("ollama", key="", mutate=lambda r: r.update(
        base_url=r["base_url"].replace("{host}", "127.0.0.1")))
    try:
        fake.queue(fp.text("Hi."))
        reply = fake.chat()
        report.check("a provider with no key sends no Authorization header",
                     isinstance(reply, prov.ModelReply)
                     and "authorization" not in fake.chat_requests()[0].headers,
                     repr(reply))
    finally:
        fake.close()


def scenario_provider_tools(report, root):
    fake = Fake()
    try:
        fake.queue(fp.calls(("set_timer", {"seconds": 600, "label": "tea"}),
                            ("set_volume", {"level": 40}),
                            usage=(200, 30)))
        reply = fake.chat(tools=TOOLS)
        names = [c.name for c in reply.tool_calls]
        report.check("two parallel calls are assembled in order",
                     names == ["set_timer", "set_volume"], str(names))
        report.check("with their arguments parsed from the fragments",
                     reply.tool_calls[0].args == {"seconds": 600,
                                                  "label": "tea"}
                     and reply.tool_calls[1].args == {"level": 40})
        report.check("and the ids the provider gave",
                     [c.id for c in reply.tool_calls] == ["call_0", "call_1"])
        report.check("the finish reason says tools",
                     reply.finish_reason == "tool_calls")
        message = reply.message
        report.check("the raw message carries every call, with its id, for "
                     "the next request",
                     message["role"] == "assistant"
                     and message["content"] is None
                     and [t["id"] for t in message["tool_calls"]]
                     == ["call_0", "call_1"]
                     and json.loads(message["tool_calls"][0]["function"]
                                    ["arguments"])["seconds"] == 600)

        sig = {"extra_content": {"google": {"thought_signature": "SIG-ONE"}}}
        fake.queue(fp.calls({"name": "a", "args": {"x": 1}, "id": "c1",
                             "extra": sig},
                            {"name": "b", "args": {"y": 2}, "id": "c2"},
                            index_bug=True))
        reply = fake.chat(tools=TOOLS)
        report.check("Google numbers every parallel call from zero, and they "
                     "are still two calls",
                     [c.id for c in reply.tool_calls] == ["c1", "c2"]
                     and reply.tool_calls[1].args == {"y": 2},
                     str(reply.tool_calls))
        report.check("a thought signature rides back inside the message",
                     reply.message["tool_calls"][0]["extra_content"]
                     == sig["extra_content"], str(reply.message))

        fake.queue(fp.calls(("a", {"x": 1}), ("b", {"y": 2}), no_ids=True))
        reply = fake.chat(tools=TOOLS)
        report.check("calls with no id are given distinct ones",
                     len({c.id for c in reply.tool_calls}) == 2
                     and all(c.id for c in reply.tool_calls),
                     str([c.id for c in reply.tool_calls]))

        fake.queue(fp.calls({"name": "set_timer", "args": "{not json"}))
        reply = fake.chat(tools=TOOLS)
        call = reply.tool_calls[0]
        report.check("arguments that are not JSON give a call with an error, "
                     "not an exception",
                     call.args is None and "JSON" in call.error, call.error)
        report.check("and the copy that goes back into the conversation is "
                     "valid, so the provider does not reject it",
                     reply.message["tool_calls"][0]["function"]["arguments"]
                     == "{}")

        fake.queue(fp.calls({"name": "set_timer", "args": "[1, 2]"}))
        reply = fake.chat(tools=TOOLS)
        report.check("arguments that are not an object are refused the same "
                     "way", reply.tool_calls[0].args is None
                     and "object" in reply.tool_calls[0].error)

        fake.queue(fp.calls(("set_timer", {}), say="Setting that now."))
        reply = fake.chat(tools=TOOLS)
        report.check("text and a call in one reply keep both",
                     reply.text == "Setting that now."
                     and len(reply.tool_calls) == 1
                     and reply.tool_calls[0].args == {})
    finally:
        fake.close()

    asm = prov.StreamAssembler()
    asm.feed({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"name": "x", "arguments": ""}}]}}]})
    asm.feed({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "late", "function": {"arguments": "{}"}}]}}]})
    calls_, _ = asm.tool_calls()
    report.check("an id that arrives after the first fragment is adopted",
                 len(calls_) == 1 and calls_[0].id == "late", str(calls_))
    asm = prov.StreamAssembler()
    asm.feed({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "n", "function": {"arguments": "{}"}}]}}]})
    report.check("a call with no name is never run",
                 asm.tool_calls() == ([], []))


def _gemini_429(quota_id="", retry="", status="RESOURCE_EXHAUSTED"):
    details = []
    if quota_id:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota_id}]})
    if retry:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": retry})
    return json.dumps({"error": {"code": 429, "message": "Resource has been "
                                 "exhausted", "status": status,
                                 "details": details}})


def scenario_provider_classify(report, root):
    classify = transport.classify

    def kind(status, body="", headers=None):
        err = classify(status, body, headers or {})
        return err.kind

    def err_body(message="", code="", typ="", status=""):
        return json.dumps({"error": {"message": message, "code": code,
                                     "type": typ, "status": status}})

    report.check("401 and 403 are the key",
                 kind(401) == "auth" and kind(403) == "auth")
    report.check("Gemini answers a bad key with a 400, and it is still the key",
                 kind(400, err_body("API key not valid. Please pass a valid "
                                    "API key.", status="INVALID_ARGUMENT"))
                 == "auth")
    report.check("OpenAI's wording for a bad key is understood too",
                 kind(400, err_body("Incorrect API key provided: sk-...",
                                    code="invalid_api_key")) == "auth")
    report.check("any other 400 is our own bug",
                 kind(400, err_body("Invalid value for tools[0]")) ==
                 "bad_request")
    report.check("a model that does not exist is a bad request too",
                 kind(404, err_body("model not found")) == "bad_request")
    report.check("5xx, 408 and 529 are the server's",
                 all(kind(s) == "server" for s in (408, 500, 502, 503, 504,
                                                   529)))
    report.check("OpenAI's insufficient_quota is the owner's money",
                 kind(429, err_body("You exceeded your current quota",
                                    code="insufficient_quota")) == "quota")
    report.check("a spend limit reached is quota, whoever words it",
                 kind(429, err_body("Your team has reached its spending "
                                    "limit")) == "quota")
    report.check("a plain rate limit is rate_limited",
                 kind(429, err_body("Rate limit reached for gpt-5-mini. "
                                    "Please try again in 20ms.",
                                    code="rate_limit_exceeded")) ==
                 "rate_limited")
    report.check("a daily limit is quota: nothing a retry fixes",
                 kind(429, err_body("Rate limit reached ... on requests per "
                                    "day (RPD)", code="rate_limit_exceeded"))
                 == "quota")
    report.check("Gemini's per-day quota id says quota",
                 kind(429, _gemini_429(
                     "GenerateRequestsPerDayPerProjectPerModel-FreeTier"))
                 == "quota")
    err = classify(429, _gemini_429("GenerateRequestsPerMinutePerProject"))
    report.check("Gemini's per-minute quota id says rate_limited, for sure",
                 err.kind == "rate_limited" and not err.ambiguous)
    err = classify(429, _gemini_429())
    report.check("Gemini's 429 with no quota id could be either, and says so",
                 err.kind == "rate_limited" and err.ambiguous)
    err = classify(429, _gemini_429(retry="34s"))
    report.check("Gemini's retryDelay is the wait",
                 err.retry_after == 34.0, str(err.retry_after))
    err = classify(429, err_body("slow down"), {"retry-after": "7"})
    report.check("a Retry-After header is the wait", err.retry_after == 7.0)
    when = calendar.timegm((2037, 10, 21, 7, 28, 5, 0, 0, 0))
    report.check("a Retry-After as a date is the wait too",
                 transport.parse_retry_after("Wed, 21 Oct 2037 07:28:05 GMT",
                                             now=when - 5) == 5.0)
    report.check("a nonsense Retry-After is nothing, not a crash",
                 transport.parse_retry_after("soon") is None
                 and transport.parse_retry_after("-5") is None
                 and transport.parse_retry_after(None) is None)
    report.check("a huge Retry-After is capped at a day",
                 transport.parse_retry_after("999999999") == 86400.0)

    report.check("OpenAI's reset times read as seconds",
                 transport.parse_reset("1s") == 1.0
                 and transport.parse_reset("6m0s") == 360.0
                 and abs(transport.parse_reset("20ms") - 0.02) < 1e-9
                 and transport.parse_reset("1h2m3s") == 3723.0
                 and transport.parse_reset("1.5s") == 1.5)
    report.check("and nonsense is nothing",
                 transport.parse_reset("") is None
                 and transport.parse_reset("tomorrow") is None
                 and transport.parse_reset("5") is None)
    info = transport.ratelimit_info({
        "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-remaining-tokens": "1234",
        "x-ratelimit-reset-requests": "1s",
        "x-ratelimit-reset-tokens": "6m0s"})
    report.check("the rate-limit headers are read",
                 info == {"remaining_requests": 0, "remaining_tokens": 1234,
                          "reset_requests": 1.0, "reset_tokens": 360.0},
                 str(info))
    report.check("and missing ones are left out",
                 transport.ratelimit_info({}) == {})

    err = classify(401, err_body("Incorrect API key provided: " + FAKE_KEY))
    report.check("a provider that echoes the key in its error does not get it "
                 "into ours", FAKE_KEY not in str(err), str(err))
    err = classify(400, err_body("x " * 400))
    report.check("a long message is cut short",
                 len(str(err)) <= transport.MAX_DETAIL)
    report.check("a body that is not JSON still classifies, by status",
                 kind(502, "<html>Bad gateway</html>") == "server"
                 and kind(429, "too many") == "rate_limited")


def scenario_provider_retries(report, root):
    fake = Fake()
    try:
        fake.queue(fp.error(401, "invalid_api_key", "Incorrect API key"))
        err = fake.chat()
        report.check("a 401 is an auth error, and is not retried",
                     isinstance(err, transport.ProviderError)
                     and err.kind == "auth" and err.status == 401
                     and len(fake.chat_requests()) == 1, repr(err))
        report.check("and the failed request was still reported",
                     len(fake.attempts) == 1
                     and fake.attempts[0].status == "auth"
                     and fake.attempts[0].http_status == 401)

        fake.clear()
        fake.attempts.clear()
        fake.queue(fp.error(401, message="Incorrect API key provided: "
                            + FAKE_KEY))
        with capture_logs() as logs:
            err = fake.chat()
        report.check("an error that quotes the key never shows it",
                     FAKE_KEY not in str(err)
                     and all(FAKE_KEY not in l for l in logs.lines))

        fake.clear()
        fake.queue(fp.error(429, "insufficient_quota",
                            "You exceeded your current quota"))
        err = fake.chat()
        report.check("out of credit is quota, and is never retried",
                     err.kind == "quota" and len(fake.chat_requests()) == 1)

        fake.clear()
        fake.attempts.clear()
        fake.queue(fp.error(429, "rate_limit_exceeded", retry_after=30,
                            headers={"x-ratelimit-remaining-requests": "0",
                                     "x-ratelimit-reset-requests": "1s"}))
        err = fake.chat()
        report.check("a long Retry-After is not waited for: busy, at once",
                     err.kind == "rate_limited" and err.retry_after == 30.0
                     and len(fake.chat_requests()) == 1)
        report.check("and the limits it sent come with the error",
                     err.ratelimit == {"remaining_requests": 0,
                                       "reset_requests": 1.0}, str(err.ratelimit))

        fake.clear()
        fake.attempts.clear()
        fake.queue(fp.error(429, "rate_limit_exceeded", retry_after=1),
                   fp.text("Worth the wait.", usage=(20, 4)))
        started = time.monotonic()
        reply = fake.chat()
        took = time.monotonic() - started
        report.check("a Retry-After of a second is waited out, and retried once",
                     isinstance(reply, prov.ModelReply)
                     and reply.text == "Worth the wait."
                     and len(fake.chat_requests()) == 2
                     and 1.0 <= took < 1.0 + prov.RETRY_JITTER + 1.5,
                     "%.2fs %r" % (took, reply))
        report.check("both attempts are reported, the failed one first",
                     [a.status for a in fake.attempts]
                     == ["rate_limited", "ok"],
                     str([a.status for a in fake.attempts]))

        fake.clear()
        fake.queue(fp.error(429, "rate_limit_exceeded", retry_after=1),
                   fp.text("never asked"))
        err = fake.chat(seconds=1.4)
        report.check("with too little of the turn left, it does not retry",
                     isinstance(err, transport.ProviderError)
                     and len(fake.chat_requests()) == 1)

        fake.clear()
        fake.queue(fp.error(429, "rate_limit_exceeded"),
                   fp.text("never asked"))
        err = fake.chat()
        report.check("a 429 with no Retry-After is not retried either",
                     isinstance(err, transport.ProviderError)
                     and len(fake.chat_requests()) == 1)

        fake.clear()
        fake.attempts.clear()
        fake.queue(fp.error(503, "overloaded"), fp.text("Back again."))
        started = time.monotonic()
        reply = fake.chat()
        report.check("a 503 is retried once, after a short pause",
                     isinstance(reply, prov.ModelReply)
                     and len(fake.chat_requests()) == 2
                     and time.monotonic() - started >= prov.RETRY_PAUSE,
                     repr(reply))

        fake.clear()
        fake.queue(fp.error(500), fp.error(500), fp.text("never asked"))
        err = fake.chat()
        report.check("and only once: two failures end the turn",
                     isinstance(err, transport.ProviderError)
                     and err.kind == "server"
                     and len(fake.chat_requests()) == 2)

        fake.clear()
        fake.queue(fp.error(400, "invalid_request", "bad tools schema"),
                   fp.text("never asked"))
        err = fake.chat()
        report.check("our own bad request is not retried",
                     err.kind == "bad_request"
                     and len(fake.chat_requests()) == 1)

        fake.clear()
        fake.queue(fp.text("one two three", pieces=3, inject=[
            {"error": {"code": 429, "message": "slow down",
                       "status": "RESOURCE_EXHAUSTED"}}]))
        err = fake.chat()
        report.check("an error sent inside a stream is classified the same",
                     isinstance(err, transport.ProviderError)
                     and err.kind == "rate_limited" and err.ambiguous,
                     repr(err))

        fake.clear()
        fake.attempts.clear()
        fake.queue(fp.text("limits", headers={
            "x-ratelimit-remaining-tokens": "1234"}))
        fake.chat()
        report.check("the limits on a good answer are kept for the meter",
                     fake.attempts[0].ratelimit == {"remaining_tokens": 1234})
    finally:
        fake.close()

    fake = Fake(key="")
    try:
        err = fake.chat()
        report.check("no key is an auth error before anything is sent",
                     isinstance(err, transport.ProviderError)
                     and err.kind == "auth"
                     and len(fake.chat_requests()) == 0, repr(err))
    finally:
        fake.close()


def scenario_provider_faults(report, root):
    fake = Fake()
    try:
        fake.queue(fp.text("one two three four five six", pieces=6,
                           drop_after=4), fp.text("never asked"))
        err = fake.chat()
        report.check("a stream cut part-way is an error that keeps what "
                     "arrived",
                     isinstance(err, transport.ProviderError)
                     and err.kind == "network" and err.partial.startswith("one")
                     and err.received_chars == len(err.partial) > 0,
                     repr(err))
        report.check("and is not retried, which would say the start twice",
                     len(fake.chat_requests()) == 1)
        report.check("the cut request is reported, with what it cost to guess",
                     fake.attempts[-1].status == "error"
                     and fake.attempts[-1].received_chars > 0
                     and fake.attempts[-1].request_chars > 0
                     and fake.attempts[-1].usage is None)

        fake.clear()
        fake.attempts.clear()
        fake.queue(fp.text("lost", drop_after=0),
                   fp.text("Second time lucky.", usage=(5, 3)))
        reply = fake.chat()
        report.check("a stream that dies before any text is retried once",
                     isinstance(reply, prov.ModelReply)
                     and reply.text == "Second time lucky."
                     and len(fake.chat_requests()) == 2, repr(reply))

        fake.clear()
        fake.queue(fp.text("a b c", no_finish=True, done=False))
        err = fake.chat()
        report.check("a stream that just ends, with no finish and no [DONE], "
                     "is a cut one",
                     isinstance(err, transport.ProviderError)
                     and err.kind == "network", repr(err))

        fake.clear()
        fake.queue(fp.text("x", endless=True))
        started = time.monotonic()
        reply = fake.chat()
        report.check("a stream that never ends is cut at a size cap",
                     isinstance(reply, prov.ModelReply)
                     and len(reply.text) == prov.MAX_REPLY_CHARS
                     and time.monotonic() - started < 5, repr(type(reply)))

    finally:
        fake.close()

    fake = Fake()
    try:
        fake.queue(fp.text("one"), fp.text("two"))
        fake.chat()
        fake.chat()
        report.check("two requests of a turn share one connection",
                     fake.connections == 1 and fake.transport.reused == 1,
                     "%d connections, %d reused" % (
                         fake.connections, fake.transport.reused))
    finally:
        fake.close()

    fake = Fake()
    try:
        fake.queue(fp.text("one", hangup_after=True), fp.text("two"))
        fake.chat()
        reply = fake.chat()
        report.check("a kept connection the far end closed is replaced "
                     "without bothering the turn",
                     isinstance(reply, prov.ModelReply) and reply.text == "two"
                     and fake.connections == 2 and len(fake.attempts) == 2,
                     "%r, %d connections, %d attempts" % (
                         reply, fake.connections, len(fake.attempts)))
    finally:
        fake.close()

    fake = Fake(idle_ttl=0.2)
    try:
        fake.queue(fp.text("one"), fp.text("two"))
        fake.chat()
        time.sleep(0.35)
        fake.chat()
        report.check("an idle connection is not trusted past its time",
                     fake.connections == 2 and fake.transport.reused == 0)
    finally:
        fake.close()

    fake = Fake(idle=0.4)
    try:
        fake.queue(fp.text("hello world", stall_after=1))
        started = time.monotonic()
        err = fake.chat(seconds=1.2)
        took = time.monotonic() - started
        report.check("a stream that stalls ends the turn at the idle limit",
                     isinstance(err, transport.ProviderError)
                     and err.kind == "timeout" and took < 1.3,
                     "%.2fs %r" % (took, err))
        report.check("and is reported as a timeout",
                     fake.attempts[-1].status == "timeout")
    finally:
        fake.close()

    fake = Fake(idle=5.0)
    try:
        fake.queue(fp.text("hello world", stall_after=1))
        started = time.monotonic()
        err = fake.chat(seconds=0.6)
        took = time.monotonic() - started
        report.check("the deadline cuts a stall even when the idle limit is "
                     "longer",
                     isinstance(err, transport.ProviderError)
                     and err.kind == "timeout" and 0.5 <= took < 1.5,
                     "%.2fs %r" % (took, err))

        fake.clear()
        err = fake.chat(seconds=0)
        report.check("a deadline already passed sends nothing",
                     err.kind == "timeout" and len(fake.requests) == 0)

        fake.clear()
        fake.queue(fp.text("late", first_byte_delay=1.5))
        started = time.monotonic()
        err = fake.chat(seconds=0.5)
        report.check("a slow first byte is cut by the deadline too",
                     isinstance(err, transport.ProviderError)
                     and err.kind == "timeout"
                     and time.monotonic() - started < 1.2)

        fake.clear()
        fake.attempts.clear()
        fake.queue(fp.text("one two three four", stall_after=2, pieces=4))
        cancel = transport.Cancel()
        timer = threading.Timer(0.3, cancel.set)
        timer.start()
        started = time.monotonic()
        err = fake.chat(cancel=cancel)
        took = time.monotonic() - started
        timer.cancel()
        report.check("cancelling from another thread wakes a read that is "
                     "blocked, long before any timeout",
                     isinstance(err, transport.ProviderError)
                     and err.kind == "cancelled" and took < 1.5,
                     "%.2fs %r" % (took, err))
        report.check("and the cancelled request is reported as such",
                     fake.attempts[-1].status == "cancelled")

        fake.clear()
        cancel = transport.Cancel()
        cancel.set()
        err = fake.chat(cancel=cancel)
        report.check("a turn already cancelled sends nothing",
                     err.kind == "cancelled" and len(fake.requests) == 0)

        fake.clear()
        fake.queue(fp.error(429, "rate_limit_exceeded", retry_after=1),
                   fp.text("never asked"))
        cancel = transport.Cancel()
        timer = threading.Timer(0.3, cancel.set)
        timer.start()
        started = time.monotonic()
        err = fake.chat(cancel=cancel)
        timer.cancel()
        report.check("cancelling while waiting to retry ends the wait",
                     err.kind == "cancelled"
                     and time.monotonic() - started < 0.9
                     and len(fake.chat_requests()) == 1, repr(err))

        cancel = transport.Cancel()
        seen = []
        handle = cancel.on_set(lambda: seen.append("a"))
        cancel.off(handle)
        cancel.on_set(lambda: seen.append("b"))
        cancel.set()
        cancel.on_set(lambda: seen.append("c"))
        report.check("a cancel hook that was taken off does not run, one "
                     "added after the fact runs at once",
                     seen == ["b", "c"], str(seen))
    finally:
        fake.close()

    fake = Fake()
    try:
        fake.server.stop()
        err = fake.chat()
        report.check("nobody listening is a network error, retried only once",
                     isinstance(err, transport.ProviderError)
                     and err.kind == "network"
                     and len(fake.attempts) == 2,
                     repr(err))
    finally:
        fake.close()

    fake = Fake(mutate=lambda r: r.update(base_url="http://127.0.0.1:1/v1"))
    try:
        err = fake.chat()
        report.check("a port with nothing behind it is refused at once",
                     isinstance(err, transport.ProviderError)
                     and err.kind == "network", repr(err))
    finally:
        fake.close()

    fake = Fake("ollama", key="", mutate=lambda r: r.update(
        base_url="http://{host}:11434/v1"))
    try:
        err = fake.chat()
        report.check("a local provider with no address says so, and sends "
                     "nothing",
                     isinstance(err, transport.ProviderError)
                     and err.kind == "bad_request"
                     and "address" in str(err), repr(err))
    finally:
        fake.close()


def close_all():
    """Anything a scenario left running, for the end of the run."""
    for fake in list(Fake.open_fakes):
        fake.close()

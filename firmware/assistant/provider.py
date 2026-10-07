"""The model client: Chat Completions, for every provider, from one adapter.

All the providers the owner can bring a key for speak the OpenAI Chat
Completions shape, so there is one client, and the differences between them are
rows in `providers.json` (plan 04). There is no `if provider == "gemini"` in
this file. Four adapters would be four places to get the tool loop wrong, and a
provider SDK is 46.8MB of a 512MB board (`docs/research/llm-workflow.md`,
Finding 1). The wire is `http.client` and `json`, through `transport.py`.

What it hands back is provider-neutral: a `ModelReply` with the text, the tool
calls, and the provider's assistant message *exactly as received*. That message
goes back into the conversation unchanged, because Gemini's thought signatures
and OpenAI's reasoning items travel inside it and a tool result sent back
without them can fail. (One deliberate exception: arguments that were not valid
JSON are replaced by `{}` in that copy, because the provider would otherwise
reject the whole conversation for the model's one bad call.)

Replies are streamed. The text is handed to the caller as it arrives, so the
first sentence can be spoken before the model has finished the second, and tool
calls are held until they are complete: half a call is never run.

Every HTTP attempt is reported to `on_attempt`, including the ones that failed
and the one that was cut off. The meter is built on that (plan 05): a provider
bills what it generated before we stopped reading, and a failed request still
counts against its rate limit, so nothing may go unrecorded.
"""

import json
import logging
import random
import time

from .table import TableError
from .transport import (AUTH, BAD_REQUEST, CANCELLED, NETWORK, RATE_LIMITED,
                        SERVER, TIMEOUT, Cancel, Deadline, Endpoint,
                        ProviderError, Transport, classify, ratelimit_info)

log = logging.getLogger("trstenko.provider")

# The turn's budget (plan 03): past this the person is waiting on a device that
# has gone quiet, and "that took too long" is a better answer than more waiting.
DEFAULT_DEADLINE = 8.0

# A retry that cannot finish is worse than none: it spends the turn's last
# seconds on a request that will be cut off. This much must be left.
MIN_TRY = 1.5
# Plan 05: a Retry-After of this long or less is worth waiting out. Longer, and
# the answer is "busy", at once.
RETRY_AFTER_MAX = 2.0
RETRY_JITTER = 0.3
# A network or server failure is retried once, after this.
RETRY_PAUSE = 0.5

# Bounds on what one reply may weigh. Two spoken sentences are a few hundred
# characters; this is for the day a provider streams without end.
MAX_REPLY_CHARS = 20000
MAX_JSON_BODY = 256 * 1024

# Message fields a provider sends that have to go back with the message.
# Google's compatibility layer carries its thought signature in this one.
CARRY_FIELDS = ("extra_content",)


def _count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


class Usage:
    """Token counts as the provider reports them: input includes the cached
    tokens, and output includes the reasoning ones (plan 05)."""

    __slots__ = ("input_tokens", "cached_tokens", "output_tokens",
                 "reasoning_tokens", "estimated")

    def __init__(self, input_tokens=0, cached_tokens=0, output_tokens=0,
                 reasoning_tokens=0, estimated=False):
        self.input_tokens = input_tokens
        self.cached_tokens = cached_tokens
        self.output_tokens = output_tokens
        self.reasoning_tokens = reasoning_tokens
        self.estimated = estimated

    @classmethod
    def parse(cls, raw):
        """From an OpenAI-style `usage` object, or None if it is not one."""
        if not isinstance(raw, dict):
            return None
        details = raw.get("prompt_tokens_details")
        out_details = raw.get("completion_tokens_details")
        return cls(
            input_tokens=_count(raw.get("prompt_tokens")),
            cached_tokens=_count(details.get("cached_tokens")
                                 if isinstance(details, dict) else 0),
            output_tokens=_count(raw.get("completion_tokens")),
            reasoning_tokens=_count(out_details.get("reasoning_tokens")
                                    if isinstance(out_details, dict) else 0))

    def __repr__(self):
        return ("Usage(in=%d cached=%d out=%d reasoning=%d%s)" % (
            self.input_tokens, self.cached_tokens, self.output_tokens,
            self.reasoning_tokens, " estimated" if self.estimated else ""))


class ToolCall:
    """One call the model asked for. `args` is None when the arguments were not
    a JSON object, and `error` says so; the registry turns that into a result
    the model can read (plan 03) rather than an exception."""

    __slots__ = ("id", "name", "args", "raw_args", "error")

    def __init__(self, id, name, args, raw_args="", error=""):
        self.id = id
        self.name = name
        self.args = args
        self.raw_args = raw_args
        self.error = error

    def __repr__(self):
        return "ToolCall(%s %s)" % (self.name, self.args)


class ModelReply:
    __slots__ = ("text", "tool_calls", "message", "usage", "finish_reason")

    def __init__(self, text, tool_calls, message, usage, finish_reason):
        self.text = text
        self.tool_calls = tool_calls
        self.message = message
        self.usage = usage
        self.finish_reason = finish_reason


class Attempt:
    """One HTTP request, for the meter. `usage` is None when the provider
    never sent any, which is what happens when a stream is cut."""

    __slots__ = ("provider", "model", "kind", "status", "http_status",
                 "latency_ms", "usage", "retry_after", "ambiguous",
                 "ratelimit", "request_chars", "received_chars")

    def __init__(self, provider, model):
        self.provider = provider
        self.model = model
        self.kind = None
        self.status = "ok"
        self.http_status = None
        self.latency_ms = 0
        self.usage = None
        self.retry_after = None
        self.ambiguous = False
        self.ratelimit = {}
        self.request_chars = 0
        self.received_chars = 0


class StreamAssembler:
    """Builds a reply out of Chat Completions chunks, streamed or not.

    Separate from the network code because it is where the providers differ in
    small ways, and it is easy to test with no network at all.
    """

    def __init__(self, on_text=None):
        self.on_text = on_text
        self._text = []
        self.chars = 0
        self.truncated = False
        self.usage = None
        self.finish_reason = None
        self._calls = []
        self._by_index = {}
        self._extra = {}

    @property
    def text(self):
        return "".join(self._text)

    def feed(self, chunk):
        if not isinstance(chunk, dict):
            return
        error = chunk.get("error")
        if error and not chunk.get("choices"):
            # Some providers fail inside a stream that began well.
            status = error.get("code") if isinstance(error, dict) else None
            if not isinstance(status, int):
                status = 500
            raise classify(status, json.dumps({"error": error}))
        usage = Usage.parse(chunk.get("usage"))
        if usage is not None:
            self.usage = usage
        for choice in chunk.get("choices") or ():
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = choice.get("message")
            if isinstance(delta, dict):
                self._delta(delta)
            reason = choice.get("finish_reason")
            if reason:
                self.finish_reason = reason

    def _delta(self, delta):
        text = delta.get("content")
        if isinstance(text, str) and text and not self.truncated:
            room = MAX_REPLY_CHARS - self.chars
            if len(text) > room:
                text, self.truncated = text[:max(0, room)], True
            if text:
                self._text.append(text)
                self.chars += len(text)
                if self.on_text is not None:
                    self.on_text(text)
        for key in CARRY_FIELDS:
            if key in delta:
                self._extra[key] = delta[key]
        for part in delta.get("tool_calls") or ():
            if isinstance(part, dict):
                self._tool_part(part)

    def _tool_part(self, part):
        index = part.get("index")
        call_id = part.get("id") or ""
        function = part.get("function")
        if not isinstance(function, dict):
            function = {}
        name = function.get("name") or ""
        fragment = function.get("arguments")
        if isinstance(fragment, (dict, list)):
            fragment = json.dumps(fragment)
        elif not isinstance(fragment, str):
            fragment = ""

        call = None
        if call_id:
            for known in self._calls:
                if known["id"] == call_id:
                    call = known
                    break
        if call is None:
            held = self._by_index.get(index) if index is not None else None
            if call_id:
                # A new id at an index already in use is a new call. Google's
                # endpoint numbers parallel calls from zero each time.
                if held is not None and not held["id"]:
                    call = held
                    call["id"] = call_id
            elif held is not None:
                call = held
            elif index is None and self._calls:
                # No id and no index: the only thing it can be a piece of is
                # the call before it.
                call = self._calls[-1]
        if call is None:
            call = {"id": call_id, "name": "", "args": [], "extra": {}}
            self._calls.append(call)
        if index is not None:
            self._by_index[index] = call
        if name and not call["name"]:
            call["name"] = name
        if fragment:
            call["args"].append(fragment)
        for key, value in part.items():
            if key not in ("index", "id", "type", "function"):
                call["extra"][key] = value

    def tool_calls(self):
        """The finished calls, and the raw form of each for the history."""
        calls, raw = [], []
        seen = set()
        for number, call in enumerate(self._calls):
            if not call["name"]:
                continue
            call_id = call["id"] or "call_%d" % number
            while call_id in seen:
                call_id += "_"
            seen.add(call_id)
            text = "".join(call["args"]).strip()
            args, error = {}, ""
            if text:
                try:
                    args = json.loads(text)
                except ValueError:
                    args, error = None, "the arguments were not valid JSON"
                else:
                    if not isinstance(args, dict):
                        args, error = None, "the arguments must be an object"
            calls.append(ToolCall(call_id, call["name"], args, text, error))
            entry = {"id": call_id, "type": "function",
                     "function": {"name": call["name"],
                                  "arguments": text if args is not None
                                  and text else "{}"}}
            entry.update(call["extra"])
            raw.append(entry)
        return calls, raw

    def reply(self):
        calls, raw = self.tool_calls()
        message = {"role": "assistant", "content": self.text or None}
        if raw:
            message["tool_calls"] = raw
        message.update(self._extra)
        return ModelReply(self.text, calls, message, self.usage,
                          self.finish_reason)


def retry_pause(err, attempt, deadline, cancel, delivered=False, rng=random):
    """How long to wait before the one retry plan 05 allows, or None for no
    retry. Shared by everything that talks to a provider, so a speech-to-text
    request and a chat request fail the same way.

    Only once, never after something has been delivered (a retry would say the
    first words twice), never for a key, a quota or our own bad request, and
    never if the turn would have no time left to use the answer."""
    if attempt >= 2 or delivered or err.partial or cancel.is_set():
        return None
    if err.kind == RATE_LIMITED:
        if err.retry_after is None or err.retry_after > RETRY_AFTER_MAX:
            return None
        pause = err.retry_after + rng.random() * RETRY_JITTER
    elif err.kind in (SERVER, NETWORK, TIMEOUT):
        pause = RETRY_PAUSE
    else:
        return None
    if deadline.remaining() < pause + MIN_TRY:
        return None
    return pause


class ChatClient:
    """Talks to one provider. Holds no key: `key_fn` is asked for it at the
    moment of the request, so it lives in the secrets store and in one request
    header, and nowhere that outlasts the call."""

    def __init__(self, provider, key_fn, host_fn=None, transport=None,
                 user_agent="Trstenko", rng=None):
        self.provider = provider
        self._key_fn = key_fn
        self._host_fn = host_fn
        self.transport = transport or Transport()
        self._user_agent = user_agent
        self._rng = rng or random.Random()

    def close(self):
        self.transport.close()

    # ---- one conversation step, with its retry ----

    def chat(self, model, messages, tools=None, max_output=None,
             deadline=None, cancel=None, on_text=None, on_attempt=None,
             session_id=""):
        """Send the conversation and return a `ModelReply`.

        Raises `ProviderError`, whose `kind` the caller maps to a sentence.
        Retries once where plan 05 allows it, and never once text has been
        handed to `on_text`: a retry would say the first words twice.
        """
        deadline = deadline or Deadline(DEFAULT_DEADLINE)
        cancel = cancel or Cancel()
        delivered = []

        def deliver(text):
            delivered.append(text)
            if on_text is not None:
                on_text(text)

        attempt = 0
        while True:
            attempt += 1
            try:
                return self._once(model, messages, tools, max_output, deadline,
                                  cancel, deliver, on_attempt, session_id)
            except ProviderError as err:
                pause = self._retry_pause(err, attempt, deadline, cancel,
                                          bool(delivered))
                if pause is None:
                    raise
                if cancel.wait(pause):
                    raise ProviderError(CANCELLED, "the turn was ended")

    def _retry_pause(self, err, attempt, deadline, cancel, delivered):
        return retry_pause(err, attempt, deadline, cancel, delivered,
                           self._rng)

    # ---- one HTTP request ----

    def _headers(self, key, session_id):
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream",
                   "User-Agent": self._user_agent}
        headers.update(self.provider.headers(session_id))
        if key:
            # Last, so nothing in the table can replace or duplicate it.
            headers["Authorization"] = "Bearer " + key
        return headers

    def _body(self, model, messages, tools, max_output):
        body = {"model": model, "messages": messages, "stream": True}
        body.update(self.provider.params(max_output, model))
        if tools:
            body["tools"] = tools
        if self.provider.stream_usage == "stream_options":
            body["stream_options"] = {"include_usage": True}
        for name in self.provider.drop_params:
            body.pop(name, None)
        return json.dumps(body, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")

    def _once(self, model, messages, tools, max_output, deadline, cancel,
              on_text, on_attempt, session_id):
        provider = self.provider
        record = Attempt(provider.name, model)
        started = time.monotonic()
        assembler = StreamAssembler(on_text)
        sent = 0
        try:
            key = self._key_fn() if provider.needs_key else ""
            if provider.needs_key and not key:
                raise ProviderError(AUTH, "no API key has been set for %s"
                                    % provider.label)
            try:
                host = self._host_fn() if self._host_fn else ""
                endpoint = Endpoint(provider.base_url(host))
            except (TableError, ValueError) as exc:
                raise ProviderError(BAD_REQUEST, str(exc))
            payload = self._body(model, messages, tools, max_output)
            sent = record.request_chars = len(payload)

            resp = self.transport.request(
                endpoint, "POST", "/chat/completions",
                self._headers(key, session_id), payload, deadline, cancel)
            try:
                record.http_status = resp.status
                record.ratelimit = ratelimit_info(resp.headers)
                if resp.status != 200:
                    raise classify(resp.status,
                                   resp.read().decode("utf-8", "replace"),
                                   resp.headers)
                self._read(resp, assembler)
            finally:
                resp.close()

            if assembler.finish_reason is None and not assembler.truncated:
                # No finish and no [DONE]: the stream was cut, whatever the
                # socket said about it.
                raise ProviderError(NETWORK, "the answer was cut off")
            record.usage = assembler.usage
            return assembler.reply()
        except ProviderError as err:
            err.request_chars = err.request_chars or sent
            err.partial = assembler.text
            err.received_chars = assembler.chars
            if err.status is None:
                err.status = record.http_status
            err.ratelimit = err.ratelimit or record.ratelimit
            record.kind = err.kind
            record.status = err.db_status
            record.retry_after = err.retry_after
            record.ambiguous = err.ambiguous
            raise
        finally:
            record.latency_ms = int((time.monotonic() - started) * 1000)
            record.received_chars = assembler.chars
            self._report(record, on_attempt)

    def _read(self, resp, assembler):
        content_type = resp.headers.get("content-type", "")
        if "event-stream" not in content_type and "json" in content_type:
            # A provider that ignored `stream`. The same chunk, whole.
            body = resp.read(MAX_JSON_BODY)
            try:
                assembler.feed(json.loads(body))
            except ValueError:
                raise ProviderError(SERVER, "the AI service sent an "
                                    "unreadable answer")
            if assembler.finish_reason is None:
                assembler.finish_reason = "stop"
            return
        while True:
            line = resp.readline()
            if not line:
                return
            line = line.strip()
            if not line.startswith(b"data:"):
                continue          # blank lines, comments, event: and id:
            data = line[5:].strip()
            if data == b"[DONE]":
                if assembler.finish_reason is None:
                    assembler.finish_reason = "stop"
                resp.finish()
                return
            try:
                chunk = json.loads(data)
            except ValueError:
                raise ProviderError(SERVER, "the AI service sent an "
                                    "unreadable stream")
            assembler.feed(chunk)
            if assembler.truncated:
                return

    def _report(self, record, on_attempt):
        log.info("model request: provider=%s model=%s status=%s ms=%d "
                 "in=%s out=%s", record.provider, record.model, record.status,
                 record.latency_ms,
                 record.usage.input_tokens if record.usage else "?",
                 record.usage.output_tokens if record.usage else "?")
        if on_attempt is not None:
            try:
                on_attempt(record)
            except Exception:
                log.exception("the usage callback failed")

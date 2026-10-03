"""One turn with the model: ask, run the tools it asks for, ask again, speak.

This is the loop of `docs/research/llm-workflow.md` Finding 2, with the three
additions of plan 03: the meter is asked before every request and told after,
state is changed through the main thread, and cancellation is checked before
every request and before every tool.

    messages = [system, *history, user]
    for step in range(MAX_STEPS):
        reply = ask(messages)
        if it asked for no tools: speak it, done
        run each tool, one after another, and add every result
        if every call was final: speak a fixed confirmation, done

Why it is shaped the way it is:

- **The step cap and the deadline are separate.** The cap stops a model that
  loops on tools; the deadline (the turn's eight seconds, shared by every
  request) stops a slow network from leaving the face on "thinking".
- **Cancelled means nothing more runs.** A reminder set after the user flipped
  the mute switch would break the promise the switch makes. The flag is looked at
  before each request and before each tool, and a late reply is thrown away.
- **Every call gets exactly one result, in order, before the next request.**
  Leave one out and the provider rejects the whole conversation. Calls past the
  cap get a result saying so, and are not run.
- **A final tool skips the second request.** For `set_timer` or `set_volume` the
  result *is* the answer ("Timer set for ten minutes."), and asking the model to
  put it in words costs a round trip, a second or more, on the commonest
  commands. Only when the model said nothing in that step: if it spoke first, the
  second request lets it finish what it started instead of talking over itself.
- **Sentences leave as they arrive.** The text is cut at sentence ends and handed
  to `on_sentence` while the stream is still open, because Piper produces nothing
  until it has a whole line and the first sentence sets the time to first sound.

- **A promise is not spoken until something stands behind it.** "I'll set
  that" with no tool call sets nothing, and the owner would believe it. Such a
  sentence is held until the step ends; with no tool call the model is told
  once, and if it still calls none the device says it could not do it
  (`promises.py`).

The loop speaks nothing and changes nothing itself: it calls `on_sentence`, and
the tools it runs send `Command`s (plan 01).
"""

import time

from . import promises, speech_text
from .transport import (AUTH, BAD_REQUEST, CANCELLED, NETWORK, QUOTA,
                        RATE_LIMITED, SERVER, TIMEOUT, ProviderError)

# Finding 2.
MAX_STEPS = 4
MAX_CALLS = 5

# What a failed or refused turn says, as a line in the language files (plan 03
# and plan 05).
FAILURE_KEYS = {
    NETWORK: "err_network", AUTH: "err_auth", RATE_LIMITED: "err_rate_limited",
    QUOTA: "err_quota", TIMEOUT: "err_timeout", BAD_REQUEST: "err_generic",
    SERVER: "err_generic",
}
REFUSAL_KEYS = {
    "daily_budget": "err_budget_day", "monthly_budget": "err_budget_month",
    "daily_turns": "err_turns", "rate_limited": "err_rate_limited",
    "quota": "err_quota",
}


class TurnInput:
    """Everything one turn needs, gathered by the worker."""

    def __init__(self, system, history, user, tool_ctx, provider, model,
                 max_output, turn_id, session_id="", transcript="",
                 audio_in=False, lang="en", max_steps=MAX_STEPS,
                 count_turn=True):
        self.system = system
        self.history = history
        self.user = user
        self.tool_ctx = tool_ctx
        self.provider = provider
        self.model = model
        self.max_output = max_output
        self.turn_id = turn_id
        self.session_id = session_id
        self.transcript = transcript
        self.audio_in = audio_in
        self.lang = lang
        self.max_steps = max(1, min(int(max_steps), 8))
        # False when the same turn is tried again on the fallback provider:
        # it is one question, however many keys it takes.
        self.count_turn = count_turn


class TurnResult:
    """What happened. `produced` is what goes into the session; the rest is for
    the worker to speak about and the log to count."""

    def __init__(self):
        self.sentences = []
        self.transcript = ""
        self.produced = []
        self.tools = []
        self.steps = 0
        self.error = None
        self.refused = None
        self.cancelled = False
        self.stuck = False
        # The model said it had done something and called no tool, twice.
        self.unkept = False
        self.empty = False
        self.shortcut = False
        self.first_sentence_at = None
        self.started = time.monotonic()
        self.input_tokens = 0
        self.output_tokens = 0

    @property
    def ok(self):
        return not (self.error or self.refused or self.cancelled)

    def failure_key(self):
        """The line to speak about what went wrong, or None."""
        if self.refused:
            return REFUSAL_KEYS.get(self.refused, "err_generic")
        if self.error is not None and self.error.kind != CANCELLED:
            if self.error.kind == AUTH and self.error.status is None:
                return "err_no_key"
            return FAILURE_KEYS.get(self.error.kind, "err_generic")
        if self.stuck:
            return "err_stuck"
        if self.unkept:
            return "err_not_done"
        if self.empty:
            return "err_generic"
        return None

    @property
    def latency_to_first_sentence(self):
        if self.first_sentence_at is None:
            return None
        return self.first_sentence_at - self.started


class Loop:
    def __init__(self, client, registry, meter, flavour, max_calls=MAX_CALLS):
        self.client = client
        self.registry = registry
        self.meter = meter
        self.flavour = flavour
        self.max_calls = max_calls
        self._tools = registry.schemas(flavour)

    def run(self, turn, on_sentence, cancel, deadline, strings):
        """Run one turn. `on_sentence(text)` is called, from this thread, for
        each sentence as it is ready to be spoken. `strings` is the owner's
        language file. Never raises for anything the model or the network did;
        the outcome is in the `TurnResult`."""
        result = TurnResult()
        return self._run(turn, on_sentence, cancel, deadline, strings, result)

    # ---- the loop proper ----

    def _run(self, turn, on_sentence, cancel, deadline, strings, result):
        splitter = speech_text.SentenceSplitter()
        cap = speech_text.SpokenCap()
        heard = speech_text.HeardFilter(turn.audio_in)
        result.transcript = turn.transcript

        def emit(sentence, trusted=False):
            cleaned = sentence if trusted else speech_text.clean_for_speech(
                sentence, turn.lang)
            if not cleaned:
                return
            piece = cleaned if trusted else cap.take(cleaned)
            if piece is None:
                return
            if result.first_sentence_at is None:
                result.first_sentence_at = time.monotonic()
            result.sentences.append(piece)
            on_sentence(piece)

        step_spoken = []
        # Sentences of this step that sound like "done", kept back until the
        # step shows whether a tool was called (promises.py). `worked` counts
        # the tools that have succeeded in this turn: after one has, the model
        # is describing something real and nothing is held.
        held = []
        worked = [0]
        nudged = [False]

        def speak(sentence):
            if held or (not worked[0] and promises.looks_like_promise(
                    sentence, turn.lang)):
                # Everything after a held sentence waits with it, so the
                # order of what is said never changes.
                held.append(sentence)
            else:
                emit(sentence)

        def on_text(delta):
            text = heard.feed(delta)
            if text:
                step_spoken.append(text)
                for sentence in splitter.feed(text):
                    speak(sentence)

        def end_of_step():
            tail = heard.finish()
            if tail:
                step_spoken.append(tail)
                for sentence in splitter.feed(tail):
                    speak(sentence)
            rest = splitter.flush()
            if rest:
                speak(rest)
            if heard.transcript:
                result.transcript = heard.transcript

        if turn.count_turn:
            verdict = self.meter.begin_turn(turn.turn_id)
            if not verdict.ok:
                result.refused = verdict.reason
                return result

        messages = [{"role": "system", "content": turn.system}]
        messages.extend(turn.history)
        user_index = len(messages)
        messages.append(turn.user)
        produced = result.produced

        for step in range(turn.max_steps):
            if cancel.is_set():
                result.cancelled = True
                return result
            estimate = self.meter.estimate_input(messages, self._tools)
            verdict = self.meter.allow(turn.provider, turn.model, estimate,
                                       turn.max_output)
            if not verdict.ok:
                result.refused = verdict.reason
                return result
            result.steps += 1
            del step_spoken[:]
            try:
                reply = self.client.chat(
                    turn.model, messages, self._tools, turn.max_output,
                    deadline, cancel, on_text=on_text,
                    on_attempt=lambda a: self.meter.record(a, turn.turn_id),
                    session_id=turn.session_id)
            except ProviderError as err:
                result.error = err
                result.cancelled = err.kind == CANCELLED
                return result
            if reply.usage is not None:
                result.input_tokens += reply.usage.input_tokens
                result.output_tokens += reply.usage.output_tokens
            if cancel.is_set():
                result.cancelled = True
                return result
            end_of_step()
            messages.append(reply.message)
            produced.append(reply.message)

            if heard.transcript and turn.audio_in and step == 0 \
                    and reply.tool_calls:
                # The audio has done its work. The next request carries the
                # words instead of half a megabyte of base64.
                messages[user_index] = {"role": "user",
                                        "content": heard.transcript}

            if not reply.tool_calls and held and held[-1].rstrip(
                    "\"')”’ ").endswith("?"):
                # "Okay. For how long?" is not a promise: it ends by asking.
                waiting, held[:] = list(held), []
                for sentence in waiting:
                    emit(sentence)
            if not reply.tool_calls and held:
                # A promise, and nothing was called. Do not say it and do not
                # remember it: tell the model once, then give up honestly.
                del held[:]
                produced.pop()
                if nudged[0]:
                    result.unkept = True
                    emit(strings("err_not_done"), trusted=True)
                    return result
                nudged[0] = True
                messages.append({"role": "user", "content": promises.NUDGE})
                continue
            if not reply.tool_calls:
                if cap.dropped:
                    emit(strings("short_version"), trusted=True)
                if not result.sentences:
                    result.empty = True
                self._after(result, emit, strings)
                return result

            ran = self._run_tools(reply, turn, messages, produced, result,
                                  cancel)
            if ran is None:
                result.cancelled = True
                return result
            worked[0] += sum(1 for r in ran if r.ok)
            if held:
                # It did call something. If that worked, what it said was
                # true and only late. If it failed, "I'll set that" would be
                # a lie; the next step explains what went wrong instead.
                waiting, held[:] = list(held), []
                if worked[0]:
                    for sentence in waiting:
                        emit(sentence)
            if (ran and all(r.final for r in ran)
                    and len(reply.tool_calls) <= self.max_calls
                    and not "".join(step_spoken).strip()):
                result.shortcut = True
                said = " ".join(r.say for r in ran)
                for r in ran:
                    emit(r.say, trusted=True)
                message = {"role": "assistant", "content": said}
                messages.append(message)
                produced.append(message)
                self._after(result, emit, strings)
                return result

        result.stuck = True
        emit(strings("err_stuck"), trusted=True)
        return result

    def _run_tools(self, reply, turn, messages, produced, result, cancel):
        """Run the calls one after another, giving each exactly one result.
        None if the turn was cancelled before a tool could run."""
        ran = []
        for number, call in enumerate(reply.tool_calls):
            if cancel.is_set():
                return None
            if number >= self.max_calls:
                content = ('{"ok":false,"error":"too many calls at once; '
                           'do one thing at a time"}')
            else:
                outcome = self.registry.run(call.name, call.args,
                                            turn.tool_ctx, call.error)
                content = outcome.content
                ran.append(outcome)
                result.tools.append(call.name)
            message = {"role": "tool", "tool_call_id": call.id,
                       "content": content}
            messages.append(message)
            produced.append(message)
        return ran

    def _after(self, result, emit, strings):
        """The end of a turn that went well: the one warning that is due."""
        warning = self.meter.take_warning()
        if warning:
            emit(strings("budget_warning_month" if warning == "budget_month"
                         else "budget_warning_day"), trusted=True)

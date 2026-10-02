"""The assistant's worker: everything slow between "the recording ended" and "the
reply is ready to be spoken", on one thread.

Main hands it a recording (`submit`). It hears it (`stt`), tries the local
commands (`intents`), and otherwise runs the model with its tools (`loop`),
posting each sentence to main as it is ready (`Sentence`) and finishing with
`TurnDone` or `TurnFailed`. It changes no device state itself: tools send
`Command`s and wait, and everything it tells main is an event carrying the id of
the turn it belongs to, so a late sentence from a turn that has been cancelled is
ignored (plan 03).

**One thread, one turn at a time.** Not a thread per turn: a thread that dies is
brought back by the supervisor, and a thread per turn would be a leak waiting
for a bad day.

**Cancelling.** `cancel(turn_id)` sets the turn's flag. The loop looks at it
before every request and every tool, and the transport reads in slices that look
at it too, so a mute switch ends a request that is waiting on the network within
a tenth of a second instead of when the model next speaks. A recording that was
queued and not yet started is dropped. A reply that arrives late is thrown away,
and its usage is recorded, because it was still billed.

**Which way it hears.** `stt.plan` says: in the chat request (Gemini), an upload,
or a transcription-only request to another provider. On the Gemini path the local
commands are not tried first, because the words are only known after the model has
answered, and trying them then would run a command twice. The tools do the same
work in the same single request, so nothing is lost but a pattern match.

**The fallback.** If the owner has a second key and the first provider is
rate-limited, out of quota or down, the same turn is tried on the second, once,
with the same prompt and tools (plan 05). Only if nothing has been spoken yet:
a reply cannot be started twice.

**Privacy.** The journal gets a line with lengths, tool names, token counts and
latency. The words go to `turn_log` only if the owner turned the conversation log
on (plan 06), and the last fifty are kept.
"""

import collections
import json
import logging
import threading
import time

from .. import events as ev
from . import intents, loop as loop_mod, prompt, speech_text, stt, tools
from .lang import strings as strings_for
from .provider import ChatClient
from .transport import (CANCELLED, QUOTA, RATE_LIMITED, SERVER, Cancel,
                        Deadline, ProviderError, Transport)

log = logging.getLogger("trstenko.worker")

# Plan 03: the whole turn, from the end of speech, including hearing it.
TURN_DEADLINE = 8.0
# What makes a turn worth trying on the second key (plan 05).
FALLBACK_KINDS = (RATE_LIMITED, QUOTA, SERVER)
FALLBACK_REFUSALS = ("rate_limited", "quota")
TURN_LOG_KEPT = 50
MAX_JOBS = 4


class Job:
    """A recording to act on. `kind` is "turn" for a conversation and "ack" for
    the words said while a reminder is going off."""

    def __init__(self, turn_id, pcm, kind="turn"):
        self.turn_id = turn_id
        self.pcm = pcm
        self.kind = kind


class Worker:
    def __init__(self, settings, secrets, providers, meter, registry, memories,
                 session, confirmations, post, send, wall, mono, synced,
                 timezone, db=None, transport=None, user_agent="Trstenko"):
        self.settings = settings
        self.secrets = secrets
        self.providers = providers
        self.meter = meter
        self.registry = registry
        self.memories = memories
        self.session = session
        self.confirmations = confirmations
        self.post = post
        self.send = send
        self.wall = wall
        self.mono = mono
        self.synced = synced
        self.timezone = timezone
        self.db = db
        self.transport = transport or Transport()
        self._user_agent = user_agent
        self._clients = {}
        self._transcriber = stt.Transcriber(providers, self._key,
                                            self.transport, user_agent)

        self._jobs = collections.deque()
        self._cv = threading.Condition()
        self._cancels = {}
        self._dropped = set()
        self._thread = None
        self._closing = False
        self.turns = 0
        self.last_error = ""

    # ---- called from main ----

    def start(self):
        self._closing = False
        self._thread = threading.Thread(target=self._run, name="assistant",
                                        daemon=True)
        self._thread.start()

    def close(self):
        with self._cv:
            self._closing = True
            for flag in self._cancels.values():
                flag.set()
            self._jobs.clear()
            self._cv.notify_all()
        if self._thread is not None and self._thread is not \
                threading.current_thread():
            self._thread.join(3.0)
        self.transport.close()

    def submit(self, job):
        """Queue a recording. Returns False if the worker is too far behind to
        take another, which means something is wrong, not that the user is
        busy: there is one turn at a time."""
        with self._cv:
            if len(self._jobs) >= MAX_JOBS:
                return False
            self._jobs.append(job)
            self._cv.notify()
        return True

    def cancel(self, turn_id):
        with self._cv:
            flag = self._cancels.get(turn_id)
            if flag is not None:
                flag.set()
            else:
                self._dropped.add(turn_id)
                if len(self._dropped) > 64:
                    self._dropped.pop()

    def busy(self):
        with self._cv:
            return bool(self._cancels) or bool(self._jobs)

    # ---- the thread ----

    def _run(self):
        try:
            while True:
                with self._cv:
                    while not self._jobs and not self._closing:
                        self._cv.wait()
                    if self._closing:
                        return
                    job = self._jobs.popleft()
                    if job.turn_id in self._dropped:
                        self._dropped.discard(job.turn_id)
                        continue
                    flag = Cancel()
                    self._cancels[job.turn_id] = flag
                try:
                    self._handle(job, flag)
                finally:
                    with self._cv:
                        self._cancels.pop(job.turn_id, None)
        except Exception:
            log.exception("the assistant worker stopped")
        finally:
            if not self._closing:
                self.post(ev.Event(ev.WORKER_DIED, data="assistant"))

    def _handle(self, job, cancel):
        started = time.monotonic()
        try:
            if job.kind == "ack":
                self._ack(job, cancel)
            else:
                self._turn(job, cancel, started)
        except Exception:
            # Ours, never the user's or the model's. The turn ends with the
            # plain sentence and the detail goes to the log.
            log.exception("turn %s failed", job.turn_id)
            self.last_error = "internal error"
            self._say(job.turn_id, self._strings()("err_generic"))
            self.post(ev.Event(ev.TURN_FAILED, data={
                "turn_id": job.turn_id, "kind": "internal"}))

    # ---- helpers ----

    def _strings(self):
        return strings_for(self.settings.data["owner"]["language"])

    def _say(self, turn_id, text):
        if text:
            self.post(ev.Event(ev.SENTENCE, data={"turn_id": turn_id,
                                                  "text": text}))

    def _key(self, name):
        return self.secrets.key(name) if self.secrets is not None else ""

    def _has_key(self, name):
        row = self.providers.get(name)
        return (not row.needs_key) or (self.secrets is not None
                                       and self.secrets.has_key(name))

    def client(self, name):
        if name not in self._clients:
            self._clients[name] = ChatClient(
                self.providers.get(name),
                lambda n=name: self._key(n),
                host_fn=lambda: getattr(self.secrets, "ollama_host", ""),
                transport=self.transport, user_agent=self._user_agent)
        return self._clients[name]

    def _tool_ctx(self, turn_id, lang):
        return tools.ToolContext(
            self._send, self.memories, strings_for(lang), self.wall,
            self.timezone, turn_id=turn_id, confirmations=self.confirmations,
            lang=lang)

    def _send(self, name, args=None):
        cmd = ev.Command(name, args)
        self.post(cmd)
        return cmd.wait()

    def _vocabulary(self):
        name = self.settings.data["owner"]["name"]
        return "Trstenko, %s" % name if name else "Trstenko"

    # ---- hearing ----

    def _hear(self, job, plan, lang, deadline, cancel):
        """The transcript for an upload or a transcription-only request, or ''.
        Raises `ProviderError`."""
        seconds = stt.seconds_of(job.pcm)
        if plan.mode == "upload":
            verdict = self.meter.allow(plan.provider, plan.model, 0, 0,
                                       audio_seconds=seconds)
            if not verdict.ok:
                raise _Refused(verdict.reason)
            return self._transcriber.upload(
                plan.provider, job.pcm, lang, self._vocabulary(), deadline,
                cancel, lambda a, s: self.meter.record(
                    a, job.turn_id, kind="stt", audio_seconds=s))
        # chat_stt: another provider's chat model does the hearing.
        row = self.providers.get(plan.provider)
        model = row.default_model
        estimate = self.meter.estimate_input(
            [stt.audio_message(job.pcm)]) + 40
        verdict = self.meter.allow(plan.provider, model, estimate, 200)
        if not verdict.ok:
            raise _Refused(verdict.reason)
        return self._transcriber.via_chat(
            self.client(plan.provider), model, job.pcm, deadline, cancel,
            on_attempt=lambda a: self.meter.record(a, job.turn_id))

    def _plan(self, lang):
        a = self.settings.data["assistant"]
        return stt.plan(self.providers, a["provider"], self._has_key, lang,
                        a["stt_provider"])

    # ---- the alert window ----

    def _ack(self, job, cancel):
        """Words said while a reminder is going off: only the local commands,
        never the model, and never a conversation."""
        lang = self.settings.data["owner"]["language"]
        plan = self._plan(lang)
        if not plan.ok or stt.check_audio(job.pcm):
            return self._done(job, 0, False)
        deadline = Deadline(TURN_DEADLINE)
        try:
            if plan.mode == "chat":
                plan = stt.SttPlan("chat_stt", self.settings.data[
                    "assistant"]["provider"])
            text = self._hear(job, plan, lang, deadline, cancel)
        except (ProviderError, _Refused):
            return self._done(job, 0, False)
        found = intents.match(text, lang, alert_window=True) if text else None
        if found is None or cancel.is_set():
            return self._done(job, 0, False)
        spoken, _ = intents.run(
            found, self.registry, self._tool_ctx(job.turn_id, lang),
            self._strings(), self.wall, self.timezone, self.synced(), lang)
        self._say(job.turn_id, spoken)
        self._done(job, 1 if spoken else 0, False)

    # ---- a conversation turn ----

    def _turn(self, job, cancel, started):
        data = self.settings.data
        lang = data["owner"]["language"]
        strings = self._strings()
        deadline = Deadline(TURN_DEADLINE)
        self.turns += 1

        if stt.check_audio(job.pcm):
            return self._done(job, 0, False)
        plan = self._plan(lang)
        if not plan.ok:
            self._fail(job, "no_stt", strings("err_no_stt"))
            return

        primary = data["assistant"]["provider"]
        transcript, user, audio_in = "", None, False
        if plan.mode == "chat":
            user, audio_in = stt.audio_message(job.pcm), True
        else:
            try:
                transcript = self._hear(job, plan, lang, deadline, cancel)
            except _Refused as refused:
                key = loop_mod.REFUSAL_KEYS.get(str(refused), "err_generic")
                self._fail(job, "refused", strings(key))
                return
            except ProviderError as err:
                if err.kind == CANCELLED or cancel.is_set():
                    return
                key = loop_mod.FAILURE_KEYS.get(err.kind, "err_generic")
                if err.kind == "auth" and err.status is None:
                    key = "err_no_key"
                self._fail(job, err.kind, strings(key))
                return
            if not transcript:
                return self._done(job, 0, False)      # silence: end quietly
            if cancel.is_set():
                return
            found = intents.match(transcript, lang)
            if found is not None:
                spoken, ok = intents.run(
                    found, self.registry, self._tool_ctx(job.turn_id, lang),
                    strings, self.wall, self.timezone, self.synced(), lang)
                self._say(job.turn_id, spoken)
                return self._done(job, 1 if spoken else 0, False)
            user = {"role": "user", "content": transcript}

        result, used = self._model(job, primary, user, audio_in, transcript,
                                   lang, strings, cancel, deadline,
                                   data["assistant"]["model"], True)
        if (self._wants_fallback(result) and not result.sentences
                and not cancel.is_set()):
            second = data["assistant"]["fallback_provider"]
            retry = self._fallback_input(second, job, transcript, result)
            if retry is not None:
                self.meter.note_fallback(primary, second, _why(result))
                user2, audio2 = retry
                result, used = self._model(
                    job, second, user2, audio2, transcript, lang, strings,
                    cancel, deadline, "", False)

        if result.cancelled or cancel.is_set():
            return
        key = result.failure_key()
        if key:
            self._say(job.turn_id, strings(key))
        if result.ok and not result.stuck and not result.empty:
            self.session.end_turn(result.transcript or transcript or "(spoken)",
                                  result.produced)
        else:
            self.session.touch()
        self._log(job, result, plan, started, used)
        if key and not result.ok:
            kind = (result.error.kind if result.error is not None
                    else result.refused or "failed")
            self.last_error = kind
            self.post(ev.Event(ev.TURN_FAILED, data={
                "turn_id": job.turn_id, "kind": kind}))
            return
        spoke = len(result.sentences) + (1 if key else 0)
        follow = bool(result.ok and not result.shortcut and result.sentences
                      and result.sentences[-1].rstrip("\"')”’ ").endswith("?"))
        self.last_error = ""
        self._done(job, spoke, follow)

    def _model(self, job, name, user, audio_in, transcript, lang, strings,
               cancel, deadline, model_setting, count_turn):
        data = self.settings.data
        row = self.providers.get(name)
        model = model_setting or row.default_model
        notes = self.memories.for_prompt(
            data["assistant"]["memories_in_prompt_tokens"])
        system = prompt.build(
            data["owner"]["name"], notes["items"], notes["more"], self.wall(),
            self.timezone(), self.synced(), audio_in)
        self.memories.touch([m["id"] for m in notes["items"]])
        turn = loop_mod.TurnInput(
            system, self.session.start_turn(), user,
            self._tool_ctx(job.turn_id, lang), name, model,
            data["budget"]["max_output_tokens"], job.turn_id,
            session_id=self.session.id, transcript=transcript,
            audio_in=audio_in, lang=lang,
            max_steps=data["budget"]["max_tool_steps"],
            count_turn=count_turn)
        loop = loop_mod.Loop(self.client(name), self.registry, self.meter,
                             row.schema_flavour)
        result = loop.run(turn, lambda s: self._say(job.turn_id, s), cancel,
                          deadline, strings)
        return result, (name, model)

    def _wants_fallback(self, result):
        second = self.settings.data["assistant"]["fallback_provider"]
        if not second or second == self.settings.data["assistant"]["provider"]:
            return False
        if not self._has_key(second):
            return False
        if result.error is not None:
            return result.error.kind in FALLBACK_KINDS
        return result.refused in FALLBACK_REFUSALS

    def _fallback_input(self, second, job, transcript, result):
        """The user message for the second provider, or None if it could not
        make use of this turn: it needs the words, or ears of its own."""
        if transcript or result.transcript:
            return {"role": "user",
                    "content": transcript or result.transcript}, False
        if self.providers.get(second).audio_in_chat:
            return stt.audio_message(job.pcm), True
        return None

    # ---- finishing ----

    def _done(self, job, spoke, follow_up):
        self.post(ev.Event(ev.TURN_DONE, data={
            "turn_id": job.turn_id, "spoke": spoke,
            "follow_up": bool(follow_up)}))

    def _fail(self, job, kind, sentence):
        self.last_error = kind
        self._say(job.turn_id, sentence)
        self.post(ev.Event(ev.TURN_FAILED, data={"turn_id": job.turn_id,
                                                 "kind": kind}))

    def _log(self, job, result, plan, started, used):
        """The journal line, and the owner's own history if they asked for one.
        No words in the first."""
        total = int((time.monotonic() - started) * 1000)
        first = result.latency_to_first_sentence
        log.info("turn %s: hear=%s tools=%s steps=%d in=%d out=%d first_ms=%s "
                 "total_ms=%d outcome=%s", job.turn_id, plan.mode,
                 ",".join(result.tools) or "-", result.steps,
                 result.input_tokens, result.output_tokens,
                 "-" if first is None else int(first * 1000), total,
                 "ok" if result.ok else (
                     result.error.kind if result.error is not None
                     else result.refused or "stopped"))
        if not self.settings.data["assistant"]["conversation_log"] \
                or self.db is None:
            return
        try:
            conn = self.db.conn()
            conn.execute(
                "INSERT INTO turn_log (at, turn_id, transcript, reply, "
                "tools_json) VALUES (?,?,?,?,?)",
                (self.wall(), job.turn_id, result.transcript,
                 " ".join(result.sentences), json.dumps(result.tools)))
            conn.execute(
                "DELETE FROM turn_log WHERE id <= (SELECT id FROM turn_log "
                "ORDER BY id DESC LIMIT 1 OFFSET ?)", (TURN_LOG_KEPT,))
        except Exception:
            log.exception("the conversation log could not be written")


class _Refused(Exception):
    """The meter said no before a request was made."""


def _why(result):
    if result.error is not None:
        return result.error.kind
    return result.refused or "failed"

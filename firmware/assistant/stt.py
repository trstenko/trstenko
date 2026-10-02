"""Speech to text: turning what the microphone heard into words.

Three ways, chosen by who the owner's provider is (plan 04):

- **In the chat request** (Gemini). The audio goes into the same request as the
  question, so there is one round trip instead of two, and the model returns the
  transcript with its answer (`speech_text.HeardFilter`). `audio_message` builds
  that message.
- **An upload** (OpenAI, Groq): a multipart POST to `/audio/transcriptions`, built
  by hand over `http.client`. Faster to first word than a chat model, and the
  only way for a provider with its own speech service.
- **A chat request that only transcribes**, for an owner whose chat provider
  cannot hear at all (Ollama on their PC, Grok): a second key, usually Gemini
  or Groq, is used for the hearing and the first for the thinking.

`plan()` picks between them and says why when it cannot.

The audio is WAV built in memory: a 44-byte header in front of the PCM, no file,
no library. It is never written to disk, and a scenario checks that nothing is.

What comes back is cleaned of what a recogniser makes of silence. Whisper-family
models answer an empty room with "Thank you for watching" and "[BLANK_AUDIO]",
and a device that took those for a question would answer a room that said
nothing (plan 03: "STT empty, or only noise: nothing; end quietly").
"""

import base64
import json
import logging
import os
import random
import re
import struct
import time

from . import provider as prov
from .table import TableError
from .transport import (AUTH, BAD_REQUEST, CANCELLED, Cancel, Deadline,
                        Endpoint, ProviderError, classify)

log = logging.getLogger("trstenko.stt")

RATE = 16000
SAMPLE_BYTES = 2
# Shorter than this is a click, longer than this is not an utterance (the
# capture itself is capped at 12s).
MIN_AUDIO_SECONDS = 0.3
MAX_AUDIO_SECONDS = 15.0
MAX_TRANSCRIPT = 600

TRANSCRIBE_PROMPT = ("Transcribe the audio exactly, in the language spoken. "
                     "Reply with only the words, nothing else. If there is no "
                     "speech, reply with nothing.")

# The order an owner with several keys is served in: Gemini hears Slovenian
# and costs nothing on its free tier, Groq's Whisper is free, OpenAI is paid.
PREFERENCE = ("gemini", "groq", "openai")

_NOISE = re.compile(
    r"^\s*(?:[\[\(\*][^\]\)\*]*[\]\)\*]|[\W_]*)\s*$")
_HALLUCINATIONS = (
    "thanks for watching", "thank you for watching", "please subscribe",
    "subscribe to my channel", "blank_audio", "hvala za ogled",
)


def wav_from_pcm(pcm, rate=RATE):
    """A WAV file as bytes, from 16-bit mono PCM."""
    pcm = bytes(pcm)
    if len(pcm) % 2:
        pcm = pcm[:-1]
    header = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
              + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * SAMPLE_BYTES,
                            SAMPLE_BYTES, 16)
              + b"data" + struct.pack("<I", len(pcm)))
    return header + pcm


def seconds_of(pcm, rate=RATE):
    return len(pcm) / float(rate * SAMPLE_BYTES)


def check_audio(pcm, rate=RATE):
    """Why this audio should not be sent, or None. A request for a click is
    money spent on a hallucination."""
    seconds = seconds_of(pcm, rate)
    if seconds < MIN_AUDIO_SECONDS:
        return "too short"
    if seconds > MAX_AUDIO_SECONDS:
        return "too long"
    return None


def audio_message(pcm, rate=RATE):
    """The user message for a provider that takes audio in the chat request."""
    data = base64.b64encode(wav_from_pcm(pcm, rate)).decode("ascii")
    return {"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": data,
                                                "format": "wav"}}]}


def is_noise(text):
    """Is this what a recogniser says to a room with nobody speaking?"""
    if not isinstance(text, str):
        return True
    if _NOISE.match(text):
        return True
    low = text.lower().strip(" .!?\n")
    return any(low == h or low.startswith(h) for h in _HALLUCINATIONS)


def clean_transcript(text):
    """The words, or '' when there were none worth acting on."""
    if not isinstance(text, str):
        return ""
    text = " ".join(text.split())[:MAX_TRANSCRIPT]
    return "" if is_noise(text) else text


class SttPlan:
    def __init__(self, mode, provider=None, model=None, reason=""):
        self.mode = mode            # "chat", "upload", "chat_stt" or None
        self.provider = provider
        self.model = model
        self.reason = reason

    @property
    def ok(self):
        return self.mode is not None

    def __repr__(self):
        return "SttPlan(%s %s %s)" % (self.mode, self.provider, self.reason)


def plan(providers, primary, has_key, language="en", preferred=""):
    """How this owner's speech becomes text.

    `has_key(name)` says whether a provider has a key. The primary provider
    hears for itself if it can; otherwise `preferred` (the owner's choice), then
    the first provider with a key that can, in `PREFERENCE` order. A language a
    provider has not been shown to understand is not offered to it (plan 04).
    """
    def mode_for(name):
        row = providers.get(name)
        if not row.speaks(language):
            return None
        if row.audio_in_chat:
            return "chat"
        if row.stt:
            return "upload"
        return None

    own = mode_for(primary)
    if own:
        return SttPlan(own, primary, providers.get(primary).stt["model"]
                       if own == "upload" else None)
    candidates = [preferred] if preferred else []
    candidates += [n for n in PREFERENCE if n != preferred]
    for name in candidates:
        if name not in providers or not has_key(name):
            continue
        mode = mode_for(name)
        if mode == "chat":
            return SttPlan("chat_stt", name)
        if mode == "upload":
            return SttPlan("upload", name, providers.get(name).stt["model"])
    reason = "language" if language != "en" else "no_stt"
    return SttPlan(None, reason=reason)


def _multipart(fields, filename, content_type, data):
    boundary = "----trstenko" + os.urandom(12).hex()
    out = []
    for name, value in fields:
        out.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n'
                    '%s\r\n' % (boundary, name, value)).encode("utf-8"))
    out.append(('--%s\r\nContent-Disposition: form-data; name="file"; '
                'filename="%s"\r\nContent-Type: %s\r\n\r\n'
                % (boundary, filename, content_type)).encode("utf-8"))
    out.append(data)
    out.append(("\r\n--%s--\r\n" % boundary).encode("ascii"))
    return boundary, b"".join(out)


class Transcriber:
    """Speech to text over HTTP, for the providers that have a service for it.
    Holds no key: `keys(provider)` is asked at the moment of the request."""

    def __init__(self, providers, keys, transport, user_agent="Trstenko",
                 rng=None):
        self.providers = providers
        self._keys = keys
        self.transport = transport
        self._user_agent = user_agent
        self._rng = rng or random.Random()

    def upload(self, name, pcm, language="en", vocabulary="", deadline=None,
               cancel=None, on_attempt=None, rate=RATE):
        """The transcript of `pcm` from provider `name`'s speech service, or
        '' for silence. Raises `ProviderError`."""
        provider = self.providers.get(name)
        if not provider.stt:
            raise ProviderError(BAD_REQUEST, "%s has no speech service"
                                % provider.label)
        problem = check_audio(pcm, rate)
        if problem:
            return ""
        deadline = deadline or Deadline(prov.DEFAULT_DEADLINE)
        cancel = cancel or Cancel()
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._once(provider, pcm, language, vocabulary,
                                  deadline, cancel, on_attempt, rate)
            except ProviderError as err:
                pause = prov.retry_pause(err, attempt, deadline, cancel,
                                         False, self._rng)
                if pause is None:
                    raise
                if cancel.wait(pause):
                    raise ProviderError(CANCELLED, "the turn was ended")

    def _once(self, provider, pcm, language, vocabulary, deadline, cancel,
              on_attempt, rate):
        record = prov.Attempt(provider.name, provider.stt["model"])
        started = time.monotonic()
        try:
            key = self._keys(provider.name)
            if not key:
                raise ProviderError(AUTH, "no API key has been set for %s"
                                    % provider.label)
            try:
                endpoint = Endpoint(provider.base_url(""))
            except (TableError, ValueError) as exc:
                raise ProviderError(BAD_REQUEST, str(exc))
            fields = [("model", provider.stt["model"]),
                      ("response_format", "json")]
            if language:
                fields.append(("language", language))
            if vocabulary:
                fields.append(("prompt", vocabulary[:200]))
            boundary, body = _multipart(fields, "speech.wav", "audio/wav",
                                        wav_from_pcm(pcm, rate))
            record.request_chars = len(body)
            headers = {"Content-Type": "multipart/form-data; boundary="
                       + boundary, "Accept": "application/json",
                       "User-Agent": self._user_agent,
                       "Authorization": "Bearer " + key}
            resp = self.transport.request(endpoint, "POST", provider.stt["path"],
                                          headers, body, deadline, cancel)
            try:
                record.http_status = resp.status
                raw = resp.read().decode("utf-8", "replace")
                if resp.status != 200:
                    raise classify(resp.status, raw, resp.headers)
            finally:
                resp.close()
            try:
                text = json.loads(raw).get("text", "")
            except (ValueError, AttributeError):
                raise ProviderError(prov.SERVER, "the speech service sent an "
                                    "unreadable answer")
            return clean_transcript(text)
        except ProviderError as err:
            record.kind = err.kind
            record.status = err.db_status
            record.retry_after = err.retry_after
            record.ambiguous = err.ambiguous
            record.ratelimit = err.ratelimit
            raise
        finally:
            record.latency_ms = int((time.monotonic() - started) * 1000)
            if on_attempt is not None:
                try:
                    on_attempt(record, seconds_of(pcm, rate))
                except Exception:
                    log.exception("the usage callback failed")

    def via_chat(self, client, model, pcm, deadline=None, cancel=None,
                 on_attempt=None, rate=RATE):
        """Transcribe with a chat model that takes audio, for an owner whose
        own provider cannot hear. One request, no tools, a short answer."""
        if check_audio(pcm, rate):
            return ""
        messages = [{"role": "system", "content": TRANSCRIBE_PROMPT},
                    audio_message(pcm, rate)]
        reply = client.chat(model, messages, None, 200,
                            deadline or Deadline(prov.DEFAULT_DEADLINE),
                            cancel or Cancel(), on_attempt=on_attempt)
        return clean_transcript(reply.text)

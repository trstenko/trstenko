"""HTTP to a model provider, on the standard library, and what goes wrong with it.

The device talks to a cloud service from a 512MB board on whatever Wi-Fi the
owner has, while a person waits for an answer. That is three things this module
exists to get right, in one place, so the chat client and the speech-to-text
client do not each get them half right:

- **A deadline that is the person's, not the socket's.** A request has a share
  of the turn's eight seconds, and a stalled stream must end the turn at that
  line rather than whenever TCP gives up. `Deadline` is shared by every request
  of a turn; each read is armed with what is left of it.
- **Cancelling a request that is waiting on the network.** The mute switch can
  end a turn at any moment. A blocked socket read cannot be interrupted by a
  flag, shutting the socket down from another thread wakes it on Linux and not
  on Windows, and a read that timed out leaves the standard library's socket
  file dead. So no read is ever left blocked: `_Poller` waits in slices of a
  tenth of a second and looks at the turn's `Cancel` and `Deadline` between
  them.
- **Telling failures apart.** A 429 for sending too fast, a 429 for being out of
  credit, a 400 for an invalid key and a 400 for our own bug all look alike on
  the wire and need different sentences and different retry rules (plan 05).
  `classify` is the one place that decides.

A TLS handshake on a Pi 3 A+ is real time, so one connection is kept between
the requests of a turn and dropped when idle. A kept connection can be closed
by the far end while it waits, and the first sign is a failed write, so a
request that fails before any answer byte on a reused connection is sent once
more on a fresh one. That is not a retry in plan 05's sense: nothing was
received, so nothing was billed.

Nothing here puts a secret in a message. Errors carry a short description that
has been through `redact`, never the request, and never the provider's whole
body.
"""

import email.utils
import http.client
import io
import json
import logging
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from ..storage.redact import redact

log = logging.getLogger("trstenko.provider")

# Failure kinds. `timeout` covers the turn's deadline passing as well as a
# stalled read, and `cancelled` is not a failure: somebody ended the turn.
RATE_LIMITED = "rate_limited"
QUOTA = "quota"
AUTH = "auth"
BAD_REQUEST = "bad_request"
SERVER = "server"
NETWORK = "network"
TIMEOUT = "timeout"
CANCELLED = "cancelled"

KINDS = (RATE_LIMITED, QUOTA, AUTH, BAD_REQUEST, SERVER, NETWORK, TIMEOUT,
         CANCELLED)

# What the `usage` table's `status` column holds for each kind (plan 05).
STATUS = {RATE_LIMITED: "rate_limited", QUOTA: "quota", AUTH: "auth",
          BAD_REQUEST: "error", SERVER: "error", NETWORK: "error",
          TIMEOUT: "timeout", CANCELLED: "cancelled"}

# A TLS handshake on the board is the slow part of a first request; past this a
# network that is not answering is not going to.
CONNECT_TIMEOUT = 5.0
# Between two chunks of a stream. A model that has said nothing for this long
# has stalled, whatever is left of the deadline.
STREAM_IDLE = 5.0
# A kept connection older than this is assumed closed by the far end. Longer
# than a follow-up window (5s) and a typical pause between turns in a session.
IDLE_TTL = 30.0

# After the last line of a reply, how long to wait for the server to finish the
# stream so the connection can be kept.
FINISH_WAIT = 0.25

# Bounds, so a misbehaving server cannot grow our memory (the board has 512MB
# and Piper alone takes 200MB of it).
MAX_LINE = 1 << 20
MAX_BODY = 64 * 1024
MAX_DETAIL = 200

# Failures that mean "the far end has gone away", on a connection that may have
# been idle.
_GONE = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
         http.client.RemoteDisconnected, http.client.CannotSendRequest,
         http.client.BadStatusLine, http.client.IncompleteRead)


class ProviderError(Exception):
    """A request failed, or was ended. `kind` is one of `KINDS`.

    `partial` is the text received before it failed, and the two `*_chars`
    counts let the meter estimate what a cut request cost when the provider
    never sent its usage (plan 05): the provider bills what it generated before
    we stopped reading.
    """

    def __init__(self, kind, message="", status=None, retry_after=None,
                 ambiguous=False, partial="", request_chars=0,
                 received_chars=0, ratelimit=None):
        super().__init__(message or kind)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        # A 429 that could be the per-minute limit or the daily one and the
        # body does not say. The meter decides, by whether it happens twice
        # within a minute (plan 05).
        self.ambiguous = ambiguous
        self.partial = partial
        self.request_chars = request_chars
        self.received_chars = received_chars
        self.ratelimit = ratelimit or {}

    @property
    def db_status(self):
        return STATUS[self.kind]


class Deadline:
    """How long the turn has left. One is made at the start of a turn and every
    request of that turn draws on it."""

    def __init__(self, seconds, mono=time.monotonic):
        self._mono = mono
        self.end = mono() + float(seconds)

    def remaining(self):
        return max(0.0, self.end - self._mono())

    def expired(self):
        return self._mono() >= self.end


class Cancel:
    """A turn's "stop" flag. Anything that waits on the network or on a timer
    looks at it between short waits (`_Poller`, `wait`), and `set()` also runs
    the hooks that something registered with `on_set`, for work that can be
    woken directly."""

    def __init__(self):
        self._event = threading.Event()
        self._hooks = []
        self._lock = threading.Lock()

    def set(self):
        with self._lock:
            self._event.set()
            hooks, self._hooks = self._hooks, []
        for hook in hooks:
            try:
                hook()
            except Exception:
                log.exception("a cancel hook failed")

    def is_set(self):
        return self._event.is_set()

    def wait(self, timeout):
        """Sleep that a cancel ends. True if cancelled."""
        return self._event.wait(timeout)

    def on_set(self, hook):
        """Run `hook` if this is cancelled. Returns what to pass to `off` when
        the thing it guards has finished. Runs at once if already cancelled."""
        with self._lock:
            if not self._event.is_set():
                self._hooks.append(hook)
                return hook
        hook()
        return None

    def off(self, handle):
        if handle is None:
            return
        with self._lock:
            try:
                self._hooks.remove(handle)
            except ValueError:
                pass


class Endpoint:
    """Where a provider lives: scheme, host, port and the path prefix its API
    hangs under."""

    def __init__(self, url):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("not a URL this device can talk to")
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")

    @property
    def key(self):
        return (self.scheme, self.host, self.port)

    def path(self, tail):
        return self.prefix + tail


# ---- reading what the provider says ----

def parse_retry_after(value, now=None):
    """Seconds, from a `Retry-After` header: a number, or an HTTP date."""
    if value is None:
        return None
    value = str(value).strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
            seconds = when.timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError):
            return None
    if seconds != seconds or seconds < 0:
        return None
    return min(seconds, 86400.0)


_RESET = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_UNIT = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_reset(value):
    """`x-ratelimit-reset-*` as OpenAI and Groq write it: `1s`, `6m0s`,
    `20ms`. Seconds, or None if it is not in that form."""
    if not value:
        return None
    text = str(value).strip()
    found = _RESET.findall(text)
    if not found or "".join(n + u for n, u in found) != text:
        return None
    return min(sum(float(n) * _UNIT[u] for n, u in found), 86400.0)


def ratelimit_info(headers):
    """What the provider says about its limits, from the `x-ratelimit-*`
    headers that OpenAI and Groq send (plan 05). Missing ones are left out."""
    out = {}
    for field, name in (("remaining_requests", "x-ratelimit-remaining-requests"),
                        ("remaining_tokens", "x-ratelimit-remaining-tokens")):
        try:
            out[field] = int(float(headers.get(name)))
        except (TypeError, ValueError):
            pass
    for field, name in (("reset_requests", "x-ratelimit-reset-requests"),
                        ("reset_tokens", "x-ratelimit-reset-tokens")):
        seconds = parse_reset(headers.get(name))
        if seconds is not None:
            out[field] = seconds
    return out


_KEY_INVALID = re.compile(
    r"api[_ ]?key (?:not valid|is invalid|invalid)|api_key_invalid"
    r"|incorrect api key|invalid api key|invalid_api_key|invalid x-api-key",
    re.IGNORECASE)
# Words that mean "you are out of money or over the limit you set", as opposed
# to "you are sending too fast". Daily quotas are in here too: nothing a retry
# in the next few seconds can fix (plan 05).
_QUOTA = re.compile(
    r"insufficient_quota|exceeded your current quota|out of credit"
    r"|billing|spend(?:ing)? limit|hard limit|no credits|credits"
    r"|per ?day|requests per day|tokens per day|\bRPD\b|\bTPD\b",
    re.IGNORECASE)


def _error_object(body):
    """The `error` object out of a provider's error body, in whichever of the
    shapes the providers use, or {}."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return {}
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        error = data.get("error", data)
        if isinstance(error, dict):
            return error
        if isinstance(error, str):
            return {"message": error}
    return {}


def _retry_delay(error):
    """Gemini puts the wait in `details[].retryDelay`, as `"34s"`."""
    for detail in error.get("details") or ():
        if isinstance(detail, dict) and "retryDelay" in detail:
            return parse_reset(detail["retryDelay"])
    return None


def _quota_ids(error):
    ids = []
    for detail in error.get("details") or ():
        if not isinstance(detail, dict):
            continue
        for violation in detail.get("violations") or ():
            if isinstance(violation, dict):
                ids.append(str(violation.get("quotaId", "")))
                ids.append(str(violation.get("quotaMetric", "")))
    return " ".join(ids)


def describe(error, status):
    """A short sentence about a failure, for the log and the page. The
    provider's own message, trimmed and with anything key-shaped blanked, and
    never the whole body: a bad request can quote what we sent."""
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str) or not message.strip():
        return "HTTP %s" % status
    message = " ".join(message.split())
    return redact(message[:MAX_DETAIL])


def classify(status, body, headers=None, now=None):
    """Turn an error response into a `ProviderError` (plan 05, section 4).

    `body` is the response text, `headers` a dict with lower-case names.
    """
    headers = headers or {}
    error = _error_object(body)
    detail = describe(error, status)
    text = " ".join(str(error.get(k, "")) for k in
                    ("message", "type", "code", "status"))
    retry_after = parse_retry_after(headers.get("retry-after"), now)
    if retry_after is None:
        retry_after = _retry_delay(error)
    limits = ratelimit_info(headers)

    def make(kind, ambiguous=False):
        return ProviderError(kind, detail, status=status,
                             retry_after=retry_after, ambiguous=ambiguous,
                             ratelimit=limits)

    if status in (401, 403):
        return make(AUTH)
    if status == 400:
        # Gemini answers a key it does not know with a 400, not a 401.
        return make(AUTH if _KEY_INVALID.search(text) else BAD_REQUEST)
    if status == 429:
        ids = _quota_ids(error)
        if _QUOTA.search(text) or re.search(r"PerDay|per_day", ids):
            return make(QUOTA)
        google = str(error.get("status", "")).upper() == "RESOURCE_EXHAUSTED"
        if google and not re.search(r"PerMinute|per_minute", ids):
            # Gemini sends the same status for both limits and does not always
            # say which. Treated as the short one until it happens twice.
            return make(RATE_LIMITED, ambiguous=True)
        return make(RATE_LIMITED)
    if status in (408, 500, 502, 503, 504, 529) or status >= 500:
        return make(SERVER)
    if 400 <= status < 500:
        return make(BAD_REQUEST)
    return make(SERVER)


def wrap(exc, cancel=None):
    """Turn whatever the socket layer raised into a `ProviderError`."""
    if isinstance(exc, ProviderError):
        return exc
    if cancel is not None and cancel.is_set():
        return ProviderError(CANCELLED, "the turn was ended")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ProviderError(TIMEOUT, "the AI service did not answer in time")
    if isinstance(exc, ssl.SSLCertVerificationError):
        # The Pi has no clock until it syncs, and a certificate is only valid
        # between two dates. This is the first thing that fails when it is wrong.
        return ProviderError(NETWORK, "the secure connection could not be "
                             "checked (is the clock right?)")
    if isinstance(exc, socket.gaierror):
        return ProviderError(NETWORK, "could not look up the AI service")
    if isinstance(exc, (OSError, http.client.HTTPException, EOFError)):
        return ProviderError(NETWORK, "the connection to the AI service "
                             "failed: %s" % type(exc).__name__)
    raise exc


class _Guard:
    """What a read has to respect: the turn's cancel flag and deadline, and a
    limit on how long a stream may say nothing."""

    def __init__(self, deadline, cancel, idle, mono):
        self.deadline = deadline
        self.cancel = cancel
        self.idle = idle
        self._mono = mono
        self.last = mono()

    def touch(self):
        self.last = self._mono()

    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise ProviderError(CANCELLED, "the turn was ended")
        if self.deadline is not None and self.deadline.expired():
            raise ProviderError(TIMEOUT, "the turn ran out of time")
        if self._mono() - self.last > self.idle:
            raise ProviderError(TIMEOUT, "the AI service stopped answering")


# How long one wait for data lasts before the guard is looked at again. This is
# what a cancel costs: a person who flips the mute switch is not kept waiting
# for the model, and it is short enough to cost nothing while waiting.
POLL = 0.1


class _Poller(io.RawIOBase):
    """Reads a socket in short slices so a cancel or a deadline is seen within
    `POLL` seconds, wherever the read is blocked.

    The obvious ways of ending a blocked read do not survive contact with the
    platforms: shutting the socket down from another thread wakes a read on
    Linux and not on Windows, and a read that timed out leaves the standard
    library's socket file unusable. So the read is never left blocked. It asks
    for a slice of data, looks at the guard, and asks again; the first answer
    that is data, or the end, goes up to `http.client` as if nothing had
    happened.
    """

    def __init__(self, sock, guard):
        super().__init__()
        self._sock = sock
        self._guard = guard

    def readable(self):
        return True

    def readinto(self, buffer):
        guard = self._guard
        while True:
            guard.check()
            self._sock.settimeout(POLL)
            try:
                got = self._sock.recv_into(buffer)
            except (socket.timeout, TimeoutError, BlockingIOError,
                    ssl.SSLWantReadError):
                continue
            guard.touch()
            return got

    def close(self):
        # The connection owns the socket. This only lets go of the file.
        super().close()


class _PollResponse(http.client.HTTPResponse):
    """A response whose reads go through `_Poller`."""

    def __init__(self, sock, guard, **kw):
        super().__init__(sock, **kw)
        original = self.fp
        self.fp = io.BufferedReader(_Poller(sock, guard))
        original.close()


class Response:
    """An open response. Read it with `readline` or `read`, then `close`."""

    def __init__(self, transport, endpoint, conn, resp, cancel, guard):
        self._transport = transport
        self._endpoint = endpoint
        self._conn = conn
        self._resp = resp
        self._cancel = cancel
        self._guard = guard
        self._closed = False
        self.status = resp.status
        self.headers = {k.lower(): v for k, v in resp.getheaders()}

    def readline(self):
        """One line of the body, or b'' at the end."""
        try:
            line = self._resp.readline(MAX_LINE + 1)
        except Exception as exc:
            raise wrap(exc, self._cancel)
        if len(line) > MAX_LINE:
            raise ProviderError(SERVER, "the AI service sent an unreadable "
                                "stream")
        return line

    def read(self, limit=MAX_BODY):
        """The whole body, up to `limit` bytes. A longer one is cut: it is only
        ever read to describe an error or to parse a reply that should be
        small."""
        try:
            data = self._resp.read(limit + 1)
        except Exception as exc:
            raise wrap(exc, self._cancel)
        return data[:limit]

    def finish(self):
        """Read what is left of a body whose last useful line has been seen:
        the terminating chunk after `[DONE]`. Only a response read to its end
        can go back in the pool, and a server that is slow to close a stream
        that is over is not worth waiting for."""
        self._guard.idle = FINISH_WAIT
        self._guard.touch()
        try:
            while self._resp.read(4096):
                pass
        except Exception:
            pass

    def close(self):
        """Hand the connection back if the response was read to its end and the
        server is willing to go on; otherwise drop it."""
        if self._closed:
            return
        self._closed = True
        reusable = (self._resp.isclosed() and not self._resp.will_close
                    and not (self._cancel and self._cancel.is_set()))
        self._transport._release(self._endpoint, self._conn, reusable)


class Transport:
    """One kept connection per provider, with deadlines and cancelling."""

    def __init__(self, connect_timeout=CONNECT_TIMEOUT, idle=STREAM_IDLE,
                 idle_ttl=IDLE_TTL, mono=time.monotonic, context=None):
        self.connect_timeout = connect_timeout
        self.idle = idle
        self.idle_ttl = idle_ttl
        self._mono = mono
        self._context = context
        self._pool = {}
        self._lock = threading.Lock()
        # For the scenarios: how many connections were opened, and reused.
        self.opened = 0
        self.reused = 0

    # ---- the pool ----

    def _context_for(self):
        if self._context is None:
            self._context = ssl.create_default_context()
        return self._context

    def _take(self, endpoint):
        with self._lock:
            held = self._pool.pop(endpoint.key, None)
        if held is None:
            return None
        conn, last = held
        if self._mono() - last > self.idle_ttl or conn.sock is None:
            conn.close()
            return None
        return conn

    def _new(self, endpoint, timeout):
        if endpoint.scheme == "https":
            conn = http.client.HTTPSConnection(
                endpoint.host, endpoint.port, timeout=timeout,
                context=self._context_for())
        else:
            conn = http.client.HTTPConnection(endpoint.host, endpoint.port,
                                              timeout=timeout)
        self.opened += 1
        return conn

    def _release(self, endpoint, conn, reusable):
        if not reusable:
            conn.close()
            return
        with self._lock:
            old = self._pool.pop(endpoint.key, None)
            self._pool[endpoint.key] = (conn, self._mono())
        if old is not None:
            old[0].close()

    def close(self):
        with self._lock:
            held, self._pool = list(self._pool.values()), {}
        for conn, _ in held:
            conn.close()

    # ---- a request ----

    def request(self, endpoint, method, path, headers, body, deadline=None,
                cancel=None):
        """Send a request and return its `Response`, headers read. Raises
        `ProviderError`."""
        for attempt in (0, 1):
            if cancel is not None and cancel.is_set():
                raise ProviderError(CANCELLED, "the turn was ended")
            if deadline is not None and deadline.expired():
                raise ProviderError(TIMEOUT, "the turn ran out of time")
            conn = self._take(endpoint) if attempt == 0 else None
            reused = conn is not None
            if reused:
                self.reused += 1
            else:
                left = deadline.remaining() if deadline else self.connect_timeout
                conn = self._new(endpoint, min(self.connect_timeout, left))
            guard = _Guard(deadline, cancel, self.idle, self._mono)
            conn.response_class = (
                lambda sock, g=guard, **kw: _PollResponse(sock, g, **kw))
            try:
                if conn.sock is None:
                    conn.connect()
                    if cancel is not None and cancel.is_set():
                        raise ProviderError(CANCELLED, "the turn was ended")
                # Sending a request this small cannot take long; a send that
                # does is a connection that is not there.
                conn.sock.settimeout(max(0.05, min(
                    self.connect_timeout,
                    deadline.remaining() if deadline else self.connect_timeout)))
                conn.request(method, endpoint.path(path), body, headers)
                guard.touch()
                resp = conn.getresponse()
            except Exception as exc:
                conn.close()
                if (reused and attempt == 0 and isinstance(exc, _GONE)
                        and not (cancel and cancel.is_set())):
                    # The far end closed it while it sat idle. Nothing was
                    # received, so nothing was billed: go again, fresh.
                    continue
                raise wrap(exc, cancel)
            return Response(self, endpoint, conn, resp, cancel, guard)
        raise ProviderError(NETWORK, "the connection to the AI service failed")

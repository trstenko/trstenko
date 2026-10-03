"""Who is allowed to tell this device what to do.

The page is served over the device's own Wi-Fi so a phone can reach it
without an account or an internet connection. That is the right shape for
setup and the wrong shape for an open API: without this module, anyone who
can see the hotspot -- or anyone on the home network the device later joins
-- can install firmware on it, roll it back, or read its API keys.

So: a six-digit pairing PIN, generated once per device, exchanged for a token
that the page keeps for the session.

## Why a PIN and not a password

Nobody should invent a password for a desk toy, and a password they invent is
one they reuse. The PIN is generated, unique per device, and shown on the
device's own screen -- the person holding it is the person who can pair with
it, which is the property that matters. It never travels over the network in
the direction that would leak it: it is typed into a page the device itself
served.

Also the rule that makes this necessary rather than nice: the RED
cybersecurity delegated act (2022/30/EU) has been mandatory since August
2025, and EN 18031-1 wants access control on network interfaces and no
default or shared credentials. A PIN printed on every unit would be a default
credential. This one is random per device.

## Brute force

Six digits is a million combinations, which a script exhausts in minutes over
a LAN if nothing stops it. Attempts are counted and the device stops
answering for a while once they pile up -- growing with each failure, reset
by a success. That turns minutes into years, which is all a PIN has to do.

## What a token is worth

A random 32 bytes, held in memory, expiring after a day and thrown away on
restart. Losing them on restart is deliberate: a device that reboots has
almost certainly been picked up by somebody, and re-entering the PIN costs
one glance at the screen.
"""

import hmac
import os
import secrets
import time

PIN_FILE = "pairing-pin"
PIN_DIGITS = 6
# An owner may choose their own, up to this long. Digits only: it is typed on
# a phone's number pad, and the lockout is what makes a short one safe.
PIN_MAX_DIGITS = 10

TOKEN_TTL = 24 * 3600

# The first few misses are honest typing. After that the wait doubles, so a
# script gets a handful of guesses an hour and a person barely notices.
FREE_ATTEMPTS = 3
BASE_LOCKOUT = 5.0
MAX_LOCKOUT = 600.0


def weak_pin(pin):
    """Why `pin` will not do as a pairing PIN, or None if it will. The rule is
    short on purpose: the lockout stops guessing, so this only refuses the
    handful that would be the first guesses."""
    if not isinstance(pin, str) or not pin.isdigit() \
            or not PIN_DIGITS <= len(pin) <= PIN_MAX_DIGITS:
        return "A PIN is %d to %d digits." % (PIN_DIGITS, PIN_MAX_DIGITS)
    steps = {(int(b) - int(a)) % 10 for a, b in zip(pin, pin[1:])}
    if steps <= {0} or steps <= {1} or steps <= {9}:
        return "Choose a PIN that is not one digit repeated or a run."
    return None


class Auth:
    def __init__(self, data_root, clock=time.time, log=None):
        self.data_root = data_root
        self.clock = clock
        self.log = log or (lambda line: None)
        self._tokens = {}
        self._failures = 0
        self._locked_until = 0.0
        self.pin = self._load_or_create_pin()

    # ---- the PIN ----

    @property
    def path(self):
        return os.path.join(self.data_root, PIN_FILE)

    def _load_or_create_pin(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                found = fh.read().strip()
            if found.isdigit() and PIN_DIGITS <= len(found) <= PIN_MAX_DIGITS:
                return found
        except OSError:
            pass

        pin = "".join(secrets.choice("0123456789") for _ in range(PIN_DIGITS))
        try:
            os.makedirs(self.data_root, exist_ok=True)
            # Written before the mode is tightened, so there is a moment where
            # it is readable. On a single-user device that is acceptable; the
            # alternative is an O_CREAT dance for a file whose contents are
            # displayed on the front of the device anyway.
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(pin)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            self.log("pairing PIN could not be saved (%s); it will change on "
                     "the next restart" % exc)
        return pin

    def set_pin(self, pin, keep=None):
        """The owner's own PIN. Returns None, or the reason it was refused.
        Every other session is signed out; `keep` is the token of the one
        making the change."""
        problem = weak_pin(pin)
        if problem:
            return problem
        try:
            os.makedirs(self.data_root, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(pin)
            os.chmod(self.path, 0o600)
        except OSError:
            return "The PIN could not be saved."
        self.pin = pin
        self._tokens = {t: exp for t, exp in self._tokens.items() if t == keep}
        self.log("the pairing PIN was changed from the page")
        return None

    def reset_pin(self):
        """Forget the PIN and make a new one. What the setup gesture does, so
        a device handed to someone else stops answering to whoever paired
        with it before."""
        try:
            os.remove(self.path)
        except OSError:
            pass
        self._tokens.clear()
        self.pin = self._load_or_create_pin()
        return self.pin

    # ---- pairing ----

    @property
    def locked_for(self):
        return max(0.0, self._locked_until - self.clock())

    def verify(self, pin):
        """(token, seconds_to_wait). A wrong PIN gives (None, wait)."""
        wait = self.locked_for
        if wait > 0:
            return None, wait

        # Compared this way so the time taken says nothing about how much of
        # the PIN was right.
        if not hmac.compare_digest(str(pin or ""), self.pin):
            self._failures += 1
            if self._failures > FREE_ATTEMPTS:
                over = self._failures - FREE_ATTEMPTS
                delay = min(MAX_LOCKOUT, BASE_LOCKOUT * (2 ** (over - 1)))
                self._locked_until = self.clock() + delay
                self.log("pairing locked for %.0fs after %d wrong PINs"
                         % (delay, self._failures))
            return None, self.locked_for

        self._failures = 0
        self._locked_until = 0.0
        token = secrets.token_hex(32)
        self._tokens[token] = self.clock() + TOKEN_TTL
        self.log("a device paired with the update page")
        return token, 0.0

    def confirm(self, pin):
        """Ask for the PIN again before something that cannot be undone. The
        same lockout as pairing, but no new token and no 'paired' line.
        Returns (ok, seconds_to_wait)."""
        wait = self.locked_for
        if wait > 0:
            return False, wait
        if not hmac.compare_digest(str(pin or ""), self.pin):
            self._failures += 1
            if self._failures > FREE_ATTEMPTS:
                over = self._failures - FREE_ATTEMPTS
                self._locked_until = self.clock() + min(
                    MAX_LOCKOUT, BASE_LOCKOUT * (2 ** (over - 1)))
            return False, self.locked_for
        self._failures = 0
        return True, 0.0

    def check(self, token):
        if not token:
            return False
        expires = self._tokens.get(token)
        if expires is None:
            return False
        if self.clock() >= expires:
            self._tokens.pop(token, None)
            return False
        return True

    def revoke(self, token):
        self._tokens.pop(token, None)

    def status(self):
        return {
            "paired_sessions": len(self._tokens),
            "locked_for": round(self.locked_for, 1),
            "failures": self._failures,
        }

"""The remembered session (task 3062): a cookie the engine issues, rotates and can revoke.

Why a cookie. It is the only thing that survives closing the browser without ever passing through the JavaScript of
the page (no `sessionStorage`, `localStorage` or `window.name`, the channel of bug 1038). The browser keeps it
encrypted with the key of the system, and the engine keeps only its SHA-256.

    hokma_resume=<256 bits, base64url>; HttpOnly; SameSite=Strict; Path=/api/session; Max-Age=2592000; Secure

There is no `Domain`, so it belongs to the exact host. Cookies ignore the port, so the host is a name unique to this
installation (`config.browser_host()`), and the cookie alone is never enough: `resume` also needs a signature made
with a private key the browser keeps for that origin (WebCrypto ECDSA P-256, not extractable, in IndexedDB, which the
browser isolates by port). The engine keeps the PUBLIC key in the verifier. A copy of the cookie that reaches another
server on the same name and another port is useless without the key, and presenting it with a bad signature changes
nothing: nothing is rotated and nothing is revoked, so it cannot be used to lock the owner out.

The verifier is a JSON `{lineage, gen, hash, prev_hash, prev_until, created_at, last_used_at, pub_x, pub_y}` in the
system credential store (`keyring`, service `hokma-session`, entry `resume`). The value of the cookie is never stored, only
its SHA-256. Without a usable credential store the verifier lives in the memory of this process only: closing and
reopening the browser works, a restart of the computer asks to enter again.

Rotation. Every successful use (cookie AND signature) gives a new value (`gen` + 1) and keeps the previous hash for 10 seconds, accepted ONE
time (two tabs that load at once send the same cookie). The engine also remembers in memory the last 8 hashes of the
lineage. A value that matches an old hash outside that window, or after its single use, AND comes with a valid signature
of the lineage key, is a copy used by someone who holds the key too: the lineage is revoked (verifier deleted, owner confirmation of the lineage forgotten) and every later use of any hash of that lineage
answers `revoked` instead of `denied`, so the real owner sees the notice the next time. The caller records the event.

Expiry. 30 days after the last use. The clock is injectable so a test can move it 31 days.

Comparisons of hashes are constant time and never stop at the first match.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from hokma.connections.store import _usable as keyring_usable
from hokma.core import config, p256, session

SERVICE = "hokma-session"
ENTRY = "resume"
COOKIE_NAME = "hokma_resume"
COOKIE_PATH = "/api/session"                # covers challenge, resume and enter; one prefix, no Domain
MAX_AGE_SECONDS = 2592000                 # 30 days
PREVIOUS_WINDOW_SECONDS = 10
REMEMBERED_HASHES = 8
REVOKED_MEMORY = 64
VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")       # 32 random bytes, base64url without padding
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

OK, DENIED, REVOKED = "ok", "denied", "revoked"

def host_allows_cookie(host: str | None) -> bool:
    """True when the Host header of the request is the name of this installation (any port: the app's Host check
    already limits it to the real one)."""
    return bool(host) and host.lower().split(":", 1)[0] == config.browser_host()


def cookie_header(value: str) -> str:
    """The `Set-Cookie` line that stores `value`."""
    return (f"{COOKIE_NAME}={value}; HttpOnly; SameSite=Strict; Path={COOKIE_PATH}; "
            f"Max-Age={MAX_AGE_SECONDS}; Secure")


def expired_cookie_header() -> str:
    """The `Set-Cookie` line that makes the browser drop the cookie."""
    return f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path={COOKIE_PATH}; Max-Age=0; Secure"


def digest_of(value: str) -> str:
    return hashlib.sha256(value.encode("ascii", "replace")).hexdigest()


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("ascii", "replace"), b.encode("ascii", "replace"))


def _any_same(candidate: str, known: list[str]) -> bool:
    found = False
    for item in known:                                    # no early exit
        found |= _same(candidate, item)
    return found


@dataclass
class Verifier:
    lineage: str
    gen: int
    hash: str
    prev_hash: str | None
    prev_until: float
    created_at: float
    last_used_at: float
    pub_x: str | None = None                              # public key of the browser: 32 bytes each, base64url
    pub_y: str | None = None

    def dumps(self) -> str:
        return json.dumps({"lineage": self.lineage, "gen": self.gen, "hash": self.hash, "prev_hash": self.prev_hash,
                           "prev_until": self.prev_until, "created_at": self.created_at,
                           "last_used_at": self.last_used_at, "pub_x": self.pub_x, "pub_y": self.pub_y},
                          separators=(",", ":"))

    @classmethod
    def loads(cls, raw: str | None) -> Verifier | None:
        """A stored verifier, or None for anything damaged (a damaged entry is the same as no entry)."""
        try:
            data = json.loads(raw or "")
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        try:
            lineage, gen, digest = data["lineage"], data["gen"], data["hash"]
            prev_hash, prev_until = data["prev_hash"], data["prev_until"]
            created_at, last_used_at = data["created_at"], data["last_used_at"]
        except KeyError:
            return None
        numbers = (prev_until, created_at, last_used_at)
        if (not isinstance(lineage, str) or not lineage or not isinstance(gen, int) or isinstance(gen, bool)
                or not isinstance(digest, str) or not HASH_PATTERN.fullmatch(digest)
                or not (prev_hash is None or (isinstance(prev_hash, str) and HASH_PATTERN.fullmatch(prev_hash)))
                or any(isinstance(n, bool) or not isinstance(n, (int, float)) for n in numbers)):
            return None
        pub_x, pub_y = data.get("pub_x"), data.get("pub_y")           # older entries have none: they cannot resume
        if pub_x is not None or pub_y is not None:
            if not (isinstance(pub_x, str) and isinstance(pub_y, str) and p256.public_key_is_valid(pub_x, pub_y)):
                return None
        return cls(lineage, gen, digest, prev_hash, float(prev_until), float(created_at), float(last_used_at),
                   pub_x, pub_y)


@dataclass(frozen=True)
class Issued:
    """A new lineage. `value` goes to the browser in `Set-Cookie` and nowhere else."""
    value: str = field(repr=False)
    lineage: str
    persistent: bool                                      # the verifier is in the credential store, not only in memory


@dataclass(frozen=True)
class Outcome:
    status: str                                           # OK, DENIED or REVOKED
    cookie: str | None = field(default=None, repr=False)  # the new value to set; None when there is nothing to set
    lineage: str | None = None
    fresh_revocation: bool = False                        # this call is the one that revoked the lineage
    clear_cookie: bool = False                            # the browser should drop what it holds
    bad_signature: bool = False                           # the cookie matched a lineage but the signature did not


class ResumeStore:
    """The verifier of the remembered session. `backend` is a `keyring` backend (tests pass one in memory; None uses
    the system one) and `clock` returns seconds since the epoch."""

    def __init__(self, backend=None, clock: Callable[[], float] = time.time) -> None:
        self._backend = backend
        self._clock = clock
        self._lock = threading.RLock()
        self._memory: Verifier | None = None                      # used only when the credential store cannot hold it
        self._history: dict[str, deque[str]] = {}
        self._revoked: deque[str] = deque(maxlen=REVOKED_MEMORY)

    # ------------------------------------------------------------------------------ the credential store

    def _credentials(self):
        try:
            import keyring

            backend = self._backend or keyring.get_keyring()
            return backend if keyring_usable(backend) else None
        except Exception:
            return None

    def available(self) -> bool:
        """True when the verifier survives a restart of the computer."""
        return self._credentials() is not None

    def _load(self) -> Verifier | None:
        if self._memory is not None:
            return self._memory
        backend = self._credentials()
        if backend is None:
            return None
        try:
            found = Verifier.loads(backend.get_password(SERVICE, ENTRY))
        except Exception:
            return None
        if found is not None:
            self._history.setdefault(found.lineage, deque([found.hash], maxlen=REMEMBERED_HASHES))
        return found

    def _save(self, verifier: Verifier) -> bool:
        """True when it reached the credential store. Otherwise it stays in memory."""
        backend = self._credentials()
        if backend is not None:
            try:
                backend.set_password(SERVICE, ENTRY, verifier.dumps())
                self._memory = None
                return True
            except Exception:
                self._drop_stored(backend)
        self._memory = verifier
        return False

    def _drop_stored(self, backend=None) -> None:
        backend = backend or self._credentials()
        if backend is None:
            return
        try:
            backend.delete_password(SERVICE, ENTRY)
        except Exception:
            pass                                              # nothing there, or the store refused: memory is cleared anyway

    def _clear(self, verifier: Verifier | None) -> None:
        self._memory = None
        self._drop_stored()
        if verifier is not None:
            self._history.pop(verifier.lineage, None)
            session.forget_lineage_owner(verifier.lineage)

    # -------------------------------------------------------------------------------------------- the use

    def issue(self, public_key: tuple[str, str], nonce: str) -> Issued | None:
        """A new lineage bound to the public key of the browser `(x, y)` (base64url, 32 bytes each, on the curve; the
        caller checked them) and the owner confirmation `nonce` of the tab that entered. It replaces any lineage that
        existed. None, and nothing stored, when the confirmation cannot be registered."""
        with self._lock:
            self._clear(self._load())
            now = self._clock()
            value, lineage = secrets.token_urlsafe(32), secrets.token_hex(8)
            if not session.register_lineage_owner(lineage, nonce):
                return None
            verifier = Verifier(lineage, 1, digest_of(value), None, 0.0, now, now, public_key[0], public_key[1])
            persistent = self._save(verifier)
            self._history[lineage] = deque([verifier.hash], maxlen=REMEMBERED_HASHES)
            return Issued(value, lineage, persistent)

    def knows(self, value: str | None) -> bool:
        """True when `value` is the current cookie, the previous one inside its window, or an older one of the live
        lineage. Read only: nothing is rotated, saved or revoked. It lets the challenge endpoint refuse strangers."""
        if not isinstance(value, str) or not VALUE_PATTERN.fullmatch(value):
            return False
        candidate = digest_of(value)
        with self._lock:
            verifier = self._load()
            if verifier is None:
                return False
            known = [verifier.hash, *(h for h in (verifier.prev_hash,) if h), *self._history.get(verifier.lineage, ())]
            return _any_same(candidate, known)

    def resume(self, value: str | None, verify: Callable[[str, str], bool], nonce: str | None = None) -> Outcome:
        """Check the cookie and rotate it. OK carries the new value (None when the previous value was accepted inside
        its window: the browser already holds the newer one).

        `verify(pub_x, pub_y)` checks the signature against the public key of the lineage the cookie belongs to. It runs
        after the cookie is found to be a hash of that lineage (current, previous or older) and BEFORE anything is
        rotated, saved or revoked: with a failing signature the answer is DENIED (`bad_signature`) and the verifier is
        untouched, so a copy of the cookie without the key can neither get in nor lock the owner out. A lineage with
        no public key (an entry from before the key existed) never verifies.

        With `nonce`, the owner confirmation of the lineage is registered while the lock is still held, so a
        revocation can never slip in between the check and the registration (and `session` refuses a late
        registration for a revoked lineage anyway)."""
        if not isinstance(value, str) or not VALUE_PATTERN.fullmatch(value):
            return Outcome(DENIED)
        candidate = digest_of(value)
        with self._lock:
            if _any_same(candidate, list(self._revoked)):
                return Outcome(REVOKED, clear_cookie=True)
            verifier = self._load()
            if verifier is None:
                return Outcome(DENIED)
            now = self._clock()
            if now - verifier.last_used_at > MAX_AGE_SECONDS:
                self._clear(verifier)
                return Outcome(DENIED, clear_cookie=True)
            remembered = list(self._history.get(verifier.lineage, ()))
            is_current = _same(candidate, verifier.hash)
            is_previous = verifier.prev_hash is not None and _same(candidate, verifier.prev_hash)
            is_older = _any_same(candidate, remembered)
            if not (is_current or is_previous or is_older):
                return Outcome(DENIED)
            if not self._signed(verifier, verify):
                return Outcome(DENIED, lineage=verifier.lineage, bad_signature=True)
            if is_current:
                outcome = self._rotate(verifier, now)
                return self._register(outcome, nonce)
            if is_previous and verifier.prev_until > 0 and now <= verifier.prev_until:
                verifier.prev_until = 0.0                     # one use only
                self._save(verifier)
                return self._register(Outcome(OK, None, verifier.lineage), nonce)
            return self._revoke(verifier)

    @staticmethod
    def _signed(verifier: Verifier, verify: Callable[[str, str], bool]) -> bool:
        if not verifier.pub_x or not verifier.pub_y:
            return False
        try:
            return bool(verify(verifier.pub_x, verifier.pub_y))
        except Exception:
            return False

    @staticmethod
    def _register(outcome: Outcome, nonce: str | None) -> Outcome:
        if nonce is not None and outcome.lineage and not session.register_lineage_owner(outcome.lineage, nonce):
            return Outcome(DENIED)
        return outcome

    def _rotate(self, verifier: Verifier, now: float) -> Outcome:
        value = secrets.token_urlsafe(32)
        verifier.prev_hash, verifier.prev_until = verifier.hash, now + PREVIOUS_WINDOW_SECONDS
        verifier.hash, verifier.gen, verifier.last_used_at = digest_of(value), verifier.gen + 1, now
        self._history.setdefault(verifier.lineage, deque(maxlen=REMEMBERED_HASHES)).append(verifier.hash)
        self._save(verifier)
        return Outcome(OK, value, verifier.lineage)

    def _revoke(self, verifier: Verifier) -> Outcome:
        for item in {verifier.hash, verifier.prev_hash, *self._history.get(verifier.lineage, ())}:
            if item:
                self._revoked.append(item)
        self._clear(verifier)
        return Outcome(REVOKED, lineage=verifier.lineage, fresh_revocation=True, clear_cookie=True)

    def logout(self) -> None:
        """The owner closes the session of this browser: verifier and lineage confirmation are deleted."""
        with self._lock:
            self._clear(self._load())


_store: ResumeStore | None = None
_store_lock = threading.Lock()


def store() -> ResumeStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = ResumeStore()
        return _store


def use_store_for_tests(replacement: ResumeStore | None) -> ResumeStore | None:
    """Swap the process store (None goes back to a fresh system one on next use). Returns the previous one."""
    global _store
    with _store_lock:
        previous, _store = _store, replacement
        return previous

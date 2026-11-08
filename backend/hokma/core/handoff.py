"""Short lived secrets of the remembered session, kept in the memory of the engine only.

Handoff code. The tab that registered at the numeric loopback asks for it (`POST /api/session/handoff-code`) and carries
it to the name of this installation, where `POST /api/session/enter` spends it. 80 random bits, 16 characters of
base32 in capitals, valid 60 seconds, one use. A new code replaces the previous one. Only its SHA-256 is kept.

Challenge. Before every `resume` the page asks for a challenge (`POST /api/session/challenge`) and signs it with the
private key the browser keeps for this origin. 32 random bytes, base64url, valid 30 seconds, one use. Only a holder
of a cookie the engine issued can ask for one, and the challenge is bound to that cookie. A cookie has at most 4 pending
(its own oldest is dropped for the newest, so the last tab to ask can always resume) and the book holds at most 64
(new ones are refused, none is evicted), so a flood cannot push out the challenge of a legitimate tab. A challenge is consumed by the first call that presents
it for its cookie, so it cannot be replayed.

The clock is injectable so a test can move time.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import OrderedDict
from typing import Callable

CODE_PATTERN = re.compile(r"^[A-Z2-7]{16}$")
CHALLENGE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
CODE_SECONDS = 60
CHALLENGE_SECONDS = 30
MAX_PENDING_CHALLENGES = 64
MAX_PENDING_PER_COOKIE = 4


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode("ascii", "replace")).digest()


class Handoff:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._code: tuple[bytes, float] | None = None                 # (sha256, expires_at)
        self._challenges: OrderedDict[bytes, tuple[float, bytes]] = OrderedDict()   # sha256 -> (expires_at, binding)

    # ------------------------------------------------------------------------------------------ handoff code

    def new_code(self) -> str:
        """A fresh code. Any code issued before stops working."""
        code = base64.b32encode(secrets.token_bytes(10)).decode("ascii")          # 80 bits, 16 characters, no padding
        with self._lock:
            self._code = (_digest(code), self._clock() + CODE_SECONDS)
        return code

    def check_code(self, code: str | None) -> bool:
        """True (and the code is spent) when `code` is the current one and still valid."""
        if not isinstance(code, str) or not CODE_PATTERN.fullmatch(code):
            return False
        candidate = _digest(code)
        with self._lock:
            held = self._code
            if held is None:
                return False
            matches = hmac.compare_digest(candidate, held[0])
            if self._clock() > held[1]:
                self._code = None                                      # expired: gone whether it matched or not
                return False
            if matches:
                self._code = None
            return matches

    # ----------------------------------------------------------------------------------------- challenges

    def new_challenge(self, binding: str) -> str | None:
        """A challenge bound to one cookie (`binding` is the SHA-256 of its value). When that cookie already has
        MAX_PENDING_PER_COOKIE pending, its own oldest one is dropped, so the last tab to ask can always resume; the
        challenges of other cookies are never touched. None when the whole book is full."""
        challenge = secrets.token_urlsafe(32)                          # 32 bytes, 43 characters
        bound = _digest(binding)
        now = self._clock()
        with self._lock:
            self._purge(now)
            if len(self._challenges) >= MAX_PENDING_CHALLENGES:
                return None
            mine = [held for held, (_, other) in self._challenges.items() if hmac.compare_digest(other, bound)]
            while len(mine) >= MAX_PENDING_PER_COOKIE:
                del self._challenges[mine.pop(0)]                      # the oldest of THIS cookie: the last tab to ask wins
            self._challenges[_digest(challenge)] = (now + CHALLENGE_SECONDS, bound)
        return challenge

    def consume_challenge(self, challenge: str | None, binding: str | None) -> bool:
        """True when `challenge` was issued for this cookie, is not expired and had not been used. A challenge that
        exists but belongs to another cookie is not spent (it is not the caller's to burn)."""
        if not isinstance(challenge, str) or not CHALLENGE_PATTERN.fullmatch(challenge) or not binding:
            return False
        candidate, bound = _digest(challenge), _digest(binding)
        now = self._clock()
        with self._lock:
            self._purge(now)
            found = False
            for held, (_, owner) in list(self._challenges.items()):    # no early exit
                if hmac.compare_digest(candidate, held) and hmac.compare_digest(bound, owner):
                    found = True
                    del self._challenges[held]
            return found

    def pending_challenges(self) -> int:
        with self._lock:
            self._purge(self._clock())
            return len(self._challenges)

    def _purge(self, now: float) -> None:
        for held in [h for h, (until, _) in self._challenges.items() if now > until]:
            del self._challenges[held]


_book: Handoff | None = None
_book_lock = threading.Lock()


def book() -> Handoff:
    global _book
    with _book_lock:
        if _book is None:
            _book = Handoff()
        return _book


def use_book_for_tests(replacement: Handoff | None) -> Handoff | None:
    """Swap the process book (None goes back to a fresh one on next use). Returns the previous one."""
    global _book
    with _book_lock:
        previous, _book = _book, replacement
        return previous

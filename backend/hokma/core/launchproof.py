"""Launch proof (SPEC-015, bug 1228): only the supervisor can start the engine as a server.

The command 'printf token | hokma engine --port N &' run from an agent's shell used to be enough, because the engine
accepted any token written to its standard input. Now the engine also needs a proof of launch that only a live
supervisor can make.

How it works
------------
* Each time the supervisor starts an engine it makes a proof: a random nonce (32 bytes) and an HMAC-SHA256 of it. The
  proof is one more line on the engine's standard input, the pipe that only this Popen holds. It is never an
  argument, never in the environment and never in a file, and the engine reads it once and drops it.
* The engine asks the supervisor, on the local control channel (a 0600 socket or a user-only named pipe), whether the
  nonce is one it issued. The supervisor answers yes ONCE: the nonce is forgotten at the first redemption, and it
  expires after PROOF_TTL_SECONDS. A copied or replayed proof is refused, and so is a guessed one (256 bits).
* The HMAC key lives in the system credential store (service "hokma", entry "engine-launch-key"), created by the
  supervisor. When the engine can read it, it checks the signature too, so a fake supervisor (another data folder
  with a control socket that says yes to everything) is not enough. With no usable credential store there is no
  key: the signature is then only a format and the control channel is the whole check.

An engine that fails the check exits with an error and never marks the process as the server (core/processguard.py).

What it is not: a sandbox. A process of the same user that reads the credential store AND runs its own control
server could still forge a proof. Standard library only (keyring is imported when needed).
"""
from __future__ import annotations

import hmac
import re
import secrets
import threading
import time
from hashlib import sha256
from typing import Callable

KEY_SERVICE = "hokma"
KEY_ACCOUNT = "engine-launch-key"
PROOF_TTL_SECONDS = 30.0
PROOF_PATTERN = re.compile(r"^([0-9a-f]{64})\.([0-9a-f]{64})$")
NONCE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
LAUNCH_WORD = "launch "                       # the control channel word: "launch <nonce>"


def _usable(backend) -> bool:
    module = type(backend).__module__
    if module.startswith(("keyring.backends.fail", "keyring.backends.null", "keyrings.alt")):
        return False                          # no store, a store that discards, or a plain-text file store
    children = getattr(backend, "backends", None)
    if children is not None:
        return any(_usable(child) for child in children)
    return getattr(backend, "priority", 0) > 0


def stored_key(create: bool = False) -> bytes | None:
    """The launch key of the credential store, or None when there is no usable store (or no key yet, without
    'create'). The supervisor creates it; the engine only reads it."""
    try:
        import keyring

        if not _usable(keyring.get_keyring()):
            return None
        value = keyring.get_password(KEY_SERVICE, KEY_ACCOUNT)
        if (not value or not NONCE_PATTERN.fullmatch(value)) and create:
            value = secrets.token_hex(32)
            keyring.set_password(KEY_SERVICE, KEY_ACCOUNT, value)
        return bytes.fromhex(value) if value and NONCE_PATTERN.fullmatch(value) else None
    except Exception:                          # a store that fails is a store that is not there
        return None


def sign(nonce: str, key: bytes) -> str:
    return hmac.new(key, nonce.encode("ascii"), sha256).hexdigest()


class Authority:
    """The supervisor's side: issues proofs and redeems each nonce once. Memory only."""

    def __init__(self, key: Callable[[], bytes | None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._key = (key or (lambda: stored_key(create=True)))() or secrets.token_bytes(32)
        self._issued: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self) -> str:
        nonce = secrets.token_hex(32)
        now = self._clock()
        with self._lock:
            self._issued = {n: t for n, t in self._issued.items() if t > now}      # forget the expired ones
            self._issued[nonce] = now + PROOF_TTL_SECONDS
        return f"{nonce}.{sign(nonce, self._key)}"

    def redeem(self, nonce: str) -> bool:
        """True the first time a live nonce comes back, False every other time."""
        if not NONCE_PATTERN.fullmatch(nonce):
            return False
        with self._lock:
            expires = self._issued.pop(nonce, None)
        return expires is not None and expires > self._clock()


def verify(proof: str, ask_supervisor: Callable[[str], bool] | None = None,
           key: Callable[[], bytes | None] | None = None) -> bool:
    """The engine's side: True only when the proof is well formed, signed with the stored key (when there is one)
    and redeemed by the supervisor of this data folder."""
    match = PROOF_PATTERN.fullmatch(proof or "")
    if match is None:
        return False
    nonce, mac = match.groups()
    secret = (key or stored_key)()
    if secret is not None and not hmac.compare_digest(mac, sign(nonce, secret)):
        return False
    return (ask_supervisor or _ask_supervisor)(nonce)


def _ask_supervisor(nonce: str) -> bool:
    from hokma.core import control

    reply = control.request(LAUNCH_WORD + nonce)
    return bool(reply and reply.get("ok") is True)

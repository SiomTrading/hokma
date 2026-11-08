"""Is the program that listens on Hokma's port really this engine? (task 3078)

The panel address `hokma.localhost:8765` is predictable. While Hokma is off, another program on this computer could
take port 8765 and answer there. Before `hokma start` (and so the "Hokma" shortcut) opens the browser on a running
Hokma, it asks the listener for a proof only this engine can give:

    GET /api/engine-proof?nonce=<32 hex>   ->   {"proof": HMAC-SHA256(key, "hokma-engine:<port>:<nonce>")}

`key` is 32 random bytes in `<data>/identity.key` (mode 0600), created once and never sent anywhere. The nonce is new
on every check, so a recorded answer is useless, and the port is inside the proof, so an impostor that relays the
question to the real engine on another port gets a proof for that other port. The answer carries nothing else: no
token, no code, no owner confirmation.

Standard library only.
"""
import hashlib
import errno
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request

from hokma.core import config

KEY_BYTES = 32
NONCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
PROOF_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ROUTE = "/api/engine-proof"
REQUEST_TIMEOUT_SECONDS = 2.0
IPV6_LOOPBACK = "::1"
IPV6_TIMEOUT_SECONDS = 6.0            # Windows retries a refused connect to a closed local port for about 2 seconds
NOBODY_THERE = frozenset(getattr(errno, name) for name in ("ECONNREFUSED", "ENETUNREACH", "EADDRNOTAVAIL", "EAFNOSUPPORT",
                                                          "EHOSTUNREACH", "WSAECONNREFUSED", "WSAEADDRNOTAVAIL")
                         if hasattr(errno, name))
BOOT_WAIT_SECONDS = 6.0               # a supervisor that just wrote its lock may not be listening yet


def key_path():
    return config.hokma_data_dir() / "identity.key"


def _read(path) -> bytes | None:
    try:
        raw = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return bytes.fromhex(raw) if re.fullmatch(r"[0-9a-f]{64}", raw) else None


def key() -> bytes:
    """The identity key of this installation, created on first use (exclusive create, so two starts never disagree)."""
    path = key_path()
    found = _read(path)
    if found is not None:
        return found
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = secrets.token_bytes(KEY_BYTES)
    try:
        if path.exists():
            path.unlink()                                  # damaged content: replaced
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(fresh.hex() + "\n")
    except FileExistsError:
        pass
    return _read(path) or fresh


def proof(nonce: str, port: int) -> str:
    message = f"hokma-engine:{port}:{nonce}".encode("ascii")
    return hmac.new(key(), message, hashlib.sha256).hexdigest()


def _ask(port: int, host: str = config.DEFAULT_HOST) -> str:
    """'ok', 'wrong' (it answered, but it is not this engine) or 'silent' (nothing usable answered)."""
    nonce = secrets.token_hex(16)
    shown = f"[{host}]" if ":" in host else host
    url = f"http://{shown}:{port}{ROUTE}?nonce={nonce}"
    try:
        timeout = IPV6_TIMEOUT_SECONDS if ":" in host else REQUEST_TIMEOUT_SECONDS
        with urllib.request.urlopen(url, timeout=timeout) as reply:
            body = reply.read(512)
    except urllib.error.HTTPError:
        return "wrong"                                    # a server answered, and it does not know the route
    except urllib.error.URLError as error:
        reason = error.reason
        # Nobody there: refused, or no such address on this computer. Anything else (a connection that opens and then
        # hangs or is cut) means a program is there and it is not answering like this engine.
        nobody = isinstance(reason, OSError) and getattr(reason, "errno", None) in NOBODY_THERE
        return "silent" if nobody else "wrong"
    except ValueError:
        return "silent"
    except OSError:
        return "wrong"
    try:
        given = json.loads(body).get("proof")
    except (ValueError, AttributeError):
        return "wrong"
    if not isinstance(given, str) or not PROOF_PATTERN.fullmatch(given):
        return "wrong"
    return "ok" if hmac.compare_digest(given.encode("ascii"), proof(nonce, port).encode("ascii")) else "wrong"


def listener_is_this_engine(port: int, wait: float = BOOT_WAIT_SECONDS) -> bool:
    """True when the program on 127.0.0.1:<port> proves it holds the key of this installation AND nothing else answers
    on [::1]:<port>: the browser resolves `hokma.localhost` to either loopback address, so an impostor on the IPv6 one
    would receive the page even though 127.0.0.1 is ours. The engine itself never listens on [::1], so a silent [::1]
    is what is expected; any answer there, right or wrong, is refused. A listener that is not there yet is waited for
    up to `wait` seconds; one that answers wrongly is refused at once."""
    deadline = time.monotonic() + wait
    while True:
        if _ask(port, IPV6_LOOPBACK) != "silent":
            return False
        verdict = _ask(port)
        if verdict != "silent":
            return verdict == "ok"
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)

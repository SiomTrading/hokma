"""The session token: one value per `hokma start`, shared by the supervisor and every engine it launches.

A supervised engine never generates its own token. The supervisor creates it once and writes it to the engine's
standard input (never an argument, never the environment, never a file), the engine adopts it here before the
application is imported, and a restarted engine receives the same value, so the open browser tab keeps working.
Without a supervisor (tests, a dev checkout) the token is generated on first use.

The owner confirmation (bug 1035). The token can be read from the browser profile by anything that runs as the same
user, so the calls only the owner may make (permission mode, Trash, restore points) also need `X-Hokma-Owner`: a
random 128 bit value the open tab makes in memory and registers ONCE with `register_owner`. The engine keeps only
its SHA-256 in memory. It is never in a file, an argument or the environment.

Who may register (task 3045). Holding the token is not enough: the registration also needs a one-time secret that
`hokma start` puts next to the token in the address it prints (`#t=<token>&o=<secret>`). The tab removes it from the
address, sends it once and drops it; it never reaches the browser storage or a file. The supervisor creates it, keeps
only its SHA-256 and hands that to each engine (standard input, third line) until a registration consumes it; the
engine compares in constant time and forgets it at the first use.

How the hash gets back to the supervisor. The engine tells it on standard error, which a child process of the engine
(an agent's command) can also write to, so the note is authenticated: every engine gets a fresh channel key from the
supervisor on standard input (fourth line, memory only) and signs the note with HMAC-SHA256. The supervisor takes
ONE valid note per engine and only while it holds no hash; any other line that carries the marker is dropped.

Remembered sessions (task 3062, see `hokma.core.resume`). A tab that comes back through the saved cookie makes a new
owner confirmation and `resume` registers its hash for the lineage of that cookie, replacing the previous one of the
same lineage. `owner_matches` accepts the first registration (as before) and the current confirmation of every live
lineage; revoking a lineage forgets its confirmation. These hashes live in memory only. A restarted engine does not
know them: the tab calls `resume` again after it reconnects and the confirmation is registered again.
"""
import hashlib
import hmac
import re
import secrets
import sys
import threading

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,512}$")
OWNER_NONCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
OWNER_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
OWNER_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
CHANNEL_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
OWNER_MARKER = "HOKMA_OWNER_HASH "

REGISTERED, TAKEN, SECRET_NEEDED = "registered", "taken", "secret_needed"

_lock = threading.Lock()
_token: str | None = None
_supervised = False
_owner_hash: str | None = None
_owner_secret_hash: str | None = None     # SHA-256 of the one-time secret; None once consumed (or never given)
_channel_key: str | None = None           # signs the note to the supervisor; memory only
_revoked_lineages: set[str] = set()       # a late registration for one of these is refused (revoke and register can race)
_lineage_owners: dict[str, str] = {}      # lineage -> SHA-256 of the owner confirmation the last `resume` registered


def new_token() -> str:
    return secrets.token_urlsafe(24)


def new_owner_secret() -> str:
    return secrets.token_urlsafe(16)


def new_channel_key() -> str:
    return secrets.token_hex(32)


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii", "replace")).hexdigest()


def owner_note(digest: str, key: str) -> str:
    """The line the engine writes for the supervisor: marker, owner hash, HMAC of the hash under the channel key."""
    mac = hmac.new(key.encode("ascii"), digest.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{OWNER_MARKER}{digest} {mac}"


def parse_owner_note(line: str, key: str) -> str | None:
    """The owner hash of a note signed with `key`, or None for anything else (wrong shape, wrong signature)."""
    start = line.find(OWNER_MARKER)
    if start < 0:
        return None
    parts = line[start + len(OWNER_MARKER):].split()
    if len(parts) != 2 or not OWNER_HASH_PATTERN.fullmatch(parts[0]) or not OWNER_HASH_PATTERN.fullmatch(parts[1]):
        return None
    expected = owner_note(parts[0], key).rsplit(" ", 1)[1]
    return parts[0] if hmac.compare_digest(expected, parts[1]) else None


def adopt(token: str, owner_hash: str = "", owner_secret_hash: str = "", channel_key: str = "") -> None:
    """Fix the token handed over by the supervisor. Must run before `hokma.rpc.ws` is imported.
    `owner_hash` is the hash of the owner confirmation a previous engine registered ("" when none yet);
    `owner_secret_hash` the hash of the one-time secret still waiting for its registration ("" when consumed);
    `channel_key` signs the note to the supervisor."""
    global _token, _supervised, _owner_hash, _owner_secret_hash, _channel_key
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("the session token has an invalid shape")
    if owner_hash and not OWNER_HASH_PATTERN.fullmatch(owner_hash):
        raise ValueError("the owner confirmation hash has an invalid shape")
    if owner_secret_hash and not OWNER_HASH_PATTERN.fullmatch(owner_secret_hash):
        raise ValueError("the owner secret hash has an invalid shape")
    if channel_key and not CHANNEL_KEY_PATTERN.fullmatch(channel_key):
        raise ValueError("the channel key has an invalid shape")
    with _lock:
        if _token is not None or "hokma.rpc.ws" in sys.modules:
            raise RuntimeError("the session token is already fixed")
        _token = token
        _supervised = True
        _owner_hash = owner_hash or None
        _owner_secret_hash = (owner_secret_hash or None) if not owner_hash else None
        _channel_key = channel_key or None


def token() -> str:
    global _token
    with _lock:
        if _token is None:
            _token = new_token()
        return _token


def handoff_secrets() -> tuple[str, str, str]:
    """What a new supervisor needs to serve the same session after an update: the token, the owner confirmation
    hash ("" when none yet) and the hash of the one-time secret still waiting ("" when consumed). Memory only:
    it travels on a standard input, never in a file, an argument or the environment."""
    current = token()
    with _lock:
        return current, _owner_hash or "", _owner_secret_hash or ""


def supervised() -> bool:
    """True when this process is an engine started by `hokma start` (its restart is automatic)."""
    return _supervised


def _hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("ascii", "replace")).hexdigest()


def register_owner_detail(nonce: str, once: str = "") -> tuple[str, bool]:
    """Same as `register_owner`, and whether this call is the one that recorded the first owner (the one-time secret
    was consumed now). A reload of the same tab is REGISTERED with `first` False."""
    global _owner_hash, _owner_secret_hash
    if not OWNER_NONCE_PATTERN.fullmatch(nonce or ""):
        return TAKEN, False
    digest = _hash(nonce)
    with _lock:
        if _owner_hash is not None:
            same = secrets.compare_digest(_owner_hash.encode("ascii"), digest.encode("ascii"))
            return (REGISTERED if same else TAKEN), False
        expected = _owner_secret_hash
        if expected is None or not OWNER_SECRET_PATTERN.fullmatch(once or "") or not secrets.compare_digest(
                expected.encode("ascii"), secret_hash(once).encode("ascii")):
            return SECRET_NEEDED, False
        _owner_hash, _owner_secret_hash = digest, None        # one use: the secret is gone with the registration
        if _supervised and _channel_key:                      # the supervisor keeps the hash for the next engine
            print(owner_note(digest, _channel_key), file=sys.stderr, flush=True)
        return REGISTERED, True


def register_owner(nonce: str, once: str = "") -> str:
    """Record the owner confirmation of the first tab.

    REGISTERED when it is recorded now (the one-time secret was right and is consumed) or the same one is presented
    again (a reload of that tab, no secret needed); TAKEN when another one is already recorded; SECRET_NEEDED when
    nothing is recorded yet and the one-time secret is missing, wrong or already used; the shape of `nonce` is
    checked by the caller (a wrong shape is TAKEN here, never recorded)."""
    return register_owner_detail(nonce, once)[0]


def register_lineage_owner(lineage: str, nonce: str) -> bool:
    """Record the owner confirmation of the tab that just came back through the cookie of `lineage`. It replaces the
    one this lineage had. False (nothing recorded) for a wrong shape."""
    if not lineage or not OWNER_NONCE_PATTERN.fullmatch(nonce or ""):
        return False
    with _lock:
        if lineage in _revoked_lineages:
            return False
        _lineage_owners[lineage] = _hash(nonce)
    return True


def forget_lineage_owner(lineage: str) -> None:
    """The lineage was revoked or closed: its confirmation stops being accepted."""
    with _lock:
        _revoked_lineages.add(lineage)
        _lineage_owners.pop(lineage, None)


def owner_matches(candidate: str | None) -> bool:
    """Constant time. False while nothing is registered, and for anything that is not a valid confirmation.
    The first registration and the current confirmation of every live lineage are accepted."""
    if not candidate or not OWNER_NONCE_PATTERN.fullmatch(candidate):
        return False
    digest = _hash(candidate).encode("ascii")
    with _lock:
        known = ([_owner_hash] if _owner_hash is not None else []) + list(_lineage_owners.values())
    ok = False
    for expected in known:                                     # no early exit: the time does not tell which matched
        ok |= secrets.compare_digest(expected.encode("ascii"), digest)
    return ok


def forget_owner_for_tests() -> None:
    global _owner_hash, _owner_secret_hash
    with _lock:
        _owner_hash = None
        _owner_secret_hash = None
        _lineage_owners.clear()
        _revoked_lineages.clear()


def arm_owner_secret_for_tests(secret: str) -> None:
    """What `hokma start` does at the start of a session: only the hash of the secret is kept."""
    global _owner_secret_hash
    with _lock:
        _owner_secret_hash = secret_hash(secret)

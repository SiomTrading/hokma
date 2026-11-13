"""DM pairing: code-based approval of new users on messaging platforms.

Unknown users receive a one-time pairing code that the bot owner approves, instead of
static user-ID allowlists. Security properties (OWASP + NIST SP 800-63-4): 8-char codes
from a 32-char unambiguous alphabet via ``secrets``, 1-hour expiry, max 3 pending per
platform, 1 request per user per 10 min, lockout after 5 failed approvals, chmod 0600
data files, codes never logged. Storage: the data directory given to ``PairingStore``.
"""

import contextlib
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Unambiguous alphabet -- excludes 0/O, 1/I to prevent confusion
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

CODE_TTL_SECONDS = 3600             # Codes expire after 1 hour
RATE_LIMIT_SECONDS = 600            # 1 request per user per 10 minutes
LOCKOUT_SECONDS = 3600              # Lockout duration after too many failures
MAX_PENDING_PER_PLATFORM = 3        # Max pending codes per platform
MAX_FAILED_ATTEMPTS = 5             # Failed approvals before lockout


def _normalize_user_id(platform: str, user_id: str) -> str:
    """Normalize platform-specific user IDs before persisting / comparing them."""
    return str(user_id or "").strip()


def _user_id_aliases(platform: str, user_id: str) -> set[str]:
    """All known equivalent user IDs for auth / allowlist matching."""
    raw_user_id = str(user_id or "").strip()
    return {raw_user_id} if raw_user_id else set()


def _user_ids_match(platform: str, left: str, right: str) -> bool:
    """True when two user IDs represent the same principal."""
    left_aliases = _user_id_aliases(platform, left)
    return bool(left_aliases and left_aliases & _user_id_aliases(platform, right))


def _matching_ids(platform: str, approved: dict, user_id: str) -> list:
    return [uid for uid in approved if _user_ids_match(platform, uid, user_id)]


def _load_json_file(path: Path) -> dict:
    """Read a JSON object; {} when missing, malformed, unreadable, or not a dict.

    PermissionError is logged loudly: a 0600 file owned by another uid would otherwise
    silently leave the user unauthorized.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except PermissionError as e:
        logger.warning("Pairing file %s exists but is not readable: %s", path, e)
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json_file(path: Path, data: dict) -> None:
    _secure_write(path, json.dumps(data, indent=2, ensure_ascii=False))



def _secure_write(path: Path, data: str) -> None:
    """Write 0600 via temp file + atomic rename so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        with contextlib.suppress(OSError):  # Windows doesn't support chmod the same way
            os.chmod(path, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _is_hashed_entry(entry) -> bool:
    return isinstance(entry, dict) and "salt" in entry and "hash" in entry


def _entry_created_at(info):
    """Numeric ``created_at`` of a pending entry, or None for malformed/legacy entries."""
    created_at = info.get("created_at") if isinstance(info, dict) else None
    return created_at if isinstance(created_at, (int, float)) else None



class PairingStore:
    """Pairing codes and approved user lists.

    Files per platform: ``{platform}-pending.json``, ``{platform}-approved.json``, plus
    shared ``_rate_limits.json``, all inside the injected data directory.
    """

    def __init__(self, data_dir):
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()  # adapters run concurrently in threads sharing one store

    def _pending_path(self, platform: str) -> Path:
        return self._dir / f"{platform}-pending.json"

    def _approved_path(self, platform: str) -> Path:
        return self._dir / f"{platform}-approved.json"

    def _rate_limit_path(self) -> Path:
        return self._dir / "_rate_limits.json"

    def _cleanup_expired(self, platform: str) -> None:
        """Remove expired pending codes; malformed/legacy entries (no numeric ``created_at``) count as expired."""
        path = self._pending_path(platform)
        pending = self._load_json(path)
        now = time.time()
        live = {
            k: v for k, v in pending.items()
            if (created := _entry_created_at(v)) is not None and (now - created) <= CODE_TTL_SECONDS
        }
        if len(live) != len(pending):
            self._save_json(path, live)

    _load_json = staticmethod(_load_json_file)
    _save_json = staticmethod(_save_json_file)

    def _platforms(self, platform: Optional[str], suffix: str) -> list:
        return [platform] if platform else self._all_platforms(suffix)

    # ----- Approved users -----

    def list_approved(self, platform: str = None) -> list:
        """List approved users, optionally filtered by platform."""
        return [
            {"platform": p, "user_id": uid, **info}
            for p in self._platforms(platform, "approved")
            for uid, info in self._load_json(self._approved_path(p)).items()
        ]

    def _approve_user(self, platform: str, user_id: str, user_name: str = "") -> None:
        """Add a user to the approved list. Must be called under self._lock."""
        approved = self._load_json(self._approved_path(platform))
        normalized_user_id = _normalize_user_id(platform, user_id)
        for approved_user_id in _matching_ids(platform, approved, normalized_user_id):
            del approved[approved_user_id]
        approved[normalized_user_id] = {"user_name": user_name, "approved_at": time.time()}
        self._save_json(self._approved_path(platform), approved)

    def revoke(self, platform: str, user_id: str) -> bool:
        """Remove a user from the approved list. Returns True if found."""
        path = self._approved_path(platform)
        with self._lock:
            approved = self._load_json(path)
            matching_ids = _matching_ids(platform, approved, user_id)
            if not matching_ids:
                return False
            for approved_user_id in matching_ids:
                del approved[approved_user_id]
            self._save_json(path, approved)
            return True

    # ----- Pending codes -----

    @staticmethod
    def _hash_code(code: str, salt: bytes) -> str:
        return hashlib.sha256(salt + code.encode("utf-8")).hexdigest()

    def _finish_approval(self, platform: str, pending: dict, matched_key: str, matched_entry: dict) -> dict:
        """Remove a pending request and approve its user. Must hold self._lock."""
        del pending[matched_key]
        self._save_json(self._pending_path(platform), pending)
        # A successful approval proves legitimacy, so the persisted brute-force streak
        # must not carry over (isolated typos would accumulate into a spurious lockout).
        self._reset_failed_attempts(platform)
        result = {"user_id": matched_entry["user_id"], "user_name": matched_entry.get("user_name", "")}
        self._approve_user(platform, result["user_id"], result["user_name"])
        return result

    def generate_code(self, platform: str, user_id: str, user_name: str = "") -> Optional[str]:
        """Generate a pairing code for a new user.

        Returns None if the user is rate-limited, the platform hit MAX_PENDING_PER_PLATFORM,
        or the platform is locked out. Only a salted SHA-256 hash of the code is persisted.
        """
        with self._lock:
            self._cleanup_expired(platform)
            normalized_user_id = _normalize_user_id(platform, user_id)
            if self._is_locked_out(platform) or self._is_rate_limited(platform, user_id):
                return None
            pending = self._load_json(self._pending_path(platform))
            if len(pending) >= MAX_PENDING_PER_PLATFORM:
                return None
            code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
            salt = os.urandom(16)
            # Keyed by a random entry id, not the code itself.
            pending[secrets.token_hex(8)] = {
                "hash": self._hash_code(code, salt), "salt": salt.hex(),
                "user_id": normalized_user_id, "user_name": user_name, "created_at": time.time(),
            }
            self._save_json(self._pending_path(platform), pending)
            self._record_rate_limit(platform, user_id)
            return code

    def approve_code(self, platform: str, code: str) -> Optional[dict]:
        """Approve a pairing code and add its user to the approved list.

        Returns ``{user_id, user_name}``, or ``None`` if the code is invalid/expired OR the
        platform is locked out (disambiguate with ``_is_locked_out``). Constant-time
        salted-hash compare; legacy plaintext entries are ignored and pruned at TTL.
        """
        with self._lock:
            self._cleanup_expired(platform)
            code = code.upper().strip()
            # Before the lookup, or an already-issued valid code would bypass lockout.
            if self._is_locked_out(platform):
                return None
            pending = self._load_json(self._pending_path(platform))
            # Skip legacy/malformed entries so an in-place upgrade doesn't crash.
            for entry_id, entry in pending.items():
                if not _is_hashed_entry(entry):
                    continue
                try:
                    salt = bytes.fromhex(entry["salt"])
                except ValueError:
                    continue
                if secrets.compare_digest(self._hash_code(code, salt), entry["hash"]):
                    return self._finish_approval(platform, pending, entry_id, entry)
            self._record_failed_attempt(platform)
            return None

    def clear_pending(self, platform: str = None) -> int:
        """Clear all pending requests. Returns count removed."""
        with self._lock:
            count = 0
            for p in self._platforms(platform, "pending"):
                count += len(self._load_json(self._pending_path(p)))
                self._save_json(self._pending_path(p), {})
        return count

    # ----- Rate limiting and lockout -----

    def _limits(self) -> dict:
        return self._load_json(self._rate_limit_path())

    def _save_limits(self, limits: dict) -> None:
        self._save_json(self._rate_limit_path(), limits)

    def _is_rate_limited(self, platform: str, user_id: str) -> bool:
        """Whether a user (under any alias) has requested a code too recently."""
        limits = self._limits()
        return any(
            (time.time() - limits.get(f"{platform}:{alias}", 0)) < RATE_LIMIT_SECONDS
            for alias in _user_id_aliases(platform, user_id)
        )

    def _record_rate_limit(self, platform: str, user_id: str) -> None:
        limits = self._limits()
        now = time.time()
        for alias in _user_id_aliases(platform, user_id):
            limits[f"{platform}:{alias}"] = now
        self._save_limits(limits)

    def _is_locked_out(self, platform: str) -> bool:
        return time.time() < self._limits().get(f"_lockout:{platform}", 0)

    def _record_failed_attempt(self, platform: str) -> None:
        """Record a failed approval attempt; triggers lockout after MAX_FAILED_ATTEMPTS."""
        limits = self._limits()
        fail_key = f"_failures:{platform}"
        fails = limits.get(fail_key, 0) + 1
        limits[fail_key] = fails
        if fails >= MAX_FAILED_ATTEMPTS:
            limits[f"_lockout:{platform}"] = time.time() + LOCKOUT_SECONDS
            limits[fail_key] = 0
            print(f"[pairing] Platform {platform} locked out for {LOCKOUT_SECONDS}s "
                  f"after {MAX_FAILED_ATTEMPTS} failed attempts", flush=True)
        self._save_limits(limits)

    def _reset_failed_attempts(self, platform: str) -> None:
        """Clear the failed-approval counter after a success (it tracks *consecutive* failures)."""
        limits = self._limits()
        fail_key = f"_failures:{platform}"
        if limits.get(fail_key):
            limits[fail_key] = 0
            self._save_limits(limits)

    def _all_platforms(self, suffix: str) -> list:
        """Platforms that have a ``-<suffix>.json`` data file (``_``-prefixed files are shared state)."""
        tail = f"-{suffix}.json"
        platforms = (f.name.replace(tail, "") for f in self._dir.iterdir() if f.name.endswith(tail))
        return [p for p in platforms if not p.startswith("_")]

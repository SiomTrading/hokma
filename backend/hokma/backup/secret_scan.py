"""A small scan for secrets before anything is committed to the fund repository.

Only what changed since the last commit is looked at, text files up to 2 MB. The scan reports the file and the
line, never the value it found. A file that matches is kept out of the commit (it stays on disk, untouched).
"""
from __future__ import annotations

import re
from pathlib import Path

MAX_SCAN_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 95 * 1024 * 1024           # GitHub refuses files above 100 MB: stay clear of the limit

PATTERNS = tuple(re.compile(p) for p in (
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"\bglpat-[A-Za-z0-9_-]{20,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bAIza[0-9A-Za-z_-]{35}",
    r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}",
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    r"\b[sr]k_live_[A-Za-z0-9]{16,}",
))
_ENV_NAME = re.compile(r"^\.env(\..+)?$", re.I)
_ENV_TEMPLATE = (".example", ".sample", ".template", ".dist")
_ENV_VALUE = re.compile(r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*(?![\"']?\s*$)\S", re.M)


def _is_env_file(name: str) -> bool:
    return bool(_ENV_NAME.match(name)) and not name.lower().endswith(_ENV_TEMPLATE)


def scan_text(text: str, name: str = "") -> int | None:
    """Line number (1-based) of the first secret in `text`, or None. `name` is the file name (for `.env` files)."""
    if _is_env_file(name):
        match = _ENV_VALUE.search(text)
        if match:
            return text.count("\n", 0, match.start()) + 1
    for pattern in PATTERNS:
        match = pattern.search(text)
        if match:
            return text.count("\n", 0, match.start()) + 1
    return None


def scan_file(path: Path) -> int | None:
    """Line of the first secret in the file, or None (also None for binary, oversized or unreadable files)."""
    try:
        if path.stat().st_size > MAX_SCAN_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return scan_text(data.decode("utf-8", errors="replace"), path.name)

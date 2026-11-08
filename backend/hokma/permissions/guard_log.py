"""Reads `<data>/guard/blocks.jsonl` (written by the hook) into `guard_events` and returns the events for the chat.

Each line: `{ts, rule, tool, target, conversation_id}` (+ `task_id` for scheduled runs). The reader keeps its position
in `<data>/guard/blocks.pos`, so a line is stored once even after a restart. Lines are data written by Hokma's hook;
a damaged line is skipped, never trusted.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from hokma.core.config import hokma_data_dir
from hokma.db.repos.governance import HARD_RULES

_lock = threading.Lock()
MAX_LINE = 4000


def log_path() -> Path:
    return hokma_data_dir() / "guard" / "blocks.jsonl"


def _pos_path() -> Path:
    return hokma_data_dir() / "guard" / "blocks.pos"


def _read_pos(size: int) -> int:
    try:
        pos = int(_pos_path().read_text(encoding="ascii").strip() or 0)
    except (OSError, ValueError):
        return 0
    return pos if 0 <= pos <= size else 0


def _write_pos(pos: int) -> None:
    try:
        _pos_path().write_text(str(pos), encoding="ascii")
    except OSError:
        pass


def _parse(raw: bytes) -> dict[str, Any] | None:
    if len(raw) > MAX_LINE:
        return None
    try:
        row = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(row, dict) or row.get("rule") not in HARD_RULES:
        return None
    return row


def drain(db: Any) -> list[dict[str, Any]]:
    """New block lines since the last call: stored in `guard_events` and returned as `guard_block` event bodies."""
    path = log_path()
    with _lock:
        try:
            size = path.stat().st_size
        except OSError:
            return []
        pos = _read_pos(size)
        if pos >= size:
            return []
        try:
            with path.open("rb") as f:
                f.seek(pos)
                chunk = f.read(size - pos)
        except OSError:
            return []
        end = chunk.rfind(b"\n")
        if end < 0:
            return []                                       # a line still being written
        events: list[dict[str, Any]] = []
        for raw in chunk[:end].split(b"\n"):
            row = _parse(raw)
            if row is None:
                continue
            task = row.get("task_id")
            task_id = int(task) if isinstance(task, (str, int)) and str(task).isdigit() else None
            target = str(row.get("target") or "")
            try:
                with db.write() as u:
                    stored = u.guard_events.record_block(row["rule"], str(row.get("tool") or "") or None, target,
                                                         task_id=task_id, at=row.get("ts"))
            except Exception:
                continue                                    # the card is still shown; the row is a record
            events.append({"type": "guard_block", "id": stored.id, "rule": row["rule"],
                           "tool": str(row.get("tool") or ""), "target": target,
                           "conversation_id": str(row.get("conversation_id") or "") or None,
                           "at": stored.created_at})
        _write_pos(pos + end + 1)
        return events


def record_session_revoked(db: Any, reason: str) -> None:
    """A copy of the remembered session was used: the lineage was revoked (guard 'session', decision 'blocked').
    Never raises: the row is a record, the revocation already happened."""
    try:
        with db.write() as u:
            u.guard_events.record("session", "blocked", reason, tool="resume")
    except Exception:
        pass


def record_session_unsigned(db: Any, reason: str) -> None:
    """The cookie of the remembered session came without a valid signature (guard 'session', decision 'blocked').
    Nothing was rotated or revoked. Never raises: the row is a record."""
    try:
        with db.write() as u:
            u.guard_events.record("session", "blocked", reason, tool="resume")
    except Exception:
        pass

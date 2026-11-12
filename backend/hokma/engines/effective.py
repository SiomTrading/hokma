"""Which engine a conversation really runs on, for the chat and for the screen (bug 1095, 0.0.30).

A student with Claude connected had every message sent to Gemini: the saved default (or the connection attached to the
conversation) was a Gemini row, and Gemini was not installed. The chat followed the row, the screen showed Claude, and
the message came back as "the gemini command is not installed".

One rule for both sides: the saved connection is used when it can run. When it cannot (its program is not installed, or it
was removed) and another way in is connected, that one is used for the turn and the chat says so. Nothing is written:
the student's choice stays as it was, so it works again the moment the program is back.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any, Callable

from hokma.engines.registry import CLIS


@dataclass
class Effective:
    conn: dict[str, Any] | None              # None = Claude Code without a saved connection
    fallback_from: dict[str, Any] | None = None   # the saved connection that could not run, when one was replaced


def installed(kind: str) -> bool:
    from hokma.engine.process import find_cli

    binary = CLIS.get(kind)
    return bool(binary and find_cli(binary))


def claude_connected() -> bool:
    """Claude Code is installed and not known to be signed out (the state Hokma saved from `claude auth status`;
    "unknown" counts as usable, the turn itself reports a real sign-in problem)."""
    if not installed("claude_code"):
        return False
    try:
        from hokma.db import get_database

        with get_database().read() as u:
            profile = u.profile.get()
            return profile is None or profile.claude_status not in ("no_session", "no_cli")
    except Exception:                                   # noqa: BLE001  (no database yet: the program is there, so try it)
        return True


def can_run(conn: dict[str, Any] | None, *, is_installed: Callable[[str], bool], claude_ok: bool) -> bool:
    """The program is there (or the key and URL, for an API): the engine can be tried."""
    if conn is None:
        return claude_ok
    if conn.get("status") == "removed":
        return False
    if conn.get("engine") == "api":
        return True                                     # a key and a URL, nothing to install
    return is_installed(str(conn.get("engine")))


def verified(conn: dict[str, Any] | None, *, is_installed: Callable[[str], bool], claude_ok: bool) -> bool:
    """Can run AND was proved to work (status connected). Claude Code without a saved row counts when it is installed and
    not known to be signed out. One rule for the engine that answers and for the model button (bug 1104)."""
    if not can_run(conn, is_installed=is_installed, claude_ok=claude_ok):
        return False
    return conn is None or conn.get("status") == "connected"


def _last_choice(conn: dict[str, Any] | None) -> str:
    return str((conn or {}).get("chosen_at") or "")


def resolve(conn: dict[str, Any] | None, others: list[dict[str, Any]], *, is_installed: Callable[[str], bool] | None = None,
            claude_ok: bool | None = None) -> Effective:
    """`conn` when it is verified. Else the verified engine the student chose last, Claude Code being one more of them
    (it goes first only when the student never chose another verified engine). Else `conn` itself when it at least can run
    (nothing better exists, the turn tests it). Else `conn` (stuck)."""
    is_installed = is_installed or installed
    claude_ok = claude_connected() if claude_ok is None else claude_ok
    if conn is not None and conn.get("status") == "removed":
        return Effective(conn)                          # the chat has its own clear message for this one
    if verified(conn, is_installed=is_installed, claude_ok=claude_ok):
        return Effective(conn)
    if conn is not None and conn.get("engine") == "claude_code" and claude_ok:
        return Effective(conn)                          # a Claude row not tested yet is still Claude
    stand_in = conn or {"label": "Claude", "engine": "claude_code"}
    candidates: list[dict[str, Any] | None] = [
        other for other in others
        if (conn is None or other.get("id") != conn.get("id")) and verified(other, is_installed=is_installed, claude_ok=claude_ok)]
    has_claude_row = any(o.get("engine") == "claude_code" and o.get("auth_kind") == "subscription" for o in others)
    if claude_ok and conn is not None and not has_claude_row:
        candidates.append(None)                         # Claude Code without a saved row, never chosen by name
    # latest choice first; among equals Claude goes first
    candidates.sort(key=lambda c: (_last_choice(c), c is None or c.get("engine") == "claude_code"), reverse=True)
    if candidates:
        return Effective(candidates[0], fallback_from=stand_in)
    return Effective(conn)


def is_stuck(conn: dict[str, Any] | None, others: list[dict[str, Any]]) -> bool:
    """True when nothing can answer: the chat then asks to connect an engine."""
    chosen = resolve(conn, others)
    shown = chosen.conn
    if shown is not None and shown.get("status") == "removed":
        return False
    return not can_run(shown, is_installed=installed, claude_ok=claude_connected())


def label_of(conn: dict[str, Any] | None) -> str:
    return "Claude" if conn is None else str(conn.get("label") or conn.get("engine") or "")

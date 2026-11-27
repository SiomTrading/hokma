"""What a strategy creation costs (1.3.0, task 3433): seconds, calls and tokens of each creation, kept in `creation_metrics` and shown to the owner at the end of it.

The baseline is the creation of 1.2.8 (Ian, then Sam, then Ben) measured by the owner on his PC: Sam 15 min 03 s, 75 calls and 254,880 tokens; one agent in the lab
4 min 35 s, 10 calls and 89,934 tokens. The migration 0040 saves it as `baseline-1.2.8`. A creation of 1.3.0 is measured with the same strategy and the same request.

Nothing here is a guess about the model: the seconds come from the clock, the calls from the tool server (every call of a tool of Hokma that the creator makes
is counted as it happens) and the tokens from what the engine reported and Hokma saved in `messages`. The turns of the model are the tool calls plus the closing
answer: an upper bound when the model calls tools in parallel, and it is saved as such. The tokens of the turn that is still running are not saved yet: `creation_report`
reads them again later and updates the row.
"""
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

from hokma.db import Database, get_database
from hokma.db.timeutil import FORMAT

BASELINE = "baseline-1.2.8"
IDLE_SECONDS = 30 * 60                                  # a window with no call for this long was another piece of work: it starts again
_LOCK = threading.Lock()
_LEDGER: dict[int, dict[str, Any]] = {}                 # conversation (0: none) -> what the creator did since its first call of this work


def _key(conversation_id: int | None) -> int:
    return int(conversation_id or 0)


def note_call(conversation_id: int | None, tool: str, agent: str | None = None, *, now: float | None = None) -> None:
    """One call of a tool of Hokma. Never raises: measuring must not break a tool."""
    moment = time.time() if now is None else now
    with _LOCK:
        entry = _LEDGER.get(_key(conversation_id))
        if entry is None or moment - entry["last"] > IDLE_SECONDS:
            entry = {"since": moment, "last": moment, "tool_calls": 0, "dry_runs": 0, "agent": agent}
            _LEDGER[_key(conversation_id)] = entry
        entry["last"] = moment
        entry["tool_calls"] += 1
        if tool == "strategy_dry_run":
            entry["dry_runs"] += 1
        if agent:
            entry["agent"] = agent


def window(conversation_id: int | None) -> dict[str, Any] | None:
    with _LOCK:
        entry = _LEDGER.get(_key(conversation_id))
        return None if entry is None else dict(entry)


def reset(conversation_id: int | None = None) -> None:
    with _LOCK:
        if conversation_id is None:
            _LEDGER.clear()
        else:
            _LEDGER.pop(_key(conversation_id), None)


def _stamp(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).strftime(FORMAT)


def _tokens(session: Any, conversation_id: int | None, since: str) -> tuple[int, int]:
    if not conversation_id:
        return 0, 0
    row = session.one("SELECT COALESCE(SUM(tokens_in), 0) AS i, COALESCE(SUM(tokens_out), 0) AS o FROM messages WHERE conversation_id = :c AND created_at >= :s",
                      {"c": int(conversation_id), "s": since})
    return int(row["i"]), int(row["o"])


def finish(*, conversation_id: int | None, bank_id: int | None, str_code: str | None, agent: str | None, hokma_version: str, db: Database | None = None,
           now: float | None = None) -> dict[str, Any]:
    """Close the window of a creation: one row of `creation_metrics` and the numbers to show. The call that closes it was counted when it came in."""
    end = time.time() if now is None else now
    with _LOCK:
        entry = _LEDGER.pop(_key(conversation_id), None) or {"since": end, "tool_calls": 0, "dry_runs": 0, "agent": agent}
    since = _stamp(entry["since"])
    database = db or get_database()
    with database.write() as u:
        s = u.session
        tin, tout = _tokens(s, conversation_id, since)
        calls = int(entry["tool_calls"])
        row_id = s.insert("creation_metrics", {
            "label": f"creation-{hokma_version}", "kind": "creation", "agent": entry.get("agent") or agent, "hokma_version": hokma_version, "conversation_id": conversation_id or None,
            "bank_id": bank_id, "str_code": str_code, "started_at": since, "finished_at": _stamp(end), "seconds": max(0, int(round(end - entry["since"]))),
            "llm_calls": calls + 1, "tool_calls": calls, "dry_runs": int(entry["dry_runs"]), "tokens_in": tin, "tokens_out": tout, "tokens_total": tin + tout,
            "note": "llm_calls is tool_calls plus the closing answer; the tokens are the ones saved when the row was made, creation_report reads them again"})
    return report(row_id, db=database)


def baseline(db: Database | None = None) -> list[dict[str, Any]]:
    with (db or get_database()).read() as u:
        return u.session.query("SELECT * FROM creation_metrics WHERE label = :l ORDER BY id", {"l": BASELINE})


def _line(row: dict[str, Any]) -> str:
    seconds = int(row["seconds"])
    tokens = f"{int(row['tokens_total']):,}".replace(",", ".")
    return f"{seconds // 60} min {seconds % 60:02d} s, {int(row['llm_calls'])} calls, {tokens} tokens"


def report(row_id: int | None = None, *, conversation_id: int | None = None, db: Database | None = None) -> dict[str, Any]:
    """The measure of a creation against the baseline of 1.2.8. Without row_id, the last creation of the conversation (or the last one of all). The tokens are read again."""
    database = db or get_database()
    with database.write() as u:
        s = u.session
        if row_id is None:
            row = (s.one("SELECT * FROM creation_metrics WHERE kind = 'creation' AND conversation_id = :c ORDER BY id DESC LIMIT 1", {"c": int(conversation_id)})
                   if conversation_id else None) or s.one("SELECT * FROM creation_metrics WHERE kind = 'creation' ORDER BY id DESC LIMIT 1")
        else:
            row = s.one("SELECT * FROM creation_metrics WHERE id = :i", {"i": int(row_id)})
        if row is None:
            from hokma.sources.ingest import SourceError

            raise SourceError("no_data", "there is no creation measured yet: the measure is made when strategy_create finishes")
        if row["kind"] == "creation" and row["started_at"] and row["conversation_id"]:
            tin, tout = _tokens(s, int(row["conversation_id"]), str(row["started_at"]))
            if tin + tout > int(row["tokens_total"]):
                s.execute("UPDATE creation_metrics SET tokens_in = :i, tokens_out = :o, tokens_total = :t WHERE id = :r", {"i": tin, "o": tout, "t": tin + tout, "r": int(row["id"])})
                row = s.one("SELECT * FROM creation_metrics WHERE id = :i", {"i": int(row["id"])})
        base = s.query("SELECT * FROM creation_metrics WHERE label = :l ORDER BY id", {"l": BASELINE})
    ref = next((b for b in base if b["agent"] == "sam"), None)
    summary = f"Creation of {row['str_code'] or 'the strategy'}: {_line(row)} ({row['tool_calls']} calls to tools, {row['dry_runs']} dry runs)."
    if ref is not None and row["kind"] == "creation":
        summary += f" Baseline 1.2.8 (Sam): {_line(ref)}."
    return {"id": int(row["id"]), "label": row["label"], "kind": row["kind"], "str_code": row["str_code"], "seconds": int(row["seconds"]), "llm_calls": int(row["llm_calls"]),
            "tool_calls": int(row["tool_calls"]), "dry_runs": int(row["dry_runs"]), "tokens_in": int(row["tokens_in"]), "tokens_out": int(row["tokens_out"]),
            "tokens_total": int(row["tokens_total"]), "baseline": [{"agent": b["agent"], "seconds": int(b["seconds"]), "llm_calls": int(b["llm_calls"]), "tokens_total": int(b["tokens_total"])} for b in base],
            "summary": summary}

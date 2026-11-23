"""Typed state of a desk run (SPEC-017: TA-23, TA-24, TA-29).

The state holds the desk, the symbol, the line and the cut-off time, the reports of the analysts and the bounded
summaries of the debates. A debate round stores a short summary, never the concatenated history, so the state does not
grow with the number of rounds. A report that was not produced is declared absent, it is never left blank.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

from hokma.desks.core.signal import HORIZONS, LINES

MAX_SUMMARY_CHARS = 900
MAX_RESPONSE_CHARS = 600
ReportStatus = Literal["ok", "absent"]


@dataclass(frozen=True)
class Message:
    """One chat message. `tool_calls` is set on assistant messages that ask for tools; `tool_call_id` on tool results."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    id: str = ""
    tool_calls: tuple["ToolCall", ...] = ()
    tool_call_id: str = ""
    name: str = ""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RemoveMessage:
    """Update operation: drop the message with this id from the history."""

    id: str


class Report(TypedDict):
    source: str
    status: ReportStatus
    text: str


class DebateState(TypedDict):
    """A debate keeps a rolling summary and the last answer of each side. Never the full text of earlier rounds."""

    count: int
    summary: str
    last_speaker: str
    last_response: str
    verdict: str


class DeskState(TypedDict, total=False):
    messages: list[Message]
    desk_id: int
    symbol: str
    line: str
    horizon: str
    as_of: str
    instrument_context: str
    reports: dict[str, Report]
    tool_rounds: dict[str, int]
    debate: DebateState
    risk_debate: DebateState
    trader_plan: str
    final_decision: str
    owner_context: str


def clip(text: str, limit: int) -> str:
    """Cut a text to `limit` characters, on a word boundary when one is near, adding an ellipsis."""
    body = " ".join((text or "").split())
    if len(body) <= limit:
        return body
    cut = body[: max(limit - 1, 0)]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:.") + "\u2026"


def report_or_absent(text: str | None, source: str) -> str:
    """An analyst's report, or a marker that says it was never produced. An absence is not an empty finding."""
    body = (text or "").strip()
    if body:
        return body
    return f"(No {source} report in this run: it is not available, not an empty finding.)"


def make_report(source: str, text: str | None) -> Report:
    body = (text or "").strip()
    return {"source": source, "status": "ok" if body else "absent", "text": report_or_absent(body, source)}


def is_absent(report: Report | None) -> bool:
    return report is None or report.get("status") == "absent"


def opponent_or_opening(text: str | None, opponent: str) -> str:
    """What the other side said, or a note that it has not spoken so nobody invents a rebuttal."""
    body = (text or "").strip()
    if body:
        return body
    return f"(The {opponent} side has not spoken yet. Open with your own strongest evidence; do not invent their argument.)"


def new_debate() -> DebateState:
    return {"count": 0, "summary": "", "last_speaker": "", "last_response": "", "verdict": ""}


def record_round(debate: DebateState, speaker: str, response: str, summary: str | None = None) -> DebateState:
    """Store one round as a bounded summary. `summary` is the rolling summary the node produced; when absent, the new
    answer is folded in front of the older summary and the whole is cut to MAX_SUMMARY_CHARS."""
    if summary is None:
        head = f"{speaker}: {clip(response, 280)}"
        summary = clip(f"{head} | {debate['summary']}" if debate["summary"] else head, MAX_SUMMARY_CHARS)
    return {"count": debate["count"] + 1, "summary": clip(summary, MAX_SUMMARY_CHARS), "last_speaker": speaker,
            "last_response": clip(response, MAX_RESPONSE_CHARS), "verdict": debate["verdict"]}


def initial_state(*, desk_id: int, symbol: str, line: str, as_of: datetime, horizon: str, instrument_context: str = "",
                  owner_context: str = "", analysts: tuple[str, ...] = ()) -> DeskState:
    """State at the start of a run: desk, symbol, line and cut-off; every expected report starts as absent."""
    if line not in LINES:
        raise ValueError(f"line must be one of {LINES}")
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be one of {HORIZONS}")
    if as_of.tzinfo is None:
        raise ValueError("as_of must carry a timezone")
    if not symbol.strip():
        raise ValueError("symbol is required")
    stamp = as_of.astimezone(timezone.utc).isoformat()
    return {
        "messages": [Message("user", symbol.strip().upper(), id="m0")],
        "desk_id": desk_id, "symbol": symbol.strip().upper(), "line": line, "horizon": horizon, "as_of": stamp,
        "instrument_context": instrument_context or f"The instrument under analysis is {symbol.strip().upper()}.",
        "owner_context": owner_context,
        "reports": {a: make_report(a, None) for a in analysts},
        "tool_rounds": {a: 0 for a in analysts},
        "debate": new_debate(), "risk_debate": new_debate(),
        "trader_plan": "", "final_decision": "",
    }

"""Audit of tool calls (SPEC-017: OA-15).

Every call an agent makes to a tool is recorded with the agent, the tool, a summary of the input and the result. Keys,
tokens and account identifiers never reach the record. A second function walks the tools with their declared examples
and reports which ones run and which have problems, so the tool surface can be reviewed without running by hand.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:     # only for the type of the argument of run_tool_audit
    from hokma.desks.core.tools import Toolbox

INPUT_LIMIT = 240
RESULT_LIMIT = 160
_SENSITIVE = re.compile(r"key|token|secret|password|passwd|authorization|credential|account|cookie|bearer", re.I)


@dataclass(frozen=True)
class AuditRecord:
    at: str                  # UTC time of the call
    agent: str
    tool: str
    input_summary: str
    ok: bool
    error_kind: str          # "" when ok
    result_summary: str
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _scrub(value: Any, depth: int = 0) -> Any:
    if isinstance(value, Mapping):
        return {str(k): ("***" if _SENSITIVE.search(str(k)) else _scrub(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth + 1) for v in value[:20]] + (["..."] if len(value) > 20 else [])
    if isinstance(value, str):
        return _clip(value, 80)
    return value


def summarize_input(arguments: Mapping[str, Any] | None) -> str:
    """A short, safe text of the arguments: sensitive keys hidden, long values cut."""
    if not arguments:
        return "{}"
    return _clip(json.dumps(_scrub(dict(arguments)), sort_keys=True, ensure_ascii=False, default=str), INPUT_LIMIT)


def summarize_result(text: str, ok: bool, error_kind: str = "") -> str:
    head = _clip(text, RESULT_LIMIT)
    return f"error {error_kind}: {head}" if not ok else f"{len(text)} chars: {head}"


class ToolAuditLog:
    """Keeps the last `max_records` records in memory, calls `sink` for each one and, with `path`, appends them as JSON lines."""

    def __init__(self, *, sink: Callable[[AuditRecord], None] | None = None, path: Path | str | None = None, max_records: int = 1000) -> None:
        if max_records < 1:
            raise ValueError("max_records must be at least 1")
        self._sink, self._path, self._max = sink, Path(path) if path else None, max_records
        self._records: list[AuditRecord] = []
        self._lock = threading.Lock()

    def record(self, *, agent: str, tool: str, arguments: Mapping[str, Any] | None, ok: bool, result_text: str = "",
               error_kind: str = "", started: float | None = None) -> AuditRecord:
        elapsed = 0 if started is None else int((time.monotonic() - started) * 1000)
        item = AuditRecord(datetime.now(timezone.utc).isoformat(), agent, tool, summarize_input(arguments), ok, error_kind,
                           summarize_result(result_text, ok, error_kind), elapsed)
        with self._lock:
            self._records.append(item)
            del self._records[: max(0, len(self._records) - self._max)]
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        if self._sink is not None:
            self._sink(item)
        return item

    def records(self, *, agent: str | None = None, tool: str | None = None) -> list[AuditRecord]:
        with self._lock:
            return [r for r in self._records if (agent is None or r.agent == agent) and (tool is None or r.tool == tool)]


@dataclass(frozen=True)
class AuditRow:
    tool: str
    ran: bool
    issues: tuple[str, ...]
    error_kind: str = ""

    @property
    def clean(self) -> bool:
        return self.ran and not self.issues


def run_tool_audit(toolbox: "Toolbox", *, agent: str = "tool-audit") -> list[AuditRow]:
    """Call every tool that does not change anything with its declared example and judge it.

    Tools marked `mutates` are never called: they are listed as skipped. A tool without a runnable example, with a thin
    description, or that errors or returns nothing gets an issue per finding.
    """
    rows: list[AuditRow] = []
    for spec in toolbox.specs():
        issues: list[str] = []
        if len(spec.description.strip()) < 30:
            issues.append("description is too thin to tell a model when to use the tool")
        if spec.mutates:
            rows.append(AuditRow(spec.name, False, (*issues, "skipped: the tool changes state and is reviewed by reading only")))
            continue
        example = spec.resolve_example()
        if example is None:
            rows.append(AuditRow(spec.name, False, (*issues, "no runnable example declared")))
            continue
        result = toolbox.call(spec.name, example, agent=agent)
        if not result.ok:
            rows.append(AuditRow(spec.name, False, (*issues, f"example failed: {result.error_kind}: {result.message}"), result.error_kind))
            continue
        if result.data in (None, "", [], {}):
            issues.append("returns nothing for its own example")
        rows.append(AuditRow(spec.name, True, tuple(issues)))
    return rows

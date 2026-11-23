"""The report tree of a desk (SPEC-018: TA-83, TA-85). Each run of a symbol on a date has fixed folders, one per stage, under the folder of the
desk: analysts, debate, judge, trader, risk, management. `ensure_tree` creates them and changes nothing that exists. `save_reports` writes the
reports of a finished run into them. It is one function for the app, the chat and code: the same state gives the same files with the same
bytes, whichever way it was called (the caller is only logged, it never reaches the files)."""
from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from hokma.backoffice.reports import markdown
from hokma.desks.core import paths
from hokma.desks.core.safety import safe_ticker_component

STAGES = ("analysts", "debate", "judge", "trader", "risk", "management")
FOLDERS = {name: f"{i + 1}_{name}" for i, name in enumerate(STAGES)}
REPORTS_DIR = "reports"
COMPLETE = "complete_report.md"
CALLERS = ("app", "chat", "code")
HEADERS = {"analysts": "I. Analyst reports", "debate": "II. Debate", "judge": "III. Judge", "trader": "IV. Trader plan", "risk": "V. Risk review",
           "management": "VI. Management decision"}
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_ANALYSTS = 40


class TreeError(ValueError):
    """The tree cannot be made for these names."""


def _day(value: Any) -> str:
    out = value.isoformat()[:10] if isinstance(value, (date, datetime)) else str(value)
    if not _DAY.match(out):
        raise TreeError("a date is YYYY-MM-DD")
    date.fromisoformat(out)
    return out


def run_dir(desk_id: int, symbol: str, day: Any) -> Path:
    """`<desk>/reports/<SYMBOL>/<date>`: built only from the desk path helpers and checked parts."""
    return paths.desk_dir(desk_id) / REPORTS_DIR / safe_ticker_component(symbol).upper() / _day(day)


def ensure_tree(desk_id: int, symbol: str, day: Any) -> dict[str, Path]:
    """The folder of each stage, created when missing. Running it again changes nothing."""
    base = run_dir(desk_id, symbol, day)
    return {stage: paths.ensure_dir(base / FOLDERS[stage]) for stage in STAGES}


def _write_same(path: Path, body: str) -> None:
    """Write a file atomically; the same text leaves the same bytes (and an existing identical file is not touched)."""
    data = body.encode("utf-8")
    try:
        if path.read_bytes() == data:
            return
    except OSError:
        pass
    handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=".report.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _file_name(key: str) -> str:
    return safe_ticker_component(key).lower() + ".md"


def _stage_texts(state: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """The text of each stage of a desk state (the shape of `hokma.desks.core.state`): reports of the analysts, the two debates, the plan and
    the final decision. A stage with nothing is an empty mapping."""
    reports = state.get("reports") or {}
    if len(reports) > MAX_ANALYSTS:
        raise TreeError(f"at most {MAX_ANALYSTS} analyst reports are saved")
    debate, risk = state.get("debate") or {}, state.get("risk_debate") or {}
    out: dict[str, dict[str, str]] = {s: {} for s in STAGES}
    for key in sorted(reports):
        item = reports[key]
        out["analysts"][key] = str(item.get("text") if isinstance(item, Mapping) else item or "")
    if debate.get("summary") or debate.get("last_response"):
        out["debate"]["debate"] = "\n\n".join(x for x in (str(debate.get("summary") or ""), str(debate.get("last_response") or "")) if x)
    if debate.get("verdict"):
        out["judge"]["verdict"] = str(debate["verdict"])
    if state.get("trader_plan"):
        out["trader"]["plan"] = str(state["trader_plan"])
    if risk.get("summary") or risk.get("last_response") or risk.get("verdict"):
        out["risk"]["risk"] = "\n\n".join(x for x in (str(risk.get("summary") or ""), str(risk.get("last_response") or ""), str(risk.get("verdict") or "")) if x)
    if state.get("final_decision"):
        out["management"]["decision"] = str(state["final_decision"])
    return out


def save_reports(desk_id: int, symbol: str, day: Any, state: Mapping[str, Any], *, caller: str = "code") -> Path:
    """Write the reports of a finished run into its tree and the complete report that joins them. Returns the path of the complete report.
    `caller` (app, chat or code) is validated and otherwise ignored: the files are the same for all three."""
    if caller not in CALLERS:
        raise TreeError(f"caller is one of {', '.join(CALLERS)}")
    folders = ensure_tree(desk_id, symbol, day)
    texts = _stage_texts(state)
    sections = [markdown.render_record({"desk_id": desk_id, "symbol": safe_ticker_component(symbol).upper(), "date": _day(day)},
                                       (markdown.Field("desk_id", "Desk"), markdown.Field("symbol", "Symbol"), markdown.Field("date", "Date")),
                                       title="Trading analysis report")]
    for stage in STAGES:
        for key, body in texts[stage].items():
            _write_same(folders[stage] / _file_name(key), markdown.section(key, body))
        joined = "\n".join(markdown.section(k, texts[stage][k], level=3) for k in sorted(texts[stage])) if texts[stage] else markdown.ABSENT + "\n"
        sections.append(f"## {HEADERS[stage]}\n\n{joined}")
    target = run_dir(desk_id, symbol, day) / COMPLETE
    _write_same(target, "\n".join(sections))
    return target

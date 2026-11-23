"""Adam, the market analyst (SPEC-019: TA-01; skills CTS-004, CTS-005, CTS-020, CTS-028).

Adam reads closed daily and weekly bars. The weekly picture is the weekly structure calculation (it cuts the daily bars at the
cut-off before it groups them into weeks); every other figure is a result of the indicators or statistics modules or comes from the
verified snapshot. His answer has levels and scenarios with the level that proves each one wrong, and no field of an order, a size
or an exit. The price of the bar of a screen is a description of that bar and never a price to trade at.
"""
from __future__ import annotations

from typing import Any

from hokma.desks.agents.analyst_kit import AnalystAgent, EvidenceLog, OutputSpec, RunContext
from hokma.desks.plantel import AnalystReport

CODE = "A01"
KEY = "market"
WEEKLY_CALC = "indicators.screens.weekly_structure"
LEVEL_KINDS = ("support", "resistance", "moving_average", "pivot", "reference")
TRENDS = ("up", "down", "range", "unclear", "unknown")
MIN_SCENARIOS, MAX_SCENARIOS = 2, 4

_TEXT = {"type": "string", "maxLength": 600}
_NUM = {"type": ["number", "null"]}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "trend", "levels", "scenarios", "screens", "data_gaps"],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 1200},
        "trend": {"type": "object", "required": ["daily", "weekly"],
                  "properties": {"daily": {"type": "string", "enum": list(TRENDS)}, "weekly": {"type": "string", "enum": list(TRENDS)}}},
        "levels": {"type": "array", "items": {
            "type": "object", "required": ["kind", "name", "value", "source"],
            "properties": {"kind": {"type": "string", "enum": list(LEVEL_KINDS)}, "name": _TEXT, "value": {"type": "number"}, "source": _TEXT}}},
        "scenarios": {"type": "array", "items": {
            "type": "object", "required": ["name", "reading", "invalidation_level", "invalidation_text"],
            "properties": {"name": _TEXT, "reading": _TEXT, "invalidation_level": {"type": "number"}, "invalidation_text": _TEXT}}},
        "screens": {"type": "array", "items": {
            "type": "object", "required": ["name", "state"], "properties": {"name": _TEXT, "state": _TEXT}}},
        "data_gaps": {"type": "array", "items": _TEXT},
    },
}


def _read_both_timeframes(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    """A desk that works on daily and weekly bars must have read both: the weekly figures come from the weekly structure call."""
    problems: list[str] = []
    trend = value.get("trend") or {}
    if trend.get("weekly", "unknown") != "unknown" and WEEKLY_CALC not in evidence.quant_called():
        problems.append(f"the weekly trend is stated but {WEEKLY_CALC} was never called")
    read_daily = evidence.snapshot is not None or any(t in ("data_bars", "data_snapshot") for t in evidence.tools_called())
    if trend.get("daily", "unknown") != "unknown" and not read_daily:
        problems.append("the daily trend is stated but no daily bar or snapshot was read")
    return problems


def _scenario_count(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    count = len(value.get("scenarios") or [])
    if MIN_SCENARIOS <= count <= MAX_SCENARIOS or "scenarios" in (value.get("data_gaps") or []):
        return []
    return [f"scenarios: {MIN_SCENARIOS} to {MAX_SCENARIOS} are required (or declare 'scenarios' in data_gaps), got {count}"]


def _show(number: Any) -> str:
    return str(number)


def render(value: dict) -> AnalystReport:
    trend = value["trend"]
    findings = [f"Trend: daily {trend['daily']}, weekly {trend['weekly']}."]
    findings += [f"Level ({v['kind']}) {v['name']}: {_show(v['value'])} (source: {v['source']})." for v in value["levels"]]
    findings += [f"Scenario {s['name']}: {s['reading']} It is wrong if {s['invalidation_text']} at {_show(s['invalidation_level'])}."
                 for s in value["scenarios"]]
    findings += [f"Screen {s['name']}: {s['state']}." for s in value["screens"]]
    return AnalystReport(value["summary"], tuple(findings), tuple(value["data_gaps"]))


SPEC = OutputSpec(SCHEMA, checks=(_read_both_timeframes, _scenario_count))
AGENT = AnalystAgent(CODE, KEY, SPEC, render, snapshot_categories=("market",))

system_prompt = AGENT.system_prompt
toolbox = AGENT.toolbox
run = AGENT.run

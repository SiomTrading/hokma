"""Ezra, the fundamentals analyst (SPEC-019: TA-04, AFS-004, DTS-11; value chain OA-04 and the ratio and DCF skill SKL-013).

On a stock options desk Ezra reads the statements public at the cut-off around one operation. He asks the fundamentals module for every
ratio and every value; a discounted cash flow whose terminal growth is not below its discount rate is refused before the module is
asked. His thesis is written to be proven wrong: `ThesisBoard` keeps it, and every new fact is a dated row of `thesis_events` with
its effect on one pillar. His answer has no exit condition with a number: the risk committee fixes that. The trades of members of
congress are context with a declared delay, and without a provider of that data the criterion is `NO_DATA`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from hokma.db import Database, get_database
from hokma.db.timeutil import to_db_time, utcnow
from hokma.desks.agents.analyst_kit import AnalystAgent, AnalystResult, EvidenceLog, OutputSpec, RunContext
from hokma.desks.core.graph import ChatModel
from hokma.desks.core.tools import ToolFailure
from hokma.desks.plantel import AnalystReport
from hokma.lessons import thesis

CODE = "A06"
KEY = "fundamentals"
PILLAR_STATUS = ("on_track", "at_risk", "broken", "unknown")
PILLAR_TREND = ("improving", "flat", "deteriorating", "unknown")
EFFECTS = ("strengthens", "weakens", "neutral")
ACTIONS = ("keep_reading", "read_again", "hand_to_judge")
CONGRESS_LAG_DAYS = 45
DCF_METHODS = ("dcf", "gordon", "ddm_two_stage", "h_model")
MAX_PILLARS = 8

# ---- congress context (DTS-11) --------------------------------------------------------------------------------------

CONGRESS_NOTE = ("Trades of members of congress are published up to {days} days after the trade. They are context with a delay, "
                 "not an edge and not a reason to act.")


@dataclass(frozen=True)
class CongressTrade:
    member: str
    transaction_date: date
    disclosure_date: date
    kind: str
    lag_days: int
    beyond_declared_lag: bool

    def to_dict(self) -> dict[str, Any]:
        return {"member": self.member, "transaction_date": self.transaction_date.isoformat(), "disclosure_date": self.disclosure_date.isoformat(),
                "kind": self.kind, "lag_days": self.lag_days, "beyond_declared_lag": self.beyond_declared_lag}


@dataclass(frozen=True)
class CongressContext:
    criterion: str                                  # "NO_DATA" or "CONTEXT"
    trades: tuple[CongressTrade, ...] = ()
    note: str = ""
    max_lag_days: int = CONGRESS_LAG_DAYS

    def to_dict(self) -> dict[str, Any]:
        return {"criterion": self.criterion, "trades": [t.to_dict() for t in self.trades], "note": self.note, "max_lag_days": self.max_lag_days}


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def congress_context(result: Any, as_of: datetime) -> CongressContext:
    """The congress trades the data module returned, with the delay of each one, or `NO_DATA` when there is no provider (DTS-11).

    A disclosure published after the cut-off is not visible and is dropped. The delay is declared in the note and no edge is promised."""
    note = CONGRESS_NOTE.format(days=CONGRESS_LAG_DAYS)
    if result is None or not getattr(result, "ok", False):
        return CongressContext("NO_DATA", (), "No data provider for the trades of members of congress is connected. " + note)
    cut = as_of.astimezone(timezone.utc).date()
    trades: list[CongressTrade] = []
    for row in result.value or ():
        done, disclosed = _date(row.get("transaction_date")), _date(row.get("disclosure_date"))
        if done is None or disclosed is None or disclosed > cut or disclosed < done:
            continue
        lag = (disclosed - done).days
        trades.append(CongressTrade(str(row.get("member") or "unknown"), done, disclosed, str(row.get("type") or "unknown"), lag,
                                    lag > CONGRESS_LAG_DAYS))
    if not trades:
        return CongressContext("NO_DATA", (), "The provider returned no trade visible at the cut-off. " + note)
    return CongressContext("CONTEXT", tuple(trades), note)


# ---- the thesis log (AFS-004) ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Pillar:
    name: str
    expected: str


@dataclass(frozen=True)
class PillarRow:
    name: str
    expected: str
    status: str
    trend: str
    last_effect: str | None = None
    last_fact_date: str | None = None


class ThesisBoard:
    """The thesis of one symbol of one desk, kept in `thesis_events`.

    `open` writes the thesis (statement, pillars, risks, catalysts) as the first dated row. `log_datum` adds one dated row per new
    fact with the pillar it touches and its effect; the state of the thesis does not change with a datum, it changes only through
    `lessons.thesis.record`. The board has no exit condition with a number: that belongs to the risk committee."""

    def __init__(self, desk_id: int, symbol: str, *, db: Database | None = None) -> None:
        self.desk_id, self.symbol, self._db = desk_id, thesis.clean_symbol(symbol), db

    def _database(self) -> Database:
        return self._db or get_database()

    def open(self, statement: str, pillars: Sequence[Pillar], *, risks: Iterable[str] = (), catalysts: Iterable[Mapping[str, str]] = (),
             now: str | None = None) -> thesis.ThesisEvent:
        if not statement.strip() or not 1 <= len(pillars) <= MAX_PILLARS or len({p.name for p in pillars}) != len(pillars):
            raise thesis.ThesisError(f"a thesis has a statement and between 1 and {MAX_PILLARS} pillars with different names")
        note = json.dumps({"kind": "thesis", "statement": statement.strip(), "pillars": [{"name": p.name, "expected": p.expected} for p in pillars],
                           "risks": list(risks), "catalysts": [dict(c) for c in catalysts]}, ensure_ascii=False)
        return thesis.record(self.desk_id, self.symbol, "open", note, now=now, db=self._database())

    def log_datum(self, *, pillar: str, effect: str, source: str, reading: str, action: str, fact_date: str | date,
                  as_of: datetime | None = None, breaks: bool = False, now: str | None = None) -> int:
        """One new fact, dated, against one pillar. The state is read and the row written in the same transaction."""
        if effect not in EFFECTS or action not in ACTIONS:
            raise thesis.ThesisError(f"effect is one of {EFFECTS} and action one of {ACTIONS}")
        day = _date(fact_date)
        if day is None or not source.strip() or not reading.strip():
            raise thesis.ThesisError("a datum needs the day the fact became public, its source and a reading")
        if as_of is not None and day > as_of.astimezone(timezone.utc).date():
            raise thesis.ThesisError("a fact published after the cut-off cannot be logged")
        if breaks and effect != "weakens":
            raise thesis.ThesisError("only a fact that weakens a pillar can break it")
        with self._database().write() as u:
            last = u.session.one("SELECT id, state, note FROM thesis_events WHERE desk_id = :d AND symbol = :s ORDER BY id DESC LIMIT 1",
                                 {"d": self.desk_id, "s": self.symbol})
            if last is None or last["state"] not in ("open", "confirmed"):
                raise thesis.InvalidTransition(f"{self.symbol}: a datum needs an open or confirmed thesis")
            names = {p["name"] for p in self._thesis(u)["pillars"]}
            if pillar not in names:
                raise thesis.ThesisError(f"{pillar!r} is not a pillar of this thesis: {', '.join(sorted(names))}")
            note = json.dumps({"kind": "datum", "fact_date": day.isoformat(), "pillar": pillar, "effect": effect, "source": source.strip(),
                               "reading": reading.strip(), "action": action, "breaks": bool(breaks)}, ensure_ascii=False)
            if len(note) > thesis.MAX_NOTE:
                raise thesis.ThesisError(f"a datum has at most {thesis.MAX_NOTE} characters")
            return int(u.session.insert("thesis_events", {"desk_id": self.desk_id, "symbol": self.symbol, "state": last["state"], "note": note,
                                                          "created_at": to_db_time(now) if now else utcnow()}))

    def _rows(self, u: Any) -> list[dict]:
        return u.session.query("SELECT id, state, note, created_at FROM thesis_events WHERE desk_id = :d AND symbol = :s ORDER BY id",
                               {"d": self.desk_id, "s": self.symbol})

    def _thesis(self, u: Any) -> dict:
        found: dict | None = None
        for row in self._rows(u):
            note = _load(row["note"])
            if note.get("kind") == "thesis":
                found = note                                   # the latest thesis of the symbol (a closed one can be followed by a new one)
        if found is None:
            raise thesis.ThesisError(f"{self.symbol}: no thesis written")
        return found

    def entries(self) -> list[dict]:
        """Every dated row of the thesis: the thesis itself and each datum, oldest first."""
        with self._database().read() as u:
            return [{"id": r["id"], "state": r["state"], "created_at": r["created_at"], **_load(r["note"])} for r in self._rows(u)]

    def board(self) -> tuple[PillarRow, ...]:
        """One row per pillar: what was expected, where it stands and where it is going, from the dated data."""
        with self._database().read() as u:
            base = self._thesis(u)
            data = [n for n in (_load(r["note"]) for r in self._rows(u)) if n.get("kind") == "datum"]
        rows: list[PillarRow] = []
        for pillar in base["pillars"]:
            mine = [d for d in data if d["pillar"] == pillar["name"]]
            moving = [d for d in mine if d["effect"] != "neutral"]
            if not mine:
                rows.append(PillarRow(pillar["name"], pillar["expected"], "unknown", "unknown"))
                continue
            last = moving[-1] if moving else None
            status = "broken" if any(d["breaks"] for d in mine) else "unknown" if last is None else ("on_track" if last["effect"] == "strengthens" else "at_risk")
            trend = "flat" if last is None else ("improving" if last["effect"] == "strengthens" else "deteriorating")
            rows.append(PillarRow(pillar["name"], pillar["expected"], status, trend, mine[-1]["effect"], mine[-1]["fact_date"]))
        return tuple(rows)


def _load(note: Any) -> dict:
    try:
        value = json.loads(note) if note else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# ---- the guard of the discounted cash flow (SKL-013) ----------------------------------------------------------------

def dcf_problems(method: str, params: Mapping[str, Any]) -> list[str]:
    """Why the inputs of a valuation are not reasonable, from the numbers the model wants to use. Empty means they pass."""
    def number(key: str) -> float | None:
        value = params.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    pairs = {"dcf": ("perpetual_growth_rate", "wacc"), "gordon": ("growth_rate", "rate_of_return"),
             "ddm_two_stage": ("stable_growth_rate", "rate_of_return"), "h_model": ("stable_growth_rate", "rate_of_return")}
    if method not in pairs:
        return []
    growth, rate = (number(k) for k in pairs[method])
    if growth is None or rate is None:
        return []
    if growth >= rate:
        return [f"the terminal growth ({growth}) is at or above the discount rate ({rate}): the model has no meaning and is rejected"]
    return []


def dcf_guard(name: str, params: dict) -> dict:
    """Quant guard of Ezra: a valuation with unreasonable inputs is refused before the module is asked."""
    if name == "fundamentals.intrinsic":
        inner = params.get("params")
        if isinstance(inner, Mapping) and (problems := dcf_problems(str(params.get("method")), inner)):
            raise ToolFailure("bad_input", "; ".join(problems))
    return params


# ---- the report -----------------------------------------------------------------------------------------------------

_TEXT = {"type": "string", "maxLength": 600}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "thesis", "quarter", "congress", "data_gaps"],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 1200},
        "thesis": {"type": "object", "required": ["statement", "pillars", "risks", "catalysts", "counter_evidence"], "properties": {
            "statement": {"type": "string", "minLength": 1, "maxLength": 600},
            "pillars": {"type": "array", "items": {"type": "object", "required": ["name", "expected", "status", "trend"], "properties": {
                "name": _TEXT, "expected": _TEXT, "status": {"type": "string", "enum": list(PILLAR_STATUS)},
                "trend": {"type": "string", "enum": list(PILLAR_TREND)}}}},
            "risks": {"type": "array", "items": _TEXT},
            "catalysts": {"type": "array", "items": {"type": "object", "required": ["name", "date", "source"], "properties": {
                "name": _TEXT, "date": {"type": "string", "minLength": 8}, "source": {"type": "string", "minLength": 1}}}},
            "counter_evidence": {"type": "array", "items": _TEXT}}},
        "quarter": {"type": "object", "required": ["fiscal_quarter", "notes"],
                    "properties": {"fiscal_quarter": {"type": ["string", "null"]}, "notes": {"type": "array", "items": _TEXT}}},
        "congress": {"type": "object", "required": ["criterion", "note"],
                     "properties": {"criterion": {"type": "string", "enum": ["NO_DATA", "CONTEXT"]}, "note": _TEXT}},
        "data_gaps": {"type": "array", "items": _TEXT},
    },
}


def _thesis_can_fail(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    thesis_part = value["thesis"]
    problems: list[str] = []
    if not thesis_part["pillars"] or not thesis_part["risks"]:
        problems.append("thesis: a thesis needs pillars and risks")
    if not thesis_part["counter_evidence"]:
        problems.append("thesis: the evidence against the idea is written with the same care as the evidence for it")
    return problems


def _congress_honest(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    given = (ctx.facts or {}).get("congress") or CongressContext("NO_DATA").to_dict()
    stated = value["congress"]
    problems: list[str] = []
    if stated["criterion"] != given["criterion"]:
        problems.append(f"congress: the data module gives {given['criterion']}, the report says {stated['criterion']}")
    if str(CONGRESS_LAG_DAYS) not in stated["note"]:
        problems.append(f"congress: the note has to declare the delay of up to {CONGRESS_LAG_DAYS} days")
    return problems


def render(value: dict) -> AnalystReport:
    t = value["thesis"]
    findings = [f"Thesis: {t['statement']}"]
    findings += [f"Pillar {p['name']}: expected {p['expected']}; {p['status'].replace('_', ' ')}, {p['trend']}." for p in t["pillars"]]
    findings += [f"Risk: {r}" for r in t["risks"]]
    findings += [f"Catalyst {c['name']} on {c['date']} (source {c['source']})." for c in t["catalysts"]]
    findings += [f"Against: {c}" for c in t["counter_evidence"]]
    q = value["quarter"]
    findings.append(f"Quarter read: {q['fiscal_quarter'] or 'not identified'}. " + " ".join(q["notes"]))
    findings.append(f"Congress context {value['congress']['criterion']}: {value['congress']['note']}")
    return AnalystReport(value["summary"], tuple(findings), tuple(value["data_gaps"]))


SPEC = OutputSpec(SCHEMA, extra_forbidden=frozenset({"exit_condition", "exit_condition_value", "gatillo"}),
                  checks=(_thesis_can_fail, _congress_honest))
AGENT = AnalystAgent(CODE, KEY, SPEC, render, guard_factory=lambda ctx: dcf_guard)

system_prompt = AGENT.system_prompt
toolbox = AGENT.toolbox


def run(model: ChatModel, *, congress: Any = None, facts: Mapping[str, Any] | None = None, **kwargs: Any) -> AnalystResult:
    """One report of Ezra. `congress` is the answer of the data module for the trades of members of congress, or None when there
    is no provider (the criterion is then `NO_DATA`)."""
    as_of = kwargs["as_of"]
    merged = {**dict(facts or {}), "congress": congress_context(congress, as_of).to_dict()}
    return AGENT.run(model, facts=merged, **kwargs)

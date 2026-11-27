"""Mandate of a desk and the plan of its analysts (SPEC-019: AH-25, AH-26, TA-21, TA-28).

The mandate is the typed contract the owner edits for a desk: the cadence, the risk budget, the order of the analysts and
its strategies with their blend policy. It is strict: a field that does not exist fails at load time and says which one.
It names no instrument: the universe is an argument of each run (`Mandate.to_desk_spec`), so the same mandate serves any
stock, ETF, option underlying or future. The text of the owner is stored in `desk_mandates.spec_json`.

The analyst plan is a declarative table: a key that is not in it fails when the mandate loads, not when a run starts.

Below the mandate lives Grant (A15, SPEC-019 B12): the tools of his seat, the morning note, the final rating, the reflection on settled lessons,
the postmortem of the views and the two layer decision log. Every answer of his goes through the checks of the analyst kit.
Turning an analyst off takes it out of the graph and leaves its report `absent`, the way the core state declares it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hokma.backoffice import runs
from hokma.broker.types import Fill
from hokma.contracts.identity import NoContract, parse_canonical
from hokma.db import Database, get_database
from hokma.db.timeutil import utcnow
from hokma.desks.agents import analyst_kit as kit
from hokma.desks.core.graph import ChatModel, ModelReply
from hokma.desks.core.output import parse_structured
from hokma.desks.core.prompts import PromptComposer
from hokma.desks.core.signal import Horizon, Line
from hokma.desks.core.spec_loader import (
    BlendPolicy, DeskSpec, ModelSpec, SpecError, StrategySpec, _describe, _read,
)
from hokma.desks.core.state import Message, Report, make_report
from hokma.desks.core.tool_audit import ToolAuditLog
from hokma.desks.core.tools import ToolCatalog, ToolFailure, ToolSpec, Toolbox
from hokma.desks.plantel import SEATS, AnalystReport, ManagerReport, seats_for
from hokma.lessons import store as lesson_store
from hokma.lessons.settle import settle as settle_lesson
from hokma.lessons.store import Lesson, LessonError, NotExecuted, NotReady

MAX_TEXT = 500
MAX_ANALYSTS = 12
MAX_STRATEGIES = 10

# The analysts a desk can run: key of the plan -> seat that runs it. The key is what a mandate writes.
ANALYSTS: Mapping[str, str] = {"market": "A01", "sentiment": "A02", "news": "A04", "fundamentals": "A06", "options": "A07"}
# Names a mandate must never carry, in any nesting of a model's params: they name instruments.
INSTRUMENT_KEYS = frozenset({"universe", "symbol", "symbols", "ticker", "tickers", "asset", "assets", "underlying",
                             "underlyings", "instrument", "instruments", "watchlist"})


class AnalystPlanError(ValueError):
    """An analyst key that is not in the table, or a plan that leaves a desk with no analyst."""


def _find_instrument_keys(value: Any, path: str = "") -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            where = f"{path}.{key}" if path else str(key)
            if str(key).strip().lower() in INSTRUMENT_KEYS:
                return where
            if (found := _find_instrument_keys(item, where)) is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            if (found := _find_instrument_keys(item, f"{path}[{index}]")) is not None:
                return found
    return None


class AnalystEntry(BaseModel):
    """One analyst of the plan. The order of the entries is the order the analysts run in."""

    model_config = ConfigDict(extra="forbid")

    key: str
    enabled: bool = True

    @field_validator("key")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in ANALYSTS:
            raise ValueError(f"unknown analyst {value!r}; the table has: {', '.join(ANALYSTS)}")
        return value


class RiskBudget(BaseModel):
    """What the desk may risk. The committee tightens these inside the frame; it never widens them."""

    model_config = ConfigDict(extra="forbid")

    max_risk_per_trade_pct: float = Field(default=1.0, gt=0, le=10)
    max_open_positions: int = Field(default=5, ge=1, le=50)
    max_drawdown_pct: float = Field(default=10.0, gt=0, le=100)


class MandateStrategy(BaseModel):
    """A strategy of the mandate: its horizon, the sources of views it blends and how it blends them. No universe."""

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = Field(default=None, max_length=80)
    horizon: Horizon
    models: list[ModelSpec] = Field(min_length=1)
    blend: BlendPolicy = Field(default_factory=BlendPolicy)
    cadence: Literal["daily", "weekly", "monthly"] = "weekly"

    @model_validator(mode="before")
    @classmethod
    def _no_instruments(cls, data: Any) -> Any:
        if (where := _find_instrument_keys(data)) is not None:
            raise ValueError(f"{where}: a mandate names no instrument; the universe is an argument of each run")
        return data

    @field_validator("name")
    @classmethod
    def _slug(cls, value: str) -> str:
        # Same rule as a strategy of the core spec: lowercase words joined by hyphens.
        if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", value) or len(value) > 60:
            raise ValueError("must be lowercase words joined by hyphens, up to 60 characters")
        return value

    @field_validator("models")
    @classmethod
    def _unique(cls, value: list[ModelSpec]) -> list[ModelSpec]:
        names = [m.name for m in value]
        if dupes := sorted({n for n in names if names.count(n) > 1}):
            raise ValueError(f"duplicate model names: {dupes}")
        return value


class Mandate(BaseModel):
    """The typed mandate of one desk (AH-25). Extra keys are rejected; instruments are never named (AH-26)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    line: Line
    variant: str | None = None
    rebalance: Literal["daily", "weekly", "monthly"] = "weekly"
    asset_classes: list[Literal["stock", "etf", "option", "future"]] = Field(
        default_factory=lambda: ["stock", "etf", "option", "future"], min_length=1, max_length=4)
    risk: RiskBudget = Field(default_factory=RiskBudget)
    analysts: list[AnalystEntry] = Field(default_factory=list, max_length=MAX_ANALYSTS)
    strategies: list[MandateStrategy] = Field(min_length=1, max_length=MAX_STRATEGIES)
    notes: str | None = Field(default=None, max_length=MAX_TEXT)

    @model_validator(mode="before")
    @classmethod
    def _no_instruments(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            top = {k: v for k, v in data.items() if k != "strategies"}      # strategies check their own keys
            if (where := _find_instrument_keys(top)) is not None:
                raise ValueError(f"{where}: a mandate names no instrument; the universe is an argument of each run")
        return data

    @model_validator(mode="after")
    def _coherent(self) -> "Mandate":
        try:
            seats = seats_for(self.line, self.variant)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        codes = {s.code for s in seats}
        keys = [a.key for a in self.analysts]
        if dupes := sorted({k for k in keys if keys.count(k) > 1}):
            raise ValueError(f"analysts: duplicate keys {dupes}")
        for key in keys:
            if ANALYSTS[key] not in codes:
                raise ValueError(f"analysts: {key!r} is not a seat of a {self.line} desk")
        if self.analysts and not any(a.enabled for a in self.analysts):
            raise ValueError("analysts: at least one analyst must stay on")
        if len(set(self.asset_classes)) != len(self.asset_classes):
            raise ValueError("asset_classes: duplicate entries")
        names = [s.name for s in self.strategies]
        if dupes := sorted({n for n in names if names.count(n) > 1}):
            raise ValueError(f"duplicate strategy names: {dupes}")
        for strategy in self.strategies:
            for model in strategy.models:
                if model.name not in codes:
                    raise ValueError(f"strategies.{strategy.name}: model {model.name!r} is not a seat of this desk")
        return self

    # ---- views ---------------------------------------------------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)

    @staticmethod
    def from_json(text: str) -> "Mandate":
        return Mandate(**json.loads(text))

    def summary(self) -> str:
        """The readable text kept in `desks.mandate` (up to 4000 characters)."""
        parts = [f"{self.name}: {self.line} desk" + (f" ({self.variant})" if self.variant else "") + f", {self.rebalance} cadence.",
                 f"Risk: up to {self.risk.max_risk_per_trade_pct:g}% per trade, {self.risk.max_open_positions} open positions, "
                 f"{self.risk.max_drawdown_pct:g}% drawdown."]
        for s in self.strategies:
            parts.append(f"Strategy {s.name} ({s.horizon}, {s.cadence}): " + ", ".join(f"{m.name} x{m.weight:g}" for m in s.models)
                         + f"; blend {s.blend.method}.")
        if self.notes:
            parts.append(self.notes)
        return " ".join(parts)[:4000]

    def to_desk_spec(self, universe: Sequence[str], *, capital: float, benchmark: str = "SPY") -> DeskSpec:
        """The core spec of this mandate for one run: the universe comes in here, not from the mandate."""
        strategies = [StrategySpec(name=s.name, display_name=s.display_name, line=self.line, horizon=s.horizon,
                                   universe=list(universe), models=s.models, blend=s.blend, benchmark=benchmark)
                      for s in self.strategies]
        return DeskSpec(name=self.name_slug(), strategies=strategies, capital=capital, rebalance=self.rebalance,
                        benchmark=benchmark)

    def name_slug(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")
        return slug or "desk"


def parse_mandate(data: Any, source: str = "<mandate>") -> Mandate:
    """A mandate from a mapping. Extra or missing fields raise SpecError naming each one."""
    if not isinstance(data, Mapping):
        raise SpecError(f"{source}: the mandate must be a mapping of fields")
    try:
        return Mandate(**dict(data))
    except ValidationError as exc:
        raise SpecError(f"{source}: {_describe(exc)}") from exc


def load_mandate(path: str | Path) -> Mandate:
    """Load a mandate from a YAML file with the same errors as the core spec loader."""
    return parse_mandate(_read(path), str(path))


# ---- analyst plan (TA-21) and selection (TA-28) ------------------------------------------------------------------

@dataclass(frozen=True)
class AnalystStep:
    key: str
    seat_code: str
    enabled: bool = True


def analyst_plan(mandate: Mandate | Iterable[str] | None = None, *, line: Line = "SWO", variant: str | None = None) -> tuple[AnalystStep, ...]:
    """Which analysts a desk runs and in which order.

    A mandate gives its own list (its order is the order); a mandate with no list, or None, gives every analyst of the
    table that the line carries, in table order. A list of keys works like a mandate list. A key that is not in the table
    raises AnalystPlanError."""
    if isinstance(mandate, Mandate):
        line, variant, entries = mandate.line, mandate.variant, [(a.key, a.enabled) for a in mandate.analysts]
    elif mandate is None:
        entries = []
    else:
        entries = [(k, True) for k in mandate]
    for key, _ in entries:
        if key not in ANALYSTS:
            raise AnalystPlanError(f"unknown analyst {key!r}; the table has: {', '.join(ANALYSTS)}")
    codes = {s.code for s in seats_for(line, variant)}
    if not entries:
        entries = [(k, True) for k, code in ANALYSTS.items() if code in codes]
    seen: set[str] = set()
    steps: list[AnalystStep] = []
    for key, enabled in entries:
        if key in seen:
            raise AnalystPlanError(f"analyst {key!r} is in the plan twice")
        if ANALYSTS[key] not in codes:
            raise AnalystPlanError(f"analyst {key!r} is not a seat of a {line} desk")
        seen.add(key)
        steps.append(AnalystStep(key, ANALYSTS[key], enabled))
    return tuple(steps)


@dataclass(frozen=True)
class AnalystSelection:
    run: tuple[str, ...]                    # analysts that go into the graph, in plan order
    off: tuple[str, ...]                    # analysts left out
    reports: Mapping[str, Report] = field(default_factory=dict)   # the absent report of each analyst left out


def select_analysts(plan: Sequence[AnalystStep], *, off: Iterable[str] = ()) -> AnalystSelection:
    """The analysts of one run: the plan minus the ones the plan or this call turn off.

    An analyst left out is not in `run` (so not in the graph) and its report is declared absent; the run goes on. The
    selection never leaves the desk with no analyst."""
    extra = tuple(off)
    known = {step.key for step in plan}
    if unknown := [k for k in extra if k not in known]:
        raise AnalystPlanError(f"cannot turn off {unknown}: not in the plan of this desk")
    out = tuple(step.key for step in plan if not step.enabled or step.key in extra)
    run = tuple(step.key for step in plan if step.key not in out)
    if not run:
        raise AnalystPlanError("at least one analyst must stay on")
    return AnalystSelection(run, out, {key: make_report(key, None) for key in out})


# ---- editing the mandate of a desk (AH-25: "it is edited by chat and by the API of the desk") ------------------------------------

class MandateError(ValueError):
    """The mandate of a desk cannot be saved. `code` is stable: no_desk, no_mandate, line_mismatch."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _db(db: Database | None) -> Database:
    return db or get_database()


def load_desk_mandate(desk_id: int, *, db: Database | None = None) -> Mandate:
    """The typed mandate stored for a desk (`desk_mandates.spec_json`)."""
    with _db(db).read() as u:
        row = u.session.one("SELECT spec_json FROM desk_mandates WHERE desk_id = :d", {"d": desk_id})
    if row is None:
        raise MandateError("no_mandate", f"desk {desk_id} has no mandate")
    return Mandate.from_json(row["spec_json"])


def save_mandate(desk_id: int, data: Mandate | Mapping[str, Any], *, by_owner: bool, db: Database | None = None) -> Mandate:
    """Replace the mandate of a desk. One door for the chat and the API of the desk.

    The mandate is validated first (extra keys, instruments and unknown analysts fail with a `SpecError` that names the field). The
    line and variant of a desk never change by editing its mandate. The mandate counts as the owner's (`approved_by = 'owner'`)
    only when `by_owner` is true; an edit by anyone else clears that mark. The readable summary in `desks.mandate` is rewritten in
    the same transaction."""
    mandate = parse_mandate(data.model_dump(mode="json") if isinstance(data, Mandate) else data)
    with _db(db).write() as u:
        s = u.session
        profile = s.one("SELECT line, variant FROM desk_profile WHERE desk_id = :d", {"d": desk_id})
        if profile is None:
            raise MandateError("no_desk", f"desk {desk_id} has no profile")
        current = s.one("SELECT version FROM desk_mandates WHERE desk_id = :d", {"d": desk_id})
        if current is None:
            raise MandateError("no_mandate", f"desk {desk_id} has no mandate")
        if mandate.line != profile["line"] or (mandate.variant or None) != (profile["variant"] or None):
            raise MandateError("line_mismatch", "the line and the variant of a desk do not change with its mandate")
        s.update("desk_mandates", {"desk_id": desk_id}, {"spec_json": mandate.to_json(), "version": int(current["version"]) + 1,
                                                          "approved_by": "owner" if by_owner else None, "updated_at": utcnow()})
        s.update("desks", {"id": desk_id}, {"mandate": mandate.summary()})
    return mandate


# ---- the memory of the desk in two layers (SKL-009) -----------------------------------------------------------------------------

RAW_KIND = "desk_debate"
CaseKey = tuple[str, str]       # (underlying, expiration as YYYY-MM-DD): what an options operation is about, read from its legs


def case_of(proposal_json: str | Mapping[str, Any]) -> CaseKey | None:
    """The underlying and the expiration of a stored proposal, read from its legs (never from a field an agent wrote)."""
    try:
        body = json.loads(proposal_json) if isinstance(proposal_json, str) else dict(proposal_json)
        found: list[tuple[str, str]] = []
        for leg in body.get("legs", []):
            parsed = parse_canonical(str(leg["contract_id"]))
            if parsed.underlying and parsed.expiration:
                found.append((parsed.underlying, parsed.expiration.isoformat()))
    except (ValueError, KeyError, TypeError, NoContract):
        return None
    return min(found, key=lambda item: item[1]) if found else None


class DecisionLog:
    """The decision log of a desk. Layer 1 is the raw debate, kept in `run_records`; layer 2 is the decision that was approved, kept in
    `decisions`. Only layer 2 feeds a later cycle (`approved`). A proposal the owner rejected is not opened again unless the owner asks
    for it (`reopen_blocked`). Everything is a table of the database of Hokma: there are no files."""

    def __init__(self, desk_id: int, *, db: Database | None = None) -> None:
        self.desk_id, self._db = desk_id, db

    def record_raw(self, *, symbol: str, as_of: datetime, debate: Mapping[str, Any], judge: Mapping[str, Any] | None = None,
                   stopped_at: str | None = None) -> int:
        """Layer 1: everything that was argued, with the rounds. It is a record of the debate, not a decision."""
        signature = hashlib.sha256(f"{self.desk_id}|{symbol.upper()}|{as_of.isoformat()}".encode()).hexdigest()
        return runs.record(self.desk_id, RAW_KIND, signature=signature, db=self._db,
                           entries={"symbol": symbol.upper(), "as_of": as_of.isoformat()},
                           outputs={"debate": dict(debate), "judge": None if judge is None else dict(judge), "stopped_at": stopped_at})

    def raw(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return runs.list_runs(self.desk_id, kind=RAW_KIND, limit=limit, db=self._db)

    def _decided(self, status: str, limit: int) -> list[dict[str, Any]]:
        with _db(self._db).read() as u:
            rows = u.session.query(
                "SELECT d.id AS decision_id, d.topic, d.proposal, d.status, d.decided_at, p.id AS proposal_id, p.proposal_json "
                "FROM decisions d JOIN exec_proposals p ON p.decision_id = d.id WHERE p.desk_id = :d AND d.status = :s "
                "ORDER BY d.id DESC LIMIT :n", {"d": self.desk_id, "s": status, "n": max(1, min(int(limit), 500))})
        out = []
        for row in rows:
            key = case_of(row["proposal_json"])
            out.append({"decision_id": row["decision_id"], "proposal_id": row["proposal_id"], "topic": row["topic"], "proposal": row["proposal"],
                        "decided_at": row["decided_at"], "underlying": key[0] if key else None, "expiration": key[1] if key else None})
        return out

    def approved(self, symbol: str | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
        """Layer 2: the decisions of this desk that were approved, newest first. The only layer a cycle reads."""
        rows = self._decided("approved", limit)
        return [r for r in rows if symbol is None or r["underlying"] == symbol.strip().upper()]

    def rejected_cases(self, symbol: str | None = None) -> list[CaseKey]:
        return [(r["underlying"], r["expiration"]) for r in self._decided("rejected", 500)
                if r["underlying"] and (symbol is None or r["underlying"] == symbol.strip().upper())]

    def reopen_blocked(self, underlying: str, expiration: date | str, *, owner_order: bool = False) -> bool:
        """True when the owner rejected an operation on this underlying and expiration and has not ordered it again."""
        if owner_order:
            return False
        key = (underlying.strip().upper(), expiration.isoformat() if isinstance(expiration, date) else str(expiration))
        return key in self.rejected_cases(key[0])

    def record_approved(self, proposal_id: int) -> int | None:
        """Layer 2 for an order that left under a rule of the owner (level 3): there is no decision row yet, so one is written, approved by that
        rule, and linked to the proposal. An order approved by the owner already has its decision. Returns None when it cannot be recorded."""
        from hokma.db import Actor, DbError

        database = _db(self._db)
        try:
            with database.write() as u:
                row = u.session.one("SELECT * FROM exec_proposals WHERE id = :i AND desk_id = :d", {"i": proposal_id, "d": self.desk_id})
                if row is None or row["status"] not in ("sent", "filled", "partially_filled"):
                    return None
                if row["decision_id"]:
                    return int(row["decision_id"])
                approver = row["approved_by"] or ""
                if not approver.startswith("rule:"):
                    return None
                proposer = row["created_by"] if u.agents.find(row["created_by"]) is not None else next((a.slug for a in u.agents.list()), None)
                if proposer is None:
                    return None
                body = json.loads(row["proposal_json"])
                decision = u.decisions.propose(f"Order sent under {approver}"[:200], str(body.get("thesis") or "Order sent under a rule of the owner"), proposed_by=proposer,
                                               context=row["proposal_json"] if len(row["proposal_json"]) <= 8000 else None)
                u.decisions.decide(decision.id, "approved", actor=Actor.agent(approver))
                u.session.update("exec_proposals", {"id": proposal_id}, {"decision_id": decision.id})
                return int(decision.id)
        except (DbError, ValueError, KeyError):
            return None


def decision_log(desk_id: int, *, db: Database | None = None) -> DecisionLog:
    """The two layer decision log of one desk (SKL-009)."""
    return DecisionLog(desk_id, db=db)


# ---- Grant (A15): the tools, the answers and the run of each task -----------------------------------------------------------------

GRANT = "A15"
RATING_SCALE = ("APPROVE", "APPROVE_WITH_CONDITIONS", "REJECT")
_RANK = {"REJECT": 0, "APPROVE_WITH_CONDITIONS": 1, "APPROVE": 2}
CLASSES = ("true_positive", "false_positive", "missed_opportunity", "regime_mismatch")
HORIZON_DAYS = (5, 20)
MIN_VIEWS = 20                       # views of one source before a change of its weight may be suggested (CTS-024)
MAX_PAGE_CHARS = 3200                # one page: the note is rejected above this
MAX_REFLECTIONS = 10
MODE_SKILLS: Mapping[str, tuple[str, ...]] = {"note": ("morning-note",), "postmortem": ("signal-postmortem",), "reflect": (), "final": ()}
LEVEL_CHANGE_KEYS = frozenset({"level_change", "set_level", "new_level", "desk_level_set", "level_request"})

_T = {"type": "string", "maxLength": 600}
_SHORT = {"type": "string", "minLength": 1, "maxLength": 300}
_LIST = {"type": "array", "items": {"type": "string", "maxLength": 400}, "maxItems": 12}

NOTE_SCHEMA: Mapping[str, Any] = {
    "type": "object", "required": ["headline", "material", "opinions", "overnight", "events_today", "ideas", "previous_error", "data_gaps"],
    "properties": {
        "headline": _SHORT, "material": {"type": "boolean"},
        "opinions": {"type": "array", "maxItems": 8, "items": {"type": "object", "required": ["text", "at"], "properties": {"text": _T, "at": {"type": "string"}}}},
        "overnight": {"type": "array", "maxItems": 12, "items": {"type": "object", "required": ["position", "text", "at"],
                                                                 "properties": {"position": {"type": "string"}, "text": _T, "at": {"type": "string"}}}},
        "events_today": {"type": "array", "maxItems": 12, "items": {"type": "object", "required": ["event", "at", "text"],
                                                                   "properties": {"event": _SHORT, "at": {"type": "string"}, "text": _T}}},
        "ideas": {"type": "array", "maxItems": 6, "items": {"type": "object", "required": ["idea", "risk", "report"],
                                                             "properties": {"idea": _T, "risk": _T, "report": {"type": "string"}}}},
        "previous_error": {"type": ["string", "null"], "maxLength": 400},
        "data_gaps": _LIST,
    },
}

POSTMORTEM_SCHEMA: Mapping[str, Any] = {
    "type": "object", "required": ["summary", "raw_return_notice", "sources", "improvements", "left_out", "data_gaps"],
    "properties": {
        "summary": _T, "raw_return_notice": _T,
        "sources": {"type": "array", "maxItems": 30, "items": {"type": "object", "required": ["source", "views", "dominant_class", "weight_change", "text"], "properties": {
            "source": {"type": "string"}, "views": {"type": "integer"}, "dominant_class": {"type": "string", "enum": [*CLASSES, "none"]},
            "weight_change": {"type": "string", "enum": ["none", "raise", "lower"]}, "text": _T}}},
        "improvements": {"type": "array", "maxItems": 30, "items": {"type": "object", "required": ["source", "text"], "properties": {"source": {"type": "string"}, "text": _T}}},
        "left_out": _LIST, "data_gaps": _LIST,
    },
}

REFLECTION_SCHEMA: Mapping[str, Any] = {
    "type": "object", "required": ["reflections", "data_gaps"],
    "properties": {
        "reflections": {"type": "array", "maxItems": MAX_REFLECTIONS, "items": {"type": "object", "required": ["order_id", "lesson"], "properties": {
            "order_id": {"type": "integer"}, "lesson": {"type": "string", "minLength": 1, "maxLength": 1200}}}},
        "data_gaps": _LIST,
    },
}

FINAL_SCHEMA: Mapping[str, Any] = {
    "type": "object", "required": ["rating", "grade", "thesis", "would_change", "horizon", "desk_level", "summary", "data_gaps"],
    "properties": {
        "rating": {"type": "string", "enum": list(RATING_SCALE)}, "grade": {"type": "string", "enum": ["A", "B", "C", "D"]},
        "thesis": {"type": "string", "minLength": 1, "maxLength": 500}, "would_change": {"type": "array", "items": _T, "maxItems": 6},
        "horizon": {"type": "string", "enum": ["days", "weeks", "months"]}, "desk_level": {"type": "integer"},
        "summary": _T, "data_gaps": _LIST,
    },
}

TASKS: Mapping[str, str] = {
    "note": "Write the morning note of the desk from the facts of this run. Start with the main call. Put the cut-off time on every opinion.",
    "postmortem": "Write the postmortem of the views of the investor profiles from the classified table in the facts. Executed orders only.",
    "reflect": "Write one short lesson for each settled order in the facts, from what its measured outcome shows. Only the orders listed there.",
    "final": ("Give the final rating of the operation after the tribunal and the risk committee: the thesis in one sentence, what would change "
              "the rating and the horizon. Report the autonomy level of the desk exactly as the facts give it. Never rate above the judge."),
}


class NotSettled(LessonError):
    """A reflection was asked for a lesson that was not settled by `lessons.settle` yet."""


@dataclass(frozen=True)
class ManagerResult:
    """What Grant answered for one task, checked. `valid` is False when the answer does not validate: the caller never uses it."""

    mode: str
    report: AnalystReport
    value: dict | None
    check: kit.ReportCheck
    degraded: bool = False
    warnings: tuple[str, ...] = ()
    calls: tuple[str, ...] = ()
    final: ManagerReport | None = None
    thesis: str = ""
    would_change: tuple[str, ...] = ()
    horizon: str = ""
    level: int | None = None

    @property
    def valid(self) -> bool:
        return self.check.valid and not self.degraded

    @property
    def text(self) -> str:
        parts = [self.report.summary, *(f"- {f}" for f in self.report.findings)]
        if self.report.data_gaps:
            parts.append("Data gaps: " + "; ".join(self.report.data_gaps))
        return "\n".join(p for p in parts if p).strip()


# ---- tools of the seat: book_pnl, lessons_propose, schedule_run, all bound to the desk of the run ---------------------------------

def _day(value: Any, name: str) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        raise ToolFailure("bad_input", f"{name} must be a date as YYYY-MM-DD") from None


def _own_desk(desk_id: int, args: dict[str, Any]) -> dict[str, Any]:
    """A tool of Grant is always about his own desk: a desk id the model writes must be that one or it is refused."""
    rest = dict(args)
    given = rest.pop("desk_id", desk_id)
    if given != desk_id:
        raise ToolFailure("blocked", "a tool of this seat works on the desk of the run only")
    return rest


def grant_toolbox(desk_id: int, *, handlers: Mapping[str, Callable[..., Any]] | None = None, audit: ToolAuditLog | None = None,
                  db: Database | None = None) -> Toolbox:
    """Exactly the tools of the seat (`plantel.SEATS['A15'].tools`). Every handler gets the desk of the run, never the model's choice.
    `handlers` replace a tool body (tests, recorded data); a handler for a tool outside the seat is refused."""
    seat = SEATS[GRANT]
    given = dict(handlers or {})
    if extra := sorted(set(given) - set(seat.tools)):
        raise ToolFailure("blocked", f"the seat of {seat.name} has no tool {extra[0]!r}")

    def pnl_body(start: str, end: str) -> Any:
        from hokma.backoffice import pnl

        return pnl.pnl(desk_id, start=f"{_day(start, 'start')} 00:00:00", end=f"{_day(end, 'end')} 23:59:59", db=db).to_dict()

    def lesson_body(decision_id: int, order_id: int, text: str) -> Any:
        batch = propose_lessons(desk_id, [(int(decision_id), int(order_id), str(text))], db=db)
        if batch.rejected:
            raise ToolFailure("blocked", f"not_executed: {batch.rejected[0][1]}")
        lesson = batch.written[0]
        return {"lesson_id": lesson.id, "status": lesson.status, "order_id": lesson.order_id}

    def schedule_body(expr: str, instruction: str) -> Any:
        from hokma.broker.service import Who
        from hokma.desks.swo.create import desk_agent_slug
        from hokma.scheduler import service as scheduler

        return scheduler.schedule_desk_run(desk_id, expr=str(expr), instruction=str(instruction), who=Who(agent=desk_agent_slug(seat, desk_id, db)), db=db)

    bodies: dict[str, tuple[Callable[..., Any], str, dict[str, Any], list[str], bool]] = {
        "book_pnl": (pnl_body, "Realized profit and loss of your desk between two dates, with commissions taken out, by symbol.",
                     {"start": {"type": "string"}, "end": {"type": "string"}}, ["start", "end"], False),
        "lessons_propose": (lesson_body, "Propose a lesson of your desk. It is accepted only for an order that really executed.",
                            {"decision_id": {"type": "integer"}, "order_id": {"type": "integer"}, "text": {"type": "string"}},
                            ["decision_id", "order_id", "text"], True),
        "schedule_run": (schedule_body, "Schedule a run of your own desk. It saves the job off: the owner turns it on.",
                         {"expr": {"type": "string"}, "instruction": {"type": "string"}}, ["expr", "instruction"], True),
    }
    catalog = ToolCatalog()
    for name in seat.tools:
        body, description, props, required, writes = bodies[name]
        override = given.get(name)

        def handler(_body: Callable[..., Any] = body, _override: Callable[..., Any] | None = override, **args: Any) -> Any:
            clean = _own_desk(desk_id, args)
            return _override(desk_id=desk_id, **clean) if _override is not None else _body(**clean)

        catalog.register(ToolSpec(name, description, {"type": "object", "properties": {"desk_id": {"type": "integer"}, **props}, "required": required},
                                  handler, mutates=writes))
    catalog.assign(GRANT, seat.tools)
    return catalog.toolbox_for(GRANT, audit=audit)


# ---- prompts and the run loop ----------------------------------------------------------------------------------------------------

def system_prompt(mode: str, *, line: str, desk_id: int, as_of: datetime, kit_root: Path | None = None, composer: PromptComposer | None = None,
                  language: str | None = None, lacks: str | Sequence[str] | None = None) -> str:
    """Common rules, the line brief, the agent file of Grant as the profile, only the skills of the task and its answer format. A seat file
    that does not carry the line is refused, so no skill of another line is ever added."""
    schema = _task(mode).spec.schema
    seat_file = kit.load_seat_file(GRANT, kit_root)
    kit._check_line(seat_file, line)
    base = (composer or PromptComposer()).compose(role="chief", line=line, desk_id=desk_id, as_of=as_of, language=language, lacks=lacks,
                                                  persona=seat_file.body)
    chosen = [s for s in seat_file.skills if s.name in MODE_SKILLS[mode]]
    blocks = [base.rstrip("\n"), *(kit.skill_block(s) for s in chosen), f"## Numbers\n{kit.NUMBERS_RULE}", f"## Task\n{TASKS[mode]}",
              kit.answer_format(schema)]
    return "\n\n".join(blocks) + "\n"


@dataclass(frozen=True)
class _Task:
    mode: str
    spec: kit.OutputSpec
    render: Callable[[dict], AnalystReport]


def _facts_of(ctx: kit.RunContext) -> Mapping[str, Any]:
    return ctx.facts


def _unknown_keys(schema: Mapping[str, Any]) -> Callable[[dict, kit.RunContext, kit.EvidenceLog], list[str]]:
    allowed = set(schema["properties"])

    def check(value: dict, ctx: kit.RunContext, evidence: kit.EvidenceLog) -> list[str]:
        problems = [f"the field {k!r} is not part of the answer" for k in sorted(set(value) - allowed)]
        problems += [f"the field {k!r} is not allowed: only the owner moves the level of a desk" for k in sorted(set(kit._all_keys(value)) & LEVEL_CHANGE_KEYS)]
        return problems

    return check


def _parse_time(text: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def _note_checks(value: dict, ctx: kit.RunContext, evidence: kit.EvidenceLog) -> list[str]:
    problems: list[str] = []
    if not value["opinions"]:
        problems.append("the note needs at least one opinion with its time")
    for index, item in enumerate(value["opinions"]):
        stamp = _parse_time(item["at"])
        if stamp is None:
            problems.append(f"opinions[{index}].at is not a time")
        elif stamp > ctx.as_of:
            problems.append(f"opinions[{index}].at is later than the cut-off")
    if not value["material"] and (value["overnight"] or value["ideas"]):
        problems.append("a note that says nothing is material lists no overnight line and no idea")
    if value["material"] is False and not value["headline"].lower().startswith("nothing material"):
        problems.append('a quiet day starts with "Nothing material"')
    if sum(len(x) for x in _note_lines(value)) > MAX_PAGE_CHARS:
        problems.append(f"the note is longer than one page ({MAX_PAGE_CHARS} characters)")
    owed = ctx.facts.get("last_note_error")
    if owed and not value.get("previous_error"):
        problems.append("the last note held an error (see the facts): name it in previous_error")
    if not owed and value.get("previous_error"):
        problems.append("previous_error names an error that the facts do not hold")
    return problems


def _note_lines(value: dict) -> list[str]:
    lines = [f"[{o['at']}] {o['text']}" for o in value["opinions"]]
    lines += [f"{o['position']} [{o['at']}]: {o['text']}" for o in value["overnight"]]
    lines += [f"{e['event']} [{e['at']}]: {e['text']}" for e in value["events_today"]]
    lines += [f"Idea: {i['idea']} Risk: {i['risk']} (report {i['report']})" for i in value["ideas"]]
    if value.get("previous_error"):
        lines.append(f"Correction of the last note: {value['previous_error']}")
    return lines


def _render_note(value: dict) -> AnalystReport:
    return AnalystReport(value["headline"], tuple(_note_lines(value)), tuple(value["data_gaps"]))


def _table_sources(facts: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(s["source"]): s for s in facts.get("sources", [])}


def _postmortem_checks(value: dict, ctx: kit.RunContext, evidence: kit.EvidenceLog) -> list[str]:
    problems: list[str] = []
    table = _table_sources(ctx.facts)
    if "price return" not in value["raw_return_notice"].lower():
        problems.append("raw_return_notice must say that the figures are raw price returns")
    seen: set[str] = set()
    for index, item in enumerate(value["sources"]):
        source = item["source"]
        seen.add(source)
        known = table.get(source)
        if known is None:
            problems.append(f"sources[{index}]: {source!r} has no executed view in the table")
            continue
        if item["views"] != known["views"]:
            problems.append(f"sources[{index}]: views must be {known['views']}, the count of the table")
        if item["dominant_class"] != known["dominant_class"]:
            problems.append(f"sources[{index}]: the dominant class of the table is {known['dominant_class']}")
        if known["views"] < MIN_VIEWS and item["weight_change"] != "none":
            problems.append(f"sources[{index}]: with fewer than {MIN_VIEWS} views no change of weight is suggested")
    problems += [f"sources: {s!r} is in the table and missing from the answer" for s in sorted(set(table) - seen)]
    for index, item in enumerate(value["improvements"]):
        if item["source"] not in table:
            problems.append(f"improvements[{index}]: {item['source']!r} is not a source of the table")
        elif table[item["source"]]["views"] < MIN_VIEWS and re.search(r"\b(weigh|weight)\w*\s+(more|less|higher|lower)|raise|lower", item["text"], re.I):
            problems.append(f"improvements[{index}]: no change of weight with fewer than {MIN_VIEWS} views")
    return problems


def _render_postmortem(value: dict) -> AnalystReport:
    findings = [value["raw_return_notice"]]
    findings += [f"{s['source']}: {s['text']}" for s in value["sources"]]
    findings += [f"Improve {i['source']}: {i['text']}" for i in value["improvements"]]
    if value["left_out"]:
        findings.append("Left out: " + "; ".join(value["left_out"]))
    return AnalystReport(value["summary"], tuple(findings), tuple(value["data_gaps"]))


def _reflection_checks(value: dict, ctx: kit.RunContext, evidence: kit.EvidenceLog) -> list[str]:
    known = {int(o["order_id"]) for o in ctx.facts.get("orders", [])}
    problems = [f"reflections[{i}]: order {r['order_id']} is not a settled order of this run" for i, r in enumerate(value["reflections"])
                if int(r["order_id"]) not in known]
    ids = [int(r["order_id"]) for r in value["reflections"]]
    problems += [f"order {n} has two reflections" for n in sorted({n for n in ids if ids.count(n) > 1})]
    return problems


def _render_reflection(value: dict) -> AnalystReport:
    return AnalystReport("Lessons from settled orders.", tuple(f"Order {r['order_id']}: {r['lesson']}" for r in value["reflections"]),
                         tuple(value["data_gaps"]))


def _final_checks(value: dict, ctx: kit.RunContext, evidence: kit.EvidenceLog) -> list[str]:
    problems: list[str] = []
    ceiling = ctx.facts.get("judge_rating")
    if ceiling not in _RANK:
        problems.append("the facts hold no rating of the judge: Grant cannot rate on his own")
    elif _RANK[value["rating"]] > _RANK[ceiling]:
        problems.append(f"the rating {value['rating']} is above the rating of the judge ({ceiling})")
    if value["desk_level"] != ctx.facts.get("desk_level"):
        problems.append("desk_level must be the level of the desk as the facts give it")
    if not value["would_change"]:
        problems.append("would_change needs at least one item: what would change the rating")
    return problems


def _render_final(value: dict) -> AnalystReport:
    findings = (f"Thesis: {value['thesis']}", *(f"Would change it: {w}" for w in value["would_change"]), f"Horizon: {value['horizon']}.",
                f"Level of the desk: {value['desk_level']}.")
    return AnalystReport(value["summary"], findings, tuple(value["data_gaps"]))


_TASKS: dict[str, _Task] = {
    "note": _Task("note", kit.OutputSpec(NOTE_SCHEMA, id_keys=frozenset({"at", "position", "report"}), extra_forbidden=LEVEL_CHANGE_KEYS,
                                         checks=(_unknown_keys(NOTE_SCHEMA), _note_checks)), _render_note),
    "postmortem": _Task("postmortem", kit.OutputSpec(POSTMORTEM_SCHEMA, id_keys=frozenset({"source"}), extra_forbidden=LEVEL_CHANGE_KEYS,
                                                     checks=(_unknown_keys(POSTMORTEM_SCHEMA), _postmortem_checks)), _render_postmortem),
    "reflect": _Task("reflect", kit.OutputSpec(REFLECTION_SCHEMA, extra_forbidden=LEVEL_CHANGE_KEYS,
                                               checks=(_unknown_keys(REFLECTION_SCHEMA), _reflection_checks)), _render_reflection),
    "final": _Task("final", kit.OutputSpec(FINAL_SCHEMA, extra_forbidden=LEVEL_CHANGE_KEYS,
                                           checks=(_unknown_keys(FINAL_SCHEMA), _final_checks)), _render_final),
}


def _task(mode: str) -> _Task:
    if mode not in _TASKS:
        raise ValueError(f"unknown task {mode!r}; Grant has: {', '.join(_TASKS)}")
    return _TASKS[mode]


def _run(mode: str, model: ChatModel, *, desk_id: int, symbol: str = "", as_of: datetime, line: str = "SWO", snapshot: Any = None,
         facts: Mapping[str, Any] | None = None, kit_root: Path | None = None, composer: PromptComposer | None = None,
         handlers: Mapping[str, Callable[..., Any]] | None = None, audit: ToolAuditLog | None = None, db: Database | None = None,
         max_tool_rounds: int = kit.DEFAULT_TOOL_ROUNDS, language: str | None = None) -> ManagerResult:
    """One task of Grant. The model may call the tools of its seat for up to `max_tool_rounds` rounds, then answers with the object of
    the task. An answer that does not validate is asked again once; a request for a tool outside the seat makes the answer invalid."""
    task = _task(mode)
    ctx = kit.RunContext(desk_id, symbol.strip().upper(), as_of, line, dict(facts or {}))
    evidence = kit.EvidenceLog(snapshot, ctx.facts)
    box = grant_toolbox(desk_id, handlers=handlers, audit=audit, db=db)
    refused: list[str] = []
    inner = evidence.runner(box)

    def run_tool(call: Any) -> str:
        if call.name not in box.names():
            refused.append(call.name)
        return inner(call)

    system = system_prompt(mode, line=line, desk_id=desk_id, as_of=as_of, kit_root=kit_root, composer=composer, language=language)
    messages: list[Message] = [Message("user", kit.user_prompt(ctx, snapshot, ctx.facts), id="u0")]
    schemas = box.schemas()
    rounds, retried, serial = 0, False, 0
    while True:
        offered = schemas if rounds < max_tool_rounds else []
        reply: ModelReply = model.chat(system, messages, offered)
        if reply.tool_calls and offered:
            serial += 1
            messages.append(Message("assistant", reply.content or "", id=f"a{serial}", tool_calls=tuple(reply.tool_calls)))
            for call in reply.tool_calls:
                messages.append(Message("tool", run_tool(call), tool_call_id=call.id, name=call.name))
            rounds += 1
            continue
        parsed = parse_structured(reply.content or "", dict(task.spec.schema))
        if parsed.degraded and not retried:
            retried = True
            messages.append(Message("assistant", reply.content or "", id="a-final"))
            messages.append(Message("user", "That answer was not valid: " + "; ".join(parsed.warnings)
                                    + ". Answer again with only the JSON object of the schema.", id="u-retry"))
            rounds = max_tool_rounds
            continue
        break
    done = kit.finalize(parsed, task.spec, task.render, ctx, evidence)
    check = done.check
    if refused:
        check = kit.ReportCheck(False, (*check.problems, *(f"asked for the tool {n!r}, which is not a tool of this seat" for n in sorted(set(refused)))), check.unverified)
    value = done.value
    final = None
    extras: dict[str, Any] = {}
    if mode == "final" and value is not None and check.valid and not done.degraded:
        final = ManagerReport(value["rating"], value["grade"], value["summary"], ())
        extras = {"thesis": value["thesis"], "would_change": tuple(value["would_change"]), "horizon": value["horizon"], "level": int(value["desk_level"])}
    return ManagerResult(mode, done.report, value, check, done.degraded, done.warnings, done.calls, final, **extras)


# ---- AFS-034: the morning note ----------------------------------------------------------------------------------------------------

def note_facts(as_of: datetime, *, positions: Sequence[Mapping[str, Any]] = (), events: Sequence[Mapping[str, Any]] = (),
               reports: Mapping[str, str] | None = None, last_note_error: str | None = None, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The facts of a morning note, built by code: the cut-off, the positions with their overnight move, the events of the day (each with its
    scheduled time), the keys of the analyst reports and the error of the previous note when there was one."""
    stamp = as_of.astimezone(timezone.utc).isoformat()
    body: dict[str, Any] = {"cutoff": stamp, "positions": [dict(p) for p in positions], "events_today": [dict(e) for e in events],
                            "report_keys": sorted(reports or {}), "reports": dict(reports or {}), "last_note_error": last_note_error}
    body.update(extra or {})
    return body


def morning_note(model: ChatModel, *, desk_id: int, as_of: datetime, facts: Mapping[str, Any], line: str = "SWO", **kwargs: Any) -> ManagerResult:
    """The opening note of the day (AFS-034). One page, the most important thing first, the time on every opinion; "Nothing material" is a valid
    note. A previous error in the facts must be named in the note. Grant has only the tools of his seat and every number is in the evidence."""
    return _run("note", model, desk_id=desk_id, as_of=as_of, line=line, facts=facts, **kwargs)


# ---- TA-13: the final rating --------------------------------------------------------------------------------------------------------

def final_facts(*, desk_level: int, judge_rating: str, symbol: str, filter_summary: str = "", judge_rationale: str = "", committee: Mapping[str, Any] | None = None,
                proposal: Mapping[str, Any] | None = None, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"symbol": symbol, "desk_level": desk_level, "judge_rating": judge_rating, "filter": filter_summary, "judge_rationale": judge_rationale,
                            "committee": dict(committee or {}), "proposal": dict(proposal or {})}
    body.update(extra or {})
    return body


def final_rating(model: ChatModel, *, desk_id: int, as_of: datetime, symbol: str, facts: Mapping[str, Any], line: str = "SWO", **kwargs: Any) -> ManagerResult:
    """Grant closes the operation (TA-13, J1): the rating on the scale of the judge (never above it), the thesis, what would change it and the
    horizon. The level of the desk is reported as the facts give it; an answer that tries to change it is rejected."""
    return _run("final", model, desk_id=desk_id, symbol=symbol, as_of=as_of, line=line, facts=facts, **kwargs)


# ---- TA-43: reflection on settled lessons of executed orders ---------------------------------------------------------------------

@dataclass(frozen=True)
class LessonBatch:
    written: tuple[Lesson, ...] = ()
    rejected: tuple[tuple[int, str], ...] = ()       # (order id, why): an order that did not execute teaches nothing yet


def propose_lessons(desk_id: int, items: Sequence[tuple[int, int, str]], *, proposed_by: str = "desk-manager", db: Database | None = None) -> LessonBatch:
    """Write lessons of a desk, each as (decision id, order id, text). Only the order that really executed counts: `lessons.store.propose`
    checks it and a decision with no executed order raises `NotExecuted`, which lands in `rejected` here. The same text for the same order is one lesson."""
    written: list[Lesson] = []
    rejected: list[tuple[int, str]] = []
    for decision_id, order_id, text in items:
        try:
            written.append(lesson_store.propose(desk_id, decision_id, order_id, text, proposed_by, db=db))
        except NotExecuted as exc:
            rejected.append((order_id, f"NotExecuted: {exc}"))
        except LessonError as exc:
            rejected.append((order_id, f"{type(exc).__name__}: {exc}"))
    return LessonBatch(tuple(written), tuple(rejected))


@dataclass(frozen=True)
class Settlement:
    """A lesson that `lessons.settle` measured, with what is needed to measure its twin again (the fills are the real ones of the broker)."""

    lesson: Lesson
    fills: tuple[Fill, ...]
    mark_price: float | None = None
    mark_at: str | None = None
    horizon_days: float | None = None
    multiplier: float = 1.0
    benchmark_return: float | None = None


@dataclass(frozen=True)
class Reflection:
    result: ManagerResult
    written: tuple[Lesson, ...] = ()
    rejected: tuple[tuple[int, str], ...] = ()


def _outcome_facts(settlements: Sequence[Settlement]) -> dict[str, Any]:
    orders = []
    for s in settlements:
        o = s.lesson.outcome or {}
        orders.append({"order_id": s.lesson.order_id, "lesson_id": s.lesson.id, "decision_id": s.lesson.decision_id, "symbol": o.get("symbol"),
                       "direction": o.get("direction"), "entry_price": o.get("entry_price"), "exit_price": o.get("exit_price"),
                       "exit_basis": o.get("exit_basis"), "return": o.get("return"), "pnl": o.get("pnl"), "costs": o.get("costs"),
                       "entry_slippage": o.get("entry_slippage"), "holding_days": o.get("holding_days"), "measured_from": s.lesson.measured_from})
    return {"orders": orders, "measured_from": "the price of the real entry fill of each order"}


def reflect(model: ChatModel, *, desk_id: int, as_of: datetime, settlements: Sequence[Settlement], line: str = "SWO", write: bool = True,
            db: Database | None = None, **kwargs: Any) -> Reflection:
    """Grant reflects on lessons already settled by `lessons.settle` (TA-43, J4). A lesson that is not settled raises `NotSettled`: the
    outcome must be measured, from the real entry, before anything is learned. Each valid reflection becomes a lesson of the desk for the
    same order and decision, measured the same way; an order that did not execute is refused by the lesson log (`NotExecuted`)."""
    for s in settlements:
        if s.lesson.status not in ("settled", "promoted"):
            raise NotSettled(f"lesson {s.lesson.id} is {s.lesson.status}: reflect only on lessons that lessons.settle measured")
        if s.lesson.desk_id != desk_id:
            raise LessonError(f"lesson {s.lesson.id} belongs to another desk")
    if not settlements:
        empty = kit.ReportCheck(True)
        return Reflection(ManagerResult("reflect", AnalystReport("Nothing settled to reflect on.", (), ()), {"reflections": [], "data_gaps": []}, empty))
    result = _run("reflect", model, desk_id=desk_id, as_of=as_of, line=line, facts=_outcome_facts(settlements), db=db, **kwargs)
    if not result.valid or not write or result.value is None:
        return Reflection(result)
    by_order = {s.lesson.order_id: s for s in settlements}
    written: list[Lesson] = []
    rejected: list[tuple[int, str]] = []
    for item in result.value["reflections"]:
        s = by_order[int(item["order_id"])]
        batch = propose_lessons(desk_id, [(s.lesson.decision_id, s.lesson.order_id, str(item["lesson"]))], db=db)
        rejected += batch.rejected
        for new in batch.written:
            written.append(new if new.status == "settled" else settle_lesson(
                new.id, fills=s.fills, mark_price=s.mark_price, mark_at=s.mark_at, horizon_days=s.horizon_days, multiplier=s.multiplier,
                benchmark_return=s.benchmark_return, db=db))
    return Reflection(result, tuple(written), tuple(rejected))


@dataclass(frozen=True)
class Learning:
    status: Literal["not_executed", "pending", "unsettled", "reflected", "invalid_reflection"]
    reason: str = ""
    seed: Lesson | None = None
    settled: Lesson | None = None
    reflection: Reflection | None = None


def learn_from_order(model: ChatModel, *, desk_id: int, decision_id: int, order_id: int, as_of: datetime, thesis: str, fills: Sequence[Fill],
                     mark_price: float | None = None, mark_at: str | None = None, horizon_days: float | None = None, multiplier: float = 1.0,
                     benchmark_return: float | None = None, line: str = "SWO", db: Database | None = None, **kwargs: Any) -> Learning:
    """Step 9 of the cycle for one order: record that it executed, measure it from the real entry fill and let Grant reflect on it.

    1. The record of the execution is a lesson row written by code. It exists only for an order that executed (otherwise `not_executed`).
    2. `lessons.settle` measures it from the entry fill; a window that has not traded leaves it `pending`: no partial result is recorded.
    3. Grant reflects on the settled lesson and each reflection is stored and settled the same way."""
    seed_text = f"Executed order {order_id}: {' '.join(thesis.split())[:600] or 'no thesis was recorded'} Measured from the real entry fill."
    batch = propose_lessons(desk_id, [(decision_id, order_id, seed_text)], proposed_by="desk-manager", db=db)
    if batch.rejected:
        return Learning("not_executed", batch.rejected[0][1])
    seed = batch.written[0]
    try:
        settled = settle_lesson(seed.id, fills=tuple(fills), mark_price=mark_price, mark_at=mark_at, horizon_days=horizon_days, multiplier=multiplier,
                                       benchmark_return=benchmark_return, db=db)
    except NotReady as exc:
        return Learning("pending", str(exc), seed)
    except NotExecuted as exc:
        return Learning("not_executed", str(exc), seed)
    except LessonError as exc:                       # an order that cannot be measured (fills the measure does not understand) never fails the cycle: the lesson waits
        return Learning("unsettled", str(exc), seed)
    reflection = reflect(model, desk_id=desk_id, as_of=as_of, line=line, db=db, settlements=[Settlement(
        settled, tuple(fills), mark_price, mark_at, horizon_days, multiplier, benchmark_return)], **kwargs)
    return Learning("reflected" if reflection.result.valid else "invalid_reflection", "; ".join(reflection.result.check.problems), seed, settled, reflection)


# ---- CTS-024: the postmortem of the views of the investor profiles ----------------------------------------------------------------

@dataclass(frozen=True)
class ViewRecord:
    """One view of a source that belongs to an order that executed. `entry_price` is the reference price of the symbol at the real entry."""

    source: str
    symbol: str
    value: float                    # -1 to 1: the sign is the direction the source expected
    confidence: int
    signal_at: datetime
    entry_at: datetime
    entry_price: float
    bias: int                       # +1 when the executed structure gains if the price rises, -1 when it gains if it falls
    order_id: int
    entry_source: str = "quote"
    regime_at_signal: str | None = None


@dataclass(frozen=True)
class PostmortemTable:
    rows: tuple[Mapping[str, Any], ...]
    sources: tuple[Mapping[str, Any], ...]
    left_out: tuple[str, ...]

    def facts(self) -> dict[str, Any]:
        return {"notice": "Returns are raw price returns of the symbol from the reference price at the real entry, not profit in money: they ignore size, leverage and costs.",
                "horizons_days": list(HORIZON_DAYS), "min_views": MIN_VIEWS, "sources": [dict(s) for s in self.sources], "rows": [dict(r) for r in self.rows[:200]],
                "left_out": list(self.left_out), "classes": list(CLASSES)}


def _expected(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _class_of(view: ViewRecord, ret: float, exit_regime: str | None) -> str | None:
    expected, actual = _expected(view.value), (1 if ret > 0 else -1 if ret < 0 else 0)
    if actual == 0:
        return None
    if expected == 0:
        return "missed_opportunity" if view.bias * actual > 0 else None
    if expected == actual:
        return "true_positive"
    if view.regime_at_signal is not None and exit_regime is not None and view.regime_at_signal != exit_regime:
        return "regime_mismatch"
    return "false_positive"


def _mark(series: pd.Series, entry_day: date, target: date, cut: date) -> tuple[date, float] | None:
    """The close of the last session on or before the target day, after the entry day and never after the cut-off. It never reads forward."""
    last: tuple[date, float] | None = None
    for stamp, price in series.items():
        day = pd.Timestamp(stamp).date()
        if entry_day < day <= min(target, cut) and day > (last[0] if last else entry_day):
            last = (day, float(price))
    return last


def classify_views(views: Sequence[ViewRecord], *, closes: Mapping[str, pd.Series], as_of: datetime, regime_at: Callable[[datetime], str | None] | None = None,
                   executed: Callable[[int], bool] | None = None) -> PostmortemTable:
    """Classify every executed view at five and at twenty days: true positive, false positive, missed opportunity or regime mismatch.

    Only views of executed orders enter (`executed` re-checks each order). A horizon that has not traded by the cut-off is pending; the
    price of a horizon is the last close on or before it, so no horizon looks ahead. A record with no usable price is left out and listed,
    never repaired. The regime is read at the view and at the exit."""
    cut = as_of.astimezone(timezone.utc).date()
    rows: list[dict[str, Any]] = []
    left_out: list[str] = []
    measured: dict[str, dict[int, list[str]]] = {}
    seen: dict[str, set[tuple[int, str]]] = {}
    pending: dict[str, int] = {}
    for v in views:
        if executed is not None and not executed(v.order_id):
            left_out.append(f"{v.source} on {v.symbol}: order {v.order_id} did not execute")
            continue
        series = closes.get(v.symbol)
        if not (v.entry_price and v.entry_price > 0) or series is None or len(series) == 0:
            left_out.append(f"{v.source} on {v.symbol}: no usable price record, left as it is")
            continue
        entry_day = v.entry_at.astimezone(timezone.utc).date()
        any_measured = False
        for horizon in HORIZON_DAYS:
            target = entry_day + pd.Timedelta(days=horizon).to_pytimedelta()
            base = {"source": v.source, "symbol": v.symbol, "order_id": v.order_id, "horizon_days": horizon}
            if cut < target:
                rows.append({**base, "status": "pending"})
                pending[v.source] = pending.get(v.source, 0) + 1
                continue
            mark = _mark(series, entry_day, target, cut)
            if mark is None or (target - mark[0]).days > 4:
                left_out.append(f"{v.source} on {v.symbol}: no close near day {horizon} after the entry")
                continue
            ret = mark[1] / v.entry_price - 1.0
            exit_regime = regime_at(datetime.combine(mark[0] + pd.Timedelta(days=1).to_pytimedelta(), datetime.min.time(), timezone.utc)) if regime_at else None
            cls = _class_of(v, ret, exit_regime)
            if cls is None:
                left_out.append(f"{v.source} on {v.symbol}: no class at day {horizon} (zero move or neutral view that did not miss a gain)")
                continue
            rows.append({**base, "status": "measured", "raw_price_return": ret, "class": cls, "regime_at_signal": v.regime_at_signal,
                         "regime_at_exit": exit_regime, "exit_session": mark[0].isoformat()})
            measured.setdefault(v.source, {}).setdefault(horizon, []).append(cls)
            any_measured = True
        if any_measured:
            seen.setdefault(v.source, set()).add((v.order_id, v.signal_at.isoformat()))
    sources = []
    for source in sorted(set(measured) | set(pending)):
        by_h = measured.get(source, {})
        longest = max(by_h) if by_h else None
        counts = {c: (by_h[longest].count(c) if longest else 0) for c in CLASSES}
        dominant = "none" if longest is None else max(CLASSES, key=lambda c: (counts[c], -CLASSES.index(c)))
        if longest is not None and counts[dominant] == 0:
            dominant = "none"
        sources.append({"source": source, "views": len(seen.get(source, ())), "pending_horizons": pending.get(source, 0), "horizon_used": longest,
                        "by_class": counts, "dominant_class": dominant})
    return PostmortemTable(tuple(rows), tuple(sources), tuple(left_out))


def executed_views(desk_id: int, *, db: Database | None = None) -> tuple[list[ViewRecord], list[str]]:
    """The views of this desk that belong to an order that executed, read from the cycle records and `desk_signals`. A view whose proposal was
    refused, is waiting for the owner or never left does not come back. The second item lists what was left out and why."""
    from hokma.db import from_db_time

    views: list[ViewRecord] = []
    left_out: list[str] = []
    database = _db(db)
    for run in runs.list_runs(desk_id, kind="swo_cycle", status="done", limit=500, db=database):
        for item in (run["record"].get("outputs") or {}).get("orders", []):
            with database.read() as u:
                prop = u.session.one("SELECT status, broker_order_record_id FROM exec_proposals WHERE id = :i AND desk_id = :d", {"i": item["proposal_id"], "d": desk_id})
                if prop is None or prop["broker_order_record_id"] is None:
                    left_out.append(f"proposal {item['proposal_id']}: no sent order")
                    continue
                try:
                    order = lesson_store.executed_order(u.session, int(prop["broker_order_record_id"]), desk_id)
                except NotExecuted as exc:
                    left_out.append(f"proposal {item['proposal_id']}: {exc}")
                    continue
                signals = u.session.query("SELECT seat_code, symbol, as_of, value, confidence, signal_json FROM desk_signals WHERE run_id = :r AND abstained = 0 ORDER BY id",
                                          {"r": run["id"]})
            ref = item.get("underlying_ref")
            for row in signals:
                if ref is None:
                    left_out.append(f"{row['seat_code']} on {row['symbol']}: no reference price at the entry")
                    continue
                try:
                    regime = (json.loads(row["signal_json"]).get("regime") if row.get("signal_json") else None) or (run["record"].get("outputs") or {}).get("regime")
                except ValueError:
                    regime = None
                views.append(ViewRecord(row["seat_code"], row["symbol"], float(row["value"]), int(row["confidence"]), from_db_time(row["as_of"]),
                                        from_db_time(order["created_at"]), float(ref), int(item.get("bias", 1)), int(prop["broker_order_record_id"]),
                                        str(item.get("ref_source", "quote")), regime))
    return views, left_out


def postmortem(model: ChatModel, *, desk_id: int, as_of: datetime, views: Sequence[ViewRecord], closes: Mapping[str, pd.Series],
               regime_at: Callable[[datetime], str | None] | None = None, line: str = "SWO", db: Database | None = None,
               extra_left_out: Sequence[str] = (), **kwargs: Any) -> tuple[PostmortemTable, ManagerResult | None]:
    """The postmortem of the views (CTS-024). The code classifies; Grant reads the table and writes what it means per source. Only executed
    orders enter (each order is checked again against the lesson log). With no measured view Grant is not asked."""
    database = _db(db)

    def executed(order_id: int) -> bool:
        with database.read() as u:
            try:
                lesson_store.executed_order(u.session, order_id, desk_id)
            except NotExecuted:
                return False
        return True

    table = classify_views(views, closes=closes, as_of=as_of, regime_at=regime_at, executed=executed)
    if extra_left_out:
        table = PostmortemTable(table.rows, table.sources, (*extra_left_out, *table.left_out))
    if not table.sources:
        return table, None
    return table, _run("postmortem", model, desk_id=desk_id, as_of=as_of, line=line, facts=table.facts(), db=database, **kwargs)

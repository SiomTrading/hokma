"""The risk committee of a desk: Jake (aggressive), Glen (conservative) and Reed (arbiter), and the pieces around them
(SPEC-019: TA-10, TA-11, TA-12, TA-19, TA-31, TA-49, TA-51, AH-19, AH-20, CTS-026, ECT-032, ECT-033, SKL-006).

The committee argues; the code decides. Nothing here applies a limit to a live order (the filter of `hokma.exec` does),
nothing sends an order and nothing calculates: every number in an output comes from a quant module through `quant_call`,
from the back office snapshot (`book_snapshot`) or from the facts the code put in the prompt. An output that carries a number
that is in none of them is marked `unverified` and does not validate; a claim without evidence is rejected.

- Voices (TA-10, TA-11, TA-12): the prompt is the composed rules of the desk, the agent file of the seat and the skills that
  file declares. The file must carry the line of the desk, so a skill of another line never reaches a prompt.
- Rotation (TA-19): Jake, Glen, Reed in a fixed order; the number of rounds comes from the line profile.
- Portfolio (TA-31, TA-49): three states, never mixed: not provided, flat and with positions. Without the back office the
  state is not provided and never flat. The SWO context carries greeks and expirations computed by `options.position_greeks`.
- Limits (AH-19, TA-51): `RiskLimits` is typed over the keys of `desk_limits.config` and rejects any other. The base is the
  owner's, set when the desk was created; Reed fills the contract by tightening only. A loosening asks the owner.
- Audit (AH-20): every cut the filter recorded in `exec_proposals.clamps_json`, with its guard, before and after.
- Process review (CTS-026): adherence, risk discipline and execution quality over the register of orders, with behavior tags only
  when the register shows the evidence.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from hokma.broker import rules
from hokma.db import Database, get_database
from hokma.desks.core import numbers
from hokma.desks.core.graph import ChatModel
from hokma.desks.core.hashing import content_hash
from hokma.desks.core.output import SchemaError, validate_schema
from hokma.desks.core.prompts import PromptComposer
from hokma.desks.core.signal import LINES
from hokma.desks.core.skills import Skill, SkillError, parse_skill, split_frontmatter
from hokma.desks.core.state import DebateState, Message, ToolCall, new_debate, record_round
from hokma.desks.core.tools import ToolCatalog, ToolFailure, ToolSpec, Toolbox
from hokma.desks.plantel import SEATS, Evidence, RiskArgument, RiskPoint, RiskVerdict, Seat, seat_file
from hokma.exec.money import ZERO, dec_or_none
from hokma.llm.jsonx import LLMParseError, extract_json
from hokma.quant import registry
from hokma.quant.errors import BadInput

# ---- the seats and the rotation (TA-19) ------------------------------------------------------------------------------

BOLD, CAREFUL, ARBITER = "A12", "A13", "A14"
VOICE_ORDER: tuple[str, ...] = (BOLD, CAREFUL, ARBITER)               # fixed: aggressive, conservative, neutral
STANCE_OF = {BOLD: "aggressive", CAREFUL: "conservative", ARBITER: "neutral"}
# Rounds of the committee by line profile: the fast lines argue once, the lines that hold risk for weeks argue twice.
LINE_ROUNDS: Mapping[str, int] = {"RF": 1, "DT": 1, "SWC": 1, "SWO": 2, "LP": 2}
MAX_ROUNDS = 4
MAX_TOOL_ROUNDS = 3
MAX_TEXT = 1200
MAX_POINTS = 8
MAX_EVIDENCE = 6
KIT_PATH = ("kit", "template", "desk")
# The lines each skill of the committee is written for (the matrix of pieces). A skill reaches the prompt of a desk only when its line is here.
SKILL_LINES: Mapping[str, frozenset[str]] = {
    "tail-risk-hedging": frozenset({"LP", "SWO"}),
    "liquidity-risk": frozenset({"SWO", "LP"}),
    "assumption-stress-test": frozenset(LINES),
    "trade-process-review": frozenset({"DT", "SWC"}),
}
_LINE_SECTION = re.compile(r"^### Line (RF|DT|SWC|SWO|LP)\s*$(.*?)(?=^#{1,3} |\Z)", re.S | re.M)
SOURCE_PREFIX = "quant:"
SOURCE_BOOK, SOURCE_CONTEXT = "book", "context"


class RiskError(ValueError):
    """The committee was asked something it cannot do. `code` is stable."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RiskTurn:
    """One turn of the rotation: which round, which seat and which stance."""

    round: int
    seat_code: str
    name: str
    stance: str


def rounds_for(line: str) -> int:
    """The number of rounds the profile of a line asks for."""
    if line not in LINES:
        raise RiskError("bad_line", f"line must be one of {', '.join(LINES)}")
    return LINE_ROUNDS[line]


def rotation(line: str, rounds: int | None = None) -> tuple[RiskTurn, ...]:
    """The turns of the committee: Jake, Glen, Reed in that order, once per round. The last turn is Reed's: he gives the verdict."""
    count = rounds_for(line) if rounds is None else rounds
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_ROUNDS:
        raise RiskError("bad_rounds", f"rounds must be a whole number from 1 to {MAX_ROUNDS}")
    return tuple(RiskTurn(r, code, SEATS[code].name, STANCE_OF[code]) for r in range(1, count + 1) for code in VOICE_ORDER)


# ---- RiskLimits (AH-19) ----------------------------------------------------------------------------------------------

class FundingRules(BaseModel):
    """The rules of a funded account (prop firm): the same keys as `desk_limits.config.funding`."""

    model_config = ConfigDict(extra="forbid")

    daily_loss_usd: float | None = None
    trailing_drawdown_usd: float | None = None
    max_contracts: int | None = None
    consistency_pct: float | None = None


class EventWindowLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match: str
    minutes_before: int = 0
    minutes_after: int = 0


class RiskLimits(BaseModel):
    """The limits of a desk, typed over the keys of `desk_limits.config` (H2). A key that does not exist is rejected; so is a value out
    of range, by the same rules the filter reads. `None` is a limit that is off. Each line fills its own fields: the daily loss, the
    drawdown and the funded account rules for DT, the margin for SWC, the premium for SWO, the weight for LP."""

    model_config = ConfigDict(extra="forbid")

    max_order_pct: float | None = None
    max_daily_loss_pct: float | None = None
    assets_only: bool | None = None
    options_allowed: bool | None = None
    short_allowed: bool | None = None
    extended_hours_allowed: bool | None = None
    max_orders_per_day: int | None = None
    max_position_pct: float | None = None
    cooldown_seconds: int | None = None
    max_drawdown_pct: float | None = None
    capital_floor_usd: float | None = None
    funding: FundingRules | None = None
    max_premium_usd: float | None = None
    max_margin_pct: float | None = None
    max_weight_pct: float | None = None
    event_windows: list[EventWindowLimit] | None = None
    guards: list[str] | None = None
    max_symbol_pct: float | None = None       # SPEC-020: exposure caps per symbol and per sector, and the sector of each root
    max_sector_pct: float | None = None
    sectors: dict[str, str] | None = None
    instructions: list[dict[str, Any]] | None = None       # the rules tab (SPEC-025): the guards ignore them
    proposed: list[dict[str, Any]] | None = None
    dismissed: list[str] | None = None
    rule_sources: dict[str, str] | None = None
    rule_modes: dict[str, dict[str, Any]] | None = None
    daily_loss_steps: list[dict[str, float]] | None = None          # SPEC-026 (A8): a USD that scales with the equity
    drawdown_steps: list[dict[str, float]] | None = None

    @model_validator(mode="after")
    def _in_range(self) -> "RiskLimits":
        try:
            rules.validate_limits(self.to_config())
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self

    def to_config(self) -> dict[str, Any]:
        """What `desk_limits.config` holds: only the limits that are on."""
        out: dict[str, Any] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, FundingRules):
                inner = {k: v for k, v in value.model_dump().items() if v is not None}
                if inner:
                    out[name] = inner
            elif name == "event_windows":
                out[name] = [w.model_dump() for w in value]
            elif name == "guards":
                out[name] = list(value)
            else:
                out[name] = value
        return out

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> "RiskLimits":
        """The typed limits of a stored configuration. An unknown key or a bad value raises RiskError(bad_limits)."""
        try:
            return cls(**dict(config or {}))
        except (ValidationError, TypeError, ValueError) as exc:
            raise RiskError("bad_limits", _first_error(exc)) from exc

    def loosened_against(self, frame: "RiskLimits") -> list[str]:
        """The keys these limits loosen against `frame`: a limit turned off, a number raised or a flag that stops being on."""
        return rules.loosened_limits(frame.to_config(), self.to_config())

    def within(self, frame: "RiskLimits") -> bool:
        return not self.loosened_against(frame)


def _first_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        item = exc.errors()[0]
        where = ".".join(str(p) for p in item.get("loc", ()))
        return f"{where}: {item.get('msg', 'invalid')}"[:300]
    return str(exc)[:300]


@dataclass(frozen=True)
class LimitsOutcome:
    """What happened to a request of limits: `applied`, `asks_owner` (it loosens something, nothing was saved) or `invalid`."""

    status: Literal["applied", "asks_owner", "invalid"]
    limits: Mapping[str, Any]
    loosened: tuple[str, ...] = ()
    message: str = ""


def apply_limit_changes(desk_id: int, changes: Mapping[str, Any], *, db: Database | None = None) -> LimitsOutcome:
    """Tighten the limits of a desk. A change that loosens or turns off any limit saves nothing and asks the owner
    (`set_limits(..., tighten_only=True)`: the comparison is made against the limits saved in the same transaction)."""
    from hokma.broker import service
    from hokma.broker.service import BrokerError

    try:
        typed = RiskLimits.from_config(changes)
    except RiskError as exc:
        return LimitsOutcome("invalid", {}, message=str(exc))
    if not typed.to_config():
        return LimitsOutcome("invalid", {}, message="there is nothing to change")
    try:
        saved = service.set_limits(desk_id, typed.to_config(), db=db, tighten_only=True)
    except BrokerError as exc:
        if getattr(exc, "code", "") == "owner_only":
            current = RiskLimits.from_config(service.get_limits(desk_id, db=db))
            loosened = tuple(rules.loosened_limits(current.to_config(), {**current.to_config(), **typed.to_config()}))
            return LimitsOutcome("asks_owner", current.to_config(), loosened, str(exc))
        return LimitsOutcome("invalid", {}, message=str(exc)[:300])
    return LimitsOutcome("applied", saved if isinstance(saved, Mapping) else {})


# ---- the contract of limits (TA-51) ----------------------------------------------------------------------------------

# The fields every desk of a line must have filled before it trades: the hole that the committee covers.
CONTRACT_FIELDS: Mapping[str, tuple[str, ...]] = {
    "DT": ("funding.daily_loss_usd", "funding.trailing_drawdown_usd", "funding.max_contracts", "max_daily_loss_pct"),
    "SWC": ("max_margin_pct", "max_daily_loss_pct", "max_drawdown_pct"),
    "SWO": ("max_premium_usd", "max_position_pct", "max_daily_loss_pct", "max_drawdown_pct"),
    "LP": ("max_weight_pct", "max_drawdown_pct"),
    "RF": ("max_position_pct", "max_drawdown_pct"),
}


@dataclass(frozen=True)
class LimitsContract:
    """The limits of one desk against what its line needs. `base` is what the owner set; `missing` is what Reed must still fill."""

    desk_id: int
    line: str
    base: Mapping[str, Any]
    required: tuple[str, ...]
    filled: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing


def _has(config: Mapping[str, Any], dotted: str) -> bool:
    head, _, tail = dotted.partition(".")
    value = config.get(head)
    if tail:
        return isinstance(value, Mapping) and value.get(tail) is not None
    return value is not None


def limits_contract(desk_id: int, *, db: Database | None = None) -> LimitsContract:
    """The contract of limits of a desk: the base the owner fixed in `desk_limits` and what its line still lacks. Reed never raises the base."""
    from hokma.broker import service

    database = db or get_database()
    with database.read() as u:
        row = u.session.one("SELECT line FROM desk_profile WHERE desk_id = :d", {"d": desk_id})
    if row is None:
        raise RiskError("no_line", "the desk has no line set")
    line = str(row["line"])
    base = RiskLimits.from_config(service.get_limits(desk_id, db=database)).to_config()
    required = CONTRACT_FIELDS.get(line, ())
    filled = tuple(f for f in required if _has(base, f))
    return LimitsContract(desk_id, line, base, required, filled, tuple(f for f in required if f not in filled))


def fill_contract(desk_id: int, requested: Mapping[str, Any], *, db: Database | None = None) -> LimitsOutcome:
    """Reed fills or tightens the contract. Adding a limit and making one smaller go through; raising or turning off one asks the owner."""
    return apply_limit_changes(desk_id, requested, db=db)


# ---- audit of the clamps (AH-20) -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ClampEvent:
    """One cut the filter made to a proposal: by which guard, which limit, from what quantity to what quantity and why."""

    proposal_id: int
    guard: str
    limit: str
    before: str
    after: str
    why: str
    contract_id: str | None = None

    @classmethod
    def from_record(cls, proposal_id: int, item: Mapping[str, Any], guard: str) -> "ClampEvent":
        return cls(proposal_id, guard, str(item.get("limit", ""))[:60], str(item.get("from_qty", ""))[:40], str(item.get("to_qty", ""))[:40],
                   str(item.get("why", ""))[:400], None if item.get("contract_id") is None else str(item["contract_id"])[:80])


@dataclass(frozen=True)
class RiskResult:
    """The audit of the cuts of a desk: every event, how many proposals were cut and the count by limit."""

    desk_id: int
    proposals: int
    clamped: int
    events: tuple[ClampEvent, ...]
    by_limit: Mapping[str, int] = field(default_factory=dict)

    def to_facts(self) -> dict[str, Any]:
        return {"proposals": self.proposals, "clamped": self.clamped, "by_limit": dict(self.by_limit),
                "events": [{"proposal": e.proposal_id, "guard": e.guard, "limit": e.limit, "before": e.before, "after": e.after, "why": e.why}
                           for e in self.events]}


def _guard_of(limit: str, prefilter_json: Any) -> str:
    if limit == "reduce_only":
        return "reduce_only"
    try:
        guards = (json.loads(prefilter_json) or {}).get("guards", []) if prefilter_json else []
    except (TypeError, ValueError):
        guards = []
    for g in guards:
        if isinstance(g, Mapping) and g.get("verdict") == "clamp" and g.get("guard"):
            return str(g["guard"])
    return "limits"


def audit_clamps(desk_id: int, *, since_id: int = 0, limit: int = 200, db: Database | None = None) -> RiskResult:
    """Read `exec_proposals.clamps_json` of a desk as typed events. `limit` caps the proposals read (the latest ones)."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise RiskError("bad_limit", "limit is a whole number from 1 to 1000")
    with (db or get_database()).read() as u:
        rows = u.session.query("SELECT id, clamps_json, prefilter_json FROM exec_proposals WHERE desk_id = :d AND id > :s ORDER BY id DESC LIMIT :n",
                               {"d": desk_id, "s": since_id, "n": limit})
    events: list[ClampEvent] = []
    clamped = 0
    for row in reversed(rows):
        try:
            items = json.loads(row["clamps_json"]) if row["clamps_json"] else []
        except ValueError:
            items = []
        items = [i for i in items if isinstance(i, Mapping)]
        if items:
            clamped += 1
        events.extend(ClampEvent.from_record(int(row["id"]), i, _guard_of(str(i.get("limit", "")), row["prefilter_json"])) for i in items)
    by_limit: dict[str, int] = {}
    for e in events:
        by_limit[e.limit] = by_limit.get(e.limit, 0) + 1
    return RiskResult(desk_id, len(rows), clamped, tuple(events), by_limit)


# ---- portfolio (TA-31, TA-49) ----------------------------------------------------------------------------------------

class PortfolioState(str, Enum):
    """Three states that are never the same: no portfolio was provided, the portfolio is flat, or it holds positions."""

    NOT_PROVIDED = "not_provided"
    FLAT = "flat"
    WITH_POSITIONS = "with_positions"


def portfolio_state(positions: Sequence[Any] | None, snapshot: Any = None) -> PortfolioState:
    """Without positions from the back office the state is not provided, never flat. Flat needs the back office to say the account is known and empty."""
    if positions is None:
        return PortfolioState.NOT_PROVIDED
    if len(positions) > 0:
        return PortfolioState.WITH_POSITIONS
    missing = dict(getattr(snapshot, "missing", None) or {})
    if snapshot is None or getattr(snapshot, "equity", None) is None or any(k.startswith("account") or k.startswith("unpriced") for k in missing):
        return PortfolioState.NOT_PROVIDED
    return PortfolioState.FLAT


@dataclass(frozen=True)
class PositionRow:
    contract_id: str
    qty: Decimal
    market_value: Decimal | None
    asset_type: str | None
    underlying: str | None
    put_call: str | None
    strike: Decimal | None
    expiration: date | None
    multiplier: Decimal | None


@dataclass(frozen=True)
class ExpirationBucket:
    expiration: date
    positions: int
    contracts: Decimal


@dataclass(frozen=True)
class PortfolioContext:
    """The portfolio the committee reads (SWO variant): positions from the back office, greeks and expirations of the options, and a footprint.

    The greeks come from `options.position_greeks`; when a volatility, a spot or the rate is not provided they are `None` and the reason is in `missing`."""

    state: PortfolioState
    line: str
    as_of: str
    equity: Decimal | None = None
    positions: tuple[PositionRow, ...] = ()
    greeks: Mapping[str, float] | None = None
    worst_case: Mapping[str, float] | None = None
    expirations: tuple[ExpirationBucket, ...] = ()
    exposure_by_underlying: Mapping[str, Decimal] = field(default_factory=dict)
    missing: Mapping[str, str] = field(default_factory=dict)
    account_id: int | None = None                       # the account this context reads (SPEC-027): the equity is the whole account's, the positions are this desk's
    shared_with: tuple[int, ...] = ()                   # the other desks that hold that account
    sections: tuple[Mapping[str, Any], ...] = ()        # one summary per account linked to the desk, marked shared when another desk holds it

    @property
    def footprint(self) -> str:
        """A hash of what the context holds: two contexts with the same positions have the same footprint."""
        body = {"state": self.state.value, "positions": sorted((p.contract_id, str(p.qty)) for p in self.positions)}
        return content_hash(body)

    def to_facts(self) -> dict[str, Any]:
        """The context as plain facts for the prompt. The numbers here are the snapshot the numbers of an output are checked against."""
        return {
            "portfolio_state": self.state.value, "line": self.line, "as_of": self.as_of,
            "equity": None if self.equity is None else str(self.equity), "footprint": self.footprint,
            "positions": [{"contract_id": p.contract_id, "qty": str(p.qty), "market_value": None if p.market_value is None else str(p.market_value),
                           "underlying": p.underlying, "expiration": None if p.expiration is None else p.expiration.isoformat(),
                           "strike": None if p.strike is None else str(p.strike), "put_call": p.put_call,
                           "multiplier": None if p.multiplier is None else str(p.multiplier)} for p in self.positions],
            "greeks": None if self.greeks is None else dict(self.greeks), "worst_case": None if self.worst_case is None else dict(self.worst_case),
            "expirations": [{"expiration": b.expiration.isoformat(), "positions": b.positions, "contracts": str(b.contracts)} for b in self.expirations],
            "exposure_by_underlying": {k: str(v) for k, v in self.exposure_by_underlying.items()},
            "missing": dict(self.missing), **self._account_facts()}

    def _account_facts(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.account_id is not None:
            out.update({"account_id": self.account_id, "account_shared": bool(self.shared_with), "shared_with_desks": list(self.shared_with),
                        "positions_scope": "this desk only; equity is the whole account"})
        if self.sections:
            out["accounts"] = [dict(s) for s in self.sections]
        return out


def _row_of(item: Mapping[str, Any], sheet: Any) -> PositionRow:
    return PositionRow(
        str(item["contract_id"]), Decimal(str(item["qty"])), dec_or_none(item.get("market_value")), None if sheet is None else sheet.asset_type,
        None if sheet is None else (sheet.underlying or sheet.root), None if sheet is None else sheet.put_call, None if sheet is None else sheet.strike,
        None if sheet is None else sheet.expiration, None if sheet is None else sheet.multiplier)


def build_portfolio_context(snapshot: Any, sheets: Mapping[str, Any], *, line: str = "SWO", volatilities: Mapping[str, float] | None = None,
                            spots: Mapping[str, float] | None = None, rate: float | None = None, dividend_yield: float = 0.0,
                            as_of: str | None = None, call: Callable[..., Any] | None = None) -> PortfolioContext:
    """The context of a portfolio from the back office snapshot and the contract sheets.

    `snapshot` is an `AccountSnapshot` or None (no back office: not provided). `volatilities` maps an option contract id to its implied volatility and
    `spots` an underlying to its price; with them (and a rate) the greeks of the options are computed by SVC-options."""
    if line not in LINES:
        raise RiskError("bad_line", f"line must be one of {', '.join(LINES)}")
    stamp = as_of or (getattr(snapshot, "as_of", None) or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    if snapshot is None:
        return PortfolioContext(PortfolioState.NOT_PROVIDED, line, stamp, missing={"portfolio": "no back office snapshot was provided"})
    raw = tuple(getattr(snapshot, "positions", ()) or ())
    state = portfolio_state(raw, snapshot)
    missing: dict[str, str] = dict(getattr(snapshot, "missing", None) or {})
    if state is PortfolioState.NOT_PROVIDED:
        missing.setdefault("portfolio", "the back office could not confirm the positions of the account")
        return PortfolioContext(state, line, stamp, getattr(snapshot, "equity", None), missing=missing)
    rows = tuple(_row_of(item, sheets.get(str(item["contract_id"]))) for item in raw)
    equity = getattr(snapshot, "equity", None)
    exposure: dict[str, Decimal] = {}
    buckets: dict[date, list[PositionRow]] = {}
    for row in rows:
        key = row.underlying or row.contract_id
        if row.market_value is not None:
            exposure[key] = exposure.get(key, ZERO) + abs(row.market_value)
        else:
            missing[f"unpriced:{row.contract_id}"] = "the position has no market value"
        if row.expiration is not None and row.asset_type in ("option", "future_option"):
            buckets.setdefault(row.expiration, []).append(row)
    expirations = tuple(ExpirationBucket(d, len(v), sum((abs(r.qty) for r in v), ZERO)) for d, v in sorted(buckets.items()))
    greeks, worst = _portfolio_greeks(rows, stamp, volatilities, spots, rate, dividend_yield, missing, call or registry.call)
    return PortfolioContext(state, line, stamp, equity, rows, greeks, worst, expirations, exposure, missing)


def _portfolio_greeks(rows: Sequence[PositionRow], as_of: str, vols: Mapping[str, float] | None, spots: Mapping[str, float] | None, rate: float | None,
                      dividend_yield: float, missing: dict[str, str], call: Callable[..., Any]) -> tuple[dict[str, float] | None, dict[str, float] | None]:
    options = [r for r in rows if r.asset_type in ("option", "future_option")]
    if not options:
        return None, None
    if rate is None or not spots or not vols:
        missing["greeks"] = "greeks need the rate, the spot of each underlying and the volatility of each option"
        return None, None
    registry.load_all()
    totals: dict[str, float] = {}
    worst: dict[str, float] | None = None
    by_underlying: dict[str, list[PositionRow]] = {}
    for r in options:
        by_underlying.setdefault(r.underlying or "", []).append(r)
    for underlying, group in by_underlying.items():
        spot = (spots or {}).get(underlying)
        legs = []
        for r in group:
            vol = (vols or {}).get(r.contract_id)
            if spot is None or vol is None or r.multiplier is None or r.strike is None or r.expiration is None or r.put_call is None:
                missing[f"greeks:{r.contract_id}"] = "spot, volatility or the contract sheet is missing"
                legs = []
                break
            legs.append({"instrument": "option", "kind": "call" if r.put_call == "C" else "put", "strike": float(r.strike), "volatility": float(vol),
                         "quantity": float(r.qty), "multiplier": float(r.multiplier), "expiration": r.expiration.isoformat()})
        if not legs:
            return None, None
        result = call("options.position_greeks", legs=legs, spot=float(spot), rate=float(rate), dividend_yield=float(dividend_yield), as_of=as_of[:10])
        value = getattr(result, "value", None)
        if not isinstance(value, Mapping):
            missing[f"greeks:{underlying}"] = "; ".join(getattr(result, "warnings", ()) or ("not computed",))[:200]
            return None, None
        for key, number in value["totals"].items():
            totals[key] = totals.get(key, 0.0) + float(number)
        case = value.get("worst_case")
        if isinstance(case, Mapping) and (worst is None or float(case["pnl_usd"]) < worst["pnl_usd"]):
            worst = {k: float(v) for k, v in case.items()}
    return totals, worst


def _desk_snapshot(database: Database, desk_id: int, account_id: int, prices: Mapping[str, Decimal] | None) -> Any:
    """The snapshot of an account where the money (cash, equity) is the whole account's and the positions are the lots of this desk only:
    two desks in one account do not read each other's positions as their own. A desk that is the only holder sees the same as before."""
    from dataclasses import replace

    from hokma.backoffice import desk_owner, valuation
    from hokma.backoffice import book as book_module
    from hokma.backoffice import snapshot as snapshot_module

    snap = snapshot_module.snapshot(account_id, db=database, prices=prices or {})
    entries = book_module.entries(account_id, db=database)
    with database.read() as u:
        mine = desk_owner.of_desk(u.session, account_id, entries, desk_id)
    valued, _errors = valuation.value_all(book_module.Book(mine).cost_positions().values(), dict(prices or {}))
    rows = tuple({"contract_id": v.contract_id, "qty": str(v.qty), "price": str(v.price), "market_value": str(v.market_value)} for v in valued)
    return replace(snap, positions=rows)


def load_portfolio_context(desk_id: int, sheets: Mapping[str, Any] | None = None, *, line: str = "SWO", db: Database | None = None,
                           prices: Mapping[str, Decimal] | None = None, account_id: int | None = None, strategy_key: str = "principal",
                           **greek_inputs: Any) -> PortfolioContext:
    """The context of the desk portfolio read from the back office (SPEC-027): the account of the strategy (or the one named; the only one when
    the desk has one), with the lots of this desk and the equity of the whole account, plus one section per linked account marked shared when
    another desk holds it. A desk with no account, an unreadable book, or several accounts and none chosen is not provided, never flat."""
    from hokma.backoffice import book as book_module
    from hokma.contracts import discipline

    database = db or get_database()
    try:
        with database.read() as u:
            linked = u.broker.accounts_of_desk(desk_id)
            bound = u.broker.account_of_strategy(desk_id, strategy_key)
            holders = {a.id: [d for d in u.broker.desks_of_account(a.id) if d != desk_id] for a in linked}
        if not linked:
            return build_portfolio_context(None, {}, line=line)
        if account_id is not None:
            account = next((a for a in linked if a.id == account_id), None)
        else:
            account = bound or (linked[0] if len(linked) == 1 else None)
        sections = tuple({"account_id": a.id, "shared": bool(holders[a.id]), "shared_with_desks": holders[a.id], "chosen": account is not None and a.id == account.id}
                         for a in linked)
        if account is None:
            reason = "the account is not one of the desk" if account_id is not None else "the desk has several accounts and the strategy has none"
            return replace_context(build_portfolio_context(None, {}, line=line), missing={"portfolio": reason}, sections=sections)
        snap = _desk_snapshot(database, desk_id, account.id, prices)
        ids = [p["contract_id"] for p in snap.positions]
        found = dict(sheets) if sheets is not None else {k: v for k, v in discipline.load_sheets(ids).items() if v is not None}
    except Exception:                                               # noqa: BLE001  (no back office data: not provided, never flat)
        return build_portfolio_context(None, {}, line=line)
    built = build_portfolio_context(snap, found, line=line, **greek_inputs)
    return replace_context(built, account_id=account.id, shared_with=tuple(holders[account.id]), sections=sections)


def replace_context(ctx: PortfolioContext, **changes: Any) -> PortfolioContext:
    from dataclasses import replace

    return replace(ctx, **changes)


# ---- prompts: the agent file, its skills, the line (TA-10, TA-11, TA-12) ---------------------------------------------

def default_kit_root() -> Path:
    from hokma.kit.integrity import app_root

    return app_root().joinpath(*KIT_PATH)


@dataclass(frozen=True)
class SeatFile:
    seat: Seat
    meta: Mapping[str, Any]
    body: str
    skills: tuple[Skill, ...]


def _body_for_line(body: str, line: str) -> str:
    """The agent text with the sections of the other lines removed: a prompt holds the section of its own line only."""
    return _LINE_SECTION.sub(lambda m: m.group(0) if m.group(1) == line else "", body).strip()


def load_seat_file(seat_code: str, line: str, *, kit_root: Path | None = None,
                   skill_lines: Mapping[str, frozenset[str]] | None = None) -> SeatFile:
    """The agent file of a risk seat and the skills it declares. The file must carry `line`, so a seat written for another line
    never builds a prompt for this one; a skill enters only because the file of the seat declares it."""
    seat = SEATS.get(seat_code)
    if seat is None or seat_code not in VOICE_ORDER:
        raise RiskError("bad_seat", f"{seat_code!r} is not a seat of the risk committee")
    if line not in LINES:
        raise RiskError("bad_line", f"line must be one of {', '.join(LINES)}")
    root = kit_root or default_kit_root()
    path = seat_file(root, seat.slug)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 200_000:
            raise RiskError("agent_missing", f"the agent file of {seat.name} is not installed")
        text = path.read_text(encoding="utf-8")
        meta, body = split_frontmatter(text, str(path))
    except (OSError, UnicodeDecodeError, SkillError) as exc:
        raise RiskError("agent_missing", f"the agent file of {seat.name} cannot be read") from exc
    lines = meta.get("lines")
    if not isinstance(lines, list) or line not in lines:
        raise RiskError("wrong_line", f"the agent file of {seat.name} is not written for a {line} desk")
    declared = meta.get("skills") or []
    if not isinstance(declared, list) or len(set(map(str, declared))) != len(declared):
        raise RiskError("bad_skills", f"the skills of {seat.name} must be a list with no repeats")
    table = SKILL_LINES if skill_lines is None else skill_lines
    skills = []
    for name in declared:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise RiskError("bad_skills", f"{str(name)[:40]!r} is not a skill name")
        if name not in table:
            raise RiskError("skill_line_unknown", f"the skill {name} has no line declared for the committee")
        if line not in table[name]:
            continue                                    # a skill of another line never reaches the prompt of this desk
        spath = root / "skills" / name / "SKILL.md"
        try:
            if spath.is_symlink() or not spath.is_file():
                raise RiskError("skill_missing", f"the skill {name} declared by {seat.name} is not installed")
            skill = parse_skill(spath.read_text(encoding="utf-8"), str(spath))
        except (OSError, UnicodeDecodeError, SkillError) as exc:
            raise RiskError("skill_missing", f"the skill {name} declared by {seat.name} cannot be read") from exc
        if skill.name != name:
            raise RiskError("skill_missing", f"the skill file {name} carries another name")
        skills.append(skill)
    return SeatFile(seat, meta, _body_for_line(body, line), tuple(skills))


ARGUMENT_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["stance", "summary", "points"],
    "properties": {
        "stance": {"type": "string", "enum": ["aggressive", "conservative"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT},
        "points": {"type": "array", "items": {"type": "object", "required": ["claim", "evidence"], "properties": {
            "claim": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT},
            "evidence": {"type": "array", "items": {"type": "object", "required": ["source", "name", "value"], "properties": {
                "source": {"type": "string", "maxLength": 80}, "name": {"type": "string", "maxLength": 120}, "value": {"type": "string", "maxLength": 120}}}}}}},
        "data_gaps": {"type": "array", "items": {"type": "string", "maxLength": 300}}}}
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["risk_level", "summary", "points"],
    "properties": {
        "risk_level": {"type": "string", "enum": ["low", "moderate", "high", "severe"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT},
        "points": ARGUMENT_SCHEMA["properties"]["points"],
        "limits": {"type": "object"},
        "open_questions": {"type": "array", "items": {"type": "string", "maxLength": 300}}}}
_ARGUMENT_KEYS = frozenset(ARGUMENT_SCHEMA["properties"])
_VERDICT_KEYS = frozenset(VERDICT_SCHEMA["properties"])


def answer_format(seat_code: str) -> str:
    schema = VERDICT_SCHEMA if seat_code == ARBITER else ARGUMENT_SCHEMA
    return ("Answer with one JSON object and nothing else, in exactly this shape. Every point needs at least one evidence item whose source is "
            "`quant:<function you called>`, `book` or `context`, and every number you write must be one that a tool returned or that the context holds. "
            "A point with no evidence is rejected.\n" + json.dumps(schema, sort_keys=True))


def system_prompt(seat_code: str, *, line: str, desk_id: int, as_of: datetime, mode: str = "live", language: str | None = None,
                  kit_root: Path | None = None, composer: PromptComposer | None = None,
                  skill_lines: Mapping[str, frozenset[str]] | None = None) -> str:
    """The system prompt of one voice: the rules of the desk by role and line, the agent file, its skills and the answer format."""
    seat_file = load_seat_file(seat_code, line, kit_root=kit_root, skill_lines=skill_lines)
    comp = composer or PromptComposer()
    lacks = ("any fill price, any quote or any figure that was not returned by a tool or given in the context; the current limits of the filter "
             "when they are not in the context")
    base = comp.compose(role="risk", line=line, desk_id=desk_id, as_of=as_of, mode=mode, language=language, lacks=lacks,
                        persona=f"{seat_file.seat.name}, seat {seat_file.seat.code}, voice: {STANCE_OF[seat_code]}.")
    blocks = [base.rstrip("\n"), f"## Agent: {seat_file.seat.name}\n{seat_file.body}"]
    for skill in seat_file.skills:
        do = "\n".join(f"- {x}" for x in skill.patterns)
        never = "\n".join(f"- {x}" for x in skill.avoid)
        blocks.append(f"## Skill: {skill.name}\n{skill.identity}\n\nDo:\n{do}\n\nNever:\n{never}\n\n{skill.body}")
    blocks.append(f"## Answer format\n{answer_format(seat_code)}")
    return "\n\n".join(blocks) + "\n"


# ---- tools by role ---------------------------------------------------------------------------------------------------

def risk_toolbox(seat_code: str, desk_id: int, *, db: Database | None = None, account_snapshot: Callable[[int], Mapping[str, Any]] | None = None) -> Toolbox:
    """The toolbox of one seat: only the tools its role has in the plantel, and `quant_call` limited to the modules of the seat.
    Anything else fails with ToolNotAllowed. `desk_configure` (Reed) can only tighten."""
    seat = SEATS.get(seat_code)
    if seat is None or seat_code not in VOICE_ORDER:
        raise RiskError("bad_seat", f"{seat_code!r} is not a seat of the risk committee")
    modules = frozenset(seat.quant_modules)

    def quant_call(name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(name, str) or "." not in name:
            raise BadInput("name is the registry name of a function, like risk.var")
        if arguments is not None and not isinstance(arguments, Mapping):
            raise BadInput("arguments must be an object")
        registry.load_all()
        spec = registry.describe(name)
        if spec.module not in modules:
            raise ToolFailure("blocked", f"{seat.name} may call the modules {', '.join(sorted(modules))} only, not {spec.module}")
        from hokma.kit.quant_tools import to_input

        kinds = {k: (v.get("type", "") if isinstance(v, dict) else "") for k, v in spec.inputs.items()}
        kwargs = {key: to_input(value, kinds.get(key, "")) for key, value in dict(arguments or {}).items()}
        return registry.call(name, **kwargs).to_dict()

    def book_snapshot(account_id: int | None = None) -> dict[str, Any]:
        if account_snapshot is not None:
            return dict(account_snapshot(desk_id))
        return dict(_read_snapshot(desk_id, db, account_id))

    def desk_configure(limits: Mapping[str, Any]) -> dict[str, Any]:
        outcome = apply_limit_changes(desk_id, limits, db=db)
        return {"status": outcome.status, "limits": dict(outcome.limits), "loosened": list(outcome.loosened), "message": outcome.message}

    catalog = ToolCatalog()
    catalog.register(ToolSpec("quant_call", "Run one registered calculation of your modules with named arguments and get its value, unit, method and warnings.",
                              {"type": "object", "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["name"]},
                              quant_call, example={"name": "risk.var", "arguments": {}}))
    catalog.register(ToolSpec("book_snapshot", "The snapshot of the desk account: cash, positions value, equity, day result and peak of the whole account. A desk with more than one account says account_id. A figure that is not known is null with its reason in missing.",
                              {"type": "object", "properties": {"account_id": {"type": "integer"}}}, book_snapshot, example={}))
    catalog.register(ToolSpec("desk_configure", "Tighten the limits of the desk: add a limit or make a number smaller. A change that loosens a limit saves nothing and asks the owner.",
                              {"type": "object", "properties": {"limits": {"type": "object"}}, "required": ["limits"]}, desk_configure,
                              example={"limits": {}}, mutates=True))
    catalog.assign(seat.slug, seat.tools)
    return catalog.toolbox_for(seat.slug)


def _read_snapshot(desk_id: int, db: Database | None, account_id: int | None = None) -> dict[str, Any]:
    from hokma.backoffice import book as book_module
    from hokma.backoffice import snapshot as snapshot_module
    from hokma.broker import sim_engine

    database = db or get_database()
    with database.read() as u:
        linked = u.broker.accounts_of_desk(desk_id)
        holders = {a.id: [d for d in u.broker.desks_of_account(a.id) if d != desk_id] for a in linked}
    if not linked:
        raise ToolFailure("no_data", "the desk has no account")
    if account_id is None and len(linked) > 1:
        raise ToolFailure("bad_input", f"the desk has {len(linked)} accounts: say account_id ({', '.join(str(a.id) for a in linked)})")
    account = linked[0] if account_id is None else next((a for a in linked if a.id == account_id), None)
    if account is None:
        raise ToolFailure("bad_input", f"account {account_id} is not an account of the desk")
    ids = list(book_module.Book.load(account.id, db=database).cost_positions())
    snap = snapshot_module.snapshot(account.id, db=database, prices=dict(sim_engine.mark_source(ids)))
    keys = ("account_id", "as_of", "cash", "positions_value", "equity", "day_pnl", "peak_equity", "realized_pnl", "total_profit", "open_contracts", "positions", "missing")
    return json.loads(json.dumps({**{k: getattr(snap, k) for k in keys}, "shared_with": holders[account.id], "positions_scope": "account"}, default=str))


# ---- running a voice -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskContext:
    """What the committee reads about one proposal. Every field is a fact the code produced; the agents add none."""

    desk_id: int
    line: str
    symbol: str
    as_of: datetime
    proposal: Mapping[str, Any]
    portfolio: PortfolioContext
    limits: Mapping[str, Any] = field(default_factory=dict)
    clamp_audit: RiskResult | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def facts(self, seat_code: str) -> dict[str, Any]:
        out: dict[str, Any] = {"desk_id": self.desk_id, "line": self.line, "symbol": self.symbol, "as_of": self.as_of.isoformat(),
                               "proposal": dict(self.proposal), "portfolio": self.portfolio.to_facts(), "limits": dict(self.limits)}
        if seat_code == ARBITER and self.clamp_audit is not None:
            out["clamp_audit"] = self.clamp_audit.to_facts()
        out.update(self.extra)
        return out


@dataclass(frozen=True)
class TurnResult:
    """The outcome of one turn. `valid` is False when the output was rejected: `problems` says why and `unverified` lists the numbers with no source."""

    seat_code: str
    output: RiskArgument | RiskVerdict | None
    valid: bool
    text: str
    problems: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()
    tools_called: tuple[str, ...] = ()
    degraded: bool = False


def _evidence(raw: Any) -> tuple[Evidence, ...]:
    return tuple(Evidence(str(e["source"]), str(e["name"]), str(e["value"])) for e in raw)


def _points(raw: Any) -> tuple[RiskPoint, ...]:
    return tuple(RiskPoint(str(p["claim"]), _evidence(p["evidence"])) for p in raw)


def parse_argument(value: Mapping[str, Any]) -> RiskArgument:
    """A voice answer as `RiskArgument`. A key that is not in the schema (a limit, a decision) is rejected."""
    extra = sorted(set(value) - _ARGUMENT_KEYS)
    if extra:
        raise SchemaError("$", f"unknown field {extra[0]!r}: a voice has no limit and no decision")
    validate_schema(value, ARGUMENT_SCHEMA)
    return RiskArgument(value["stance"], str(value["summary"]), _points(value["points"]), tuple(str(x) for x in value.get("data_gaps", ())))


def parse_verdict(value: Mapping[str, Any]) -> RiskVerdict:
    """The arbiter answer as `RiskVerdict`. The limits it asks for must be a typed `RiskLimits`."""
    extra = sorted(set(value) - _VERDICT_KEYS)
    if extra:
        raise SchemaError("$", f"unknown field {extra[0]!r}: the arbiter does not decide the order")
    validate_schema(value, VERDICT_SCHEMA)
    limits = value.get("limits") or {}
    try:
        typed = RiskLimits.from_config(limits).to_config()
    except RiskError as exc:
        raise SchemaError("$.limits", str(exc)) from exc
    return RiskVerdict(value["risk_level"], str(value["summary"]), _points(value["points"]), typed, tuple(str(x) for x in value.get("open_questions", ())))


def _plain(output: Any) -> Any:
    if is_dataclass(output) and not isinstance(output, type):
        return {f.name: _plain(getattr(output, f.name)) for f in fields(output)}
    if isinstance(output, Mapping):
        return {str(k): _plain(v) for k, v in output.items()}
    if isinstance(output, (list, tuple)):
        return [_plain(x) for x in output]
    return output


def check_output(output: RiskArgument | RiskVerdict, *, snapshot: Any, sources: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(problems, unverified numbers) of an output. A point with no evidence, an evidence whose source was not used in this run and a
    number that is in the snapshot of the run (the context and what the tools returned) nowhere are all rejected."""
    problems: list[str] = []
    allowed = set(sources)
    if len(output.points) > MAX_POINTS:
        problems.append("too_many_points")
    for index, point in enumerate(output.points):
        if not point.evidence:
            problems.append(f"objection_without_data:{index}")
        if len(point.evidence) > MAX_EVIDENCE:
            problems.append(f"too_much_evidence:{index}")
        for item in point.evidence:
            if item.source not in allowed:
                problems.append(f"source_not_used:{item.source[:60]}")
    plain = _plain(output)
    if isinstance(output, RiskVerdict):
        # The limits the arbiter asks for are typed configuration, not claims: the code checks their keys and ranges and applies them by tightening
        # only. They need a basis in the points, and every number of the points and the text is checked.
        if plain.pop("limits", None) and not any(p.evidence for p in output.points):
            problems.append("limits_without_basis")
    body = json.dumps(plain, ensure_ascii=False, default=str)
    bad = tuple(c.raw for c in numbers.unverified_numbers(body, snapshot))
    if bad:
        problems.append("unverified_numbers")
    return tuple(dict.fromkeys(problems)), bad


def _tool_data(text: str) -> Any:
    try:
        body = json.loads(text)
    except ValueError:
        return None
    return body.get("data") if isinstance(body, dict) and body.get("ok") else None


def run_turn(seat_code: str, model: ChatModel, ctx: RiskContext, toolbox: Toolbox, *, debate: DebateState | None = None, kit_root: Path | None = None,
             composer: PromptComposer | None = None, language: str | None = None) -> TurnResult:
    """One voice speaks. It may call its tools for up to `MAX_TOOL_ROUNDS` rounds; its answer must parse, carry data and use only numbers the
    context or its tools gave. Anything else is an invalid result with the reasons: the committee goes on without that voice."""
    system = system_prompt(seat_code, line=ctx.line, desk_id=ctx.desk_id, as_of=ctx.as_of, kit_root=kit_root, composer=composer, language=language)
    facts = ctx.facts(seat_code)
    state = debate or new_debate()
    user = (f"Facts of the case (JSON):\n{json.dumps(facts, sort_keys=True, ensure_ascii=False, default=str)}\n\n"
            f"Risk discussion so far (summary): {state['summary'] or '(none)'}\n"
            f"Last answer: {state['last_response'] or '(none)'}")
    messages: list[Message] = [Message("user", user, id="u0")]
    snapshot: list[Any] = [facts]
    sources = {SOURCE_CONTEXT}
    called: list[str] = []
    reply_text = ""
    for round_no in range(MAX_TOOL_ROUNDS + 1):
        offered = tuple(toolbox.schemas()) if round_no < MAX_TOOL_ROUNDS else ()
        reply = model.chat(system, messages, offered)
        calls = reply.tool_calls if offered else ()
        messages.append(Message("assistant", reply.content or "", id=f"a{round_no}", tool_calls=tuple(calls)))
        if not calls:
            reply_text = reply.content or ""
            break
        for call in calls:
            out = toolbox.run(ToolCall(call.id, call.name, dict(call.arguments)), agent=SEATS[seat_code].name)
            messages.append(Message("tool", out, tool_call_id=call.id, name=call.name))
            data = _tool_data(out)
            if data is not None:
                snapshot.append(data)
                called.append(call.name)
                if call.name == "book_snapshot":
                    sources.add(SOURCE_BOOK)
                elif call.name == "quant_call" and isinstance(call.arguments.get("name"), str):
                    sources.add(SOURCE_PREFIX + call.arguments["name"])
    try:
        raw = extract_json(reply_text)
        output: RiskArgument | RiskVerdict = parse_verdict(raw) if seat_code == ARBITER else parse_argument(raw)
    except (LLMParseError, SchemaError, ValueError, KeyError, TypeError) as exc:
        return TurnResult(seat_code, None, False, reply_text.strip()[:MAX_TEXT], (f"unreadable_answer: {str(exc)[:160]}",), (), tuple(called), True)
    problems, unverified = check_output(output, snapshot=snapshot, sources=sources)
    text = numbers.mark_unverified(output.summary, snapshot)
    return TurnResult(seat_code, None if problems else output, not problems, text, problems, unverified, tuple(called))


@dataclass(frozen=True)
class CommitteeResult:
    """The whole discussion: every turn, the arbiter verdict (None when he gave none that validates) and what happened to his limits."""

    turns: tuple[TurnResult, ...]
    debate: DebateState
    verdict: RiskVerdict | None
    limits: LimitsOutcome | None = None


def run_committee(ctx: RiskContext, models: Mapping[str, ChatModel], *, rounds: int | None = None, kit_root: Path | None = None,
                  composer: PromptComposer | None = None, language: str | None = None, db: Database | None = None,
                  toolboxes: Mapping[str, Toolbox] | None = None, apply_limits: bool = True) -> CommitteeResult:
    """Jake, Glen and Reed in the fixed rotation of the line. The verdict is Reed's last valid one; his limits are applied by tightening only.
    The committee never decides the order and never touches a live order: the filter of `hokma.exec` applies the limits."""
    debate = new_debate()
    results: list[TurnResult] = []
    boxes = dict(toolboxes or {})
    for turn in rotation(ctx.line, rounds):
        box = boxes.get(turn.seat_code) or boxes.setdefault(turn.seat_code, risk_toolbox(turn.seat_code, ctx.desk_id, db=db))
        model = models.get(turn.seat_code)
        if model is None:
            raise RiskError("no_model", f"there is no model for {turn.name}")
        result = run_turn(turn.seat_code, model, ctx, box, debate=debate, kit_root=kit_root, composer=composer, language=language)
        results.append(result)
        debate = record_round(debate, turn.stance, result.text or "(no answer)")
    verdict = next((r.output for r in reversed(results) if r.seat_code == ARBITER and r.valid and isinstance(r.output, RiskVerdict)), None)
    outcome = None
    if verdict is not None and verdict.limits and apply_limits:
        outcome = apply_limit_changes(ctx.desk_id, verdict.limits, db=db)
    final = {**debate, "verdict": verdict.summary if verdict else ""}
    return CommitteeResult(tuple(results), final, verdict, outcome)  # type: ignore[arg-type]


# ---- process review (CTS-026) ----------------------------------------------------------------------------------------

BEHAVIOR_TAGS = ("fear_entry", "revenge_trade", "moved_stop_without_rule", "size_creep", "premature_exit")
REVIEW_VERDICTS = ("OK", "WARN", "REVIEW_REQUIRED", "RULE_VIOLATION", "COOL_DOWN")
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["verdict", "adherence", "risk_discipline", "execution_quality", "behavior_tags"],
    "properties": {
        "verdict": {"type": "string", "enum": list(REVIEW_VERDICTS)},
        "adherence": {"type": "string", "maxLength": MAX_TEXT}, "risk_discipline": {"type": "string", "maxLength": MAX_TEXT},
        "execution_quality": {"type": "string", "maxLength": MAX_TEXT},
        "behavior_tags": {"type": "array", "items": {"type": "object", "required": ["tag", "evidence"], "properties": {
            "tag": {"type": "string", "enum": list(BEHAVIOR_TAGS)}, "evidence": {"type": "array", "items": {"type": "string", "maxLength": 40}}}}},
        "temporary_rules": {"type": "array", "items": {"type": "string", "maxLength": 300}}}}


@dataclass(frozen=True)
class BehaviorTag:
    tag: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ProcessReview:
    """A review of the process of one order or of one month. It is consultive: a verdict of COOL_DOWN or RULE_VIOLATION waits for the owner."""

    scope: str
    verdict: str
    adherence: str
    risk_discipline: str
    execution_quality: str
    behavior_tags: tuple[BehaviorTag, ...]
    dropped_tags: tuple[str, ...] = ()
    temporary_rules: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()
    valid: bool = True
    needs_owner: bool = False


def build_order_register(desk_id: int, *, proposal_id: int | None = None, month: str | None = None, db: Database | None = None,
                         sheets: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The register the review stands on: every proposal of the order or of the month with the facts the code derived. Each record has an id
    (`P<id>`) that a behavior tag must cite. The risk of an order is in dollars with the multiplier of the contract sheet, never in R."""
    from hokma.contracts import discipline

    if (proposal_id is None) == (month is None):
        raise RiskError("bad_scope", "give a proposal_id or a month, one of them")
    if month is not None and not _MONTH.match(month):
        raise RiskError("bad_scope", "month is like 2026-03")
    database = db or get_database()
    with database.read() as u:
        if proposal_id is not None:
            rows = u.session.query("SELECT * FROM exec_proposals WHERE desk_id = :d AND id = :i", {"d": desk_id, "i": proposal_id})
        else:
            rows = u.session.query("SELECT * FROM exec_proposals WHERE desk_id = :d AND substr(created_at, 1, 7) = :m ORDER BY id LIMIT 500", {"d": desk_id, "m": month})
        orders = {r["id"]: r for r in u.session.query("SELECT id, response_json FROM broker_orders WHERE desk_id = :d", {"d": desk_id})}
        limits = u.session.one("SELECT config FROM desk_limits WHERE desk_id = :d", {"d": desk_id})
    config = json.loads(limits["config"]) if limits and limits["config"] else {}
    ids = sorted({leg["contract_id"] for r in rows for leg in json.loads(r["proposal_json"]).get("legs", [])})
    found = dict(sheets) if sheets is not None else {k: v for k, v in discipline.load_sheets(ids).items() if v is not None}
    records = []
    for r in rows:
        body = json.loads(r["proposal_json"])
        legs = []
        for leg in body.get("legs", []):
            sheet = found.get(leg["contract_id"])
            legs.append({"contract_id": leg["contract_id"], "side": leg["side"], "effect": leg["effect"], "qty": leg["qty"],
                         "multiplier": None if sheet is None else str(sheet.multiplier)})
        fill = _avg_fill(orders.get(r["broker_order_record_id"]))
        limit_price = dec_or_none(body.get("limit_price"))
        slip = None
        if fill is not None and limit_price is not None and legs and legs[0]["multiplier"] is not None:
            sign = 1 if legs[0]["side"] == "buy" else -1
            slip = (fill - limit_price) * sign * Decimal(legs[0]["qty"]) * Decimal(legs[0]["multiplier"])
        clamps = json.loads(r["clamps_json"]) if r["clamps_json"] else []
        records.append({
            "record": f"P{r['id']}", "created_at": str(r["created_at"]), "origin": r["origin"], "status": r["status"], "code": r["code"],
            "legs": legs, "limit_price": body.get("limit_price"), "stop_loss": body.get("stop_loss"), "take_profit": body.get("take_profit"),
            "risk_usd": body.get("risk_usd"), "avg_fill_price": None if fill is None else str(fill),
            "slippage_usd": None if slip is None else str(slip),
            "adherence": {"has_stop": body.get("stop_loss") is not None, "has_target": body.get("take_profit") is not None,
                          "was_clamped": bool(clamps), "was_refused": r["status"] == "refused", "owner_order": r["origin"] == "chat"},
            "clamps": clamps})
    return {"desk_id": desk_id, "scope": "order" if proposal_id is not None else f"month {month}", "limits": config, "records": records}


def _avg_fill(order: Mapping[str, Any] | None) -> Decimal | None:
    if not order or not order.get("response_json"):
        return None
    try:
        body = json.loads(order["response_json"])
        return dec_or_none(body.get("avg_price") if isinstance(body, dict) else None)
    except (ValueError, TypeError, ArithmeticError):
        return None


def _tag_supported(tag: BehaviorTag, records: Mapping[str, Mapping[str, Any]]) -> bool:
    """A behavior tag stands only on records of the register that are orders of the owner (a bot has no behavior) and, for size creep, on growing quantities."""
    if not tag.evidence or any(e not in records for e in tag.evidence):
        return False
    cited = [records[e] for e in tag.evidence]
    if not all(r["adherence"]["owner_order"] for r in cited):
        return False
    if tag.tag == "size_creep":
        sizes = [Decimal(str(r["legs"][0]["qty"])) for r in cited if r["legs"]]
        return len(sizes) >= 2 and all(a < b for a, b in zip(sizes, sizes[1:]))
    if tag.tag in ("revenge_trade", "moved_stop_without_rule"):
        return len(cited) >= 2
    return True


def process_review(model: ChatModel, desk_id: int, *, proposal_id: int | None = None, month: str | None = None, line: str = "SWO",
                   db: Database | None = None, kit_root: Path | None = None, composer: PromptComposer | None = None,
                   sheets: Mapping[str, Any] | None = None, as_of: datetime | None = None, language: str | None = None) -> ProcessReview:
    """Reed reviews the process of one order or of one month over the register of orders. Adherence, risk discipline and execution quality
    are read from facts the code computed; a behavior tag with no evidence in the register is not emitted. The review advises nothing to buy or sell."""
    register = build_order_register(desk_id, proposal_id=proposal_id, month=month, db=db, sheets=sheets)
    system = system_prompt(ARBITER, line=line, desk_id=desk_id, as_of=as_of or datetime.now(timezone.utc), kit_root=kit_root, composer=composer, language=language)
    if "## Skill: trade-process-review" not in system:
        raise RiskError("not_for_line", f"the process review is not part of a {line} desk")
    system += ("\n## Task: process review\nReview the process of the register below. Answer with one JSON object and nothing else, in exactly this shape: "
               + json.dumps(REVIEW_SCHEMA, sort_keys=True) + "\nA behavior tag needs the record ids that show it; only orders of the owner can carry one. "
               "Every number must be one of the register. You do not advise to buy or sell.")
    user = f"Register (JSON):\n{json.dumps(register, sort_keys=True, ensure_ascii=False, default=str)}"
    reply = model.chat(system, [Message("user", user, id="r0")], ())
    try:
        raw = extract_json(reply.content or "")
        validate_schema(raw, REVIEW_SCHEMA)
    except (LLMParseError, SchemaError, ValueError) as exc:
        raise RiskError("unreadable_review", f"the review could not be read: {str(exc)[:160]}") from exc
    records = {r["record"]: r for r in register["records"]}
    tags = tuple(BehaviorTag(str(t["tag"]), tuple(str(e) for e in t["evidence"])) for t in raw["behavior_tags"])
    kept = tuple(t for t in tags if _tag_supported(t, records))
    dropped = tuple(t.tag for t in tags if t not in kept)
    body = {k: raw[k] for k in ("adherence", "risk_discipline", "execution_quality")} | {"rules": raw.get("temporary_rules", [])}
    bad = tuple(c.raw for c in numbers.unverified_numbers(json.dumps(body, ensure_ascii=False), register))
    verdict = str(raw["verdict"])
    return ProcessReview(register["scope"], verdict, str(raw["adherence"]), str(raw["risk_discipline"]), str(raw["execution_quality"]), kept, dropped,
                         tuple(str(x) for x in raw.get("temporary_rules", ())), bad, not bad, verdict in ("RULE_VIOLATION", "COOL_DOWN", "REVIEW_REQUIRED"))

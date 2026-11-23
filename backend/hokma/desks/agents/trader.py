"""Isaac, the trader of a desk (SPEC-019: A11; TA-09, AH-21, AH-55, OA-51).

Isaac turns the verdict of the judge into one typed proposal. He chooses the structure and its legs from the chain he was given
and writes the thesis. He never writes a number that decides money: the premium comes from the quotes, the size from
desks.trading.sizing and desks.trading.budget, the stop and the objective from desks.trading.exits, all of them with the
multiplier of the contract sheet. A size written by the model, in any field or inside the thesis, refuses the answer. A number of
the thesis that is not in the snapshot the model was given leaves the answer invalid.

He never sends an order. The proposal leaves through the tool exec_propose and nothing else: this module imports nothing that
talks to a broker, and the toolbox of the seat holds exec_propose, exec_status and contract_get only.

Pieces (the prompt of the trader is built by pieces, TA-09) live in kit/template/desk/trader-pieces/: each file names the lines it is
written for, and a piece of another line never enters the prompt of a desk. Funding rules and strategy rules are added from the
desk itself (its limits and its mandate).
"""
from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

from hokma.contracts.identity import ContractSheet, NoContract, parse_canonical
from hokma.desks.core import numbers as numbers_module
from hokma.desks.core.prompts import Piece, PromptComposer, kit_dir
from hokma.desks.core.signal import LINES
from hokma.desks.core.skills import SkillError, split_frontmatter
from hokma.desks.core.tools import ToolCatalog, ToolFailure, ToolSpec, Toolbox
from hokma.desks.plantel import SEATS, TraderPlan
from hokma.desks.swo.schema import (CREDIT_STRUCTURES, DEBIT_STRUCTURES, SHARES_PER_OPTION, STRUCTURES, SWOProposal,
                                    derive_structure)
from hokma.desks.trading import budget as budget_module
from hokma.desks.trading import exits as exits_module
from hokma.desks.trading import sizing as sizing_module
from hokma.exec.money import dec
from hokma.exec.proposal import Leg, ProposalError
from hokma.llm.jsonx import LLMParseError, extract_json

SEAT = SEATS["A11"]
ROLE = "trader"
LINE = "SWO"
COPILOT_LINES = frozenset({"SWC", "SWO", "LP"})          # DT has no copilot: its panel never receives the thesis while it is written
PIECES_FOLDER = "trader-pieces"
PIECE_ORDER = ("sizing", "exits", "precheck", "funding-rules", "strategy-rules")
MAX_THESIS_CHARS = 2000
MAX_VERDICT_CHARS = 6000
MAX_QUOTES = 400
ZERO = Decimal(0)
BPS = Decimal(10_000)
CENT = Decimal("0.01")
DEFAULT_OPTION_TICK = Decimal("0.01")

# What a model must not write. A size, a stop, an objective, a price or a field that the code or the tool sets.
SIZE_KEYS = frozenset({"qty", "quantity", "size", "contracts", "lots", "shares", "position_size", "stop", "stop_loss", "stop_price",
                       "take_profit", "target", "objective", "risk", "risk_usd", "limit_price", "net_premium", "max_loss_usd",
                       "order_type", "tif", "desk_id", "line", "origin", "created_by", "reduce_only", "effect"})
SIZE_IN_TEXT = re.compile(
    r"\b\d[\d.,]*\s*(?:contracts?|lots?|shares?|units?)\b|\b(?:size|sized|qty|quantity)\s*(?:of|is|=|:)?\s*\d|\b(?:buy|sell)\s+\d",
    re.IGNORECASE)
ANSWER_KEYS = frozenset({"action", "structure", "underlying", "expiration", "legs", "stop_basis", "thesis", "reason"})
LEG_KEYS = frozenset({"contract_id", "side"})
RETRYABLE = frozenset({"bad_json", "free_size", "unknown_field", "size_in_text", "unverified_numbers", "bad_answer"})

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["action"],
    "properties": {
        "action": {"type": "string", "enum": ["propose", "stand_aside"]},
        "structure": {"type": "string", "enum": list(STRUCTURES)},
        "underlying": {"type": "string", "minLength": 1, "maxLength": 12},
        "expiration": {"type": "string", "minLength": 10, "maxLength": 10},
        "legs": {"type": "array", "items": {"type": "object", "required": ["contract_id", "side"],
                                             "properties": {"contract_id": {"type": "string", "minLength": 3, "maxLength": 80},
                                                            "side": {"type": "string", "enum": ["buy", "sell"]}}}},
        "stop_basis": {"type": "string", "enum": ["premium", "underlying"]},
        "thesis": {"type": "string", "minLength": 1, "maxLength": MAX_THESIS_CHARS},
        "reason": {"type": "string", "maxLength": 600},
    },
}
OUTPUT_FORMAT = (
    "Answer with one JSON object and nothing else. To propose: "
    '{"action": "propose", "structure": "<name>", "underlying": "<symbol>", "expiration": "YYYY-MM-DD", '
    '"legs": [{"contract_id": "<canonical id from the chain>", "side": "buy" | "sell"}], '
    '"stop_basis": "premium" | "underlying", "thesis": "<why, in the words the owner reads>"}. '
    'To stand aside: {"action": "stand_aside", "reason": "<the blocker or the missing evidence>"}. '
    "Never write a quantity, a stop, an objective, a price or any number that is not in the snapshot: the code computes them."
)


class TraderError(ValueError):
    """The kit of the trader is not complete or the inputs are not acceptable. code is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---- pieces and the prompt ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TraderPiece:
    key: str
    summary: str
    lines: frozenset[str]
    text: str


def load_pieces(line: str, *, root: Path | None = None) -> list[TraderPiece]:
    """The pieces written for a line, in the fixed order. A piece whose file does not name the line is left out: the pieces of
    another line never enter this prompt. A required piece that is missing for the line raises TraderError."""
    if line not in LINES:
        raise TraderError("bad_line", f"line must be one of {LINES}")
    folder = kit_dir(PIECES_FOLDER, root=root)
    found: dict[str, TraderPiece] = {}
    if folder.is_dir():
        for path in sorted(folder.glob("*.md")):
            if path.is_symlink() or path.stat().st_size > 20_000:
                raise TraderError("bad_piece", f"{path.name}: a piece is a plain file of at most 20000 bytes")
            try:
                meta, body = split_frontmatter(path.read_text(encoding="utf-8"), path.name)
            except (SkillError, OSError, UnicodeDecodeError) as exc:
                raise TraderError("bad_piece", str(exc)[:200]) from exc
            lines = meta.get("lines")
            if meta.get("name") != path.stem or not isinstance(lines, list) or not lines or not set(map(str, lines)) <= set(LINES):
                raise TraderError("bad_piece", f"{path.name}: needs a name equal to the file and lines from {LINES}")
            if not body.strip():
                raise TraderError("bad_piece", f"{path.name}: empty piece")
            found[path.stem] = TraderPiece(path.stem, str(meta.get("summary", "")), frozenset(map(str, lines)), body.strip())
    chosen = [found[k] for k in PIECE_ORDER if k in found and line in found[k].lines]
    missing = [k for k in PIECE_ORDER if k not in {p.key for p in chosen}]
    if missing:
        raise TraderError("piece_missing", f"no piece {missing[0]} is written for the line {line}")
    return chosen


def load_persona(*, root: Path | None = None) -> str:
    """The body of the agent file of Isaac: his identity and what he does not do."""
    path = kit_dir("agents", f"{SEAT.slug}.md", root=root)
    try:
        _meta, body = split_frontmatter(path.read_text(encoding="utf-8"), path.name)
    except (SkillError, OSError, UnicodeDecodeError) as exc:
        raise TraderError("agent_missing", f"the agent file of {SEAT.name} cannot be read") from exc
    return body.strip()


def _limits_text(limits: Mapping[str, Any]) -> str:
    rows = []
    funding = limits.get("funding") or {}
    for key in ("daily_loss_usd", "trailing_drawdown_usd", "max_contracts", "consistency_pct"):
        if funding.get(key) is not None:
            rows.append(f"- funding.{key}: {funding[key]}")
    for key in ("max_premium_usd", "max_position_pct", "max_order_pct", "max_drawdown_pct", "capital_floor_usd", "max_orders_per_day"):
        if limits.get(key) is not None:
            rows.append(f"- {key}: {limits[key]}")
    return "\n".join(rows)


def build_system_prompt(*, desk_id: int, as_of: datetime, line: str = LINE, mandate_summary: str = "", limits: Mapping[str, Any] | None = None,
                        premium_selling_open: bool = False, language: str | None = None, mode: str = "live", root: Path | None = None,
                        persona: str | None = None) -> str:
    """The system prompt of the trader of one desk, assembled by pieces for its line (TA-09)."""
    composer = PromptComposer(root=root)
    for piece in load_pieces(line, root=root):
        composer.add_piece(Piece(piece.key, piece.text, roles=frozenset({ROLE}), lines=frozenset({line})))
    limits_block = _limits_text(limits or {})
    composer.add_piece(Piece("desk-limits", "Limits of this desk, set by the committee inside the frame of the owner:\n"
                             + (limits_block or "- none set"), roles=frozenset({ROLE}), lines=frozenset({line}), desk_id=desk_id))
    if mandate_summary.strip():
        composer.add_piece(Piece("desk-mandate", "Mandate of this desk:\n" + mandate_summary.strip()[:4000], roles=frozenset({ROLE}),
                                 lines=frozenset({line}), desk_id=desk_id))
    state = "open" if premium_selling_open else "closed"
    composer.add_piece(Piece("premium-switch", f"The premium selling switch of this desk is {state}."
                             + ("" if premium_selling_open else " Offer only purchases and debit spreads. A credit structure is refused at the door of execution."),
                             roles=frozenset({ROLE}), lines=frozenset({line}), desk_id=desk_id))
    base = composer.compose(role=ROLE, line=line, desk_id=desk_id, as_of=as_of, mode=mode, language=language,
                            persona=load_persona(root=root) if persona is None else persona)
    return f"{base}\n## Answer format\n{OUTPUT_FORMAT}\n"


# ---- the answer of the model ------------------------------------------------------------------------------------------

class TraderAnswerError(ValueError):
    """The answer of the model cannot be used. code is stable: bad_json, free_size, unknown_field, size_in_text, bad_answer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TraderAnswer:
    action: Literal["propose", "stand_aside"]
    structure: str = ""
    underlying: str = ""
    expiration: date | None = None
    legs: tuple[tuple[str, str], ...] = ()          # (contract_id, side): never a quantity
    stop_basis: Literal["premium", "underlying"] = "premium"
    thesis: str = ""
    reason: str = ""


def parse_answer(text: str) -> TraderAnswer:
    """The answer of the model as a typed object. Any size, stop, objective or price in a field, any field the format does not
    have and any size written inside the thesis refuse the whole answer."""
    try:
        data = extract_json(text)
    except LLMParseError as exc:
        raise TraderAnswerError("bad_json", f"the answer is not a JSON object: {exc}") from exc
    sized = sorted(set(data) & SIZE_KEYS)
    for leg in data.get("legs") or ():
        if isinstance(leg, Mapping):
            sized += sorted(set(leg) & SIZE_KEYS)
    if sized:
        raise TraderAnswerError("free_size", f"{sized[0]} was written by the model: sizes, stops, objectives and prices are computed by code")
    extra = sorted(set(data) - ANSWER_KEYS)
    for leg in data.get("legs") or ():
        if isinstance(leg, Mapping):
            extra += sorted(set(leg) - LEG_KEYS)
    if extra:
        raise TraderAnswerError("unknown_field", f"{str(extra[0])[:40]} is not a field of the format")
    action = data.get("action")
    if action not in ("propose", "stand_aside"):
        raise TraderAnswerError("bad_answer", "action is propose or stand_aside")
    if action == "stand_aside":
        reason = data.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise TraderAnswerError("bad_answer", "standing aside needs a reason")
        return TraderAnswer("stand_aside", reason=reason.strip()[:600])
    thesis = data.get("thesis")
    if not isinstance(thesis, str) or not 1 <= len(thesis.strip()) <= MAX_THESIS_CHARS:
        raise TraderAnswerError("bad_answer", f"thesis is text of 1 to {MAX_THESIS_CHARS} characters")
    if SIZE_IN_TEXT.search(thesis):
        raise TraderAnswerError("size_in_text", "the thesis holds a size: sizes are computed by code and never written")
    structure, underlying = data.get("structure"), data.get("underlying")
    if structure not in STRUCTURES or not isinstance(underlying, str) or not underlying.strip():
        raise TraderAnswerError("bad_answer", "a proposal needs a structure from the list and an underlying")
    try:
        expiration = date.fromisoformat(str(data.get("expiration")))
    except ValueError as exc:
        raise TraderAnswerError("bad_answer", "expiration is a date such as 2026-12-18") from exc
    legs = data.get("legs")
    if not isinstance(legs, list) or not 1 <= len(legs) <= 4:
        raise TraderAnswerError("bad_answer", "a proposal has one to four legs")
    parsed: list[tuple[str, str]] = []
    for leg in legs:
        if not isinstance(leg, Mapping) or not isinstance(leg.get("contract_id"), str) or leg.get("side") not in ("buy", "sell"):
            raise TraderAnswerError("bad_answer", "a leg is a contract id and a side")
        parsed.append((leg["contract_id"].strip(), leg["side"]))
    basis = data.get("stop_basis", "premium")
    if basis not in ("premium", "underlying"):
        raise TraderAnswerError("bad_answer", "stop_basis is premium or underlying")
    return TraderAnswer("propose", structure, underlying.strip().upper(), expiration, tuple(parsed), basis, thesis.strip())


# ---- cost of a fill (AH-21: the costs of the broker of the desk) -------------------------------------------------------

@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal

    def __post_init__(self) -> None:
        for name in ("bid", "ask"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise TraderError("bad_quote", f"{name} must be a Decimal of zero or more")
        if self.ask <= 0 or self.bid > self.ask:
            raise TraderError("bad_quote", "a quote needs an ask above zero and a bid not above the ask")


@dataclass(frozen=True)
class BrokerCosts:
    """The commission and the slippage of the broker of the desk. A broker with no figures of its own gets the defaults of the
    simulated broker, so a cost is never counted as zero by omission."""

    commission_type: Literal["bps", "per_share"] = "bps"
    commission_value: Decimal = Decimal(1)
    commission_min: Decimal = ZERO
    slippage_bps: Decimal = Decimal(2)

    @classmethod
    def from_broker(cls, broker: Any) -> "BrokerCosts":
        from hokma.broker import costs as broker_costs

        cfg = getattr(broker, "commission", None) or broker_costs.DEFAULT_COMMISSION
        cfg = broker_costs.validate_commission(cfg)
        slip = getattr(broker, "slippage_bps", None)
        slip = broker_costs.DEFAULT_SLIPPAGE_BPS if slip is None else slip
        return cls(cfg["type"], dec(cfg["value"]), dec(cfg["min"]), dec(slip))

    def fill(self, side: str, reference: Decimal) -> Decimal:
        """Against the trader: a buy fills above the reference, a sell below it. Four decimals."""
        factor = BPS + self.slippage_bps if side == "buy" else BPS - self.slippage_bps
        return (reference * factor / BPS).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)

    def commission(self, notional: Decimal, qty: Decimal) -> Decimal:
        if qty == 0:
            return ZERO
        raw = notional * self.commission_value / BPS if self.commission_type == "bps" else qty * self.commission_value
        return max(raw, self.commission_min).quantize(CENT, rounding=ROUND_HALF_UP)


# ---- build_orders (AH-21) ------------------------------------------------------------------------------------------------

class BuildError(ValueError):
    """The orders cannot be built from the books and the quotes. code is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BuiltLeg:
    leg: Leg
    reference: Decimal            # the price of the real entry: the ask for a buy, the bid for a sell
    fill: Decimal                 # the reference moved by the slippage of the broker
    multiplier: Decimal
    commission: Decimal           # USD
    slippage_usd: Decimal
    is_option: bool


@dataclass(frozen=True)
class BuiltOrders:
    legs: tuple[Leg, ...]
    detail: tuple[BuiltLeg, ...]
    deferred: Mapping[str, Decimal]       # what a flip would still need after the closes (signed), proposed after the fill
    option_net_reference_usd: Decimal     # buys positive, sells negative, options only, at the real entry price
    option_net_fill_usd: Decimal
    outlay_usd: Decimal                   # cash out for every leg at the fill price, shares included, plus commission
    cost_usd: Decimal                     # commission plus slippage of all legs


def build_orders(target: Mapping[str, Decimal], current: Mapping[str, Decimal], *, quotes: Mapping[str, Quote],
                 sheets: Mapping[str, ContractSheet], costs: BrokerCosts) -> BuiltOrders:
    """The difference between the target book and the current book of the back office, as the legs of one order (SWO: options and,
    for a covered call, the share). The reference price of a leg is the price of the real entry at the broker (a buy at the ask, a sell at
    the bid) and the cost counts the commission and the slippage of the broker of the desk. The effect of a leg is read from the current
    book: a leg that reduces a position closes it, and a flip closes first and leaves the rest in `deferred`."""
    legs: list[Leg] = []
    detail: list[BuiltLeg] = []
    deferred: dict[str, Decimal] = {}
    for cid in list(dict.fromkeys([*target, *current])):
        have = Decimal(current.get(cid, ZERO))
        diff = Decimal(target.get(cid, ZERO)) - have
        if diff == 0:
            continue
        if diff != diff.to_integral_value():
            raise BuildError("fractional", f"{cid}: the difference {diff} is not a whole number of contracts or shares")
        side = "buy" if diff > 0 else "sell"
        effect, qty = "open", abs(diff)
        if have != 0 and (have > 0) != (diff > 0):
            effect = "close"
            if abs(diff) > abs(have):
                qty = abs(have)
                deferred[cid] = diff + have
        quote, sheet = quotes.get(cid), sheets.get(cid)
        if quote is None or sheet is None:
            raise BuildError("no_quote", f"{cid} has no quote or no contract sheet in what the desk was given")
        reference = quote.ask if side == "buy" else quote.bid
        if reference <= 0:
            raise BuildError("no_price", f"{cid} has no {'ask' if side == 'buy' else 'bid'} to enter at")
        fill = costs.fill(side, reference)
        mult = sheet.multiplier
        commission = costs.commission(fill * qty * mult, qty)
        slippage = abs(fill - reference) * qty * mult
        legs.append(Leg(cid, side, effect, qty))                                                      # type: ignore[arg-type]
        detail.append(BuiltLeg(legs[-1], reference, fill, mult, commission, slippage, sheet.asset_type in ("option", "future_option")))
    sign = lambda b: 1 if b.leg.side == "buy" else -1                                                 # noqa: E731
    ref_net = sum((sign(b) * b.reference * b.leg.qty * b.multiplier for b in detail if b.is_option), ZERO)
    fill_net = sum((sign(b) * b.fill * b.leg.qty * b.multiplier for b in detail if b.is_option), ZERO)
    outlay = sum((sign(b) * b.fill * b.leg.qty * b.multiplier + b.commission for b in detail), ZERO)
    cost = sum((b.commission + b.slippage_usd for b in detail), ZERO)
    return BuiltOrders(tuple(legs), tuple(detail), deferred, ref_net, fill_net, outlay, cost)


# ---- the context and the plan -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ExitRules:
    """The rules of the exits of the desk. None means the default of desks.trading.exits, marked uncalibrated in the plan."""

    r_multiple: Decimal = exits_module.DEFAULT_R_MULTIPLE
    debit_stop_fraction: Decimal | None = None
    credit_stop_multiple: Decimal | None = None
    credit_profit_fraction: Decimal | None = None


@dataclass(frozen=True)
class TradeContext:
    """Everything the trader reads, from code and from the back office. The model sees a text of it and writes nothing into it."""

    desk_id: int
    underlying: str
    as_of: datetime
    sheets: Mapping[str, ContractSheet]
    quotes: Mapping[str, Quote]
    current_book: Mapping[str, Decimal]
    budget: budget_module.BudgetInputs
    costs: BrokerCosts = field(default_factory=BrokerCosts)
    rules: ExitRules = field(default_factory=ExitRules)
    premium_selling_open: bool = False
    verdict: str = ""
    reports: Mapping[str, str] = field(default_factory=dict)
    mandate_summary: str = ""
    limits: Mapping[str, Any] = field(default_factory=dict)
    underlying_stop: Decimal | None = None          # level of the share, from the indicators module, for a stop on the underlying
    premium_at_stop: Decimal | None = None          # the structure priced at that level by the options module
    agent: str = "trader"

    def __post_init__(self) -> None:
        if not self.underlying or not isinstance(self.underlying, str):
            raise TraderError("bad_context", "the underlying is the symbol of a share")
        if self.as_of.tzinfo is None:
            raise TraderError("bad_context", "as_of must carry a timezone")
        if len(self.quotes) > MAX_QUOTES:
            raise TraderError("bad_context", f"at most {MAX_QUOTES} quotes")


def snapshot_of(ctx: TradeContext) -> dict[str, Any]:
    """What the model was given, as the snapshot the numbers of its answer are checked against."""
    return {"symbol": ctx.underlying, "verdict": ctx.verdict[:MAX_VERDICT_CHARS], "reports": dict(ctx.reports),
            "quotes": {cid: {"bid": str(q.bid), "ask": str(q.ask)} for cid, q in ctx.quotes.items()},
            "limits": _plain(ctx.limits), "mandate": ctx.mandate_summary}


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return str(value) if isinstance(value, Decimal) else value


@dataclass(frozen=True)
class TradeResult:
    status: Literal["proposed", "stand_aside", "no_trade", "blocked_precheck", "invalid_output", "unavailable"]
    code: str
    why: str = ""
    proposal: SWOProposal | None = None
    contracts: int = 0
    detail: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()
    thesis: str = ""

    def to_plan(self) -> TraderPlan:
        """The structured output of the seat (plantel.TraderPlan): the body of exec_propose or None, and the rationale."""
        return TraderPlan(None if self.proposal is None else self.proposal.to_tool_body(), self.thesis or self.why)


def _no(status: str, code: str, why: str, **extra: Any) -> TradeResult:
    return TradeResult(status, code, why, **extra)                                                     # type: ignore[arg-type]


def _round_to(price: Decimal, tick: Decimal, mode: str) -> Decimal:
    return (price / tick).to_integral_value(rounding=mode) * tick


def _unit_legs(answer: TraderAnswer, ctx: TradeContext) -> tuple[dict[str, Decimal], list[Any]]:
    """The signed quantities of one unit of the structure by contract id, checked against the chain the desk was given."""
    target: dict[str, Decimal] = {}
    infos = []
    for cid, side in answer.legs:
        if cid in target:
            raise TraderError("bad_legs", "a contract appears in two legs")
        try:
            parsed = parse_canonical(cid)
        except NoContract as exc:
            raise TraderError("bad_legs", f"{cid[:40]} is not a canonical contract id") from exc
        if (parsed.underlying or parsed.root) != ctx.underlying or answer.underlying != ctx.underlying:
            raise TraderError("bad_legs", f"{cid[:40]} is not on {ctx.underlying}, the symbol of this run")
        if cid not in ctx.quotes or cid not in ctx.sheets:
            raise TraderError("bad_legs", f"{cid[:40]} is not in the chain the desk gave you")
        qty = Decimal(1)
        if parsed.asset_type in ("equity", "etf"):
            qty = Decimal(SHARES_PER_OPTION)
        target[cid] = qty if side == "buy" else -qty
        infos.append(parsed)
    return target, infos


def _structure_legs(unit: Mapping[str, Decimal], units: int) -> tuple[Leg, ...]:
    return tuple(Leg(cid, "buy" if q > 0 else "sell", "open", abs(q) * units) for cid, q in unit.items())


def plan_trade(answer: TraderAnswer, ctx: TradeContext) -> TradeResult:
    """From the answer of the model to a proposal, or to the reason there is none. Size, exits and costs come from desks.trading;
    nothing the model wrote enters them."""
    if answer.action == "stand_aside":
        return _no("stand_aside", "stand_aside", answer.reason)
    unverified = [c.raw for c in numbers_module.unverified_numbers(answer.thesis, snapshot_of(ctx))]
    if unverified:
        return _no("invalid_output", "unverified_numbers", "the thesis holds numbers that are not in the snapshot: " + ", ".join(unverified[:5]),
                   unverified=tuple(unverified), thesis=answer.thesis)
    blockers = budget_module.pretrade_check(ctx.budget)
    if blockers:
        return _no("blocked_precheck", blockers[0].code, "; ".join(b.why for b in blockers), detail={"blockers": [b.code for b in blockers]})
    try:
        unit_target, parsed = _unit_legs(answer, ctx)
        unit_legs = _structure_legs(unit_target, 1)
        derived = derive_structure(unit_legs)
    except (TraderError, ProposalError) as exc:
        return _no("invalid_output", getattr(exc, "code", "bad_legs"), str(exc))
    if derived is None or not (derived == answer.structure or (derived == "short_put" and answer.structure == "cash_secured_put")):
        return _no("invalid_output", "structure_mismatch", f"the legs do not make a {answer.structure}" + ("" if derived is None else f", they make a {derived}"))
    option_legs = [(cid, p) for (cid, _), p in zip(answer.legs, parsed) if p.asset_type == "option"]
    expiration = min(p.expiration for _, p in option_legs)                                              # type: ignore[type-var]
    if expiration != answer.expiration:
        return _no("invalid_output", "expiration_mismatch", "the expiration is the nearest expiration among the option legs")
    mults = {ctx.sheets[cid].multiplier for cid, _ in option_legs}
    if len(mults) != 1:
        return _no("invalid_output", "multiplier_mismatch", "the option legs have different multipliers")
    multiplier = mults.pop()
    current = {k: Decimal(v) for k, v in ctx.current_book.items()}
    desired = {cid: current.get(cid, ZERO) + q for cid, q in unit_target.items()}
    try:
        one = build_orders({**current, **desired}, current, quotes=ctx.quotes, sheets=ctx.sheets, costs=ctx.costs)
    except BuildError as exc:
        return _no("no_trade", exc.code, str(exc))
    if one.deferred or any(leg.effect != "open" for leg in one.legs):
        return _no("no_trade", "touches_position", "the legs would close or flip a position the desk holds: an entry opens new risk only")

    sells_premium = answer.structure in CREDIT_STRUCTURES
    net_ref_unit = one.option_net_reference_usd / multiplier                  # per share of one unit, positive is a debit
    net_fill_unit = one.option_net_fill_usd / multiplier
    ticks = [ctx.sheets[cid].tick_size for cid, _ in option_legs if ctx.sheets[cid].tick_size]
    tick = min(ticks) if ticks else DEFAULT_OPTION_TICK
    if (net_ref_unit > 0) != (answer.structure in DEBIT_STRUCTURES) or net_ref_unit == 0 or (net_fill_unit > 0) != (net_ref_unit > 0):
        return _no("no_trade", "premium_side", f"at the quotes the {answer.structure} is not a {'debit' if answer.structure in DEBIT_STRUCTURES else 'credit'}")
    position = "debit" if net_ref_unit > 0 else "credit"
    entry = _round_to(abs(net_fill_unit), tick, ROUND_CEILING if position == "debit" else ROUND_FLOOR)
    if entry <= 0:
        return _no("no_trade", "premium_side", "the premium rounds to zero at the tick")
    rules = ctx.rules
    try:
        plan = exits_module.swo_exit(position, entry, tick=tick, basis=answer.stop_basis, r_multiple=rules.r_multiple,
                                     stop_fraction=rules.debit_stop_fraction, stop_multiple=rules.credit_stop_multiple,
                                     profit_fraction=rules.credit_profit_fraction, underlying_stop=ctx.underlying_stop,
                                     premium_at_stop=ctx.premium_at_stop)
        sized = sizing_module.contracts(risk_budget_usd=(ctx.budget.base or ZERO) * ctx.budget.risk_pct / Decimal(100), entry=plan.entry,
                                        stop=plan.stop, multiplier=multiplier, tick_size=tick)
    except (exits_module.ExitError, sizing_module.SizingError) as exc:
        return _no("no_trade", exc.code, str(exc))
    if sized.status != "OK":
        return _no("no_trade", sized.code, sized.reason, detail={"plan": _plan_dict(plan)})
    stock_ask = next((ctx.quotes[cid].ask for cid, p in zip((c for c, _ in answer.legs), parsed) if p.asset_type in ("equity", "etf")), None)
    infos = [sizing_module.OptionLegInfo(p.put_call, side, p.strike)                                    # type: ignore[arg-type]
             for (cid, side), p in zip(answer.legs, parsed) if p.asset_type == "option"]
    try:
        max_loss_unit = sizing_module.max_loss_per_unit_usd(answer.structure, infos, net_ref_unit, multiplier, stock_ask)
    except sizing_module.SizingError as exc:
        return _no("no_trade", exc.code, str(exc))
    commission_unit = sum((b.commission for b in one.detail), ZERO)
    stop_risk_unit = sized.risk_per_contract if max_loss_unit is None else min(sized.risk_per_contract, max_loss_unit)
    risk_unit = stop_risk_unit + 2 * commission_unit                                                    # in and out
    gross_buy_unit = sum((b.reference * b.multiplier for b in one.detail if b.is_option and b.leg.side == "buy"), ZERO)
    cash_unit = max(one.outlay_usd, max_loss_unit if max_loss_unit is not None else risk_unit)
    if cash_unit <= 0:
        return _no("no_trade", "bad_quote", "one unit of the structure needs no cash at these quotes: the quotes are not usable")
    result = budget_module.size_by_plan(ctx.budget, risk_per_contract_usd=risk_unit, premium_per_contract_usd=gross_buy_unit,
                                        cash_per_contract_usd=cash_unit)
    detail = {"plan": _plan_dict(plan), "budget": {"limits": dict(result.limits), "binding": result.binding},
              "risk_per_contract_usd": str(risk_unit), "multiplier": str(multiplier), "cost_per_contract_usd": str(one.cost_usd)}
    if result.status != "OK":
        return _no("no_trade", result.code, result.reason, detail=detail)
    n = result.contracts
    final_target = {**current, **{cid: current.get(cid, ZERO) + q * n for cid, q in unit_target.items()}}
    try:
        built = build_orders(final_target, current, quotes=ctx.quotes, sheets=ctx.sheets, costs=ctx.costs)
    except BuildError as exc:
        return _no("no_trade", exc.code, str(exc))
    max_loss = None if max_loss_unit is None else max_loss_unit * n
    covered = answer.structure == "covered_call"                                                         # decision 1264: its exits are orders apart, never native children
    try:
        proposal = SWOProposal(desk_id=ctx.desk_id, line="SWO", origin="desk", legs=built.legs, order_type="limit", limit_price=entry, tif="day",
                               stop_loss=None if covered else plan.stop, take_profit=None if covered else plan.target, risk_usd=risk_unit * n, thesis=answer.thesis, created_by=ctx.agent,
                               structure=answer.structure, expiration=expiration, net_premium=net_ref_unit, max_loss_usd=max_loss,
                               underlying=ctx.underlying)                                               # type: ignore[arg-type]
    except ProposalError as exc:
        return _no("invalid_output", "bad_proposal", str(exc))
    warnings = []
    if sells_premium and not ctx.premium_selling_open:
        warnings.append("premium_selling_closed: the door of execution will refuse this proposal while the switch of the desk is closed")
    if plan.uncalibrated:
        warnings.append("uncalibrated defaults in the exits: " + ", ".join(plan.uncalibrated))
    if covered:
        detail["exits_apart"] = {"stop": str(plan.stop), "target": str(plan.target), "why": "a covered call goes as a plain order without native children; its exits are orders of their own"}
        warnings.append("covered_call: the stop and the target are not attached to the entry; they go as separate orders")
    detail["cost_usd"] = str(built.cost_usd)
    return TradeResult("proposed", "ok", "", proposal, n, detail, tuple(warnings), (), answer.thesis)


def _plan_dict(plan: exits_module.ExitPlan) -> dict[str, Any]:
    return {"entry": str(plan.entry), "stop": str(plan.stop), "target": str(plan.target), "risk_per_unit": str(plan.risk_per_unit),
            "r_multiple": str(plan.r_multiple), "basis": plan.basis, "uncalibrated": list(plan.uncalibrated)}


# ---- the thesis while it is written (AH-55) ------------------------------------------------------------------------------

@dataclass(frozen=True)
class ThesisPart:
    index: int
    text: str
    html: str                       # the same text escaped for a page: nothing a model writes reaches HTML raw


class ThesisDecoder:
    """Reads the JSON answer of the model as it arrives and yields the text of its thesis field piece by piece. It follows the
    structure of the JSON (strings, escapes, depth), so a word thesis inside another field is not taken for the field."""

    def __init__(self) -> None:
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._unicode: list[str] | None = None
        self._pending_high = ""
        self._buffer: list[str] = []
        self._last_string = ""
        self._previous = ""
        self._key = ""
        self._capture = False
        self.done = False

    def feed(self, chunk: str) -> str:
        """The new text of the thesis in this chunk (possibly empty)."""
        out: list[str] = []
        for ch in chunk:
            if self._in_string:
                self._string_char(ch, out)
            elif ch == '"':
                self._in_string, self._buffer = True, []
                self._capture = self._depth == 1 and self._previous == ":" and self._key == "thesis" and not self.done
            elif ch in "{[":
                self._depth += 1
                self._previous = ch
            elif ch in "}]":
                self._depth -= 1
                self._previous = ch
            elif ch == ":":
                self._key, self._previous = (self._last_string if self._depth == 1 else ""), ":"
            elif not ch.isspace():
                self._previous = ch
        return "".join(out)

    def _string_char(self, ch: str, out: list[str]) -> None:
        if self._unicode is not None:
            self._unicode.append(ch)
            if len(self._unicode) == 4:
                code, self._unicode = "".join(self._unicode), None
                try:
                    value = chr(int(code, 16))
                except ValueError:
                    value = "\ufffd"
                self._emit(self._join_surrogate(value), out)
            return
        if self._escape:
            self._escape = False
            if ch == "u":
                self._unicode = []
                return
            self._emit({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "/": "/", "\\": "\\", '"': '"'}.get(ch, ch), out)
        elif ch == "\\":
            self._escape = True
        elif ch == '"':
            self._in_string = False
            self._last_string = "".join(self._buffer)
            self._previous = '"'
            if self._capture:
                self._capture, self.done = False, True
        else:
            self._emit(ch, out)

    def _join_surrogate(self, value: str) -> str:
        if "\ud800" <= value <= "\udbff":
            self._pending_high = value
            return ""
        if "\udc00" <= value <= "\udfff" and self._pending_high:
            pair = (self._pending_high + value).encode("utf-16", "surrogatepass").decode("utf-16", "replace")
            self._pending_high = ""
            return pair
        return value

    def _emit(self, text: str, out: list[str]) -> None:
        self._buffer.append(text)
        if self._capture:
            out.append(text)


@dataclass(frozen=True)
class StreamedAnswer:
    text: str                       # the whole answer of the model
    parts: tuple[ThesisPart, ...]   # the pieces of the thesis delivered to the panel (empty when there is no copilot)
    copilot: bool


def thesis_stream(chunks: Iterable[str], *, line: str, sink: Callable[[ThesisPart], None] | None = None, copilot: bool = True) -> StreamedAnswer:
    """Run the stream of the model: collect the answer and, when the copilot is on for the line, hand every piece of the thesis to the
    panel as it arrives. The copilot is off for DT (and for any line not in COPILOT_LINES) and when the caller turns it off: then the
    answer is collected and the sink is never called. A sink that fails never stops the answer."""
    if line not in LINES:
        raise TraderError("bad_line", f"line must be one of {LINES}")
    active = copilot and line in COPILOT_LINES
    decoder = ThesisDecoder()
    whole: list[str] = []
    parts: list[ThesisPart] = []
    for chunk in chunks:
        if not isinstance(chunk, str):
            continue
        whole.append(chunk)
        piece = decoder.feed(chunk) if active else ""
        if piece:
            part = ThesisPart(len(parts), piece, html.escape(piece, quote=True))
            parts.append(part)
            if sink is not None:
                try:
                    sink(part)
                except Exception:                                                                      # noqa: BLE001 (a panel that fails must not stop the trader)
                    pass
    return StreamedAnswer("".join(whole), tuple(parts), active)


# ---- the model and the run ------------------------------------------------------------------------------------------

class TraderModel(Protocol):
    """What the trader needs from a model: the complete call of hokma.llm; a model that also has stream() feeds the copilot."""

    def complete(self, messages: list[dict], *, schema: dict | None = None, max_tokens: int = 4096) -> Any: ...


def user_prompt(ctx: TradeContext) -> str:
    """What the model reads: the verdict, the reports, the check of the account, the switch and the chain with its quotes."""
    blockers = budget_module.pretrade_check(ctx.budget)
    rows = [f"- {cid}: bid {q.bid} ask {q.ask}" for cid, q in sorted(ctx.quotes.items())]
    reports = "\n".join(f"### {k}\n{v}" for k, v in ctx.reports.items()) or "(none)"
    check = "clear" if not blockers else "; ".join(f"{b.code}: {b.why}" for b in blockers)
    return (f"Symbol: {ctx.underlying}\nCut-off (UTC): {ctx.as_of.astimezone(timezone.utc).isoformat()}\n"
            f"Premium selling switch: {'open' if ctx.premium_selling_open else 'closed'}\nPre-trade check: {check}\n\n"
            f"Verdict of the judge:\n{ctx.verdict[:MAX_VERDICT_CHARS] or '(none)'}\n\nReports:\n{reports}\n\n"
            f"Chain (the only contracts you may name):\n" + ("\n".join(rows) or "(empty)") + "\n")


def _text_of(reply: Any) -> str:
    return reply if isinstance(reply, str) else str(getattr(reply, "text", ""))


def run_trader(model: Any, ctx: TradeContext, *, system: str | None = None, sink: Callable[[ThesisPart], None] | None = None, copilot: bool = True,
               max_attempts: int = 2, language: str | None = None, root: Path | None = None, line: str = LINE) -> TradeResult:
    """One run of Isaac. The answer is asked for, parsed and planned; a refused answer (a size written, a field outside the format,
    numbers that are not in the snapshot) is asked once more with the reason. The first attempt streams the thesis to the sink when the
    model can stream and the copilot is on for the line."""
    prompt = system if system is not None else build_system_prompt(desk_id=ctx.desk_id, as_of=ctx.as_of, line=line, mandate_summary=ctx.mandate_summary,
                                                                   limits=ctx.limits, premium_selling_open=ctx.premium_selling_open,
                                                                   language=language, root=root)
    messages: list[dict] = [{"role": "system", "content": prompt}, {"role": "user", "content": user_prompt(ctx)}]
    result = _no("unavailable", "no_attempt", "the trader did not run")
    for attempt in range(max(1, min(max_attempts, 3))):
        try:
            if attempt == 0 and callable(getattr(model, "stream", None)) and copilot and line in COPILOT_LINES:
                text = thesis_stream(model.stream(messages, schema=ANSWER_SCHEMA), line=line, sink=sink, copilot=True).text
            else:
                text = _text_of(model.complete(messages, schema=ANSWER_SCHEMA))
        except Exception as exc:                                                                       # noqa: BLE001 (a failed call is a result, not a crash)
            return _no("unavailable", "model_failed", f"the model call failed: {type(exc).__name__}")
        try:
            result = plan_trade(parse_answer(text), ctx)
        except TraderAnswerError as exc:
            result = _no("invalid_output", exc.code, str(exc))
        if result.code not in RETRYABLE:
            return result
        messages = [*messages, {"role": "assistant", "content": text[:4000]},
                    {"role": "user", "content": f"Your answer was refused: {result.why} Answer again in the format, without it."}]
    return result


# ---- tools of the seat (OA-51) ------------------------------------------------------------------------------------------

TOOL_SCHEMAS: Mapping[str, tuple[str, Mapping[str, Any]]] = {
    "exec_propose": ("Hand one proposal to the execution door. You only propose: the code applies the limits and the level of the desk decides what follows (1 analyzes, 2 asks the owner, 3 sends under rules the owner approved). The thesis is the message the owner reads.",
                     {"type": "object", "properties": {"desk_id": {"type": "integer"}, "proposal": {"type": "object"}}, "required": ["desk_id", "proposal"]}),
    "exec_status": ("The state of a proposal of your desk: analysis_only, refused, awaiting_owner, sent, filled, rejected_by_broker, expired or canceled.",
                    {"type": "object", "properties": {"proposal_id": {"type": "integer"}}, "required": ["proposal_id"]}),
    "contract_get": ("The contract sheet of an instrument: canonical id, multiplier, tick, expiration, strike, underlying. Never type a multiplier from memory.",
                     {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}),
}


def trader_toolbox(handlers: Mapping[str, Callable[..., Any]], *, audit: Any = None) -> Toolbox:
    """The toolbox of Isaac: exactly the tools of his seat, bound to the handlers given. A handler for any other tool (order_place
    included) is refused when the toolbox is built, and a call to a tool outside the set raises ToolNotAllowed."""
    extra = sorted(set(handlers) - set(SEAT.tools))
    if extra:
        raise TraderError("tool_not_allowed", f"the seat of {SEAT.name} has no tool {extra[0]!r}")
    catalog = ToolCatalog()
    for name in SEAT.tools:
        if name not in handlers:
            raise TraderError("tool_missing", f"no handler for {name}")
        description, schema = TOOL_SCHEMAS[name]
        catalog.register(ToolSpec(name, description, schema, handlers[name], mutates=name == "exec_propose"))
    catalog.assign(SEAT.slug, SEAT.tools)
    return catalog.toolbox_for(SEAT.slug, audit=audit)


_KIND_OF_CODE = {"no_data": "no_data", "unavailable": "unavailable", "stale": "stale", "bad_input": "bad_input", "blocked": "blocked", "refused": "blocked"}


def mcp_handlers(caller: Any) -> dict[str, Callable[..., Any]]:
    """Handlers that reach the tools of hokma through the same door an agent of the app uses, as the agent of this caller. A refusal of
    the door (a limit, the premium switch, a missing contract) comes back as a classified failure with its code in the text."""
    from hokma.kit import hokma_tools

    def make(name: str) -> Callable[..., Any]:
        def call(**arguments: Any) -> Any:
            text, error = hokma_tools.call(name, arguments, caller)
            try:
                body = json.loads(text)
            except ValueError:
                body = {"message": str(text)[:300]}
            if error:
                code = str(body.get("error_code", "bad_input")) if isinstance(body, dict) else "bad_input"
                raise ToolFailure(_KIND_OF_CODE.get(code, "bad_input"), f"{code}: {body.get('message', '') if isinstance(body, dict) else ''}"[:400])
            return body
        return call

    return {name: make(name) for name in SEAT.tools}


@dataclass(frozen=True)
class Delivery:
    status: str                  # analysis_only, awaiting_owner, sent..., or refused / blocked / unavailable
    code: str
    why: str = ""
    proposal_id: int | None = None


def deliver(toolbox: Toolbox, desk_id: int, result: TradeResult) -> Delivery:
    """Hand a proposal to the execution door through exec_propose, the only way out of this module. The thesis travels as the message of
    the proposal. At level 1 the door answers analysis_only and at level 2 awaiting_owner."""
    if result.proposal is None:
        return Delivery("none", result.code, result.why)
    answer = toolbox.call("exec_propose", {"desk_id": desk_id, "proposal": result.proposal.to_tool_body()}, agent=SEAT.slug)
    if not answer.ok:
        return Delivery("refused" if answer.error_kind == "blocked" else answer.error_kind, answer.error_kind, answer.message)
    data = answer.data if isinstance(answer.data, Mapping) else {}
    return Delivery(str(data.get("status", "")), str(data.get("code", "")), str(data.get("why", "")), data.get("proposal_id"))

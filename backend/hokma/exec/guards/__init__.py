"""The guards of the filter before an order (SPEC-018: OA-43). Types and helpers shared by every guard.

A guard reads a `GuardContext` that cannot be changed (a frozen object, read-only mappings and tuples), and answers pass, clamp or
block with a stable code, a sentence for the owner and the numbers it used. The same facts always give the same answer."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal, Protocol

from hokma.backoffice.snapshot import AccountSnapshot
from hokma.contracts.identity import ContractSheet
from hokma.econ_calendar.windows import EventWindow
from hokma.exec.money import ZERO, dec_or_none
from hokma.exec.proposal import Leg, OrderProposal

Level = Literal[1, 2, 3]


def freeze(value: Any) -> Any:
    """Read-only copy: dict to MappingProxyType, list to tuple, recursively."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class GuardContext:
    proposal: OrderProposal
    contracts: Mapping[str, ContractSheet]
    positions: tuple[Any, ...]                 # broker Position objects with contract_id set
    book: AccountSnapshot
    limits: Mapping[str, Any]
    level: Level
    windows: tuple[EventWindow, ...]
    now: datetime
    desk_id: int = 0
    assets: tuple[str, ...] = ()
    capital: Decimal = ZERO
    desk_status: str = "active"
    marks: Mapping[str, Decimal] = field(default_factory=dict)
    state: Mapping[str, Any] = field(default_factory=dict)
    orders_enabled: bool = True
    account_ok: bool = True
    preview: Any = None
    open_orders: tuple[Any, ...] = ()
    orders_today: int = 0

    def __post_init__(self) -> None:
        for name in ("contracts", "limits", "marks", "state"):
            object.__setattr__(self, name, freeze(dict(getattr(self, name))))
        for name in ("positions", "windows", "assets", "open_orders"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True)
class GuardResult:
    guard: str
    verdict: Literal["pass", "clamp", "block"]
    code: str
    why: str
    numbers: Mapping[str, float] = field(default_factory=dict)
    clamped: OrderProposal | None = None
    clamps: tuple[dict[str, Any], ...] = ()          # the record of every cut when the verdict is clamp
    flags: tuple[str, ...] = ()                      # soft conditions that need the owner (verdict pass, code needs_owner)

    def to_json(self) -> dict[str, Any]:
        return {"guard": self.guard, "verdict": self.verdict, "code": self.code, "why": self.why, "numbers": dict(self.numbers)}


class Guard(Protocol):
    name: str
    applies_to_closing: bool

    def check(self, ctx: GuardContext) -> GuardResult: ...


def passed(name: str, code: str = "ok", why: str = "", **numbers: Any) -> GuardResult:
    return GuardResult(name, "pass", code, why, _nums(numbers))


def blocked(name: str, code: str, why: str, **numbers: Any) -> GuardResult:
    return GuardResult(name, "block", code, why, _nums(numbers))


def clamped(name: str, code: str, why: str, proposal: OrderProposal, clamps: Sequence[Mapping[str, Any]] = (), **numbers: Any) -> GuardResult:
    return GuardResult(name, "clamp", code, why, _nums(numbers), proposal, tuple(dict(c) for c in clamps))


def _nums(values: Mapping[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in values.items():
        if isinstance(value, bool) or value is None:
            continue
        out[key] = float(value)
    return out


def root_of(contract_id: str) -> str:
    """The name a list of symbols speaks of: the underlying of an option, the root of a future, the symbol of a share."""
    parts = contract_id.split(":")
    return parts[1].upper() if len(parts) > 1 else contract_id.upper()


def signed(leg: Leg) -> Decimal:
    return leg.qty if leg.side == "buy" else -leg.qty


def position_qty(ctx: GuardContext, contract_id: str) -> Decimal:
    total = ZERO
    for p in ctx.positions:
        if getattr(p, "contract_id", None) == contract_id:
            total += dec_or_none(getattr(p, "qty", 0)) or ZERO
    return total


_DONE = frozenset({"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "REPLACED"})


def _side_of(word: Any) -> str | None:
    text = str(word or "").lower()
    return "buy" if text.startswith("buy") else "sell" if text.startswith("sell") else None


def _contract_of(leg: Mapping[str, Any]) -> str | None:
    from hokma.contracts import symbols

    cid = leg.get("contract_id")
    if cid:
        return str(cid)
    instrument = leg.get("instrument") if isinstance(leg.get("instrument"), Mapping) else {}
    symbol = leg.get("symbol") or instrument.get("symbol")
    try:
        return symbols.from_provider("schwab", symbol) if symbol else None
    except Exception:                                                              # noqa: BLE001  (a symbol with no contract id is unreadable, never skipped)
        return None


def _leg_records(legs: Any, out: list[tuple[str, str, Decimal]]) -> bool:
    """Add (contract, side, quantity) for every leg; False when any leg cannot be read whole."""
    ok = True
    for leg in legs or ():
        if not isinstance(leg, Mapping):
            ok = False
            continue
        cid, side = _contract_of(leg), _side_of(leg.get("side") or leg.get("instruction"))
        qty = Decimal("Infinity") if leg.get("quantityType") == "ALL_SHARES" else dec_or_none(leg.get("quantity", leg.get("qty")))
        if not cid or side is None or qty is None or qty <= 0:
            ok = False
            continue
        out.append((cid, side, qty))
    return ok


def _children(node: Any, out: list[tuple[str, str, Decimal]], depth: int = 0) -> bool:
    """The legs of every child of a Schwab order tree (TRIGGER, OCO), at any depth. False when a node cannot be read whole."""
    ok = True
    for child in (node.get("childOrderStrategies") or ()) if isinstance(node, Mapping) else ():
        if not isinstance(child, Mapping) or depth > 8:
            ok = False
            continue
        if child.get("orderLegCollection"):
            ok = _leg_records(child["orderLegCollection"], out) and ok
        ok = _children(child, out, depth + 1) and ok
    return ok


def scan_orders(open_orders: Sequence[Any]) -> tuple[list[tuple[str, str, Decimal]], bool]:
    """What the live orders of the account still ask to trade, (contract id, side, quantity left) per leg and per child, and whether EVERY live
    order could be read whole. An order that is done is ignored; one with no status counts as live. A live order with a leg whose contract
    cannot be resolved, with no legs, or with a child that cannot be read makes the answer unreadable: the caller then trusts no close."""
    out: list[tuple[str, str, Decimal]] = []
    readable = True
    for o in open_orders or ():
        get = (lambda k, d=None, o=o: o.get(k, d)) if isinstance(o, Mapping) else (lambda k, d=None, o=o: getattr(o, k, d))
        if str(get("status") or "").upper() in _DONE:
            continue
        data = get("data") if isinstance(get("data"), Mapping) else {}
        legs = get("legs") or data.get("orderLegCollection") or ()
        if legs:
            readable = _leg_records(legs, out) and readable
        else:
            single = {"contract_id": get("contract_id"), "symbol": get("symbol"), "side": get("side"),
                      "quantity": (dec_or_none(get("remaining_qty")) if dec_or_none(get("remaining_qty")) is not None
                                   else (dec_or_none(get("qty")) or ZERO) - (dec_or_none(get("filled_qty")) or ZERO))}
            if dec_or_none(single["quantity"]) is not None and single["quantity"] <= 0 and (single["contract_id"] or single["symbol"]):
                continue                                                           # nothing left to trade
            readable = _leg_records([single], out) and readable
        readable = _children(data, out) and readable
    return out, readable


def pending_orders(open_orders: Sequence[Any]) -> list[tuple[str, str, Decimal]]:
    return scan_orders(open_orders)[0]


def _held(positions: Sequence[Any], open_orders: Sequence[Any] = ()) -> dict[str, Decimal]:
    """The quantity of each contract that can still be CLOSED: the position minus what the live orders already take out of it (a sell against a
    long, a buy against a short). Two orders cannot both close the same unit. When a live order cannot be read whole nothing counts as
    closable: every close is then an opening and meets the guards of an opening."""
    held: dict[str, Decimal] = {}
    for p in positions:
        cid = getattr(p, "contract_id", None)
        if cid:
            held[cid] = held.get(cid, ZERO) + (dec_or_none(getattr(p, "qty", 0)) or ZERO)
    records, readable = scan_orders(open_orders)
    if not readable:
        return {cid: ZERO for cid in held}
    for cid, side, qty in records:
        have = held.get(cid, ZERO)
        if have > 0 and side == "sell":
            held[cid] = max(have - qty, ZERO)
        elif have < 0 and side == "buy":
            held[cid] = min(have + qty, ZERO)
    return held


def derived_closing(proposal: OrderProposal, positions: Sequence[Any], open_orders: Sequence[Any] = ()) -> list[bool]:
    """Per leg: does it really reduce what the account holds? Read from the positions NET of the working closing orders, never from the
    declared effect. The quantity left of each contract is carried from leg to leg."""
    left = dict(_held(positions, open_orders))
    out = []
    for leg in proposal.legs:
        have = left.get(leg.contract_id, ZERO)
        reduces = (have > 0 and leg.side == "sell" and leg.qty <= have) or (have < 0 and leg.side == "buy" and leg.qty <= -have)
        if reduces:
            left[leg.contract_id] = have - signed(leg)
        out.append(reduces)
    return out


def normalize_effects(proposal: OrderProposal, positions: Sequence[Any], open_orders: Sequence[Any] = ()) -> OrderProposal:
    """The effect of every leg as the positions (net of the working closing orders) say it is: a leg declared close that does not
    reduce a position is an open."""
    from dataclasses import replace

    flags = derived_closing(proposal, positions, open_orders)
    legs = tuple(replace(g, effect="close" if c else "open") for g, c in zip(proposal.legs, flags))
    return proposal if legs == proposal.legs else replace(proposal, legs=legs)


def is_closing(proposal: OrderProposal, positions: Sequence[Any], open_orders: Sequence[Any] = ()) -> bool:
    """An order that only reduces what the account holds, by the positions (reduce_only and the declared effect are not believed)."""
    return all(derived_closing(proposal, positions, open_orders))


def leg_price(ctx: GuardContext, leg: Leg) -> Decimal | None:
    """The price a leg is worth in: the limit (single leg), the stop, or the mark of the contract. None when there is none."""
    return priced_with_mark(ctx.proposal, leg, ctx.marks)


def priced_with_mark(p: OrderProposal, leg: Leg, marks: Mapping[str, Any]) -> Decimal | None:
    """The value price of a leg: the greater of the price the agent gave (limit or stop, single leg) and the market mark; no mark, no price.
    A far limit price can never make an exposure look smaller than the market says."""
    mark = dec_or_none(marks.get(leg.contract_id))
    if mark is None:
        return None
    given = [x for x in (p.limit_price, p.stop_price) if x is not None] if len(p.legs) == 1 else []
    return max([mark, *given])


def multiplier_of(ctx: GuardContext, contract_id: str) -> Decimal | None:
    sheet = ctx.contracts.get(contract_id)
    return None if sheet is None else sheet.multiplier


def base_of(ctx: GuardContext) -> Decimal | None:
    """What a percentage is a percentage of: the equity of the book, else the capital of the desk."""
    if ctx.book.equity is not None and ctx.book.equity > 0:
        return ctx.book.equity
    return ctx.capital if ctx.capital and ctx.capital > 0 else None

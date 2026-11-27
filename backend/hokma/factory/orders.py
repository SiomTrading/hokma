"""Orders of the factory (SPEC-020: B-04, B-08, B-09, V-09, V-13, V-14).

A strategy only emits `OrderIntent` values. Sizes and stop units are explicit: a bare number is never a stop, and a size
type is converted to whole contracts with the multiplier of the contract sheet and the rounding of `round_qty` (V-07).
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import Enum
from typing import Any, Literal

from hokma.contracts.identity import ContractSheet
from hokma.desks.core.signal import LINES

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop", "stop_limit", "trailing"]
ORDER_TYPES = ("market", "limit", "stop", "stop_limit", "trailing")
_TAG = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_WHOLE_ASSETS = ("future", "option", "bond", "bill")


class BadSize(ValueError):
    code = "bad_size"


class BadUnit(ValueError):
    """A stop or target without an explicit unit, or with a level on the wrong side of the entry."""

    code = "bad_unit"


class BadOrder(ValueError):
    code = "bad_order"


class LoosenStop(ValueError):
    """The stop moved against the position. A stop only tightens."""

    code = "loosen_stop"


class BracketError(RuntimeError):
    code = "bracket_state"


def _dec(value: Any, label: str, error: type[ValueError] = BadSize) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise error(f"{label} must be a number, got {value!r}")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:
        raise error(f"{label} must be a number, got {value!r}") from None
    if not number.is_finite():
        raise error(f"{label} must be finite, got {value!r}")
    return number


# ---- V-13: stop units ----

@dataclass(frozen=True)
class _Unit:
    value: Decimal

    def __post_init__(self) -> None:
        number = _dec(self.value, f"{type(self).__name__} value", BadUnit)
        if number <= 0:
            raise BadUnit(f"{type(self).__name__} needs a value above zero, got {self.value!r}")
        object.__setattr__(self, "value", number)


@dataclass(frozen=True)
class Price(_Unit):
    """An absolute price level."""


@dataclass(frozen=True)
class FixedPrice(Price):
    """An absolute price level that stays where the strategy put it (1.3.0): the stop or target of a strategy that fixes it on the bar of its signal. A `Price` is measured again from the
    price where the order fills; a `FixedPrice` is not. The sizer reads its distance from the close of the signal bar, like any `Price`."""


@dataclass(frozen=True)
class Ticks(_Unit):
    """A distance in ticks of the contract."""


@dataclass(frozen=True)
class ATR(_Unit):
    """A distance as a multiple of the average true range supplied by the runner."""


@dataclass(frozen=True)
class Pct(_Unit):
    """A distance as a percentage of the entry price: 1 means 1 percent."""

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.value >= 100:
            raise BadUnit(f"Pct needs a value below 100, got {self.value}")


Unit = Price | Ticks | ATR | Pct


def _as_unit(value: Any, label: str) -> Unit | None:
    if value is None or isinstance(value, (Price, Ticks, ATR, Pct)):
        return value
    raise BadUnit(f"{label} needs an explicit unit (Price, Ticks, ATR or Pct), got {value!r}")


def stop_units(unit: Unit, *, entry: Decimal, sheet: ContractSheet, atr: Decimal | None = None) -> Decimal:
    """The distance from the entry price in price points. A number without a unit fails (V-13)."""
    unit = _as_unit(unit, "a stop distance")
    if unit is None:
        raise BadUnit("a stop distance needs an explicit unit (Price, Ticks, ATR or Pct)")
    entry = _dec(entry, "entry", BadUnit)
    if entry <= 0:
        raise BadUnit("the entry price must be above zero")
    if isinstance(unit, Price):
        return abs(entry - unit.value)
    if isinstance(unit, Ticks):
        if sheet.tick_size is None or sheet.tick_size <= 0:
            raise BadUnit(f"{sheet.contract_id} has no tick size: use Price, ATR or Pct")
        return unit.value * sheet.tick_size
    if isinstance(unit, Pct):
        return entry * unit.value / Decimal(100)
    if atr is None or atr <= 0:
        raise BadUnit("an ATR distance needs the current ATR value above zero")
    return unit.value * atr


def protective_price(unit: Unit, *, side: Side, kind: Literal["sl", "tp"], entry: Decimal, sheet: ContractSheet,
                     atr: Decimal | None = None) -> Decimal:
    """The price of a stop (sl) or target (tp) of an entry on `side`, on the tick grid, never on the wrong side of the entry."""
    if side not in ("buy", "sell"):
        raise BadOrder("side is buy or sell")
    long = side == "buy"
    distance = stop_units(unit, entry=entry, sheet=sheet, atr=atr)
    below = (kind == "sl") == long
    price = unit.value if isinstance(unit, Price) else (entry - distance if below else entry + distance)
    if sheet.tick_size and sheet.tick_size > 0:
        # a stop rounds away from the entry (more room), a target toward it (the pessimistic side)
        away = kind == "sl"
        mode = ROUND_FLOOR if below == away else ROUND_CEILING
        price = (price / sheet.tick_size).to_integral_value(rounding=mode) * sheet.tick_size
    if (below and price >= entry) or (not below and price <= entry):
        raise BadUnit(f"the {kind} {price} is on the wrong side of the entry {entry} for a {'long' if long else 'short'}")
    return price


# ---- V-09: size vocabulary ----

class SizeType(str, Enum):
    CONTRACTS = "contracts"
    NOTIONAL = "notional"
    PCT_EQUITY = "pct_equity"
    RISK_USD = "risk_usd"
    TARGET_WEIGHT = "target_weight"

    @classmethod
    def parse(cls, value: Any) -> "SizeType":
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            raise BadSize(f"unknown size type {value!r}; use one of {', '.join(t.value for t in cls)}") from None


@dataclass(frozen=True)
class Size:
    kind: SizeType
    value: Decimal

    def __post_init__(self) -> None:
        kind = SizeType.parse(self.kind)
        number = _dec(self.value, "size")
        if number <= 0:
            raise BadSize(f"a size is above zero, got {self.value!r}")
        if kind is SizeType.PCT_EQUITY and number > 100:
            raise BadSize(f"pct_equity is up to 100, got {number}")
        if kind is SizeType.TARGET_WEIGHT and number > 1:
            raise BadSize(f"target_weight is a fraction of the capital up to 1, got {number}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", number)


def to_contracts(size: Size, *, sheet: ContractSheet, price: Decimal, equity: Decimal, stop_distance: Decimal | None = None,
                 mode: str = "integer", lot: Decimal = Decimal(1)) -> tuple[Decimal, tuple[str, ...]]:
    """The size as contracts or units, rounded down by V-07. Returns the quantity and the rounding warnings."""
    from hokma.backtest.spec import SpecError, round_qty   # imported here: the backtest package imports the factory

    price, equity = _dec(price, "price"), _dec(equity, "equity")
    if price <= 0 or equity <= 0:
        raise BadSize("price and equity must be above zero")
    unit_value = price * sheet.multiplier
    if size.kind is SizeType.CONTRACTS:
        if sheet.asset_type in _WHOLE_ASSETS and size.value != size.value.to_integral_value():
            raise BadSize(f"{sheet.asset_type} trades whole contracts, got {size.value}")
        raw = size.value
    elif size.kind is SizeType.NOTIONAL:
        raw = size.value / unit_value
    elif size.kind is SizeType.PCT_EQUITY:
        raw = equity * size.value / Decimal(100) / unit_value
    elif size.kind is SizeType.TARGET_WEIGHT:
        raw = equity * size.value / unit_value
    else:
        if stop_distance is None or stop_distance <= 0:
            raise BadSize("a size by risk needs a stop distance above zero")
        raw = size.value / (stop_distance * sheet.multiplier)
    try:
        return round_qty(raw, sheet, mode=mode, lot=lot)
    except SpecError as exc:
        raise BadSize(str(exc)) from None


# ---- B-04: the intent ----

@dataclass(frozen=True)
class OrderIntent:
    """What a strategy emits; never an engine or broker object."""

    contract_id: str
    side: Side
    size: Size
    order_type: OrderType = "market"
    limit: Decimal | None = None
    stop: Decimal | None = None
    sl: Unit | None = None
    tp: Unit | None = None
    tag: str | None = None
    reduce_only: bool = False

    def __post_init__(self) -> None:
        if not self.contract_id or not isinstance(self.contract_id, str):
            raise BadOrder("an order needs a contract id")
        if self.side not in ("buy", "sell"):
            raise BadOrder(f"side is buy or sell, got {self.side!r}")
        if not isinstance(self.size, Size):
            raise BadSize(f"size must be a Size, got {self.size!r}")
        if self.order_type not in ORDER_TYPES:
            raise BadOrder(f"order_type must be one of {', '.join(ORDER_TYPES)}, got {self.order_type!r}")
        for label in ("limit", "stop"):
            raw = getattr(self, label)
            if raw is not None:
                number = _dec(raw, label, BadOrder)
                if number <= 0:
                    raise BadOrder(f"{label} must be above zero, got {raw!r}")
                object.__setattr__(self, label, number)
        needs_limit = self.order_type in ("limit", "stop_limit")
        needs_stop = self.order_type in ("stop", "stop_limit", "trailing")
        if needs_limit != (self.limit is not None) or needs_stop != (self.stop is not None):
            raise BadOrder(f"a {self.order_type} order {'needs' if needs_limit else 'has no'} limit and "
                           f"{'needs' if needs_stop else 'has no'} stop")
        _as_unit(self.sl, "sl")
        _as_unit(self.tp, "tp")
        if self.tag is not None and not _TAG.match(self.tag):
            raise BadOrder(f"tag must be 1 to 64 letters, digits or _ . : -, got {self.tag!r}")
        if self.reduce_only and (self.sl is not None or self.tp is not None):
            raise BadOrder("a reduce-only order carries no sl or tp")

    @property
    def has_bracket(self) -> bool:
        return self.sl is not None or self.tp is not None


def _line_size(size: Any, line: str) -> Size:
    if isinstance(size, Size):
        return size
    if line not in LINES:
        raise BadOrder(f"line must be one of {', '.join(LINES)}, got {line!r}")
    number = _dec(size, "size")
    if line == "LP":
        return Size(SizeType.TARGET_WEIGHT, number)
    if number != number.to_integral_value():
        raise BadSize(f"{line} trades whole contracts or units, got {number}")
    return Size(SizeType.CONTRACTS, number)


def _make(side: Side, contract_id: str, size: Any, line: str, limit: Any, stop: Any, sl: Any, tp: Any, tag: str | None,
          order_type: str | None, reduce_only: bool) -> OrderIntent:
    kind = order_type or ("stop_limit" if limit is not None and stop is not None else "limit" if limit is not None
                          else "stop" if stop is not None else "market")
    return OrderIntent(contract_id, side, _line_size(size, line), kind, limit, stop, _as_unit(sl, "sl"), _as_unit(tp, "tp"),  # type: ignore[arg-type]
                       tag, reduce_only)


def buy(contract_id: str, size: Any, *, line: str = "DT", limit: Any = None, stop: Any = None, sl: Any = None, tp: Any = None,
        tag: str | None = None, order_type: str | None = None) -> OrderIntent:
    """A buy intent. A plain number is whole contracts (DT, SWC, SWO, RF) or a weight of the capital (LP)."""
    return _make("buy", contract_id, size, line, limit, stop, sl, tp, tag, order_type, False)


def sell(contract_id: str, size: Any, *, line: str = "DT", limit: Any = None, stop: Any = None, sl: Any = None, tp: Any = None,
         tag: str | None = None, order_type: str | None = None) -> OrderIntent:
    """A sell intent, same rules as `buy`."""
    return _make("sell", contract_id, size, line, limit, stop, sl, tp, tag, order_type, False)


def intent_quantity(intent: OrderIntent, *, sheet: ContractSheet, price: Decimal, equity: Decimal, atr: Decimal | None = None,
                    mode: str = "integer") -> tuple[Decimal, tuple[str, ...]]:
    """The quantity of an intent at a reference price: the stop distance of its sl feeds the size by risk."""
    distance = None if intent.sl is None else stop_units(intent.sl, entry=price, sheet=sheet, atr=atr)
    return to_contracts(intent.size, sheet=sheet, price=price, equity=equity, stop_distance=distance, mode=mode)


# ---- B-09: partial close ----

@dataclass(frozen=True)
class Lot:
    qty: Decimal
    price: Decimal


@dataclass(frozen=True)
class Reduction:
    remaining: tuple[Lot, ...]
    closed: tuple[Lot, ...]        # the closed pieces at their entry price
    realized_pnl: Decimal          # gross of costs, in account currency


def reduce(lots: Sequence[Lot], qty: Any, *, exit_price: Decimal, side_held: Side, multiplier: Decimal) -> Reduction:
    """Close `qty` of a position without closing it whole. Lots close first in, first out at their own entry price."""
    quantity = _dec(qty, "qty")
    held = sum((lot.qty for lot in lots), Decimal(0))
    if quantity <= 0:
        raise BadSize(f"a reduction is above zero, got {qty!r}")
    if quantity > held:
        raise BadSize(f"cannot reduce {quantity}: the position holds {held}")
    if side_held not in ("buy", "sell"):
        raise BadOrder("side_held is buy or sell")
    exit_price = _dec(exit_price, "exit_price")
    sign = Decimal(1) if side_held == "buy" else Decimal(-1)
    left, pnl = quantity, Decimal(0)
    remaining: list[Lot] = []
    closed: list[Lot] = []
    for lot in lots:
        take = min(left, lot.qty)
        if take > 0:
            closed.append(Lot(take, lot.price))
            pnl += sign * (exit_price - lot.price) * take * multiplier
            left -= take
        if lot.qty - take > 0:
            remaining.append(Lot(lot.qty - take, lot.price))
    return Reduction(tuple(remaining), tuple(closed), pnl)


def reduce_intent(contract_id: str, side_held: Side, qty: Any, *, tag: str | None = None) -> OrderIntent:
    """The market intent that closes `qty` of a position held on `side_held`: the opposite side, reduce only."""
    if side_held not in ("buy", "sell"):
        raise BadOrder("side_held is buy or sell")
    size = Size(SizeType.CONTRACTS, _dec(qty, "qty"))
    return OrderIntent(contract_id, "sell" if side_held == "buy" else "buy", size, "market", tag=tag, reduce_only=True)


# ---- V-14: moving the protection ----

@dataclass(frozen=True)
class Protection:
    sl: Decimal | None
    tp: Decimal | None


def adjust_protection(side: Side, current: Protection, *, new_sl: Any = None, new_tp: Any = None) -> Protection:
    """The protection after a move. A stop that moves against the position raises LoosenStop (V-14)."""
    if side not in ("buy", "sell"):
        raise BadOrder("side is buy or sell")
    if new_sl is None and new_tp is None:
        raise BadOrder("an adjustment moves the stop, the target or both")
    sl, tp = current.sl, current.tp
    long = side == "buy"
    if new_sl is not None:
        level = _dec(new_sl, "new_sl", BadOrder)
        if sl is not None and ((long and level < sl) or (not long and level > sl)):
            raise LoosenStop(f"the stop cannot move from {sl} to {level}: that is against the {'long' if long else 'short'} position")
        sl = level
    if new_tp is not None:
        tp = _dec(new_tp, "new_tp", BadOrder)
    if sl is not None and tp is not None and ((long and tp <= sl) or (not long and tp >= sl)):
        raise BadOrder(f"the target {tp} must be on the profit side of the stop {sl}")
    return Protection(sl, tp)


# ---- B-08: bracket ----

@dataclass(frozen=True)
class ChildOrder:
    contract_id: str
    side: Side
    kind: Literal["stop", "target"]
    price: Decimal
    qty: Decimal
    group: str                    # one cancels the other: both share the group


@dataclass(frozen=True)
class ChildFill:
    filled: ChildOrder
    cancelled: ChildOrder | None


class Bracket:
    """Stop and target as children of one entry. They exist only after the entry fills; one fill cancels the other."""

    def __init__(self, entry: OrderIntent, *, sheet: ContractSheet, atr: Decimal | None = None) -> None:
        if not entry.has_bracket:
            raise BadOrder("an entry without sl or tp has no bracket")
        if entry.reduce_only:
            raise BadOrder("a reduce-only order has no bracket")
        self.entry = entry
        self._sheet = sheet
        self._atr = atr
        self.state: Literal["pending", "open", "closed", "cancelled"] = "pending"
        self._children: dict[str, ChildOrder] = {}

    @property
    def children(self) -> tuple[ChildOrder, ...]:
        return tuple(self._children.values())

    def fill_entry(self, price: Decimal, qty: Decimal) -> tuple[ChildOrder, ...]:
        """The entry filled at `price`: the children are built from the real fill price, not from the intent."""
        if self.state != "pending":
            raise BracketError(f"the entry cannot fill in state {self.state}")
        exit_side: Side = "sell" if self.entry.side == "buy" else "buy"
        group = f"oco:{self.entry.tag or self.entry.contract_id}"
        for kind, unit, label in (("stop", self.entry.sl, "sl"), ("target", self.entry.tp, "tp")):
            if unit is not None:
                level = protective_price(unit, side=self.entry.side, kind=label, entry=price, sheet=self._sheet, atr=self._atr)  # type: ignore[arg-type]
                self._children[kind] = ChildOrder(self.entry.contract_id, exit_side, kind, level, qty, group)  # type: ignore[arg-type]
        self.state = "open"
        return self.children

    def cancel_entry(self) -> None:
        """The entry did not fill: there are no children to cancel."""
        if self.state != "pending":
            raise BracketError(f"the entry cannot be cancelled in state {self.state}")
        self.state = "cancelled"

    def fill_child(self, kind: Literal["stop", "target"]) -> ChildFill:
        """A child filled: the other one is cancelled in the same bar."""
        if self.state != "open":
            raise BracketError(f"a child cannot fill in state {self.state}")
        if kind not in self._children:
            raise BracketError(f"this bracket has no {kind}")
        filled = self._children[kind]
        other = "target" if kind == "stop" else "stop"
        cancelled = self._children.get(other)
        self.state = "closed"
        return ChildFill(filled, cancelled)

    def protection(self) -> Protection:
        stop, target = self._children.get("stop"), self._children.get("target")
        return Protection(None if stop is None else stop.price, None if target is None else target.price)

    def modify(self, *, new_sl: Any = None, new_tp: Any = None) -> tuple[ChildOrder, ...]:
        """Move the stop and or the target of the open children (V-14). Returns the children that changed."""
        if self.state != "open":
            raise BracketError(f"the protection cannot move in state {self.state}")
        moved = adjust_protection(self.entry.side, self.protection(), new_sl=new_sl, new_tp=new_tp)
        changed: list[ChildOrder] = []
        for kind, level in (("stop", moved.sl), ("target", moved.tp)):
            current = self._children.get(kind)
            if level is not None and (current is None or current.price != level):
                if current is None:
                    raise BracketError(f"this bracket has no {kind} to move")
                self._children[kind] = ChildOrder(current.contract_id, current.side, current.kind, level, current.qty, current.group)
                changed.append(self._children[kind])
        return tuple(changed)


def bracket(entry: OrderIntent, *, sheet: ContractSheet, atr: Decimal | None = None) -> Bracket:
    """The bracket of an entry intent that carries sl and or tp (B-08)."""
    return Bracket(entry, sheet=sheet, atr=atr)

"""Fill rules of the backtest (SPEC-020, section 4.3). What happens to one order against one bar, in pure functions of Decimal.

The rules are Hokma's, measured against the engine defaults: the order decided at the close of bar N fills at the OPEN of bar N+1 plus
one tick against the trader (resolve); when the stop and the target are both inside one bar the stop fills (stop_first); the stop is
measured from the price where the entry filled, never from the close of the entry bar (stop_price); a stop crossed by a gap fills at
the open and never at the price that was waiting (by_order_type). The tick against goes through `hokma.broker.costs.fill_price`, the one
slippage function of Hokma. Nothing here imports the engine: the engine only runs the ticks that `ticks.py` builds."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from hokma.backtest.spec import FillRules, SpecError
from hokma.backtest.ticks import Bar, PositionSide
from hokma.broker import costs

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop", "stop_limit", "trailing_stop", "limit_if_touched"]
ORDER_TYPES: tuple[str, ...] = ("market", "limit", "stop", "stop_limit", "trailing_stop", "limit_if_touched")
Mode = Literal["fast", "ticks"]

# The approved catalog of fill models (N-09). The model that fills at the best price is optimistic and is rejected by FillRules.
MODELS: Mapping[str, str] = {
    "one_tick": "the fill is the price of the rule plus the ticks against the trader, always (the default)",
    "probabilistic": "the ticks against apply on half of the fills, drawn from the seed: only for robustness runs",
}
FORBIDDEN_MODELS: tuple[str, ...] = ("best_price",)
PROBABILISTIC_SLIP_SHARE = 0.5


@dataclass(frozen=True)
class Order:
    type: OrderType
    side: Side
    qty: Decimal = Decimal(1)
    trigger: Decimal | None = None     # stop, stop_limit, limit_if_touched
    limit: Decimal | None = None       # limit, stop_limit, limit_if_touched
    trail: Decimal | None = None       # trailing_stop: distance in price points
    extreme: Decimal | None = None     # trailing_stop: the best price seen before this bar (high for a sell, low for a buy)

    def __post_init__(self) -> None:
        if self.type not in ORDER_TYPES or self.side not in ("buy", "sell"):
            raise SpecError(f"unknown order type or side: {self.type!r} {self.side!r}")
        needs = {"market": (), "limit": ("limit",), "stop": ("trigger",), "stop_limit": ("trigger", "limit"),
                 "trailing_stop": ("trail", "extreme"), "limit_if_touched": ("trigger",)}[self.type]
        missing = [name for name in needs if getattr(self, name) is None]
        if missing or self.qty <= 0:
            raise SpecError(f"a {self.type} order needs {', '.join(needs) or 'a positive quantity'}")


@dataclass(frozen=True)
class Decision:
    """What a strategy decides at the close of a bar: open a position (with optional protective distances in price points) or leave it."""
    action: Literal["enter", "exit", "reduce", "modify"]
    side: PositionSide | None = None
    qty: Decimal = Decimal(1)
    stop_points: Decimal | None = None
    target_points: Decimal | None = None
    limit: Decimal | None = None           # enter: a limit entry, filled when the bar crosses this price (1.2.4)
    valid_bars: int = 1                    # enter with a limit: the bars the order lives, counted from the one that follows the decision
    new_stop: Decimal | None = None        # modify: the price of the live stop from the next bar on
    new_target: Decimal | None = None      # modify: the price of the live target from the next bar on
    stop_at: Decimal | None = None         # enter: the stop as an absolute price fixed on the bar of the signal (1.3.0); stop_points stays as its distance from the signal close
    target_at: Decimal | None = None       # enter: the same for the target

    def __post_init__(self) -> None:
        if self.action == "enter" and (self.side not in ("long", "short") or self.qty <= 0):
            raise SpecError("an entry needs a side and a positive quantity")
        if self.action in ("exit", "reduce", "modify") and (self.stop_points is not None or self.target_points is not None):
            raise SpecError("an exit carries no protective distance")
        if self.action == "reduce" and self.qty <= 0:
            raise SpecError("a partial exit needs a positive quantity")
        if self.action == "modify" and self.new_stop is None and self.new_target is None:
            raise SpecError("a modify moves the stop, the target or both")
        if self.limit is not None and (self.action != "enter" or self.limit <= 0 or self.valid_bars < 1):
            raise SpecError("a limit belongs to an entry, above zero, with a life of one bar or more")
        if self.target_points is not None and self.stop_points is None:
            raise SpecError("a target without a stop is not allowed: every protective order set has its stop")
        if any(p is not None and p <= 0 for p in (self.stop_points, self.target_points)):
            raise SpecError("protective distances are above zero")
        if (self.stop_at is not None or self.target_at is not None) and (self.action != "enter" or self.stop_points is None):
            raise SpecError("an absolute stop or target belongs to an entry that has its stop distance")
        if any(p is not None and p <= 0 for p in (self.stop_at, self.target_at)):
            raise SpecError("absolute protective prices are above zero")

    @property
    def protective(self) -> bool:
        return self.stop_points is not None


@dataclass(frozen=True)
class Resolution:
    status: Literal["filled", "open", "rejected"]
    price: Decimal | None = None
    reason: str = ""


@dataclass(frozen=True)
class ExitChoice:
    kind: Literal["stop", "target"] | None
    ambiguous: bool = False            # stop and target were both inside the bar (counted in the result, B-07)
    at_open: bool = False              # a gap crossed the level: the exit is the open, not the level


def draw(seed: int, index: int, salt: str = "") -> float:
    """A reproducible number in [0, 1) for the order number `index`: the same seed and the same index always give the same number,
    whatever else ran before (a hash, not a shared random state)."""
    digest = hashlib.sha256(f"{int(seed)}:{int(index)}:{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def reject_prob(seed: int, order_index: int, prob: float) -> bool:
    """Whether the broker rejects the order number `order_index` (robustness runs, V-06). 0 never rejects."""
    if not 0.0 <= float(prob) < 1.0:
        raise SpecError("reject_prob is in [0, 1)")
    return prob > 0 and draw(seed, order_index, "reject") < prob


def choose_mode(protective_orders: bool) -> Mode:
    """Strategies with no stop and no target run on the open tick and the bar alone; any protective order needs every tick."""
    return "ticks" if protective_orders else "fast"


def against(side: Side, price: Decimal, ticks: int, tick_size: Decimal) -> Decimal:
    """`price` moved `ticks` against the trader, through the one slippage function of Hokma."""
    moved = costs.fill_price(side, float(price), ticks=ticks, tick=float(tick_size))
    return Decimal(repr(moved))


def _ticks_against(rules: FillRules, seed: int, index: int) -> int:
    if rules.model == "probabilistic":
        return rules.next_open_ticks if draw(seed, index, "slip") < PROBABILISTIC_SLIP_SHARE else 0
    return rules.next_open_ticks


def stop_price(side: PositionSide, fill_ref: Decimal, distance: Decimal) -> Decimal:
    """The stop of a position, `distance` price points from `fill_ref`: the price where the entry filled (the open of the entry bar,
    before the ticks against). It is never measured from the close of the entry bar: that moves the stop by the whole move of the bar."""
    if distance <= 0:
        raise SpecError("a stop distance is above zero")
    return fill_ref - distance if side == "long" else fill_ref + distance


def target_price(side: PositionSide, fill_ref: Decimal, distance: Decimal) -> Decimal:
    """The objective of a position: the mirror of the stop, on the winning side of `fill_ref`."""
    if distance <= 0:
        raise SpecError("a target distance is above zero")
    return fill_ref + distance if side == "long" else fill_ref - distance


def limit_fill(side: PositionSide, limit: Decimal, bar: Bar) -> tuple[Decimal, bool] | None:
    """Whether a limit entry fills in `bar`: (price, at_open). A long fills when the low reaches the limit, a short when the high does; at the
    limit, or at the open when the bar opens through it (a gap in favour). None when the bar does not cross it."""
    long = side == "long"
    if bar.open <= limit if long else bar.open >= limit:
        return bar.open, True
    return (limit, False) if (bar.low <= limit if long else bar.high >= limit) else None


def stop_first(position: PositionSide, stop: Decimal, target: Decimal | None, bar: Bar) -> ExitChoice:
    """Which protective order fills in `bar`. A gap through a level fills that level at the open; when stop and target are both inside the
    range the stop fills, whichever the data could say (the pessimistic rule, B-06); otherwise the one that was touched."""
    long = position == "long"
    gap_stop = bar.open <= stop if long else bar.open >= stop
    gap_target = target is not None and (bar.open >= target if long else bar.open <= target)
    hit_stop = bar.low <= stop if long else bar.high >= stop
    hit_target = target is not None and (bar.high >= target if long else bar.low <= target)
    if gap_stop:
        return ExitChoice("stop", at_open=True)
    if gap_target:
        return ExitChoice("target", at_open=True)
    if hit_stop and hit_target:
        return ExitChoice("stop", ambiguous=True)
    if hit_stop:
        return ExitChoice("stop")
    return ExitChoice("target") if hit_target else ExitChoice(None)


def _reaches(rises: bool, level: Decimal, bar: Bar) -> tuple[bool, bool]:
    """(the open is already through the level, the range touches the level)."""
    return (bar.open >= level, bar.high >= level) if rises else (bar.open <= level, bar.low <= level)


def _within(side: Side, price: Decimal, limit: Decimal) -> bool:
    return price <= limit if side == "buy" else price >= limit


def by_order_type(order: Order, bar: Bar, rules: FillRules, tick_size: Decimal, *, seed: int = 0, index: int = 0) -> Resolution:
    """The rule of each order type against `bar`. Market, stop and trailing stop pay the ticks against; a limit never fills worse than
    its price and pays none. A level crossed by a gap fills at the OPEN (L-13). A limit fills when the bar trades through its price."""
    buy = order.side == "buy"
    ticks = _ticks_against(rules, seed, index)
    if order.type == "market":
        return Resolution("filled", against(order.side, bar.open, ticks, tick_size), "next_open")
    if order.type == "limit":
        limit = order.limit
        if _within(order.side, bar.open, limit):
            return Resolution("filled", bar.open, "open_through_limit")
        traded_through = bar.low < limit if buy else bar.high > limit
        return Resolution("filled", limit, "limit") if traded_through else Resolution("open")
    if order.type == "trailing_stop":
        level = order.extreme + order.trail if buy else order.extreme - order.trail
        order = Order("stop", order.side, order.qty, trigger=level)
    trigger = order.trigger
    rises = buy if order.type in ("stop", "stop_limit") else not buy        # a buy stop and a sell touch wait for a rise
    gap, hit = _reaches(rises, trigger, bar)
    if not gap and not hit:
        return Resolution("open")
    at = bar.open if gap else trigger
    reason = "gap_at_open" if gap else "trigger"
    if order.limit is not None:                                              # stop_limit, limit_if_touched with a limit
        return Resolution("filled", at, reason) if _within(order.side, at, order.limit) else Resolution("open", reason="beyond_limit")
    return Resolution("filled", against(order.side, at, ticks, tick_size), reason)


def resolve(order: Order, bar: Bar, rules: FillRules, tick_size: Decimal, *, seed: int = 0, index: int = 0) -> Resolution:
    """An order decided at the close of the bar before `bar` and sent at its open: rejected by the seeded draw of `reject_prob`, or
    resolved by its type. A market order fills at the open of `bar` plus the ticks against."""
    if reject_prob(seed, index, rules.reject_prob):
        return Resolution("rejected", reason="reject_prob")
    return by_order_type(order, bar, rules, tick_size, seed=seed, index=index)

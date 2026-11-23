"""The fill engine of the simulated broker (SPEC-018: AH-39, decision 1215). The broker of Hokma trades a real market with a real cost:
a market order fills at the price of the stream (a buy at the ask, a sell at the bid) worse by the slippage of the broker and
with its commission; a limit order that does not cross waits and fills when the stream crosses it. It has no book depth, so an
order fills whole at the best price of the moment. Without a fresh quote the order is refused (`no_market_price`): a price is never made up.

Prices arrive by subscription: whoever owns the stream calls `QuoteBoard.feed` on every update and the engine fills the orders that wait
(`process`). Nothing here asks the market in a loop. `hokma.market` is not touched: `quote_source` is the one place that reads it."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from hokma.broker import costs
from hokma.exec.money import ZERO, dec, round_to_tick

DEFAULT_MAX_QUOTE_AGE = 15.0           # seconds: an older quote is not a price


@dataclass(frozen=True)
class Quote:
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    ts: float                          # seconds since the epoch

    def side_price(self, side: str) -> Decimal | None:
        """What a market order of this side meets: a buy the ask, a sell the bid; the last trade when the stream gave no book."""
        price = self.ask if side == "buy" else self.bid
        return price if price is not None else self.last

    def mark(self) -> Decimal | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.last if self.last is not None else (self.bid if self.bid is not None else self.ask)


class QuoteSource(Protocol):
    def quote(self, contract_id: str) -> Quote | None: ...


class QuoteBoard:
    """The last quote of each contract, fed by the stream. Thread safe. `listeners` are called on every `feed` (the engine of waiting orders)."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._quotes: dict[str, Quote] = {}
        self._lock = threading.Lock()
        self.clock = clock
        self.listeners: list[Callable[[str, Quote], None]] = []

    def feed(self, contract_id: str, *, bid: Any = None, ask: Any = None, last: Any = None, ts: float | None = None) -> Quote:
        quote = Quote(None if bid is None else dec(bid), None if ask is None else dec(ask), None if last is None else dec(last),
                      self.clock() if ts is None else float(ts))
        with self._lock:
            self._quotes[contract_id] = quote
            listeners = list(self.listeners)
        for listener in listeners:
            listener(contract_id, quote)
        return quote

    def quote(self, contract_id: str) -> Quote | None:
        with self._lock:
            return self._quotes.get(contract_id)


_board = QuoteBoard()
_source: QuoteSource | None = None


def board() -> QuoteBoard:
    return _board


def set_quote_source(source: QuoteSource | None) -> None:
    """Tests, and the stream of `hokma.market` when it is wired: where the prices come from. None goes back to the board."""
    global _source
    _source = source


def quote_source() -> QuoteSource:
    return _source or _board


def mark_source(contract_ids: list[str]) -> Mapping[str, Decimal]:
    """The marks of some contracts for the filter and the snapshot: the mid of the fresh quotes."""
    out: dict[str, Decimal] = {}
    src = quote_source()
    for cid in contract_ids:
        q = src.quote(cid)
        if q is not None and q.mark() is not None:
            out[cid] = q.mark()
    return out


class Refused(Exception):
    """The simulated broker refuses the order. `code` is stable (no_market_price, insufficient_cash, bad_order)."""

    def __init__(self, code: str, why: str) -> None:
        super().__init__(code)
        self.code, self.why = code, why


@dataclass(frozen=True)
class LegFill:
    contract_id: str
    side: str
    qty: Decimal
    price: Decimal
    commission: Decimal
    multiplier: Decimal
    quote: Quote


def fresh(quote: Quote | None, now: float, max_age: float) -> bool:
    return quote is not None and now - quote.ts <= max_age and quote.side_price("buy") is not None


def slipped(side: str, price: Decimal, slippage_bps: float, tick: Decimal | None) -> Decimal:
    """Worse for the trader by the slippage, then on the grid of the tick (a buy rounds up, a sell down)."""
    bps = Decimal(str(slippage_bps)) / Decimal(10_000)
    raw = price * (1 + bps) if side == "buy" else price * (1 - bps)
    return round_to_tick(raw, tick, direction="up" if side == "buy" else "down")


def leg_commission(price: Decimal, qty: Decimal, multiplier: Decimal, cfg: Mapping[str, Any]) -> Decimal:
    value = costs.commission(float(price * qty * multiplier), float(qty), cfg)
    return Decimal(str(value))


def crosses(side: str, order_type: str, limit: Decimal | None, stop: Decimal | None, quote: Quote) -> bool:
    """Whether the quote lets a waiting order fill: a buy limit at or above the ask, a sell limit at or under the bid, a stop when the last or
    the side price reaches it."""
    ref = quote.side_price(side)
    if ref is None:
        return False
    if order_type == "market":
        return True
    if order_type == "limit":
        return limit is not None and (ref <= limit if side == "buy" else ref >= limit)
    probe = quote.last if quote.last is not None else ref
    triggered = stop is not None and (probe >= stop if side == "buy" else probe <= stop)
    if order_type == "stop":
        return triggered
    return triggered and limit is not None and (ref <= limit if side == "buy" else ref >= limit)


def fill_leg(leg: Mapping[str, Any], *, order_type: str, limit: Decimal | None, stop: Decimal | None, quote: Quote, slippage_bps: float,
             commission_cfg: Mapping[str, Any]) -> LegFill:
    side, qty = leg["side"], dec(leg["qty"], "qty")
    multiplier = dec(leg["multiplier"], "multiplier")
    tick = dec(leg["tick_size"]) if leg.get("tick_size") else None
    ref = quote.side_price(side)
    if ref is None:
        raise Refused("no_market_price", f"{leg['contract_id']} has no price in the stream")
    price = slipped(side, ref, slippage_bps, tick)
    if order_type in ("limit", "stop_limit") and limit is not None:          # never worse than the limit
        price = min(price, limit) if side == "buy" else max(price, limit)
    commission = leg_commission(price, qty, multiplier, commission_cfg)
    return LegFill(leg["contract_id"], side, qty, price, commission, multiplier, quote)


def cash_needed(fills: list[LegFill], assets: Mapping[str, str]) -> Decimal | None:
    """Cash a set of fills takes from the account: buys of shares, options and crypto, plus their commission. A future is margin, which is not
    modeled, so a set made only of futures needs no cash check (None)."""
    need, counted = ZERO, False
    for f in fills:
        if assets.get(f.contract_id, "equity") == "future":
            continue
        counted = True
        cash = f.qty * f.price * f.multiplier
        need += (cash if f.side == "buy" else -cash) + f.commission
    return need if counted else None

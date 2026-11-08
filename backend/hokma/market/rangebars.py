"""Range bars: exact (from ticks) and approximate (from 1-minute candles).

Rule set (SPEC-001 addendum 4.3), with range R in price:
 1. A bar opens at the price of its first tick.
 2. Each tick updates high, low and close while `high - low <= R` (a tick that lands exactly on the edge stays).
 3. A tick that would make `high - low > R` closes the bar exactly at its edge (`low + R` going up, `high - R`
    going down). The bar is final.
 4. The next bar opens one tick beyond that edge in the direction of the move (price-only range with no known
    tick size: at the edge itself).
 5. If the price jumped more than one range, the complete intermediate bars of size R are created until the bar
    that holds the price.
 6. Volume is the sum of the tick sizes of the bar.

Decisions that the rules leave open:
 - A tick without size (size None, 0, negative or not finite) adds 0 to the volume but its PRICE still counts: it
   moves high, low and close and can close bars. (Coinbase `ticker` messages carry a real last price and no size.)
 - The size of a tick that closes bars goes to the LAST bar it touches (the one that holds its price). Closed and
   intermediate bars keep the volume they had; intermediate bars have volume 0.
 - Arithmetic is done in integer units (ticks, or 1e-8 of price when the tick is unknown) so edges are exact:
   `high - low == R` is never lost to floating point error. Prices are rounded to the tick grid; R in price is
   rounded to the tick (minimum one tick).
 - Bar time `t` is the epoch second of the tick that opened the bar (intermediate bars and the bar opened after a
   close take the time of the tick that caused it). `i` is a correlative index from 0 and is what the client
   chart receives as time.

Approximate bars (4.4): every 1-minute candle becomes a path of 4 prices (O, L, H, C if it closed up or equal;
O, H, L, C if it closed down) fed to the SAME builder, spaced 15 s apart from the candle start. The candle volume is
attached to the O tick, so it lands on the bar that holds the candle open. The sum of volumes equals the sum of
the candles.

T-6 STATUS: PENDING. The comparison with the NinjaTrader capture that Cristian provides has not been done yet.
If the bars differ, the rule to adjust is:
 - Rule 4 (where the next bar opens: one tick beyond the edge versus exactly at the edge, or a different
   treatment when the move reverses direction), or
 - Rule 5 (how a jump larger than one range is split into intermediate bars, or whether NinjaTrader omits them).
Rules 1 to 3 and 6 are not expected to change.
"""
import math
from collections.abc import Iterable
from typing import NamedTuple

UNKNOWN_TICK_INV = 100_000_000  # units per price unit when the tick size is unknown (1e-8 resolution)
PATH_STEP_MS = 15_000


class RangeBar(NamedTuple):
    i: int
    t: int  # epoch seconds of the tick that opened the bar
    o: float
    h: float
    l: float
    c: float
    v: float
    approx: bool


class RangeChange(NamedTuple):
    bar: RangeBar
    final: bool


class RangeBarBuilder:
    """Builds range bars tick by tick.

    `RangeBarBuilder(range_price, tick_size=None, approx=False)`; `range_price` is R in price (ticks x tick size).
    `on_tick(ts_ms, price, size=None)` returns the changes caused by the tick: first every bar that closed
    (`final=True`, in order), then the bar in progress (`final=False`). Invalid prices (NaN, infinite, <= 0) are
    ignored and return an empty list.
    """

    def __init__(self, range_price: float, tick_size: float | None = None, approx: bool = False):
        if not (isinstance(range_price, (int, float)) and math.isfinite(range_price) and range_price > 0):
            raise ValueError("range_price must be greater than zero")
        if tick_size is None:
            self._inv = UNKNOWN_TICK_INV
            self._gap = 0
        else:
            if not (math.isfinite(tick_size) and tick_size > 0):
                raise ValueError("tick_size must be greater than zero")
            self._inv = round(1 / tick_size)
            if self._inv < 1 or abs(self._inv * tick_size - 1) > 1e-9:
                raise ValueError("tick_size must be 1 divided by a whole number")
            self._gap = 1
        self.tick_size = tick_size
        self.approx = approx
        self.range_units = max(1, round(range_price * self._inv))
        self.range_price = self.range_units / self._inv
        self._closed: list[RangeBar] = []
        self._open = False
        self._t = self._o = self._h = self._l = self._c = 0
        self._v = 0.0

    def _bar(self, i: int, t: int, o: int, h: int, l: int, c: int, v: float) -> RangeBar:
        inv = self._inv
        return RangeBar(i, t, o / inv, h / inv, l / inv, c / inv, v, self.approx)

    def _start(self, t: int, price: int) -> None:
        self._open = True
        self._t, self._o, self._h, self._l, self._c, self._v = t, price, price, price, price, 0.0

    def current(self) -> RangeBar | None:
        if not self._open:
            return None
        return self._bar(len(self._closed), self._t, self._o, self._h, self._l, self._c, self._v)

    def closed(self) -> list[RangeBar]:
        """The final bars, oldest first (a copy of the list)."""
        return list(self._closed)

    def bars(self) -> list[RangeBar]:
        """Every final bar plus the bar in progress, oldest first."""
        cur = self.current()
        return self._closed + [cur] if cur else list(self._closed)

    def on_tick(self, ts_ms: int, price: float, size: float | None = None) -> list[RangeChange]:
        if price is None or not math.isfinite(price) or price <= 0:
            return []
        p = round(price * self._inv)
        if p <= 0:
            return []
        vol = size if (size is not None and math.isfinite(size) and size > 0) else 0.0
        t = int(ts_ms // 1000)
        out: list[RangeChange] = []
        if not self._open:
            self._start(t, p)
        r, gap = self.range_units, self._gap
        while True:
            nh = p if p > self._h else self._h
            nl = p if p < self._l else self._l
            if nh - nl <= r:
                break
            if p > self._h:
                edge = self._l + r
                self._h = self._c = edge
                nxt = edge + gap
            else:
                edge = self._h - r
                self._l = self._c = edge
                nxt = edge - gap
            bar = self._bar(len(self._closed), self._t, self._o, self._h, self._l, self._c, self._v)
            self._closed.append(bar)
            out.append(RangeChange(bar, True))
            self._start(t, nxt)
        if p > self._h:
            self._h = p
        if p < self._l:
            self._l = p
        self._c = p
        self._v += vol
        out.append(RangeChange(self.current(), False))
        return out


def build_from_ticks(ticks: Iterable[tuple[int, float, float | None]], range_price: float,
                     tick_size: float | None, approx: bool = False) -> RangeBarBuilder:
    """Feeds (ts_ms, price, size) ticks, oldest first, to a new builder and returns it so the caller can keep
    feeding it live. `builder.bars()` has the result."""
    builder = RangeBarBuilder(range_price, tick_size, approx)
    for ts_ms, price, size in ticks:
        builder.on_tick(ts_ms, price, size)
    return builder


def _usable(candle) -> bool:
    prices = (candle.o, candle.h, candle.l, candle.c)
    return all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in prices)


def approximate_from_minutes(candles_1m: Iterable, range_price: float, tick_size: float | None,
                             until_ts: int | None = None) -> list[RangeBar]:
    """Approximate range bars (all `approx=True`) from 1-minute candles (objects with t, o, h, l, c, v; `t` is the
    start in epoch seconds). With `until_ts`, only candles that start before it are used (the part that the
    tape does not cover). Candles with a missing or non-positive price are skipped."""
    builder = RangeBarBuilder(range_price, tick_size, approx=True)
    for candle in candles_1m:
        if (until_ts is not None and candle.t >= until_ts) or not _usable(candle):
            continue
        if candle.c >= candle.o:
            path = (candle.o, candle.l, candle.h, candle.c)
        else:
            path = (candle.o, candle.h, candle.l, candle.c)
        base = candle.t * 1000
        volume = candle.v if isinstance(candle.v, (int, float)) and math.isfinite(candle.v) else None
        for step, price in enumerate(path):
            builder.on_tick(base + step * PATH_STEP_MS, price, volume if step == 0 else None)
    return builder.bars()


def splice(approx_bars: list[RangeBar], exact_bars: list[RangeBar],
           exact_from: int | None = None) -> tuple[list[RangeBar], int | None]:
    """Joins the approximate bars that open before `exact_from` with all the exact bars, and reindexes `i` from 0.
    `exact_from` is the epoch second of the first tape tick; when None it is the open time of the first exact
    bar. Returns (bars, approx_until): `approx_until` is `exact_from` when some approximate bar was kept, else
    None (also None when there are no exact bars and so no boundary)."""
    if exact_from is None and exact_bars:
        exact_from = exact_bars[0].t
    kept = [b for b in approx_bars if exact_from is None or b.t < exact_from]
    merged = [b._replace(i=n) for n, b in enumerate(kept + list(exact_bars))]
    approx_until = exact_from if kept and exact_from is not None else None
    return merged, approx_until

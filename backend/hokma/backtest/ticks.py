"""Synthetic ticks of a bar (SPEC-020, section 4.3). The data of a bar does not say in which order the price visited the open, the
high and the low, so the order is a rule of Hokma and not a default of the engine: the extreme that hurts the open position comes
first (long: the low, short: the high), and an exact tick is put at the price of the stop and of the target when the range of the
bar crosses them, so a protective order fills at its own price and never at the far extreme. Pure functions, no engine here."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

PositionSide = Literal["long", "short"]

BAR_NS = 60 * 10**9              # the default span of a bar: one minute
OPEN_OFFSET_NS = 1_000           # the open tick is one microsecond after the open of the bar


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar. `ts` is the OPEN time of the bar (UTC); the bar closes at ts plus its span."""
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if not self.low <= min(self.open, self.close) or not self.high >= max(self.open, self.close) or self.low > self.high:
            raise ValueError("a bar needs low <= open and close <= high")


@dataclass(frozen=True)
class SyntheticTick:
    offset_ns: int                   # from the open of the bar
    price: Decimal
    kind: Literal["open", "stop", "low", "high", "target", "close"]


def crossed(level: Decimal | None, bar: Bar) -> bool:
    """The range of the bar passes through `level` and the bar did not open exactly on it."""
    return level is not None and bar.low <= level <= bar.high and level != bar.open


def bar_to_ticks(bar: Bar, *, side: PositionSide | None, stop: Decimal | None = None, target: Decimal | None = None,
                 fast: bool = False, span_ns: int = BAR_NS) -> tuple[SyntheticTick, ...]:
    """The ticks of one bar in time order. `side` is the open position when the extremes are produced (None: flat, the low goes first).
    `fast` is only the open tick, for strategies with no protective orders. The strategy still receives the whole bar at its close."""
    if span_ns < 100:
        raise ValueError("a bar needs a span of at least 100 ns")
    out = [SyntheticTick(OPEN_OFFSET_NS, bar.open, "open")]
    if fast:
        return tuple(out)
    first, second = (("high", bar.high), ("low", bar.low)) if side == "short" else (("low", bar.low), ("high", bar.high))
    if crossed(stop, bar):
        out.append(SyntheticTick(span_ns // 6, stop, "stop"))
    out.append(SyntheticTick(span_ns // 3, first[1], first[0]))
    if crossed(target, bar):
        out.append(SyntheticTick(span_ns // 2, target, "target"))
    out.append(SyntheticTick(span_ns * 2 // 3, second[1], second[0]))
    out.append(SyntheticTick(span_ns * 59 // 60, bar.close, "close"))
    return tuple(out)

"""Channel breakout alpha (SPEC-020: ECT-052): a long entry channel of N bars and a shorter exit channel of M bars.

The rules, written without any reading of the current bar into its own channel:

  * the entry channel is the highest high and the lowest low of the N bars BEFORE the current one: the current bar is not in it;
  * a close above the entry high is a long view, a close below the entry low a short view; the order itself is decided at that close and
    fills at the next open one tick against (the engine does that: this alpha only gives the view);
  * a long view turns flat when the close falls below the lowest low of the previous M bars (M is smaller than N), and a short view when the
    close rises above the highest high of the previous M bars;
  * with `skip_after_winner` a breakout is passed over when the previous breakout, taken or passed over, ended in a gain (the 20 and 10 rule);
  * with `adx_min` above zero a breakout counts only while the trend strength is at least that level: the one filter of the intraday variant.

The state is found by replaying the last `history` bars. The facts high, low and close come from the bars; adx, when used, is a fact the
runner supplies from the indicator library.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import numpy as np

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.factory.alphas._base import fact, view
from hokma.factory.params import Param, ParamSchema
from hokma.factory.strategy import IndicatorRequest

ADX_LENGTH = 14                     # the trend strength is the ADX of the indicator module with Wilder's own length


def channel_positions(high: np.ndarray, low: np.ndarray, close: np.ndarray, entry_n: int, exit_m: int, *, skip_after_winner: bool = False,
                      entry_ok: np.ndarray | None = None) -> np.ndarray:
    """The side held after each bar (1 long, -1 short, 0 flat) by the two channel rules. A bar before the entry channel exists is flat.

    A passed over breakout is followed as if it had been taken, so that its result decides whether the next one is passed over too.
    `entry_ok` (one boolean per bar) lets a filter veto an entry on that bar.
    """
    n = len(close)
    out = np.zeros(n, dtype=np.int8)
    side, taken, entry_close, last_won = 0, False, 0.0, False
    for t in range(entry_n, n):
        if side != 0:
            lowest_m, highest_m = low[t - exit_m:t].min(), high[t - exit_m:t].max()
            if (side > 0 and close[t] < lowest_m) or (side < 0 and close[t] > highest_m):
                last_won = (close[t] - entry_close) * side > 0
                side = 0
        if side == 0 and (entry_ok is None or entry_ok[t]):
            highest_n, lowest_n = high[t - entry_n:t].max(), low[t - entry_n:t].min()
            if close[t] > highest_n:
                side = 1
            elif close[t] < lowest_n:
                side = -1
            if side != 0:
                taken, entry_close = not (skip_after_winner and last_won), float(close[t])
        out[t] = side if (side != 0 and taken) else 0
    return out


@dataclass(frozen=True)
class ChannelBreakout:
    """Reads the facts high, low and close (and adx when `adx_min` is above zero). Flat view when no breakout is active."""

    entry_n: int = 20
    exit_m: int = 10
    skip_after_winner: bool = False
    adx_min: float = 0.0
    history: int = 250

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("entry_n", "int", 20, 2, 1000), Param("exit_m", "int", 10, 1, 999),
                                                 Param("skip_after_winner", "bool", False), Param("adx_min", "float", 0.0, 0.0, 100.0),
                                                 Param("history", "int", 250, 4, 5000)))

    def __post_init__(self) -> None:
        if self.exit_m >= self.entry_n:
            raise ValueError("exit_m must be shorter than entry_n")
        if self.history < self.entry_n + 2:
            raise ValueError("history must hold the entry channel and two more bars")

    @property
    def FACTS(self) -> tuple[str, ...]:
        return ("high", "low", "close", "adx") if self.adx_min > 0 else ("high", "low", "close")

    @property
    def lookback(self) -> int:
        return self.history

    def requests(self) -> tuple[IndicatorRequest, ...]:
        """The ADX, asked only when a threshold is set: the runner supplies it from `hokma.quant.indicators` (it reads the high, low and close)."""
        return (IndicatorRequest("adx", "adx", "bars", (("length", ADX_LENGTH),), 2 * ADX_LENGTH),) if self.adx_min > 0 else ()

    def signal(self, ctx: SignalContext) -> Signal:
        high, low, close = (fact(ctx, name, self.entry_n + 1) for name in ("high", "low", "close"))
        if high is None or low is None or close is None:
            return ctx.abstain("The highs, lows and closes do not cover the entry channel and the current bar yet. No view is given until they do.", ("high", "low", "close"))
        adx = fact(ctx, "adx", 1) if self.adx_min > 0 else None
        if self.adx_min > 0 and adx is None:
            return ctx.abstain("The trend strength is not available. No view is given until it is.", ("adx",))
        size = min(len(high), len(low), len(close), self.history)
        high, low, close = high[-size:], low[-size:], close[-size:]
        ok = None if adx is None else np.nan_to_num(adx[-size:], nan=-1.0) >= self.adx_min
        side = int(channel_positions(high, low, close, self.entry_n, self.exit_m, skip_after_winner=self.skip_after_winner, entry_ok=ok)[-1])
        upper, lower = float(high[-1 - self.entry_n:-1].max()), float(low[-1 - self.entry_n:-1].min())
        criteria = [Criterion("close_vs_entry_high", float(close[-1]), upper, bool(close[-1] > upper), "data.high"),
                    Criterion("close_vs_entry_low", float(close[-1]), lower, bool(close[-1] < lower), "data.low")]
        if adx is not None:
            criteria.append(Criterion("adx", float(adx[-1]), self.adx_min, bool(adx[-1] >= self.adx_min), "indicator.adx"))
        word = {1: "long", -1: "short", 0: "flat"}[side]
        reason = (f"The close is measured against the highest high and the lowest low of the {self.entry_n} bars before it. "
                  f"The view is {word}. A trend view turns flat when the close crosses the channel of the last {self.exit_m} bars.")
        return view(ctx, "alpha.channel_breakout", float(side), criteria, reason)

"""Sequential count of closes: setup to 9 and countdown to 13, buy and sell (SPEC-020: L-57).

Written from the published idea, with these rules.
- Buy setup: a run of closes each below the close four bars earlier; the count goes 1 to 9 and a close that breaks the run resets it to
  0. After a 9 the next qualifying close starts a new count at 1. The sell setup is the mirror (closes above the close four bars earlier).
- Perfected: at the 9 of a buy setup, the lower low of bars 8 and 9 is at or below the lower low of bars 6 and 7 (sell: highs, mirrored).
- Countdown: starts on the bar that completes the setup of its side (that bar can count). A bar counts when its close is at or below the low
  of two bars earlier (sell: at or above the high). Bars need not be consecutive. The 13th count needs the low of its bar at or below the
  close of the 8th count bar (sell: the high at or above); until a bar meets it the count stays at 12.
- A countdown ends when it reaches 13, or when the setup of the opposite side completes. A new setup of the same side does not restart it.

Every value at a bar uses that bar and earlier bars only. A bar needs four bars before it to be compared: the first four bars count 0.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from hokma.quant.indicators.catalog import register_indicator

_PIECE = ("L-57",)
LAG_BARS = 0
SETUP = 9
COUNTDOWN = 13
_BACK = 4


def sequential(frame: pd.DataFrame) -> pd.DataFrame:
    close, high, low = (frame[c].to_numpy(dtype=float) for c in ("close", "high", "low"))
    n = len(close)
    out = {k: np.zeros(n) for k in ("buy_setup", "sell_setup", "buy_perfected", "sell_perfected", "buy_countdown", "sell_countdown")}
    run = {"buy": 0, "sell": 0}
    active = {"buy": False, "sell": False}
    count = {"buy": 0, "sell": 0}
    eighth = {"buy": 0.0, "sell": 0.0}
    for t in range(n):
        done = {"buy": False, "sell": False}
        if t >= _BACK:
            for side, qualifies in (("buy", close[t] < close[t - _BACK]), ("sell", close[t] > close[t - _BACK])):
                run[side] = (1 if run[side] == SETUP else run[side] + 1) if qualifies else 0
                done[side] = run[side] == SETUP
        else:
            run["buy"] = run["sell"] = 0
        if done["buy"]:
            out["buy_perfected"][t] = float(min(low[t - 1], low[t]) <= min(low[t - 3], low[t - 2]))
            active["buy"], active["sell"], count["sell"] = True, False, 0
        if done["sell"]:
            out["sell_perfected"][t] = float(max(high[t - 1], high[t]) >= max(high[t - 3], high[t - 2]))
            active["sell"], active["buy"], count["buy"] = True, False, 0
        for side, sign in (("buy", 1), ("sell", -1)):
            finished = False
            if active[side] and t >= 2:
                level = low[t - 2] if sign == 1 else high[t - 2]
                counts = close[t] <= level if sign == 1 else close[t] >= level
                if counts and count[side] == COUNTDOWN - 1:
                    reach = low[t] <= eighth[side] if sign == 1 else high[t] >= eighth[side]
                    if reach:
                        count[side] += 1
                        finished = True
                elif counts:
                    count[side] += 1
                    if count[side] == 8:
                        eighth[side] = close[t]
            out[f"{side}_setup"][t] = run[side]
            out[f"{side}_countdown"][t] = count[side]
            if finished:
                active[side], count[side] = False, 0
    return pd.DataFrame(out, index=frame.index)


td_sequential = register_indicator(
    "td_sequential", sequential, pieces=_PIECE, needed=("high", "low", "close"), lag_bars=LAG_BARS, unit="count", closed_only=True, min_bars=5,
    summary="Sequential count on closed daily bars: buy and sell setup 1 to 9, perfected flag at the 9, and countdown 1 to 13 (the 13th needs its qualifier). Rules are in the module text; counts use the bar and earlier bars only.")

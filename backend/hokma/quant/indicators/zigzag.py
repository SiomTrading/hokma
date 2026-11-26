"""ZigZag: the confirmed turning points of a price, found without looking ahead (SPEC-020: L-57).

The input is the close. A turn is confirmed only on the bar where the price has already pulled back by threshold (a fraction, 0.05 is 5
percent) from the extreme of the leg that is running. Until then that extreme is only a candidate, so a confirmed pivot never changes
and the result on the first n bars is the start of the result on any later bars (no repaint). Each pivot says on which bar it was
confirmed and how many bars after its own bar that was.

Each pivot has its own confirmation lag; the registered lag of one bar is the least it can be.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from hokma.quant.indicators.catalog import register_indicator

_PIECE = ("L-57",)
LAG_BARS = 1
_EPS = 1e-9


def _move(price: float, reference: float) -> float:
    return round(price / reference - 1.0, 10)


def find_pivots(close: pd.Series, threshold: float) -> dict[str, Any]:
    """Confirmed pivots, the direction of the running leg and its unconfirmed extreme, from the closes in order."""
    values = close.to_numpy(dtype=float)
    labels = [str(i) for i in close.index]
    pivots: list[dict[str, Any]] = []
    high_at = low_at = 0
    direction = 0          # 0 not decided yet, 1 the leg goes up (the last pivot was a low), -1 it goes down
    extreme = 0

    def confirm(at: int, kind: str, now: int) -> None:
        pivots.append({"index": labels[at], "price": float(values[at]), "kind": kind, "confirmed_at": labels[now], "bars_to_confirm": now - at})

    for t in range(1, len(values)):
        price = values[t]
        if direction == 0:
            high_at = t if price > values[high_at] else high_at
            low_at = t if price < values[low_at] else low_at
            if _move(price, values[low_at]) >= threshold - _EPS:
                confirm(low_at, "low", t)
                direction, extreme = 1, t
            elif _move(price, values[high_at]) <= -threshold + _EPS:
                confirm(high_at, "high", t)
                direction, extreme = -1, t
        elif direction == 1:
            if price > values[extreme]:
                extreme = t
            elif _move(price, values[extreme]) <= -threshold + _EPS:
                confirm(extreme, "high", t)
                direction, extreme = -1, t
        else:
            if price < values[extreme]:
                extreme = t
            elif _move(price, values[extreme]) >= threshold - _EPS:
                confirm(extreme, "low", t)
                direction, extreme = 1, t
    leg = None
    if direction != 0:
        leg = {"direction": "up" if direction == 1 else "down", "extreme_index": labels[extreme], "extreme_price": float(values[extreme])}
    return {"pivots": pivots, "leg": leg, "n_pivots": len(pivots), "bars": len(values)}


def zigzag(frame: pd.DataFrame, threshold: float) -> dict[str, Any]:
    if threshold >= 1.0:
        raise ValueError("threshold must be a fraction below 1, for example 0.05 for 5 percent")
    return find_pivots(frame["close"], threshold)


registered_zigzag = register_indicator(
    "zigzag", zigzag, pieces=_PIECE, needed=("close",), lag_bars=LAG_BARS, unit="price", closed_only=True, output="value", min_bars=3,
    defaults={"threshold": 0.05},
    summary="ZigZag of the closes of closed daily bars: confirmed pivots (index, price, high or low, bar of confirmation) and the running leg. A turn is confirmed only after the pullback of threshold (default 5 percent) has happened, so it never repaints.")

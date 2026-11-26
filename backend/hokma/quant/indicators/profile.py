"""Volume profile, market profile and time profile of a window of bars (SPEC-020: L-57).

Each one summarises the last window bars (all of them when fewer): the profile is a distribution over price or over the time of day, not a
series. They use only bars up to the last one, so they are causal; a boolean closed column in the bars drops candles that are still open.

- Volume profile: the volume of each bar is spread evenly over the price range of the bar and added into bins of equal width between the lowest
  low and the highest high. The point of control (POC) is the bin with the most volume; the value area is the run of bins around it that
  holds value_area (70 percent) of the volume.
- Market profile: the same bins but each bar counts one time price opportunity (TPO) in every bin its range touches.
- Time profile: the share of volume in each slot of the day (slot_minutes wide) over the last window sessions of intraday bars.

Value area rule: from the POC, compare the volume of the two bins above with the two below, add the larger pair (the one above on a tie)
and repeat until the area holds the wanted share. A POC tie goes to the bin closest to the middle of the range.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant.errors import BadInput
from hokma.quant.indicators.catalog import register_indicator

_PIECE = ("L-57",)
LAG_BARS = 0
MAX_BINS = 500
_EPS = 1e-12


def _check_bins(bins: int, value_area: float) -> None:
    if bins < 2:
        raise ValueError("bins must be at least 2")
    if bins > MAX_BINS:
        raise BadInput(f"bins={bins} is above the limit {MAX_BINS}")
    if not 0.0 < value_area <= 1.0:
        raise ValueError("value_area must be in (0, 1]")


def _edges(low: np.ndarray, high: np.ndarray, bins: int) -> np.ndarray:
    lo, hi = float(low.min()), float(high.max())
    if not hi > lo:
        raise ValueError("the window has no price range: a profile needs at least one bar with high above low")
    return np.linspace(lo, hi, bins + 1)


def point_of_control(weights: np.ndarray, edges: np.ndarray) -> int:
    """Index of the bin with the most weight; a tie goes to the bin whose centre is closest to the middle of the range."""
    top = float(weights.max())
    tied = np.flatnonzero(weights >= top - _EPS * max(top, 1.0))
    centres = (edges[:-1] + edges[1:]) / 2.0
    middle = (edges[0] + edges[-1]) / 2.0
    return int(tied[np.argmin(np.abs(centres[tied] - middle))])


def value_area(weights: np.ndarray, poc: int, share: float) -> tuple[int, int]:
    """First and last bin of the value area around the POC (the rule is in the module text)."""
    target = share * float(weights.sum())
    first = last = poc
    held = float(weights[poc])
    while held < target - _EPS * max(target, 1.0):
        above = float(weights[last + 1:last + 3].sum()) if last + 1 < len(weights) else None
        below = float(weights[max(first - 2, 0):first].sum()) if first > 0 else None
        if above is None and below is None:
            break
        if below is None or (above is not None and above >= below):
            take = min(2, len(weights) - 1 - last)
            held += float(weights[last + 1:last + 1 + take].sum())
            last += take
        else:
            take = min(2, first)
            held += float(weights[first - take:first].sum())
            first -= take
    return first, last


def _summary(weights: np.ndarray, edges: np.ndarray, share: float, key: str, bars: int) -> dict[str, Any]:
    poc = point_of_control(weights, edges)
    first, last = value_area(weights, poc, share)
    return {"poc": float((edges[poc] + edges[poc + 1]) / 2.0), "vah": float(edges[last + 1]), "val": float(edges[first]), "edges": edges.tolist(),
            key: weights.tolist(), "bars": bars}


def volume_profile(frame: pd.DataFrame, window: int, bins: int, value_area_share: float) -> dict[str, Any]:
    _check_bins(bins, value_area_share)
    work = frame.iloc[-window:]
    low, high, volume = (work[c].to_numpy(dtype=float) for c in ("low", "high", "volume"))
    edges = _edges(low, high, bins)
    weights = np.zeros(bins)
    for lo_b, hi_b, vol in zip(low, high, volume):
        if hi_b > lo_b:
            overlap = np.clip(np.minimum(hi_b, edges[1:]) - np.maximum(lo_b, edges[:-1]), 0.0, None)
            weights += vol * overlap / (hi_b - lo_b)
        else:
            weights[min(int(np.searchsorted(edges, lo_b, side="right")) - 1, bins - 1)] += vol
    if float(weights.sum()) <= 0.0:
        raise ValueError("the window has no volume")
    return _summary(weights, edges, value_area_share, "volume", len(work))


def market_profile(frame: pd.DataFrame, window: int, bins: int, value_area_share: float) -> dict[str, Any]:
    _check_bins(bins, value_area_share)
    work = frame.iloc[-window:]
    low, high = (work[c].to_numpy(dtype=float) for c in ("low", "high"))
    edges = _edges(low, high, bins)
    width = (edges[-1] - edges[0]) / bins
    counts = np.zeros(bins)
    for lo_b, hi_b in zip(low, high):
        first = min(int(np.floor((lo_b - edges[0]) / width + _EPS)), bins - 1)
        last = min(int(np.floor((hi_b - edges[0]) / width + _EPS)), bins - 1)
        counts[first:last + 1] += 1.0
    return _summary(counts, edges, value_area_share, "tpo", len(work))


def time_profile(frame: pd.DataFrame, window: int, slot_minutes: int) -> dict[str, Any]:
    if slot_minutes < 1 or slot_minutes > 1440:
        raise ValueError("slot_minutes must be between 1 and 1440")
    index = frame.index
    days = pd.Series(index.normalize(), index=index)
    if days.groupby(days).size().max() < 2:
        raise ValueError("a time profile needs intraday bars (several bars per session), got daily or slower bars")
    keep = sorted(days.unique())[-window:]
    work = frame[days.isin(keep).to_numpy()]
    minutes = work.index.hour * 60 + work.index.minute
    slot = (minutes // slot_minutes) * slot_minutes
    volume = work["volume"].groupby(slot).sum()
    count = work["volume"].groupby(slot).size()
    total = float(volume.sum())
    if total <= 0.0:
        raise ValueError("the window has no volume")
    slots = [{"slot": f"{int(m) // 60:02d}:{int(m) % 60:02d}", "volume": float(v), "share": float(v) / total, "bars": int(count[m])} for m, v in volume.items()]
    peak = max(slots, key=lambda s: s["volume"])
    return {"slots": slots, "peak_slot": peak["slot"], "peak_share": peak["share"], "sessions": len(keep), "bars": len(work)}


_PROFILE_DEFAULTS = {"window": 20, "bins": 24, "value_area_share": 0.7}
registered_volume_profile = register_indicator(
    "volume_profile", volume_profile, pieces=_PIECE, needed=("high", "low", "volume"), lag_bars=LAG_BARS, unit="price", output="value",
    defaults=_PROFILE_DEFAULTS,
    summary="Volume profile of the last window bars (default 20): volume by price bin, point of control and value area (default 70 percent of the volume). The volume of a bar is spread evenly over its range. A closed column drops open candles.")
registered_market_profile = register_indicator(
    "market_profile", market_profile, pieces=_PIECE, needed=("high", "low"), lag_bars=LAG_BARS, unit="price", output="value",
    defaults=_PROFILE_DEFAULTS,
    summary="Market profile of the last window bars (default 20): time price opportunities by price bin (one per bar that touches the bin), point of control and value area. A closed column drops open candles.")
registered_time_profile = register_indicator(
    "time_profile", time_profile, pieces=_PIECE, needed=("volume",), lag_bars=LAG_BARS, unit="share", output="value",
    defaults={"window": 20, "slot_minutes": 30},
    summary="Time profile of the last window sessions (default 20) of intraday bars: share of the volume in each slot of the day (default 30 minutes) and the peak slot. Daily bars are refused. A closed column drops open candles.")

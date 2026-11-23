"""Weekly structure of one instrument from daily bars (SPEC-019: CTS-005).

The daily bars are cut at as_of first and only then resampled to weeks (a week is closed once its Friday 16:00 New York has
passed), so the week in progress never enters. The result holds the trend, the swing structure, supports and resistances
(fractal pivots of five weeks, confirmed only after two later weeks), the moving averages, the volume, and two to four
scenarios with a trigger and an invalidation level.

Scenario probabilities are not calibrated by anything, so none is given: each scenario carries probability None.
Confirmation mode answers whether a reversal is real: a change of the 13 week direction, a failed 52 week extreme or a failed
breakout, vetoed when the larger trend continues, with strict inequalities. A visual reading that disagrees with the reading by
code makes the verdict not_confirmed.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import limits, registry
from hokma.quant.indicators._common import build, closed_weekly, empty, to_weekly
from hokma.quant.indicators.screens import _base
from hokma.quant.types import QuantResult

_PIECE = ("CTS-005",)
_NAME = "indicators.screens.weekly_structure"
_FRACTAL = 2          # five week fractal: two weeks on each side
_DIRECTIONS = ("up", "down", "sideways")


def fractal_pivots(high: pd.Series, low: pd.Series, order: int = _FRACTAL) -> tuple[list[int], list[int]]:
    """Positions of pivot highs and pivot lows. A pivot beats its neighbours on both sides strictly, so it is known only
    after order more bars have closed (the last order bars never hold a pivot)."""
    h, l_ = high.to_numpy(), low.to_numpy()
    highs, lows = [], []
    for i in range(order, len(h) - order):
        window_h = np.r_[h[i - order:i], h[i + 1:i + order + 1]]
        window_l = np.r_[l_[i - order:i], l_[i + 1:i + order + 1]]
        if h[i] > window_h.max():
            highs.append(i)
        if l_[i] < window_l.min():
            lows.append(i)
    return highs, lows


def _trend(close: pd.Series, sma30: pd.Series) -> str:
    slope = float(sma30.iloc[-1] - sma30.iloc[-5])
    if close.iloc[-1] > sma30.iloc[-1] and slope > 0:
        return "up"
    if close.iloc[-1] < sma30.iloc[-1] and slope < 0:
        return "down"
    return "sideways"


def confirmation(weekly: pd.DataFrame, trend: str, sma30: pd.Series, pivot_highs: list[int], *, direction_weeks: int,
                 extreme_weeks: int) -> dict:
    close, high, low = weekly["close"], weekly["high"], weekly["low"]
    n = len(weekly)
    direction = np.sign(close - close.shift(direction_weeks))
    now, before = float(direction.iloc[-1]), float(direction.iloc[-2])
    changed = (now != 0.0) and (before != 0.0) and (now != before)
    new_dir = "up" if now > 0 else "down" if now < 0 else None
    prior_high = float(high.iloc[-1 - extreme_weeks:-1].max())
    prior_low = float(low.iloc[-1 - extreme_weeks:-1].min())
    failed_high = bool(high.iloc[-1] > prior_high and close.iloc[-1] <= prior_high)
    failed_low = bool(low.iloc[-1] < prior_low and close.iloc[-1] >= prior_low)
    confirmed_before = [i for i in pivot_highs if i <= n - 2 - _FRACTAL]
    level = float(high.iloc[confirmed_before[-1]]) if confirmed_before else None
    failed_breakout = bool(level is not None and close.iloc[-2] > level and close.iloc[-1] < level)
    slope = float(sma30.iloc[-1] - sma30.iloc[-5])
    veto = bool(changed and ((new_dir == "down" and close.iloc[-1] > sma30.iloc[-1] and slope > 0)
                             or (new_dir == "up" and close.iloc[-1] < sma30.iloc[-1] and slope < 0)))
    return {"direction_change": bool(changed), "new_direction": new_dir if changed else None, "failed_extreme_high": failed_high,
            "failed_extreme_low": failed_low, "failed_breakout": failed_breakout, "continuation_veto": veto,
            "prior_extreme_high": prior_high, "prior_extreme_low": prior_low}


def _levels(close: float, weekly: pd.DataFrame, pivot_highs: list[int], pivot_lows: list[int]) -> tuple[list[float], list[float]]:
    resistances = sorted({float(weekly["high"].iloc[i]) for i in pivot_highs if weekly["high"].iloc[i] > close})[:2]
    supports = sorted({float(weekly["low"].iloc[i]) for i in pivot_lows if weekly["low"].iloc[i] < close}, reverse=True)[:2]
    return supports, resistances


@registry.register(_NAME, module="indicators",
                   summary="Weekly structure from daily bars (cut at as_of before resampling, only closed weeks): trend, swing structure, fractal supports and resistances, averages, volume, 2 to 4 scenarios with trigger and invalidation (no probabilities), and a confirmation mode for reversals.",
                   inputs={"bars": {"type": "dataframe", "required": True, "columns": ["open", "high", "low", "close", "volume"]},
                           "as_of": {"type": "str", "required": False}, "timeframe": {"type": "str", "required": False, "default": "daily"},
                           "direction_weeks": {"type": "int", "required": False, "default": 13},
                           "extreme_weeks": {"type": "int", "required": False, "default": 52},
                           "visual_read": {"type": "str", "required": False}}, piece_ids=_PIECE)
def weekly_structure(bars: Any, as_of: Any = None, timeframe: str = "daily", direction_weeks: int = 13, extreme_weeks: int = 52,
                     visual_read: str | None = None) -> QuantResult:
    direction_weeks, extreme_weeks = limits.as_int("direction_weeks", direction_weeks), limits.as_int("extreme_weeks", extreme_weeks)
    params = {"timeframe": timeframe, "direction_weeks": direction_weeks, "extreme_weeks": extreme_weeks, "visual_read": visual_read}
    if timeframe not in ("daily", "weekly"):
        return empty(_NAME, "structure", "weekly_structure", "timeframe must be daily or weekly", params=params, piece_ids=_PIECE)
    if visual_read is not None and visual_read not in _DIRECTIONS:
        return empty(_NAME, "structure", "weekly_structure", f"visual_read must be one of {list(_DIRECTIONS)}", params=params, piece_ids=_PIECE)
    if direction_weeks < 2 or extreme_weeks < 5:
        return empty(_NAME, "structure", "weekly_structure", "direction_weeks must be at least 2 and extreme_weeks at least 5", params=params, piece_ids=_PIECE)
    needed = max(extreme_weeks + 2, 45)
    if timeframe == "daily":
        frame, dropped, warns = _base.daily_closed(bars, ("open", "high", "low", "close", "volume"), as_of=as_of, min_bars=needed * 3)
        params["dropped_open_bars"] = dropped
        if frame is None:
            return empty(_NAME, "structure", "weekly_structure", warns, params=params, piece_ids=_PIECE)
        weekly, open_weeks = closed_weekly(to_weekly(frame), as_of)
        params["dropped_open_weeks"] = open_weeks
    else:
        frame, dropped, warns = _base.daily_closed(bars, ("open", "high", "low", "close", "volume"), as_of=None, min_bars=needed)
        if frame is None:
            return empty(_NAME, "structure", "weekly_structure", warns, params=params, piece_ids=_PIECE)
        weekly, open_weeks = closed_weekly(frame, as_of)
        warns = ()
        params["dropped_open_weeks"] = open_weeks
    if len(weekly) < needed:
        return empty(_NAME, "structure", "weekly_structure", warns + (f"need {needed} closed weeks, got {len(weekly)}",), params=params, piece_ids=_PIECE)
    close = weekly["close"]
    mas = {w: close.rolling(w).mean() for w in (10, 30, 40)}
    trend = _trend(close, mas[30])
    highs, lows = fractal_pivots(weekly["high"], weekly["low"])
    last_close = float(close.iloc[-1])
    supports, resistances = _levels(last_close, weekly, highs, lows)
    low13, high13 = float(weekly["low"].iloc[-13:].min()), float(weekly["high"].iloc[-13:].max())
    support = supports[0] if supports else low13
    resistance = resistances[0] if resistances else high13
    structure = "undefined"
    if len(highs) >= 2 and len(lows) >= 2:
        hh, hl = weekly["high"].iloc[highs[-1]] > weekly["high"].iloc[highs[-2]], weekly["low"].iloc[lows[-1]] > weekly["low"].iloc[lows[-2]]
        structure = ("higher_highs_higher_lows" if hh and hl else "lower_highs_lower_lows" if not hh and not hl
                     else "expanding" if hh and not hl else "contracting")
    volume_ratio = float(weekly["volume"].iloc[-1] / weekly["volume"].iloc[-11:-1].mean())
    confirm = confirmation(weekly, trend, mas[30], highs, direction_weeks=direction_weeks, extreme_weeks=extreme_weeks)
    reasons = []
    reversal = confirm["direction_change"] or confirm["failed_extreme_high"] or confirm["failed_extreme_low"] or confirm["failed_breakout"]
    if not reversal:
        reasons.append("no_reversal_signal")
    if confirm["continuation_veto"]:
        reasons.append("continuation_veto")
    code_read = trend
    if visual_read is not None and visual_read != code_read:
        reasons.append("visual_code_mismatch")
    verdict = "confirmed" if reversal and not reasons else "not_confirmed"
    scenarios = [
        {"name": "bullish_continuation", "direction": "up", "trigger": resistance, "invalidation": support, "probability": None},
        {"name": "bearish_break", "direction": "down", "trigger": support, "invalidation": resistance, "probability": None},
    ]
    if trend == "sideways":
        scenarios.append({"name": "range_holds", "direction": "flat", "trigger": None, "invalidation": support,
                          "upper": resistance, "lower": support, "probability": None})
    if confirm["direction_change"] or confirm["failed_extreme_high"] or confirm["failed_extreme_low"] or confirm["failed_breakout"]:
        up = confirm["new_direction"] == "up" or confirm["failed_extreme_low"]
        scenarios.append({"name": "reversal", "direction": "up" if up else "down", "trigger": float(weekly["high"].iloc[-1]) if up else float(weekly["low"].iloc[-1]),
                          "invalidation": float(weekly["low"].iloc[-1]) if up else float(weekly["high"].iloc[-1]), "probability": None})
    value = {
        "week_ending": _base.day_text(weekly.index[-1]), "trend": trend, "swing_structure": structure,
        "close": last_close, "averages": {f"sma_{w}": float(s.iloc[-1]) for w, s in mas.items()},
        "supports": supports, "resistances": resistances, "volume_ratio_10w": volume_ratio,
        "scenarios": scenarios, "probabilities_calibrated": False,
        "confirmation": {**confirm, "verdict": verdict, "reasons": reasons, "code_read": code_read, "visual_read": visual_read},
    }
    return build(_NAME, value, unit="structure", method="weekly_structure", params=params, piece_ids=_PIECE, inputs=(weekly,),
                 as_of_source=weekly, warnings=warns + ("scenario_probabilities_not_calibrated: none is given",))

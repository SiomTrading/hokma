"""Volatility contraction pattern screen over daily closed bars (SPEC-019: CTS-020).

Reads, at the last closed bar: the seven point trend template, two to four successive contractions where each depth is at most
contraction_ratio (0.70) of the one before, the dry up of volume, the pivot (the high that starts the last contraction), and the
state Pre-breakout, Breakout, Early-post-breakout, Extended or Damaged. It needs 253 sessions with valid high, low and close:
anything less or invalid gives no value (never a zero). Swing points are strict fractals confirmed after swing_order later bars.

The forward outcome of a detection is a separate function that fills like the real order would: the signal is read at a close,
the entry is the next open plus one tick, the stop is checked on the low (the stop is taken first when one bar does both), the
exit price is the stop or a gap open, costs are deducted, and every gain and loss is measured from the fill, not from the
close of the detection date. No size multiplier is produced: a size comes from the risk module.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import limits, registry
from hokma.quant.indicators._common import build, empty, entry_fill
from hokma.quant.indicators.screens import _base
from hokma.quant.indicators.screens.weekly_structure import fractal_pivots
from hokma.quant.types import QuantResult

_PIECE = ("CTS-020",)
_NAME = "indicators.screens.vcp"
_FORWARD = "indicators.screens.vcp_forward_outcome"
MIN_SESSIONS = 253
_EPS = 1e-9


def trend_template(close: pd.Series, high: pd.Series, low: pd.Series) -> dict:
    """The seven points of the trend template at the last bar."""
    sma50, sma150, sma200 = (float(close.iloc[-w:].mean()) for w in (50, 150, 200))
    sma200_before = float(close.iloc[-222:-22].mean())
    last = float(close.iloc[-1])
    low52, high52 = float(low.iloc[-252:].min()), float(high.iloc[-252:].max())
    points = {
        "close_above_sma150_and_sma200": last > sma150 and last > sma200,
        "sma150_above_sma200": sma150 > sma200,
        "sma200_rising_22_sessions": sma200 > sma200_before,
        "sma50_above_sma150_and_sma200": sma50 > sma150 and sma50 > sma200,
        "close_above_sma50": last > sma50,
        "close_30_percent_above_52w_low": last >= 1.30 * low52 - _EPS,
        "close_within_25_percent_of_52w_high": last >= 0.75 * high52 - _EPS,
    }
    return {"points": points, "passed": int(sum(points.values())), "sma50": sma50, "sma150": sma150, "sma200": sma200,
            "low_52w": low52, "high_52w": high52}


def contractions(high: pd.Series, low: pd.Series, *, swing_order: int, lookback: int) -> list[dict]:
    """Alternating swing high to swing low legs inside the lookback, oldest first. depth = (high - low) / high."""
    start = max(0, len(high) - lookback)
    hh, ll = fractal_pivots(high, low, swing_order)
    points = sorted([(i, "H") for i in hh if i >= start] + [(i, "L") for i in ll if i >= start])
    merged: list[tuple[int, str]] = []
    for pos, kind in points:
        if merged and merged[-1][1] == kind:
            prev = merged[-1][0]
            keep_new = high.iloc[pos] > high.iloc[prev] if kind == "H" else low.iloc[pos] < low.iloc[prev]
            if keep_new:
                merged[-1] = (pos, kind)
        else:
            merged.append((pos, kind))
    legs = []
    for (p1, k1), (p2, k2) in zip(merged, merged[1:]):
        if k1 == "H" and k2 == "L":
            top, bottom = float(high.iloc[p1]), float(low.iloc[p2])
            legs.append({"start": p1, "end": p2, "high": top, "low": bottom, "depth": (top - bottom) / top})
    return legs


def contraction_chain(legs: list[dict], ratio: float, max_legs: int = 4) -> list[dict]:
    """The longest run of legs ending at the last one where each depth is at most ratio times the one before (at most max_legs)."""
    chain: list[dict] = []
    for leg in reversed(legs):
        if not chain or chain[0]["depth"] <= ratio * leg["depth"] + _EPS:
            chain.insert(0, leg)
        else:
            break
        if len(chain) == max_legs:
            break
    return chain


def classify_state(last: float, pivot: float, stop: float, closes_after_low: np.ndarray, *, extended_pct: float,
                   early_bars: int) -> tuple[str, int | None]:
    """State of a valid pattern from closes only (a wick never decides). closes_after_low are the closes after the last contraction
    low up to and including the last bar. Returns (state, sessions since the first close above the pivot)."""
    if last < stop:
        return "Damaged", None
    if last <= pivot:
        return "Pre-breakout", None
    above = closes_after_low > pivot
    first_up = int(np.argmax(above))
    days_since = len(above) - 1 - first_up
    gap = last / pivot - 1.0
    if days_since == 0:
        return ("Breakout" if gap <= extended_pct + _EPS else "Extended"), days_since
    if gap > extended_pct + _EPS or days_since > early_bars:
        return "Extended", days_since
    return "Early-post-breakout", days_since


@registry.register(_NAME, module="indicators",
                   summary="VCP screen on closed daily bars: 7 point trend template, 2 to 4 contractions with depth ratio 0.70, volume dry up, pivot and state (Pre-breakout, Breakout, Early-post-breakout, Extended, Damaged). Needs 253 valid sessions; missing data is no value, never zero. Closes decide, never wicks.",
                   inputs={"bars": {"type": "dataframe", "required": True, "columns": ["high", "low", "close", "volume"]},
                           "as_of": {"type": "str", "required": False}, "contraction_ratio": {"type": "float", "required": False, "default": 0.70},
                           "swing_order": {"type": "int", "required": False, "default": 3},
                           "pattern_lookback": {"type": "int", "required": False, "default": 130},
                           "min_first_depth": {"type": "float", "required": False, "default": 0.08},
                           "dryup_bars": {"type": "int", "required": False, "default": 10},
                           "dryup_reference_bars": {"type": "int", "required": False, "default": 50},
                           "dryup_ratio": {"type": "float", "required": False, "default": 0.8},
                           "extended_pct": {"type": "float", "required": False, "default": 0.05},
                           "early_bars": {"type": "int", "required": False, "default": 5},
                           "max_bar_age": {"type": "int", "required": False, "default": 2}}, piece_ids=_PIECE)
def vcp(bars: Any, as_of: Any = None, contraction_ratio: float = 0.70, swing_order: int = 3, pattern_lookback: int = 130,
        min_first_depth: float = 0.08, dryup_bars: int = 10, dryup_reference_bars: int = 50, dryup_ratio: float = 0.8,
        extended_pct: float = 0.05, early_bars: int = 5, max_bar_age: int = 2) -> QuantResult:
    ints = {n: limits.as_int(n, v) for n, v in (("swing_order", swing_order), ("pattern_lookback", pattern_lookback), ("dryup_bars", dryup_bars),
                                                 ("dryup_reference_bars", dryup_reference_bars), ("early_bars", early_bars), ("max_bar_age", max_bar_age))}
    params = {"contraction_ratio": contraction_ratio, "min_first_depth": min_first_depth, "dryup_ratio": dryup_ratio, "extended_pct": extended_pct, **ints}
    try:
        ratio = _base.as_float("contraction_ratio", contraction_ratio, positive=True)
        first_depth = _base.as_float("min_first_depth", min_first_depth, positive=True)
        dry = _base.as_float("dryup_ratio", dryup_ratio, positive=True)
        ext = _base.as_float("extended_pct", extended_pct, positive=True)
        if ratio >= 1.0 or min(ints["swing_order"], ints["dryup_bars"], ints["dryup_reference_bars"], ints["early_bars"]) < 1 or ints["pattern_lookback"] < 20 or ints["max_bar_age"] < 0:
            raise _base.OutOfDomain("contraction_ratio must be below 1 and the bar counts at least 1 (pattern_lookback at least 20)")
    except _base.OutOfDomain as exc:
        return empty(_NAME, "state", "vcp", str(exc), params=params, piece_ids=_PIECE)
    frame, dropped, warns = _base.daily_closed(bars, ("high", "low", "close", "volume"), as_of=as_of, min_bars=MIN_SESSIONS)
    params["dropped_open_bars"] = dropped
    if frame is None:
        return empty(_NAME, "state", "vcp", warns + (f"not available: the screen needs {MIN_SESSIONS} closed sessions with valid high, low, close and volume",),
                     params=params, piece_ids=_PIECE)
    stale = _base.stale_reason(frame, as_of, ints["max_bar_age"])
    if stale:
        return empty(_NAME, "state", "vcp", warns + (stale,), params=params, piece_ids=_PIECE)
    close, high, low, volume = frame["close"], frame["high"], frame["low"], frame["volume"]
    template = trend_template(close, high, low)
    legs = contractions(high, low, swing_order=ints["swing_order"], lookback=ints["pattern_lookback"])
    chain = contraction_chain(legs, ratio)
    last = float(close.iloc[-1])
    dry_ratio = float(volume.iloc[-ints["dryup_bars"]:].mean() / volume.iloc[-(ints["dryup_bars"] + ints["dryup_reference_bars"]):-ints["dryup_bars"]].mean())
    reasons: list[str] = []
    if template["passed"] < 7:
        reasons.append("trend_template_failed")
    if len(chain) < 2:
        reasons.append("fewer_than_2_contractions")
    elif chain[0]["depth"] < first_depth:
        reasons.append("first_contraction_too_shallow")
    state, pivot, stop, days_since = None, None, None, None
    if not reasons:
        pivot, stop = chain[-1]["high"], chain[-1]["low"]
        state, days_since = classify_state(last, pivot, stop, close.iloc[chain[-1]["end"] + 1:].to_numpy(), extended_pct=ext,
                                           early_bars=ints["early_bars"])
    value = {
        "bar_date": _base.day_text(frame.index[-1]), "state": state, "reasons": reasons, "trend_template": template,
        "contractions": [{"start": _base.day_text(frame.index[c["start"]]), "end": _base.day_text(frame.index[c["end"]]), "high": c["high"],
                          "low": c["low"], "depth": c["depth"]} for c in chain],
        "contraction_count": len(chain), "depth_ratios": [chain[i + 1]["depth"] / chain[i]["depth"] for i in range(len(chain) - 1)],
        "volume_dry_up": {"ratio": dry_ratio, "ok": bool(dry_ratio <= dry + _EPS)}, "pivot": pivot, "stop": stop,
        "days_since_breakout": days_since, "close": last, "entry_rule": "next_open_plus_1_tick", "same_bar_priority": "stop_first",
    }
    return build(_NAME, value, unit="state", method="vcp", params=params, piece_ids=_PIECE, inputs=(frame,), as_of_source=frame, warnings=warns)


@registry.register(_FORWARD, module="indicators",
                   summary="Forward outcome of a VCP detection with realistic fills: breakout on a close above the pivot, entry at the next open plus one tick, stop checked on the low (stop first in the entry bar) or on the close (exit at the next open minus one tick), costs deducted, gains measured from the fill. Needs the bars after the detection date.",
                   inputs={"bars": {"type": "dataframe", "required": True, "columns": ["open", "high", "low", "close"]},
                           "detection_date": {"type": "str", "required": True}, "pivot": {"type": "float", "required": True},
                           "stop": {"type": "float", "required": True}, "horizon": {"type": "int", "required": False, "default": 60},
                           "tick": {"type": "float", "required": False, "default": 0.01},
                           "cost_per_share": {"type": "float", "required": False, "default": 0.0},
                           "stop_on": {"type": "str", "required": False, "default": "low"}}, piece_ids=_PIECE)
def vcp_forward_outcome(bars: Any, detection_date: Any, pivot: float, stop: float, horizon: int = 60, tick: float = 0.01,
                        cost_per_share: float = 0.0, stop_on: str = "low") -> QuantResult:
    horizon = limits.as_int("horizon", horizon)
    limits.check_param("horizon", horizon)
    params = {"detection_date": str(detection_date), "pivot": pivot, "stop": stop, "horizon": horizon, "tick": tick,
              "cost_per_share": cost_per_share, "stop_on": stop_on}
    try:
        pivot_f = _base.as_float("pivot", pivot, positive=True)
        stop_f = _base.as_float("stop", stop, positive=True)
        tick_f = _base.as_float("tick", tick, low=0.0)
        cost_f = _base.as_float("cost_per_share", cost_per_share, low=0.0)
        if stop_f >= pivot_f or horizon < 1 or stop_on not in ("low", "close"):
            raise _base.OutOfDomain("need stop below pivot, horizon at least 1 and stop_on low or close")
    except _base.OutOfDomain as exc:
        return empty(_FORWARD, "return", "vcp_forward", str(exc), params=params, piece_ids=_PIECE)
    day = _base.as_date(detection_date)
    frame, dropped, warns = _base.daily_closed(bars, ("open", "high", "low", "close"), as_of=None, min_bars=2)
    if frame is None:
        return empty(_FORWARD, "return", "vcp_forward", warns, params=params, piece_ids=_PIECE)
    after = frame[frame.index.tz_localize(None).normalize() > day] if frame.index.tz is not None else frame[frame.index.normalize() > day]
    after = after.iloc[:horizon + 1]
    closes = after["close"].to_numpy()
    breakout = np.flatnonzero(closes > pivot_f)
    if breakout.size == 0:
        return build(_FORWARD, {"outcome": "no_breakout", "bars_checked": int(len(after))}, unit="return", method="vcp_forward", params=params,
                     piece_ids=_PIECE, inputs=(frame,), as_of_source=frame, warnings=warns)
    b = int(breakout[0])
    if b + 1 >= len(after):
        return build(_FORWARD, {"outcome": "breakout_without_next_bar", "breakout_date": _base.day_text(after.index[b])}, unit="return",
                     method="vcp_forward", params=params, piece_ids=_PIECE, inputs=(frame,), as_of_source=frame, warnings=warns)
    entry_bar = b + 1
    entry = entry_fill(float(after["open"].iloc[entry_bar]), tick_f)
    exit_price, exit_pos, reason = None, None, "open_at_horizon"
    for j in range(entry_bar, len(after)):
        op, lo, cl = float(after["open"].iloc[j]), float(after["low"].iloc[j]), float(after["close"].iloc[j])
        if stop_on == "low":
            if lo <= stop_f:
                exit_price, exit_pos, reason = min(op, stop_f), j, ("stopped_gap" if op <= stop_f else "stopped")
        elif cl < stop_f:
            if j + 1 < len(after):
                exit_price, exit_pos, reason = float(after["open"].iloc[j + 1]) - tick_f, j + 1, "stopped_at_close_exit_next_open"
            else:
                exit_price, exit_pos, reason = None, None, "stop_close_without_next_bar"
        if exit_pos is not None or reason == "stop_close_without_next_bar":
            break
    last_pos = exit_pos if exit_pos is not None else len(after) - 1
    if exit_price is None:
        exit_price = float(after["close"].iloc[last_pos])
    span = after.iloc[entry_bar:last_pos + 1]
    value = {"outcome": reason, "breakout_date": _base.day_text(after.index[b]), "entry_date": _base.day_text(after.index[entry_bar]),
             "entry_price": entry, "exit_date": _base.day_text(after.index[last_pos]), "exit_price": exit_price,
             "return_net": (exit_price - entry - cost_f) / entry, "max_gain": float(span["high"].max() / entry - 1.0),
             "max_loss": float(span["low"].min() / entry - 1.0), "cost_per_share": cost_f, "measured_from": "fill"}
    return build(_FORWARD, value, unit="return", method="vcp_forward", params=params, piece_ids=_PIECE, inputs=(frame,), as_of_source=frame, warnings=warns)

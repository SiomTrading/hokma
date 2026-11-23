"""Distribution day monitor over daily closed bars of an index or an ETF that stands in for it (SPEC-019: CTS-028).

A distribution day is a close that falls at least drop_pct (0.2 percent) with more volume than the day before. It leaves the
count after session_window (25) sessions or when a later close is rally_pct (5 percent) above its close. d5, d15 and d25 count
the days still active among the last 5, 15 and 25 sessions. The risk level (NORMAL, CAUTION, HIGH, SEVERE) comes from d25 and
is moved one step by the position of the close against the 21 day EMA and the 50 day SMA.

Missing volume is a warning, never a distribution day. The count thresholds and the exposure policy that people attach to the
levels are heuristics that nobody has calibrated: the result says so. An ETF is a proxy for the index, not the index.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import limits, registry
from hokma.quant.indicators import _lib
from hokma.quant.indicators._common import build, empty
from hokma.quant.indicators.screens import _base
from hokma.quant.types import QuantResult

_PIECE = ("CTS-028",)
_NAME = "indicators.screens.distribution_days"
_LEVELS = ("NORMAL", "CAUTION", "HIGH", "SEVERE")
_EPS = 1e-9


def distribution_flags(close: pd.Series, volume: pd.Series, drop_pct: float) -> pd.Series:
    """True on the bars that close down by at least drop_pct on more volume than the bar before (missing volume gives False)."""
    change = np.round(close / close.shift(1) - 1.0, 10)
    more_volume = (volume > volume.shift(1)).fillna(False)
    return (change <= -drop_pct + _EPS) & more_volume


def risk_level(d25: int, caution: int, high: int, severe: int, step: int) -> str:
    base = 0 if d25 < caution else 1 if d25 < high else 2 if d25 < severe else 3
    return _LEVELS[max(0, min(3, base + step))]


@registry.register(_NAME, module="indicators",
                   summary="Distribution day monitor on closed daily bars: distribution days (close down 0.2 percent or more on higher volume), expiry at 25 sessions or a 5 percent rally, counts d5, d15, d25 and the risk level NORMAL, CAUTION, HIGH or SEVERE. Thresholds are uncalibrated heuristics; missing volume is a warning.",
                   inputs={"bars": {"type": "dataframe", "required": True, "columns": ["close", "volume"]},
                           "as_of": {"type": "str", "required": False}, "drop_pct": {"type": "float", "required": False, "default": 0.002},
                           "rally_pct": {"type": "float", "required": False, "default": 0.05},
                           "session_window": {"type": "int", "required": False, "default": 25},
                           "caution": {"type": "int", "required": False, "default": 3}, "high": {"type": "int", "required": False, "default": 5},
                           "severe": {"type": "int", "required": False, "default": 7},
                           "use_ma_filter": {"type": "bool", "required": False, "default": True},
                           "is_etf_proxy": {"type": "bool", "required": False, "default": True},
                           "max_bar_age": {"type": "int", "required": False, "default": 2}}, piece_ids=_PIECE)
def distribution_days(bars: Any, as_of: Any = None, drop_pct: float = 0.002, rally_pct: float = 0.05, session_window: int = 25,
                      caution: int = 3, high: int = 5, severe: int = 7, use_ma_filter: bool = True, is_etf_proxy: bool = True,
                      max_bar_age: int = 2) -> QuantResult:
    session_window, caution, high, severe, max_bar_age = (limits.as_int(n, v) for n, v in (
        ("session_window", session_window), ("caution", caution), ("high", high), ("severe", severe), ("max_bar_age", max_bar_age)))
    params = {"drop_pct": drop_pct, "rally_pct": rally_pct, "session_window": session_window, "caution": caution, "high": high,
              "severe": severe, "use_ma_filter": bool(use_ma_filter), "is_etf_proxy": bool(is_etf_proxy)}
    try:
        drop_pct = _base.as_float("drop_pct", drop_pct, positive=True)
        rally_pct = _base.as_float("rally_pct", rally_pct, positive=True)
        if session_window < 5 or not 1 <= caution <= high <= severe or max_bar_age < 0:
            raise _base.OutOfDomain("need session_window >= 5 and 1 <= caution <= high <= severe")
    except _base.OutOfDomain as exc:
        return empty(_NAME, "level", "distribution_days", str(exc), params=params, piece_ids=_PIECE)
    frame, dropped, warns = _base.daily_closed(bars, ("close",), as_of=as_of, min_bars=session_window + 2, optional=("volume",))
    params["dropped_open_bars"] = dropped
    if frame is None:
        return empty(_NAME, "level", "distribution_days", warns, params=params, piece_ids=_PIECE)
    stale = _base.stale_reason(frame, as_of, max_bar_age)
    if stale:
        return empty(_NAME, "level", "distribution_days", warns + (stale,), params=params, piece_ids=_PIECE)
    if "volume" not in frame.columns or frame["volume"].iloc[-(session_window + 1):].isna().all():
        return empty(_NAME, "level", "distribution_days", warns + ("volume is missing for the whole window: no distribution day can be counted",),
                     params=params, piece_ids=_PIECE)
    out_warns = list(warns)
    volume = frame["volume"]
    missing = int(volume.iloc[-(session_window + 1):].isna().sum())
    if missing:
        out_warns.append(f"volume missing on {missing} bar(s) of the window: they are not counted as distribution days")
    if (volume.dropna() < 0).any():
        return empty(_NAME, "level", "distribution_days", warns + ("volume cannot be negative",), params=params, piece_ids=_PIECE)
    close = frame["close"]
    flags = distribution_flags(close, volume, drop_pct)
    last = len(close) - 1
    days = []
    for pos in np.flatnonzero(flags.to_numpy()):
        age = last - int(pos)
        if age >= 2 * session_window:
            continue
        later_max = float(close.iloc[pos + 1:].max()) if pos < last else float("nan")
        rallied = bool(later_max >= float(close.iloc[pos]) * (1.0 + rally_pct) - _EPS) if pos < last else False
        status = "rallied_out" if rallied else "expired" if age >= session_window else "active"
        days.append({"date": _base.day_text(close.index[pos]), "age": age, "status": status})
    active = [d for d in days if d["status"] == "active"]
    d5, d15, d25 = (sum(1 for d in active if d["age"] < w) for w in (5, 15, session_window))
    step, filter_note = 0, "off"
    if use_ma_filter:
        if len(close) >= 50:
            ema21 = float(_lib.ta_overlap.get_exponential_moving_average(close, 21).iloc[-1])
            sma50 = float(close.iloc[-50:].mean())
            above, below = close.iloc[-1] > ema21 and close.iloc[-1] > sma50, close.iloc[-1] < ema21 and close.iloc[-1] < sma50
            step = -1 if above else 1 if below else 0
            filter_note = "above_both" if above else "below_both" if below else "between"
        else:
            filter_note = "skipped_under_50_bars"
            out_warns.append("moving average filter skipped: fewer than 50 bars")
    out_warns.append("thresholds_uncalibrated: the count thresholds and any exposure policy tied to the levels are heuristics")
    if is_etf_proxy:
        out_warns.append("etf_proxy_not_index: an ETF stands in for the index and its volume is not the index volume")
    value = {"bar_date": _base.day_text(close.index[-1]), "d5": d5, "d15": d15, "d25": d25,
             "risk": risk_level(d25, caution, high, severe, step), "base_risk": risk_level(d25, caution, high, severe, 0),
             "ma_filter": filter_note, "distribution_days": days}
    return build(_NAME, value, unit="level", method="distribution_days", params=params, piece_ids=_PIECE, inputs=(frame,),
                 as_of_source=frame, warnings=tuple(out_warns))

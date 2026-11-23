"""Overlap indicators: moving averages, VWAP, parabolic SAR, pivots, support and resistance, Fibonacci (SPEC-019: FT-079).

Defaults are the windows of the SWO line (several day averages); "line" scales every window (DT short, LP long).
The session VWAP works only on intraday bars: daily bars give an empty result. Pivots, support and resistance are causal:
a pivot level uses the previous bar and a swing level is published only after its confirmation bars have closed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from hokma.quant.indicators import _lib
from hokma.quant.indicators._common import indicator

_PIECE = ("FT-079",)
_O = _lib.ta_overlap


@indicator("sma", _PIECE, ("close",), unit="price", defaults={"window": 50},
           summary="Simple moving average of the close.")
def sma(frame: pd.DataFrame, window: int) -> pd.Series:
    return _O.get_moving_average(frame["close"], window)


@indicator("ema", _PIECE, ("close",), unit="price", defaults={"window": 21},
           summary="Exponential moving average of the close, seeded with the simple average of the first window.")
def ema(frame: pd.DataFrame, window: int) -> pd.Series:
    return _O.get_exponential_moving_average(frame["close"], window)


@indicator("wma", _PIECE, ("close",), unit="price", defaults={"window": 20},
           summary="Linearly weighted moving average of the close (the newest bar has the largest weight).")
def wma(frame: pd.DataFrame, window: int) -> pd.Series:
    return _O.get_weighted_moving_average(frame["close"], window)


@indicator("kama", _PIECE, ("close",), unit="price", defaults={"window": 10, "fast_window": 2, "slow_window": 30},
           summary="Kaufman adaptive moving average: the smoothing constant follows the efficiency ratio of the window.")
def kama(frame: pd.DataFrame, window: int, fast_window: int, slow_window: int) -> pd.Series:
    if fast_window >= slow_window:
        raise ValueError("fast_window must be below slow_window")
    return _O.get_kaufman_adaptive_moving_average(frame["close"], window, fast_window, slow_window)


@indicator("hull", _PIECE, ("close",), unit="price", defaults={"window": 20},
           summary="Hull moving average: weighted average of (2 times the half window WMA minus the full window WMA) over sqrt(window).")
def hull(frame: pd.DataFrame, window: int) -> pd.Series:
    return _O.get_hull_moving_average(frame["close"], window)


@indicator("vwma", _PIECE, ("close", "volume"), unit="price", defaults={"window": 20},
           summary="Volume weighted moving average of the close over the window.")
def vwma(frame: pd.DataFrame, window: int) -> pd.Series:
    volume = frame["volume"].rolling(window).sum()
    return (frame["close"] * frame["volume"]).rolling(window).sum() / volume.where(volume > 0)


@indicator("session_vwap", _PIECE, ("high", "low", "close", "volume"), unit="price",
           summary="Session VWAP of intraday bars: cumulative typical price times volume over cumulative volume, restarted each session date. Daily bars are refused.")
def session_vwap(frame: pd.DataFrame) -> pd.Series:
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex):
        raise ValueError("session VWAP needs a datetime index of intraday bars")
    days = pd.Series(index.normalize(), index=index)
    if days.groupby(days).size().max() < 2 or index.to_series().diff().median() >= pd.Timedelta(hours=12):
        raise ValueError("session VWAP needs intraday bars (several bars per session), got daily or slower bars")
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    flow = (typical * frame["volume"]).groupby(days.to_numpy()).cumsum()
    volume = frame["volume"].groupby(days.to_numpy()).cumsum()
    return (flow / volume.where(volume > 0)).rename("vwap")


@indicator("parabolic_sar", _PIECE, ("high", "low", "close"), unit="price", defaults={"af_start": 0.02, "af_increment": 0.02, "af_max": 0.2},
           summary="Parabolic SAR: trailing stop-and-reverse level that accelerates with each new extreme.")
def parabolic_sar(frame: pd.DataFrame, af_start: float, af_increment: float, af_max: float) -> pd.DataFrame:
    if af_start > af_max:
        raise ValueError("af_start must not be above af_max")
    sar = _O.get_parabolic_sar(frame["high"], frame["low"], af_start, af_increment, af_max)
    return pd.DataFrame({"sar": sar, "trend": np.where(sar < frame["close"], 1.0, -1.0)}).where(sar.notna())


@indicator("pivot_points", _PIECE, ("high", "low", "close"), unit="price",
           summary="Classic pivot point with three supports and three resistances, computed from the previous bar only (no look ahead).")
def pivot_points(frame: pd.DataFrame) -> pd.DataFrame:
    out = _O.get_pivot_points(frame["high"], frame["low"], frame["close"])
    out.columns = ["pivot", "r1", "r2", "r3", "s1", "s2", "s3"]
    return out


@indicator("support_resistance", _PIECE, ("close",), unit="price", defaults={"window": 5, "sensitivity": 0.05},
           summary="Support and resistance levels from swing extremes of the close; a level is published only after window later bars confirmed it.")
def support_resistance(frame: pd.DataFrame, window: int, sensitivity: float) -> pd.DataFrame:
    out = _O.get_support_resistance_levels(frame["close"], window, sensitivity)
    out.columns = ["resistance", "support"]
    return out


@indicator("fibonacci", _PIECE, ("high", "low"), unit="price", defaults={"window": 60, "trend": "uptrend"},
           summary="Fibonacci retracement levels (0, 23.6, 38.2, 50, 61.8, 78.6 and 100 percent) of the swing between the window low and high.")
def fibonacci(frame: pd.DataFrame, window: int, trend: str) -> pd.DataFrame:
    if trend not in ("uptrend", "downtrend"):
        raise ValueError("trend must be uptrend or downtrend")
    high = frame["high"].rolling(window).max()
    low = frame["low"].rolling(window).min()
    out = _O.get_fibonacci_retracement_levels(high, low, None, trend)
    out.columns = [f"level_{c.replace('%', '').replace('.', '_')}" for c in out.columns]
    return out

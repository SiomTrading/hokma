"""Momentum indicators over daily bars (SPEC-019: FT-078).

Defaults are the windows of the SWO line (daily bars); "line" scales every window (DT short, LP long) and an explicit window wins.
Functions register as "indicators.<name>" and return the latest row plus the last_n rows.
"""
from __future__ import annotations

import pandas as pd

from hokma.quant.indicators import _lib
from hokma.quant.indicators._common import indicator

_PIECE = ("FT-078",)
_HLC = ("high", "low", "close")
_M = _lib.ta_momentum


@indicator("rsi", _PIECE, ("close",), unit="index", defaults={"window": 14},
           summary="Relative strength index with Wilder smoothing, 0 to 100.")
def rsi(frame: pd.DataFrame, window: int) -> pd.Series:
    return _M.get_relative_strength_index(frame["close"], window)


@indicator("macd", _PIECE, ("close",), unit="price", defaults={"short_window": 12, "long_window": 26, "signal_window": 9},
           summary="MACD line (short EMA minus long EMA of the close), its signal line and the histogram.")
def macd(frame: pd.DataFrame, short_window: int, long_window: int, signal_window: int) -> pd.DataFrame:
    if short_window >= long_window:
        raise ValueError("short_window must be below long_window")
    out = _M.get_moving_average_convergence_divergence(frame["close"], short_window, long_window, signal_window)
    out.columns = ["macd", "signal"]
    out["histogram"] = out["macd"] - out["signal"]
    return out


@indicator("stochastic", _PIECE, _HLC, unit="index", defaults={"window": 14, "smooth_window": 3},
           summary="Stochastic oscillator: %K (close within the window range, 0 to 100) and %D (its simple average).")
def stochastic(frame: pd.DataFrame, window: int, smooth_window: int) -> pd.DataFrame:
    out = _M.get_stochastic_oscillator(frame["high"], frame["low"], frame["close"], window, smooth_window)
    out.columns = ["k", "d"]
    return out


@indicator("stochastic_rsi", _PIECE, ("close",), unit="index", defaults={"window": 14, "stochastic_window": 14, "smooth_window": 3},
           summary="Stochastic oscillator applied to the RSI: %K and %D, 0 to 100.")
def stochastic_rsi(frame: pd.DataFrame, window: int, stochastic_window: int, smooth_window: int) -> pd.DataFrame:
    out = _M.get_stochastic_relative_strength_index(frame["close"], window, stochastic_window, smooth_window)
    out.columns = ["k", "d"]
    return out


@indicator("williams_r", _PIECE, _HLC, unit="index", defaults={"window": 14},
           summary="Williams %R: close within the window range from -100 (at the low) to 0 (at the high).")
def williams_r(frame: pd.DataFrame, window: int) -> pd.Series:
    return _M.get_williams_percent_r(frame["high"], frame["low"], frame["close"], window)


@indicator("aroon", _PIECE, ("high", "low"), unit="index", defaults={"window": 25},
           summary="Aroon up and down: 100 times the bars left in the window since the highest high and lowest low, over the window.")
def aroon(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    out = _M.get_aroon_indicator(frame["high"], frame["low"], window)
    out.columns = ["up", "down"]
    out["oscillator"] = out["up"] - out["down"]
    return out


@indicator("cci", _PIECE, _HLC, unit="index", defaults={"window": 20, "constant": 0.015},
           summary="Commodity channel index: typical price minus its average, over the constant times the mean absolute deviation.")
def cci(frame: pd.DataFrame, window: int, constant: float) -> pd.Series:
    return _M.get_commodity_channel_index(frame["high"], frame["low"], frame["close"], window, constant)


@indicator("adx", _PIECE, _HLC, unit="index", defaults={"window": 14},
           summary="Average directional index with Wilder smoothing: trend strength from 0 to 100, with +DI and -DI.")
def adx(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    strength = _M.get_average_directional_index(frame["high"], frame["low"], frame["close"], window)
    up, down = frame["high"].diff(), -frame["low"].diff()
    plus = up.where((up > down) & (up > 0), 0.0)
    minus = down.where((down > up) & (down > 0), 0.0)
    tr = _lib.ta_volatility.get_true_range(frame["high"], frame["low"], frame["close"]).where(frame["close"].shift(1).notna())
    smooth = _lib.ta_volatility.get_wilder_moving_average
    atr = smooth(tr, window)
    return pd.DataFrame({"adx": strength, "plus_di": 100 * smooth(plus.where(up.notna()), window) / atr,
                         "minus_di": 100 * smooth(minus.where(down.notna()), window) / atr})


@indicator("ichimoku", _PIECE, ("high", "low", "close"), unit="price",
           defaults={"conversion_window": 9, "base_window": 26, "lead_span_b_window": 52},
           summary="Ichimoku cloud: conversion line, base line, leading spans A and B (shifted forward by the base window) and the lagging close.")
def ichimoku(frame: pd.DataFrame, conversion_window: int, base_window: int, lead_span_b_window: int) -> pd.DataFrame:
    out = _M.get_ichimoku_cloud(frame["high"], frame["low"], conversion_window, base_window, lead_span_b_window)
    out.columns = ["conversion", "base", "span_a", "span_b"]
    out["lagging_close"] = frame["close"]
    return out


@indicator("mfi", _PIECE, ("high", "low", "close", "volume"), unit="index", defaults={"window": 14},
           summary="Money flow index: volume weighted RSI of the typical price, 0 to 100.")
def mfi(frame: pd.DataFrame, window: int) -> pd.Series:
    return _M.get_money_flow_index(frame["high"], frame["low"], frame["close"], frame["volume"], window)


@indicator("roc", _PIECE, ("close",), unit="fraction", defaults={"window": 12},
           summary="Rate of change: close over the close window bars ago, minus one.")
def roc(frame: pd.DataFrame, window: int) -> pd.Series:
    return _M.get_rate_of_change(frame["close"], window)


@indicator("awesome_oscillator", _PIECE, ("high", "low"), unit="price", defaults={"short_window": 5, "long_window": 34},
           summary="Awesome oscillator: short minus long simple average of the bar midpoint.")
def awesome_oscillator(frame: pd.DataFrame, short_window: int, long_window: int) -> pd.Series:
    if short_window >= long_window:
        raise ValueError("short_window must be below long_window")
    return _M.get_awesome_oscillator(frame["high"], frame["low"], short_window, long_window)


@indicator("chande_momentum", _PIECE, ("close",), unit="index", defaults={"window": 14},
           summary="Chande momentum oscillator: (sum of gains minus sum of losses) over their total, -100 to 100.")
def chande_momentum(frame: pd.DataFrame, window: int) -> pd.Series:
    return _M.get_chande_momentum_oscillator(frame["close"], window)


@indicator("ultimate_oscillator", _PIECE, _HLC, unit="index", defaults={"window_1": 7, "window_2": 14, "window_3": 28},
           summary="Ultimate oscillator: weighted blend of buying pressure over three windows, 0 to 100.")
def ultimate_oscillator(frame: pd.DataFrame, window_1: int, window_2: int, window_3: int) -> pd.Series:
    return _M.get_ultimate_oscillator(frame["high"], frame["low"], frame["close"], window_1, window_2, window_3)


@indicator("true_strength_index", _PIECE, ("close",), unit="index", defaults={"long_window": 25, "short_window": 13, "signal_window": 13},
           summary="True strength index: double smoothed momentum over double smoothed absolute momentum, with its signal line.")
def true_strength_index(frame: pd.DataFrame, long_window: int, short_window: int, signal_window: int) -> pd.DataFrame:
    out = _M.get_true_strength_index(frame["close"], long_window, short_window, signal_window)
    out.columns = ["tsi", "signal"]
    return out


@indicator("ppo", _PIECE, ("close",), unit="fraction", defaults={"short_window": 12, "long_window": 26},
           summary="Percentage price oscillator: (short EMA minus long EMA) over the long EMA, as a fraction (0.01 is one percent).")
def ppo(frame: pd.DataFrame, short_window: int, long_window: int) -> pd.Series:
    if short_window >= long_window:
        raise ValueError("short_window must be below long_window")
    return _M.get_percentage_price_oscillator(frame["close"], short_window, long_window)


@indicator("vortex", _PIECE, _HLC, unit="ratio", defaults={"window": 14},
           summary="Vortex indicator: positive and negative trend movement over the true range.")
def vortex(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    out = _M.get_vortex_indicator(frame["high"], frame["low"], frame["close"], window)
    out.columns = ["plus", "minus"]
    return out


@indicator("balance_of_power", _PIECE, ("open", "high", "low", "close"), unit="ratio",
           summary="Balance of power: (close minus open) over the bar range, -1 to 1.")
def balance_of_power(frame: pd.DataFrame) -> pd.Series:
    return _M.get_balance_of_power(frame["open"], frame["high"], frame["low"], frame["close"])


@indicator("force_index", _PIECE, ("close", "volume"), unit="volume", defaults={"window": 13},
           summary="Force index: close change times volume, smoothed over the window.")
def force_index(frame: pd.DataFrame, window: int) -> pd.Series:
    return _M.get_force_index(frame["close"], frame["volume"], window)


@indicator("choppiness", _PIECE, _HLC, unit="index", defaults={"window": 14},
           summary="Choppiness index: 100 means a sideways market, low values mean a trend (log of true range sum over the window range).")
def choppiness(frame: pd.DataFrame, window: int) -> pd.Series:
    return _M.get_choppiness_index(frame["high"], frame["low"], frame["close"], window)

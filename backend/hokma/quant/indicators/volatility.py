"""Volatility indicators: true range, ATR, supertrend, Keltner, Donchian, Bollinger and the volatility cone (SPEC-019: FT-080).

Defaults are the windows of the SWO line; "line" scales every window (DT and SWC use ATR for size and stop, LP uses Bollinger as context).
The range based estimators of volatility (Yang-Zhang, Rogers-Satchell) live in the statistics module and are not repeated here.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from hokma.quant import limits
from hokma.quant.indicators import _lib
from hokma.quant.indicators._common import indicator

_PIECE = ("FT-080",)
_V = _lib.ta_volatility
_HLC = ("high", "low", "close")


@indicator("true_range", _PIECE, _HLC, unit="price",
           summary="True range: the largest of high minus low and the distance from the previous close to the high and to the low.")
def true_range(frame: pd.DataFrame) -> pd.Series:
    return _V.get_true_range(frame["high"], frame["low"], frame["close"]).where(frame["close"].shift(1).notna())


@indicator("atr", _PIECE, _HLC, unit="price", defaults={"window": 14},
           summary="Average true range with Wilder smoothing, for position size and stop distance.")
def atr(frame: pd.DataFrame, window: int) -> pd.Series:
    return _V.get_average_true_range(frame["high"], frame["low"], frame["close"], window)


@indicator("supertrend", _PIECE, _HLC, unit="price", defaults={"window": 10, "multiplier": 3.0},
           summary="Supertrend line (ATR bands that only tighten in the trend direction) and the trend direction, 1 up and -1 down.")
def supertrend(frame: pd.DataFrame, window: int, multiplier: float) -> pd.DataFrame:
    out = _V.get_supertrend(frame["high"], frame["low"], frame["close"], window, multiplier)
    out.columns = ["supertrend", "direction"]
    return out


@indicator("keltner", _PIECE, _HLC, unit="price", defaults={"window": 20, "atr_window": 10, "atr_multiplier": 2.0},
           summary="Keltner channels: EMA of the close plus and minus a multiple of the ATR.")
def keltner(frame: pd.DataFrame, window: int, atr_window: int, atr_multiplier: float) -> pd.DataFrame:
    out = _V.get_keltner_channels(frame["high"], frame["low"], frame["close"], window, atr_window, atr_multiplier)
    out.columns = ["upper", "middle", "lower"]
    return out


@indicator("donchian", _PIECE, ("high", "low"), unit="price", defaults={"window": 20},
           summary="Donchian channels of the previous window bars (the current bar is excluded, so a close above the upper line is a breakout).")
def donchian(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    out = _V.get_donchian_channels(frame["high"], frame["low"], window)
    out.columns = ["upper", "middle", "lower"]
    return out


@indicator("bollinger", _PIECE, ("close",), unit="price", defaults={"window": 20, "num_std_dev": 2.0},
           summary="Bollinger bands: simple average of the close plus and minus a multiple of the population standard deviation, with %B and bandwidth.")
def bollinger(frame: pd.DataFrame, window: int, num_std_dev: float) -> pd.DataFrame:
    bands = _V.get_bollinger_bands(frame["close"], window, num_std_dev)
    extra = _V.get_bollinger_percent_b_and_bandwidth(frame["close"], window, num_std_dev)
    return pd.DataFrame({"upper": bands["Upper Band"], "middle": bands["Middle Band"], "lower": bands["Lower Band"],
                         "percent_b": extra["%B"], "bandwidth": extra["BandWidth"]})


@indicator("chandelier_exit", _PIECE, _HLC, unit="price", defaults={"window": 22, "multiplier": 3.0},
           summary="Chandelier exit: highest high minus a multiple of the ATR for longs, lowest low plus it for shorts.")
def chandelier_exit(frame: pd.DataFrame, window: int, multiplier: float) -> pd.DataFrame:
    out = _V.get_chandelier_exit(frame["high"], frame["low"], frame["close"], window, multiplier)
    out.columns = ["long_exit", "short_exit"]
    return out


@indicator("volatility_cone", _PIECE, ("close",), unit="volatility", defaults={"window": 20, "periods_per_year": 252.0},
           summary="Volatility cone: annualised realised volatility of the window against the minimum, 10th, 25th, median, 75th, 90th percentile and maximum of its own history.")
def volatility_cone(frame: pd.DataFrame, window: int, periods_per_year: float) -> pd.DataFrame:
    limits.check_param("window", window)
    log_returns = np.log(frame["close"] / frame["close"].shift(1))
    realised = log_returns.rolling(window).std() * np.sqrt(periods_per_year)
    history = realised.dropna()
    if len(history) < 2:
        raise ValueError("need more bars than the window to build a cone")
    out = pd.DataFrame(index=frame.index)
    out["realised"] = realised
    for label, q in (("min", 0.0), ("p10", 0.10), ("p25", 0.25), ("median", 0.50), ("p75", 0.75), ("p90", 0.90), ("max", 1.0)):
        out[label] = float(history.quantile(q))
    out["percentile_rank"] = float((history <= history.iloc[-1]).mean())
    return out


# FM-09 and FM-10: the range volatility estimators live once in the stats module; the indicators module registers the same
# function objects under its own names (an alias, not a second implementation).
from hokma.quant import registry as _registry  # noqa: E402
from hokma.quant.stats import volatility as _stats_volatility  # noqa: E402

for _alias, _target in _stats_volatility.ALIASES.items():
    _spec = _registry.describe("stats." + _target)
    _registry.register(_alias, module="indicators", summary=_spec.summary, inputs=_spec.inputs,
                       piece_ids=_spec.piece_ids)(_spec.fn)

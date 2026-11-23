"""Breadth and volume flow indicators (SPEC-019: FT-077).

Panel indicators take a table of closes (rows = dates, columns = the symbols of the market) and, for TRIN, a table of volumes.
Single instrument indicators take daily bars. Defaults are the parameters of the SWO line; "line" scales every window
(DT short, SWC and SWO daily, LP long) and an explicit window always wins. Functions register as "indicators.<name>".
"""
from __future__ import annotations

import pandas as pd

from hokma.quant.indicators import _lib
from hokma.quant.indicators._common import indicator, panel_indicator

_PIECE = ("FT-077",)


@panel_indicator("advance_decline", _PIECE, unit="count",
                 summary="Advancers, decliners, unchanged, net advances, the advance-decline line and their ratio across a market panel of closes.")
def advance_decline(closes: pd.DataFrame) -> pd.DataFrame:
    changes = closes.diff()
    advancers = (changes > 0).sum(axis=1)
    decliners = (changes < 0).sum(axis=1)
    unchanged = (changes == 0).sum(axis=1)
    net = advancers - decliners
    out = pd.DataFrame({"advancers": advancers, "decliners": decliners, "unchanged": unchanged, "net_advances": net,
                        "ad_line": net.iloc[1:].cumsum().reindex(net.index),
                        "ad_ratio": advancers / decliners.where(decliners > 0)})
    return out.iloc[1:]


@panel_indicator("mcclellan", _PIECE, unit="count", defaults={"short_window": 19, "long_window": 39, "ratio_adjusted": True},
                 summary="McClellan oscillator (short EMA minus long EMA of net advances, ratio adjusted per 1,000 issues by default) and its summation index.")
def mcclellan(closes: pd.DataFrame, short_window: int, long_window: int, ratio_adjusted: bool) -> pd.DataFrame:
    if short_window >= long_window:
        raise ValueError("short_window must be below long_window")
    changes = closes.diff().iloc[1:]
    advancers = (changes > 0).sum(axis=1)
    decliners = (changes < 0).sum(axis=1)
    net = (advancers - decliners).astype(float)
    if ratio_adjusted:
        total = (advancers + decliners).where((advancers + decliners) > 0)
        net = (net / total * 1000.0).fillna(0.0)
    oscillator = (_lib.ta_overlap.get_exponential_moving_average(net, short_window)
                  - _lib.ta_overlap.get_exponential_moving_average(net, long_window))
    return pd.DataFrame({"net_advances": net, "oscillator": oscillator, "summation_index": oscillator.cumsum()})


@panel_indicator("trin", _PIECE, with_volumes=True, unit="ratio",
                 summary="TRIN (Arms index): advancing over declining issues divided by advancing over declining volume. Undefined when an input of the ratio is zero.")
def trin(closes: pd.DataFrame, volumes: pd.DataFrame) -> pd.Series:
    return _lib.ta_breadth.get_trin(closes, volumes).iloc[1:]


@panel_indicator("new_highs_lows", _PIECE, unit="count", defaults={"window": 252}, min_rows=3,
                 summary="Number of symbols that close above their previous window high, below their previous window low, and the net.")
def new_highs_lows(closes: pd.DataFrame, window: int) -> pd.DataFrame:
    table = _lib.ta_breadth.get_market_breadth(closes, high_low_window=window)
    return pd.DataFrame({"new_highs": table["New Highs"], "new_lows": table["New Lows"],
                         "net": table["New Highs"] - table["New Lows"]})


@panel_indicator("percent_above_average", _PIECE, unit="fraction", defaults={"window": 50},
                 summary="Share of symbols whose close is above their own simple moving average of the window (0 to 1).")
def percent_above_average(closes: pd.DataFrame, window: int) -> pd.Series:
    average = closes.rolling(window).mean()
    counted = average.notna() & closes.notna()
    above = (closes > average).where(counted)
    return (above.sum(axis=1) / counted.sum(axis=1).where(counted.sum(axis=1) > 0)).rename("share")


@indicator("obv", _PIECE, ("close", "volume"), unit="volume",
           summary="On-balance volume: cumulative volume signed by the direction of the close.")
def obv(frame: pd.DataFrame) -> pd.Series:
    return _lib.ta_breadth.get_on_balance_volume(frame["close"], frame["volume"])


@indicator("accumulation_distribution", _PIECE, ("high", "low", "close", "volume"), unit="volume",
           summary="Accumulation/distribution line: cumulative money flow volume (close location value times volume).")
def accumulation_distribution(frame: pd.DataFrame) -> pd.Series:
    return _lib.ta_breadth.get_accumulation_distribution_line(frame["high"], frame["low"], frame["close"], frame["volume"])


@indicator("chaikin_oscillator", _PIECE, ("high", "low", "close", "volume"), unit="volume",
           defaults={"short_window": 3, "long_window": 10},
           summary="Chaikin oscillator: short EMA minus long EMA of the accumulation/distribution line.")
def chaikin_oscillator(frame: pd.DataFrame, short_window: int, long_window: int) -> pd.Series:
    if short_window >= long_window:
        raise ValueError("short_window must be below long_window")
    return _lib.ta_breadth.get_chaikin_oscillator(frame["high"], frame["low"], frame["close"], frame["volume"], short_window, long_window)


@indicator("chaikin_money_flow", _PIECE, ("high", "low", "close", "volume"), unit="ratio", defaults={"window": 20},
           summary="Chaikin money flow: windowed money flow volume over windowed volume, between -1 and 1.")
def chaikin_money_flow(frame: pd.DataFrame, window: int) -> pd.Series:
    return _lib.ta_breadth.get_chaikin_money_flow(frame["high"], frame["low"], frame["close"], frame["volume"], window)


@indicator("negative_volume_index", _PIECE, ("close", "volume"), unit="index", defaults={"start_value": 1000.0},
           summary="Negative volume index: compounds the price change only on days when volume fell from the day before.")
def negative_volume_index(frame: pd.DataFrame, start_value: float) -> pd.Series:
    return _lib.ta_breadth.get_negative_volume_index(frame["close"], frame["volume"], start_value)


@indicator("positive_volume_index", _PIECE, ("close", "volume"), unit="index", defaults={"start_value": 1000.0},
           summary="Positive volume index: compounds the price change only on days when volume rose from the day before.")
def positive_volume_index(frame: pd.DataFrame, start_value: float) -> pd.Series:
    return _lib.ta_breadth.get_positive_volume_index(frame["close"], frame["volume"], start_value)


@indicator("price_volume_trend", _PIECE, ("close", "volume"), unit="volume",
           summary="Price volume trend: cumulative volume weighted by the percentage change of the close.")
def price_volume_trend(frame: pd.DataFrame) -> pd.Series:
    return _lib.ta_breadth.get_price_volume_trend(frame["close"], frame["volume"])


@indicator("ease_of_movement", _PIECE, ("high", "low", "volume"), unit="value", defaults={"window": 14, "volume_divisor": 100000000.0},
           summary="Ease of movement: midpoint move divided by the box ratio (scaled volume over range), averaged over the window.")
def ease_of_movement(frame: pd.DataFrame, window: int, volume_divisor: float) -> pd.Series:
    return _lib.ta_breadth.get_ease_of_movement(frame["high"], frame["low"], frame["volume"], window, volume_divisor)

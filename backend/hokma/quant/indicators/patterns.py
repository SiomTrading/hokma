"""Candlestick patterns for swing trading: doji, hammer and engulfing (SPEC-020: V-42).

A pattern needs a complete candle: it is read only on closed daily bars (a bar is closed once 16:00 New York of its date has passed
at as_of; a boolean closed column also applies), never on the candle that is still open. A pattern is known only when its candle
closes, so each one is registered with a lag of one bar: the earliest action is the next open.

The shapes are written from their usual definitions, with the thresholds as parameters. They judge the shape only: the trend before
the candle (a hammer after a decline, for example) is a separate decision of the strategy.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from hokma.quant.indicators.catalog import register_indicator

_PIECE = ("V-42",)
_OHLC = ("open", "high", "low", "close")
LAG_BARS = 1


def _flag(mask: pd.Series) -> pd.Series:
    return mask.astype(float)


def doji(frame: pd.DataFrame, body_ratio: float) -> pd.Series:
    """1 when the real body is at most body_ratio of the range (open and close nearly equal) and the candle has a range, else 0."""
    rng = frame["high"] - frame["low"]
    body = (frame["close"] - frame["open"]).abs()
    return _flag((rng > 0) & (body <= body_ratio * rng))


def hammer(frame: pd.DataFrame, wick_multiple: float, upper_ratio: float) -> pd.Series:
    """1 when the body is not empty, the lower shadow is at least wick_multiple times the body and the upper shadow is at most upper_ratio of the range."""
    top = frame[["open", "close"]].max(axis=1)
    bottom = frame[["open", "close"]].min(axis=1)
    body = top - bottom
    rng = frame["high"] - frame["low"]
    lower = bottom - frame["low"]
    upper = frame["high"] - top
    return _flag((rng > 0) & (body > 0) & (lower >= wick_multiple * body) & (upper <= upper_ratio * rng))


def engulfing(frame: pd.DataFrame) -> pd.Series:
    """1 for a bullish engulfing, -1 for a bearish one, 0 otherwise; the first bar has no previous candle (NaN).

    Bullish: a down candle followed by an up candle whose body covers the previous body (it opens at or below the previous close and closes at
    or above the previous open) and is larger. Bearish is the mirror."""
    o, c = frame["open"], frame["close"]
    po, pc = o.shift(1), c.shift(1)
    bigger = (c - o).abs() > (pc - po).abs()
    bullish = (pc < po) & (c > o) & (o <= pc) & (c >= po) & bigger
    bearish = (pc > po) & (c < o) & (o >= pc) & (c <= po) & bigger
    out = pd.Series(np.where(bullish, 1.0, np.where(bearish, -1.0, 0.0)), index=frame.index)
    return out.where(pc.notna())


candle_doji = register_indicator(
    "candle_doji", doji, pieces=_PIECE, needed=_OHLC, lag_bars=LAG_BARS, unit="flag", closed_only=True, defaults={"body_ratio": 0.1},
    summary="Doji on closed daily candles: 1 when the body is at most body_ratio (default 10 percent) of the range, else 0. Read on closed bars only.")
candle_hammer = register_indicator(
    "candle_hammer", hammer, pieces=_PIECE, needed=_OHLC, lag_bars=LAG_BARS, unit="flag", closed_only=True,
    defaults={"wick_multiple": 2.0, "upper_ratio": 0.1},
    summary="Hammer on closed daily candles: 1 when the lower shadow is at least wick_multiple (default 2) times the body and the upper shadow is at most upper_ratio (default 10 percent) of the range. Shape only: the prior trend is not checked.")
candle_engulfing = register_indicator(
    "candle_engulfing", engulfing, pieces=_PIECE, needed=_OHLC, lag_bars=LAG_BARS, unit="flag", closed_only=True,
    summary="Engulfing on closed daily candles: 1 bullish, -1 bearish, 0 none. The body must cover the previous body and be larger. Shape only: the prior trend is not checked.")

"""Momentum burst screen over daily closed bars (SPEC-019: CTS-004).

Reads the trigger of a one day burst: a close of at least pct_threshold over the previous close with more volume than the
day before, or a gain in dollars of at least dollar_threshold with more volume, or a range larger than the three previous
ranges with the day before not extended. Filters: close near the high of the bar, a contracted range in the base before, no
run of three up days just before, no drop of drop_pct in the last bars.

Fill rule (the source filled at the signal): the signal is read at the close, so the entry is the next open plus one tick;
the stop is the low of the trigger bar, exact, and when one bar both fills the entry and touches the stop the stop is taken
first. The dollar threshold depends on the price scale: the result carries it as a share of the close and warns when it is
far from the percent threshold.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from hokma.quant import registry
from hokma.quant.indicators._common import build, empty
from hokma.quant.indicators.screens import _base
from hokma.quant.types import QuantResult

_PIECE = ("CTS-004",)
_NAME = "indicators.screens.momentum_burst"
_EPS = 1e-9


def scan(frame: pd.DataFrame, *, pct_threshold: float, dollar_threshold: float, range_lookback: int, extended_pct: float,
         close_near_high: float, base_bars: int, base_lookback: int, contraction_ratio: float, run_bars: int, drop_pct: float,
         drop_bars: int) -> pd.DataFrame:
    close, high, low, volume = frame["close"], frame["high"], frame["low"], frame["volume"]
    prev_close = close.shift(1)
    gain = close / prev_close - 1.0
    rng = high - low
    vol_up = volume > volume.shift(1)
    trig_pct = (gain >= pct_threshold - _EPS) & vol_up
    trig_dollar = ((close - prev_close) >= dollar_threshold - _EPS) & vol_up
    prior_max_range = rng.shift(1).rolling(range_lookback).max()
    prior_extended = gain.shift(1) >= extended_pct - _EPS
    trig_range = (rng > prior_max_range) & (close > prev_close) & ~prior_extended.fillna(True) & prior_max_range.notna()
    position = ((close - low) / rng.where(rng > 0)).fillna(0.0)
    near_high = position >= close_near_high - _EPS
    recent = rng.shift(1).rolling(base_bars).mean()
    reference = rng.shift(1 + base_bars).rolling(base_lookback).mean()
    ratio = recent / reference
    base_ok = ratio <= contraction_ratio + _EPS
    up = (close.diff() > 0).astype(float)
    run_fail = up.shift(1).rolling(run_bars).sum() == run_bars
    down_hard = (gain <= -drop_pct + _EPS).astype(float)
    drop_fail = down_hard.shift(1).rolling(drop_bars).sum() > 0
    trigger = (trig_pct | trig_dollar | trig_range).fillna(False)
    signal = trigger & near_high & base_ok.fillna(False) & ~run_fail.fillna(True) & ~drop_fail.fillna(True)
    return pd.DataFrame({"gain": gain, "dollar_gain": close - prev_close, "close_position": position, "base_ratio": ratio,
                         "trigger_pct": trig_pct, "trigger_dollar": trig_dollar, "trigger_range": trig_range.fillna(False),
                         "trigger": trigger, "near_high": near_high, "base_ok": base_ok.fillna(False),
                         "run_fail": run_fail.fillna(True), "drop_fail": drop_fail.fillna(True), "signal": signal})


@registry.register(_NAME, module="indicators",
                   summary="Momentum burst screen on closed daily bars: percent, dollar or range trigger with close near the high, contracted base and failure filters. Entry is the next open plus one tick, stop is the trigger bar low (stop first inside a bar). Returns trigger and signal dates.",
                   inputs={"bars": {"type": "dataframe", "required": True, "columns": ["open", "high", "low", "close", "volume"]},
                           "as_of": {"type": "str", "required": False}, "pct_threshold": {"type": "float", "required": False, "default": 0.04},
                           "dollar_threshold": {"type": "float", "required": False, "default": 0.90},
                           "range_lookback": {"type": "int", "required": False, "default": 3},
                           "extended_pct": {"type": "float", "required": False, "default": 0.04},
                           "close_near_high": {"type": "float", "required": False, "default": 0.7},
                           "base_bars": {"type": "int", "required": False, "default": 5},
                           "base_lookback": {"type": "int", "required": False, "default": 20},
                           "contraction_ratio": {"type": "float", "required": False, "default": 0.8},
                           "run_bars": {"type": "int", "required": False, "default": 3},
                           "drop_pct": {"type": "float", "required": False, "default": 0.04},
                           "drop_bars": {"type": "int", "required": False, "default": 3},
                           "max_bar_age": {"type": "int", "required": False, "default": 2},
                           "tick": {"type": "float", "required": False, "default": 0.01}}, piece_ids=_PIECE)
def momentum_burst(bars: Any, as_of: Any = None, pct_threshold: float = 0.04, dollar_threshold: float = 0.90, range_lookback: int = 3,
                   extended_pct: float = 0.04, close_near_high: float = 0.7, base_bars: int = 5, base_lookback: int = 20,
                   contraction_ratio: float = 0.8, run_bars: int = 3, drop_pct: float = 0.04, drop_bars: int = 3,
                   max_bar_age: int = 2, tick: float = 0.01) -> QuantResult:
    from hokma.quant import limits

    ints = {n: limits.as_int(n, v) for n, v in (("range_lookback", range_lookback), ("base_bars", base_bars), ("base_lookback", base_lookback),
                                                 ("run_bars", run_bars), ("drop_bars", drop_bars), ("max_bar_age", max_bar_age))}
    params = {"pct_threshold": pct_threshold, "dollar_threshold": dollar_threshold, "extended_pct": extended_pct,
              "close_near_high": close_near_high, "contraction_ratio": contraction_ratio, "drop_pct": drop_pct, "tick": tick, **ints}
    try:
        floats = {"pct_threshold": _base.as_float("pct_threshold", pct_threshold, positive=True),
                  "dollar_threshold": _base.as_float("dollar_threshold", dollar_threshold, positive=True),
                  "extended_pct": _base.as_float("extended_pct", extended_pct, positive=True),
                  "close_near_high": _base.as_float("close_near_high", close_near_high, positive=True),
                  "contraction_ratio": _base.as_float("contraction_ratio", contraction_ratio, positive=True),
                  "drop_pct": _base.as_float("drop_pct", drop_pct, positive=True), "tick": _base.as_float("tick", tick, low=0.0)}
        if floats["close_near_high"] > 1.0:
            raise _base.OutOfDomain("close_near_high must be at most 1")
        if any(ints[n] < 1 for n in ("range_lookback", "base_bars", "base_lookback", "run_bars", "drop_bars")) or ints["max_bar_age"] < 0:
            raise _base.OutOfDomain("bar counts must be at least 1")
    except _base.OutOfDomain as exc:
        return empty(_NAME, "signal", "momentum_burst", str(exc), params=params, piece_ids=_PIECE)
    min_bars = ints["base_bars"] + ints["base_lookback"] + 3
    frame, dropped, warns = _base.daily_closed(bars, ("open", "high", "low", "close", "volume"), as_of=as_of, min_bars=min_bars)
    params["dropped_open_bars"] = dropped
    if frame is None:
        return empty(_NAME, "signal", "momentum_burst", warns, params=params, piece_ids=_PIECE)
    stale = _base.stale_reason(frame, as_of, ints["max_bar_age"])
    if stale:
        return empty(_NAME, "signal", "momentum_burst", warns + (stale,), params=params, piece_ids=_PIECE)
    table = scan(frame, **{k: floats[k] for k in ("pct_threshold", "dollar_threshold", "extended_pct", "close_near_high", "contraction_ratio", "drop_pct")},
                 range_lookback=ints["range_lookback"], base_bars=ints["base_bars"], base_lookback=ints["base_lookback"],
                 run_bars=ints["run_bars"], drop_bars=ints["drop_bars"])
    last = table.iloc[-1]
    close, low = float(frame["close"].iloc[-1]), float(frame["low"].iloc[-1])
    share = floats["dollar_threshold"] / close
    out_warns = list(warns)
    if share > 2 * floats["pct_threshold"] or share < 0.5 * floats["pct_threshold"]:
        out_warns.append(f"dollar_threshold_price_scale: {floats['dollar_threshold']} is {share:.1%} of the close, far from the {floats['pct_threshold']:.1%} threshold; the dollar trigger depends on the price scale")
    entry_estimate = close + floats["tick"]
    value = {
        "bar_date": _base.day_text(frame.index[-1]), "signal": bool(last["signal"]), "trigger": bool(last["trigger"]),
        "triggers": {"percent": bool(last["trigger_pct"]), "dollar": bool(last["trigger_dollar"]), "range": bool(last["trigger_range"])},
        "gain": float(last["gain"]), "dollar_gain": float(last["dollar_gain"]), "close_position": float(last["close_position"]),
        "base_ratio": None if pd.isna(last["base_ratio"]) else float(last["base_ratio"]),
        "filters": {"near_high": bool(last["near_high"]), "base_contracted": bool(last["base_ok"]),
                    "run_of_up_days": bool(last["run_fail"]), "recent_drop": bool(last["drop_fail"])},
        "dollar_threshold_share_of_close": share,
        "entry_rule": "next_open_plus_1_tick", "tick": floats["tick"], "stop": low, "stop_rule": "trigger_bar_low_exact",
        "same_bar_priority": "stop_first", "entry_estimate": entry_estimate, "entry_is_estimate": True,
        "risk_per_share_estimate": entry_estimate - low,
        "trigger_dates": [_base.day_text(i) for i in table.index[table["trigger"]]],
        "signal_dates": [_base.day_text(i) for i in table.index[table["signal"]]],
    }
    return build(_NAME, value, unit="signal", method="momentum_burst", params=params, piece_ids=_PIECE, inputs=(frame,), as_of_source=frame,
                 warnings=tuple(out_warns))

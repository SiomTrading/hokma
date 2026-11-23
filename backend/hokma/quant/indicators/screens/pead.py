"""Post earnings announcement drift screen on closed weekly candles (SPEC-019: CTS-001).

After an upward earnings gap of at least min_gap (3 percent), the setup waits for a red weekly candle (the pullback) and then
for a weekly close above that candle's high. States: MONITORING (gap seen, no red candle yet), SIGNAL_READY (red candle closed,
no breakout yet), BREAKOUT (a closed week closed above the red candle high inside the window), EXPIRED (the window of
window_weeks weeks passed without a breakout, or a weekly close fell under the red candle low before it).

Corrections to the source rules: only closed weekly candles count (the week in progress is never read, and a price that is
live does not make a breakout); the signal is read at the weekly close, so the entry is the next open plus one tick; the stop
is the red candle low, exact; and when one bar both fills and touches the stop the stop is taken first. The earnings reaction
sessions are given by the caller (the first session that trades on the results); this screen does not look for news.
The score weights are not validated: the result carries the warning weights_unvalidated until a backtest sets them.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import limits, registry
from hokma.quant.indicators._common import build, closed_weekly, empty, entry_fill, to_weekly
from hokma.quant.indicators.screens import _base
from hokma.quant.types import QuantResult

_PIECE = ("CTS-001",)
_NAME = "indicators.screens.pead"
_EPS = 1e-9
SCORE_WEIGHTS = {"breakout": 0.4, "volume": 0.3, "stop_tightness": 0.3}
SCORE_WEIGHTS_VALIDATED = False


def _week_label(day: pd.Timestamp) -> pd.Timestamp:
    """Friday that ends the week of a day."""
    return day.normalize() + pd.offsets.Week(weekday=4) if day.weekday() != 4 else day.normalize()


def gap_weeks(daily: pd.DataFrame | None, weekly: pd.DataFrame, earnings: list[pd.Timestamp], timeframe: str) -> list[dict]:
    """The earnings reactions with their gap: daily bars measure the open of the reaction session against the previous close;
    weekly bars measure the open of the week against the previous week close."""
    found = []
    for day in earnings:
        label = _week_label(day)
        if label not in weekly.index:
            continue
        pos = weekly.index.get_loc(label)
        if timeframe == "daily":
            assert daily is not None
            idx = daily.index.tz_localize(None).normalize() if daily.index.tz is not None else daily.index.normalize()
            hits = np.flatnonzero(idx >= day)
            if hits.size == 0 or hits[0] == 0:
                continue
            r = int(hits[0])
            gap = float(daily["open"].iloc[r] / daily["close"].iloc[r - 1] - 1.0)
        else:
            if pos == 0:
                continue
            gap = float(weekly["open"].iloc[pos] / weekly["close"].iloc[pos - 1] - 1.0)
        found.append({"week": label, "pos": pos, "gap": gap})
    return found


@registry.register(_NAME, module="indicators",
                   summary="PEAD screen on closed weekly candles: earnings gap up of at least 3 percent, red pullback week, breakout on a weekly close above its high within 5 weeks. States MONITORING, SIGNAL_READY, BREAKOUT, EXPIRED. Entry next open plus one tick, stop the red candle low, target 2R, score with unvalidated weights.",
                   inputs={"bars": {"type": "dataframe", "required": True, "columns": ["open", "high", "low", "close", "volume"]},
                           "earnings_dates": {"type": "list", "required": True}, "as_of": {"type": "str", "required": False},
                           "timeframe": {"type": "str", "required": False, "default": "daily"},
                           "min_gap": {"type": "float", "required": False, "default": 0.03},
                           "window_weeks": {"type": "int", "required": False, "default": 5},
                           "target_r": {"type": "float", "required": False, "default": 2.0},
                           "tick": {"type": "float", "required": False, "default": 0.01},
                           "min_adv20": {"type": "float", "required": False, "default": 25000000.0},
                           "min_volume": {"type": "float", "required": False, "default": 1000000.0},
                           "min_price": {"type": "float", "required": False, "default": 5.0},
                           "volume_weeks": {"type": "int", "required": False, "default": 4}}, piece_ids=_PIECE)
def pead(bars: Any, earnings_dates: Any, as_of: Any = None, timeframe: str = "daily", min_gap: float = 0.03, window_weeks: int = 5,
         target_r: float = 2.0, tick: float = 0.01, min_adv20: float = 25_000_000.0, min_volume: float = 1_000_000.0, min_price: float = 5.0,
         volume_weeks: int = 4) -> QuantResult:
    window_weeks, volume_weeks = limits.as_int("window_weeks", window_weeks), limits.as_int("volume_weeks", volume_weeks)
    params = {"timeframe": timeframe, "min_gap": min_gap, "window_weeks": window_weeks, "target_r": target_r, "tick": tick,
              "min_adv20": min_adv20, "min_volume": min_volume, "min_price": min_price, "volume_weeks": volume_weeks}
    if not isinstance(earnings_dates, (list, tuple)):
        raise _base.BadInput("earnings_dates must be a list of dates")
    dates = sorted({_base.as_date(d) for d in earnings_dates})
    params["earnings_dates"] = [_base.day_text(d) for d in dates]
    try:
        gap_min = _base.as_float("min_gap", min_gap, positive=True)
        r_mult = _base.as_float("target_r", target_r, positive=True)
        tick_f = _base.as_float("tick", tick, low=0.0)
        adv_min = _base.as_float("min_adv20", min_adv20, low=0.0)
        vol_min = _base.as_float("min_volume", min_volume, low=0.0)
        price_min = _base.as_float("min_price", min_price, low=0.0)
        if timeframe not in ("daily", "weekly") or window_weeks < 1 or volume_weeks < 1:
            raise _base.OutOfDomain("timeframe must be daily or weekly, window_weeks and volume_weeks at least 1")
        if not dates:
            raise _base.OutOfDomain("earnings_dates is empty: the screen needs the earnings reaction sessions")
    except _base.OutOfDomain as exc:
        return empty(_NAME, "state", "pead", str(exc), params=params, piece_ids=_PIECE)
    daily = None
    if timeframe == "daily":
        daily, dropped, warns = _base.daily_closed(bars, ("open", "high", "low", "close", "volume"), as_of=as_of, min_bars=30)
        params["dropped_open_bars"] = dropped
        if daily is None:
            return empty(_NAME, "state", "pead", warns, params=params, piece_ids=_PIECE)
        weekly, open_weeks = closed_weekly(to_weekly(daily), as_of)
    else:
        weekly, dropped, warns = _base.daily_closed(bars, ("open", "high", "low", "close", "volume"), as_of=None, min_bars=8)
        if weekly is None:
            return empty(_NAME, "state", "pead", warns, params=params, piece_ids=_PIECE)
        warns = ()
        weekly, open_weeks = closed_weekly(weekly, as_of)
    params["dropped_open_weeks"] = open_weeks
    if len(weekly) < 8:
        return empty(_NAME, "state", "pead", warns + (f"need 8 closed weeks, got {len(weekly)}",), params=params, piece_ids=_PIECE)
    qualifying = [g for g in gap_weeks(daily, weekly, dates, timeframe) if g["gap"] >= gap_min - _EPS]
    if not qualifying:
        return build(_NAME, {"state": None, "reason": "no_qualifying_earnings_gap", "min_gap": gap_min}, unit="state", method="pead",
                     params=params, piece_ids=_PIECE, inputs=(weekly,), as_of_source=weekly, warnings=warns)
    g = qualifying[-1]
    gpos, n = g["pos"], len(weekly)
    after = n - 1 - gpos                                     # closed weeks after the gap week
    state, reason, red, breakout = None, None, None, None
    inside = weekly.iloc[gpos + 1:gpos + 1 + window_weeks]
    for k in range(len(inside)):
        row = inside.iloc[k]
        if red is None:
            if row["close"] < row["open"]:
                red = (gpos + 1 + k, row)
            continue
        if row["close"] > red[1]["high"] + _EPS:
            breakout = (gpos + 1 + k, row)
            break
        if row["close"] < red[1]["low"] - _EPS:
            reason = "closed_under_red_low"
            break
    if breakout is not None:
        state = "BREAKOUT"
    elif reason is not None or after >= window_weeks:
        state, reason = "EXPIRED", reason or "window_elapsed"
    else:
        state = "SIGNAL_READY" if red is not None else "MONITORING"
    value: dict[str, Any] = {"state": state, "reason": reason, "gap": g["gap"], "gap_week": _base.day_text(g["week"]), "weeks_since_gap": after,
                             "window_weeks": window_weeks, "week_ending": _base.day_text(weekly.index[-1]),
                             "entry_rule": "next_open_plus_1_tick", "tick": tick_f, "same_bar_priority": "stop_first", "target_r": r_mult}
    if red is not None:
        rpos, rrow = red
        trigger, stop = float(rrow["high"]), float(rrow["low"])
        est_entry = trigger + tick_f
        value.update({"red_week": _base.day_text(weekly.index[rpos]), "red_candle": {k: float(rrow[k]) for k in ("open", "high", "low", "close")},
                      "trigger_level": trigger, "stop": stop, "stop_rule": "red_candle_low_exact", "entry_estimate": est_entry,
                      "entry_is_estimate": True, "target_estimate": est_entry + r_mult * (est_entry - stop)})
    if breakout is not None:
        bpos, brow = breakout
        prior = weekly["volume"].iloc[max(0, bpos - volume_weeks):bpos]
        volume_ratio = float(brow["volume"] / prior.mean()) if len(prior) and prior.mean() > 0 else None
        pct = float(brow["close"] / value["trigger_level"] - 1.0)
        entry_price = None
        if daily is not None:
            nxt = daily[daily.index.tz_localize(None).normalize() > weekly.index[bpos]] if daily.index.tz is not None else daily[daily.index.normalize() > weekly.index[bpos]]
            if len(nxt):
                entry_price = entry_fill(float(nxt["open"].iloc[0]), tick_f)
        elif bpos + 1 < n:
            entry_price = entry_fill(float(weekly["open"].iloc[bpos + 1]), tick_f)
        risk_pct = (value["entry_estimate"] - value["stop"]) / value["entry_estimate"]
        vol_part = 0.0 if volume_ratio is None else min(max(volume_ratio - 1.0, 0.0), 1.0)
        score = 100.0 * (SCORE_WEIGHTS["breakout"] * min(max(pct, 0.0) / 0.05, 1.0) + SCORE_WEIGHTS["volume"] * vol_part
                         + SCORE_WEIGHTS["stop_tightness"] * (1.0 - min(risk_pct / 0.10, 1.0)))
        real_target = None if entry_price is None else entry_price + r_mult * (entry_price - value["stop"])
        value.update({"breakout_week": _base.day_text(weekly.index[bpos]), "breakout_close": float(brow["close"]), "breakout_pct": pct,
                      "volume_ratio": volume_ratio, "entry_price": entry_price, "target_price": real_target, "score": score,
                      "weeks_since_breakout": n - 1 - bpos})
    avg_dollar = float((weekly["close"] * weekly["volume"]).iloc[-4:].mean() / 5.0)
    avg_volume = float(weekly["volume"].iloc[-4:].mean() / 5.0)
    if daily is not None and len(daily) >= 20:
        avg_dollar = float((daily["close"] * daily["volume"]).iloc[-20:].mean())
        avg_volume = float(daily["volume"].iloc[-20:].mean())
    last_close = float(weekly["close"].iloc[-1])
    flt = {"adv20": avg_dollar, "adv20_ok": avg_dollar >= adv_min, "volume": avg_volume, "volume_ok": avg_volume >= vol_min,
           "price": last_close, "price_ok": last_close >= price_min}
    flt["passed"] = bool(flt["adv20_ok"] and flt["volume_ok"] and flt["price_ok"])
    value["liquidity"] = flt
    out_warns = list(warns) + ["weights_unvalidated: the score weights are not set by a backtest"]
    if not flt["passed"]:
        out_warns.append("liquidity_filter_failed: " + ", ".join(k for k in ("adv20_ok", "volume_ok", "price_ok") if not flt[k]))
    return build(_NAME, value, unit="state", method="pead", params=params, piece_ids=_PIECE, inputs=(weekly,), as_of_source=weekly,
                 warnings=tuple(out_warns))

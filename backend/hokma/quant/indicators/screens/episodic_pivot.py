"""Episodic pivot classifier for a day 1 candidate over daily closed bars (SPEC-019: CTS-017).

Combines the quality of the catalyst (given by the caller, with a flag that says whether it was verified) with four price
confirmations measured on the reaction session (day 1): range against the mean range of the 20 sessions before, volume against
their mean volume, close position inside the day range, and the risk to the low of the day.

States: ACTIONABLE_DAY1 (day 1 is the last closed bar, the catalyst is verified and high or medium quality, all four hold),
DAY1_WATCH (day 1 is the last closed bar and at least two hold, or the catalyst is not verified), DELAYED_EP_WATCH (day 1 was up
to delayed_max sessions ago, held its low and passed at least three confirmations), CATALYST_WATCH (a good catalyst with no
reaction session yet), REJECT. Earnings and guidance candidates carry pead_handoff for the PEAD screen.

Corrections: the source measured the risk at the day 1 low from a fill at the signal. The signal is read at the close, so the
entry is the next open plus one tick: when that bar is in the data it is used, otherwise the close plus one tick is shown as
an estimate. The stop is the low of day 1 and the stop is taken first inside a bar. The catalyst is never searched here: an
unverified catalyst is a warning and keeps the candidate out of ACTIONABLE_DAY1.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from hokma.quant import limits, registry
from hokma.quant.indicators._common import build, empty, entry_fill
from hokma.quant.indicators.screens import _base
from hokma.quant.types import QuantResult

_PIECE = ("CTS-017",)
_NAME = "indicators.screens.episodic_pivot"
CATALYST_QUALITY = {"earnings": "high", "guidance": "high", "fda": "high", "contract": "medium", "mna": "low"}
HANDOFF = frozenset({"earnings", "guidance"})
_EPS = 1e-9


@registry.register(_NAME, module="indicators",
                   summary="Episodic pivot classifier: catalyst quality (earnings, guidance, FDA, contract, M and A) plus range, volume, close and risk-to-day-low confirmation on day 1. States ACTIONABLE_DAY1, DAY1_WATCH, DELAYED_EP_WATCH, CATALYST_WATCH, REJECT. Entry is the next open plus one tick. Flags pead_handoff for earnings and guidance.",
                   inputs={"bars": {"type": "dataframe", "required": True, "columns": ["open", "high", "low", "close", "volume"]},
                           "catalyst": {"type": "str", "required": True}, "reaction_date": {"type": "str", "required": False},
                           "catalyst_verified": {"type": "bool", "required": False, "default": False},
                           "as_of": {"type": "str", "required": False}, "range_multiple": {"type": "float", "required": False, "default": 2.0},
                           "volume_multiple": {"type": "float", "required": False, "default": 3.0},
                           "close_position_min": {"type": "float", "required": False, "default": 0.6},
                           "max_risk_pct": {"type": "float", "required": False, "default": 0.10},
                           "delayed_max": {"type": "int", "required": False, "default": 5},
                           "tick": {"type": "float", "required": False, "default": 0.01}}, piece_ids=_PIECE)
def episodic_pivot(bars: Any, catalyst: str, reaction_date: Any = None, catalyst_verified: bool = False, as_of: Any = None,
                   range_multiple: float = 2.0, volume_multiple: float = 3.0, close_position_min: float = 0.6, max_risk_pct: float = 0.10,
                   delayed_max: int = 5, tick: float = 0.01) -> QuantResult:
    delayed_max = limits.as_int("delayed_max", delayed_max)
    params = {"catalyst": catalyst, "reaction_date": None if reaction_date is None else str(reaction_date), "catalyst_verified": bool(catalyst_verified),
              "range_multiple": range_multiple, "volume_multiple": volume_multiple, "close_position_min": close_position_min,
              "max_risk_pct": max_risk_pct, "delayed_max": delayed_max, "tick": tick}
    if not isinstance(catalyst, str):
        raise _base.BadInput("catalyst must be a string")
    if not isinstance(catalyst_verified, bool):
        raise _base.BadInput("catalyst_verified must be true or false")
    try:
        rng_x = _base.as_float("range_multiple", range_multiple, positive=True)
        vol_x = _base.as_float("volume_multiple", volume_multiple, positive=True)
        pos_min = _base.as_float("close_position_min", close_position_min, positive=True)
        risk_max = _base.as_float("max_risk_pct", max_risk_pct, positive=True)
        tick_f = _base.as_float("tick", tick, low=0.0)
        if pos_min > 1.0 or delayed_max < 1:
            raise _base.OutOfDomain("close_position_min must be at most 1 and delayed_max at least 1")
    except _base.OutOfDomain as exc:
        return empty(_NAME, "state", "episodic_pivot", str(exc), params=params, piece_ids=_PIECE)
    kind = catalyst.strip().lower()
    quality = CATALYST_QUALITY.get(kind)
    frame, dropped, warns = _base.daily_closed(bars, ("open", "high", "low", "close", "volume"), as_of=as_of, min_bars=25)
    params["dropped_open_bars"] = dropped
    if frame is None:
        return empty(_NAME, "state", "episodic_pivot", warns, params=params, piece_ids=_PIECE)
    out_warns = list(warns)
    if not catalyst_verified:
        out_warns.append("catalyst_unverified: the catalyst came from a person or a model and nothing here verified it")
    if quality is None:
        out_warns.append(f"unknown_catalyst: {catalyst!r} is not one of {sorted(CATALYST_QUALITY)}")
    idx = frame.index.tz_localize(None).normalize() if frame.index.tz is not None else frame.index.normalize()
    r = None
    if reaction_date is not None:
        hits = np.flatnonzero(idx == _base.as_date(reaction_date))
        r = int(hits[0]) if hits.size else None
    base_value: dict[str, Any] = {"catalyst": kind, "catalyst_quality": quality or "unknown", "catalyst_verified": catalyst_verified,
                                  "bar_date": _base.day_text(frame.index[-1]), "entry_rule": "next_open_plus_1_tick", "tick": tick_f,
                                  "same_bar_priority": "stop_first", "pead_handoff": False}

    def done(state: str, extra: dict | None = None) -> QuantResult:
        value = {**base_value, "state": state, **(extra or {})}
        value["pead_handoff"] = bool(kind in HANDOFF and state in ("ACTIONABLE_DAY1", "DAY1_WATCH", "DELAYED_EP_WATCH"))
        return build(_NAME, value, unit="state", method="episodic_pivot", params=params, piece_ids=_PIECE, inputs=(frame,), as_of_source=frame,
                     warnings=tuple(out_warns))

    if quality is None or quality == "low":
        return done("REJECT", {"reason": "catalyst_quality_not_enough"})
    if r is None:
        return done("CATALYST_WATCH", {"reason": "no_reaction_session_yet"})
    if r < 21:
        return empty(_NAME, "state", "episodic_pivot", out_warns + ["need 21 sessions before the reaction session"], params=params, piece_ids=_PIECE)
    last = len(frame) - 1
    age = last - r
    day = frame.iloc[r]
    prior = frame.iloc[r - 20:r]
    day_range = float(day["high"] - day["low"])
    range_ratio = day_range / float((prior["high"] - prior["low"]).mean())
    volume_ratio = float(day["volume"] / prior["volume"].mean())
    position = float((day["close"] - day["low"]) / day_range) if day_range > 0 else 0.0
    nxt_open = float(frame["open"].iloc[r + 1]) if r + 1 <= last else None
    entry = entry_fill(nxt_open, tick_f) if nxt_open is not None else entry_fill(float(day["close"]), tick_f)
    stop = float(day["low"])
    risk_pct = (entry - stop) / entry
    checks = {"range": range_ratio >= rng_x - _EPS, "volume": volume_ratio >= vol_x - _EPS, "close": position >= pos_min - _EPS and day["close"] > day["open"],
              "risk": 0 < risk_pct <= risk_max + _EPS}
    passed = int(sum(checks.values()))
    detail = {"reaction_date": _base.day_text(frame.index[r]), "age_sessions": age, "range_ratio": range_ratio, "volume_ratio": volume_ratio,
              "close_position": position, "checks": {k: bool(v) for k, v in checks.items()}, "checks_passed": passed, "stop": stop,
              "entry": entry, "entry_is_estimate": nxt_open is None, "risk_pct": risk_pct, "day1_high": float(day["high"])}
    if age == 0:
        if passed == 4 and catalyst_verified:
            return done("ACTIONABLE_DAY1", detail)
        return done("DAY1_WATCH" if passed >= 2 else "REJECT", detail)
    if age <= delayed_max:
        held = bool(frame["close"].iloc[r + 1:].min() >= stop - _EPS)
        if held and passed >= 3:
            return done("DELAYED_EP_WATCH", detail)
        return done("REJECT", {**detail, "reason": "day1_low_broken" if not held else "confirmations_not_enough"})
    return done("REJECT", {**detail, "reason": "reaction_too_old"})

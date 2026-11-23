"""CTS-005: weekly structure. Trend, fractal levels, scenarios with invalidation and the confirmation mode, with hand built weeks."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators import _common
from hokma.quant.indicators.screens import weekly_structure as ws

from .helpers import expand_weekly, weekly_from_closes


def tri(w: int) -> int:
    m = w % 12
    return 3 * m if m <= 6 else 3 * (12 - m)


def zigzag(n: int = 58) -> pd.DataFrame:
    return weekly_from_closes([100 + 0.3 * w + tri(w) for w in range(n)])


def run(weekly: pd.DataFrame, **kw):
    return ws.weekly_structure(weekly, timeframe="weekly", **kw)


def flat(n: int = 60, overrides: dict[int, float] | None = None) -> pd.DataFrame:
    closes = np.full(n, 100.0)
    for k, v in (overrides or {}).items():
        closes[k] = v
    return weekly_from_closes(closes)


def test_fractal_pivots_are_strict_and_known_only_after_two_more_weeks():
    high = pd.Series([1.0, 2.0, 3.0, 2.0, 1.0, 2.0, 1.0])
    assert ws.fractal_pivots(high, high - 0.5)[0] == [2]                         # position 5 is not a pivot: it has only one week after it
    tie = pd.Series([1.0, 2.0, 3.0, 3.0, 1.0])
    assert ws.fractal_pivots(tie, tie - 0.5)[0] == []                            # equal highs are not a pivot (strict inequalities)
    low = pd.Series([3.0, 2.0, 1.0, 2.0, 3.0])
    assert ws.fractal_pivots(low + 1, low)[1] == [2]


def test_structure_by_hand_on_a_zigzag():
    value = run(zigzag(), as_of=None).value
    assert value["trend"] == "up" and value["swing_structure"] == "higher_highs_higher_lows"
    assert value["close"] == pytest.approx(126.1)
    assert value["resistances"] == pytest.approx([127.5, 131.1]) and value["supports"] == pytest.approx([113.9, 110.3])
    assert value["averages"] == {"sma_10": pytest.approx(125.65), "sma_30": pytest.approx(122.65), "sma_40": pytest.approx(120.7)}
    assert value["volume_ratio_10w"] == pytest.approx(1.0)
    names = [s["name"] for s in value["scenarios"]]
    assert names == ["bullish_continuation", "bearish_break"]
    bull, bear = value["scenarios"]
    assert (bull["trigger"], bull["invalidation"]) == (pytest.approx(127.5), pytest.approx(113.9))
    assert (bear["trigger"], bear["invalidation"]) == (pytest.approx(113.9), pytest.approx(127.5))


def test_scenario_probabilities_are_never_invented():
    for weekly in (zigzag(), flat(), flat(overrides={45: 110, 46: 100, 58: 105, 59: 105})):
        res = run(weekly)
        assert res.value["probabilities_calibrated"] is False
        assert 2 <= len(res.value["scenarios"]) <= 4 and all(s["probability"] is None for s in res.value["scenarios"])
        assert any(w.startswith("scenario_probabilities_not_calibrated") for w in res.warnings)


def test_direction_change_of_13_weeks_by_hand():
    weekly = flat(overrides={45: 110, 46: 100, 58: 105, 59: 105})
    value = run(weekly).value
    c = value["confirmation"]
    assert c["direction_change"] is True and c["new_direction"] == "up"
    assert c["continuation_veto"] is False and c["verdict"] == "confirmed" and c["reasons"] == []
    reversal = [s for s in value["scenarios"] if s["name"] == "reversal"][0]
    assert reversal["direction"] == "up" and reversal["trigger"] == pytest.approx(105.5) and reversal["invalidation"] == pytest.approx(104.5)
    assert 2 <= len(value["scenarios"]) <= 4


def test_no_change_when_the_13_week_difference_is_zero():
    c = run(flat()).value["confirmation"]
    assert c["direction_change"] is False and c["verdict"] == "not_confirmed" and c["reasons"] == ["no_reversal_signal"]


def test_a_reversal_is_vetoed_when_the_larger_trend_continues():
    closes = [100.0 + w for w in range(59)] + [145.0]
    c = run(weekly_from_closes(closes)).value["confirmation"]
    assert c["direction_change"] is True and c["new_direction"] == "down"
    assert c["continuation_veto"] is True and c["verdict"] == "not_confirmed" and "continuation_veto" in c["reasons"]


def test_a_failed_52_week_extreme_needs_strictly_more_and_a_close_back_inside():
    base = flat()

    def last_week(high=None, low=None, close=100.0):
        w = base.copy()
        w.iloc[-1, w.columns.get_loc("high")] = high if high is not None else 100.5
        w.iloc[-1, w.columns.get_loc("low")] = low if low is not None else 99.5
        w.iloc[-1, w.columns.get_loc("close")] = close
        return w

    assert run(last_week(high=100.5)).value["confirmation"]["failed_extreme_high"] is False        # equal to the prior high: not above it
    failed = run(last_week(high=120.0)).value["confirmation"]
    assert failed["failed_extreme_high"] is True and failed["verdict"] == "confirmed"
    assert run(last_week(high=122.0, close=121.0)).value["confirmation"]["failed_extreme_high"] is False    # closing above is a breakout, not a failure
    assert run(last_week(low=99.5)).value["confirmation"]["failed_extreme_low"] is False
    assert run(last_week(low=90.0)).value["confirmation"]["failed_extreme_low"] is True


def test_a_failed_breakout_by_hand():
    weekly = flat(overrides={40: 110, 58: 111, 59: 108})
    c = run(weekly).value["confirmation"]
    assert c["failed_breakout"] is True and c["failed_extreme_high"] is False
    held = flat(overrides={40: 110, 58: 111, 59: 111})
    assert run(held).value["confirmation"]["failed_breakout"] is False


def test_visual_and_code_readings_that_disagree_are_not_confirmed():
    weekly = flat(overrides={45: 110, 46: 100, 58: 105, 59: 105})
    agree = run(weekly, visual_read="up").value["confirmation"]
    assert agree["code_read"] == "up" and agree["verdict"] == "confirmed"
    clash = run(weekly, visual_read="down").value["confirmation"]
    assert clash["verdict"] == "not_confirmed" and "visual_code_mismatch" in clash["reasons"]


def test_daily_bars_are_cut_at_as_of_before_resampling():
    weekly = zigzag()
    daily = expand_weekly(weekly)
    friday = weekly.index[-1]
    at = f"{friday.date()}T17:00"
    direct = run(weekly).value
    from_daily = ws.weekly_structure(daily, as_of=at).value
    assert from_daily == direct
    partial = pd.DataFrame({"open": [100.0, 100.0], "high": [400.0, 400.0], "low": [90.0, 90.0], "close": [300.0, 350.0], "volume": [9e9, 9e9]},
                           index=[friday + pd.Timedelta(days=3), friday + pd.Timedelta(days=4)])
    extended = pd.concat([daily, partial])
    assert ws.weekly_structure(extended, as_of=at).value == direct                                                   # bars after as_of are cut
    midweek = f"{(friday + pd.Timedelta(days=4)).date()}T17:00"
    res = ws.weekly_structure(extended, as_of=midweek)
    assert res.value == direct and res.params["dropped_open_weeks"] == 1                                              # the week in progress never counts
    done = f"{(friday + pd.Timedelta(days=8)).date()}T17:00"
    assert ws.weekly_structure(extended, as_of=done).value != direct


def test_weekly_bars_in_progress_are_dropped_too():
    weekly = zigzag()
    extra = pd.concat([weekly, weekly.iloc[[-1]].set_axis([weekly.index[-1] + pd.Timedelta(days=7)]).assign(close=300.0, high=301.0, open=300.0, low=299.0)])
    at = f"{weekly.index[-1].date()}T17:00"
    assert ws.weekly_structure(extra, timeframe="weekly", as_of=at).value == run(weekly).value


def test_not_enough_weeks_nan_and_bad_options_give_no_value():
    assert run(zigzag(40)).value is None
    nan = zigzag()
    nan.iloc[20, nan.columns.get_loc("close")] = np.nan
    assert run(nan).value is None
    assert ws.weekly_structure(zigzag(), timeframe="monthly").value is None
    assert run(zigzag(), visual_read="sideways-ish").value is None
    assert run(zigzag(), direction_weeks=1).value is None
    assert run(zigzag().iloc[:0]).value is None
    with pytest.raises(BadInput):
        run(zigzag(), direction_weeks=2.5)
    with pytest.raises(BadInput):
        ws.weekly_structure([1, 2, 3])
    with pytest.raises(BadInput):
        ws.weekly_structure(zigzag().reset_index(drop=True), timeframe="weekly")


def test_on_the_fixed_daily_bars_and_through_the_registry(spy, spy_weekly):
    at = f"{spy.index[-1].date()}T17:00"
    daily = registry.call("indicators.screens.weekly_structure", bars=spy, as_of=at)
    assert daily.piece_ids == ("CTS-005",) and daily.value["trend"] in {"up", "down", "sideways"}
    assert 2 <= len(daily.value["scenarios"]) <= 4
    weekly = ws.weekly_structure(spy_weekly, timeframe="weekly", as_of=at)
    assert weekly.value["trend"] == daily.value["trend"]
    last_friday = _common.closed_weekly(spy_weekly, at)[0].index[-1]
    assert weekly.value["week_ending"] == str(last_friday.date())

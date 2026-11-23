"""CTS-001: PEAD screen on closed weekly candles. States, window, the fill rule and the corrections, with hand built weeks."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators.screens import pead

from .helpers import expand_weekly

PRE = 6                    # flat weeks before the gap week
FLAT = (100.0, 100.5, 99.5, 100.0)


def weeks(rows, volume: float = 10_000_000.0, gap_open: float = 105.0) -> pd.DataFrame:
    """Weekly bars: PRE flat weeks, then the gap week (open gap_open, close 108) and the given (open, high, low, close) weeks."""
    spec = [FLAT] * PRE + [(gap_open, 109.0, min(gap_open, 108.0) - 0.5, 108.0)] + list(rows)
    frame = pd.DataFrame(spec, columns=["open", "high", "low", "close"], index=pd.date_range("2023-01-06", periods=len(spec), freq="W-FRI"))
    frame["volume"] = volume
    return frame


GREEN1 = (108.5, 111.0, 108.0, 110.5)
GREEN2 = (110.5, 112.0, 110.0, 111.5)
RED = (110.5, 111.0, 108.0, 109.0)                 # the pullback: high 111.0, low 108.0
INSIDE = (109.0, 110.5, 108.5, 110.0)              # closes under the red high
BREAK = (110.0, 113.0, 109.5, 112.0)               # closes over the red high


def earnings(frame: pd.DataFrame) -> list[str]:
    return [str((frame.index[PRE] - pd.Timedelta(days=2)).date())]          # the Wednesday of the gap week


def run(frame, **kw):
    return pead.pead(frame, earnings_dates=earnings(frame), timeframe="weekly", **kw)


def test_monitoring_until_a_red_candle_closes():
    res = run(weeks([GREEN1, GREEN2]))
    assert res.value["state"] == "MONITORING" and res.value["weeks_since_gap"] == 2 and res.value["gap"] == pytest.approx(0.05)
    assert "red_candle" not in res.value


def test_signal_ready_with_the_exact_stop_by_hand():
    v = run(weeks([GREEN1, RED, INSIDE])).value
    assert v["state"] == "SIGNAL_READY" and v["trigger_level"] == 111.0 and v["stop"] == 108.0 and v["stop_rule"] == "red_candle_low_exact"
    assert v["red_candle"] == {"open": 110.5, "high": 111.0, "low": 108.0, "close": 109.0}
    assert v["entry_rule"] == "next_open_plus_1_tick" and v["entry_is_estimate"] is True
    assert v["entry_estimate"] == pytest.approx(111.01) and v["target_estimate"] == pytest.approx(111.01 + 2 * (111.01 - 108.0))
    assert v["same_bar_priority"] == "stop_first"


def test_breakout_needs_a_weekly_close_over_the_red_high_and_enters_at_the_next_open_plus_a_tick():
    nxt = (112.4, 114.0, 112.0, 113.0)
    v = run(weeks([GREEN1, RED, INSIDE, BREAK, nxt])).value
    assert v["state"] == "BREAKOUT" and v["breakout_close"] == 112.0 and v["weeks_since_breakout"] == 1
    assert v["entry_price"] == pytest.approx(112.41)                                  # the next open plus one tick, not the breakout close
    assert v["target_price"] == pytest.approx(112.41 + 2 * (112.41 - 108.0))
    assert v["breakout_pct"] == pytest.approx(112.0 / 111.0 - 1)
    ready = run(weeks([GREEN1, RED, INSIDE, BREAK]))
    assert ready.value["state"] == "BREAKOUT" and ready.value["entry_price"] is None            # the next bar is not in the data yet


def test_a_wick_over_the_red_high_is_not_a_breakout():
    wick = (110.0, 113.0, 109.5, 110.8)
    assert run(weeks([GREEN1, RED, wick])).value["state"] == "SIGNAL_READY"


def test_the_window_is_five_weeks_and_a_late_breakout_is_expired():
    five = [GREEN1, RED, INSIDE, INSIDE, INSIDE]
    assert run(weeks(five[:4])).value["state"] == "SIGNAL_READY"                               # four weeks after the gap
    expired = run(weeks(five))
    assert expired.value["state"] == "EXPIRED" and expired.value["reason"] == "window_elapsed"   # five weeks passed with no breakout
    late = run(weeks(five + [BREAK]))
    assert late.value["state"] == "EXPIRED"
    assert run(weeks([GREEN1, RED, INSIDE, INSIDE, BREAK])).value["state"] == "BREAKOUT"        # the fifth week is still inside
    assert run(weeks([GREEN1, GREEN1, GREEN1, GREEN1, GREEN1])).value["state"] == "EXPIRED"      # never a red candle
    assert run(weeks(five[:4]), window_weeks=8).value["state"] == "SIGNAL_READY"


def test_a_close_under_the_red_low_before_the_breakout_expires_the_setup():
    broke = (109.0, 109.5, 107.0, 107.5)
    v = run(weeks([GREEN1, RED, broke])).value
    assert v["state"] == "EXPIRED" and v["reason"] == "closed_under_red_low"


def test_the_gap_must_be_at_least_three_percent():
    assert run(weeks([GREEN1, RED], gap_open=102.9)).value["state"] is None
    assert run(weeks([GREEN1, RED], gap_open=102.9)).value["reason"] == "no_qualifying_earnings_gap"
    assert run(weeks([GREEN1, RED], gap_open=103.0)).value["state"] == "SIGNAL_READY"             # 3 percent exactly counts
    assert run(weeks([GREEN1, RED], gap_open=105.0), min_gap=0.06).value["state"] is None


def test_the_daily_path_reads_the_gap_from_the_reaction_session_and_matches_the_weekly_path():
    weekly = weeks([GREEN1, RED, INSIDE, BREAK, (112.4, 114.0, 112.0, 113.0)])
    daily = expand_weekly(weekly)
    reaction = [str((weekly.index[PRE] - pd.Timedelta(days=4)).date())]                              # the Monday of the gap week
    at = f"{weekly.index[-1].date()}T17:00"
    d = pead.pead(daily, earnings_dates=reaction, as_of=at).value
    w = run(weekly).value
    for key in ("state", "gap", "trigger_level", "stop", "breakout_week", "entry_price", "target_price", "score"):
        assert d[key] == pytest.approx(w[key]), key


def test_a_price_that_is_live_never_makes_a_breakout():
    weekly = weeks([GREEN1, RED, INSIDE])
    daily = expand_weekly(weekly)
    reaction = [str((weekly.index[PRE] - pd.Timedelta(days=4)).date())]
    friday = weekly.index[-1]
    live = pd.DataFrame({"open": [110.0, 112.5], "high": [112.6, 114.0], "low": [109.9, 112.4], "close": [112.5, 113.9], "volume": [4e6, 4e6]},
                        index=[friday + pd.Timedelta(days=3), friday + pd.Timedelta(days=4)])      # Monday and Tuesday of the next week
    extended = pd.concat([daily, live])
    midweek = f"{(friday + pd.Timedelta(days=4)).date()}T17:00"
    res = pead.pead(extended, earnings_dates=reaction, as_of=midweek)
    assert res.value["state"] == "SIGNAL_READY" and res.params["dropped_open_weeks"] == 1             # the week in progress is not read
    before = pead.pead(daily, earnings_dates=reaction, as_of=f"{friday.date()}T17:00").value
    assert {k: v for k, v in res.value.items() if k != "liquidity"} == {k: v for k, v in before.items() if k != "liquidity"}    # liquidity reads the closed daily bars
    closed_week = pd.concat([daily, pd.DataFrame({"open": [110.0] * 5, "high": [113.0] * 5, "low": [109.5] * 5, "close": [111.0, 111.5, 112.0, 112.2, 112.5],
                                                  "volume": [2e6] * 5}, index=pd.bdate_range(friday + pd.Timedelta(days=3), periods=5))])
    done = pead.pead(closed_week, earnings_dates=reaction, as_of=f"{(friday + pd.Timedelta(days=7)).date()}T17:00")
    assert done.value["state"] == "BREAKOUT"


def test_the_score_and_the_unvalidated_weights():
    nxt = (112.4, 114.0, 112.0, 113.0)
    frame = weeks([GREEN1, RED, INSIDE])
    frame.loc[frame.index[-1] + pd.Timedelta(days=7)] = (110.0, 113.0, 109.5, 112.0, 15_000_000.0)
    frame.loc[frame.index[-1] + pd.Timedelta(days=7)] = (*nxt, 10_000_000.0)
    res = run(frame)
    v = res.value
    risk_pct = (111.01 - 108.0) / 111.01
    expected = 100.0 * (0.4 * min((112.0 / 111.0 - 1) / 0.05, 1.0) + 0.3 * min(15 / 10 - 1, 1.0) + 0.3 * (1 - min(risk_pct / 0.10, 1.0)))
    assert v["score"] == pytest.approx(expected) and v["volume_ratio"] == pytest.approx(1.5)
    assert any(w.startswith("weights_unvalidated") for w in res.warnings)
    assert pead.SCORE_WEIGHTS_VALIDATED is False and sum(pead.SCORE_WEIGHTS.values()) == pytest.approx(1.0)


def test_the_liquidity_filters_are_reported_and_warn():
    ok = run(weeks([GREEN1, RED], volume=10_000_000.0))
    assert ok.value["liquidity"]["passed"] is True
    thin = run(weeks([GREEN1, RED], volume=2_000_000.0))                     # 400 thousand shares a day
    assert thin.value["liquidity"]["volume_ok"] is False and thin.value["liquidity"]["passed"] is False
    assert any(w.startswith("liquidity_filter_failed") for w in thin.warnings)
    cheap = run(weeks([GREEN1, RED]), min_price=500.0)
    assert cheap.value["liquidity"]["price_ok"] is False


def test_empty_nan_short_and_bad_inputs():
    frame = weeks([GREEN1, RED])
    assert pead.pead(frame.iloc[:0], earnings_dates=earnings(frame), timeframe="weekly").value is None
    nan = frame.copy()
    nan.iloc[3, nan.columns.get_loc("close")] = np.nan
    assert run(nan).value is None
    assert pead.pead(frame.iloc[:5], earnings_dates=earnings(frame), timeframe="weekly").value is None
    assert pead.pead(frame, earnings_dates=[], timeframe="weekly").value is None
    assert run(frame, min_gap=-0.01).value is None
    assert run(frame, window_weeks=0).value is None
    assert pead.pead(frame, earnings_dates=earnings(frame), timeframe="monthly").value is None
    assert pead.pead(frame, earnings_dates=["2020-01-01"], timeframe="weekly").value["state"] is None
    with pytest.raises(BadInput):
        pead.pead(frame, earnings_dates="2023-02-15", timeframe="weekly")
    with pytest.raises(BadInput):
        pead.pead(frame, earnings_dates=[5], timeframe="weekly")
    with pytest.raises(BadInput):
        run(frame, tick="x")
    with pytest.raises(BadInput):
        run(frame, window_weeks=2.5)
    with pytest.raises(BadInput):
        pead.pead([1, 2], earnings_dates=["2023-02-15"])


def test_registered_and_called_through_the_registry():
    assert registry.describe("indicators.screens.pead").piece_ids == ("CTS-001",)
    frame = weeks([GREEN1, RED, INSIDE])
    res = registry.call("indicators.screens.pead", bars=frame, earnings_dates=earnings(frame), timeframe="weekly")
    assert res.value["state"] == "SIGNAL_READY"


def test_on_the_fixed_daily_and_weekly_bars(spy, spy_weekly):
    day = str(spy.index[400].date())
    at = f"{spy.index[-1].date()}T17:00"
    daily = pead.pead(spy, earnings_dates=[day], as_of=at)
    weekly = pead.pead(spy_weekly, earnings_dates=[day], timeframe="weekly", as_of=at)
    assert daily.ok and weekly.ok
    assert daily.value["state"] in {None, "MONITORING", "SIGNAL_READY", "BREAKOUT", "EXPIRED"}

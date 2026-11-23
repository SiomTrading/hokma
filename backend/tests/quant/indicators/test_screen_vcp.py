"""CTS-020: VCP screen. Trend template, contraction chain with ratio 0.70, pivot, states, 253 sessions, and the forward outcome fills."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators.screens import vcp

from .helpers import stamp, vcp_bars, vcp_closes


def run(bars, **kw):
    return vcp.vcp(bars, as_of=stamp(bars, len(bars) - 1), **kw)


def pivot_and_stop() -> tuple[float, float]:
    _, lv = vcp_closes()
    return lv["h3"] * 1.002, lv["l3"] * 0.998


def test_trend_template_has_seven_points_by_hand():
    up = pd.Series(np.arange(1.0, 301.0))
    t = vcp.trend_template(up, up, up)
    assert t["passed"] == 7 and len(t["points"]) == 7 and all(t["points"].values())
    assert t["sma50"] == pytest.approx(275.5) and t["sma150"] == pytest.approx(225.5) and t["sma200"] == pytest.approx(200.5)
    down = pd.Series(np.arange(300.0, 0.0, -1.0))
    assert vcp.trend_template(down, down, down)["passed"] == 0
    dropped = up.copy()
    dropped.iloc[-1] = 200.0
    t = vcp.trend_template(dropped, dropped, dropped)
    assert t["points"]["close_within_25_percent_of_52w_high"] is False and t["passed"] < 7


def test_contraction_chain_ratio_070_by_hand():
    def legs(depths):
        return [{"start": i * 10, "end": i * 10 + 5, "high": 100.0, "low": 100.0 * (1 - d), "depth": d} for i, d in enumerate(depths)]

    assert [c["depth"] for c in vcp.contraction_chain(legs([0.20, 0.14, 0.09]), 0.70)] == [0.20, 0.14, 0.09]     # 0.14 = 0.70 x 0.20 counts
    assert [c["depth"] for c in vcp.contraction_chain(legs([0.20, 0.15]), 0.70)] == [0.15]                       # 0.75 of the one before: no chain
    assert [c["depth"] for c in vcp.contraction_chain(legs([0.40, 0.28, 0.19, 0.13, 0.09]), 0.70)] == [0.28, 0.19, 0.13, 0.09]   # at most four
    assert vcp.contraction_chain([], 0.70) == []


def test_states_by_hand_from_closes_only():
    after = np.array([95.0, 97.0, 99.0])
    kw = {"extended_pct": 0.05, "early_bars": 5}
    assert vcp.classify_state(94.0, 100.0, 95.0, np.array([96.0, 94.0]), **kw) == ("Damaged", None)              # a close under the last low
    assert vcp.classify_state(99.0, 100.0, 95.0, after, **kw) == ("Pre-breakout", None)
    assert vcp.classify_state(102.0, 100.0, 95.0, np.array([97.0, 102.0]), **kw) == ("Breakout", 0)
    assert vcp.classify_state(103.0, 100.0, 95.0, np.array([97.0, 102.0, 103.0]), **kw) == ("Early-post-breakout", 1)
    assert vcp.classify_state(107.0, 100.0, 95.0, np.array([97.0, 107.0]), **kw) == ("Extended", 0)
    assert vcp.classify_state(101.0, 100.0, 95.0, np.array([97.0] + [101.0] * 7), **kw) == ("Extended", 6)
    assert vcp.classify_state(95.0, 100.0, 95.0, np.array([97.0, 95.0]), **kw)[0] == "Pre-breakout"             # equal to the low is not under it


def test_pattern_end_to_end_by_hand():
    bars = vcp_bars()
    res = run(bars)
    v = res.value
    pivot, stop = pivot_and_stop()
    assert v["state"] == "Pre-breakout" and v["reasons"] == [] and v["trend_template"]["passed"] == 7
    assert v["contraction_count"] == 3 and len(v["depth_ratios"]) == 2 and all(r <= 0.70 for r in v["depth_ratios"])
    assert [round(c["depth"], 2) for c in v["contractions"]] == [0.20, 0.12, 0.06]
    assert v["pivot"] == pytest.approx(pivot) and v["stop"] == pytest.approx(stop)
    assert v["volume_dry_up"]["ok"] is True and v["volume_dry_up"]["ratio"] == pytest.approx(0.5)
    assert vcp.vcp(vcp_bars(volume_tail=1000.0), as_of=stamp(bars, len(bars) - 1)).value["volume_dry_up"]["ok"] is False
    assert v["entry_rule"] == "next_open_plus_1_tick" and "size" not in v and "multiplier" not in v


def test_state_follows_the_closes_after_the_pattern():
    pivot, stop = pivot_and_stop()
    cases = {"Breakout": [pivot * 1.02], "Early-post-breakout": [pivot * 1.02, pivot * 1.03], "Extended": [pivot * 1.07]}
    for state, extra in cases.items():
        bars = vcp_bars(extra)
        assert run(bars).value["state"] == state, state
    assert run(vcp_bars([pivot * 1.01] * 7)).value["state"] == "Extended"
    assert run(vcp_bars([pivot * 1.02, pivot * 1.03])).value["days_since_breakout"] == 1


def test_a_wick_above_the_pivot_is_not_a_breakout():
    pivot, _ = pivot_and_stop()
    bars = vcp_bars([pivot * 0.995])
    bars.iloc[-1, bars.columns.get_loc("high")] = pivot * 1.05
    assert run(bars).value["state"] == "Pre-breakout"


def test_a_failed_template_or_chain_gives_no_state_but_a_value():
    pivot, stop = pivot_and_stop()
    crash = run(vcp_bars([stop * 0.97])).value
    assert crash["state"] is None and "trend_template_failed" in crash["reasons"]
    steady = np.array([50 * 1.003 ** i for i in range(300)])
    bars = pd.DataFrame({"open": np.r_[steady[0], steady[:-1]], "high": steady * 1.002, "low": steady * 0.998, "close": steady, "volume": 1000.0},
                        index=pd.bdate_range("2023-01-02", periods=300))
    no_legs = run(bars).value
    assert no_legs["state"] is None and "fewer_than_2_contractions" in no_legs["reasons"] and no_legs["trend_template"]["passed"] == 7


def test_it_needs_253_valid_sessions_and_missing_data_is_never_zero():
    bars = vcp_bars()
    assert vcp.vcp(bars.iloc[:252], as_of=stamp(bars, 251)).value is None
    assert any("not available" in w for w in vcp.vcp(bars.iloc[:252], as_of=stamp(bars, 251)).warnings)
    assert vcp.vcp(bars.iloc[:253], as_of=stamp(bars, 252)).ok
    nan = bars.copy()
    nan.iloc[100, nan.columns.get_loc("low")] = np.nan
    assert run(nan).value is None
    zero = bars.copy()
    zero.iloc[100, zero.columns.get_loc("high")] = 0.0
    assert run(zero).value is None
    assert run(bars.drop(columns="volume").assign(volume=np.nan)).value is None


def test_only_closed_bars_are_used():
    bars = vcp_bars()
    pivot, _ = pivot_and_stop()
    open_bar = pd.concat([bars, pd.DataFrame({"open": [100.0], "high": [150.0], "low": [99.0], "close": [149.0], "volume": [9e5]}, index=[bars.index[-1] + pd.offsets.BDay(1)])])
    at = stamp(bars, len(bars) - 1)
    a, b = vcp.vcp(open_bar, as_of=at), vcp.vcp(bars, as_of=at)
    assert a.value == b.value and a.params["dropped_open_bars"] == 1 and a.value["state"] == "Pre-breakout"


def test_bad_parameters_and_types():
    bars = vcp_bars()
    assert run(bars, contraction_ratio=1.5).value is None
    assert run(bars, swing_order=0).value is None
    assert run(bars, pattern_lookback=5).value is None
    with pytest.raises(BadInput):
        run(bars, contraction_ratio="x")
    with pytest.raises(BadInput):
        run(bars, swing_order=1.5)
    with pytest.raises(BadInput):
        vcp.vcp("x")
    with pytest.raises(BadInput):
        vcp.vcp(bars.drop(columns="high"))


# ------------------------------------------------------------------ forward outcome
def after_bars(rows) -> pd.DataFrame:
    """Bars after a detection on 2024-01-02: (open, high, low, close) from 2024-01-03."""
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=pd.bdate_range("2024-01-03", periods=len(rows)))


def fwd(rows, **kw):
    return vcp.vcp_forward_outcome(after_bars(rows), detection_date="2024-01-02", pivot=100.0, stop=95.0, **kw)


def test_forward_outcome_fills_at_the_next_open_plus_a_tick_and_stops_at_the_stop():
    rows = [(98.0, 99.5, 97.5, 99.0), (100.0, 101.5, 99.5, 101.0),      # first close above the pivot on bar 2
            (101.5, 103.0, 100.0, 102.0), (102.0, 103.0, 94.0, 96.0)]    # fill on bar 3 at the open; the stop trades on bar 4
    res = fwd(rows)
    v = res.value
    assert v["breakout_date"] == "2024-01-04" and v["entry_date"] == "2024-01-05" and v["entry_price"] == pytest.approx(101.51)
    assert v["outcome"] == "stopped" and v["exit_price"] == 95.0 and v["exit_date"] == "2024-01-08"
    assert v["return_net"] == pytest.approx((95.0 - 101.51) / 101.51)
    assert v["max_gain"] == pytest.approx(103.0 / 101.51 - 1) and v["max_loss"] == pytest.approx(94.0 / 101.51 - 1)
    assert v["measured_from"] == "fill"
    with_cost = fwd(rows, cost_per_share=0.05).value
    assert with_cost["return_net"] == pytest.approx((95.0 - 101.51 - 0.05) / 101.51)


def test_the_stop_is_taken_first_when_the_entry_bar_also_touches_it():
    rows = [(98.0, 101.5, 97.5, 101.0), (101.5, 103.0, 94.0, 98.0), (98.0, 99.0, 97.0, 98.0)]
    v = fwd(rows).value
    assert v["entry_date"] == v["exit_date"] == "2024-01-04" and v["outcome"] == "stopped" and v["exit_price"] == 95.0


def test_a_gap_under_the_stop_exits_at_the_open_and_not_at_the_stop():
    rows = [(98.0, 101.5, 97.5, 101.0), (101.5, 103.0, 100.0, 102.0), (93.0, 94.0, 92.0, 93.5)]
    v = fwd(rows).value
    assert v["outcome"] == "stopped_gap" and v["exit_price"] == 93.0 and v["return_net"] == pytest.approx((93.0 - 101.51) / 101.51)


def test_a_close_based_stop_ignores_the_wick_and_exits_at_the_next_open():
    rows = [(98.0, 101.5, 97.5, 101.0), (101.5, 103.0, 100.0, 102.0), (102.0, 103.0, 94.0, 96.0), (95.5, 96.0, 93.0, 94.0), (93.5, 94.0, 92.0, 93.0)]
    wick = fwd(rows, stop_on="close").value
    assert wick["outcome"] == "stopped_at_close_exit_next_open"
    assert wick["exit_date"] == "2024-01-09" and wick["exit_price"] == pytest.approx(93.49)
    low_based = fwd(rows).value
    assert low_based["exit_date"] == "2024-01-05" and low_based["exit_price"] == 95.0                  # the wick of that bar already stopped the low based rule


def test_other_endings_of_a_detection():
    assert fwd([(98.0, 99.5, 97.5, 99.0), (99.0, 99.9, 98.0, 99.5)]).value["outcome"] == "no_breakout"
    assert fwd([(98.0, 99.5, 97.5, 99.0), (99.0, 101.5, 98.0, 101.0)]).value["outcome"] == "breakout_without_next_bar"
    calm = [(98.0, 101.5, 97.5, 101.0)] + [(101.0 + i * 0.1, 102.0 + i * 0.1, 100.5, 101.5 + i * 0.1) for i in range(5)]
    v = fwd(calm, horizon=3).value
    assert v["outcome"] == "open_at_horizon" and v["exit_date"] == "2024-01-08"
    assert v["exit_price"] == pytest.approx(101.7)                                                       # the close of the last bar of the horizon


def test_forward_outcome_bad_input():
    rows = [(98.0, 101.5, 97.5, 101.0), (101.5, 103.0, 100.0, 102.0)]
    assert fwd(rows, stop_on="wick").value is None
    assert vcp.vcp_forward_outcome(after_bars(rows), detection_date="2024-01-02", pivot=100.0, stop=105.0).value is None
    assert vcp.vcp_forward_outcome(after_bars(rows), detection_date="2024-01-02", pivot=100.0, stop=95.0, horizon=0).value is None
    assert vcp.vcp_forward_outcome(after_bars(rows)[:0], detection_date="2024-01-02", pivot=100.0, stop=95.0).value is None
    nan = after_bars(rows)
    nan.iloc[0, 3] = np.nan
    assert vcp.vcp_forward_outcome(nan, detection_date="2024-01-02", pivot=100.0, stop=95.0).value is None
    with pytest.raises(BadInput):
        fwd(rows, tick="x")
    with pytest.raises(BadInput):
        fwd(rows, horizon=1.5)
    with pytest.raises(BadInput):
        vcp.vcp_forward_outcome(after_bars(rows), detection_date=5, pivot=100.0, stop=95.0)


def test_registered_and_called_through_the_registry():
    assert registry.describe("indicators.screens.vcp").piece_ids == ("CTS-020",)
    assert registry.describe("indicators.screens.vcp_forward_outcome").piece_ids == ("CTS-020",)
    bars = vcp_bars()
    assert registry.call("indicators.screens.vcp", bars=bars, as_of=stamp(bars, len(bars) - 1)).value["state"] == "Pre-breakout"
    rows = [(98.0, 101.5, 97.5, 101.0), (101.5, 103.0, 100.0, 102.0)]
    res = registry.call("indicators.screens.vcp_forward_outcome", bars=after_bars(rows), detection_date="2024-01-02", pivot=100.0, stop=95.0)
    assert res.value["entry_price"] == pytest.approx(101.51)


def test_on_the_fixed_daily_bars_the_screen_runs(spy):
    res = vcp.vcp(spy, as_of=stamp(spy, len(spy) - 1))
    assert res.ok and res.value["trend_template"]["passed"] in range(0, 8)
    assert res.value["state"] in {None, "Pre-breakout", "Breakout", "Early-post-breakout", "Extended", "Damaged"}

"""FT-077: breadth and volume flow indicators against hand written values, the library functions and bad input."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators import breadth

from .helpers import B, assert_parity, column, latest, tiny

IDX = pd.bdate_range("2024-01-02", periods=5)
CLOSES = pd.DataFrame({"A": [10, 11, 12, 11, 12], "B": [20, 19, 18, 19, 20], "C": [5, 5, 6, 7, 6]}, index=IDX, dtype=float)
VOLUMES = pd.DataFrame({"A": [100.0] * 5, "B": [200.0] * 5, "C": [300.0] * 5}, index=IDX)


def test_advance_decline_counts_by_hand():
    res = breadth.advance_decline(CLOSES)
    assert latest(res) == {"advancers": 2.0, "decliners": 1.0, "unchanged": 0.0, "net_advances": 1.0, "ad_line": 3.0, "ad_ratio": 2.0}
    assert list(column(res, "ad_line")) == [0.0, 1.0, 2.0, 3.0]
    assert latest(res)["advancers"] + latest(res)["decliners"] + latest(res)["unchanged"] == 3


def test_mcclellan_raw_and_ratio_adjusted_by_hand():
    raw = breadth.mcclellan(CLOSES, short_window=2, long_window=3, ratio_adjusted=False)
    assert latest(raw, "oscillator") == pytest.approx(1 / 9)
    assert latest(raw, "summation_index") == pytest.approx(5 / 18)
    scaled = breadth.mcclellan(CLOSES, short_window=2, long_window=3)
    assert latest(scaled, "oscillator") == pytest.approx(1000 / 27)


def test_mcclellan_lines_scale_the_windows():
    big = pd.DataFrame(np.cumsum(np.random.default_rng(1).normal(size=(120, 4)), axis=0) + 100, index=pd.bdate_range("2023-01-02", periods=120))
    assert breadth.mcclellan(big, line="DT").params["short_window"] == 10
    assert breadth.mcclellan(big, line="SWO").params["short_window"] == 19
    assert breadth.mcclellan(big, line="LP").params["short_window"] == 38
    assert breadth.mcclellan(big, line="LP", short_window=5).params["short_window"] == 5


def test_trin_by_hand():
    assert latest(breadth.trin(CLOSES, VOLUMES), "trin") == pytest.approx(2.0)
    assert_parity(breadth.trin(CLOSES, VOLUMES, last_n=100), "trin", B.get_trin(CLOSES, VOLUMES).iloc[1:])


def test_trin_is_undefined_when_nothing_declines():
    up = pd.DataFrame({"A": [1.0, 2.0, 3.0], "B": [1.0, 2.0, 3.0]}, index=IDX[:3])
    res = breadth.trin(up, up * 0 + 100)
    assert res.value is None and res.warnings


def test_new_highs_lows_by_hand_and_percent_above_average():
    res = breadth.new_highs_lows(CLOSES, window=3)
    assert latest(res) == {"new_highs": 1.0, "new_lows": 0.0, "net": 1.0}
    assert latest(breadth.percent_above_average(CLOSES, window=3), "percent_above_average") == pytest.approx(2 / 3)


def test_single_instrument_flow_by_hand():
    bars = tiny()
    assert latest(breadth.obv(bars), "obv") == 700.0
    assert latest(breadth.accumulation_distribution(bars), "accumulation_distribution") == pytest.approx(1100.0)
    assert latest(breadth.chaikin_money_flow(bars, window=3), "chaikin_money_flow") == pytest.approx(1 / 3)
    assert latest(breadth.negative_volume_index(bars), "negative_volume_index") == pytest.approx(1276.9775678866586)
    assert latest(breadth.positive_volume_index(bars), "positive_volume_index") == pytest.approx(1174.648668639053)
    assert latest(breadth.price_volume_trend(bars), "price_volume_trend") == pytest.approx(129.28321678321672)
    assert latest(breadth.ease_of_movement(bars, window=3), "ease_of_movement") == pytest.approx(455555.5555555555)


def test_single_instrument_flow_equals_the_library(spy):
    n = 100_000
    assert_parity(breadth.obv(spy, last_n=n), "obv", B.get_on_balance_volume(spy["close"], spy["volume"]))
    assert_parity(breadth.accumulation_distribution(spy, last_n=n), "accumulation_distribution",
                  B.get_accumulation_distribution_line(spy["high"], spy["low"], spy["close"], spy["volume"]))
    assert_parity(breadth.chaikin_oscillator(spy, last_n=n), "chaikin_oscillator",
                  B.get_chaikin_oscillator(spy["high"], spy["low"], spy["close"], spy["volume"], 3, 10))
    assert_parity(breadth.chaikin_money_flow(spy, last_n=n), "chaikin_money_flow",
                  B.get_chaikin_money_flow(spy["high"], spy["low"], spy["close"], spy["volume"], 20))
    assert_parity(breadth.price_volume_trend(spy, last_n=n), "price_volume_trend", B.get_price_volume_trend(spy["close"], spy["volume"]))


def test_chaikin_oscillator_needs_short_below_long():
    res = breadth.chaikin_oscillator(tiny(), short_window=10, long_window=3)
    assert res.value is None and "short_window" in res.warnings[0]


@pytest.mark.parametrize("fn", [breadth.obv, breadth.accumulation_distribution, breadth.chaikin_money_flow])
def test_empty_nan_and_out_of_domain_give_no_value(fn):
    empty = pd.DataFrame({c: [] for c in ("open", "high", "low", "close", "volume")}, dtype=float)
    assert fn(empty).value is None
    bad = tiny()
    bad.loc[bad.index[3], "close"] = np.nan
    res = fn(bad)
    assert res.value is None and "NaN" in res.warnings[0]
    zero = fn(tiny(), window=0) if fn is breadth.chaikin_money_flow else fn(tiny().assign(volume=-1.0))
    assert zero.value is None and zero.warnings


def test_panel_with_nan_or_wrong_shape():
    nan_panel = CLOSES.copy()
    nan_panel.iloc[:, :] = np.nan
    assert breadth.advance_decline(nan_panel).value is None
    assert breadth.advance_decline(CLOSES.iloc[:0]).value is None
    with pytest.raises(BadInput):
        breadth.advance_decline([1, 2, 3])
    with pytest.raises(BadInput):
        breadth.trin(CLOSES, VOLUMES.iloc[:, :2])
    with pytest.raises(BadInput):
        breadth.obv(tiny().drop(columns="volume"))


def test_registered_with_the_piece_id():
    names = {s.name for s in registry.list("indicators") if "FT-077" in s.piece_ids}
    assert {"indicators.advance_decline", "indicators.mcclellan", "indicators.trin", "indicators.new_highs_lows", "indicators.obv",
            "indicators.accumulation_distribution", "indicators.chaikin_oscillator", "indicators.chaikin_money_flow",
            "indicators.negative_volume_index", "indicators.positive_volume_index", "indicators.price_volume_trend",
            "indicators.ease_of_movement", "indicators.percent_above_average"} <= names


def test_call_through_the_registry_applies_the_limits():
    res = registry.call("indicators.obv", bars=tiny(), last_n=3)
    assert len(res.value["index"]) == 3
    with pytest.raises(BadInput):
        registry.call("indicators.obv", bars=tiny(), bogus=1)
    with pytest.raises(BadInput):
        registry.call("indicators.obv", bars=tiny(), last_n=2_000_000)

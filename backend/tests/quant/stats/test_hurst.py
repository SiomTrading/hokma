"""Hurst exponent. Row FT-059."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.stats import _lib, hurst

from .conftest import call, piece_ids, rng


def test_hurst_FT_059_library_parity_and_hand_value(daily_returns):
    levels = np.log(400.0) + daily_returns.cumsum()
    res = call("stats.hurst", series=levels)
    origin = float(_lib.ft_risk_model.get_hurst_exponent(pd.Series(levels.to_numpy()), max_lag=20))
    assert res.value["hurst"] == pytest.approx(origin, abs=1e-8)
    # hand value: slope of log(std of the lag differences) on log(lag), lags 2 to 20
    arr = levels.to_numpy()
    lags = np.arange(2, 21)
    sds = np.array([np.std(arr[k:] - arr[:-k]) for k in lags])
    assert res.value["hurst"] == pytest.approx(np.polyfit(np.log(lags), np.log(sds), 1)[0], abs=1e-10)
    assert res.value["null_interval_95"][0] < 0.5 < res.value["null_interval_95"][1]
    assert "FT-059" in piece_ids("stats.hurst")


def test_hurst_FT_059_alternating_series_has_exponent_zero():
    # odd lag differences are always +1 or -1 (standard deviation 1) and even lag differences are 0: the exponent is exactly 0
    series = pd.Series([0.0, 1.0] * 20 + [0.0])         # 41 points: every odd lag has as many +1 as -1 differences
    res = hurst.hurst(series, max_lag=8)
    assert res.value["hurst"] == pytest.approx(0.0, abs=1e-12)
    assert res.value["reading"] == "mean_reverting"


def test_hurst_FT_059_readings_on_known_processes():
    walk = pd.Series(rng(1).standard_normal(1000).cumsum())
    res = call("stats.hurst", series=walk)
    assert 0.4 < res.value["hurst"] < 0.6
    assert res.value["reading"] == "indistinguishable_from_random_walk"
    g = rng(2)
    increments = np.zeros(1000)
    for t in range(1, 1000):
        increments[t] = 0.9 * increments[t - 1] + g.standard_normal()
    trending = pd.Series(increments.cumsum())
    assert call("stats.hurst", series=trending).value["reading"] == "persistent"
    reverting = pd.Series(g.standard_normal(1000))                      # white noise levels revert completely
    assert call("stats.hurst", series=reverting).value["reading"] == "mean_reverting"


def test_hurst_FT_059_rescaled_range_and_returns_input(daily_returns):
    levels = pd.Series(np.concatenate([[0.0], daily_returns.cumsum().to_numpy()]))
    by_levels = call("stats.hurst", series=levels, method="rescaled_range")
    by_returns = call("stats.hurst", series=daily_returns, method="rescaled_range", input_kind="returns")
    assert by_levels.value["hurst"] == pytest.approx(by_returns.value["hurst"], abs=1e-12)
    # hand value: R/S of one window of 8 equal-sized steps with known dispersion
    chunk = np.array([1.0, -1.0, 2.0, -2.0, 1.0, -1.0, 0.5, -0.5])
    sizes = (8, 16, 32, 64)
    rs = [np.ptp(np.cumsum(np.tile(chunk, s // 8) - np.tile(chunk, s // 8).mean())) / np.tile(chunk, s // 8).std() for s in sizes]
    expected = np.polyfit(np.log(sizes), np.log(rs), 1)[0]
    assert hurst.rescaled_range_exponent(np.tile(chunk, 16)) == pytest.approx(expected, abs=1e-10)
    assert any("biased" in w for w in by_levels.warnings)


def test_hurst_FT_059_empty_nan_and_out_of_domain(daily_returns):
    assert call("stats.hurst", series=pd.Series([], dtype=float)).value is None
    assert call("stats.hurst", series=pd.Series(np.ones(100))).value is None
    assert call("stats.hurst", series=pd.Series(np.arange(10.0))).value is None
    assert call("stats.hurst", series=daily_returns.cumsum(), method="wavelet").value is None
    assert call("stats.hurst", series=daily_returns.cumsum(), input_kind="prices").value is None
    assert call("stats.hurst", series=daily_returns.cumsum(), max_lag=2).value is None
    nan = daily_returns.cumsum()
    nan.iloc[4] = np.nan
    res = call("stats.hurst", series=nan)
    assert res.value is not None and any("dropped" in w for w in res.warnings)
    assert call("stats.hurst", series=pd.Series(np.full(100, np.nan))).value is None
    with pytest.raises(BadInput):
        call("stats.hurst", series=daily_returns.cumsum(), max_lag=2.5)
    with pytest.raises(BadInput):
        call("stats.hurst", series=daily_returns.cumsum(), max_lag=5000)
    with pytest.raises(BadInput):
        call("stats.hurst", series="abc")

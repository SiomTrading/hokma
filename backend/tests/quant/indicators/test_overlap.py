"""FT-079: moving averages, VWAP, SAR, pivots, support and resistance, Fibonacci: hand values, the library, causality, bad input."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.indicators import overlap

from .helpers import O, assert_parity, column, latest, tiny

N = 100_000


def test_averages_by_hand():
    bars = tiny()
    assert latest(overlap.sma(bars, window=3), "sma") == pytest.approx(46 / 3)
    assert latest(overlap.ema(bars, window=3), "ema") == pytest.approx(15.0)
    assert latest(overlap.wma(bars, window=3), "wma") == pytest.approx(92 / 6)
    assert latest(overlap.vwma(bars, window=3), "vwma") == pytest.approx(15.290322580645162)
    assert latest(overlap.hull(bars, window=4), "hull") == pytest.approx(15.855555555555554)
    assert latest(overlap.kama(bars, window=3), "kama") == pytest.approx(13.438070222190078)


def test_pivots_and_fibonacci_by_hand():
    bars = tiny()
    piv = latest(overlap.pivot_points(bars))
    assert piv["pivot"] == pytest.approx(47 / 3)
    assert piv["r1"] == pytest.approx(52 / 3) and piv["s1"] == pytest.approx(43 / 3)
    assert piv["r2"] == pytest.approx(56 / 3) and piv["s2"] == pytest.approx(38 / 3)
    assert piv["r3"] == pytest.approx(61 / 3) and piv["s3"] == pytest.approx(34 / 3)
    fib = latest(overlap.fibonacci(bars, window=3))
    assert fib["level_0_0"] == pytest.approx(17.0) and fib["level_100_0"] == pytest.approx(13.0)
    assert fib["level_38_2"] == pytest.approx(17 - 0.382 * 4) and fib["level_61_8"] == pytest.approx(17 - 0.618 * 4)
    down = latest(overlap.fibonacci(bars, window=3, trend="downtrend"))
    assert down["level_0_0"] == pytest.approx(13.0) and down["level_100_0"] == pytest.approx(17.0)


def test_pivot_uses_only_the_previous_bar():
    bars = tiny()
    changed = bars.copy()
    changed.loc[changed.index[-1], ["high", "low", "close"]] = [999.0, 1.0, 500.0]
    assert latest(overlap.pivot_points(changed)) == latest(overlap.pivot_points(bars))


def test_session_vwap_restarts_each_session_by_hand():
    first = pd.date_range("2024-01-02 09:30", periods=3, freq="5min")
    second = pd.date_range("2024-01-03 09:30", periods=2, freq="5min")
    bars = pd.DataFrame({"high": [10, 11, 12, 20, 22], "low": [8, 9, 10, 18, 20], "close": [9, 10, 11, 19, 21],
                         "volume": [100, 200, 100, 50, 50]}, index=first.append(second), dtype=float)
    res = overlap.session_vwap(bars, last_n=10)
    np.testing.assert_allclose(column(res, "session_vwap").to_numpy(), [9.0, 29 / 3, 10.0, 19.0, 20.0])


def test_session_vwap_refuses_daily_bars():
    res = overlap.session_vwap(tiny())
    assert res.value is None and "intraday" in res.warnings[0]
    weekly = tiny().resample("W-FRI").last()
    assert overlap.session_vwap(weekly).value is None


def test_support_resistance_publishes_only_after_confirmation():
    closes = [5, 6, 9, 6, 5, 6, 7, 6, 5, 4, 5, 6]
    bars = pd.DataFrame({"close": closes}, index=pd.bdate_range("2024-01-02", periods=len(closes)), dtype=float)
    res = overlap.support_resistance(bars, window=2, sensitivity=0.05, last_n=20)
    resistance, support = column(res, "resistance"), column(res, "support")
    assert resistance.iloc[:4].isna().all()                     # the peak at position 2 is not known before position 4
    assert resistance.iloc[4] == 9.0 and resistance.iloc[7] == 9.0 and resistance.iloc[8] == 7.0
    assert support.iloc[:6].isna().all() and support.iloc[6] == 5.0 and support.iloc[10] == 5.0 and support.iloc[11] == 4.0


def test_parabolic_sar_trails_below_price_in_an_uptrend():
    n = 40
    close = np.linspace(10, 30, n)
    bars = pd.DataFrame({"high": close + 0.5, "low": close - 0.5, "close": close}, index=pd.bdate_range("2024-01-02", periods=n))
    res = overlap.parabolic_sar(bars, last_n=n)
    sar = column(res, "sar").dropna()
    assert (sar < bars["close"].loc[pd.to_datetime(sar.index)].to_numpy()).all()
    assert latest(res, "trend") == 1.0
    assert overlap.parabolic_sar(bars, af_start=0.5, af_max=0.2).value is None


def test_equal_to_the_library_on_the_fixed_bars(spy):
    h, l_, c, v = spy["high"], spy["low"], spy["close"], spy["volume"]
    assert_parity(overlap.sma(spy, last_n=N), "sma", O.get_moving_average(c, 50))
    assert_parity(overlap.ema(spy, last_n=N), "ema", O.get_exponential_moving_average(c, 21))
    assert_parity(overlap.wma(spy, last_n=N), "wma", O.get_weighted_moving_average(c, 20))
    assert_parity(overlap.kama(spy, last_n=N), "kama", O.get_kaufman_adaptive_moving_average(c, 10, 2, 30))
    assert_parity(overlap.hull(spy, last_n=N), "hull", O.get_hull_moving_average(c, 20))
    assert_parity(overlap.parabolic_sar(spy, last_n=N), "sar", O.get_parabolic_sar(h, l_, 0.02, 0.02, 0.2))
    piv = O.get_pivot_points(h, l_, c)
    assert_parity(overlap.pivot_points(spy, last_n=N), "s3", piv["Support 3"])
    assert_parity(overlap.support_resistance(spy, last_n=N), "support", O.get_support_resistance_levels(c, 5, 0.05)["Support"])
    vwma = (c * v).rolling(20).sum() / v.rolling(20).sum()
    assert_parity(overlap.vwma(spy, last_n=N), "vwma", vwma)


def test_averages_are_causal(spy):
    cut = spy.iloc[:700]
    for fn, col in ((overlap.sma, "sma"), (overlap.ema, "ema"), (overlap.kama, "kama"), (overlap.hull, "hull"), (overlap.parabolic_sar, "sar")):
        full = column(fn(spy, last_n=N), col)
        part = column(fn(cut, last_n=N), col)
        np.testing.assert_allclose(full.loc[part.index].to_numpy(), part.to_numpy(), atol=1e-9, equal_nan=True)


def test_lines_scale_the_windows(spy):
    assert overlap.sma(spy, line="DT").params["window"] == 25
    assert overlap.sma(spy, line="SWC").params["window"] == 50
    assert overlap.sma(spy, line="LP").params["window"] == 100
    assert overlap.kama(spy, line="LP").params["fast_window"] == 4


@pytest.mark.parametrize("fn", [overlap.sma, overlap.ema, overlap.hull, overlap.kama, overlap.pivot_points, overlap.fibonacci])
def test_empty_nan_and_out_of_domain_give_no_value(fn):
    empty = pd.DataFrame({c: [] for c in ("open", "high", "low", "close", "volume")}, dtype=float)
    assert fn(empty).value is None
    nan_bars = tiny()
    nan_bars.loc[nan_bars.index[2], "close"] = np.nan
    assert fn(nan_bars).value is None
    if fn not in (overlap.pivot_points,):
        assert fn(tiny(), window=0).value is None
    assert fn(tiny().iloc[:1]).value is None


def test_bad_types_and_options():
    with pytest.raises(BadInput):
        overlap.sma(tiny(), window="x")
    with pytest.raises(BadInput):
        overlap.fibonacci(tiny(), trend=5)
    assert overlap.fibonacci(tiny(), window=3, trend="sideways").value is None
    assert overlap.kama(tiny(), window=3, fast_window=30, slow_window=2).value is None

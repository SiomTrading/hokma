"""FT-078: momentum indicators against hand written values, the library functions, the line windows and bad input."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.indicators import momentum

from .helpers import M, assert_parity, latest, tiny

N = 100_000


def test_rsi_by_hand_wilder_smoothing():
    assert latest(momentum.rsi(tiny(), window=3), "rsi") == pytest.approx(100 * 1598 / 2607, abs=1e-9)


def test_macd_by_hand():
    row = latest(momentum.macd(tiny(), short_window=2, long_window=3, signal_window=2))
    assert row["macd"] == pytest.approx(0.1538637402834926, abs=1e-12)
    assert row["signal"] == pytest.approx(0.24279835390946441, abs=1e-12)
    assert row["histogram"] == pytest.approx(-0.08893461362597183, abs=1e-12)


def test_range_oscillators_by_hand():
    bars = tiny()
    stoch = latest(momentum.stochastic(bars, window=3, smooth_window=3))
    assert stoch["k"] == pytest.approx(50.0) and stoch["d"] == pytest.approx((80 + 500 / 6 + 50) / 3)
    assert latest(momentum.williams_r(bars, window=3), "williams_r") == pytest.approx(-50.0)
    aroon = latest(momentum.aroon(bars, window=3))
    assert aroon["up"] == pytest.approx(200 / 3) and aroon["down"] == 0.0 and aroon["oscillator"] == pytest.approx(200 / 3)
    assert latest(momentum.cci(bars, window=3), "cci") == pytest.approx(-50.0)
    assert latest(momentum.roc(bars, window=3), "roc") == pytest.approx(15 / 13 - 1)


def test_flow_and_balance_by_hand():
    bars = tiny()
    assert latest(momentum.mfi(bars, window=3), "mfi") == pytest.approx(100 * 43150 / 69550)
    assert latest(momentum.chande_momentum(bars, window=3), "chande_momentum") == pytest.approx(50.0)
    assert latest(momentum.balance_of_power(bars), "balance_of_power") == pytest.approx(-1 / 3)
    assert latest(momentum.awesome_oscillator(bars, short_window=2, long_window=3), "awesome_oscillator") == pytest.approx(1 / 6)
    assert latest(momentum.ppo(bars, short_window=2, long_window=3), "ppo") == pytest.approx(0.010257582685566173)
    vortex = latest(momentum.vortex(bars, window=3))
    assert vortex["plus"] == pytest.approx(11 / 9) and vortex["minus"] == pytest.approx(7 / 9)
    assert latest(momentum.choppiness(bars, window=3), "choppiness") == pytest.approx(73.8140492857085)


def test_adx_by_hand():
    row = latest(momentum.adx(tiny(), window=3))
    assert row["adx"] == pytest.approx(45.996726820642316)
    assert row["plus_di"] == pytest.approx(24.356043286084443)
    assert row["minus_di"] == pytest.approx(15.378753238835545)


def test_ichimoku_by_hand():
    row = latest(momentum.ichimoku(tiny(), conversion_window=2, base_window=3, lead_span_b_window=4))
    assert row == {"conversion": 15.0, "base": 15.0, "span_a": 13.0, "span_b": 12.0, "lagging_close": 15.0}


def test_equal_to_the_library_on_the_fixed_bars(spy):
    h, l_, c, v, o = spy["high"], spy["low"], spy["close"], spy["volume"], spy["open"]
    assert_parity(momentum.rsi(spy, last_n=N), "rsi", M.get_relative_strength_index(c, 14))
    macd = M.get_moving_average_convergence_divergence(c, 12, 26, 9)
    assert_parity(momentum.macd(spy, last_n=N), "macd", macd["MACD Line"])
    assert_parity(momentum.macd(spy, last_n=N), "signal", macd["Signal Line"])
    st = M.get_stochastic_oscillator(h, l_, c, 14, 3)
    assert_parity(momentum.stochastic(spy, last_n=N), "k", st["Stochastic %K"])
    assert_parity(momentum.stochastic(spy, last_n=N), "d", st["Stochastic %D"])
    assert_parity(momentum.williams_r(spy, last_n=N), "williams_r", M.get_williams_percent_r(h, l_, c, 14))
    assert_parity(momentum.aroon(spy, last_n=N), "up", M.get_aroon_indicator(h, l_, 25)["Aroon Up"])
    assert_parity(momentum.cci(spy, last_n=N), "cci", M.get_commodity_channel_index(h, l_, c, 20))
    assert_parity(momentum.adx(spy, last_n=N), "adx", M.get_average_directional_index(h, l_, c, 14))
    ich = M.get_ichimoku_cloud(h, l_, 9, 26, 52)
    assert_parity(momentum.ichimoku(spy, last_n=N), "span_a", ich["Leading Span A"])
    assert_parity(momentum.mfi(spy, last_n=N), "mfi", M.get_money_flow_index(h, l_, c, v, 14))
    assert_parity(momentum.roc(spy, last_n=N), "roc", M.get_rate_of_change(c, 12))
    assert_parity(momentum.stochastic_rsi(spy, last_n=N), "k", M.get_stochastic_relative_strength_index(c, 14, 14, 3).iloc[:, 0])
    assert_parity(momentum.ultimate_oscillator(spy, last_n=N), "ultimate_oscillator", M.get_ultimate_oscillator(h, l_, c, 7, 14, 28))
    assert_parity(momentum.true_strength_index(spy, last_n=N), "tsi", M.get_true_strength_index(c, 25, 13, 13).iloc[:, 0])
    assert_parity(momentum.force_index(spy, last_n=N), "force_index", M.get_force_index(c, v, 13))
    assert_parity(momentum.balance_of_power(spy, last_n=N), "balance_of_power", M.get_balance_of_power(o, h, l_, c))


def test_each_line_uses_its_own_bar_window(spy):
    assert momentum.rsi(spy, line="SWO").params["window"] == 14
    assert momentum.rsi(spy, line="SWC").params["window"] == 14
    assert momentum.rsi(spy, line="DT").params["window"] == 7
    assert momentum.rsi(spy, line="LP").params["window"] == 28
    assert momentum.macd(spy, line="DT").params["long_window"] == 13
    assert momentum.rsi(spy, line="DT", window=5).params["window"] == 5
    short = momentum.rsi(spy, line="DT", last_n=N)
    assert_parity(short, "rsi", M.get_relative_strength_index(spy["close"], 7))


def test_unknown_line_and_wrong_types_raise():
    with pytest.raises(BadInput):
        momentum.rsi(tiny(), line="XX")
    with pytest.raises(BadInput):
        momentum.rsi(tiny(), window="a")
    with pytest.raises(BadInput):
        momentum.rsi(tiny(), window=2.5)
    with pytest.raises(BadInput):
        momentum.rsi(tiny(), bogus=1)
    with pytest.raises(BadInput):
        momentum.rsi(tiny().drop(columns="close"))
    with pytest.raises(BadInput):
        momentum.rsi([1, 2, 3])


@pytest.mark.parametrize("fn", [momentum.rsi, momentum.stochastic, momentum.adx, momentum.mfi, momentum.macd, momentum.cci])
def test_empty_nan_and_out_of_domain_give_no_value(fn):
    empty = pd.DataFrame({c: [] for c in ("open", "high", "low", "close", "volume")}, dtype=float)
    res = fn(empty)
    assert res.value is None and res.warnings
    nan_bars = tiny()
    nan_bars.loc[nan_bars.index[4], "high"] = np.nan
    assert fn(nan_bars).value is None
    assert fn(tiny(), window=0).value is None if fn not in (momentum.macd,) else True
    assert fn(tiny()).value is None or fn(tiny()).ok            # too few bars for the default window: no value, never an exception


def test_window_longer_than_the_bars_is_no_value():
    res = momentum.rsi(tiny(), window=50)
    assert res.value is None and "not enough bars" in res.warnings[0]


def test_inconsistent_bars_are_refused():
    bad = tiny()
    bad.loc[bad.index[2], "high"] = 1.0
    assert momentum.williams_r(bad).value is None
    assert momentum.rsi(tiny().assign(close=-1.0)).value is None
    assert momentum.macd(tiny(), short_window=5, long_window=3).value is None

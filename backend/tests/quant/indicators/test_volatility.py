"""FT-080: true range, ATR, supertrend, Keltner, Donchian, Bollinger and the volatility cone: hand values, the library, bad input."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.indicators import volatility

from .helpers import V, assert_parity, column, latest, tiny

N = 100_000


def test_range_indicators_by_hand():
    bars = tiny()
    assert latest(volatility.true_range(bars), "true_range") == 3.0
    assert latest(volatility.atr(bars, window=3), "atr") == pytest.approx(3.0)
    kel = latest(volatility.keltner(bars, window=3, atr_window=3, atr_multiplier=2.0))
    assert kel == {"upper": pytest.approx(21.0), "middle": pytest.approx(15.0), "lower": pytest.approx(9.0)}
    don = latest(volatility.donchian(bars, window=3))
    assert don == {"upper": 17.0, "middle": 14.0, "lower": 11.0}


def test_bollinger_by_hand_uses_the_population_deviation():
    row = latest(volatility.bollinger(tiny(), window=3, num_std_dev=2.0))
    assert row["middle"] == pytest.approx(46 / 3)
    assert row["upper"] == pytest.approx(16.2761423749154) and row["lower"] == pytest.approx(14.390524291751271)
    assert row["percent_b"] == pytest.approx(0.32322330470336247) and row["bandwidth"] == pytest.approx(0.12297509238026913)


def test_donchian_excludes_the_current_bar():
    bars = tiny()
    spike = bars.copy()
    spike.loc[spike.index[-1], ["high", "close"]] = [500.0, 400.0]
    assert latest(volatility.donchian(spike, window=3), "upper") == 17.0


def test_volatility_cone_by_hand():
    row = latest(volatility.volatility_cone(tiny(), window=3))
    assert row["realised"] == pytest.approx(1.6642641439789274)
    assert row["min"] == pytest.approx(1.6343027388776787) and row["max"] == pytest.approx(2.062057355138695)
    assert row["min"] <= row["p10"] <= row["p25"] <= row["median"] <= row["p75"] <= row["p90"] <= row["max"]
    assert 0.0 <= row["percentile_rank"] <= 1.0


def test_supertrend_direction_is_one_or_minus_one(spy):
    res = volatility.supertrend(spy, last_n=N)
    direction = column(res, "direction").dropna()
    assert set(direction.unique()) <= {1.0, -1.0}
    sup = column(res, "supertrend").dropna()
    close = spy["close"].loc[pd.to_datetime(sup.index)].to_numpy()
    assert ((direction.to_numpy() == 1.0) <= (sup.to_numpy() <= close + 1e-9)).all()      # an up trend line sits under the close


def test_equal_to_the_library_on_the_fixed_bars(spy):
    h, l_, c = spy["high"], spy["low"], spy["close"]
    assert_parity(volatility.atr(spy, last_n=N), "atr", V.get_average_true_range(h, l_, c, 14))
    st = V.get_supertrend(h, l_, c, 10, 3.0)
    assert_parity(volatility.supertrend(spy, last_n=N), "supertrend", st["Supertrend"])
    assert_parity(volatility.supertrend(spy, last_n=N), "direction", st["Trend Direction"])
    kel = V.get_keltner_channels(h, l_, c, 20, 10, 2.0)
    assert_parity(volatility.keltner(spy, last_n=N), "upper", kel["Upper Line"])
    don = V.get_donchian_channels(h, l_, 20)
    assert_parity(volatility.donchian(spy, last_n=N), "lower", don["Lower Channel"])
    bol = V.get_bollinger_bands(c, 20, 2.0)
    assert_parity(volatility.bollinger(spy, last_n=N), "upper", bol["Upper Band"])
    ch = V.get_chandelier_exit(h, l_, c, 22, 3.0)
    assert_parity(volatility.chandelier_exit(spy, last_n=N), "long_exit", ch.iloc[:, 0])
    cone = V.get_volatility_cone(c, [20])
    assert latest(volatility.volatility_cone(spy), "median") == pytest.approx(float(cone.loc[20, "Median"]), abs=1e-8)
    realised = np.log(c / c.shift(1)).rolling(20).std() * np.sqrt(252)
    assert latest(volatility.volatility_cone(spy), "p90") == pytest.approx(float(realised.quantile(0.9)), abs=1e-8)


def test_cone_follows_the_line_window(spy):
    assert volatility.volatility_cone(spy, line="DT").params["window"] == 10
    assert volatility.volatility_cone(spy, line="SWO").params["window"] == 20
    assert volatility.atr(spy, line="LP").params["window"] == 28


@pytest.mark.parametrize("fn", [volatility.atr, volatility.keltner, volatility.donchian, volatility.bollinger, volatility.supertrend, volatility.volatility_cone])
def test_empty_nan_and_out_of_domain_give_no_value(fn):
    empty = pd.DataFrame({c: [] for c in ("open", "high", "low", "close", "volume")}, dtype=float)
    assert fn(empty).value is None
    nan_bars = tiny()
    nan_bars.loc[nan_bars.index[5], "low"] = np.nan
    assert fn(nan_bars).value is None
    assert fn(tiny(), **({"window": 0} if fn is not volatility.supertrend else {"multiplier": -1.0})).value is None


def test_a_constant_price_gives_a_flat_band_or_no_value():
    flat = tiny().assign(open=10.0, high=10.0, low=10.0, close=10.0)
    res = volatility.bollinger(flat, window=3)
    assert latest(res, "upper") == 10.0 and latest(res, "bandwidth") == 0.0
    assert latest(volatility.volatility_cone(flat, window=3), "realised") == 0.0


def test_wrong_types_raise():
    with pytest.raises(BadInput):
        volatility.atr(tiny(), window=1.5)
    with pytest.raises(BadInput):
        volatility.bollinger(tiny(), num_std_dev="two")
    with pytest.raises(BadInput):
        volatility.atr(tiny().drop(columns="low"))

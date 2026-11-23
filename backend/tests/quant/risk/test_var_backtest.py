"""Backtest of VaR and ES models. Row FT-048 (and CC-06, which lists the backtest among the extras)."""
import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.risk import _lib

from .conftest import call, piece_ids

TOL = 1e-9
BT = _lib.ft_backtest


def _chi2_sf_1(x: float) -> float:
    return math.erfc(math.sqrt(x / 2.0))


def _xlogy(x: float, y: float) -> float:
    return 0.0 if x == 0 else x * math.log(y)


def _series(values, start="2021-01-04"):
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)))


@pytest.fixture
def rolling_var(ret):
    res = call("risk.rolling_var", returns=ret, window=250)
    return pd.Series(res.value["values"], index=ret.index[249:])


@pytest.fixture
def rolling_cvar(ret):
    res = call("risk.rolling_cvar", returns=ret, window=250)
    return pd.Series(res.value["values"], index=ret.index[249:])


def test_kupiec_FT_048(ret, rolling_var):
    res = call("risk.var_backtest", returns=ret, var_series=rolling_var, test="kupiec", level=0.95)
    origin = BT.get_kupiec_test(ret, -rolling_var, 0.05)
    assert res.value["statistic"] == pytest.approx(origin["Kupiec Statistic"], abs=TOL)
    assert res.value["p_value"] == pytest.approx(origin["P-Value"], abs=TOL)
    assert res.value["breaches"] == 35 and res.value["observations"] == 751


def test_kupiec_hand_value_FT_048():
    # 250 days, 10 breaches of a 2% VaR built for a 5% breach rate
    values = [0.001] * 250
    for i in range(10, 250, 25):
        values[i] = -0.05
    n, x, p = 250, 10, 0.05
    lr = -2 * (_xlogy(n - x, 1 - p) + _xlogy(x, p)) + 2 * (_xlogy(n - x, 1 - x / n) + _xlogy(x, x / n))
    res = call("risk.var_backtest", returns=_series(values), var_series=[0.02] * 250, test="kupiec", level=0.95)
    assert res.value["breaches"] == 10
    assert res.value["expected_breaches"] == pytest.approx(12.5, abs=TOL)
    assert res.value["statistic"] == pytest.approx(lr, abs=1e-9)
    assert res.value["p_value"] == pytest.approx(_chi2_sf_1(lr), abs=1e-9)
    assert res.value["rejected_at_5pct"] is False
    assert res.unit == "test_statistic" and res.method == "kupiec" and res.params["level"] == 0.95
    assert "FT-048" in piece_ids("risk.var_backtest")


def test_kupiec_rejects_a_model_that_breaches_too_often_FT_048():
    values = [0.001] * 200
    for i in range(0, 200, 5):
        values[i] = -0.05
    res = call("risk.var_backtest", returns=_series(values), var_series=[0.02] * 200, test="kupiec", level=0.95)
    assert res.value["breaches"] == 40
    assert res.value["rejected_at_5pct"] is True and res.value["p_value"] < 1e-6


def test_christoffersen_FT_048(ret, rolling_var):
    res = call("risk.var_backtest", returns=ret, var_series=rolling_var, test="christoffersen")
    origin = BT.get_christoffersen_test(ret, -rolling_var)
    assert res.value["statistic"] == pytest.approx(origin["Christoffersen Statistic"], abs=TOL)
    assert res.value["p_value"] == pytest.approx(origin["P-Value"], abs=TOL)


def test_christoffersen_hand_value_FT_048():
    # breaches arrive in pairs: the independence statistic follows the transition counts
    values = [0.001] * 120
    for i in (10, 11, 40, 41, 70, 71, 100, 101):
        values[i] = -0.05
    hits = np.array([1 if v < -0.02 else 0 for v in values])
    prev, cur = hits[:-1], hits[1:]
    n00 = int(((prev == 0) & (cur == 0)).sum()); n01 = int(((prev == 0) & (cur == 1)).sum())
    n10 = int(((prev == 1) & (cur == 0)).sum()); n11 = int(((prev == 1) & (cur == 1)).sum())
    pi01, pi11, pi = n01 / (n00 + n01), n11 / (n10 + n11), (n01 + n11) / (n00 + n01 + n10 + n11)
    restricted = _xlogy(n00 + n10, 1 - pi) + _xlogy(n01 + n11, pi)
    free = _xlogy(n00, 1 - pi01) + _xlogy(n01, pi01) + _xlogy(n10, 1 - pi11) + _xlogy(n11, pi11)
    lr = -2 * (restricted - free)
    res = call("risk.var_backtest", returns=_series(values), var_series=[0.02] * 120, test="christoffersen")
    assert res.value["statistic"] == pytest.approx(lr, abs=1e-9)
    assert res.value["p_value"] == pytest.approx(_chi2_sf_1(lr), abs=1e-9)
    assert res.value["p_value"] < 0.05


def test_acerbi_szekely_FT_048(ret, rolling_var, rolling_cvar):
    res = call("risk.var_backtest", returns=ret, var_series=rolling_var, cvar_series=rolling_cvar, test="acerbi_szekely", seed=42)
    origin = BT.get_acerbi_szekely_test(ret, -rolling_var, -rolling_cvar, 0.05, random_state=42)
    assert res.value["statistic"] == pytest.approx(origin["Acerbi-Szekely Statistic"], abs=TOL)
    assert res.value["p_value"] == pytest.approx(origin["P-Value"], abs=TOL)
    # the same seed gives the same bootstrap
    again = call("risk.var_backtest", returns=ret, var_series=rolling_var, cvar_series=rolling_cvar, test="acerbi_szekely", seed=42)
    assert again.value == res.value


def test_acerbi_szekely_hand_value_FT_048():
    # Z2 = sum(r * breach / ES) / (n * alpha) - 1 with 5 breaches of -6% against an ES of -3%
    values = [0.001] * 100
    for i in (5, 25, 45, 65, 85):
        values[i] = -0.06
    res = call("risk.var_backtest", returns=_series(values), var_series=[0.02] * 100, cvar_series=[0.03] * 100,
               test="acerbi_szekely", level=0.95)
    assert res.value["statistic"] == pytest.approx(5 * (0.06 / 0.03) / (100 * 0.05) - 1, abs=1e-9)
    assert res.value["breaches"] == 5


def test_backtest_bad_data_FT_048(ret, rolling_var):
    assert call("risk.var_backtest", returns=[], var_series=[]).value is None
    assert call("risk.var_backtest", returns=_series([0.01] * 50), var_series=[0.02] * 50).value is None
    assert call("risk.var_backtest", returns=_series([float("nan")] * 50), var_series=[0.02] * 50).value is None
    assert call("risk.var_backtest", returns=ret, var_series=rolling_var, test="acerbi_szekely").value is None
    assert call("risk.var_backtest", returns=ret, var_series=rolling_var, test="basel").value is None
    assert call("risk.var_backtest", returns=ret, var_series=rolling_var, level=2.0).value is None
    with pytest.raises(BadInput):
        call("risk.var_backtest", returns=ret, var_series=[0.02, 0.02])
    with pytest.raises(BadInput):
        call("risk.var_backtest", returns="not a series", var_series=[0.02])


def test_public_entry_point_FT_048(ret, rolling_var):
    from hokma.quant import risk

    assert risk.var_backtest(ret, rolling_var).value == call("risk.var_backtest", returns=ret, var_series=rolling_var).value
    assert risk.var_backtest(ret, rolling_var, test="christoffersen").method == "christoffersen"

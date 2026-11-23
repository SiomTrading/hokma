"""Drawdown risk. Rows FT-049, FT-050, FT-053, SK-023, SK-027, SK-028, SK-029 and SK-030."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.risk import _lib

from .conftest import call, piece_ids

TOL = 1e-9
M = _lib.sk_measures
FT = _lib.ft_risk_model


def _dd(series):
    return M.get_drawdowns(series.to_numpy(), compounded=True)


def _daily(series):
    out = series.copy()
    out.index = out.index.to_period("D")
    return out


@pytest.fixture
def tiny_dated(tiny):
    return pd.Series(tiny, index=pd.bdate_range("2021-01-04", periods=5))


def test_drawdown_stats_FT_049(ret, tiny_dated):
    res = call("risk.drawdown_stats", returns=ret)
    indexed = _daily(ret)
    assert res.value["max_drawdown"] == pytest.approx(-FT.get_max_drawdown(indexed), abs=TOL)
    assert res.value["duration"] == pytest.approx(FT.get_max_drawdown_duration(indexed), abs=TOL)
    # the fixture never regains its high after the deepest fall: recovery is unknown, not zero
    assert res.value["recovery_time"] is None
    # hand value: the only fall is 0.98/1 - 1 and it is regained one period later
    hand = call("risk.drawdown_stats", returns=tiny_dated)
    assert hand.value["max_drawdown"] == pytest.approx(0.02, abs=TOL)
    assert hand.value["duration"] == 1.0 and hand.value["recovery_time"] == 1.0
    assert "FT-049" in piece_ids("risk.drawdown_stats")


def test_ulcer_index_FT_050(ret, tiny):
    res = call("risk.ulcer_index", returns=ret)
    assert res.value == pytest.approx(FT.get_ui(_daily(ret), rolling=None, method="return"), abs=TOL)
    # hand value: drawdowns 0, -2%, 0, -1%, 0 give sqrt((0.02^2 + 0.01^2) / 5) = 1%
    assert call("risk.ulcer_index", returns=tiny).value == pytest.approx(0.01, abs=TOL)
    assert "FT-050" in piece_ids("risk.ulcer_index")


def test_cdar_and_rolling_FT_053(ret, tiny_dated):
    res = call("risk.cdar", returns=ret, level=0.95)
    assert res.value == pytest.approx(-FT.get_conditional_drawdown_at_risk(_daily(ret), 0.05, method="return"), abs=TOL)
    rolling = call("risk.rolling_cdar", returns=ret, window=250, level=0.95)
    origin = -FT.get_rolling_conditional_drawdown_at_risk(_daily(ret), 0.05, 250).dropna()
    assert rolling.value["latest"] == pytest.approx(origin.iloc[-1], abs=TOL)
    assert rolling.value["values"] == pytest.approx(list(origin.to_numpy()), abs=TOL)
    # hand value: one window holding all five returns, 20% tail = the single worst drawdown of 2%
    hand = call("risk.rolling_cdar", returns=tiny_dated, window=5, level=0.8)
    assert hand.value["latest"] == pytest.approx(0.02, abs=TOL)
    assert call("risk.rolling_cdar", returns=ret, window=10_000).value is None
    with pytest.raises(BadInput):
        call("risk.rolling_cdar", returns=ret, window="250")
    assert "FT-053" in piece_ids("risk.rolling_cdar") and "FT-053" in piece_ids("risk.cdar")


def test_worst_realization_SK_023(ret, tiny):
    assert call("risk.worst_realization", returns=ret).value == pytest.approx(M.worst_realization(ret.to_numpy()), abs=TOL)
    assert call("risk.worst_realization", returns=tiny).value == pytest.approx(0.02, abs=TOL)
    assert "SK-023" in piece_ids("risk.worst_realization")


def test_drawdown_series_SK_027(ret, tiny):
    res = call("risk.drawdown_series", returns=ret)
    assert res.value["cumulative_returns"] == pytest.approx(list(M.get_cumulative_returns(ret.to_numpy(), compounded=True)), abs=TOL)
    assert res.value["drawdowns"] == pytest.approx(list(_dd(ret)), abs=TOL)
    hand = call("risk.drawdown_series", returns=tiny)
    assert hand.value["cumulative_returns"] == pytest.approx([1.01, 1.01 * 0.98, 1.01 * 0.98 * 1.03, 1.01 * 0.98 * 1.03 * 0.99,
                                                              1.01 * 0.98 * 1.03 * 0.99 * 1.02], abs=TOL)
    assert hand.value["drawdowns"] == pytest.approx([0.0, -0.02, 0.0, -0.01, 0.0], abs=TOL)
    assert "SK-027" in piece_ids("risk.drawdown_series")


def test_max_average_and_dar_SK_028(ret, tiny):
    assert call("risk.max_drawdown", returns=ret).value == pytest.approx(M.max_drawdown(_dd(ret)), abs=TOL)
    assert call("risk.average_drawdown", returns=ret).value == pytest.approx(M.average_drawdown(_dd(ret)), abs=TOL)
    assert call("risk.drawdown_at_risk", returns=ret, level=0.95).value == pytest.approx(M.drawdown_at_risk(_dd(ret), beta=0.95), abs=TOL)
    assert call("risk.max_drawdown", returns=tiny).value == pytest.approx(0.02, abs=TOL)
    assert call("risk.average_drawdown", returns=tiny).value == pytest.approx(0.006, abs=TOL)
    assert call("risk.drawdown_at_risk", returns=tiny, level=0.8).value == pytest.approx(0.01, abs=TOL)
    for name in ("risk.max_drawdown", "risk.average_drawdown", "risk.drawdown_at_risk"):
        assert "SK-028" in piece_ids(name)


def test_cdar_and_edar_SK_029(ret, tiny):
    assert call("risk.cdar", returns=ret).value == pytest.approx(M.cdar(_dd(ret), beta=0.95), abs=TOL)
    assert call("risk.edar", returns=ret).value == pytest.approx(M.edar(_dd(ret), beta=0.95), abs=TOL)
    assert call("risk.cdar", returns=tiny, level=0.8).value == pytest.approx(0.02, abs=TOL)
    # the entropic bound is never below the conditional one, which is never below the quantile
    dar = call("risk.drawdown_at_risk", returns=ret).value
    cdar = call("risk.cdar", returns=ret).value
    edar = call("risk.edar", returns=ret).value
    assert dar <= cdar <= edar
    assert "SK-029" in piece_ids("risk.edar") and "SK-029" in piece_ids("risk.cdar")


def test_ulcer_index_SK_030(ret, tiny):
    assert call("risk.ulcer_index", returns=ret).value == pytest.approx(M.ulcer_index(_dd(ret)), abs=TOL)
    assert call("risk.ulcer_index", returns=tiny).value == pytest.approx(float(np.sqrt((0.02**2 + 0.01**2) / 5)), abs=TOL)
    assert "SK-030" in piece_ids("risk.ulcer_index")


@pytest.mark.parametrize("measure", ["max", "average", "dar", "cdar", "edar", "ulcer"])
def test_drawdown_risk_dispatch_SK_028(ret, measure):
    from hokma.quant import risk

    via_function = risk.drawdown_risk(ret, measure=measure, level=0.9)
    via_registry = call("risk.drawdown_risk", returns=ret, measure=measure, level=0.9)
    assert via_function.value == via_registry.value and via_function.value is not None
    assert via_function.method == measure


def test_drawdown_risk_rejects_unknown_measure_and_level(ret):
    assert call("risk.drawdown_risk", returns=ret, measure="calmar").value is None
    assert call("risk.cdar", returns=ret, level=0.0).value is None
    with pytest.raises(BadInput):
        call("risk.cdar", returns=ret, level="high")

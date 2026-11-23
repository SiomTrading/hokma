"""Volatility of the stats module. Rows FT-061, FT-062, FT-063, FT-064, FM-09 and FM-10."""
import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.stats import _lib, volatility

from .conftest import call, piece_ids

TOL = 1e-8
WEEK = 252 / 52


def _bars(frame):
    return {"open_": frame["open"], "high": frame["high"], "low": frame["low"], "close": frame["close"]}


def test_range_estimators_FT_063_hand_values(toy_bars):
    w = toy_bars.iloc[:5]
    o, h, lo, c = w["open"], w["high"], w["low"], w["close"]
    parkinson = math.sqrt(np.sum(np.log(h / lo) ** 2) / (4 * 5 * math.log(2)) * WEEK)
    rs = math.sqrt(np.mean(np.log(h / c) * np.log(h / o) + np.log(lo / c) * np.log(lo / o)) * WEEK)
    gk = math.sqrt(np.mean(0.5 * np.log(h / lo) ** 2 - (2 * math.log(2) - 1) * np.log(c / o) ** 2) * WEEK)
    got = {est: call("stats.range_estimators", **_bars(toy_bars), estimator=est, period="weekly").value["values"] for est in volatility.ESTIMATORS}
    assert got["parkinson"][0] == pytest.approx(parkinson, abs=TOL)
    assert got["rogers_satchell"][0] == pytest.approx(rs, abs=TOL)
    assert got["garman_klass"][0] == pytest.approx(gk, abs=TOL)
    # Yang-Zhang of the second week: overnight gaps use the last close of the first week
    w2 = toy_bars.iloc[5:10]
    overnight = np.log(w2["open"] / toy_bars["close"].shift(1).iloc[5:10])
    open_close = np.log(w2["close"] / w2["open"])
    rs_term = np.mean(np.log(w2["high"] / w2["close"]) * np.log(w2["high"] / w2["open"]) + np.log(w2["low"] / w2["close"]) * np.log(w2["low"] / w2["open"]))
    n = 5
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    yz = math.sqrt((overnight.var(ddof=1) + k * open_close.var(ddof=1) + (1 - k) * rs_term) * WEEK)
    assert got["yang_zhang"][1] == pytest.approx(yz, abs=TOL)
    assert "FT-063" in piece_ids("stats.range_estimators")


def test_range_estimators_FT_063_library_parity(ohlcv):
    indexed = ohlcv.copy()
    indexed.index = indexed.index.to_period("D")
    o, h, lo, c = (indexed[k] for k in ("open", "high", "low", "close"))
    lib = {"parkinson": _lib.ft_realized.get_parkinson_volatility(h, lo, "monthly"),
           "garman_klass": _lib.ft_realized.get_garman_klass_volatility(o, h, lo, c, "monthly"),
           "rogers_satchell": _lib.ft_realized.get_rogers_satchell_volatility(o, h, lo, c, "monthly"),
           "yang_zhang": _lib.ft_realized.get_yang_zhang_volatility(o, h, lo, c, "monthly")}
    for est, expected in lib.items():
        res = call("stats.range_estimators", **_bars(ohlcv), estimator=est, period="monthly")
        assert res.value["values"] == pytest.approx(list(expected.dropna().to_numpy()), abs=TOL)
        assert res.value["latest"] == pytest.approx(float(expected.dropna().iloc[-1]), abs=TOL)
        assert res.as_of is None or res.as_of >= pd.Timestamp("2024-01-01")


def test_range_estimators_FT_063_empty_nan_and_out_of_domain(toy_bars, ohlcv):
    empty = pd.Series([], dtype=float, index=pd.DatetimeIndex([]))
    assert call("stats.range_estimators", open_=empty, high=empty, low=empty, close=empty).value is None
    nan_bars = toy_bars.copy()
    nan_bars.iloc[3] = np.nan
    res = call("stats.range_estimators", **_bars(nan_bars), estimator="parkinson", period="weekly")
    assert res.value is None or any("dropped" in w for w in res.warnings)
    short = call("stats.range_estimators", **_bars(toy_bars.iloc[:5]))
    assert short.value is None and short.warnings
    bad = toy_bars.copy()
    bad.iloc[2, bad.columns.get_loc("high")] = bad.iloc[2]["low"] - 1.0
    assert call("stats.range_estimators", **_bars(bad)).value is None
    negative = toy_bars.copy()
    negative.iloc[2, negative.columns.get_loc("low")] = -1.0
    assert call("stats.range_estimators", **_bars(negative)).value is None
    assert call("stats.range_estimators", **_bars(toy_bars), estimator="median").value is None
    assert call("stats.range_estimators", **_bars(toy_bars), period="daily").value is None
    plain = {k: v.reset_index(drop=True) for k, v in _bars(toy_bars).items()}
    with pytest.raises(BadInput):
        call("stats.range_estimators", **plain)
    with pytest.raises(BadInput):
        call("stats.range_estimators", open_=toy_bars["open"], high=toy_bars["high"].iloc[:5], low=toy_bars["low"], close=toy_bars["close"])


def test_yang_zhang_FM_09_one_implementation_shared_with_the_alias(toy_bars):
    assert registry.describe("stats.yang_zhang").fn is volatility.yang_zhang
    assert "FM-09" in piece_ids("stats.yang_zhang") and "FT-063" in piece_ids("stats.yang_zhang")
    for alias, target in volatility.ALIASES.items():
        assert callable(getattr(volatility, target))
        if alias in {s.name for s in registry.list("indicators")}:
            assert registry.describe(alias).fn is getattr(volatility, target), alias        # identity, not a copy
    direct = call("stats.yang_zhang", **_bars(toy_bars), period="weekly")
    via = call("stats.range_estimators", **_bars(toy_bars), estimator="yang_zhang", period="weekly")
    assert direct.value == via.value
    assert call("stats.yang_zhang", **_bars(toy_bars.iloc[:3])).value is None


def test_rogers_satchell_FM_10_term_is_the_one_used_by_the_range_estimators(toy_bars):
    term = volatility.rogers_satchell_term(toy_bars["open"], toy_bars["high"], toy_bars["low"], toy_bars["close"])
    o, h, lo, c = (toy_bars[k].iloc[0] for k in ("open", "high", "low", "close"))
    assert float(term.iloc[0]) == pytest.approx(math.log(h / c) * math.log(h / o) + math.log(lo / c) * math.log(lo / o), abs=1e-12)
    res = call("stats.rogers_satchell", **_bars(toy_bars), period="weekly")
    assert res.value["term"] == pytest.approx(list(term.to_numpy()), abs=1e-12)
    assert res.value["values"][0] == pytest.approx(math.sqrt(float(term.iloc[:5].mean()) * WEEK), abs=TOL)
    shared = call("stats.range_estimators", **_bars(toy_bars), estimator="rogers_satchell", period="weekly")
    assert shared.value["values"] == res.value["values"]
    assert registry.describe("stats.rogers_satchell").fn is volatility.rogers_satchell
    assert "FM-10" in piece_ids("stats.rogers_satchell")
    assert call("stats.rogers_satchell", **_bars(toy_bars), period="decade").value is None


def test_har_rv_FT_064(daily_returns):
    rv = daily_returns ** 2
    res = call("stats.har_rv", realized_variance=rv)
    origin = _lib.ft_realized.get_har_rv_forecast(rv, 5, 22, 1).dropna()
    assert res.value["forecast_variance"] == pytest.approx(float(origin.iloc[-1]), abs=TOL)
    assert res.value["fitted"] == pytest.approx(list(origin.to_numpy()), abs=TOL)
    # hand value: ordinary least squares of RV(t+1) on RV(t), its 5 day and its 22 day mean
    daily, weekly, monthly = rv, rv.rolling(5).mean(), rv.rolling(22).mean()
    frame = pd.concat([rv.shift(-1), daily, weekly, monthly], axis=1).dropna()
    x = np.column_stack([np.ones(len(frame)), frame.iloc[:, 1:].to_numpy()])
    beta = np.linalg.lstsq(x, frame.iloc[:, 0].to_numpy(), rcond=None)[0]
    got = res.value["coefficients"]
    assert [got["intercept"], got["daily"], got["weekly"], got["monthly"]] == pytest.approx(list(beta), abs=1e-10)
    last = np.array([1.0, rv.iloc[-1], rv.iloc[-5:].mean(), rv.iloc[-22:].mean()])
    assert res.value["forecast_variance"] == pytest.approx(float(last @ beta), abs=1e-10)
    assert res.value["forecast_volatility"] == pytest.approx(math.sqrt(res.value["forecast_variance"]), abs=1e-12)
    assert 0.0 <= res.value["r2"] <= 1.0
    assert call("stats.har_rv", realized_variance=rv, horizon=5).value["forecast_variance"] != res.value["forecast_variance"]
    assert call("stats.har_rv", realized_variance=pd.Series([], dtype=float)).value is None
    assert call("stats.har_rv", realized_variance=rv.iloc[:20]).value is None
    assert call("stats.har_rv", realized_variance=-rv).value is None
    assert call("stats.har_rv", realized_variance=rv, weekly_window=30, monthly_window=22).value is None
    nan = rv.copy()
    nan.iloc[10] = np.nan
    assert any("dropped" in w for w in call("stats.har_rv", realized_variance=nan).warnings)
    with pytest.raises(BadInput):
        call("stats.har_rv", realized_variance=rv, horizon=2.5)
    assert "FT-064" in piece_ids("stats.har_rv")


@pytest.fixture(scope="module")
def gjr(daily_returns):
    return volatility.gjr_garch(daily_returns, horizon=60)


def test_gjr_garch_FT_061(daily_returns, gjr):
    values = daily_returns.to_numpy()
    weights = _lib.ft_garch.get_gjr_garch_weights(values)
    v = gjr.value
    assert [v["omega"], v["alpha"], v["gamma"], v["beta"]] == pytest.approx(list(weights), abs=TOL)
    forecast = _lib.ft_garch.get_gjr_garch_forecast(values, weights, time_steps=60)
    assert v["forecast_variance"] == pytest.approx(list(forecast), abs=TOL)
    # hand values: persistence, long run variance, half life, and the one step forecast of the recursion
    omega, alpha, gamma, beta = weights
    persistence = alpha + gamma / 2 + beta
    assert v["persistence"] == pytest.approx(persistence, abs=TOL)
    assert v["long_run_variance"] == pytest.approx(omega / (1 - persistence), abs=1e-12)
    assert v["half_life"] == pytest.approx(math.log(2) / -math.log(persistence), abs=1e-9)
    cond = np.asarray(_lib.ft_garch.get_gjr_garch(values, weights, len(values)))
    shock = (alpha + gamma * (values[-1] < 0)) * values[-1] ** 2
    assert v["forecast_variance"][0] == pytest.approx(omega + shock + beta * cond[-1], rel=1e-9)
    assert v["forecast_variance"][-1] == pytest.approx(v["long_run_variance"], rel=0.05)
    assert v["forecast_volatility"] == pytest.approx(list(np.sqrt(forecast)), abs=TOL)
    assert v["leverage_effect"] == bool(gamma > 0)
    assert 0.0 <= v["standardized_residuals"]["ljung_box_squared_p"] <= 1.0
    assert any("seasonality" in w for w in gjr.warnings)
    assert "FT-061" in piece_ids("stats.gjr_garch")
    res = call("stats.gjr_garch", returns=daily_returns.iloc[:50])
    assert res.value is None and res.warnings
    assert call("stats.gjr_garch", returns=pd.Series([], dtype=float)).value is None
    assert call("stats.gjr_garch", returns=pd.Series(np.zeros(200))).value is None
    assert call("stats.gjr_garch", returns=daily_returns, horizon=0).value is None
    with pytest.raises(BadInput):
        call("stats.gjr_garch", returns=daily_returns, horizon=2.5)


@pytest.fixture(scope="module")
def egarch(daily_returns):
    return volatility.egarch(daily_returns, horizon=5)


def test_egarch_FT_062_with_forecast(daily_returns, egarch):
    values = daily_returns.to_numpy()
    weights = _lib.ft_garch.get_egarch_weights(values)
    v = egarch.value
    assert [v["omega"], v["alpha"], v["gamma"], v["beta"]] == pytest.approx(list(weights), abs=TOL)
    forecast = _lib.ft_garch.get_egarch_forecast(values, weights, time_steps=5)
    assert v["forecast_variance"] == pytest.approx(list(forecast), abs=TOL)
    # hand values: the forecast follows the log variance recursion and decays at the rate beta toward omega / (1 - beta)
    omega, alpha, gamma, beta = weights
    cond = np.asarray(_lib.ft_garch.get_egarch(values, weights, len(values)))
    z = values[-1] / math.sqrt(cond[-1])
    next_log = omega + beta * math.log(cond[-1]) + alpha * (abs(z) - math.sqrt(2 / math.pi)) + gamma * z
    assert v["forecast_variance"][0] == pytest.approx(math.exp(next_log), rel=1e-9)
    long_log = omega / (1 - beta)
    assert v["forecast_variance"][2] == pytest.approx(math.exp(long_log + beta ** 2 * (next_log - long_log)), rel=1e-9)
    assert v["persistence"] == pytest.approx(abs(beta), abs=TOL)
    assert v["long_run_variance"] == pytest.approx(math.exp(long_log), rel=1e-9)
    assert len(v["forecast_variance"]) == 5
    assert "FT-062" in piece_ids("stats.egarch")
    assert call("stats.egarch", returns=daily_returns.iloc[:50]).value is None
    assert call("stats.egarch", returns=pd.Series([], dtype=float)).value is None
    nan = daily_returns.copy()
    nan.iloc[5] = np.nan
    assert any("dropped" in w for w in call("stats.egarch", returns=nan, horizon=2).warnings)
    assert call("stats.egarch", returns=daily_returns, horizon=0).value is None


def test_stats_volatility_runs_in_the_worker_process(toy_bars, monkeypatch):
    monkeypatch.setattr(registry, "IN_PROCESS", False)
    res = registry.call("stats.yang_zhang", **_bars(toy_bars), period="weekly")
    assert res.value["values"][1] > 0

"""Time series tools. Rows FT-082, FT-083, FT-087 and ECT-015."""
import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.stats import _econ as econ

from .conftest import call, piece_ids, rng


@pytest.fixture(scope="module")
def levels(daily_returns):
    return np.log(400.0) + daily_returns.cumsum()


def _df_t_stat(y):
    """Dickey-Fuller t statistic with a constant and no lagged differences, by plain least squares."""
    dy, x = np.diff(y), np.column_stack([y[:-1], np.ones(len(y) - 1)])
    beta = np.linalg.lstsq(x, dy, rcond=None)[0]
    resid = dy - x @ beta
    s2 = resid @ resid / (len(dy) - 2)
    return beta[0] / math.sqrt(s2 * np.linalg.inv(x.T @ x)[0, 0])


def test_adf_FT_082_hand_value_and_critical_values(levels, daily_returns):
    res = call("stats.unit_root", series=levels, test="adf", lags=0)
    assert res.value["statistic"] == pytest.approx(_df_t_stat(levels.to_numpy()), abs=1e-10)
    assert res.value["reject_null_5pct"] is False
    assert call("stats.unit_root", series=daily_returns, test="adf").value["reject_null_5pct"] is True
    # published asymptotic values with a constant: -3.43 at 1%, -2.86 at 5%, -2.57 at 10%
    crit = econ.df_critical_values("c", 10 ** 9)
    assert crit["1%"] == pytest.approx(-3.43035, abs=1e-4) and crit["5%"] == pytest.approx(-2.86154, abs=1e-4) and crit["10%"] == pytest.approx(-2.56677, abs=1e-4)
    assert econ.df_critical_values("c", 100)["5%"] == pytest.approx(-2.8909, abs=2e-4)
    assert call("stats.unit_root", series=levels, regression="ct").value["critical_values"]["5%"] < crit["5%"]
    assert call("stats.unit_root", series=levels, test="adf", lags=None).value["lags"] >= 0
    assert "FT-082" in piece_ids("stats.unit_root")


def test_pp_FT_082_equals_the_adf_statistic_without_serial_correlation_correction(levels):
    pp0 = call("stats.unit_root", series=levels, test="pp", lags=0)
    adf0 = call("stats.unit_root", series=levels, test="adf", lags=0)
    assert pp0.value["statistic"] == pytest.approx(adf0.value["statistic"], abs=1e-7)          # long run variance equals the variance (levels near 6 make the design ill conditioned)
    pp = call("stats.unit_root", series=levels, test="pp")
    assert pp.value["reject_null_5pct"] is False
    walk = pd.Series(rng(3).standard_normal(500).cumsum())
    assert call("stats.unit_root", series=walk, test="pp").value["reject_null_5pct"] is False
    assert call("stats.unit_root", series=pd.Series(rng(4).standard_normal(500)), test="pp").value["reject_null_5pct"] is True


def test_kpss_FT_082_hand_value(levels, daily_returns):
    y = levels.to_numpy()
    resid = y - y.mean()
    expected = float(np.sum(np.cumsum(resid) ** 2) / (len(y) ** 2 * (resid @ resid / len(y))))     # zero lags: long run variance is the variance
    res = call("stats.unit_root", series=levels, test="kpss", lags=0)
    assert res.value["statistic"] == pytest.approx(expected, abs=1e-10)
    assert res.value["critical_values"]["5%"] == 0.463 and call("stats.unit_root", series=levels, test="kpss", regression="ct").value["critical_values"]["5%"] == 0.146
    assert call("stats.unit_root", series=levels, test="kpss").value["reject_null_5pct"] is True       # a trending level is not stationary
    assert call("stats.unit_root", series=daily_returns, test="kpss").value["reject_null_5pct"] is False
    assert res.value["null_hypothesis"] == "stationary"


def test_zivot_andrews_FT_082_finds_a_break_that_hides_stationarity():
    g = rng(11)
    noise = np.zeros(600)
    for t in range(1, 600):
        noise[t] = 0.5 * noise[t - 1] + g.standard_normal()
    y = pd.Series(0.02 * np.arange(600) + 12.0 * (np.arange(600) > 300) + noise)
    za = call("stats.unit_root", series=y, test="za", regression="ct")
    assert za.value["reject_null_5pct"] is True
    assert abs(za.value["break_index"] - 300) <= 15
    assert za.value["critical_values"] == {"1%": -5.57, "5%": -5.08, "10%": -4.82}
    walk = pd.Series(g.standard_normal(400).cumsum())
    assert call("stats.unit_root", series=walk, test="za", regression="c").value["reject_null_5pct"] is False
    assert call("stats.unit_root", series=pd.Series(g.standard_normal(6000)), test="za").value is None


def test_unit_root_FT_082_empty_nan_and_out_of_domain(levels):
    assert call("stats.unit_root", series=pd.Series([], dtype=float)).value is None
    assert call("stats.unit_root", series=pd.Series(np.ones(100))).value is None
    assert call("stats.unit_root", series=levels.iloc[:10]).value is None
    assert call("stats.unit_root", series=levels, test="chow").value is None
    assert call("stats.unit_root", series=levels, regression="ctt").value is None
    assert call("stats.unit_root", series=levels, test="kpss", regression="n").value is None
    assert call("stats.unit_root", series=levels, lags=-1).value is None
    nan = levels.copy()
    nan.iloc[7] = np.nan
    assert any("dropped" in w for w in call("stats.unit_root", series=nan).warnings)
    with pytest.raises(BadInput):
        call("stats.unit_root", series=levels, lags=1.5)
    with pytest.raises(BadInput):
        call("stats.unit_root", series=levels, lags=5000)


def test_stationarity_check_ECT_015_reads_adf_and_kpss_together(levels, daily_returns):
    assert call("stats.stationarity_check", series=levels).value["conclusion"] == "unit_root"
    assert call("stats.stationarity_check", series=daily_returns).value["conclusion"] == "stationary"
    both = call("stats.stationarity_check", series=daily_returns).value
    assert both["adf"]["null_hypothesis"] == "unit root" and both["kpss"]["null_hypothesis"] == "stationary"
    assert call("stats.stationarity_check", series=pd.Series([], dtype=float)).value is None
    assert call("stats.stationarity_check", series=levels, regression="n").value is None
    assert "ECT-015" in piece_ids("stats.stationarity_check")


def test_autocorrelation_ECT_015_hand_value():
    x = pd.Series([1.0, -1.0] * 10)
    res = call("stats.autocorrelation", series=x, lags=2)
    assert res.value["acf"][0] == 1.0
    assert res.value["acf"][1] == pytest.approx(-19 / 20, abs=1e-12)            # sum of x_t x_(t+1) is -19, sum of squares 20
    assert res.value["acf"][2] == pytest.approx(18 / 20, abs=1e-12)
    assert res.value["pacf"][1] == pytest.approx(res.value["acf"][1], abs=1e-12)
    n, r1, r2 = 20, -19 / 20, 18 / 20
    q = n * (n + 2) * (r1 ** 2 / (n - 1) + r2 ** 2 / (n - 2))
    assert res.value["ljung_box"]["q"] == pytest.approx(q, abs=1e-10)
    assert res.value["band_95"] == pytest.approx(1.96 / math.sqrt(20), abs=1e-12)
    assert res.value["significant_acf_lags"] == [1, 2]
    # a pure AR(1) has a partial autocorrelation that stops after lag 1
    g = rng(5)
    ar = np.zeros(2000)
    for t in range(1, 2000):
        ar[t] = 0.7 * ar[t - 1] + g.standard_normal()
    pac = call("stats.autocorrelation", series=pd.Series(ar), lags=5).value
    assert pac["pacf"][1] == pytest.approx(0.7, abs=0.05) and abs(pac["pacf"][3]) < 0.08
    assert call("stats.autocorrelation", series=pd.Series([], dtype=float)).value is None
    assert call("stats.autocorrelation", series=pd.Series(np.ones(50))).value is None
    assert call("stats.autocorrelation", series=x, lags=15).value is None
    assert call("stats.autocorrelation", series=x, lags=0).value is None
    with pytest.raises(BadInput):
        call("stats.autocorrelation", series=x, lags=2000)
    assert "ECT-015" in piece_ids("stats.autocorrelation")


def test_models_arima_FT_083_hand_values_and_warnings(daily_returns):
    # AR(1): exact least squares, written independently
    y = daily_returns.to_numpy()
    x = np.column_stack([np.ones(len(y) - 1), y[:-1]])
    beta = np.linalg.lstsq(x, y[1:], rcond=None)[0]
    res = call("stats.models", data=daily_returns, kind="arima", p=1, d=0, q=0, horizon=3)
    assert res.value["ar"][0] == pytest.approx(beta[1], abs=1e-10)
    assert res.value["constant"] == pytest.approx(beta[0] / (1 - beta[1]), abs=1e-10)
    resid = y[1:] - x @ beta
    assert res.value["sigma2"] == pytest.approx(float(resid @ resid) / len(resid), abs=1e-12)
    assert res.value["forecast"][0] == pytest.approx(res.value["constant"] + res.value["ar"][0] * (y[-1] - res.value["constant"]), abs=1e-10)
    assert any("no tradable signal" in w for w in res.warnings)          # ARIMA on raw returns
    # integrated model with drift: the forecast continues the last level at the average step
    prices = pd.Series(100.0 + np.cumsum(0.3 + rng(6).standard_normal(300)))
    walk = call("stats.models", data=prices, kind="arima", p=0, d=1, q=0, horizon=4).value
    drift = float(np.diff(prices.to_numpy()).mean())
    assert walk["forecast"] == pytest.approx([prices.iloc[-1] + drift * h for h in range(1, 5)], abs=1e-9)
    # ARMA(1,1) by conditional least squares recovers its parameters
    g = rng(8)
    e = g.standard_normal(3000)
    arma = np.zeros(3000)
    for t in range(1, 3000):
        arma[t] = 0.6 * arma[t - 1] + e[t] + 0.3 * e[t - 1]
    fit = call("stats.models", data=pd.Series(arma), kind="arima", p=1, d=0, q=1).value
    assert fit["ar"][0] == pytest.approx(0.6, abs=0.08) and fit["ma"][0] == pytest.approx(0.3, abs=0.1)
    assert fit["stationary"] and fit["invertible"] and fit["ljung_box"]["p_value"] > 0.01 and fit["explained_variance"] > 0.2
    assert fit["bic"] - fit["aic"] == pytest.approx(3 * (math.log(fit["n_obs"]) - 2), abs=1e-9)       # k = p + q + 1 parameters
    assert call("stats.models", data=pd.Series([], dtype=float)).value is None
    assert call("stats.models", data=pd.Series(np.ones(100))).value is None
    assert call("stats.models", data=prices, p=11).value is None
    assert call("stats.models", data=prices, kind="garch").value is None
    assert call("stats.models", data=prices, horizon=0).value is None
    with pytest.raises(BadInput):
        call("stats.models", data=prices, p=1.5)
    assert {"FT-083", "ECT-015"} <= set(piece_ids("stats.models"))


@pytest.fixture(scope="module")
def var_data():
    g = rng(21)
    a = np.array([[0.5, 0.1], [0.2, 0.3]])
    chol = np.array([[1.0, 0.0], [0.5, 0.8]])
    data = np.zeros((600, 2))
    for t in range(1, 600):
        data[t] = a @ data[t - 1] + chol @ g.standard_normal(2)
    return pd.DataFrame(data, columns=["a", "b"])


def test_models_var_irf_fevd_FT_083_hand_values(var_data):
    arr = var_data.to_numpy()
    x = np.column_stack([np.ones(len(arr) - 1), arr[:-1]])
    beta = np.linalg.lstsq(x, arr[1:], rcond=None)[0]
    var = call("stats.models", data=var_data, kind="var", lags=1, horizon=2).value
    assert np.array(var["coefficients"][0]) == pytest.approx(beta[1:].T, abs=1e-10)
    resid = arr[1:] - x @ beta
    sigma = resid.T @ resid / (len(resid) - 3)
    assert np.array(var["sigma_u"]) == pytest.approx(sigma, abs=1e-10)
    assert var["stable"] is True and var["max_root_modulus"] < 1.0
    assert var["forecast"][0] == pytest.approx(list(beta[0] + arr[-1] @ beta[1:]), abs=1e-10)
    irf = np.array(call("stats.models", data=var_data, kind="irf", lags=1, horizon=3).value["irf"])
    chol = np.linalg.cholesky(sigma)
    assert irf[0] == pytest.approx(chol, abs=1e-10)                              # impact is the Cholesky factor
    assert irf[1] == pytest.approx(beta[1:].T @ chol, abs=1e-10)                 # one step is A1 times the impact
    assert irf[2] == pytest.approx(beta[1:].T @ beta[1:].T @ chol, abs=1e-10)
    fevd = np.array(call("stats.models", data=var_data, kind="fevd", lags=1, horizon=4).value["fevd"])
    assert fevd[0][0] == pytest.approx([1.0, 0.0], abs=1e-12)                    # first variable at step 1 answers only to its own shock
    assert fevd.sum(axis=2) == pytest.approx(np.ones((4, 2)), abs=1e-12)
    out = call("stats.models", data=var_data, kind="irf", lags=1, horizon=2)
    assert any("Cholesky" in w for w in out.warnings)
    assert call("stats.models", data=var_data.iloc[:10], kind="var").value is None
    with pytest.raises(BadInput):
        call("stats.models", data=var_data["a"], kind="var")                    # one series is not a system
    assert call("stats.models", data=var_data, kind="var", lags=0).value is None
    exploding = pd.DataFrame(np.cumsum(rng(1).standard_normal((300, 2)), axis=0) * 1.0, columns=["a", "b"])
    assert call("stats.models", data=exploding, kind="var", lags=1).value["max_root_modulus"] > 0.9


def test_models_vecm_FT_083_error_correction():
    g = rng(31)
    x = np.cumsum(g.standard_normal(800))
    y = 2.0 + 0.8 * x + g.standard_normal(800) * 0.5
    res = call("stats.models", data=pd.DataFrame({"y": y, "x": x}), kind="vecm", rank=1, lags=2, horizon=3).value
    beta = np.array(res["beta"])[:, 0]
    assert beta[0] == pytest.approx(1.0) and beta[1] == pytest.approx(-0.8, abs=0.05)
    assert np.array(res["alpha"])[0, 0] < -0.3                                  # y corrects toward the long run relation
    assert len(res["forecast"]) == 3 and len(res["gamma"]) == 1
    # the error correction term is stationary
    term = pd.Series(y * beta[0] + x * beta[1])
    assert call("stats.unit_root", series=term).value["reject_null_5pct"] is True
    assert call("stats.models", data=pd.DataFrame({"y": y, "x": x}), kind="vecm", rank=2).value is None
    assert call("stats.models", data=pd.DataFrame({"y": y[:20], "x": x[:20]}), kind="vecm").value is None


def test_granger_FT_087_hand_value_and_direction():
    g = rng(41)
    x = g.standard_normal(600)
    y = np.zeros(600)
    for t in range(1, 600):
        y[t] = 0.5 * y[t - 1] + 0.6 * x[t - 1] + g.standard_normal() * 0.5
    res = call("stats.granger", x=pd.Series(x), y=pd.Series(y), max_lag=2).value
    rows = np.arange(2, 600)
    dep = y[rows]
    own = np.column_stack([np.ones(len(rows)), y[rows - 1], y[rows - 2]])
    full = np.column_stack([own, x[rows - 1], x[rows - 2]])
    rss = lambda m: float(np.sum((dep - m @ np.linalg.lstsq(m, dep, rcond=None)[0]) ** 2))
    f = ((rss(own) - rss(full)) / 2) / (rss(full) / (len(rows) - 5))
    assert res["by_lag"][1]["f_stat"] == pytest.approx(f, abs=1e-8)
    assert res["by_lag"][1]["df_den"] == len(rows) - 5
    assert res["min_p_value"] < 1e-10 and res["direction"] == "x causes y"
    reverse = call("stats.granger", x=pd.Series(y), y=pd.Series(x), max_lag=2).value
    assert reverse["min_p_value"] > 0.01
    assert res["min_p_value_bonferroni"] == pytest.approx(min(1.0, res["min_p_value"] * 2), abs=1e-300)
    walks = call("stats.granger", x=pd.Series(g.standard_normal(300).cumsum()), y=pd.Series(g.standard_normal(300).cumsum()), max_lag=1)
    assert any("unit root" in w for w in walks.warnings)
    assert call("stats.granger", x=pd.Series([], dtype=float), y=pd.Series([], dtype=float)).value is None
    assert call("stats.granger", x=pd.Series(np.ones(100)), y=pd.Series(x[:100])).value is None
    assert call("stats.granger", x=pd.Series(x[:10]), y=pd.Series(y[:10]), max_lag=4).value is None
    assert call("stats.granger", x=pd.Series(x), y=pd.Series(y), max_lag=0).value is None
    with pytest.raises(BadInput):
        call("stats.granger", x=pd.Series(x[:50]), y=pd.Series(y[:40]))
    with pytest.raises(BadInput):
        call("stats.granger", x=pd.Series(x), y=pd.Series(y), max_lag=5000)
    assert "FT-087" in piece_ids("stats.granger")


def test_half_life_ECT_015_hand_values():
    for phi, expected in ((0.9, 6.5788), (0.98, 34.3095)):
        series = pd.Series(100.0 * phi ** np.arange(80))
        res = call("stats.half_life", series=series)
        assert res.value["phi"] == pytest.approx(phi, abs=1e-9)
        assert res.value["half_life"] == pytest.approx(math.log(2) / -math.log(phi), abs=1e-9)
        assert res.value["half_life"] == pytest.approx(expected, abs=1e-3)
    walk = pd.Series(rng(9).standard_normal(500).cumsum())
    res = call("stats.half_life", series=walk)
    assert res.value is None or res.value["phi"] < 1.0
    assert call("stats.half_life", series=pd.Series(1.01 ** np.arange(100))).value is None
    assert call("stats.half_life", series=pd.Series([], dtype=float)).value is None
    assert call("stats.half_life", series=pd.Series(np.ones(50))).value is None


def test_structural_breaks_ECT_015():
    g = rng(51)
    shifted = pd.Series(np.concatenate([g.standard_normal(150), g.standard_normal(150) + 4.0]))
    stable = pd.Series(g.standard_normal(300))
    cus = call("stats.structural_breaks", series=shifted, method="cusum").value
    assert cus["reject_stability_5pct"] is True and abs(cus["peak_index"] - 150) <= 10
    assert call("stats.structural_breaks", series=stable, method="cusum").value["reject_stability_5pct"] is False
    assert cus["critical_values"] == {"10%": 1.224, "5%": 1.358, "1%": 1.628}
    step = pd.Series([0.0] * 100 + [1.0] * 100) + np.sin(np.arange(200)) * 0.01
    seg = call("stats.structural_breaks", series=step, method="mean_shift", max_breaks=3).value
    assert seg["chosen"]["n_breaks"] == 1 and seg["chosen"]["break_indices"] == [100]
    assert call("stats.structural_breaks", series=stable, method="mean_shift").value["chosen"]["n_breaks"] == 0
    chow = call("stats.structural_breaks", series=shifted, method="chow", break_index=150).value
    arr = shifted.to_numpy()
    rows = np.arange(1, 300)
    x = np.column_stack([np.ones(299), arr[:-1]])
    dep = arr[rows]
    rss = lambda a, b: float(np.sum((b - a @ np.linalg.lstsq(a, b, rcond=None)[0]) ** 2))
    split = 149
    rss_split = rss(x[:split], dep[:split]) + rss(x[split:], dep[split:])
    expected = ((rss(x, dep) - rss_split) / 2) / (rss_split / (299 - 4))
    assert chow["f_stat"] == pytest.approx(expected, abs=1e-8) and chow["p_value"] < 1e-6
    scan = call("stats.structural_breaks", series=shifted, method="chow").value
    assert abs(scan["break_index"] - 150) <= 20 and "note" in scan
    assert call("stats.structural_breaks", series=pd.Series([], dtype=float)).value is None
    assert call("stats.structural_breaks", series=pd.Series(np.ones(100))).value is None
    assert call("stats.structural_breaks", series=stable, method="bai").value is None
    assert call("stats.structural_breaks", series=stable, method="chow", break_index=2).value is None
    assert call("stats.structural_breaks", series=stable, method="mean_shift", max_breaks=0).value is None
    with pytest.raises(BadInput):
        call("stats.structural_breaks", series=stable, max_breaks=50)


def test_intraday_seasonality_ECT_015_hand_values():
    days = pd.bdate_range("2024-03-04", periods=20)
    slots = [pd.Timedelta(hours=9, minutes=30 + 5 * i) for i in range(12)]
    scale = np.array([4.0, 2.0, 1.5, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.5, 2.0, 3.0]) * 0.001
    index = pd.DatetimeIndex([d + s for d in days for s in slots])
    sign = np.tile([1.0, -1.0], len(index) // 2)
    returns = pd.Series(np.tile(scale, len(days)) * sign, index=index)
    res = call("stats.intraday_seasonality", returns=returns).value
    overall = float(np.abs(returns).mean())
    assert res["factors"]["09:30"] == pytest.approx(0.004 / overall, abs=1e-12)
    assert res["factors"]["10:05"] == pytest.approx(0.001 / overall, abs=1e-12)
    assert res["u_shape"] is True
    adjusted = pd.Series(res["adjusted_returns"], index=index)
    assert np.abs(adjusted).std() == pytest.approx(0.0, abs=1e-12)          # every slot now has the same size
    assert call("stats.intraday_seasonality", returns=returns.iloc[:10]).value is None
    assert call("stats.intraday_seasonality", returns=pd.Series(np.zeros(100), index=pd.date_range("2024-01-01", periods=100, freq="5min"))).value is None
    with pytest.raises(BadInput):
        call("stats.intraday_seasonality", returns=pd.Series(np.ones(100)))

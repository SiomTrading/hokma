"""Statistical tests: FT-084, FT-085, FT-086, FT-088 (own implementations, checked against hand formulas and the reference distributions)."""
import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.validation import _lib, stat_tests as st

from .conftest import assert_registered

RNG = np.random.default_rng(20261122)
N = 1000


def _ar1(rho: float, n: int = N, seed: int = 1) -> np.ndarray:
    noise = np.random.default_rng(seed).normal(0, 1, n)
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = rho * x[t - 1] + noise[t]
    return x


def _diag(values, **kw):
    return registry.call("validation.residual_diagnostics", residuals=pd.Series(values), **kw).value


def test_jarque_bera_equals_reference_FT_084():
    values = np.random.default_rng(2).standard_t(3, N)
    out = _diag(values, tests=["jarque_bera"])["jarque_bera"]
    ref = _lib.sp_stats.jarque_bera(values)
    assert out["statistic"] == pytest.approx(float(ref.statistic), abs=1e-9) and out["p_value"] == pytest.approx(float(ref.pvalue), abs=1e-9)
    assert out["p_value"] < 1e-6
    assert _diag(np.random.default_rng(3).normal(0, 1, N), tests=["jarque_bera"])["jarque_bera"]["p_value"] > 0.05


def test_ljung_box_hand_formula_and_power_FT_084():
    x = _ar1(0.5)
    out = _diag(x, tests=["ljung_box"], lags=10)["ljung_box"]
    c = x - x.mean()
    rho = [float(c[:-k] @ c[k:] / (c @ c)) for k in range(1, 11)]
    q = N * (N + 2) * sum(r * r / (N - k) for k, r in zip(range(1, 11), rho))
    assert out["statistic"] == pytest.approx(q, abs=1e-9)
    assert out["p_value"] == pytest.approx(float(_lib.sp_stats.chi2.sf(q, 10)), abs=1e-12) and out["p_value"] < 1e-10
    assert _diag(np.random.default_rng(4).normal(0, 1, N), tests=["ljung_box"], lags=10)["ljung_box"]["p_value"] > 0.05


def test_arch_lm_detects_volatility_clustering_FT_084():
    rng = np.random.default_rng(5)
    h, e = np.ones(N), np.zeros(N)
    for t in range(1, N):
        h[t] = 0.2 + 0.7 * e[t - 1] ** 2
        e[t] = math.sqrt(h[t]) * rng.normal()
    out = _diag(e, tests=["arch_lm"], lags=5)["arch_lm"]
    sq = e ** 2
    y = sq[5:]
    x = np.column_stack([sq[5 - j: N - j] for j in range(1, 6)])
    r2 = st.ols(y, x)["r2"]
    assert out["statistic"] == pytest.approx(len(y) * r2, abs=1e-9)
    assert out["p_value"] < 0.01
    assert _diag(np.random.default_rng(6).normal(0, 1, N), tests=["arch_lm"], lags=5)["arch_lm"]["p_value"] > 0.05


def test_variance_ratio_random_walk_and_trend_FT_084():
    iid = np.random.default_rng(8).normal(0, 1, 2000)
    out = _diag(iid, tests=["variance_ratio"], vr_q=5)["variance_ratio"]
    assert out["variance_ratio"] == pytest.approx(1.0, abs=0.15) and out["p_value"] > 0.05
    trending = _diag(_ar1(0.3, 2000, seed=9), tests=["variance_ratio"], vr_q=5)["variance_ratio"]
    assert trending["variance_ratio"] > 1.5 and trending["p_value"] < 0.01


def test_cusum_detects_a_level_shift_FT_084():
    stable = np.random.default_rng(30).normal(0, 1, N)
    shifted = stable.copy()
    shifted[N // 2:] += 1.0
    assert _diag(stable, tests=["cusum"])["cusum"]["p_value"] > 0.05
    assert _diag(shifted, tests=["cusum"])["cusum"]["p_value"] < 0.01


def test_residual_diagnostics_registered_and_defaults_FT_084():
    out = _diag(np.random.default_rng(11).normal(0, 1, 300))
    assert set(out) == {"arch_lm", "jarque_bera", "ljung_box", "variance_ratio", "cusum"}
    assert_registered("validation.residual_diagnostics", "FT-084")
    with pytest.raises(BadInput):
        _diag(np.random.default_rng(11).normal(0, 1, 300), tests=["nope"])


@pytest.mark.parametrize("bad", [pd.Series([], dtype=float), pd.Series([0.01] * 100), pd.Series([float("nan")] * 100)])
def test_residual_diagnostics_degenerate_FT_084(bad):
    result = registry.call("validation.residual_diagnostics", residuals=bad)
    assert result.value is None and result.warnings


# ------------------------------------------------------------------ specification (FT-085)

def _spec(y, x, **kw):
    return registry.call("validation.specification_tests", y=pd.Series(y), x=pd.DataFrame(x), **kw)


def test_durbin_watson_hand_formula_FT_085():
    x = np.random.default_rng(12).normal(0, 1, (N, 1))
    y = 1.0 + 2.0 * x[:, 0] + _ar1(0.8, N, seed=13)
    out = _spec(y, x, tests=["durbin_watson"]).value["durbin_watson"]["statistic"]
    resid = st.ols(y, x)["resid"]
    assert out == pytest.approx(float(np.sum(np.diff(resid) ** 2) / np.sum(resid ** 2)), abs=1e-12)
    assert out < 1.0
    y_iid = 1.0 + 2.0 * x[:, 0] + np.random.default_rng(14).normal(0, 1, N)
    assert _spec(y_iid, x, tests=["durbin_watson"]).value["durbin_watson"]["statistic"] == pytest.approx(2.0, abs=0.15)


def test_breusch_pagan_and_white_detect_heteroskedasticity_FT_085():
    rng = np.random.default_rng(15)
    x = rng.normal(0, 1, (N, 2))
    noise = rng.normal(0, 1, N)
    hetero = 1 + x[:, 0] + x[:, 1] + np.exp(0.8 * x[:, 0]) * noise
    homo = 1 + x[:, 0] + x[:, 1] + noise
    for test in ("breusch_pagan", "white"):
        assert _spec(hetero, x, tests=[test]).value[test]["p_value"] < 0.01
        assert _spec(homo, x, tests=[test]).value[test]["p_value"] > 0.05
    out = _spec(hetero, x, tests=["breusch_pagan"]).value["breusch_pagan"]
    fit = st.ols(hetero, x)
    aux = st.ols(fit["resid"] ** 2, x)
    assert out["statistic"] == pytest.approx(N * aux["r2"], abs=1e-9)
    assert out["p_value"] == pytest.approx(float(_lib.sp_stats.chi2.sf(N * aux["r2"], 2)), abs=1e-12)


def test_vif_matches_the_closed_form_FT_085():
    rng = np.random.default_rng(16)
    x1 = rng.normal(0, 1, N)
    x2 = 0.9 * x1 + math.sqrt(1 - 0.81) * rng.normal(0, 1, N)
    x3 = rng.normal(0, 1, N)
    frame = pd.DataFrame({"x1": x1, "x2": x2, "x3": x3})
    out = _spec(1 + x1 + x3 + rng.normal(0, 1, N), frame, tests=["vif"]).value["vif"]["statistic"]
    r = float(np.corrcoef(x1, x2)[0, 1])
    # x1 and x2 are explained by each other and slightly by x3; the pairwise closed form bounds it
    assert out["x1"] == pytest.approx(1 / (1 - r * r), rel=0.05)
    assert out["x3"] == pytest.approx(1.0, abs=0.1)


def test_ramsey_reset_detects_a_missing_square_FT_085():
    rng = np.random.default_rng(17)
    x = rng.normal(0, 1, (N, 1))
    good = 1 + x[:, 0] + rng.normal(0, 1, N)
    bad = 1 + x[:, 0] + 1.5 * x[:, 0] ** 2 + rng.normal(0, 1, N)
    assert _spec(good, x, tests=["reset"]).value["reset"]["p_value"] > 0.05
    assert _spec(bad, x, tests=["reset"]).value["reset"]["p_value"] < 0.01


def test_chow_detects_a_break_FT_085():
    rng = np.random.default_rng(18)
    x = rng.normal(0, 1, (N, 1))
    stable = 1 + 2 * x[:, 0] + rng.normal(0, 1, N)
    broken = stable.copy()
    broken[N // 2:] = 1 + 4 * x[N // 2:, 0] + rng.normal(0, 1, N // 2)
    assert _spec(stable, x, tests=["chow"], break_index=N // 2).value["chow"]["p_value"] > 0.05
    out = _spec(broken, x, tests=["chow"], break_index=N // 2).value["chow"]
    assert out["p_value"] < 1e-6
    pooled, a, b = st.ols(broken, x), st.ols(broken[: N // 2], x[: N // 2]), st.ols(broken[N // 2:], x[N // 2:])
    assert out["statistic"] == pytest.approx(((pooled["ssr"] - a["ssr"] - b["ssr"]) / 2) / ((a["ssr"] + b["ssr"]) / (N - 4)), abs=1e-9)
    assert_registered("validation.specification_tests", "FT-085")
    with pytest.raises(BadInput):
        _spec(broken, x, tests=["chow"], break_index=1)


def test_specification_degenerate_and_bad_inputs_FT_085():
    x = np.random.default_rng(19).normal(0, 1, (50, 1))
    assert _spec(np.full(50, 0.01), x).value is None
    assert _spec(np.full(50, np.nan), x).value is None
    assert _spec(np.array([]), np.zeros((0, 1))).value is None
    with pytest.raises(BadInput):
        _spec(np.arange(5.0), np.zeros((6, 1)))


# ------------------------------------------------------------------ hypothesis tests (FT-086)

def _hyp(test, **kw):
    return registry.call("validation.hypothesis_tests", test=test, **kw)


def test_two_sample_t_equals_reference_FT_086():
    a, b = np.random.default_rng(20).normal(0.0, 1, 400), np.random.default_rng(21).normal(0.3, 1, 400)
    out = _hyp("two_sample_t", sample_a=pd.Series(a), sample_b=pd.Series(b)).value
    ref = _lib.sp_stats.ttest_ind(a, b, equal_var=False)
    assert out["statistic"] == pytest.approx(float(ref.statistic), abs=1e-9) and out["p_value"] == pytest.approx(float(ref.pvalue), abs=1e-9)
    assert out["p_value"] < 0.01


def test_f_wald_and_lr_agree_on_nested_models_FT_086():
    rng = np.random.default_rng(22)
    x = rng.normal(0, 1, (N, 3))
    y = 1 + 2 * x[:, 0] + 0.5 * x[:, 1] + rng.normal(0, 1, N)
    f_out = _hyp("f", y=pd.Series(y), x_restricted=pd.DataFrame(x[:, :1]), x=pd.DataFrame(x)).value
    r, u = st.ols(y, x[:, :1]), st.ols(y, x)
    assert f_out["statistic"] == pytest.approx(((r["ssr"] - u["ssr"]) / 2) / (u["ssr"] / (N - 4)), abs=1e-9)
    assert f_out["p_value"] < 1e-6
    lr_out = _hyp("likelihood_ratio", y=pd.Series(y), x_restricted=pd.DataFrame(x[:, :1]), x=pd.DataFrame(x)).value
    assert lr_out["statistic"] == pytest.approx(N * (math.log(r["ssr"]) - math.log(u["ssr"])), abs=1e-9)
    irrelevant = _hyp("f", y=pd.Series(y), x_restricted=pd.DataFrame(x[:, :2]), x=pd.DataFrame(x)).value
    assert irrelevant["p_value"] > 0.05
    # Wald on beta_3 = 0 equals the squared t statistic
    wald = _hyp("wald", y=pd.Series(y), x=pd.DataFrame(x), restriction=[[0, 0, 0, 1]], rhs=[0.0]).value
    sigma2 = u["ssr"] / (N - 4)
    t_sq = (u["beta"][3] ** 2) / (sigma2 * u["xtx_inv"][3, 3])
    assert wald["statistic"] == pytest.approx(float(t_sq), abs=1e-9)
    assert wald["p_value"] == pytest.approx(float(_lib.sp_stats.chi2.sf(t_sq, 1)), abs=1e-12)


def test_hausman_wu_flags_endogeneity_FT_086():
    rng = np.random.default_rng(23)
    n = 3000
    z, u = rng.normal(0, 1, n), rng.normal(0, 1, n)
    x_exog = z + rng.normal(0, 1, n)
    y_exog = x_exog + rng.normal(0, 1, n)
    x_endog = z + u + rng.normal(0, 1, n)
    y_endog = x_endog + 2 * u + rng.normal(0, 1, n)
    instruments = pd.DataFrame({"z": z})
    assert _hyp("hausman_wu", y=pd.Series(y_exog), x_suspect=pd.Series(x_exog), instruments=instruments).value["p_value"] > 0.05
    assert _hyp("hausman_wu", y=pd.Series(y_endog), x_suspect=pd.Series(x_endog), instruments=instruments).value["p_value"] < 0.01
    assert_registered("validation.hypothesis_tests", "FT-086")


def test_hypothesis_degenerate_and_bad_inputs_FT_086():
    assert _hyp("two_sample_t", sample_a=pd.Series([], dtype=float), sample_b=pd.Series([1.0, 2.0, 3.0])).value is None
    assert _hyp("two_sample_t", sample_a=pd.Series([0.1] * 10), sample_b=pd.Series([0.1] * 10)).value is None
    assert _hyp("two_sample_t", sample_a=pd.Series([float("nan")] * 10), sample_b=pd.Series([1.0] * 10)).value is None
    with pytest.raises(BadInput):
        _hyp("nope")
    with pytest.raises(BadInput):
        _hyp("f", y=pd.Series([1.0, 2.0, 3.0]))


# ------------------------------------------------------------------ forecast tests (FT-088)

def test_diebold_mariano_hand_formula_FT_088():
    rng = np.random.default_rng(24)
    actual = rng.normal(0, 1, 500)
    good, bad = actual + rng.normal(0, 0.5, 500), actual + rng.normal(0, 1.0, 500)
    ea, eb = actual - bad, actual - good
    out = registry.call("validation.forecast_tests", test="diebold_mariano", errors_a=pd.Series(ea), errors_b=pd.Series(eb), h=1).value
    d = ea ** 2 - eb ** 2
    n = len(d)
    dm = d.mean() / math.sqrt(((d - d.mean()) @ (d - d.mean()) / n) / n)
    adjusted = dm * math.sqrt((n + 1 - 2 + 0) / n)
    assert out["statistic"] == pytest.approx(adjusted, abs=1e-9)
    assert out["statistic"] > 0 and out["p_value"] < 0.01
    same = registry.call("validation.forecast_tests", test="diebold_mariano", errors_a=pd.Series(ea), errors_b=pd.Series(ea.copy()))
    assert same.value is None and same.warnings


def test_forecast_accuracy_rmse_mae_out_of_sample_FT_088():
    actual = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    forecast = pd.Series([1.5, 2.5, 2.5, 4.5, 4.0, 7.0])
    out = registry.call("validation.forecast_tests", test="accuracy", actual=actual, forecast=forecast, oos_start=4).value
    err = np.array([-0.5, -0.5, 0.5, -0.5, 1.0, -1.0])
    assert out["rmse"] == pytest.approx(math.sqrt(np.mean(err ** 2)), abs=1e-12)
    assert out["mae"] == pytest.approx(np.mean(np.abs(err)), abs=1e-12)
    assert out["rmse_out_of_sample"] == pytest.approx(1.0, abs=1e-12) and out["mae_out_of_sample"] == pytest.approx(1.0, abs=1e-12)
    assert out["rmse_in_sample"] == pytest.approx(0.5, abs=1e-12)
    assert_registered("validation.forecast_tests", "FT-088")


def test_forecast_tests_degenerate_and_bad_inputs_FT_088():
    assert registry.call("validation.forecast_tests", test="accuracy", actual=pd.Series([], dtype=float), forecast=pd.Series([], dtype=float)).value is None
    nan = pd.Series([float("nan")] * 20)
    assert registry.call("validation.forecast_tests", test="diebold_mariano", errors_a=nan, errors_b=nan).value is None
    with pytest.raises(BadInput):
        registry.call("validation.forecast_tests", test="accuracy", actual=pd.Series([1.0, 2.0]), forecast=pd.Series([1.0]))
    with pytest.raises(BadInput):
        registry.call("validation.forecast_tests", test="nope")

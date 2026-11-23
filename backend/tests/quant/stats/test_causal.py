"""Causal inference tools, registered as a methodological reference. Row FT-093."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput

from .conftest import call, piece_ids, rng


def test_iv_2sls_FT_093_hand_value_and_weak_instruments():
    g = rng(101)
    n = 2000
    z = g.standard_normal(n)
    u = g.standard_normal(n)
    x = 0.8 * z + u + 0.5 * g.standard_normal(n)
    y = 1.0 + 2.0 * x + u + 0.3 * g.standard_normal(n)
    res = call("stats.iv_2sls", y=pd.Series(y), endog=pd.Series(x), instruments=pd.Series(z)).value
    # hand value: with one regressor and one instrument the estimate is cov(z, y) / cov(z, x)
    expected = np.cov(z, y)[0, 1] / np.cov(z, x)[0, 1]
    assert res["coefficients"]["endog"] == pytest.approx(expected, abs=1e-10)
    assert res["coefficients"]["endog"] == pytest.approx(2.0, abs=0.1)
    ols = np.cov(x, y)[0, 1] / np.var(x, ddof=1)
    assert ols > 2.2                                                       # least squares is biased upward by the shared shock
    assert res["first_stage"]["endog"]["f_stat"] > 100 and "sargan" not in res
    weak = call("stats.iv_2sls", y=pd.Series(y), endog=pd.Series(x), instruments=pd.Series(0.01 * z + g.standard_normal(n)))
    assert any("weak instruments" in w for w in weak.warnings)
    over = call("stats.iv_2sls", y=pd.Series(y), endog=pd.Series(x), instruments=pd.DataFrame({"z1": z, "z2": z * 0.5 + g.standard_normal(n)})).value
    assert over["sargan"]["df"] == 1 and 0.0 <= over["sargan"]["p_value"] <= 1.0
    with_control = call("stats.iv_2sls", y=pd.Series(y), endog=pd.Series(x), instruments=pd.Series(z), controls=pd.Series(g.standard_normal(n), name="c")).value
    assert "c" in with_control["coefficients"]
    assert "FT-093" in piece_ids("stats.iv_2sls")
    assert call("stats.iv_2sls", y=pd.Series([], dtype=float), endog=pd.Series([], dtype=float), instruments=pd.Series([], dtype=float)).value is None
    assert call("stats.iv_2sls", y=pd.Series(y), endog=pd.DataFrame({"a": x, "b": x ** 2}), instruments=pd.Series(z)).value is None      # underidentified
    nan = pd.Series(y)
    nan.iloc[3] = np.nan
    assert any("dropped" in w for w in call("stats.iv_2sls", y=nan, endog=pd.Series(x), instruments=pd.Series(z)).warnings)
    with pytest.raises(BadInput):
        call("stats.iv_2sls", y=pd.Series(y[:50]), endog=pd.Series(x), instruments=pd.Series(z))


def test_diff_in_diff_FT_093_hand_value():
    # four cells with known means: treated pre 10, treated post 16, control pre 8, control post 11 -> effect (16 - 10) - (11 - 8) = 3
    y = pd.Series([9.0, 11.0, 15.0, 17.0, 7.0, 9.0, 10.0, 12.0])
    treated = pd.Series([1, 1, 1, 1, 0, 0, 0, 0])
    post = pd.Series([0, 0, 1, 1, 0, 0, 1, 1])
    res = call("stats.diff_in_diff", y=y, treated=treated, post=post).value
    assert res["effect"] == pytest.approx(3.0, abs=1e-12)
    assert res["cell_means"] == {"control_pre": 8.0, "control_post": 11.0, "treated_pre": 10.0, "treated_post": 16.0}
    g = rng(102)
    t, p = g.integers(0, 2, 600), g.integers(0, 2, 600)
    big = 1 + 0.5 * t + 0.3 * p + 2.0 * t * p + 0.5 * g.standard_normal(600)
    out = call("stats.diff_in_diff", y=pd.Series(big), treated=pd.Series(t), post=pd.Series(p)).value
    assert out["effect"] == pytest.approx(2.0, abs=0.2) and out["p_value"] < 1e-6
    clustered = call("stats.diff_in_diff", y=pd.Series(big), treated=pd.Series(t), post=pd.Series(p), cluster=pd.Series(g.integers(0, 30, 600))).value
    assert clustered["clustered"] is True and clustered["effect"] == out["effect"] and clustered["std_error"] != out["std_error"]
    assert any("parallel trends" in w for w in call("stats.diff_in_diff", y=y, treated=treated, post=post).warnings)
    assert call("stats.diff_in_diff", y=y.iloc[:4], treated=treated.iloc[:4], post=post.iloc[:4]).value is None          # a cell is empty
    assert call("stats.diff_in_diff", y=y, treated=treated * 2, post=post).value is None
    assert call("stats.diff_in_diff", y=pd.Series([], dtype=float), treated=pd.Series([], dtype=float), post=pd.Series([], dtype=float)).value is None
    with pytest.raises(BadInput):
        call("stats.diff_in_diff", y=y, treated=treated.iloc[:5], post=post)


def test_regression_discontinuity_FT_093_exact_jump():
    x = np.linspace(-1, 1, 401)
    y = 1.0 + 0.5 * x + 2.0 * (x >= 0)                                      # a line with a jump of 2 at zero, no noise
    res = call("stats.regression_discontinuity", y=pd.Series(y), running=pd.Series(x), cutoff=0.0, bandwidth=0.5).value
    assert res["effect"] == pytest.approx(2.0, abs=1e-9)
    assert res["left_intercept"] == pytest.approx(1.0, abs=1e-9) and res["right_intercept"] == pytest.approx(3.0, abs=1e-9)
    assert res["n_left"] > 5 and res["n_right"] > 5
    g = rng(103)
    xr = g.uniform(-1, 1, 1000)
    noisy = call("stats.regression_discontinuity", y=pd.Series(1 + 0.5 * xr + 2 * (xr >= 0) + 0.2 * g.standard_normal(1000)), running=pd.Series(xr), cutoff=0.0).value
    assert noisy["effect"] == pytest.approx(2.0, abs=0.25) and noisy["bandwidth"] > 0
    assert call("stats.regression_discontinuity", y=pd.Series(y), running=pd.Series(x), cutoff=5.0).value is None          # nobody around the cutoff
    assert call("stats.regression_discontinuity", y=pd.Series(y), running=pd.Series(x), cutoff=0.0, bandwidth=-1.0).value is None
    assert call("stats.regression_discontinuity", y=pd.Series(y[:10]), running=pd.Series(x[:10]), cutoff=0.0).value is None
    assert call("stats.regression_discontinuity", y=pd.Series([], dtype=float), running=pd.Series([], dtype=float), cutoff=0.0).value is None
    with pytest.raises(BadInput):
        call("stats.regression_discontinuity", y=pd.Series(y), running=pd.Series(x[:100]), cutoff=0.0)
    with pytest.raises(BadInput):
        call("stats.regression_discontinuity", y=pd.Series(y), running=pd.Series(x), cutoff="0")


def test_propensity_score_FT_093_saturated_model_equals_the_stratified_estimate():
    # one binary covariate: the logit is saturated, so the weighted estimate is the average of the stratum differences
    rows = ([(0, 0, 1.0)] * 20 + [(0, 1, 3.0)] * 10 + [(1, 0, 2.0)] * 10 + [(1, 1, 7.0)] * 20)
    frame = pd.DataFrame(rows, columns=["x", "t", "y"])
    frame["y"] = frame["y"] + np.tile([0.0, 0.5], 30)
    res = call("stats.propensity_score", treatment=frame["t"], x=frame[["x"]], y=frame["y"], n_boot=0).value
    s0, s1 = frame[frame["x"] == 0], frame[frame["x"] == 1]
    diff0 = s0[s0["t"] == 1]["y"].mean() - s0[s0["t"] == 0]["y"].mean()
    diff1 = s1[s1["t"] == 1]["y"].mean() - s1[s1["t"] == 0]["y"].mean()
    assert res["ate_ipw"] == pytest.approx(0.5 * diff0 + 0.5 * diff1, abs=1e-8)
    assert res["n_treated"] == 30 and res["n_control"] == 30 and res["se_ate"] is None
    g = rng(104)
    cov = g.standard_normal((800, 2))
    t = (g.random(800) < 1 / (1 + np.exp(-cov[:, 0]))).astype(float)
    y = 1 + cov[:, 0] + 3.0 * t + g.standard_normal(800)
    naive = y[t == 1].mean() - y[t == 0].mean()
    full = call("stats.propensity_score", treatment=pd.Series(t), x=pd.DataFrame(cov), y=pd.Series(y), n_boot=40).value
    assert abs(full["ate_ipw"] - 3.0) < abs(naive - 3.0) and full["ate_ipw"] == pytest.approx(3.0, abs=0.4)
    assert full["se_ate"] > 0 and full["att_matching"] == pytest.approx(3.0, abs=0.5)
    again = call("stats.propensity_score", treatment=pd.Series(t), x=pd.DataFrame(cov), y=pd.Series(y), n_boot=40).value
    assert again["se_ate"] == full["se_ate"]                                            # seeded bootstrap
    assert call("stats.propensity_score", treatment=pd.Series(t[:15]), x=pd.DataFrame(cov[:15]), y=pd.Series(y[:15])).value is None
    assert call("stats.propensity_score", treatment=pd.Series(t * 2), x=pd.DataFrame(cov), y=pd.Series(y)).value is None
    assert call("stats.propensity_score", treatment=pd.Series([], dtype=float), x=pd.DataFrame({"a": []}), y=pd.Series([], dtype=float)).value is None
    with pytest.raises(BadInput):
        call("stats.propensity_score", treatment=pd.Series(t), x=pd.DataFrame(cov[:10]), y=pd.Series(y))
    with pytest.raises(BadInput):
        call("stats.propensity_score", treatment=pd.Series(t), x=pd.DataFrame(cov), y=pd.Series(y), n_boot=500000)


def test_synthetic_control_FT_093_recovers_exact_weights_and_the_effect():
    g = rng(105)
    donors = pd.DataFrame(g.standard_normal((50, 6)).cumsum(axis=0), columns=list("ABCDEF"))
    treated = 0.3 * donors["A"] + 0.7 * donors["B"]
    pre_t, post_t = treated.iloc[:35].to_numpy(), treated.iloc[35:].to_numpy() + 5.0         # a jump of 5 after the event
    res = call("stats.synthetic_control", treated_pre=pd.Series(pre_t), donors_pre=donors.iloc[:35], treated_post=pd.Series(post_t), donors_post=donors.iloc[35:]).value
    assert res["weights"]["A"] == pytest.approx(0.3, abs=1e-3) and res["weights"]["B"] == pytest.approx(0.7, abs=1e-3)
    assert sum(res["weights"].values()) == pytest.approx(1.0, abs=1e-9) and min(res["weights"].values()) >= 0.0
    assert res["average_effect"] == pytest.approx(5.0, abs=1e-2)
    assert res["rmspe_pre"] < 1e-2 and res["rmspe_ratio"] > 100
    assert res["placebo_p_value"] == pytest.approx(1 / 7, abs=1e-9)                          # the treated unit ranks first of 7 (6 placebos)
    assert call("stats.synthetic_control", treated_pre=pd.Series(pre_t[:3]), donors_pre=donors.iloc[:3], treated_post=pd.Series(post_t), donors_post=donors.iloc[35:]).value is None
    assert call("stats.synthetic_control", treated_pre=pd.Series(pre_t), donors_pre=donors.iloc[:35, :1], treated_post=pd.Series(post_t), donors_post=donors.iloc[35:, :1]).value is None
    nan = donors.copy()
    nan.iloc[2, 0] = np.nan
    assert call("stats.synthetic_control", treated_pre=pd.Series(pre_t), donors_pre=nan.iloc[:35], treated_post=pd.Series(post_t), donors_post=nan.iloc[35:]).value is None
    with pytest.raises(BadInput):
        call("stats.synthetic_control", treated_pre=pd.Series(pre_t), donors_pre=donors.iloc[:30], treated_post=pd.Series(post_t), donors_post=donors.iloc[35:])
    with pytest.raises(BadInput):
        call("stats.synthetic_control", treated_pre=pd.Series(pre_t), donors_pre=donors.iloc[:35], treated_post=pd.Series(post_t), donors_post=donors.iloc[35:, :3])

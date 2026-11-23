"""Regressions, Fama-MacBeth and panel models. Rows FT-090, FT-091 and FT-092."""
import math

import numpy as np
import pandas as pd
import pytest
from hokma.quant.errors import BadInput
from hokma.quant.stats import _lib

from .conftest import call, piece_ids, rng

LinearRegression, LogisticRegression, QuantileRegressor = _lib.SkLinearRegression, _lib.SkLogisticRegression, _lib.SkQuantileRegressor      # parity runs through the lib file
TOL = 1e-8


@pytest.fixture(scope="module")
def data():
    g = rng(91)
    n = 300
    x = pd.DataFrame({"a": g.standard_normal(n), "b": g.standard_normal(n)})
    y = 1.0 + 2.0 * x["a"] - 1.0 * x["b"] + g.standard_normal(n)
    return x, y


def test_ols_FT_091_hand_value_and_library_parity(data):
    x, y = data
    res = call("stats.regress", y=y, x=x).value
    design = np.column_stack([np.ones(len(y)), x.to_numpy()])
    beta = np.linalg.lstsq(design, y.to_numpy(), rcond=None)[0]
    assert list(res["coefficients"].values()) == pytest.approx(list(beta), abs=TOL)
    sk = LinearRegression().fit(x.to_numpy(), y.to_numpy())
    assert res["coefficients"]["const"] == pytest.approx(sk.intercept_, abs=TOL) and [res["coefficients"]["a"], res["coefficients"]["b"]] == pytest.approx(list(sk.coef_), abs=TOL)
    resid = y.to_numpy() - design @ beta
    s2 = resid @ resid / (len(y) - 3)
    assert list(res["std_errors"].values()) == pytest.approx(list(np.sqrt(np.diag(s2 * np.linalg.inv(design.T @ design)))), abs=TOL)
    assert res["r2"] == pytest.approx(1 - resid @ resid / np.sum((y - y.mean()) ** 2), abs=TOL) and res["n_obs"] == 300
    exact = call("stats.regress", y=pd.Series(1.0 + 2.0 * np.arange(20.0)), x=pd.Series(np.arange(20.0), name="t")).value
    assert exact["coefficients"]["const"] == pytest.approx(1.0, abs=1e-10) and exact["coefficients"]["t"] == pytest.approx(2.0, abs=1e-10)
    assert exact["r2"] == pytest.approx(1.0, abs=1e-12)
    no_const = call("stats.regress", y=y, x=x, add_constant=False).value
    assert "const" not in no_const["coefficients"]
    hc = call("stats.regress", y=y, x=x, cov_type="hc1").value
    meat = (design * resid[:, None]).T @ (design * resid[:, None])
    inv = np.linalg.inv(design.T @ design)
    assert list(hc["std_errors"].values()) == pytest.approx(list(np.sqrt(np.diag(inv @ meat @ inv * 300 / 297))), abs=TOL)
    hac0 = call("stats.regress", y=y, x=x, cov_type="hac", hac_lags=0).value
    assert list(hac0["std_errors"].values()) == pytest.approx(list(hc["std_errors"].values()), abs=TOL)       # no lags: same as HC1
    assert list(call("stats.regress", y=y, x=x, cov_type="hac", hac_lags=5).value["std_errors"].values()) != list(hc["std_errors"].values())
    assert "FT-091" in piece_ids("stats.regress")


def test_wls_and_gls_FT_091(data):
    x, y = data
    weights = pd.Series(np.where(np.arange(len(y)) % 2 == 0, 2.0, 1.0))
    wls = call("stats.regress", y=y, x=x, method="wls", weights=weights).value
    design = np.column_stack([np.ones(len(y)), x.to_numpy()])
    w = weights.to_numpy()
    beta = np.linalg.solve(design.T @ (design * w[:, None]), design.T @ (w * y.to_numpy()))
    assert list(wls["coefficients"].values()) == pytest.approx(list(beta), abs=TOL)
    # a weight of 2 is the same as the row twice
    doubled_idx = np.concatenate([np.arange(len(y)), np.arange(0, len(y), 2)])
    twice = call("stats.regress", y=y.iloc[doubled_idx].reset_index(drop=True), x=x.iloc[doubled_idx].reset_index(drop=True)).value
    assert list(wls["coefficients"].values()) == pytest.approx(list(twice["coefficients"].values()), abs=TOL)
    ols = call("stats.regress", y=y.iloc[:50], x=x.iloc[:50]).value
    identity = call("stats.regress", y=y.iloc[:50], x=x.iloc[:50], method="gls", sigma=np.eye(50)).value
    assert list(identity["coefficients"].values()) == pytest.approx(list(ols["coefficients"].values()), abs=TOL)
    variances = np.linspace(0.5, 3.0, 50)
    diag = call("stats.regress", y=y.iloc[:50], x=x.iloc[:50], method="gls", sigma=pd.Series(variances)).value
    again = call("stats.regress", y=y.iloc[:50], x=x.iloc[:50], method="wls", weights=pd.Series(1.0 / variances)).value
    assert list(diag["coefficients"].values()) == pytest.approx(list(again["coefficients"].values()), abs=TOL)
    cov = 0.5 * np.eye(50) + 0.5 * np.ones((50, 50))
    chol = np.linalg.cholesky(cov)
    d50 = np.column_stack([np.ones(50), x.iloc[:50].to_numpy()])
    expected = np.linalg.solve((d50.T @ np.linalg.solve(cov, d50)), d50.T @ np.linalg.solve(cov, y.iloc[:50].to_numpy()))
    full = call("stats.regress", y=y.iloc[:50], x=x.iloc[:50], method="gls", sigma=cov).value
    assert list(full["coefficients"].values()) == pytest.approx(list(expected), abs=1e-8) and chol.shape == (50, 50)
    fgls = call("stats.regress", y=y, x=x, method="gls")
    assert "rho" in fgls.value and any("not comparable" in w for w in fgls.warnings)
    assert call("stats.regress", y=y, x=x, method="wls").value is None
    assert call("stats.regress", y=y, x=x, method="wls", weights=-weights).value is None
    assert call("stats.regress", y=y.iloc[:50], x=x.iloc[:50], method="gls", sigma=-np.eye(50)).value is None


def test_logit_and_probit_FT_091(data):
    x, y = data
    g = rng(92)
    prob = 1.0 / (1.0 + np.exp(-(0.5 + 1.0 * x["a"] - 0.5 * x["b"])))
    yb = pd.Series((g.random(len(x)) < prob).astype(float))
    res = call("stats.regress", y=yb, x=x, method="logit").value
    sk = LogisticRegression(C=np.inf, tol=1e-14, max_iter=10000, solver="newton-cg").fit(x.to_numpy(), yb.to_numpy())
    assert [res["coefficients"]["const"], res["coefficients"]["a"], res["coefficients"]["b"]] == pytest.approx([sk.intercept_[0], *sk.coef_[0]], abs=1e-6)
    # hand values: with only a constant the logit is ln(p / (1 - p)) and the probit the inverse normal of p
    only = pd.Series([1.0] * 30 + [0.0] * 70)
    constant_only = pd.DataFrame(index=range(100))
    assert call("stats.regress", y=only, x=constant_only, method="logit").value["coefficients"]["const"] == pytest.approx(math.log(0.3 / 0.7), abs=1e-8)
    assert call("stats.regress", y=only, x=constant_only, method="probit").value["coefficients"]["const"] == pytest.approx(_lib.sp_stats.norm.ppf(0.3), abs=1e-8)
    # the same rate 0.3 in both groups of z: the slope is zero
    stratified = pd.Series([1.0] * 30 + [0.0] * 70)
    one = pd.DataFrame({"z": [0.0] * 15 + [1.0] * 15 + [0.0] * 35 + [1.0] * 35})
    out = call("stats.regress", y=stratified, x=one, method="logit").value
    assert out["coefficients"]["const"] == pytest.approx(math.log(0.3 / 0.7), abs=1e-8) and out["coefficients"]["z"] == pytest.approx(0.0, abs=1e-8)
    pro = call("stats.regress", y=stratified, x=one, method="probit").value
    assert pro["coefficients"]["const"] == pytest.approx(_lib.sp_stats.norm.ppf(0.3), abs=1e-8) and pro["coefficients"]["z"] == pytest.approx(0.0, abs=1e-8)
    assert pro["pseudo_r2"] == pytest.approx(0.0, abs=1e-8)
    probit = call("stats.regress", y=yb, x=x, method="probit").value
    assert probit["coefficients"]["a"] < res["coefficients"]["a"]                   # probit slopes are smaller than logit slopes
    assert 0.0 < res["pseudo_r2"] < 1.0 and res["log_likelihood"] < 0
    robust = call("stats.regress", y=yb, x=x, method="logit", cov_type="hc1").value
    assert robust["std_errors"]["a"] != res["std_errors"]["a"]
    assert call("stats.regress", y=y, x=x, method="logit").value is None             # y is not 0 or 1
    assert call("stats.regress", y=pd.Series(np.ones(100)), x=x.iloc[:100], method="logit").value is None
    separated = pd.Series((x["a"] > 0).astype(float))
    sep = call("stats.regress", y=separated, x=x[["a"]], method="logit")
    assert sep.value is None and any("separation" in w or "singular" in w for w in sep.warnings)
    assert call("stats.regress", y=yb, x=x, method="logit", cov_type="hac").value is None


def test_quantile_FT_091(data):
    x, y = data
    res = call("stats.regress", y=y, x=x, method="quantile", tau=0.3, n_boot=30).value
    sk = QuantileRegressor(quantile=0.3, alpha=0.0, fit_intercept=True, solver="highs").fit(x.to_numpy(), y.to_numpy())
    assert [res["coefficients"]["const"], res["coefficients"]["a"], res["coefficients"]["b"]] == pytest.approx([sk.intercept_, *sk.coef_], abs=1e-6)
    median = call("stats.regress", y=pd.Series([1.0, 2, 3, 4, 5, 6, 7]), x=pd.DataFrame({"z": np.zeros(7)}), add_constant=True, method="quantile", n_boot=20)
    assert median.value is None                                                        # a constant column is collinear with the constant: no unique solution is not claimed
    one_col = call("stats.regress", y=pd.Series([1.0, 2, 3, 4, 5, 6, 7]), x=pd.DataFrame({"z": np.ones(7)}), add_constant=False, method="quantile", n_boot=20).value
    assert one_col["coefficients"]["z"] == pytest.approx(4.0, abs=1e-9)                # hand value: the median of 1 to 7
    assert res["check_loss"] > 0 and res["n_boot"] == 30
    again = call("stats.regress", y=y, x=x, method="quantile", tau=0.3, n_boot=30).value
    assert again["std_errors"] == res["std_errors"]                                    # seeded bootstrap
    assert call("stats.regress", y=y, x=x, method="quantile", tau=0.0).value is None
    assert call("stats.regress", y=y, x=x, method="quantile", tau=1.0).value is None
    assert call("stats.regress", y=y, x=x, method="quantile", n_boot=5).value is None


def test_regress_FT_091_empty_nan_and_out_of_domain(data):
    x, y = data
    assert call("stats.regress", y=pd.Series([], dtype=float), x=pd.DataFrame({"a": []})).value is None
    assert call("stats.regress", y=y.iloc[:2], x=x.iloc[:2]).value is None
    assert call("stats.regress", y=y, x=x, method="lasso").value is None
    assert call("stats.regress", y=y, x=x, cov_type="cluster").value is None
    collinear = x.assign(c=x["a"] * 2)
    assert call("stats.regress", y=y, x=collinear).value is None
    nan = y.copy()
    nan.iloc[3] = np.nan
    res = call("stats.regress", y=nan, x=x)
    assert res.value["n_obs"] == 299 and any("dropped" in w for w in res.warnings)
    with pytest.raises(BadInput):
        call("stats.regress", y=y.iloc[:50], x=x)
    with pytest.raises(BadInput):
        call("stats.regress", y=y, x="abc")
    with pytest.raises(BadInput):
        call("stats.regress", y=y, x=x, n_boot=500000)
    with pytest.raises(BadInput):
        call("stats.regress", y=y, x=x, hac_lags=2000)


@pytest.fixture(scope="module")
def long_panel():
    g = rng(93)
    rows = []
    for d in range(60):
        for a in range(40):
            z = g.standard_normal()
            rows.append((d, a, 0.5 + (0.8 + 0.1 * d / 60) * z + g.standard_normal(), z))
    return pd.DataFrame(rows, columns=["date", "entity", "ret", "z"])


def test_fama_macbeth_FT_090_hand_values(long_panel):
    res = call("stats.fama_macbeth", data=long_panel, y="ret", x=["z"]).value
    slopes = []
    for _, grp in long_panel.groupby("date"):
        d = np.column_stack([np.ones(len(grp)), grp["z"].to_numpy()])
        slopes.append(np.linalg.lstsq(d, grp["ret"].to_numpy(), rcond=None)[0])
    slopes = np.array(slopes)
    assert res["coefficients"]["z"] == pytest.approx(slopes[:, 1].mean(), abs=1e-10)
    assert res["std_errors"]["z"] == pytest.approx(slopes[:, 1].std(ddof=1) / math.sqrt(60), abs=1e-10)
    assert res["t_stats"]["z"] == pytest.approx(slopes[:, 1].mean() / (slopes[:, 1].std(ddof=1) / math.sqrt(60)), abs=1e-8)
    assert res["n_dates"] == 60 and res["mean_assets_per_date"] == 40
    assert res["period_coefficients"]["z"] == pytest.approx(list(slopes[:, 1]), abs=1e-10)
    nw = call("stats.fama_macbeth", data=long_panel, y="ret", x=["z"], nw_lags=3).value
    assert nw["coefficients"]["z"] == res["coefficients"]["z"] and nw["std_errors"]["z"] != res["std_errors"]["z"]
    # exact cross sections: every date has slope equal to its index plus 1
    exact = pd.DataFrame([(d, a, 2.0 + (d + 1) * a, float(a)) for d in range(5) for a in range(6)], columns=["date", "entity", "ret", "z"])
    ex = call("stats.fama_macbeth", data=exact, y="ret", x=["z"]).value
    assert ex["coefficients"]["z"] == pytest.approx(3.0, abs=1e-10) and ex["coefficients"]["const"] == pytest.approx(2.0, abs=1e-10)
    assert ex["std_errors"]["z"] == pytest.approx(math.sqrt(2.5) / math.sqrt(5), abs=1e-10)   # slopes 1..5 have standard deviation sqrt(2.5)
    assert "FT-090" in piece_ids("stats.fama_macbeth")


def test_fama_macbeth_FT_090_empty_nan_and_out_of_domain(long_panel):
    assert call("stats.fama_macbeth", data=long_panel.iloc[:0], y="ret", x=["z"]).value is None
    assert call("stats.fama_macbeth", data=long_panel[long_panel["date"] < 2], y="ret", x=["z"]).value is None
    assert call("stats.fama_macbeth", data=long_panel, y="ret", x=["z"], nw_lags=-1).value is None
    nan = long_panel.copy()
    nan.loc[3, "ret"] = np.nan
    assert any("dropped" in w for w in call("stats.fama_macbeth", data=nan, y="ret", x=["z"]).warnings)
    with pytest.raises(BadInput):
        call("stats.fama_macbeth", data=long_panel, y="missing", x=["z"])
    with pytest.raises(BadInput):
        call("stats.fama_macbeth", data=long_panel, y="ret", x="z")
    with pytest.raises(BadInput):
        call("stats.fama_macbeth", data=long_panel.values, y="ret", x=["z"])
    with pytest.raises(BadInput):
        call("stats.fama_macbeth", data=long_panel, y="ret", x=["z"], nw_lags=5000)


@pytest.fixture(scope="module")
def panel_data():
    g = rng(94)
    ent = np.repeat(np.arange(40), 12)
    tm = np.tile(np.arange(12), 40)
    alpha = g.standard_normal(40)[ent]
    x = g.standard_normal(480) + 0.7 * alpha
    y = alpha + 1.5 * x + g.standard_normal(480)
    return pd.DataFrame({"entity": ent, "date": tm, "y": y, "x": x})


def test_panel_fixed_effects_FT_092_equals_dummy_variable_regression(panel_data):
    fe = call("stats.panel", data=panel_data, y="y", x=["x"], model="fixed", cluster=False).value
    dummies = pd.get_dummies(panel_data["entity"], dtype=float).to_numpy()
    design = np.column_stack([panel_data["x"].to_numpy(), dummies])
    beta = np.linalg.lstsq(design, panel_data["y"].to_numpy(), rcond=None)[0]
    assert fe["coefficients"]["x"] == pytest.approx(beta[0], abs=TOL)
    resid = panel_data["y"].to_numpy() - design @ beta
    s2 = resid @ resid / (480 - 40 - 1)
    assert fe["std_errors"]["x"] == pytest.approx(math.sqrt(s2 * np.linalg.inv(design.T @ design)[0, 0]), abs=TOL)
    assert fe["df_resid"] == 480 - 40 - 1 and fe["n_entities"] == 40
    clustered = call("stats.panel", data=panel_data, y="y", x=["x"], model="fixed").value
    assert clustered["coefficients"]["x"] == fe["coefficients"]["x"] and clustered["std_errors"]["x"] != fe["std_errors"]["x"]
    assert len(fe["entity_effects"]) == 40 and 0 < fe["r2_within"] < 1
    assert "FT-092" in piece_ids("stats.panel")


def test_panel_random_pooled_between_and_hausman_FT_092(panel_data):
    pooled = call("stats.panel", data=panel_data, y="y", x=["x"], model="pooled", cluster=False).value
    d = np.column_stack([np.ones(480), panel_data["x"].to_numpy()])
    assert pooled["coefficients"]["x"] == pytest.approx(np.linalg.lstsq(d, panel_data["y"].to_numpy(), rcond=None)[0][1], abs=TOL)
    means = panel_data.groupby("entity")[["y", "x"]].mean()
    between = call("stats.panel", data=panel_data, y="y", x=["x"], model="between").value
    assert between["coefficients"]["x"] == pytest.approx(np.polyfit(means["x"], means["y"], 1)[0], abs=TOL)
    random = call("stats.panel", data=panel_data, y="y", x=["x"], model="random", cluster=False).value
    fixed = call("stats.panel", data=panel_data, y="y", x=["x"], model="fixed", cluster=False).value
    assert fixed["coefficients"]["x"] < random["coefficients"]["x"] < pooled["coefficients"]["x"]      # x is correlated with the effect
    assert 0.0 < random["theta_mean"] < 1.0 and random["sigma_alpha2"] > 0
    haus = call("stats.panel", data=panel_data, y="y", x=["x"], model="hausman").value
    assert haus["prefer"] == "fixed" and haus["p_value"] < 0.05 and haus["df"] == 1
    diff = haus["fixed"]["coefficients"]["x"] - haus["random"]["coefficients"]["x"]
    var = haus["fixed"]["std_errors"]["x"] ** 2 - haus["random"]["std_errors"]["x"] ** 2
    assert haus["statistic"] == pytest.approx(diff ** 2 / var, abs=1e-8)
    rejections = 0
    for seed in range(100, 112):                       # effects unrelated to x: the test should rarely reject (5% level)
        g = rng(seed)
        ent = np.repeat(np.arange(40), 12)
        clean = pd.DataFrame({"entity": ent, "date": np.tile(np.arange(12), 40), "x": g.standard_normal(480)})
        clean["y"] = g.standard_normal(40)[ent] + 1.0 * clean["x"] + g.standard_normal(480)
        rejections += call("stats.panel", data=clean, y="y", x=["x"], model="hausman").value["prefer"] == "fixed"
    assert rejections <= 3
    rnd = call("stats.panel", data=clean, y="y", x=["x"], model="random", cluster=False).value
    fxd = call("stats.panel", data=clean, y="y", x=["x"], model="fixed", cluster=False).value
    assert rnd["coefficients"]["x"] == pytest.approx(fxd["coefficients"]["x"], abs=0.05)         # no correlation with the effect: both agree


def test_panel_FT_092_empty_nan_and_out_of_domain(panel_data):
    assert call("stats.panel", data=panel_data.iloc[:0], y="y", x=["x"]).value is None
    assert call("stats.panel", data=panel_data[panel_data["entity"] == 0], y="y", x=["x"]).value is None
    assert call("stats.panel", data=pd.concat([panel_data, panel_data.iloc[:3]]), y="y", x=["x"]).value is None
    fixed_x = panel_data.assign(x=panel_data["entity"].astype(float))
    assert call("stats.panel", data=fixed_x, y="y", x=["x"], model="fixed").value is None
    assert call("stats.panel", data=panel_data, y="y", x=["x"], model="gmm").value is None
    nan = panel_data.copy()
    nan.loc[2, "y"] = np.nan
    assert any("dropped" in w for w in call("stats.panel", data=nan, y="y", x=["x"]).warnings)
    with pytest.raises(BadInput):
        call("stats.panel", data=panel_data, y="y", x=["nope"])
    with pytest.raises(BadInput):
        call("stats.panel", data=panel_data, y="y", x="x")

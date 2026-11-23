"""Metrics part B: metrics against a benchmark (QS-033, QS-050, FT-069, FT-070, FT-073, FT-075, FT-076)."""
import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.metrics import _lib

from .metrics_b_qa import assert_bad_data_gives_none, assert_close, assert_registered, benchmark, call, returns

R = returns()
B = benchmark()
N = len(R)


def lib():
    return _lib.returns_stats()


def pm():
    return _lib.performance_model()


def test_alpha_beta_QS_033():
    assert_registered("metrics.alpha_beta", "QS-033")
    res = call("metrics.alpha_beta", returns=R, benchmark=B)
    beta = np.cov(R, B, ddof=1)[0, 1] / B.var(ddof=1)
    alpha = (R.mean() - beta * B.mean()) * 252
    assert_close(res.value["beta"], beta)
    assert_close(res.value["alpha"], alpha)
    ref = lib().greeks(R, B, periods=252, prepare_returns=False)
    assert_close(res.value["beta"], ref["beta"])
    assert_close(res.value["alpha"], ref["alpha"])
    exact = call("metrics.alpha_beta", returns=0.001 + 2 * B, benchmark=B)
    assert_close(exact.value["beta"], 2.0)
    assert_close(exact.value["alpha"], 0.001 * 252)
    assert_bad_data_gives_none(lambda s: call("metrics.alpha_beta", returns=s, benchmark=B))
    assert_bad_data_gives_none(lambda s: call("metrics.alpha_beta", returns=R, benchmark=s))


def test_alpha_beta_rolling_QS_033():
    assert_registered("metrics.alpha_beta_rolling", "QS-033")
    res = call("metrics.alpha_beta_rolling", returns=R, benchmark=B, window=126)
    last = slice(-126, None)
    beta = np.cov(R.iloc[last], B.iloc[last], ddof=1)[0, 1] / B.iloc[last].var(ddof=1)
    alpha = R.iloc[last].mean() - beta * B.iloc[last].mean()
    assert_close(res.value["beta"][-1], beta)
    assert_close(res.value["alpha"][-1], alpha)
    ref = lib().rolling_greeks(R, B, periods=126, prepare_returns=False).dropna()
    assert_close(res.value["beta"], ref["beta"].tolist())
    assert len(res.value["dates"]) == N - 125
    short = call("metrics.alpha_beta_rolling", returns=R.iloc[:50], benchmark=B.iloc[:50], window=126)
    assert short.value is None and short.warnings
    with pytest.raises(BadInput):
        call("metrics.alpha_beta_rolling", returns=R, benchmark=B, window=1)
    assert_bad_data_gives_none(lambda s: call("metrics.alpha_beta_rolling", returns=s, benchmark=B))


def test_versus_benchmark_QS_050():
    assert_registered("metrics.versus_benchmark", "QS-050")
    res = call("metrics.versus_benchmark", returns=R, benchmark=B, rf=0.01)
    assert_close(res.value["correlation"], np.corrcoef(R, B)[0, 1])
    assert_close(res.value["beta"], np.cov(R, B, ddof=1)[0, 1] / B.var(ddof=1))
    total = float(np.prod(1 + R.to_numpy()) - 1)
    assert_close(res.value["treynor_ratio"], (total - 0.01) / res.value["beta"])
    assert_close(res.value["treynor_ratio"], lib().treynor_ratio(R, B, 252, 0.01))
    assert_bad_data_gives_none(lambda s: call("metrics.versus_benchmark", returns=s, benchmark=B))
    assert_bad_data_gives_none(lambda s: call("metrics.versus_benchmark", returns=R, benchmark=s))


def _annual(x):
    return float(np.prod(1 + x.to_numpy())) ** (252 / len(x)) - 1


def test_capm_FT_069():
    assert_registered("metrics.capm", "FT-069")
    res = call("metrics.capm", returns=R, benchmark=B, rf=0.02)
    beta = np.cov(R, B, ddof=1)[0, 1] / B.var(ddof=1)
    ra, ba = _annual(R), _annual(B)
    assert_close(res.value["beta"], beta)
    assert_close(res.value["capm_expected_return"], 0.02 + beta * (ba - 0.02))
    assert_close(res.value["jensens_alpha"], ra - (0.02 + beta * (ba - 0.02)))
    daily = 1.02 ** (1 / 252) - 1
    residuals = ((R - daily) - beta * (B - daily)) * math.sqrt(252)
    assert_close(res.value["residual_std_annual"], residuals.std(ddof=1))
    assert_close(res.value["appraisal_ratio"], res.value["jensens_alpha"] / residuals.std(ddof=1))
    # against the library functions on the same inputs
    assert_close(res.value["beta"], pm().get_beta(R, B))
    assert_close(res.value["capm_expected_return"], pm().get_capital_asset_pricing_model(0.02, beta, ba))
    assert_close(res.value["jensens_alpha"], pm().get_jensens_alpha(ra, 0.02, beta, ba))
    assert_close(res.value["appraisal_ratio"], pm().get_appraisal_ratio(pd.Series([res.value["jensens_alpha"]]), residuals).iloc[0])
    assert_bad_data_gives_none(lambda s: call("metrics.capm", returns=s, benchmark=B))
    assert_bad_data_gives_none(lambda s: call("metrics.capm", returns=R, benchmark=s))
    with pytest.raises(BadInput):
        call("metrics.capm", returns=R, benchmark=B, rf=-2.0)
    exact = call("metrics.capm", returns=2 * B, benchmark=B)
    assert_close(exact.value["beta"], 2.0)
    assert exact.value["appraisal_ratio"] is None and exact.warnings


def _factors():
    rng = np.random.default_rng(777)
    return pd.DataFrame({"Mkt-RF": B.to_numpy(), "SMB": rng.normal(0, 0.004, N), "HML": rng.normal(0, 0.004, N), "Mom": rng.normal(0, 0.005, N)}, index=R.index)


@pytest.mark.parametrize("model", ["ff3", "carhart"])
def test_factor_model_FT_070(model):
    assert_registered("metrics.factor_model", "FT-070")
    f = _factors()
    cols = ["Mkt-RF", "SMB", "HML"] + (["Mom"] if model == "carhart" else [])
    y = (R + 0.0003 * 0).to_numpy()
    res = call("metrics.factor_model", returns=R, factors=f, model=model)
    design = np.column_stack([np.ones(N), f[cols].to_numpy()])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    assert_close(res.value["alpha"], coef[0], tol=1e-9)
    for i, c in enumerate(cols):
        assert_close(res.value["loadings"][c], coef[i + 1], tol=1e-9)
    fitted = design @ coef
    r2 = 1 - ((y - fitted) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    assert_close(res.value["r_squared"], r2)
    assert_close(res.value["alpha_annual"], coef[0] * 252)
    assert res.value["observations"] == N
    ref, _, err = pm().get_fama_and_french_model_multi(R, f[cols])
    assert err is None
    assert_close(res.value["alpha"], ref["Intercept"])
    assert_close(res.value["loadings"]["SMB"], ref["SMB Slope"])
    with pytest.raises(BadInput):
        call("metrics.factor_model", returns=R, factors=f.drop(columns=["SMB"]), model=model)
    with pytest.raises(BadInput):
        call("metrics.factor_model", returns=R, factors=f, model="ff5")
    with pytest.raises(BadInput):
        call("metrics.factor_model", returns=R, factors=B)
    assert_bad_data_gives_none(lambda s: call("metrics.factor_model", returns=s, factors=f, model=model))


def test_factor_model_few_shared_dates_FT_070():
    f = _factors()
    shifted = f.copy()
    shifted.index = shifted.index + pd.Timedelta(days=4000)
    res = call("metrics.factor_model", returns=R, factors=shifted)
    assert res.value is None and res.warnings


def test_capture_ratios_FT_073():
    assert_registered("metrics.capture_ratios", "FT-073")
    res = call("metrics.capture_ratios", returns=R, benchmark=B)
    up, down = B > 0, B < 0

    def geo(x):
        return float(np.prod(1 + x.to_numpy())) ** (1 / len(x)) - 1

    assert_close(res.value["upside_capture"], geo(R[up]) / geo(B[up]))
    assert_close(res.value["downside_capture"], geo(R[down]) / geo(B[down]))
    assert_close(res.value["upside_capture"], pm().get_upside_capture_ratio(R, B))
    assert_close(res.value["downside_capture"], pm().get_downside_capture_ratio(R, B))
    assert_close(res.value["capture_spread"], res.value["upside_capture"] - res.value["downside_capture"])
    same = call("metrics.capture_ratios", returns=B * 1.0, benchmark=B)
    assert_close(same.value["upside_capture"], 1.0)
    assert_close(same.value["downside_capture"], 1.0)
    assert_bad_data_gives_none(lambda s: call("metrics.capture_ratios", returns=s, benchmark=B))
    assert_bad_data_gives_none(lambda s: call("metrics.capture_ratios", returns=R, benchmark=s))


def test_fama_decomposition_FT_075():
    assert_registered("metrics.fama_decomposition", "FT-075")
    res = call("metrics.fama_decomposition", returns=R, benchmark=B, rf=0.01)
    beta = np.cov(R, B, ddof=1)[0, 1] / B.var(ddof=1)
    ra, ba = _annual(R), _annual(B)
    ratio = R.std(ddof=1) / B.std(ddof=1)
    selectivity = (ra - 0.01) - ratio * (ba - 0.01)
    diversification = (ratio - beta) * (ba - 0.01)
    assert_close(res.value["net_selectivity"], selectivity)
    assert_close(res.value["diversification"], diversification)
    assert_close(res.value["net_selectivity"] + res.value["diversification"], res.value["jensens_alpha"])
    ref = pm().get_fama_decomposition(ra, 0.01, beta, ba, R.std(ddof=1), B.std(ddof=1))
    assert_close(res.value["net_selectivity"], ref[0])
    assert_close(res.value["diversification"], ref[1])
    assert_bad_data_gives_none(lambda s: call("metrics.fama_decomposition", returns=s, benchmark=B))
    assert_bad_data_gives_none(lambda s: call("metrics.fama_decomposition", returns=R, benchmark=s))


def test_market_timing_FT_076():
    assert_registered("metrics.market_timing", "FT-076")
    res = call("metrics.market_timing", returns=R, benchmark=B)
    y, x = R.to_numpy(), B.to_numpy()
    tm_design = np.column_stack([np.ones(N), x, x ** 2])
    tm, *_ = np.linalg.lstsq(tm_design, y, rcond=None)
    hm_design = np.column_stack([np.ones(N), x, np.clip(x, 0, None)])
    hm, *_ = np.linalg.lstsq(hm_design, y, rcond=None)
    assert_close(res.value["treynor_mazuy"]["alpha"], tm[0])
    assert_close(res.value["treynor_mazuy"]["beta"], tm[1])
    assert_close(res.value["treynor_mazuy"]["gamma"], tm[2])
    assert_close(res.value["henriksson_merton"]["down_market_beta"], hm[1])
    assert_close(res.value["henriksson_merton"]["up_market_beta"], hm[1] + hm[2])
    ref, _ = pm().get_treynor_mazuy_model(R, B)
    assert_close(res.value["treynor_mazuy"]["gamma"], ref["Gamma"])
    # a manager that doubles exposure in up markets has a positive timing coefficient
    timed = B * np.where(B > 0, 2.0, 1.0)
    skill = call("metrics.market_timing", returns=timed + np.linspace(-1e-6, 1e-6, N), benchmark=B)
    assert skill.value["henriksson_merton"]["up_market_beta_extra"] > 0.9
    assert skill.value["treynor_mazuy"]["gamma"] > 0
    assert_bad_data_gives_none(lambda s: call("metrics.market_timing", returns=s, benchmark=B))
    assert_bad_data_gives_none(lambda s: call("metrics.market_timing", returns=R, benchmark=s))

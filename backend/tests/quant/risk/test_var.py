"""Value at risk and expected shortfall. Rows FT-041 to FT-047, SK-024 to SK-026 and CC-06."""
from statistics import NormalDist

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.risk import _lib

from .conftest import call, piece_ids

TOL = 1e-9
VAR = _lib.ft_var
CVAR = _lib.ft_cvar
Z95 = NormalDist().inv_cdf(0.95)


def test_var_historical_gaussian_and_rolling_FT_041(ret, ramp):
    assert call("risk.var", returns=ret).value == pytest.approx(-VAR.get_var_historic(ret, 0.05), abs=TOL)
    assert call("risk.var", returns=ret, method="gaussian").value == pytest.approx(-VAR.get_var_gaussian(ret, 0.05), abs=TOL)
    # hand values: evenly spaced returns from -5% to +5% have a 5th percentile of exactly -4.5%
    assert call("risk.var", returns=ramp).value == pytest.approx(0.045, abs=TOL)
    # normal case: VaR 95% = -(mu - 1.645 sigma)
    gaussian = call("risk.var", returns=ret, method="gaussian")
    assert gaussian.value == pytest.approx(-(ret.mean() - Z95 * ret.std(ddof=1)), abs=TOL)
    assert gaussian.method == "gaussian" and gaussian.params == {"level": 0.95, "method": "gaussian"}
    rolling = call("risk.rolling_var", returns=ret, window=250)
    origin = -VAR.get_rolling_var_historic(ret, 0.05, 250).dropna()
    assert rolling.value["latest"] == pytest.approx(origin.iloc[-1], abs=TOL)
    assert rolling.value["values"] == pytest.approx(list(origin.to_numpy()), abs=TOL)
    assert rolling.value["latest"] == pytest.approx(float(-np.quantile(ret.iloc[-250:], 0.05)), abs=TOL)
    assert "FT-041" in piece_ids("risk.var") and "FT-041" in piece_ids("risk.rolling_var")


def test_var_evt_and_gpd_tail_FT_042(ret):
    res = call("risk.var", returns=ret, level=0.99, method="evt")
    assert res.value == pytest.approx(-float(np.asarray(VAR.get_var_evt(ret, 0.01, warn=False)).ravel()[0]), abs=TOL)
    shape, scale, threshold, prob = VAR.fit_gpd_tail(ret.to_frame("x"), 0.95)
    tail = call("risk.gpd_tail", returns=ret)
    assert tail.value["shape"] == pytest.approx(shape[0], abs=TOL)
    assert tail.value["scale"] == pytest.approx(scale[0], abs=TOL)
    # hand value: the threshold is the 95th percentile of the losses and 5% of the sample exceeds it
    assert tail.value["threshold"] == pytest.approx(float(np.percentile(-ret, 95)), abs=TOL)
    assert tail.value["exceedance_probability"] == pytest.approx(0.05, abs=0.001)
    # too few observations for a tail fit: value None and a reason
    short = call("risk.var", returns=ret.iloc[:20], method="evt")
    assert short.value is None and short.warnings
    assert "FT-042" in piece_ids("risk.var") and "FT-042" in piece_ids("risk.gpd_tail")


def test_var_student_t_and_cornish_fisher_FT_043(ret):
    assert call("risk.var", returns=ret, method="student_t").value == pytest.approx(-float(VAR.get_var_studentt(ret, 0.05)[0]), abs=TOL)
    cf = call("risk.var", returns=ret, method="cornish_fisher")
    assert cf.value == pytest.approx(-VAR.get_var_cornish_fisher(ret, 0.05), abs=TOL)
    # hand value: with zero skew and zero excess kurtosis Cornish-Fisher is the Gaussian quantile
    rng = np.random.default_rng(7)
    sample = pd.Series(rng.standard_normal(200000) * 0.01)
    sample = (sample - sample.mean()) / sample.std(ddof=1) * 0.01
    cf_normal = call("risk.var", returns=sample, method="cornish_fisher").value
    assert cf_normal == pytest.approx(Z95 * 0.01, abs=2e-4)
    assert "FT-043" in piece_ids("risk.var")


@pytest.mark.parametrize("distribution", ["historical", "gaussian", "student_t", "cornish_fisher"])
def test_marginal_and_component_var_FT_044(panel, weights, distribution):
    names = {"historical": "historic", "gaussian": "gaussian", "student_t": "studentt", "cornish_fisher": "cornish-fisher"}
    w = pd.Series(weights)
    marginal = call("risk.marginal_var", panel=panel, weights=weights, distribution=distribution)
    component = call("risk.component_var", panel=panel, weights=weights, distribution=distribution)
    origin = VAR.get_marginal_var(panel, w, 0.05, names[distribution])
    for asset, value in origin.items():
        assert marginal.value["contributions"][asset] == pytest.approx(-value, abs=TOL)
    # hand value: the components add up to the VaR of the portfolio and each is weight times marginal
    for asset in panel.columns:
        assert component.value["contributions"][asset] == pytest.approx(weights[asset] * marginal.value["contributions"][asset], abs=1e-12)
    portfolio = call("risk.var", returns=panel @ w, method=distribution).value
    assert component.value["total"] == pytest.approx(portfolio, abs=1e-6)
    assert "FT-044" in piece_ids("risk.marginal_var") and "FT-044" in piece_ids("risk.component_var")


def test_contribution_bad_input_FT_044(panel, weights):
    with pytest.raises(BadInput):
        call("risk.marginal_var", panel=panel, weights={"ZZZ": 1.0})
    with pytest.raises(BadInput):
        call("risk.component_var", panel=panel, weights=[0.2] * 5)
    assert call("risk.component_var", panel=panel, weights=weights, distribution="laplace").value is None


@pytest.mark.parametrize("method", ["historical", "gaussian", "student_t", "laplace", "logistic", "cornish_fisher", "evt"])
def test_cvar_eight_variants_FT_045(ret, method):
    fn = {"historical": CVAR.get_cvar_historic, "gaussian": CVAR.get_cvar_gaussian, "student_t": CVAR.get_cvar_studentt,
          "laplace": CVAR.get_cvar_laplace, "logistic": CVAR.get_cvar_logistic, "cornish_fisher": CVAR.get_cvar_cornish_fisher}
    res = call("risk.cvar", returns=ret, method=method)
    if method == "evt":
        origin = CVAR.get_cvar_evt(ret, 0.05, warn=False)
    else:
        origin = fn[method](ret, 0.05)
    assert res.value == pytest.approx(-float(np.asarray(origin).ravel()[0]), abs=TOL)
    assert res.value >= call("risk.var", returns=ret, method="historical").value * 0.9


def test_cvar_rolling_and_hand_values_FT_045(ret, ramp):
    rolling = call("risk.rolling_cvar", returns=ret, window=250)
    origin = -CVAR.get_rolling_cvar_historic(ret, 0.05, 250).dropna()
    assert rolling.value["latest"] == pytest.approx(origin.iloc[-1], abs=TOL)
    # hand value: the mean of the six worst returns -5.0% to -4.5% is -4.75%
    assert call("risk.cvar", returns=ramp).value == pytest.approx(0.0475, abs=TOL)
    with pytest.raises(BadInput):
        call("risk.rolling_cvar", returns=ret, window=2.5)
    assert call("risk.cvar", returns=ret, method="expected").value is None
    assert "FT-045" in piece_ids("risk.cvar") and "FT-045" in piece_ids("risk.rolling_cvar")


def test_evar_gaussian_FT_046(ret):
    res = call("risk.evar", returns=ret)
    assert res.value == pytest.approx(-_lib.ft_evar.get_evar_gaussian(ret, 0.05), abs=TOL)
    # hand value: the closed form EVaR = -mu + sigma * sqrt(-2 ln(alpha)) bounds the Gaussian VaR from above
    closed = -ret.mean() + ret.std(ddof=1) * np.sqrt(-2 * np.log(0.05))
    assert res.value == pytest.approx(closed, abs=1e-6)
    assert res.value >= call("risk.var", returns=ret, method="gaussian").value
    assert "FT-046" in piece_ids("risk.evar")


def test_covar_FT_047(panel):
    a, b = panel["AAA"], panel["BBB"]
    res = call("risk.covar", returns=a, conditioning_returns=b)
    origin = _lib.ft_covar.get_covar(a, b, 0.05)
    assert res.value["covar"] == pytest.approx(-origin["CoVaR"], abs=TOL)
    assert res.value["delta_covar"] == pytest.approx(-origin["Delta-CoVaR"], abs=TOL)
    assert res.value["observations"] == 1000
    # hand value: delta CoVaR = slope * (VaR of the conditioning series - its median), as a loss
    var_b = -float(np.quantile(b, 0.05))
    assert res.value["delta_covar"] == pytest.approx(-res.value["slope"] * (-var_b - float(b.median())), abs=1e-6)
    constant = call("risk.covar", returns=a, conditioning_returns=pd.Series(0.01, index=a.index))
    assert constant.value is None and constant.warnings
    assert "FT-047" in piece_ids("risk.covar")


def test_empirical_value_at_risk_SK_024(ret, ramp):
    for level in (0.9, 0.95, 0.99):
        res = call("risk.var", returns=ret, level=level, method="empirical")
        assert res.value == pytest.approx(_lib.sk_measures.value_at_risk(ret.to_numpy(), beta=level), abs=TOL)
        assert res.params["level"] == level
    assert call("risk.var", returns=ramp, method="empirical").value == pytest.approx(0.045, abs=TOL)
    assert "SK-024" in piece_ids("risk.var")


def test_empirical_cvar_SK_025(ret, ramp):
    res = call("risk.cvar", returns=ret, level=0.95, method="empirical")
    assert res.value == pytest.approx(_lib.sk_measures.cvar(ret.to_numpy(), beta=0.95), abs=TOL)
    # hand value: the 101 ramp points leave 5.05 observations in the 5% tail
    assert call("risk.cvar", returns=ramp, method="empirical").value == pytest.approx(_lib.sk_measures.cvar(ramp.to_numpy(), beta=0.95), abs=TOL)
    assert call("risk.cvar", returns=ramp, method="empirical").value == pytest.approx(0.0479702970297, abs=1e-9)
    assert "SK-025" in piece_ids("risk.cvar")


def test_entropic_measures_SK_026(ret, tiny):
    assert call("risk.evar", returns=ret, method="empirical").value == pytest.approx(_lib.sk_measures.evar(ret.to_numpy(), beta=0.95), abs=TOL)
    erm = call("risk.entropic_risk_measure", returns=ret, theta=2.0, level=0.9)
    assert erm.value == pytest.approx(_lib.sk_measures.entropic_risk_measure(ret.to_numpy(), theta=2.0, beta=0.9), abs=TOL)
    # hand value: ERM = (1/theta) ln(mean(exp(-theta r)) / (1 - beta))
    x = np.array(tiny)
    assert call("risk.entropic_risk_measure", returns=tiny, theta=1.0, level=0.95).value == pytest.approx(
        float(np.log(np.exp(-x).mean() / 0.05)), abs=TOL)
    assert call("risk.entropic_risk_measure", returns=ret, theta=-1.0).value is None
    with pytest.raises(BadInput):
        call("risk.entropic_risk_measure", returns=ret, theta="x")
    assert "SK-026" in piece_ids("risk.evar") and "SK-026" in piece_ids("risk.entropic_risk_measure")


def test_public_entry_points_match_registry_SK_024(ret):
    from hokma.quant import risk

    for method in ("historical", "gaussian", "evt"):
        assert risk.var(ret, level=0.99, method=method).value == call("risk.var", returns=ret, level=0.99, method=method).value
        assert risk.cvar(ret, level=0.99, method=method).value == call("risk.cvar", returns=ret, level=0.99, method=method).value
    assert risk.var(ret).name == "risk.var" and risk.var(ret).unit == "loss_fraction"


def test_registry_exposes_the_risk_extras_CC_06():
    """C6: the module holds VaR backtest, EVT VaR, copulas and Yang-Zhang volatility, each listed with its piece."""
    names = {s.name: s for s in registry.list("risk")}
    for required in ("risk.var_backtest", "risk.var", "risk.copula_fit", "risk.copula_dependence", "risk.yang_zhang_volatility"):
        assert required in names, required
        assert "CC-06" in names[required].piece_ids
    assert "evt" in registry.describe("risk.var").summary

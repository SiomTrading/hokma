"""Return distributions. Rows FT-058, SK-062, SK-064, SK-065 and SK-066."""
import math
from statistics import NormalDist

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.risk import _lib

from .conftest import call, piece_ids

TOL = 1e-9


def _col(series):
    return series.to_numpy(dtype=float).reshape(-1, 1)


def test_hill_estimator_FT_058(ret):
    res = call("risk.hill_estimator", returns=ret)
    origin = _lib.ft_risk_model.get_hill_estimator(ret.to_frame("x"), k=0.1, tail="left")["x"]
    assert res.value["tail_index"] == pytest.approx(origin.iloc[0], abs=TOL)
    assert res.value["shape_xi"] == pytest.approx(origin.iloc[1], abs=TOL)
    assert res.value["standard_error"] == pytest.approx(origin.iloc[2], abs=TOL)
    assert res.method == "hill_left_tail" and res.params == {"k": 0.1, "tail": "left"}
    # hand value: a symmetric Pareto sample with tail index 3 gives a Hill estimate of 3
    n = 2000
    u = np.linspace(0.5 / n, 1 - 0.5 / n, n)
    p = 0.01 * (1 - u) ** (-1 / 3.0)
    pareto = pd.Series(np.concatenate([-p, p]))
    assert call("risk.hill_estimator", returns=pareto).value["tail_index"] == pytest.approx(3.0, abs=0.01)
    assert call("risk.hill_estimator", returns=pareto, tail="up").value is None
    assert "FT-058" in piece_ids("risk.hill_estimator")


def test_fit_distribution_gaussian_has_closed_form_SK_065(ret):
    res = call("risk.fit_distribution", returns=ret, family="gaussian")
    x = ret.to_numpy()
    mu, sigma = x.mean(), x.std(ddof=0)
    assert res.value["params"]["loc"] == pytest.approx(mu, abs=TOL)
    assert res.value["params"]["scale"] == pytest.approx(sigma, abs=TOL)
    loglik = float(np.sum(-0.5 * np.log(2 * math.pi * sigma**2) - (x - mu) ** 2 / (2 * sigma**2)))
    assert res.value["log_likelihood"] == pytest.approx(loglik, abs=1e-6)
    assert res.value["aic"] == pytest.approx(4 - 2 * loglik, abs=1e-6)
    # analytic VaR of a normal: -(mu - z sigma)
    assert res.value["var"] == pytest.approx(-(mu - NormalDist().inv_cdf(0.95) * sigma), abs=1e-6)
    assert res.method == "gaussian" and res.params["level"] == 0.95


@pytest.mark.parametrize("family", ["gaussian", "student_t", "johnson_su", "normal_inverse_gaussian"])
def test_fit_distribution_matches_origin_SK_065(ret, family):
    res = call("risk.fit_distribution", returns=ret, family=family, seed=0)
    origin = _lib.UNIVARIATE_FAMILIES[family](random_state=0).fit(_col(ret))
    for key, val in origin._scipy_params.items():
        assert res.value["params"][key] == pytest.approx(float(val), abs=TOL)
    assert res.value["log_likelihood"] == pytest.approx(float(origin.score_samples(_col(ret)).sum()), abs=TOL)
    assert res.value["n_params"] == origin.n_params
    assert "SK-065" in piece_ids("risk.fit_distribution")


def test_fit_distribution_bad_input_SK_065(ret):
    assert call("risk.fit_distribution", returns=ret, family="laplace").value is None
    assert call("risk.fit_distribution", returns=ret, level=1.5).value is None
    with pytest.raises(BadInput):
        call("risk.fit_distribution", returns=ret, level="high")


def test_select_univariate_dist_SK_066(ret):
    res = call("risk.select_univariate_dist", returns=ret)
    origin = _lib.sk_dist.select_univariate_dist(_col(ret))
    assert res.value["repr"] == origin.fitted_repr
    names = {cls: key for key, cls in _lib.UNIVARIATE_FAMILIES.items()}
    assert res.value["family"] == names[type(origin)]
    # hand value: the chosen family has the lowest AIC and the Gaussian AIC follows its closed form
    scores = res.value["scores"]
    assert res.value["family"] == min(scores, key=scores.get)
    x = ret.to_numpy()
    sigma = x.std(ddof=0)
    loglik = float(np.sum(-0.5 * np.log(2 * math.pi * sigma**2) - (x - x.mean()) ** 2 / (2 * sigma**2)))
    assert scores["gaussian"] == pytest.approx(4 - 2 * loglik, abs=1e-6)
    bic = call("risk.select_univariate_dist", returns=ret, criterion="bic")
    assert bic.method == "lowest_bic"
    assert call("risk.select_univariate_dist", returns=ret, criterion="hqic").value is None
    assert "SK-066" in piece_ids("risk.select_univariate_dist")


def test_return_distribution_SK_062(panel):
    res = call("risk.return_distribution", panel=panel)
    assert res.value["mu"] == pytest.approx(list(panel.mean()), abs=TOL)
    assert np.allclose(res.value["covariance"], np.cov(panel.to_numpy(), rowvar=False), atol=TOL)
    assert res.value["n_observations"] == 1000 and all(res.value["investable"])
    origin = _lib.sk_prior.ReturnDistribution(mu=panel.mean().to_numpy(), covariance=np.cov(panel.to_numpy(), rowvar=False), returns=panel.to_numpy())
    assert res.value["mu"] == pytest.approx(list(origin.mu), abs=TOL)
    assert "SK-062" in piece_ids("risk.return_distribution")


def test_covariance_sqrt_SK_064(panel):
    res = call("risk.covariance_sqrt", panel=panel)
    factor = np.array(res.value["component"])
    cov = np.cov(panel.to_numpy(), rowvar=False)
    assert np.allclose(factor @ factor.T, cov, atol=1e-15)
    assert np.allclose(factor, np.tril(factor))
    assert res.value["max_reconstruction_error"] < 1e-15
    # hand value: a diagonal covariance has its standard deviations as the square root
    diag = pd.DataFrame({"a": [1.0, -1.0, 1.0, -1.0], "b": [1.0, 1.0, -1.0, -1.0]})
    root = np.array(call("risk.covariance_sqrt", panel=diag).value["component"])
    assert root == pytest.approx(np.diag([math.sqrt(4 / 3)] * 2), abs=TOL)
    singular = pd.DataFrame({"a": [1.0, 2.0, 3.0, 4.0], "b": [2.0, 4.0, 6.0, 8.0]})
    res = call("risk.covariance_sqrt", panel=singular)
    assert res.value is None and res.warnings
    assert "SK-064" in piece_ids("risk.covariance_sqrt")

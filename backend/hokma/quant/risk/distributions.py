"""Return distributions: tail index, fitted families, automatic selection and the containers that carry a distribution."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.risk import _common as c
from hokma.quant.risk import _lib
from hokma.quant.types import QuantResult

_RET = {"type": "series", "required": True}
_FAMILIES = tuple(_lib.UNIVARIATE_FAMILIES)


def _col(series: pd.Series) -> np.ndarray:
    return series.to_numpy(dtype=float).reshape(-1, 1)


def _fitted(dist: Any, x: np.ndarray) -> dict:
    """Parameters and fit statistics of a fitted univariate distribution."""
    n = len(x)
    k = int(dist.n_params)
    loglik = float(dist.score_samples(x).sum())
    return {
        "params": {key: float(val) for key, val in dist._scipy_params.items()},
        "n_params": k,
        "log_likelihood": loglik,
        "aic": 2 * k - 2 * loglik,
        "bic": k * float(np.log(n)) - 2 * loglik,
        "repr": dist.fitted_repr,
    }


@registry.register("risk.hill_estimator", module="risk",
                   summary="Hill tail index of the returns (heavy tail estimator) with its shape and standard error.",
                   inputs={"returns": _RET, "k": {"type": "float", "required": False, "default": 0.1},
                           "tail": {"type": "str", "required": False, "default": "left"}},
                   piece_ids=("FT-058",))
def hill_estimator(returns: Any, k: float = 0.1, tail: str = "left") -> QuantResult:
    params = {"k": k, "tail": tail}

    def compute(s: pd.Series) -> dict:
        if tail not in ("left", "right"):
            raise ValueError("tail must be 'left' or 'right'")
        table = _lib.ft_risk_model.get_hill_estimator(s.to_frame("x"), k=k, tail=tail)["x"]
        return {"tail_index": table.iloc[0], "shape_xi": table.iloc[1], "standard_error": table.iloc[2], "observations_used": table.iloc[3]}

    return c.measure("risk.hill_estimator", returns, unit="index", method=f"hill_{tail}_tail", params=params,
                     piece_ids=("FT-058",), compute=compute, min_obs=20)


@registry.register("risk.fit_distribution", module="risk",
                   summary="Fit one return distribution family (gaussian, student_t, johnson_su, normal_inverse_gaussian) and report parameters, likelihood, AIC, BIC and its VaR.",
                   inputs={"returns": _RET, "family": {"type": "str", "required": False, "default": "student_t"},
                           "level": {"type": "float", "required": False, "default": 0.95},
                           "seed": {"type": "int", "required": False, "default": 0}},
                   piece_ids=("SK-065",))
def fit_distribution(returns: Any, family: str = "student_t", level: float = 0.95, seed: int = 0) -> QuantResult:
    params = {"family": family, "level": level, "seed": seed}
    if family not in _FAMILIES:
        return c.empty("risk.fit_distribution", "return", family, f"unknown family {family!r}; use one of {list(_FAMILIES)}",
                       params=params, piece_ids=("SK-065",))

    def compute(s: pd.Series) -> dict:
        x = _col(s)
        dist = _lib.UNIVARIATE_FAMILIES[family](random_state=seed).fit(x)
        out = _fitted(dist, x)
        out["family"] = family
        out["var"] = -float(dist.ppf(np.array([[1.0 - level]]))[0, 0])
        return out

    return c.measure("risk.fit_distribution", returns, unit="return", method=family, params=params, piece_ids=("SK-065",),
                     compute=compute, min_obs=10, level=level)


@registry.register("risk.select_univariate_dist", module="risk",
                   summary="Fit every candidate family and keep the one with the lowest AIC or BIC.",
                   inputs={"returns": _RET, "criterion": {"type": "str", "required": False, "default": "aic"},
                           "seed": {"type": "int", "required": False, "default": 0}},
                   piece_ids=("SK-066",))
def select_univariate_dist(returns: Any, criterion: str = "aic", seed: int = 0) -> QuantResult:
    params = {"criterion": criterion, "seed": seed}
    selection = {"aic": _lib.sk_dist.SelectionCriterion.AIC, "bic": _lib.sk_dist.SelectionCriterion.BIC}
    if criterion not in selection:
        return c.empty("risk.select_univariate_dist", "return", criterion, f"unknown criterion {criterion!r}; use aic or bic",
                       params=params, piece_ids=("SK-066",))
    names = {cls: name for name, cls in _lib.UNIVARIATE_FAMILIES.items()}

    def compute(s: pd.Series) -> dict:
        x = _col(s)
        candidates = [cls(random_state=seed) for cls in _lib.UNIVARIATE_FAMILIES.values()]
        best = _lib.sk_dist.select_univariate_dist(x, distribution_candidates=candidates, selection_criterion=selection[criterion])
        out = _fitted(best, x)
        out["family"] = names[type(best)]
        out["scores"] = {names[type(cand)]: _fitted(cand.fit(x), x)[criterion] for cand in
                         [cls(random_state=seed) for cls in _lib.UNIVARIATE_FAMILIES.values()]}
        return out

    return c.measure("risk.select_univariate_dist", returns, unit="return", method=f"lowest_{criterion}", params=params,
                     piece_ids=("SK-066",), compute=compute, min_obs=10)


@registry.register("risk.return_distribution", module="risk",
                   summary="Container of a returns panel: expected returns, covariance and scenarios, ready for the other risk functions.",
                   inputs={"panel": {"type": "dataframe", "required": True}}, piece_ids=("SK-062",))
def return_distribution(panel: Any) -> QuantResult:
    name, unit, method, pieces = "risk.return_distribution", "return", "empirical", ("SK-062",)
    frame = c.as_panel(panel)
    cleaned, warns = c.clean_panel(frame, min_obs=3)
    if not c.panel_ok(cleaned, warns, 3):
        return c.empty(name, unit, method, warns or ("degenerate input",), piece_ids=pieces)
    values = cleaned.to_numpy()
    dist = _lib.sk_prior.ReturnDistribution(mu=values.mean(axis=0), covariance=np.cov(values, rowvar=False), returns=values)
    mask = dist.investable_mask
    value = {"assets": [str(x) for x in cleaned.columns], "mu": dist.mu, "covariance": dist.covariance,
             "n_observations": int(dist.returns.shape[0]),
             "investable": [True] * values.shape[1] if mask is None else [bool(m) for m in mask]}
    return c.build(name, value, unit=unit, method=method, params={}, piece_ids=pieces, inputs=(cleaned,),
                   as_of_source=cleaned.iloc[:, 0], warnings=warns)


@registry.register("risk.covariance_sqrt", module="risk",
                   summary="Matrix square root of the covariance of a returns panel (A with A A' = covariance) and its reconstruction error.",
                   inputs={"panel": {"type": "dataframe", "required": True}}, piece_ids=("SK-064",))
def covariance_sqrt(panel: Any) -> QuantResult:
    name, unit, method, pieces = "risk.covariance_sqrt", "return", "cholesky", ("SK-064",)
    frame = c.as_panel(panel)
    cleaned, warns = c.clean_panel(frame, min_obs=3)
    if not c.panel_ok(cleaned, warns, 3):
        return c.empty(name, unit, method, warns or ("degenerate input",), piece_ids=pieces)
    cov = np.cov(cleaned.to_numpy(), rowvar=False)
    try:
        factor = np.linalg.cholesky(cov)
    except np.linalg.LinAlgError:
        return c.empty(name, unit, method, warns + ("covariance is not positive definite",), piece_ids=pieces)
    holder = _lib.sk_prior.CovarianceSqrt(components=(factor,))
    rebuilt = sum(a @ a.T for a in holder.components)
    value = {"assets": [str(x) for x in cleaned.columns], "component": holder.components[0],
             "max_reconstruction_error": float(np.abs(rebuilt - cov).max())}
    return c.build(name, value, unit=unit, method=method, params={}, piece_ids=pieces, inputs=(cleaned,),
                   as_of_source=cleaned.iloc[:, 0], warnings=warns)

"""Dependence between assets or desks: bivariate copulas, tail dependence and vine copulas."""
from __future__ import annotations

from typing import Any

import warnings

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.risk import _common as c
from hokma.quant.risk import _lib
from hokma.quant.types import QuantResult

_X = {"type": "series", "required": True}
_Y = {"type": "series", "required": True}
_SEED = {"type": "int", "required": False, "default": 0}
_MIN_PAIRS = 30
FAMILIES = tuple(_lib.COPULA_FAMILIES)
FT_FAMILIES = ("gaussian", "student_t", "clayton", "gumbel", "frank")


def _pair_or_empty(name: str, unit: str, method: str, x: Any, y: Any, params: dict, pieces: tuple[str, ...]):
    sx, sy, warns = c.pair(x, y)
    if len(sx) < _MIN_PAIRS or c.is_constant(sx) or c.is_constant(sy):
        return None, c.empty(name, unit, method, warns + (f"need {_MIN_PAIRS} complete pairs and no constant series",),
                             params=params, piece_ids=pieces)
    return (sx, sy, warns), None


def _copula_summary(model: Any, u: np.ndarray, family: str) -> dict:
    n = len(u)
    k = int(model.n_params)
    loglik = float(model.score_samples(u).sum())
    out: dict[str, Any] = {
        "family": family, "n_params": k, "log_likelihood": loglik, "aic": 2 * k - 2 * loglik,
        "bic": k * float(np.log(n)) - 2 * loglik,
        "lower_tail_dependence": float(model.lower_tail_dependence), "upper_tail_dependence": float(model.upper_tail_dependence),
    }
    for attr, key in (("rho_", "rho"), ("theta_", "theta"), ("dof_", "dof")):
        if hasattr(model, attr):
            out[key] = float(getattr(model, attr))
    if hasattr(model, "rotation_"):
        out["rotation"] = str(model.rotation_)
    return out


@registry.register("risk.copula_fit", module="risk",
                   summary="Fit one bivariate copula (gaussian, student_t, clayton, gumbel, joe, independent) to two return series: parameters, tail dependence, likelihood, AIC and BIC.",
                   inputs={"x": _X, "y": _Y, "family": {"type": "str", "required": False, "default": "student_t"}, "seed": _SEED},
                   piece_ids=("SK-067", "CC-06"))
def copula_fit(x: Any, y: Any, family: str = "student_t", seed: int = 0) -> QuantResult:
    name, unit, pieces = "risk.copula_fit", "copula", ("SK-067", "CC-06")
    params = {"family": family, "seed": seed}
    if family not in FAMILIES:
        return c.empty(name, unit, family, f"unknown family {family!r}; use one of {list(FAMILIES)}", params=params, piece_ids=pieces)
    got, bad = _pair_or_empty(name, unit, family, x, y, params, pieces)
    if bad is not None:
        return bad
    sx, sy, warns = got
    try:
        u = _lib.sk_dist.compute_pseudo_observations(np.column_stack([sx.to_numpy(), sy.to_numpy()]))
        model = _lib.COPULA_FAMILIES[family](random_state=seed).fit(u)
        value = _copula_summary(model, u, family)
    except Exception as exc:
        return c.empty(name, unit, family, warns + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=pieces)
    return c.build(name, value, unit=unit, method=family, params=params, piece_ids=pieces, inputs=(sx, sy), as_of_source=sx, warnings=warns)


def _calibrate(ft: Any, family: str, sx: pd.Series, sy: pd.Series, level: float) -> Any:
    if family == "empirical":
        return ft.get_tail_dependence_coefficient(sx, sy, q=level, method="empirical")
    fit = {"gaussian": ft.get_gaussian_copula_parameters, "student_t": ft.get_student_t_copula_parameters,
           "clayton": ft.get_clayton_copula_parameters, "gumbel": ft.get_gumbel_copula_parameters,
           "frank": ft.get_frank_copula_parameters}[family]
    return fit(sx, sy)


@registry.register("risk.copula_dependence", module="risk",
                   summary="Parametric copula calibration (gaussian, student_t, clayton, gumbel, frank) or the empirical tail dependence of two return series.",
                   inputs={"x": _X, "y": _Y, "family": {"type": "str", "required": False, "default": "student_t"},
                           "level": {"type": "float", "required": False, "default": 0.95}},
                   piece_ids=("FT-066", "CC-06"))
def copula_dependence(x: Any, y: Any, family: str = "student_t", level: float = 0.95) -> QuantResult:
    name, unit, pieces = "risk.copula_dependence", "copula", ("FT-066", "CC-06")
    params = {"family": family, "level": level}
    options = FT_FAMILIES + ("empirical",)
    if family not in options:
        return c.empty(name, unit, family, f"unknown family {family!r}; use one of {list(options)}", params=params, piece_ids=pieces)
    problem = c.level_warning(level)
    if problem:
        return c.empty(name, unit, family, problem, params=params, piece_ids=pieces)
    got, bad = _pair_or_empty(name, unit, family, x, y, params, pieces)
    if bad is not None:
        return bad
    sx, sy, warns = got
    ft = _lib.ft_copula
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # the calibration probes parameters where the density overflows
            table = _calibrate(ft, family, sx, sy, level)
    except Exception as exc:
        return c.empty(name, unit, family, warns + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=pieces)
    value = {str(k).lower().replace(" ", "_").replace("-", "_"): v for k, v in dict(table).items()}
    value["family"] = family
    return c.build(name, value, unit=unit, method=family, params=params, piece_ids=pieces, inputs=(sx, sy), as_of_source=sx, warnings=warns)


@registry.register("risk.select_bivariate_copula", module="risk",
                   summary="Fit every copula family to two return series and keep the one with the lowest AIC or BIC (independence is kept when the series are not dependent).",
                   inputs={"x": _X, "y": _Y, "criterion": {"type": "str", "required": False, "default": "aic"}, "seed": _SEED},
                   piece_ids=("SK-068",))
def select_bivariate_copula(x: Any, y: Any, criterion: str = "aic", seed: int = 0) -> QuantResult:
    name, unit, pieces = "risk.select_bivariate_copula", "copula", ("SK-068",)
    params = {"criterion": criterion, "seed": seed}
    selection = {"aic": _lib.sk_dist.SelectionCriterion.AIC, "bic": _lib.sk_dist.SelectionCriterion.BIC}
    if criterion not in selection:
        return c.empty(name, unit, criterion, f"unknown criterion {criterion!r}; use aic or bic", params=params, piece_ids=pieces)
    got, bad = _pair_or_empty(name, unit, criterion, x, y, params, pieces)
    if bad is not None:
        return bad
    sx, sy, warns = got
    names = {cls: key for key, cls in _lib.COPULA_FAMILIES.items()}
    try:
        u = _lib.sk_dist.compute_pseudo_observations(np.column_stack([sx.to_numpy(), sy.to_numpy()]))
        # independence is not a candidate: the selection returns it itself when the test finds no dependence
        candidates = [cls(random_state=seed) for key, cls in _lib.COPULA_FAMILIES.items() if key != "independent"]
        best = _lib.sk_dist.select_bivariate_copula(u, copula_candidates=candidates, selection_criterion=selection[criterion])
        value = _copula_summary(best, u, names[type(best)])
    except Exception as exc:
        return c.empty(name, unit, criterion, warns + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=pieces)
    return c.build(name, value, unit=unit, method=f"lowest_{criterion}", params=params, piece_ids=pieces, inputs=(sx, sy),
                   as_of_source=sx, warnings=warns)


@registry.register("risk.tail_concentration", module="risk",
                   summary="Empirical tail concentration of two return series: how often both fall together (quantiles below 0.5) or both rise together (above 0.5).",
                   inputs={"x": _X, "y": _Y, "quantiles": {"type": "list", "required": False, "default": [0.05, 0.1, 0.5, 0.9, 0.95]}},
                   piece_ids=("SK-069",))
def tail_concentration(x: Any, y: Any, quantiles: Any = (0.05, 0.1, 0.5, 0.9, 0.95)) -> QuantResult:
    name, unit, method, pieces = "risk.tail_concentration", "prob", "empirical_tail_concentration", ("SK-069",)
    try:
        q = np.asarray(quantiles, dtype=float).ravel()
    except (TypeError, ValueError) as exc:
        raise BadInput("quantiles: values are not numeric") from exc
    params = {"quantiles": [float(v) for v in q]}
    if q.size == 0 or ((q <= 0) | (q >= 1)).any():
        return c.empty(name, unit, method, "quantiles must be non empty and strictly between 0 and 1", params=params, piece_ids=pieces)
    got, bad = _pair_or_empty(name, unit, method, x, y, params, pieces)
    if bad is not None:
        return bad
    sx, sy, warns = got
    try:
        u = _lib.sk_dist.compute_pseudo_observations(np.column_stack([sx.to_numpy(), sy.to_numpy()]))
        conc = _lib.sk_dist.empirical_tail_concentration(u, q)
    except Exception as exc:
        return c.empty(name, unit, method, warns + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=pieces)
    value = {"quantiles": q, "concentration": conc}
    return c.build(name, value, unit=unit, method=method, params=params, piece_ids=pieces, inputs=(sx, sy), as_of_source=sx, warnings=warns)


@registry.register("risk.vine_copula", module="risk",
                   summary="Fit a regular vine copula to a returns panel (multivariate dependence of a portfolio or of desks): structure size, parameters, likelihood and fitted marginals.",
                   inputs={"panel": {"type": "dataframe", "required": True}, "max_depth": {"type": "int", "required": False, "default": 4},
                           "seed": _SEED},
                   piece_ids=("SK-070",))
def vine_copula(panel: Any, max_depth: int = 4, seed: int = 0) -> QuantResult:
    name, unit, method, pieces = "risk.vine_copula", "copula", "regular_vine", ("SK-070",)
    from hokma.quant import limits

    limits.check_max_depth(max_depth, getattr(panel, "shape", (0, None))[1] if len(getattr(panel, "shape", ())) == 2 else None)
    params = {"max_depth": max_depth, "seed": seed}
    frame = c.as_panel(panel)
    cleaned, warns = c.clean_panel(frame, min_obs=_MIN_PAIRS)
    if not c.panel_ok(cleaned, warns, _MIN_PAIRS):
        return c.empty(name, unit, method, warns or ("degenerate input",), params=params, piece_ids=pieces)
    try:
        model = _lib.sk_dist.VineCopula(random_state=seed, max_depth=max_depth).fit(cleaned)
        loglik = float(model.score_samples(cleaned).sum())
        k, n = int(model.n_params), len(cleaned)
        value = {
            "assets": [str(a) for a in cleaned.columns], "n_params": k, "n_trees": len(model.trees_),
            "log_likelihood": loglik, "aic": 2 * k - 2 * loglik, "bic": k * float(np.log(n)) - 2 * loglik,
            "central_assets": [] if model.central_assets_ is None else [str(a) for a in model.central_assets_],
            "marginals": {str(a): d.fitted_repr for a, d in zip(cleaned.columns, model.marginal_distributions_)},
        }
    except Exception as exc:
        return c.empty(name, unit, method, warns + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=pieces)
    return c.build(name, value, unit=unit, method=method, params=params, piece_ids=pieces, inputs=(cleaned,),
                   as_of_source=cleaned.iloc[:, 0], warnings=warns)

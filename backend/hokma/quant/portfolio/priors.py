"""Estimators of the return distribution (SK-051, SK-053, SK-055, SK-056).

Variance (empirical and exponentially weighted), covariance with implied volatilities, Black-Litterman views and entropy pooling
views. Views are given as structured dicts and written as text here: the agent never sends a pattern to the library.
"""
from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _common as c
from hokma.quant.portfolio import _lib
from hokma.quant.types import QuantResult

_RET = {"type": "dataframe", "required": True}
MIN_OBS = 3
MAX_VIEWS = 100
MAX_HALF_LIFE = 10_000.0
VARIANCE_METHODS = ("empirical", "ew")
RELATIONS = ("==", "<=", ">=")


@registry.register("portfolio.variance", module="portfolio",
                   summary="Variance of each asset: empirical (sample, ddof 1) or exponentially weighted with a half life.",
                   inputs={"returns": _RET, "method": {"type": "str", "required": False, "default": "empirical"},
                           "half_life": {"type": "float", "required": False, "default": 40.0},
                           "window_size": {"type": "int", "required": False},
                           "assume_centered": {"type": "bool", "required": False}},
                   piece_ids=("SK-053",))
def variance(returns: Any, method: str = "empirical", half_life: float = 40.0, window_size: int | None = None,
             assume_centered: bool | None = None) -> QuantResult:
    name, pieces = "portfolio.variance", ("SK-053",)
    params = {"method": method, "half_life": half_life, "window_size": window_size, "assume_centered": assume_centered}

    def compute() -> Any:
        which = c.choice("method", method, VARIANCE_METHODS)
        panel, warns = c.usable_panel(returns, min_obs=MIN_OBS)
        if panel is None:
            raise c.Domain("; ".join(warns))
        window = None if window_size is None else int(c.number("window_size", window_size, low=2, integer=True))
        if which == "empirical":
            est = _lib.sk_moments.EmpiricalVariance(window_size=window, assume_centered=bool(assume_centered) if assume_centered is not None else False)
        else:
            hl = c.number("half_life", half_life, low=0.0, high=MAX_HALF_LIFE, low_open=True)
            est = _lib.sk_moments.EWVariance(half_life=hl, window_size=window,
                                             assume_centered=True if assume_centered is None else bool(assume_centered))
        values = np.asarray(_lib.quiet(est.fit, panel).variance_, dtype=float)
        if not np.all(np.isfinite(values)):
            raise c.Domain("not enough observations for this method and half life (variance is not finite)")
        return {"variance": c.weights_to_dict(values, panel.columns),
                "standard_deviation": c.weights_to_dict(np.sqrt(values), panel.columns)}, warns

    return c.run(name, unit="variance", method="EmpiricalVariance" if method == "empirical" else "EWVariance", params=params,
                 piece_ids=pieces, compute=compute, inputs=(returns, params))


@registry.register("portfolio.implied_covariance", module="portfolio",
                   summary="Covariance from implied volatilities: the realized correlation of the history and volatilities forecast from the implied ones over a window.",
                   inputs={"returns": _RET, "implied_vol": {"type": "dataframe", "required": True},
                           "window_size": {"type": "int", "required": False, "default": 20},
                           "annualization_factor": {"type": "float", "required": False}},
                   piece_ids=("SK-051",))
def implied_covariance(returns: Any, implied_vol: Any, window_size: int = 20, annualization_factor: float | None = None) -> QuantResult:
    name, pieces = "portfolio.implied_covariance", ("SK-051",)
    params = {"window_size": window_size, "annualization_factor": annualization_factor}

    def compute() -> Any:
        panel = c.as_panel(returns, min_cols=1)
        iv = c.as_panel(implied_vol, name="implied_vol", min_cols=1)
        if list(map(str, iv.columns)) != list(map(str, panel.columns)):
            raise BadInput("implied_vol must have the same assets as returns, in the same order")
        if len(iv) != len(panel):
            raise BadInput("implied_vol must have the same number of rows as returns")
        window = int(c.number("window_size", window_size, low=2, integer=True))
        annual = None if annualization_factor is None else c.number("annualization_factor", annualization_factor, low=0.0, low_open=True)
        both = pd.concat([panel.reset_index(drop=True), iv.reset_index(drop=True).add_prefix("iv_")], axis=1).replace([np.inf, -np.inf], np.nan)
        keep = both.notna().all(axis=1).to_numpy()
        warns = (f"dropped {int((~keep).sum())} row(s) with NaN or infinite values",) if not keep.all() else ()
        if keep.sum() < window + 2:
            raise c.Domain(f"need at least {window + 2} complete rows, got {int(keep.sum())}")
        est = _lib.sk_moments.ImpliedCovariance(window_size=window, annualization_factor=annual)
        cov = np.asarray(_lib.quiet(est.fit, panel[keep], implied_vol=iv[keep]).covariance_, dtype=float)
        if not np.all(np.isfinite(cov)):
            raise c.Domain("covariance is not finite")
        vol = np.sqrt(np.diag(cov))
        corr = cov / np.outer(vol, vol)
        return {"covariance": c.matrix_to_dict(cov, panel.columns), "volatility": c.weights_to_dict(vol, panel.columns),
                "correlation": c.matrix_to_dict(corr, panel.columns)}, warns

    return c.run(name, unit="covariance", method="ImpliedCovariance", params=params, piece_ids=pieces, compute=compute,
                 inputs=(returns, implied_vol, params))


def view_text(view: Any, columns: pd.Index, *, allowed: tuple[str, ...] = ("==",), label: str = "view") -> str:
    """One view as the text the library reads: {"assets": {"AAA": 1, "BBB": -1}, "relation": "==", "value": 0.001}."""
    if not isinstance(view, Mapping) or set(view) - {"assets", "relation", "value"}:
        raise BadInput(f"{label}: expected a dict with assets, relation and value")
    assets = view.get("assets")
    if not isinstance(assets, Mapping) or not assets:
        raise BadInput(f"{label}: assets must be a non-empty dict asset -> coefficient")
    names = {str(col) for col in columns}
    parts: list[str] = []
    for asset, coef in assets.items():
        if str(asset) not in names:
            raise BadInput(f"{label}: unknown asset {asset!r}")
        k = c.number(f"{label} coefficient of {asset}", coef)
        if k == 0.0:
            raise c.Domain(f"{label}: coefficient of {asset} is zero")
        sign = "-" if k < 0 else "+"
        term = f"{abs(k)!r} * {asset}"
        parts.append(f"{sign} {term}" if parts else (f"- {term}" if k < 0 else term))
    relation = view.get("relation", "==")
    if relation not in allowed:
        raise c.Domain(f"{label}: relation {relation!r} is not allowed here; use one of {list(allowed)}")
    value = c.number(f"{label} value", view.get("value"))
    return f"{' '.join(parts)} {relation} {value!r}"


def _views(views: Any, columns: pd.Index, *, allowed: tuple[str, ...], label: str) -> list[str]:
    if views is None:
        return []
    if not isinstance(views, (list, tuple)) or len(views) > MAX_VIEWS:
        raise BadInput(f"{label} must be a list of at most {MAX_VIEWS} views")
    return [view_text(v, columns, allowed=allowed, label=f"{label}[{i}]") for i, v in enumerate(views)]


@registry.register("portfolio.black_litterman", module="portfolio",
                   summary="Black-Litterman: expected returns and covariance after mixing the market prior with views (each view an equality with its confidence).",
                   inputs={"returns": _RET, "views": {"type": "list", "required": True},
                           "view_confidences": {"type": "list", "required": False},
                           "tau": {"type": "float", "required": False, "default": 0.05},
                           "risk_free_rate": {"type": "float", "required": False, "default": 0.0}},
                   piece_ids=("SK-055",))
def black_litterman(returns: Any, views: Any, view_confidences: Any = None, tau: float = 0.05, risk_free_rate: float = 0.0) -> QuantResult:
    name, pieces = "portfolio.black_litterman", ("SK-055",)
    params = {"views": views, "view_confidences": view_confidences, "tau": tau, "risk_free_rate": risk_free_rate}

    def compute() -> Any:
        panel, warns = c.usable_panel(returns, min_obs=MIN_OBS)
        if panel is None:
            raise c.Domain("; ".join(warns))
        texts = _views(views, panel.columns, allowed=("==",), label="views")
        if not texts:
            raise c.Domain("at least one view is needed")
        conf = None
        if view_confidences is not None:
            if not isinstance(view_confidences, (list, tuple)) or len(view_confidences) != len(texts):
                raise BadInput("view_confidences must be a list with one value per view")
            conf = [c.number("view confidence", v, low=0.0, high=1.0, low_open=True) for v in view_confidences]
        est = _lib.sk_prior.BlackLitterman(views=texts, view_confidences=conf, tau=c.number("tau", tau, low=0.0, low_open=True),
                                           risk_free_rate=c.number("risk_free_rate", risk_free_rate))
        rd = _lib.quiet(est.fit, panel).return_distribution_
        return {"mu": c.weights_to_dict(rd.mu, panel.columns), "covariance": c.matrix_to_dict(rd.covariance, panel.columns), "views": texts}, warns

    return c.run(name, unit="return", method="BlackLitterman", params=params, piece_ids=pieces, compute=compute, inputs=(returns, params))


@registry.register("portfolio.entropy_pooling", module="portfolio",
                   summary="Entropy pooling: the scenario probabilities closest to the history that satisfy mean and variance views; gives mu, covariance and the cost of the views.",
                   inputs={"returns": _RET, "mean_views": {"type": "list", "required": False},
                           "variance_views": {"type": "list", "required": False}},
                   piece_ids=("SK-056",))
def entropy_pooling(returns: Any, mean_views: Any = None, variance_views: Any = None) -> QuantResult:
    name, pieces = "portfolio.entropy_pooling", ("SK-056",)
    params = {"mean_views": mean_views, "variance_views": variance_views}

    def compute() -> Any:
        panel, warns = c.usable_panel(returns, min_obs=MIN_OBS)
        if panel is None:
            raise c.Domain("; ".join(warns))
        mv = _views(mean_views, panel.columns, allowed=RELATIONS, label="mean_views")
        vv = _views(variance_views, panel.columns, allowed=RELATIONS, label="variance_views")
        if not mv and not vv:
            raise c.Domain("at least one view is needed")
        est = _lib.sk_prior.EntropyPooling(mean_views=mv or None, variance_views=vv or None)
        try:
            fitted = _lib.quiet(est.fit, panel)
        except _lib.SOLVER_ERRORS as exc:
            raise c.Domain(f"{type(exc).__name__}: {str(exc)[:300]}") from exc
        rd = fitted.return_distribution_
        return {"mu": c.weights_to_dict(rd.mu, panel.columns), "covariance": c.matrix_to_dict(rd.covariance, panel.columns),
                "relative_entropy": c.jsonable(fitted.relative_entropy_),
                "effective_number_of_scenarios": c.jsonable(fitted.effective_number_of_scenarios_), "n_scenarios": int(len(panel))}, warns

    return c.run(name, unit="return", method="EntropyPooling", params=params, piece_ids=pieces, compute=compute, inputs=(returns, params))

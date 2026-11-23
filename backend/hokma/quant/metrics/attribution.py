"""Performance attribution by asset, factor and factor family, predicted (from a factor model) and realized (from returns).

Every function takes plain arrays and lists and returns a QuantResult whose value holds plain lists and floats. Volatility
contributions are additive: they sum to the portfolio volatility. Mean contributions are additive too.
"""
from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.metrics import _lib
from hokma.quant.metrics.summary import empty, finish, quiet
from hokma.quant.types import QuantResult

_PARTS = ("systematic", "idio", "unattributed", "total")
_BY = {"asset": "assets", "factor": "factors", "family": "families"}


def to_plain(obj: Any) -> Any:
    """A container or array as plain dicts, lists and floats."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, np.ndarray):
        return [to_plain(x) for x in obj.tolist()] if obj.dtype.kind in "fiub" else obj.astype(str).tolist()
    if isinstance(obj, (list, tuple)):
        return [to_plain(x) for x in obj]
    return obj


def _array(values: Any, name: str, ndim: tuple[int, ...]) -> np.ndarray:
    if values is None or isinstance(values, (str, bytes, dict)):
        raise BadInput(f"{name}: expected an array")
    try:
        arr = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"{name}: values are not numeric") from exc
    if arr.ndim not in ndim:
        raise BadInput(f"{name}: expected {' or '.join(str(n) for n in ndim)} dimensions, got {arr.ndim}")
    return arr


def _names(values: Any, name: str, size: int) -> list[str]:
    if not isinstance(values, (list, tuple, np.ndarray)):
        raise BadInput(f"{name}: expected a list of names")
    out = [str(v) for v in values]
    if len(out) != size:
        raise BadInput(f"{name}: expected {size} names, got {len(out)}")
    return out


def _bad_numbers(**arrays: np.ndarray) -> tuple[str, ...]:
    reasons = []
    for key, arr in arrays.items():
        if arr.size == 0:
            reasons.append(f"{key} is empty")
        elif not np.isfinite(arr).all():
            reasons.append(f"{key} has NaN or infinite values")
    return tuple(reasons)


def predicted_attribution(weights: Any, loading_matrix: Any, factor_covariance: Any, idio_covariance: Any, asset_names: Any, factor_names: Any,
                          factor_families: Any = None, factor_mu: Any = None, idio_mu: Any = None, annualization_factor: float = 252.0,
                          compute_asset_breakdowns: bool = True) -> tuple[Any | None, tuple[str, ...]]:
    """The attribution container of a factor model, or None and the reasons. Shared by every predicted view."""
    w = _array(weights, "weights", (1,))
    loadings = _array(loading_matrix, "loading_matrix", (2,))
    fcov = _array(factor_covariance, "factor_covariance", (2,))
    icov = _array(idio_covariance, "idio_covariance", (2,))
    n_assets, n_factors = loadings.shape if loadings.ndim == 2 else (0, 0)
    assets = _names(asset_names, "asset_names", n_assets)
    factors = _names(factor_names, "factor_names", n_factors)
    families = None if factor_families is None else _names(factor_families, "factor_families", n_factors)
    mu_f = None if factor_mu is None else _array(factor_mu, "factor_mu", (1,))
    mu_i = None if idio_mu is None else _array(idio_mu, "idio_mu", (1,))
    if w.shape != (n_assets,) or fcov.shape != (n_factors, n_factors) or icov.shape != (n_assets, n_assets):
        raise BadInput("weights, loading_matrix, factor_covariance and idio_covariance have inconsistent shapes")
    bad = _bad_numbers(weights=w, loading_matrix=loadings, factor_covariance=fcov, idio_covariance=icov)
    if bad:
        return None, bad
    with quiet():
        try:
            out = _lib.attribution_lib().predicted_factor_attribution(
                weights=w, loading_matrix=loadings, factor_covariance=fcov, idio_covariance=icov, asset_names=assets, factor_names=factors,
                factor_families=families, factor_mu=mu_f, idio_mu=mu_i, annualization_factor=float(annualization_factor),
                compute_asset_breakdowns=compute_asset_breakdowns)
        except (ValueError, np.linalg.LinAlgError) as exc:
            return None, (f"attribution could not be computed: {exc}",)
    return out, ()


def _hash_parts(*arrays: Any) -> tuple:
    return tuple(np.asarray(a, dtype=float).round(12).tolist() for a in arrays if a is not None)


def _attribution_result(name: str, pieces: tuple[str, ...], method: str, container: Any, reasons: tuple[str, ...], params: dict, hash_parts: tuple,
                        view: Any = None) -> QuantResult:
    if container is None:
        return empty(name, "mixed", method, reasons, pieces, params)
    value = to_plain(container) if view is None else view(container)
    return finish(name, value, unit="mixed", method=method, series=None, pieces=pieces, params=params, warnings=reasons, hash_extra=hash_parts)


_PRED_IN = {"weights": {"type": "array", "required": True}, "loading_matrix": {"type": "matrix", "required": True},
            "factor_covariance": {"type": "matrix", "required": True}, "idio_covariance": {"type": "matrix", "required": True},
            "asset_names": {"type": "list", "required": True}, "factor_names": {"type": "list", "required": True},
            "factor_families": {"type": "list", "required": False, "default": None}, "factor_mu": {"type": "array", "required": False, "default": None},
            "idio_mu": {"type": "array", "required": False, "default": None}, "annualization_factor": {"type": "float", "required": False, "default": 252.0}}


# --------------------------------------------------------------------------------------------------------------
# SK-082: predicted attribution
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.attribution_predicted", module="metrics",
                   summary="Expected volatility and return attribution from a factor model: systematic, idiosyncratic and total, by factor, family and asset.",
                   inputs=_PRED_IN, piece_ids=("SK-082",))
def attribution_predicted(weights: Any, loading_matrix: Any, factor_covariance: Any, idio_covariance: Any, asset_names: Any, factor_names: Any,
                          factor_families: Any = None, factor_mu: Any = None, idio_mu: Any = None, annualization_factor: float = 252.0) -> QuantResult:
    container, reasons = predicted_attribution(weights, loading_matrix, factor_covariance, idio_covariance, asset_names, factor_names, factor_families,
                                               factor_mu, idio_mu, annualization_factor)
    return _attribution_result("metrics.attribution_predicted", ("SK-082",), "exposure-volatility-correlation decomposition of the factor model", container, reasons,
                               {"annualization_factor": annualization_factor}, _hash_parts(weights, loading_matrix, factor_covariance, idio_covariance, factor_mu, idio_mu))


# --------------------------------------------------------------------------------------------------------------
# SK-081: breakdown by asset, factor or family
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.attribution_breakdown", module="metrics",
                   summary="Attribution table by asset, by factor or by factor family (predicted, from a factor model).",
                   inputs={**_PRED_IN, "by": {"type": "str", "required": False, "default": "factor"}}, piece_ids=("SK-081",))
def attribution_breakdown(weights: Any, loading_matrix: Any, factor_covariance: Any, idio_covariance: Any, asset_names: Any, factor_names: Any,
                          factor_families: Any = None, factor_mu: Any = None, idio_mu: Any = None, annualization_factor: float = 252.0,
                          by: str = "factor") -> QuantResult:
    if by not in _BY:
        raise BadInput(f"by must be one of {sorted(_BY)}, got {by!r}")
    name, pieces, method = "metrics.attribution_breakdown", ("SK-081",), f"contribution of each {by}"
    if by == "family" and factor_families is None:
        raise BadInput("by='family' needs factor_families")
    container, reasons = predicted_attribution(weights, loading_matrix, factor_covariance, idio_covariance, asset_names, factor_names, factor_families,
                                               factor_mu, idio_mu, annualization_factor)
    return _attribution_result(name, pieces, method, container, reasons, {"by": by, "annualization_factor": annualization_factor},
                               _hash_parts(weights, loading_matrix, factor_covariance, idio_covariance, factor_mu, idio_mu),
                               view=lambda c: to_plain(getattr(c, _BY[by])))


# --------------------------------------------------------------------------------------------------------------
# SK-084: containers (Attribution, Component, AssetByFactorContribution)
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.attribution_components", module="metrics",
                   summary="One component of the attribution: systematic, idio, total, or the asset-by-factor contribution matrices.",
                   inputs={**_PRED_IN, "part": {"type": "str", "required": False, "default": "total"}}, piece_ids=("SK-084",))
def attribution_components(weights: Any, loading_matrix: Any, factor_covariance: Any, idio_covariance: Any, asset_names: Any, factor_names: Any,
                           factor_families: Any = None, factor_mu: Any = None, idio_mu: Any = None, annualization_factor: float = 252.0,
                           part: str = "total") -> QuantResult:
    allowed = ("systematic", "idio", "total", "asset_by_factor")
    if part not in allowed:
        raise BadInput(f"part must be one of {allowed}, got {part!r}")
    name, pieces, method = "metrics.attribution_components", ("SK-084",), f"component {part} of the attribution"
    container, reasons = predicted_attribution(weights, loading_matrix, factor_covariance, idio_covariance, asset_names, factor_names, factor_families,
                                               factor_mu, idio_mu, annualization_factor)
    attr = "asset_by_factor_contrib" if part == "asset_by_factor" else part
    return _attribution_result(name, pieces, method, container, reasons, {"part": part, "annualization_factor": annualization_factor},
                               _hash_parts(weights, loading_matrix, factor_covariance, idio_covariance, factor_mu, idio_mu),
                               view=lambda c: to_plain(getattr(c, attr)))


# --------------------------------------------------------------------------------------------------------------
# SK-083: realized attribution, full sample and rolling
# --------------------------------------------------------------------------------------------------------------
_REAL_IN = {"asset_names": {"type": "list", "required": True}, "factor_names": {"type": "list", "required": True},
            "weights": {"type": "array", "required": True}, "factor_returns": {"type": "matrix", "required": True},
            "portfolio_returns": {"type": "array", "required": True}, "exposures": {"type": "matrix", "required": True},
            "idio_returns": {"type": "matrix", "required": True}, "factor_families": {"type": "list", "required": False, "default": None},
            "exposure_lag": {"type": "int", "required": False, "default": 1}, "annualization_factor": {"type": "float", "required": False, "default": 252.0}}


def _realized_args(asset_names: Any, factor_names: Any, weights: Any, factor_returns: Any, portfolio_returns: Any, exposures: Any, idio_returns: Any,
                   factor_families: Any, exposure_lag: int, annualization_factor: float):
    if not isinstance(exposure_lag, int) or isinstance(exposure_lag, bool) or exposure_lag < 0:
        raise BadInput("exposure_lag must be a non-negative integer")
    fr = _array(factor_returns, "factor_returns", (2,))
    pr = _array(portfolio_returns, "portfolio_returns", (1,))
    ex = _array(exposures, "exposures", (2, 3))
    ir = _array(idio_returns, "idio_returns", (2,))
    w = _array(weights, "weights", (1, 2))
    n_factors = fr.shape[1]
    n_assets = ir.shape[1]
    assets = _names(asset_names, "asset_names", n_assets)
    factors = _names(factor_names, "factor_names", n_factors)
    families = None if factor_families is None else _names(factor_families, "factor_families", n_factors)
    if len(pr) != len(fr) or len(ir) != len(fr):
        raise BadInput("factor_returns, portfolio_returns and idio_returns must have the same number of observations")
    bad = _bad_numbers(factor_returns=fr, portfolio_returns=pr, idio_returns=ir, weights=w)
    kwargs = dict(asset_names=assets, factor_names=factors, factor_families=families, weights=w, factor_returns=fr, portfolio_returns=pr,
                  exposures=ex, exposure_lag=exposure_lag, idio_returns=ir, annualization_factor=float(annualization_factor))
    return kwargs, bad, (fr, pr, ex, ir, w)


@registry.register("metrics.attribution_realized", module="metrics",
                   summary="Realized volatility and return attribution from factor returns, exposures and idiosyncratic returns.",
                   inputs=_REAL_IN, piece_ids=("SK-083",))
def attribution_realized(asset_names: Any, factor_names: Any, weights: Any, factor_returns: Any, portfolio_returns: Any, exposures: Any,
                         idio_returns: Any, factor_families: Any = None, exposure_lag: int = 1, annualization_factor: float = 252.0) -> QuantResult:
    name, pieces, method = "metrics.attribution_realized", ("SK-083",), "covariance decomposition of realized portfolio variance into factors, idiosyncratic and unattributed"
    kwargs, bad, arrays = _realized_args(asset_names, factor_names, weights, factor_returns, portfolio_returns, exposures, idio_returns, factor_families,
                                         exposure_lag, annualization_factor)
    params = {"exposure_lag": exposure_lag, "annualization_factor": annualization_factor}
    if bad:
        return empty(name, "mixed", method, bad, pieces, params)
    with quiet():
        try:
            container = _lib.attribution_lib().realized_factor_attribution(**kwargs)
        except (ValueError, np.linalg.LinAlgError) as exc:
            return empty(name, "mixed", method, (f"attribution could not be computed: {exc}",), pieces, params)
    return finish(name, to_plain(container), unit="mixed", method=method, series=None, pieces=pieces, params=params, hash_extra=_hash_parts(*arrays))


@registry.register("metrics.attribution_rolling", module="metrics",
                   summary="Realized attribution over rolling windows: one row of contributions per window.",
                   inputs={**_REAL_IN, "window_size": {"type": "int", "required": False, "default": 60}, "step": {"type": "int", "required": False, "default": 21}},
                   piece_ids=("SK-083",))
def attribution_rolling(asset_names: Any, factor_names: Any, weights: Any, factor_returns: Any, portfolio_returns: Any, exposures: Any,
                        idio_returns: Any, factor_families: Any = None, exposure_lag: int = 1, annualization_factor: float = 252.0,
                        window_size: int = 60, step: int = 21) -> QuantResult:
    name, pieces, method = "metrics.attribution_rolling", ("SK-083",), "realized attribution over rolling windows"
    for key, val in (("window_size", window_size), ("step", step)):
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            raise BadInput(f"{key} must be a positive integer")
    kwargs, bad, arrays = _realized_args(asset_names, factor_names, weights, factor_returns, portfolio_returns, exposures, idio_returns, factor_families,
                                         exposure_lag, annualization_factor)
    params = {"exposure_lag": exposure_lag, "annualization_factor": annualization_factor, "window_size": window_size, "step": step}
    if bad:
        return empty(name, "mixed", method, bad, pieces, params)
    n_obs = len(arrays[0])
    if n_obs <= window_size + exposure_lag:
        return empty(name, "mixed", method, (f"need more than {window_size + exposure_lag} observations, got {n_obs}",), pieces, params)
    with quiet():
        try:
            container = _lib.attribution_lib().rolling_realized_factor_attribution(observations=np.arange(n_obs), window_size=window_size, step=step, **kwargs)
        except (ValueError, np.linalg.LinAlgError) as exc:
            return empty(name, "mixed", method, (f"attribution could not be computed: {exc}",), pieces, params)
    return finish(name, to_plain(container), unit="mixed", method=method, series=None, pieces=pieces, params=params, hash_extra=_hash_parts(*arrays))

"""Uncertainty sets (SK-077): the containers a robust optimizer reads.

`UncertaintySet` is a norm ball around an estimate, z = estimate + L u with the norm of u at most the radius; its support function
radius * ||L' e||_q is the worst case deviation of an exposure e. `CompactCovarianceUncertaintySet` stores the quadratic
covariance penalty radius * min_z ||C w - Q z||^2 in reduced form.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _common as c
from hokma.quant.portfolio import _lib
from hokma.quant.types import QuantResult

_RET = {"type": "dataframe", "required": True}
MIN_OBS = 3
TARGETS = ("mu", "covariance")
MAX_GEOMETRY_CELLS = 2_500


@dataclass(frozen=True)
class UncertaintySet:
    """Norm ball uncertainty set (z = estimate + geometry u, norm(u) <= radius)."""

    radius: float
    geometry: np.ndarray
    norm: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.radius) or self.radius < 0:
            raise c.Domain("radius must be a non-negative number")
        if self.norm < 1:
            raise c.Domain("norm must be at least 1")
        object.__setattr__(self, "_library", _lib.sk_uncertainty.UncertaintySet(radius=self.radius, geometry=self.geometry, norm=self.norm))

    @property
    def dual_norm(self) -> float:
        return float(self._library.dual_norm)  # type: ignore[attr-defined]

    def worst_case(self, exposure: np.ndarray) -> float:
        """Largest deviation of exposure . z from exposure . estimate over the set: radius * norm_q(geometry' e)."""
        q = self.dual_norm
        return float(self.radius * np.linalg.norm(self.geometry.T @ np.asarray(exposure, dtype=float), ord=q))


@dataclass(frozen=True)
class CompactCovarianceUncertaintySet:
    """Quadratic covariance penalty in reduced form: radius * C'(I - QQ')C (SK-077)."""

    radius: float
    metric_sqrt: np.ndarray
    basis: np.ndarray

    def __post_init__(self) -> None:
        if not math.isfinite(self.radius) or self.radius < 0:
            raise c.Domain("radius must be a non-negative number")
        if self.metric_sqrt.ndim != 1 or self.basis.ndim != 2 or self.basis.shape[0] != self.metric_sqrt.shape[0]:
            raise BadInput("metric_sqrt must be a vector and basis a matrix with one row per asset")
        if self.basis.shape[1] > self.basis.shape[0]:
            raise c.Domain("the basis has more columns than assets")
        if not np.allclose(self.basis.T @ self.basis, np.eye(self.basis.shape[1]), atol=1e-8):
            raise c.Domain("the basis must be orthonormal (Q'Q = I)")
        object.__setattr__(self, "_library", _lib.sk_uncertainty.CompactCovarianceUncertaintySet(
            radius=self.radius, metric_sqrt=self.metric_sqrt, basis=self.basis))

    def penalty(self, weights: np.ndarray) -> float:
        cw = self.metric_sqrt * np.asarray(weights, dtype=float)
        residual = cw - self.basis @ (self.basis.T @ cw)
        return float(self.radius * residual @ residual)


@registry.register("portfolio.uncertainty_set", module="portfolio",
                   summary="Empirical uncertainty set of the mean or the covariance of the returns: radius, norm, geometry and, for the mean, the worst case loss of given weights.",
                   inputs={"returns": _RET, "target": {"type": "str", "required": False, "default": "mu"},
                           "confidence_level": {"type": "float", "required": False, "default": 0.95},
                           "diagonal": {"type": "bool", "required": False, "default": True},
                           "weights": {"type": "dict", "required": False}},
                   piece_ids=("SK-077",))
def uncertainty_set(returns: Any, target: str = "mu", confidence_level: float = 0.95, diagonal: bool = True, weights: Any = None) -> QuantResult:
    name, pieces = "portfolio.uncertainty_set", ("SK-077",)
    params = {"target": target, "confidence_level": confidence_level, "diagonal": diagonal}

    def compute() -> Any:
        which = c.choice("target", target, TARGETS)
        level = c.number("confidence_level", confidence_level)
        if not 0.0 < level < 1.0:
            raise c.Domain(f"confidence_level must be strictly between 0 and 1, got {confidence_level}")
        panel, warns = c.usable_panel(returns, min_obs=MIN_OBS)
        if panel is None:
            raise c.Domain("; ".join(warns))
        cls = _lib.sk_uncertainty.EmpiricalMuUncertaintySet if which == "mu" else _lib.sk_uncertainty.EmpiricalCovarianceUncertaintySet
        raw = _lib.quiet(cls(confidence_level=level, diagonal=bool(diagonal)).fit, panel).uncertainty_set_
        container = UncertaintySet(float(raw.radius), np.asarray(raw.geometry, dtype=float), float(raw.norm))
        geom = container.geometry
        out: dict[str, Any] = {"target": which, "radius": container.radius, "norm": container.norm, "dual_norm": container.dual_norm,
                               "geometry_shape": list(geom.shape), "n_observations": int(len(panel))}
        if geom.size <= MAX_GEOMETRY_CELLS and bool(diagonal):
            out["geometry_diagonal"] = c.jsonable(np.diag(geom) if geom.shape[0] == geom.shape[1] else geom)
        if weights is not None:
            if which != "mu":
                raise c.Domain("worst case weights apply to the mean set only")
            vec = c.weights_vector(weights, panel.columns)
            out["worst_case_return_loss"] = container.worst_case(vec)
        return out, warns

    return c.run(name, unit="return", method="UncertaintySet", params=params, piece_ids=pieces, compute=compute, inputs=(returns, params))


@registry.register("portfolio.compact_covariance_set", module="portfolio",
                   summary="Compact covariance uncertainty set (radius, metric square root, orthonormal basis) and its worst case variance penalty for given weights.",
                   inputs={"radius": {"type": "float", "required": True}, "metric_sqrt": {"type": "list", "required": True},
                           "basis": {"type": "list", "required": True}, "weights": {"type": "list", "required": False}},
                   piece_ids=("SK-077",))
def compact_covariance_set(radius: float, metric_sqrt: Any, basis: Any, weights: Any = None) -> QuantResult:
    name, pieces = "portfolio.compact_covariance_set", ("SK-077",)
    params = {"radius": radius}

    def compute() -> Any:
        if not isinstance(metric_sqrt, (list, tuple, np.ndarray)) or not isinstance(basis, (list, tuple, np.ndarray)):
            raise BadInput("metric_sqrt and basis must be lists")
        c_vec = np.asarray(metric_sqrt, dtype=float)
        q = np.asarray(basis, dtype=float)
        if c_vec.size == 0 or q.size == 0:
            raise c.Domain("metric_sqrt and basis must not be empty")
        if not (np.all(np.isfinite(c_vec)) and np.all(np.isfinite(q))):
            raise c.Domain("metric_sqrt and basis must be finite")
        if q.ndim == 1:
            q = q.reshape(-1, 1)
        container = CompactCovarianceUncertaintySet(c.number("radius", radius), c_vec, q)
        out: dict[str, Any] = {"radius": container.radius, "n_assets": int(c_vec.size), "rank": int(q.shape[1])}
        if weights is not None:
            w = np.asarray(weights, dtype=float).ravel()
            if w.size != c_vec.size or not np.all(np.isfinite(w)):
                raise c.Domain("weights must be finite and have one value per asset")
            out["variance_penalty"] = container.penalty(w)
        return out

    return c.run(name, unit="variance", method="CompactCovarianceUncertaintySet", params=params, piece_ids=pieces, compute=compute,
                 inputs=(radius, metric_sqrt, basis, weights))

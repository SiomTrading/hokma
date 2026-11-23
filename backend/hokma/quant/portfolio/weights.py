"""Baseline weights (SK-115): random weights, random weights from a Dirichlet distribution and inverse volatility weights.

A candidate portfolio must beat these. The random ones accept a seed and never touch the global random state of the process.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _common as c
from hokma.quant.portfolio import _lib
from hokma.quant.types import QuantResult

MAX_ASSETS = 5_000
_SEED = {"type": "int", "required": False}


def _seed(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or not 0 <= int(value) < 2**32:
        raise BadInput("seed must be a whole number in [0, 2**32)")
    return int(value)


def _n(value: Any) -> int:
    n = int(c.number("n_assets", value, low=1, integer=True))
    if n > MAX_ASSETS:
        raise BadInput(f"n_assets={n} is above the limit {MAX_ASSETS}")
    return n


@registry.register("portfolio.rand_weights", module="portfolio",
                   summary="Random weights that sum to one (uniform draw, then normalized), optionally with some weights set to zero. Seeded.",
                   inputs={"n_assets": {"type": "int", "required": True}, "zeros": {"type": "int", "required": False, "default": 0}, "seed": _SEED},
                   piece_ids=("SK-115",))
def rand_weights(n_assets: int, zeros: int = 0, seed: int | None = None) -> QuantResult:
    name, pieces = "portfolio.rand_weights", ("SK-115",)
    params = {"n_assets": n_assets, "zeros": zeros, "seed": seed}

    def compute() -> Any:
        n = _n(n_assets)
        z = int(c.number("zeros", zeros, low=0, integer=True))
        if z >= n:
            raise c.Domain(f"zeros={z} must be below n_assets={n}")
        return {"weights": c.jsonable(_lib.sk_stats.rand_weights(n, zeros=z, seed=_seed(seed)))}

    return c.run(name, unit="weights", method="rand_weights", params=params, piece_ids=pieces, compute=compute, inputs=(params,))


@registry.register("portfolio.rand_weights_dirichlet", module="portfolio",
                   summary="Random weights that sum to one drawn uniformly over the simplex (Dirichlet with all parameters 1). Seeded.",
                   inputs={"n_assets": {"type": "int", "required": True}, "seed": _SEED}, piece_ids=("SK-115",))
def rand_weights_dirichlet(n_assets: int, seed: int | None = None) -> QuantResult:
    name, pieces = "portfolio.rand_weights_dirichlet", ("SK-115",)
    params = {"n_assets": n_assets, "seed": seed}

    def compute() -> Any:
        n = _n(n_assets)
        s = _seed(seed)
        state = np.random.get_state()
        try:
            if s is not None:
                np.random.seed(s)
            weights = _lib.sk_stats.rand_weights_dirichlet(n)
        finally:
            np.random.set_state(state)
        return {"weights": c.jsonable(weights)}

    return c.run(name, unit="weights", method="rand_weights_dirichlet", params=params, piece_ids=pieces, compute=compute, inputs=(params,))


@registry.register("portfolio.inverse_volatility_weights", module="portfolio",
                   summary="Weights proportional to 1 / volatility from a covariance matrix (a DataFrame with the assets as rows and columns).",
                   inputs={"covariance": {"type": "dataframe", "required": True}}, piece_ids=("SK-115",))
def inverse_volatility_weights(covariance: Any) -> QuantResult:
    name, pieces = "portfolio.inverse_volatility_weights", ("SK-115",)

    def compute() -> Any:
        cov = c.as_panel(covariance, name="covariance")
        if cov.shape[0] == 0:
            raise c.Domain("covariance is empty")
        if cov.shape[0] != cov.shape[1]:
            raise BadInput(f"covariance must be square, got {cov.shape[0]} by {cov.shape[1]}")
        values = cov.to_numpy()
        if not np.all(np.isfinite(values)):
            raise c.Domain("covariance has NaN or infinite values")
        if np.any(np.diag(values) < 0):
            raise c.Domain("a variance on the diagonal is negative")
        if np.all(np.diag(values) == 0):
            raise c.Domain("every variance is zero")
        return {"weights": c.weights_to_dict(_lib.sk_stats.inverse_volatility_weights(values), cov.columns)}

    return c.run(name, unit="weights", method="inverse_volatility_weights", params={}, piece_ids=pieces, compute=compute, inputs=(covariance,))

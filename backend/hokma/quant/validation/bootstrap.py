"""Stationary bootstrap and optimal block length (SPEC-017 row SK-113)."""
from __future__ import annotations

from typing import Any

import numpy as np

from hokma.quant import helpers, registry
from hokma.quant.types import QuantResult
from hokma.quant.validation import _lib
from hokma.quant.validation._checks import clean_returns

stationary_bootstrap = _lib.stationary_bootstrap
optimal_block_size = _lib.optimal_block_size


def block_size_of(values: np.ndarray) -> float:
    """Optimal expected block length of a one dimensional series."""
    return float(_lib.optimal_block_size(np.asarray(values, dtype=float)))


@registry.register(
    "validation.optimal_block_size", module="validation",
    summary="Optimal expected block length for a dependent bootstrap of a return series.",
    inputs={"returns": {"type": "series", "required": True}},
    piece_ids=("SK-113",),
)
def optimal_block_length(returns: Any) -> QuantResult:
    name = "validation.optimal_block_size"
    series, warnings, degenerate = clean_returns(returns, min_obs=20)
    if degenerate:
        return QuantResult.empty(name, "observations", "automatic_block_length", warnings[-1])
    return QuantResult(name=name, value=block_size_of(series.to_numpy()), unit="observations", method="automatic_block_length",
                       params={"n": int(len(series))}, inputs_hash=helpers.hash_inputs(series),
                       as_of=helpers.last_as_of(series), warnings=warnings, piece_ids=("SK-113",))


@registry.register(
    "validation.bootstrap_sharpe", module="validation",
    summary="Distribution of the Sharpe ratio under a stationary bootstrap with optimal block length (fixed seed).",
    inputs={"returns": {"type": "series", "required": True}, "n_samples": {"type": "int", "required": False, "default": 1000},
            "block_size": {"type": "float", "required": False, "default": None}, "seed": {"type": "int", "required": False, "default": 0},
            "periods_per_year": {"type": "int", "required": False, "default": 252}},
    piece_ids=("SK-113",),
)
def bootstrap_sharpe(returns: Any, n_samples: int = 1000, block_size: float | None = None, seed: int = 0,
                     periods_per_year: int = 252) -> QuantResult:
    name = "validation.bootstrap_sharpe"
    series, warnings, degenerate = clean_returns(returns, min_obs=20)
    if degenerate:
        return QuantResult.empty(name, "ratio", "stationary_bootstrap", warnings[-1])
    from hokma.quant import limits

    limits.check_param("n_samples", n_samples)
    limits.check_product("n_samples x length", n_samples, len(returns) if hasattr(returns, "__len__") else 1)
    if n_samples < 2:
        return QuantResult.empty(name, "ratio", "stationary_bootstrap", "n_samples must be at least 2")
    block = float(block_size) if block_size is not None else block_size_of(series.to_numpy())
    samples = _lib.stationary_bootstrap(series.to_numpy().reshape(-1, 1), int(n_samples), block, int(seed))[:, :, 0]
    stds = samples.std(axis=1, ddof=1)
    valid = stds > 0
    sharpes = samples.mean(axis=1)[valid] / stds[valid] * np.sqrt(periods_per_year)
    if len(sharpes) == 0:
        return QuantResult.empty(name, "ratio", "stationary_bootstrap", "every bootstrap sample is constant")
    value = {
        "mean": float(sharpes.mean()), "std": float(sharpes.std(ddof=1)) if len(sharpes) > 1 else 0.0,
        "p05": float(np.quantile(sharpes, 0.05)), "p50": float(np.quantile(sharpes, 0.50)), "p95": float(np.quantile(sharpes, 0.95)),
        "prob_positive": float((sharpes > 0).mean()), "block_size": block, "n_samples": int(len(sharpes)),
    }
    return QuantResult(name=name, value=value, unit="ratio", method="stationary_bootstrap",
                       params={"seed": int(seed), "block_size": block, "periods_per_year": int(periods_per_year), "n_samples": int(n_samples)},
                       inputs_hash=helpers.hash_inputs(series, int(n_samples), block, int(seed), int(periods_per_year)),
                       as_of=helpers.last_as_of(series), warnings=warnings, piece_ids=("SK-113",))

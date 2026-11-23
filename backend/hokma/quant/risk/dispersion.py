"""Alternative dispersion measures and concentration of a portfolio (risk module)."""
from __future__ import annotations

from typing import Any

import numpy as np

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.risk import _common as c
from hokma.quant.risk import _lib
from hokma.quant.types import QuantResult


@registry.register("risk.gini_mean_difference", module="risk",
                   summary="Gini mean difference of the returns: mean absolute gap between pairs of observations (alternative dispersion for optimization).",
                   inputs={"returns": {"type": "series", "required": True}}, piece_ids=("SK-031",))
def gini_mean_difference(returns: Any) -> QuantResult:
    return c.measure("risk.gini_mean_difference", returns, unit="return", method="gini_mean_difference", params={},
                     piece_ids=("SK-031",), compute=lambda s: _lib.sk_measures.gini_mean_difference(s.to_numpy()))


@registry.register("risk.owa_gmd_weights", module="risk",
                   summary="Ordered weighted average weights that turn the sorted returns into the Gini mean difference.",
                   inputs={"n_observations": {"type": "int", "required": True}}, piece_ids=("SK-031",))
def owa_gmd_weights(n_observations: int) -> QuantResult:
    name, unit, method, pieces = "risk.owa_gmd_weights", "weight", "owa_gmd", ("SK-031",)
    if isinstance(n_observations, bool) or not isinstance(n_observations, (int, np.integer)):
        raise BadInput(f"n_observations must be an integer, got {type(n_observations).__name__}")
    from hokma.quant import limits

    limits.check_param("n_observations", n_observations)
    params = {"n_observations": int(n_observations)}
    if n_observations < 2:
        return c.empty(name, unit, method, f"need at least 2 observations, got {n_observations}", params=params, piece_ids=pieces)
    weights = _lib.sk_measures.owa_gmd_weights(int(n_observations))
    return c.build(name, {"weights": weights}, unit=unit, method=method, params=params, piece_ids=pieces, inputs=(params,))


@registry.register("risk.effective_number_assets", module="risk",
                   summary="Effective number of assets: inverse of the Herfindahl index of the weights (concentration of a portfolio or of a set of desks).",
                   inputs={"weights": {"type": "dict", "required": True}}, piece_ids=("SK-032",))
def effective_number_assets(weights: Any) -> QuantResult:
    name, unit, method, pieces = "risk.effective_number_assets", "count", "inverse_herfindahl", ("SK-032",)
    if isinstance(weights, dict):
        values = list(weights.values())
    elif isinstance(weights, (list, tuple, np.ndarray)) or hasattr(weights, "to_numpy"):
        values = list(np.asarray(weights, dtype=object).ravel())
    else:
        raise BadInput(f"weights: expected a dict, list or array, got {type(weights).__name__}")
    try:
        w = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise BadInput("weights: values are not numeric") from exc
    finite = w[np.isfinite(w)]
    warns: tuple[str, ...] = ()
    if len(finite) != len(w):
        warns = (f"dropped {len(w) - len(finite)} non finite weight(s)",)
    total = float(np.abs(finite).sum()) if len(finite) else 0.0
    if total == 0.0:
        return c.empty(name, unit, method, warns + ("weights are empty or all zero",), piece_ids=pieces)
    normalized = finite / total
    return c.build(name, float(_lib.sk_measures.effective_number_assets(normalized)), unit=unit, method=method,
                   params={"normalized": True}, piece_ids=pieces, inputs=(list(normalized),), warnings=warns)

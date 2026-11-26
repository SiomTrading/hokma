"""Aggregate risk overlay (P-10): reduce every position when the risk of the portfolio is above the target.

The risk of the portfolio is the square root of w'Cw for the positions w and the covariance C, in the units of C (so positions and
covariance must use the same units: weights with the covariance of returns, or contracts with the covariance of the value of a contract).
When it is above `target_risk` every position is multiplied by the same factor, target over risk, which brings the risk back to the
target and keeps the proportions. The factor is never above 1: the overlay reduces, it never adds exposure.
"""
from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _common as c
from hokma.quant.types import QuantResult

MAX_POSITIONS = 5_000


@registry.register("portfolio.risk_overlay", module="portfolio",
                   summary="Scale every position by min(1, target_risk / portfolio risk) when the risk sqrt(w'Cw) is above the target. Never scales above 1.",
                   inputs={"positions": {"type": "dict", "required": True}, "covariance": {"type": "dataframe", "required": True},
                           "target_risk": {"type": "float", "required": True}},
                   piece_ids=("P-10",))
def risk_overlay(positions: Any, covariance: Any, target_risk: float) -> QuantResult:
    name, pieces = "portfolio.risk_overlay", ("P-10",)
    params = {"target_risk": target_risk}

    def compute() -> Any:
        target = c.number("target_risk", target_risk, low=0.0, low_open=True)
        if not isinstance(positions, Mapping) or not positions:
            raise BadInput("positions must be a non-empty dict asset -> position")
        if len(positions) > MAX_POSITIONS:
            raise BadInput(f"more than {MAX_POSITIONS} positions")
        cov = c.as_panel(covariance, name="covariance")
        if cov.shape[0] != cov.shape[1]:
            raise BadInput(f"covariance must be square, got {cov.shape[0]} by {cov.shape[1]}")
        names = [str(col) for col in cov.columns]
        if [str(i) for i in cov.index] != names:
            raise BadInput("covariance rows and columns must be the same assets in the same order")
        unknown = [k for k in positions if str(k) not in names]
        if unknown:
            raise BadInput(f"positions: unknown asset(s) {unknown}")
        by_name = {str(k): v for k, v in positions.items()}
        w = np.array([c.number(f"positions[{n}]", by_name.get(n, 0.0)) for n in names])
        matrix = cov.to_numpy()
        if not np.all(np.isfinite(matrix)):
            raise c.Domain("covariance has NaN or infinite values")
        variance = float(w @ matrix @ w)
        if variance < -1e-12 * float(np.abs(matrix).max()) * float(w @ w):
            raise c.Domain("the covariance is not positive semi-definite for these positions")
        risk = float(np.sqrt(max(variance, 0.0)))
        scale = 1.0 if risk <= target else target / risk
        return {"scale": scale, "risk_before": risk, "risk_after": risk * scale, "target_risk": target, "reduced": scale < 1.0,
                "positions": {n: float(x * scale) for n, x in zip(names, w)}}

    return c.run(name, unit="positions", method="scale = min(1, target_risk / sqrt(w'Cw))", params=params, piece_ids=pieces, compute=compute,
                 inputs=(positions, covariance, params))

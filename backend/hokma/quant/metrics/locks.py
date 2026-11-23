"""Locks of the metrics module: library functions that are blocked, and the rule on probabilities (SPEC-017).

Two defects of the metrics library are closed here:
* its probabilistic Sharpe family subtracts 3 from a kurtosis that is already an excess value, which overstates the
  probability (C2, CC-02);
* with `annualize=True` it multiplies a probability by the square root of 252 (C3, CC-03).

The correct probabilistic Sharpe ratio is `validation.psr`. `metrics.psr` forwards to it unchanged, so the
metrics module and the validation module can never disagree.
"""
from __future__ import annotations

import importlib
from typing import Any

from hokma.quant import registry
from hokma.quant.errors import BadInput, UnknownFunction
from hokma.quant.metrics.returns import check_series
from hokma.quant.types import QuantResult

PSR_FUNCTION = "validation.psr"
NOT_ANNUALIZED = "probabilities are not annualized"

# Library functions that stay unreachable; every one names the function that takes its place.
BLOCKED_PROBABILISTIC = (
    "probabilistic_ratio",
    "probabilistic_sharpe_ratio",
    "probabilistic_sortino_ratio",
    "probabilistic_adjusted_sortino_ratio",
)

for _name in BLOCKED_PROBABILISTIC:
    registry.register_blocked(
        f"metrics.{_name}", module="metrics", replacement=PSR_FUNCTION, piece_ids=("QS-013a", "CC-02", "CC-03"),
        summary=f"Blocked: {_name} uses a wrong kurtosis convention and can multiply a probability by sqrt(252).")


def reject_annualize(**kwargs: Any) -> None:
    """Raise ValueError when a probability function is asked to annualize: a probability has no time scale."""
    if kwargs.get("annualize"):
        raise ValueError(NOT_ANNUALIZED)


def probability_functions() -> list[str]:
    """Names of the registered functions that return a probability (their summary starts with 'Probability')."""
    return [s.name for s in registry.list(include_blocked=True) if s.summary.lower().startswith("probability")]


def _validation_ready() -> None:
    try:
        importlib.import_module("hokma.quant.validation")
    except ModuleNotFoundError as exc:      # a validation module that is not built yet is simply absent
        if exc.name not in ("hokma.quant.validation", "hokma.quant"):
            raise


def psr(returns: Any, *, sr_benchmark: float = 0.0, **kwargs: Any) -> QuantResult:
    """Probability that the true Sharpe ratio exceeds `sr_benchmark` (per-period units), from `validation.psr`.

    The result of `validation.psr` is returned as it is. `annualize=True` is refused with a ValueError.
    """
    reject_annualize(**kwargs)
    kwargs.pop("annualize", None)                 # annualize=False is the only accepted value, and it changes nothing
    if kwargs:
        raise BadInput(f"metrics.psr: unknown parameter(s) {sorted(kwargs)}")
    series, why = check_series(returns, "returns", min_obs=3)
    if series is None:
        return QuantResult.empty("metrics.psr", "prob", "forwards to validation.psr", why or "invalid input",
                                 piece_ids=("QS-013a", "CC-02", "CC-03"))
    _validation_ready()
    try:
        return registry.call(PSR_FUNCTION, returns=returns, sr_benchmark=sr_benchmark)
    except UnknownFunction:
        return QuantResult.empty("metrics.psr", "prob", "forwards to validation.psr",
                                 f"{PSR_FUNCTION} is not registered", piece_ids=("QS-013a", "CC-02", "CC-03"))


registry.register(
    "metrics.psr", module="metrics", piece_ids=("QS-013a", "CC-02", "CC-03"),
    summary="Probability that the true Sharpe ratio exceeds a benchmark Sharpe ratio; forwards to validation.psr.",
    inputs={"returns": {"type": "series", "required": True}, "sr_benchmark": {"type": "float", "required": False, "default": 0.0}},
)(psr)

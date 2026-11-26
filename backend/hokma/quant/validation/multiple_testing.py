"""Multiple testing corrections: Bonferroni, Holm and Benjamini-Hochberg adjusted p-values (SPEC-020: ECT-036, ECT-055).

Testing many ideas or many pairs at one level makes some of them look significant by chance. Each function takes the raw p-values
of every test that was run (failed ideas included) and returns the adjusted p-values and which tests survive at `alpha`:

  * bonferroni: p * m, capped at 1. Controls the chance of any false discovery. The most conservative.
  * holm: step down version of the same control. It never rejects less than bonferroni.
  * bh: step up control of the false discovery rate (the expected share of false ones among the survivors). The least conservative.

The adjusted p-values keep the order of the input. No haircut factor on a Sharpe ratio lives here: a correction acts on p-values only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from hokma.quant import helpers, registry
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult

METHODS = ("bonferroni", "holm", "bh")
MAX_TESTS = 1_000_000
PIECES = ("ECT-036", "ECT-055")


@dataclass(frozen=True)
class Correction:
    """Adjusted p-values (input order) and the tests that survive at `alpha`."""

    method: str
    alpha: float
    adjusted: tuple[float, ...]
    reject: tuple[bool, ...]

    @property
    def n_rejected(self) -> int:
        return sum(self.reject)


def _p_values(values: Any) -> np.ndarray:
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError):
        raise BadInput("p_values must be numbers") from None
    if array.ndim != 1 or array.size == 0:
        raise BadInput("p_values must be a non-empty list of numbers")
    if array.size > MAX_TESTS:
        raise BadInput(f"p_values has {array.size} values; the limit is {MAX_TESTS}")
    if not np.all(np.isfinite(array)) or np.any(array < 0.0) or np.any(array > 1.0):
        raise BadInput("every p-value must be a finite number between 0 and 1")
    return array


def _alpha(alpha: Any) -> float:
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0.0 < float(alpha) < 1.0:
        raise BadInput(f"alpha must be a number between 0 and 1, got {alpha!r}")
    return float(alpha)


def _result(method: str, alpha: float, adjusted: np.ndarray) -> Correction:
    adjusted = np.minimum(adjusted, 1.0)
    return Correction(method, alpha, tuple(float(v) for v in adjusted), tuple(bool(v) for v in adjusted <= alpha))


def bonferroni(p_values: Any, alpha: float = 0.05) -> Correction:
    """Each p-value times the number of tests."""
    p = _p_values(p_values)
    return _result("bonferroni", _alpha(alpha), p * p.size)


def holm(p_values: Any, alpha: float = 0.05) -> Correction:
    """Sorted ascending, the i-th smallest (from 0) is multiplied by m - i; a running maximum keeps the order."""
    p = _p_values(p_values)
    m = p.size
    order = np.argsort(p, kind="stable")
    stepped = np.maximum.accumulate(np.minimum(p[order] * (m - np.arange(m)), 1.0))
    adjusted = np.empty(m)
    adjusted[order] = stepped
    return _result("holm", _alpha(alpha), adjusted)


def benjamini_hochberg(p_values: Any, alpha: float = 0.05) -> Correction:
    """Sorted ascending, the i-th smallest (from 1) is multiplied by m / i; a running minimum from the largest keeps the order."""
    p = _p_values(p_values)
    m = p.size
    order = np.argsort(p, kind="stable")
    scaled = p[order] * m / np.arange(1, m + 1)
    stepped = np.minimum.accumulate(scaled[::-1])[::-1]
    adjusted = np.empty(m)
    adjusted[order] = np.minimum(stepped, 1.0)
    return _result("bh", _alpha(alpha), adjusted)


_METHOD_FUNCTIONS = {"bonferroni": bonferroni, "holm": holm, "bh": benjamini_hochberg}


def correct(p_values: Any, method: str = "bh", alpha: float = 0.05) -> Correction:
    """One of METHODS by name."""
    if method not in _METHOD_FUNCTIONS:
        raise BadInput(f"method is one of {', '.join(METHODS)}, got {method!r}")
    return _METHOD_FUNCTIONS[method](p_values, alpha)


@registry.register(
    "validation.multiple_testing", module="validation",
    summary="Adjusted p-values and survivors after a multiple testing correction (bonferroni, holm or bh) over every test that was run.",
    inputs={"p_values": {"type": "series", "required": True}, "method": {"type": "str", "required": False, "default": "bh"},
            "alpha": {"type": "float", "required": False, "default": 0.05}},
    piece_ids=PIECES,
)
def multiple_testing(p_values: Any, method: str = "bh", alpha: float = 0.05) -> QuantResult:
    name = "validation.multiple_testing"
    series = helpers.as_series(p_values, name="p_values")
    if method not in METHODS:
        raise BadInput(f"method is one of {', '.join(METHODS)}, got {method!r}")
    level = _alpha(alpha)
    finite = helpers.clean(series)
    if series.empty or len(finite) < len(series):
        return QuantResult.empty(name, "prob", method, "p_values is empty or holds a NaN or an infinite value")
    if (series < 0).any() or (series > 1).any():
        return QuantResult.empty(name, "prob", method, "every p-value must be between 0 and 1")
    result = correct(series.to_numpy(), method, level)
    return QuantResult(name=name, value={"adjusted": list(result.adjusted), "reject": list(result.reject), "n_rejected": result.n_rejected},
                       unit="prob", method=method, params={"alpha": level, "n_tests": len(series)},
                       inputs_hash=helpers.hash_inputs(series, method, level), piece_ids=PIECES)

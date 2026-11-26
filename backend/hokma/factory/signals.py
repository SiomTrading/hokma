"""Signal utilities over closed bars (SPEC-020: B-36, AH-38). Pure functions on numpy arrays or pandas series.

Every function returns numpy arrays of the same length as its input. A NaN never produces a cross.
"""
from __future__ import annotations

from typing import Any, Literal

import numpy as np

from hokma.quant.errors import BadInput
from hokma.quant.limits import check_inputs


def _array(values: Any, name: str) -> np.ndarray:
    if np.isscalar(values):
        return np.asarray(values, dtype=float)
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError):
        raise BadInput(f"{name}: values are not numeric") from None
    if array.ndim != 1:
        raise BadInput(f"{name}: expected one dimension, got {array.ndim}")
    check_inputs({name: array})
    return array


def _pair(a: Any, b: Any) -> tuple[np.ndarray, np.ndarray]:
    x, y = _array(a, "a"), _array(b, "b")
    if x.ndim == 0 and y.ndim == 0:
        raise BadInput("at least one of a and b must be a series")
    n = max(x.shape[0] if x.ndim else 0, y.shape[0] if y.ndim else 0)
    if (x.ndim and x.shape[0] != n) or (y.ndim and y.shape[0] != n):
        raise BadInput(f"a and b must have the same length, got {x.shape} and {y.shape}")
    return np.broadcast_to(x, (n,)), np.broadcast_to(y, (n,))


def crossover(a: Any, b: Any) -> np.ndarray:
    """True on the bar where a rises through b: a was at or below b on the bar before and is above it now."""
    x, y = _pair(a, b)
    out = np.zeros(len(x), dtype=bool)
    out[1:] = (x[:-1] <= y[:-1]) & (x[1:] > y[1:])
    return out


def crossunder(a: Any, b: Any) -> np.ndarray:
    """True on the bar where a falls through b: a was at or above b on the bar before and is below it now."""
    x, y = _pair(a, b)
    out = np.zeros(len(x), dtype=bool)
    out[1:] = (x[:-1] >= y[:-1]) & (x[1:] < y[1:])
    return out


def cross(a: Any, b: Any) -> np.ndarray:
    """True on every bar where a crosses b in either direction."""
    return crossover(a, b) | crossunder(a, b)


def barssince(condition: Any) -> np.ndarray:
    """Bars since the condition was last true: 0 on that bar, -1 before it was ever true."""
    flags = np.asarray(condition, dtype=bool)
    if flags.ndim != 1:
        raise BadInput("condition: expected one dimension")
    check_inputs({"condition": flags})
    index = np.where(flags, np.arange(len(flags)), -1)
    last = np.maximum.accumulate(index) if len(flags) else index
    return np.where(last >= 0, np.arange(len(flags)) - last, -1).astype(np.int64)


def quantile(values: Any, q: float) -> float:
    """The q quantile (0 to 1) of the finite values, interpolated linearly between the two nearest ranks."""
    if isinstance(q, bool) or not isinstance(q, (int, float)) or not 0.0 <= float(q) <= 1.0:
        raise BadInput(f"q must be between 0 and 1, got {q!r}")
    data = _array(values, "values")
    data = data[np.isfinite(data)] if data.ndim else data.reshape(1)
    if data.size == 0:
        raise BadInput("values has no finite number")
    return float(np.quantile(data, float(q)))


class EdgeTrigger:
    """Opens once per signal episode: it opens when the wish changes from flat to a direction (AH-38).

    `on_flip` says what happens when the wish goes from long to short, or back, without passing through flat:
    "ignore" (the default) waits for flat, "reverse" opens the other side. The alpha never overlaps its own positions.
    """

    def __init__(self, on_flip: Literal["ignore", "reverse"] = "ignore") -> None:
        if on_flip not in ("ignore", "reverse"):
            raise BadInput(f"on_flip is ignore or reverse, got {on_flip!r}")
        self.on_flip = on_flip
        self.held = 0

    def update(self, wish: int) -> int:
        """The direction to open on this bar (1, -1) or 0 for nothing."""
        if wish not in (-1, 0, 1) or isinstance(wish, bool):
            raise BadInput(f"the wish is -1, 0 or 1, got {wish!r}")
        if wish == 0:
            self.held = 0
            return 0
        if self.held == 0 or (wish != self.held and self.on_flip == "reverse"):
            self.held = wish
            return wish
        return 0


def edge_trigger(wish: Any, *, on_flip: Literal["ignore", "reverse"] = "ignore") -> np.ndarray:
    """The directions to open (1, -1, 0) over a series of wishes (-1, 0, 1)."""
    values = _array(wish, "wish")
    if not np.all(np.isin(values, (-1.0, 0.0, 1.0))):
        raise BadInput("wish values are -1, 0 or 1")
    trigger = EdgeTrigger(on_flip)
    return np.array([trigger.update(int(v)) for v in values], dtype=np.int64)

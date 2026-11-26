"""Labels for models without leakage (SPEC-020: V-28).

A label looks at the future on purpose, so it is a target and never a feature. It is measured from the open of the next bar (the
price a decision at the close can really get), and the training rows whose label window touches the test window are purged.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from hokma.quant.errors import BadInput
from hokma.quant.limits import as_int, check_inputs, check_param


class LeakError(ValueError):
    """A label was used as an input, or a training row overlaps the test window. `code` is stable: label_leak."""

    code = "label_leak"


@dataclass(frozen=True)
class Label:
    """A target. It cannot be turned into an array by accident: read it with `target()`."""

    name: str
    horizon: int                   # bars between the entry open (t+1) and the exit open (t+1+horizon)
    _values: np.ndarray

    def target(self) -> np.ndarray:
        """A copy of the label values, for use as y."""
        return np.array(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __array__(self, dtype: Any = None, copy: Any = None) -> np.ndarray:
        raise LeakError(f"label {self.name!r} is a target, not an input: use target() for y and keep it out of the features")


def _open_array(open_: Any) -> np.ndarray:
    try:
        array = np.asarray(open_, dtype=float)
    except (TypeError, ValueError):
        raise BadInput("open_: values are not numeric") from None
    if array.ndim != 1:
        raise BadInput("open_: expected one dimension")
    check_inputs({"open_": array})
    return array


def _horizon(value: Any) -> int:
    h = as_int("horizon", value)
    if h < 1:
        raise BadInput(f"horizon is at least 1 bar, got {h}")
    check_param("horizon", h)
    return h


def forward_return(open_: Any, horizon: int, *, name: str = "fwd_return") -> Label:
    """label[t] = open[t+1+horizon] / open[t+1] - 1. NaN where the window runs past the last bar."""
    h = _horizon(horizon)
    prices = _open_array(open_)
    out = np.full(len(prices), np.nan)
    last = len(prices) - 1 - h
    if last >= 1:
        entry = prices[1:last + 1]
        out[:last] = prices[1 + h:1 + h + last] / entry - 1.0
    out.flags.writeable = False
    return Label(name, h, out)


def direction_label(open_: Any, horizon: int, *, dead_zone: float = 0.0, name: str = "direction") -> Label:
    """The sign of the forward return (1, -1) with 0 inside `dead_zone`. NaN where the return is NaN."""
    if not np.isfinite(dead_zone) or dead_zone < 0:
        raise BadInput(f"dead_zone is zero or more, got {dead_zone!r}")
    base = forward_return(open_, horizon).target()
    out = np.where(np.isnan(base), np.nan, np.where(base > dead_zone, 1.0, np.where(base < -dead_zone, -1.0, 0.0)))
    out.flags.writeable = False
    return Label(name, _horizon(horizon), out)


def check_features(features: Mapping[str, Any], labels: Mapping[str, Label] | None = None) -> None:
    """LeakError when a feature is a label, or has the name of one."""
    names = set(labels or ())
    for name, value in features.items():
        if isinstance(value, Label) or name in names:
            raise LeakError(f"feature {name!r} is a label: a label uses future prices and is never an input")


def split_purged(label: Label, test_start: int, test_end: int, *, embargo: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Train and test row indexes for the test window [test_start, test_end).

    A row t uses prices from bar t to bar t+1+horizon. A training row is dropped when that span touches the span of any test
    row (purge) or starts within `embargo` bars after it. Rows with no label (NaN) are never used.
    """
    n = len(label)
    start, end, gap = as_int("test_start", test_start), as_int("test_end", test_end), as_int("embargo", embargo)
    if not 0 <= start < end <= n:
        raise BadInput(f"the test window must satisfy 0 <= test_start < test_end <= {n}, got {start} and {end}")
    if gap < 0:
        raise BadInput(f"embargo is zero or more bars, got {gap}")
    h = label.horizon
    span_start, span_end = start, end - 1 + 1 + h + gap
    rows = np.arange(n)
    touches = (rows <= span_end) & (rows + 1 + h >= span_start)
    usable = ~np.isnan(label.target())
    train = rows[usable & ~touches]
    test = rows[start:end][usable[start:end]]
    if np.intersect1d(train, test).size:
        raise LeakError("a training row overlaps the test window")
    return train, test

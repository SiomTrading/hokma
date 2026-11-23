"""Cross-sectional correlation (SK-116): the information coefficient between a forecast and what happened, and the cross-sectional rank.

Rows are dates and columns are assets. The correlation is taken across the assets of each date (axis "assets") or across the dates of
each asset (axis "time"). Non finite values are left out; fewer than `min_count` pairs give no value.
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

AXES = ("assets", "time")
_AB = {"type": "dataframe", "required": True}
_COMMON = {"axis": {"type": "str", "required": False, "default": "assets"}, "min_count": {"type": "int", "required": False, "default": 3}}


def _frame(value: Any, label: str) -> pd.DataFrame:
    """A numeric 2-D frame; a Series or a list is one cross-section (one row)."""
    if isinstance(value, pd.DataFrame):
        frame = value
    elif isinstance(value, pd.Series):
        frame = value.to_frame().T
    elif isinstance(value, (list, tuple, np.ndarray)):
        arr = np.asarray(value, dtype=float)
        frame = pd.DataFrame(arr.reshape(1, -1) if arr.ndim == 1 else arr)
    else:
        raise BadInput(f"{label}: expected a DataFrame, Series or list, got {type(value).__name__}")
    try:
        return frame.astype(float)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"{label}: values are not numeric") from exc


def _pair(a: Any, b: Any) -> tuple[pd.DataFrame, pd.DataFrame]:
    fa, fb = _frame(a, "a"), _frame(b, "b")
    if fa.shape != fb.shape:
        raise BadInput(f"a and b must have the same shape, got {fa.shape} and {fb.shape}")
    return fa, fb


def _axis(axis: str) -> int:
    return 1 if c.choice("axis", axis, AXES) == "assets" else 0


def _summary(values: np.ndarray, labels: list[Any]) -> dict[str, Any]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        raise c.Domain("no cross-section has enough finite pairs (min_count)")
    return {"ic": c.jsonable(values), "labels": [str(x) for x in labels], "mean": float(finite.mean()), "n_valid": int(finite.size),
            "std": float(finite.std(ddof=1)) if finite.size > 1 else None}


def _correlation(name: str, piece: str, method: str, a: Any, b: Any, axis: str, min_count: int, weights: Any, use: Any) -> QuantResult:
    params = {"axis": axis, "min_count": min_count}

    def compute() -> Any:
        ax = _axis(axis)
        mc = int(c.number("min_count", min_count, low=1, integer=True))
        fa, fb = _pair(a, b)
        if fa.size == 0:
            raise c.Domain("a and b are empty")
        w = None
        if weights is not None:
            w = np.asarray(weights, dtype=float).ravel()
            expected = fa.shape[1] if ax == 1 else fa.shape[0]
            if w.size != expected:
                raise BadInput(f"weights needs {expected} value(s), got {w.size}")
        values = np.atleast_1d(np.asarray(use(fa.to_numpy(), fb.to_numpy(), ax, mc, w), dtype=float))
        labels = list(fa.index) if ax == 1 else list(fa.columns)
        return _summary(values, labels)

    return c.run(name, unit="correlation", method=method, params=params, piece_ids=(piece,), compute=compute, inputs=(a, b, params))


@registry.register("portfolio.cs_pearson_correlation", module="portfolio",
                   summary="Cross-sectional Pearson correlation (information coefficient) between a and b, per date; optional weights per asset.",
                   inputs={"a": _AB, "b": _AB, "weights": {"type": "list", "required": False}, **_COMMON}, piece_ids=("SK-116",))
def cs_pearson_correlation(a: Any, b: Any, weights: Any = None, axis: str = "assets", min_count: int = 3) -> QuantResult:
    return _correlation("portfolio.cs_pearson_correlation", "SK-116", "cs_pearson_correlation", a, b, axis, min_count, weights,
                        lambda x, y, ax, mc, w: _lib.sk_stats.cs_pearson_correlation(x, y, weights=w, axis=ax, min_count=mc))


@registry.register("portfolio.cs_spearman_correlation", module="portfolio",
                   summary="Cross-sectional Spearman rank correlation (rank information coefficient) between a and b, per date.",
                   inputs={"a": _AB, "b": _AB, **_COMMON}, piece_ids=("SK-116",))
def cs_spearman_correlation(a: Any, b: Any, axis: str = "assets", min_count: int = 3) -> QuantResult:
    return _correlation("portfolio.cs_spearman_correlation", "SK-116", "cs_spearman_correlation", a, b, axis, min_count, None,
                        lambda x, y, ax, mc, w: _lib.sk_stats.cs_spearman_correlation(x, y, axis=ax, min_count=mc))


@registry.register("portfolio.cs_rank", module="portfolio",
                   summary="Cross-sectional rank: 1-based ranks per date (ties get the average rank); non finite values stay empty.",
                   inputs={"a": _AB, "axis": _COMMON["axis"]}, piece_ids=("SK-116",))
def cs_rank(a: Any, axis: str = "assets") -> QuantResult:
    name, pieces = "portfolio.cs_rank", ("SK-116",)
    params = {"axis": axis}

    def compute() -> Any:
        ax = _axis(axis)
        frame = _frame(a, "a")
        if frame.size == 0:
            raise c.Domain("a is empty")
        ranks = np.asarray(_lib.sk_stats.cs_rank(frame.to_numpy(), axis=ax), dtype=float)
        if not np.isfinite(ranks).any():
            raise c.Domain("no finite value to rank")
        return {"ranks": c.jsonable(ranks), "labels": [str(x) for x in (frame.index if ax == 1 else frame.columns)]}

    return c.run(name, unit="rank", method="cs_rank", params=params, piece_ids=pieces, compute=compute, inputs=(a, params))

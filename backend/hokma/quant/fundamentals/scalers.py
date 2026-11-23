"""Cross sectional scalers (SK-104): standardize, winsorize, Gaussian rank, percentile rank and tanh shrinkage, date by date.

Every date is scaled with the assets of that date only, so a later date can never change an earlier result. Assets that are inactive
on a date (NaN) are left out of that date's cross section and come back as NaN.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _common as c
from hokma.quant.fundamentals import _lib

METHODS = ("standard", "winsorize", "gaussian_rank", "percentile_rank", "tanh")
MAX_GROUP = 100_000


def _float(label: str, value: Any, low: float, high: float, *, open_low: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise BadInput(f"{label} must be a finite number")
    f = float(value)
    if f < low or f > high or (open_low and f == low):
        raise BadInput(f"{label}={f} is outside the allowed range")
    return f


def build_scaler(method: str, params: dict[str, Any]):
    """The library scaler of a method with validated parameters."""
    sk = _lib.sk_preprocessing
    if method not in METHODS:
        raise BadInput(f"method must be one of {list(METHODS)}")
    group = params.get("min_group_size")
    if group is not None:
        if isinstance(group, bool) or not isinstance(group, int) or not 1 <= group <= MAX_GROUP:
            raise BadInput(f"min_group_size must be a whole number between 1 and {MAX_GROUP}")
    kwargs = {} if group is None else {"min_group_size": group}
    if method == "standard":
        return sk.CSStandardScaler(**kwargs)
    if method == "winsorize":
        low = _float("low", params.get("low", 0.01), 0.0, 1.0)
        high = _float("high", params.get("high", 0.99), 0.0, 1.0)
        if not low < high:
            raise BadInput("low must be below high")
        return sk.CSWinsorizer(low=low, high=high)
    if method == "gaussian_rank":
        return sk.CSGaussianRankScaler(scale=bool(params.get("scale", True)), **kwargs)
    if method == "percentile_rank":
        return sk.CSPercentileRankScaler(**kwargs)
    return sk.CSTanhShrinker(knee=_float("knee", params.get("knee", 3.0), 0.0, 100.0, open_low=True))


@registry.register("fundamentals.cs_scale", module="fundamentals", piece_ids=("SK-104",),
                   inputs={"values": {"type": "dataframe", "required": True}, "method": {"type": "str", "required": True},
                           "min_group_size": {"type": "int", "required": False, "default": None}, "low": {"type": "float", "required": False, "default": None},
                           "high": {"type": "float", "required": False, "default": None}, "knee": {"type": "float", "required": False, "default": None},
                           "scale": {"type": "bool", "required": False, "default": None}},
                   summary="Scales a signal across assets on each date: standard (z score), winsorize (clip at quantiles), gaussian_rank, percentile_rank or tanh (soft shrink). A date with fewer assets than min_group_size gives NaN.")
def cs_scale(values: Any, method: str, min_group_size: int | None = None, low: float | None = None, high: float | None = None,
             knee: float | None = None, scale: bool | None = None):
    name, pieces = "fundamentals.cs_scale", ("SK-104",)
    if not isinstance(values, pd.DataFrame):
        raise BadInput(f"values: expected a DataFrame of dates by assets, got {type(values).__name__}")
    params = {k: v for k, v in (("min_group_size", min_group_size), ("low", low), ("high", high), ("knee", knee), ("scale", scale)) if v is not None}
    scaler = build_scaler(method, params)
    if values.shape[0] == 0 or values.shape[1] == 0:
        return c.empty(name, "score", method, "values is empty", params={"method": method, **params}, piece_ids=pieces)
    try:
        frame = values.astype(float).replace([np.inf, -np.inf], np.nan)
    except (TypeError, ValueError) as exc:
        raise BadInput("values are not numeric") from exc
    if not frame.notna().to_numpy().any():
        return c.empty(name, "score", method, "values has no finite number", params={"method": method, **params}, piece_ids=pieces)
    warns = ("some dates have fewer finite assets than min_group_size and come back as NaN",) if method in ("standard", "gaussian_rank", "percentile_rank") else ()
    try:
        out = scaler.fit_transform(frame.to_numpy())
    except ValueError as exc:
        return c.empty(name, "score", method, str(exc)[:200], params={"method": method, **params}, piece_ids=pieces)
    scaled = pd.DataFrame(out, index=frame.index, columns=frame.columns)
    warn = warns if scaled.isna().to_numpy().sum() > frame.isna().to_numpy().sum() else ()
    value = {"dates": [str(i) for i in scaled.index], "assets": [str(x) for x in scaled.columns], "values": scaled.to_numpy(), "latest": {str(k): v for k, v in scaled.iloc[-1].items()}}
    return c.build(name, value, unit="score", method=method, params={"method": method, **params}, piece_ids=pieces,
                   inputs=(frame,), warnings=warn)

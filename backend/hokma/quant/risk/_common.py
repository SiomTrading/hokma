"""Shared plumbing of the risk module: input cleaning, result building and failure handling.

Bad data never raises: it returns a QuantResult with value None and the reason in warnings.
BadInput is raised only for a wrong type or shape.
"""
from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np
import pandas as pd

from hokma.quant import helpers
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult


def jsonable(value: Any) -> Any:
    """numpy and pandas containers to plain python; non finite floats to None."""
    if isinstance(value, (pd.Series, pd.Index)):
        return [jsonable(v) for v in value.tolist()]
    if isinstance(value, pd.DataFrame):
        return {str(c): jsonable(value[c]) for c in value.columns}
    if isinstance(value, np.ndarray):
        return jsonable(value.tolist())
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    return value


def empty(name: str, unit: str, method: str, warnings: tuple[str, ...] | str, *, params: dict | None = None,
          piece_ids: tuple[str, ...] = ()) -> QuantResult:
    if isinstance(warnings, str):
        warnings = (warnings,)
    return QuantResult(name=name, value=None, unit=unit, method=method, params=dict(params or {}),
                       warnings=tuple(warnings), piece_ids=tuple(piece_ids))


def is_constant(series: pd.Series) -> bool:
    """True when every value is the same (exact range, not a floating point standard deviation)."""
    return len(series) > 0 and float(series.max() - series.min()) == 0.0


def level_warning(level: Any) -> str | None:
    """None when the confidence level is a number strictly between 0 and 1."""
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        raise BadInput(f"level must be a number, got {type(level).__name__}")
    if not (0.0 < float(level) < 1.0) or math.isnan(float(level)):
        return f"level must be strictly between 0 and 1, got {level}"
    return None


def build(name: str, value: Any, *, unit: str, method: str, params: dict, piece_ids: tuple[str, ...],
          inputs: tuple[Any, ...], as_of_source: pd.Series | None = None, warnings: tuple[str, ...] = ()) -> QuantResult:
    """Final QuantResult. A scalar that is not finite, or an all-None dict, becomes an empty result."""
    clean_value = jsonable(value)
    if clean_value is None or (isinstance(clean_value, dict) and all(v is None for v in clean_value.values())):
        return empty(name, unit, method, warnings + ("result is not finite",), params=params, piece_ids=piece_ids)
    return QuantResult(
        name=name, value=clean_value, unit=unit, method=method, params=dict(params),
        inputs_hash=helpers.hash_inputs(*inputs, params),
        as_of=helpers.last_as_of(as_of_source) if as_of_source is not None else None,
        warnings=tuple(warnings), piece_ids=tuple(piece_ids),
    )


def measure(name: str, returns: Any, *, unit: str, method: str, params: dict, piece_ids: tuple[str, ...],
            compute: Callable[[pd.Series], Any], min_obs: int = 2, allow_constant: bool = False,
            level: float | None = None) -> QuantResult:
    """Run `compute` on a clean returns series. Empty, constant or all-NaN input gives value None."""
    if level is not None:
        problem = level_warning(level)
        if problem:
            return empty(name, unit, method, problem, params=params, piece_ids=piece_ids)
    series, warns = helpers.validate_returns(returns, min_obs=min_obs)
    constant = len(series) > 1 and float(series.max() - series.min()) == 0.0
    if len(series) < min_obs or (constant and not allow_constant):
        reason = warns or (("constant series: dispersion is zero",) if constant else ("degenerate input",))
        if constant and not any("constant" in w for w in reason):
            reason = reason + ("constant series: dispersion is zero",)
        return empty(name, unit, method, reason, params=params, piece_ids=piece_ids)
    try:
        raw = compute(series)
    except BadInput:
        raise
    except Exception as exc:  # a library failure on odd data is a data problem, not a crash
        return empty(name, unit, method, warns + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=piece_ids)
    return build(name, raw, unit=unit, method=method, params=params, piece_ids=piece_ids, inputs=(series,),
                 as_of_source=series, warnings=warns)


def as_panel(panel: Any, *, name: str = "panel", min_cols: int = 2) -> pd.DataFrame:
    """A numeric DataFrame with at least `min_cols` columns. Wrong type or shape raises BadInput."""
    if not isinstance(panel, pd.DataFrame):
        raise BadInput(f"{name}: expected a DataFrame, got {type(panel).__name__}")
    if panel.shape[1] < min_cols:
        raise BadInput(f"{name}: expected at least {min_cols} columns, got {panel.shape[1]}")
    try:
        return panel.astype(float)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"{name}: values are not numeric") from exc


def clean_panel(panel: pd.DataFrame, *, min_obs: int) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """Drop rows with NaN or infinite values; warn when too few rows remain or a column is constant."""
    cleaned = panel.replace([np.inf, -np.inf], np.nan).dropna(how="any")
    warns: list[str] = []
    dropped = len(panel) - len(cleaned)
    if dropped:
        warns.append(f"dropped {dropped} row(s) with NaN or infinite values")
    if len(cleaned) < min_obs:
        warns.append(f"need at least {min_obs} complete row(s), got {len(cleaned)}")
    elif ((cleaned.max() - cleaned.min()) == 0.0).any():
        constant = [str(c) for c in cleaned.columns[((cleaned.max() - cleaned.min()) == 0.0).to_numpy()]]
        warns.append(f"constant column(s): {constant}")
    return cleaned, tuple(warns)


def panel_ok(cleaned: pd.DataFrame, warns: tuple[str, ...], min_obs: int) -> bool:
    return len(cleaned) >= min_obs and not any(w.startswith("constant column") for w in warns)


def pair(x: Any, y: Any) -> tuple[pd.Series, pd.Series, tuple[str, ...]]:
    """Two returns series aligned on their index (or position when both have the default index)."""
    sx = helpers.as_series(x, name="x")
    sy = helpers.as_series(y, name="y")
    if len(sx) != len(sy) and not (isinstance(sx.index, pd.DatetimeIndex) and isinstance(sy.index, pd.DatetimeIndex)):
        raise BadInput(f"x and y must have the same length, got {len(sx)} and {len(sy)}")
    joined = pd.concat([sx.rename("x"), sy.rename("y")], axis=1, join="inner" if isinstance(sx.index, pd.DatetimeIndex) else "outer")
    joined = joined.replace([np.inf, -np.inf], np.nan)
    cleaned = joined.dropna()
    warns = ()
    if len(cleaned) != len(joined):
        warns = (f"dropped {len(joined) - len(cleaned)} row(s) with NaN or infinite values",)
    return cleaned["x"], cleaned["y"], warns


def weights_series(weights: Any, columns: pd.Index) -> pd.Series:
    """Weights as a Series aligned to the panel columns. Wrong type or unknown asset raises BadInput."""
    if isinstance(weights, pd.Series):
        weights = weights.to_dict()
    if not isinstance(weights, dict):
        raise BadInput(f"weights: expected a dict, got {type(weights).__name__}")
    unknown = [k for k in weights if k not in set(columns)]
    if unknown:
        raise BadInput(f"weights: unknown asset(s) {unknown}")
    try:
        return pd.Series({c: float(weights.get(c, 0.0)) for c in columns})
    except (TypeError, ValueError) as exc:
        raise BadInput("weights: values are not numeric") from exc


def period_index(series_or_frame: Any) -> Any:
    """A copy indexed by daily periods when the index is a DatetimeIndex (the form the library expects)."""
    out = series_or_frame.copy()
    if isinstance(out.index, pd.DatetimeIndex):
        out.index = out.index.to_period("D")
    return out

"""Shared plumbing of the portfolio module: input cleaning, result building and failure handling.

Bad data never raises: it returns a QuantResult with value None and the reason in warnings.
BadInput is raised only for a wrong type or shape.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Mapping

import numpy as np
import pandas as pd

from hokma.quant import helpers
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult


class Domain(Exception):
    """The data or a parameter is outside what the method supports. The message goes to warnings."""


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
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def empty(name: str, unit: str, method: str, warnings: tuple[str, ...] | str, *, params: dict | None = None,
          piece_ids: tuple[str, ...] = ()) -> QuantResult:
    if isinstance(warnings, str):
        warnings = (warnings,)
    return QuantResult(name=name, value=None, unit=unit, method=method, params=jsonable(dict(params or {})),
                       warnings=tuple(warnings), piece_ids=tuple(piece_ids))


def _hashable(item: Any) -> Any:
    if isinstance(item, pd.DataFrame) and not all(pd.api.types.is_numeric_dtype(t) for t in item.dtypes):
        return {"columns": [str(col) for col in item.columns], "values": item.astype(str).values.tolist()}
    if isinstance(item, (pd.Series, pd.DataFrame)):
        return item
    return jsonable(item)


def build(name: str, value: Any, *, unit: str, method: str, params: dict, piece_ids: tuple[str, ...], inputs: tuple[Any, ...],
          as_of_source: Any = None, warnings: tuple[str, ...] = ()) -> QuantResult:
    """Final QuantResult. A value that is not finite becomes an empty result."""
    clean_value = jsonable(value)
    if clean_value is None:
        return empty(name, unit, method, tuple(warnings) + ("result is not finite",), params=params, piece_ids=piece_ids)
    safe_params = jsonable(dict(params))
    return QuantResult(
        name=name, value=clean_value, unit=unit, method=method, params=safe_params,
        inputs_hash=helpers.hash_inputs(*[_hashable(i) for i in inputs], safe_params),
        as_of=helpers.last_as_of(as_of_source) if as_of_source is not None else None,
        warnings=tuple(warnings), piece_ids=tuple(piece_ids),
    )


def run(name: str, *, unit: str, method: str, params: dict, piece_ids: tuple[str, ...], compute: Callable[[], Any],
        inputs: tuple[Any, ...] = (), as_of_source: Any = None, warnings: tuple[str, ...] = ()) -> QuantResult:
    """Run `compute`. A Domain error or a numerical failure on odd data gives value None with the reason in warnings.

    `compute` may return (value, extra_warnings).
    """
    try:
        raw = compute()
    except BadInput:
        raise
    except Domain as exc:
        return empty(name, unit, method, tuple(warnings) + (str(exc),), params=params, piece_ids=piece_ids)
    except (ValueError, ArithmeticError, np.linalg.LinAlgError, KeyError, IndexError, TypeError, RuntimeError, AttributeError) as exc:
        return empty(name, unit, method, tuple(warnings) + (f"{type(exc).__name__}: {str(exc)[:240]}",), params=params, piece_ids=piece_ids)
    extra: tuple[str, ...] = ()
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], tuple):
        raw, extra = raw
    return build(name, raw, unit=unit, method=method, params=params, piece_ids=piece_ids, inputs=inputs,
                 as_of_source=as_of_source, warnings=tuple(warnings) + extra)


def number(name: str, value: Any, *, low: float | None = None, high: float | None = None, integer: bool = False,
           low_open: bool = False) -> float:
    """A finite number. A wrong type raises BadInput; a value outside [low, high] raises Domain."""
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise BadInput(f"{name} must be a number, got {type(value).__name__}")
    f = float(value)
    if integer and f != int(f):
        raise BadInput(f"{name} must be a whole number, got {value!r}")
    if not math.isfinite(f):
        raise Domain(f"{name} must be finite")
    if low is not None and (f <= low if low_open else f < low):
        raise Domain(f"{name} must be {'above' if low_open else 'at least'} {low}, got {value}")
    if high is not None and f > high:
        raise Domain(f"{name} must be at most {high}, got {value}")
    return f


def choice(name: str, value: Any, options: tuple[str, ...]) -> str:
    """A string from a closed list. Anything else raises Domain naming the options."""
    if not isinstance(value, str) or value not in options:
        raise Domain(f"unknown {name} {value!r}; use one of {list(options)}")
    return value


def as_panel(panel: Any, *, name: str = "returns", min_cols: int = 1) -> pd.DataFrame:
    """A numeric DataFrame with at least `min_cols` columns. Wrong type or shape raises BadInput."""
    if not isinstance(panel, pd.DataFrame):
        raise BadInput(f"{name}: expected a DataFrame, got {type(panel).__name__}")
    if panel.shape[1] < min_cols:
        raise BadInput(f"{name}: expected at least {min_cols} column(s), got {panel.shape[1]}")
    if len(set(map(str, panel.columns))) != panel.shape[1]:
        raise BadInput(f"{name}: asset names must be unique")
    try:
        return panel.astype(float)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"{name}: values are not numeric") from exc


def clean_panel(panel: pd.DataFrame, *, min_obs: int) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """Drop rows with NaN or infinite values; warn when too few rows remain."""
    cleaned = panel.replace([np.inf, -np.inf], np.nan).dropna(how="any")
    warns: list[str] = []
    dropped = len(panel) - len(cleaned)
    if dropped:
        warns.append(f"dropped {dropped} row(s) with NaN or infinite values")
    if len(cleaned) < min_obs:
        warns.append(f"need at least {min_obs} complete row(s), got {len(cleaned)}")
    return cleaned, tuple(warns)


def usable_panel(returns: Any, *, min_obs: int, min_cols: int = 1, name: str = "returns") -> tuple[pd.DataFrame | None, tuple[str, ...]]:
    """Clean panel, or None with the reasons when it cannot be used (empty, too short)."""
    panel = as_panel(returns, name=name, min_cols=min_cols)
    cleaned, warns = clean_panel(panel, min_obs=min_obs)
    if len(cleaned) < min_obs:
        return None, warns
    return cleaned, warns


def per_asset(value: Any, columns: pd.Index, label: str, default: float | None) -> list[float] | float | None:
    """A scalar, or a dict asset -> number expanded to the column order (missing assets take `default`)."""
    if value is None:
        return default
    if isinstance(value, Mapping):
        unknown = [k for k in value if k not in set(map(str, columns)) and k not in set(columns)]
        if unknown:
            raise BadInput(f"{label}: unknown asset(s) {unknown}")
        if default is None:
            missing = [str(c) for c in columns if c not in value and str(c) not in value]
            if missing:
                raise BadInput(f"{label}: no value for asset(s) {missing}")
        out = []
        for c in columns:
            raw = value.get(c, value.get(str(c), default))
            out.append(float(number(f"{label}[{c}]", raw)))
        return out
    return float(number(label, value))


def matrix_to_dict(matrix: Any, columns: pd.Index) -> dict[str, dict[str, float | None]]:
    """A square matrix as {asset: {asset: value}}; non finite values become None."""
    arr = np.asarray(matrix, dtype=float)
    names = [str(col) for col in columns]
    return {a: {b: jsonable(float(arr[i, j])) for j, b in enumerate(names)} for i, a in enumerate(names)}


def weights_to_dict(weights: Any, columns: pd.Index) -> dict[str, float]:
    return {str(c): float(w) for c, w in zip(columns, np.asarray(weights, dtype=float))}


def weights_vector(weights: Any, columns: pd.Index, *, label: str = "weights") -> np.ndarray:
    """Weights (dict asset -> number, Series or list) aligned to the columns. Unknown asset or wrong size raises BadInput."""
    if isinstance(weights, pd.Series):
        weights = weights.to_dict()
    if isinstance(weights, Mapping):
        unknown = [k for k in weights if k not in set(columns) and k not in set(map(str, columns))]
        if unknown:
            raise BadInput(f"{label}: unknown asset(s) {unknown}")
        try:
            return np.array([float(weights.get(c, weights.get(str(c), 0.0))) for c in columns])
        except (TypeError, ValueError) as exc:
            raise BadInput(f"{label}: values are not numeric") from exc
    if isinstance(weights, (list, tuple, np.ndarray)):
        arr = np.asarray(weights, dtype=float).ravel()
        if len(arr) != len(columns):
            raise BadInput(f"{label}: expected {len(columns)} value(s), got {len(arr)}")
        return arr
    raise BadInput(f"{label}: expected a dict, list or Series, got {type(weights).__name__}")

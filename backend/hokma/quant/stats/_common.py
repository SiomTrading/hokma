"""Shared plumbing of the stats module: input cleaning, result building and failure handling.

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
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def empty(name: str, unit: str, method: str, warnings: tuple[str, ...] | str, *, params: dict | None = None,
          piece_ids: tuple[str, ...] = ()) -> QuantResult:
    if isinstance(warnings, str):
        warnings = (warnings,)
    return QuantResult(name=name, value=None, unit=unit, method=method, params=jsonable(dict(params or {})),
                       warnings=tuple(warnings), piece_ids=tuple(piece_ids))


def build(name: str, value: Any, *, unit: str, method: str, params: dict, piece_ids: tuple[str, ...],
          inputs: tuple[Any, ...], as_of_source: pd.Series | None = None, warnings: tuple[str, ...] = ()) -> QuantResult:
    """Final QuantResult. A scalar that is not finite, or an all-None dict, becomes an empty result."""
    clean_value = jsonable(value)
    if clean_value is None or (isinstance(clean_value, dict) and all(v is None for v in clean_value.values())):
        return empty(name, unit, method, tuple(warnings) + ("result is not finite",), params=params, piece_ids=piece_ids)
    safe_params = jsonable(dict(params))
    return QuantResult(
        name=name, value=clean_value, unit=unit, method=method, params=safe_params,
        inputs_hash=helpers.hash_inputs(*[_hashable(i) for i in inputs], safe_params),
        as_of=helpers.last_as_of(as_of_source) if as_of_source is not None else None,
        warnings=tuple(warnings), piece_ids=tuple(piece_ids),
    )


def _hashable(item: Any) -> Any:
    if isinstance(item, (pd.Series, pd.DataFrame)):
        return item
    return jsonable(item)


def run(name: str, *, unit: str, method: str, params: dict, piece_ids: tuple[str, ...], compute: Callable[[], Any],
        inputs: tuple[Any, ...] = (), as_of_source: pd.Series | None = None, warnings: tuple[str, ...] = ()) -> QuantResult:
    """Run `compute`. A Domain error or a numerical failure on odd data gives value None with the reason in warnings."""
    try:
        raw = compute()
    except BadInput:
        raise
    except Domain as exc:
        return empty(name, unit, method, tuple(warnings) + (str(exc),), params=params, piece_ids=piece_ids)
    except (ValueError, ArithmeticError, np.linalg.LinAlgError, KeyError, IndexError, TypeError, RuntimeError) as exc:
        return empty(name, unit, method, tuple(warnings) + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=piece_ids)
    extra: tuple[str, ...] = ()
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], tuple):
        raw, extra = raw
    return build(name, raw, unit=unit, method=method, params=params, piece_ids=piece_ids, inputs=inputs,
                 as_of_source=as_of_source, warnings=tuple(warnings) + extra)


def number(name: str, value: Any, *, low: float | None = None, high: float | None = None, integer: bool = False) -> float:
    """A finite number. A wrong type raises BadInput; a value outside [low, high] raises Domain."""
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise BadInput(f"{name} must be a number, got {type(value).__name__}")
    f = float(value)
    if integer and f != int(f):
        raise BadInput(f"{name} must be a whole number, got {value!r}")
    if not math.isfinite(f):
        raise Domain(f"{name} must be finite")
    if low is not None and f < low:
        raise Domain(f"{name} must be at least {low}, got {value}")
    if high is not None and f > high:
        raise Domain(f"{name} must be at most {high}, got {value}")
    return f


def clean_series(values: Any, *, name: str = "series", min_obs: int = 2) -> tuple[pd.Series, tuple[str, ...]]:
    """A float Series without NaN or infinite values, and the warnings. Too short or constant series are Domain errors."""
    series = helpers.as_series(values, name=name)
    cleaned = helpers.clean(series)
    warns: list[str] = []
    if len(cleaned) != len(series):
        warns.append(f"{name}: dropped {len(series) - len(cleaned)} NaN or infinite value(s)")
    if len(cleaned) < min_obs:
        raise Domain(f"{name}: need at least {min_obs} observation(s), got {len(cleaned)}")
    return cleaned, tuple(warns)


def is_constant(values: Any) -> bool:
    arr = np.asarray(values, dtype=float)
    return arr.size > 0 and float(arr.max() - arr.min()) == 0.0


def clean_frame(data: Any, *, name: str = "data", min_cols: int = 1, min_obs: int = 2) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """A numeric DataFrame without rows that hold NaN or infinite values."""
    if isinstance(data, pd.Series):
        data = data.to_frame()
    if not isinstance(data, pd.DataFrame):
        raise BadInput(f"{name}: expected a DataFrame, got {type(data).__name__}")
    if data.shape[1] < min_cols:
        raise BadInput(f"{name}: expected at least {min_cols} column(s), got {data.shape[1]}")
    try:
        frame = data.astype(float)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"{name}: values are not numeric") from exc
    cleaned = frame.replace([np.inf, -np.inf], np.nan).dropna(how="any")
    warns: tuple[str, ...] = ()
    if len(cleaned) != len(frame):
        warns = (f"{name}: dropped {len(frame) - len(cleaned)} row(s) with NaN or infinite values",)
    if len(cleaned) < min_obs:
        raise Domain(f"{name}: need at least {min_obs} complete row(s), got {len(cleaned)}")
    return cleaned, warns


def ohlc_frame(open_: Any, high: Any, low: Any, close: Any, *, min_obs: int = 10, datetime_index: bool = False) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """Bars as one clean frame (open, high, low, close). Mismatched lengths raise BadInput; bad bars are Domain errors."""
    bars = [helpers.as_series(v, name=n) for v, n in ((open_, "open_"), (high, "high"), (low, "low"), (close, "close"))]
    if len({len(b) for b in bars}) != 1:
        raise BadInput("open_, high, low and close must have the same length")
    if datetime_index and not all(isinstance(b.index, pd.DatetimeIndex) for b in bars):
        raise BadInput("open_, high, low and close need a datetime index")
    frame = pd.concat(bars, axis=1, keys=["open", "high", "low", "close"]).replace([np.inf, -np.inf], np.nan)
    cleaned = frame.dropna()
    warns: tuple[str, ...] = ()
    if len(cleaned) != len(frame):
        warns = (f"dropped {len(frame) - len(cleaned)} row(s) with missing values",)
    if len(cleaned) < min_obs:
        raise Domain(f"need {min_obs} complete bars, got {len(cleaned)}")
    if (cleaned <= 0).any().any():
        raise Domain("prices must be positive")
    if (cleaned["high"] < cleaned[["open", "close"]].max(axis=1)).any() or (cleaned["low"] > cleaned[["open", "close"]].min(axis=1)).any():
        raise Domain("bars are inconsistent: high below open or close, or low above them")
    return cleaned, warns


def acf(x: np.ndarray, nlags: int) -> np.ndarray:
    """Sample autocorrelations at lags 0..nlags (denominator n, mean removed)."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    xc = x - x.mean()
    denom = float(np.dot(xc, xc))
    if denom == 0.0:
        raise Domain("constant series: autocorrelation is undefined")
    return np.array([float(np.dot(xc[: n - k], xc[k:])) / denom for k in range(nlags + 1)])


def pacf(x: np.ndarray, nlags: int) -> np.ndarray:
    """Partial autocorrelations at lags 0..nlags by the Durbin-Levinson recursion."""
    r = acf(x, nlags)
    out = np.zeros(nlags + 1)
    out[0] = 1.0
    phi_prev = np.zeros(0)
    for k in range(1, nlags + 1):
        if k == 1:
            phi_kk = r[1]
            phi = np.array([phi_kk])
        else:
            num = r[k] - float(np.dot(phi_prev, r[k - 1:0:-1]))
            den = 1.0 - float(np.dot(phi_prev, r[1:k]))
            if den == 0.0:
                raise Domain("partial autocorrelation is undefined")
            phi_kk = num / den
            phi = np.concatenate([phi_prev - phi_kk * phi_prev[::-1], [phi_kk]])
        out[k] = phi_kk
        phi_prev = phi
    return out


def ljung_box(x: np.ndarray, lags: int, model_df: int = 0) -> tuple[float, float]:
    """Ljung-Box Q statistic and its chi-square p-value at `lags`, with `model_df` fitted parameters removed."""
    from hokma.quant.stats import _lib

    n = len(x)
    r = acf(x, lags)
    q = n * (n + 2) * float(np.sum(r[1:] ** 2 / (n - np.arange(1, lags + 1))))
    dof = lags - model_df
    if dof < 1:
        raise Domain("lags must exceed the number of fitted parameters")
    return q, float(_lib.sp_stats.chi2.sf(q, dof))


def newey_west_variance(u: np.ndarray, lags: int) -> float:
    """Long run variance of a series (Bartlett weights) with `lags` autocovariances; u is used as given."""
    u = np.asarray(u, dtype=float)
    n = len(u)
    total = float(np.dot(u, u)) / n
    for k in range(1, lags + 1):
        w = 1.0 - k / (lags + 1.0)
        total += 2.0 * w * float(np.dot(u[k:], u[:-k])) / n
    return total


def default_nw_lags(n: int) -> int:
    return int(math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))

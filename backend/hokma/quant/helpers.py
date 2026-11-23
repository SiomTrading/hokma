"""Safe numeric helpers shared by the quant modules (SPEC-017: AH-11).

Pure Python plus numpy and pandas. No quantitative library is imported here: those live in each module's _lib.py.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable

import numpy as np
import pandas as pd

from hokma.core.contenthash import content_hash
from hokma.quant.errors import BadInput

PERIODS = {"daily": 252, "weekly": 52, "monthly": 12, "quarterly": 4, "yearly": 1, "annual": 1, "intraday_1m": 252 * 390}


def safe_div(num: float, den: float, default: float | None = None) -> float | None:
    """num / den; `default` when the denominator is zero, NaN or the numerator is NaN."""
    try:
        if den is None or num is None or math.isnan(den) or math.isnan(num) or den == 0:
            return default
        return num / den
    except TypeError:
        return default


def as_series(values: Any, *, name: str = "returns") -> pd.Series:
    """Coerce a list, array or Series to a float Series. Wrong type or shape raises BadInput."""
    if isinstance(values, pd.DataFrame):
        if values.shape[1] != 1:
            raise BadInput(f"{name}: expected one column, got {values.shape[1]}")
        values = values.iloc[:, 0]
    if isinstance(values, pd.Series):
        series = values
    elif isinstance(values, (list, tuple, np.ndarray)):
        series = pd.Series(list(values))
    else:
        raise BadInput(f"{name}: expected a Series, list or array, got {type(values).__name__}")
    try:
        return series.astype(float)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"{name}: values are not numeric") from exc


def clean(series: pd.Series) -> pd.Series:
    """Drop NaN and infinite values."""
    return series.replace([np.inf, -np.inf], np.nan).dropna()


def validate_returns(values: Any, *, min_obs: int = 2, name: str = "returns") -> tuple[pd.Series, tuple[str, ...]]:
    """Returns the clean series and the warnings. An empty series gives a warning, not an exception."""
    series = as_series(values, name=name)
    warnings: list[str] = []
    cleaned = clean(series)
    dropped = len(series) - len(cleaned)
    if dropped:
        warnings.append(f"dropped {dropped} NaN or infinite value(s)")
    if len(cleaned) < min_obs:
        warnings.append(f"need at least {min_obs} observation(s), got {len(cleaned)}")
    elif float(cleaned.std(ddof=1)) == 0.0 and min_obs >= 2:
        warnings.append("constant series: dispersion is zero")
    return cleaned, tuple(warnings)


def is_degenerate(series: pd.Series, *, min_obs: int = 2) -> bool:
    return len(series) < min_obs or (len(series) > 1 and float(series.std(ddof=1)) == 0.0)


def periods_per_year(label_or_number: str | int | float) -> int:
    if isinstance(label_or_number, (int, float)):
        if label_or_number <= 0:
            raise BadInput("periods_per_year must be positive")
        return int(label_or_number)
    try:
        return PERIODS[str(label_or_number).lower()]
    except KeyError as exc:
        raise BadInput(f"unknown frequency {label_or_number!r}") from exc


def annualize_rate(period_rate: float, periods: int) -> float:
    return (1.0 + period_rate) ** periods - 1.0


def deannualize_rate(annual_rate: float, periods: int) -> float:
    return (1.0 + annual_rate) ** (1.0 / periods) - 1.0


def total_return(series: pd.Series) -> float:
    return float((1.0 + series).prod() - 1.0)


def last_as_of(series: pd.Series) -> datetime | None:
    if len(series) == 0:
        return None
    idx = series.index[-1]
    if isinstance(idx, pd.Timestamp):
        return idx.to_pydatetime()
    return None


def hash_inputs(*parts: Any) -> str:
    """Content hash of the inputs of a call (series are hashed by their values and index)."""
    prepared: list[Any] = []
    for p in parts:
        if isinstance(p, pd.Series):
            prepared.append({"index": [str(i) for i in p.index], "values": p.astype(float).tolist()})
        elif isinstance(p, pd.DataFrame):
            prepared.append({"columns": [str(c) for c in p.columns], "index": [str(i) for i in p.index], "values": p.astype(float).values.tolist()})
        else:
            prepared.append(p)
    return content_hash(prepared)


def finite_or_none(value: Any) -> float | None:
    """A float, or None when it is NaN, infinite or not a number."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def drawdown_series(returns: pd.Series) -> pd.Series:
    wealth = (1.0 + returns).cumprod()
    return wealth / wealth.cummax() - 1.0


def clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def rolling_windows(n: int, window: int) -> Iterable[tuple[int, int]]:
    """Index pairs [start, end) of every full window."""
    for start in range(0, n - window + 1):
        yield start, start + window

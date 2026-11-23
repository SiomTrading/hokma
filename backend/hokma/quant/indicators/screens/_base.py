"""Shared input handling of the screens: daily bars cut to the closed ones, dates as text, and the result builder.

Every screen reads only closed bars. A daily bar is closed once 16:00 New York of its date has passed at as_of; a date only
as_of means the start of that day (that day's bar is not closed). Bars dropped by the gate never change a result.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from hokma.quant.errors import BadInput  # noqa: F401  (re-exported for the screens)
from hokma.quant.indicators._common import bars_frame, closed_bars, sessions_between


class OutOfDomain(ValueError):
    """A parameter value outside its domain: the screen returns no value with the reason (a wrong type is BadInput instead)."""


def day_text(stamp: Any) -> str:
    return str(pd.Timestamp(stamp).date())


def daily_closed(bars: Any, needed: tuple[str, ...], *, as_of: Any, min_bars: int, optional: tuple[str, ...] = (),
                 label: str = "bars") -> tuple[pd.DataFrame | None, int, tuple[str, ...]]:
    """(closed daily bars, number of bars dropped as not closed, warnings). None bars means the reason is in warnings."""
    if not isinstance(bars, pd.DataFrame):
        raise BadInput(f"{label}: expected a DataFrame, got {type(bars).__name__}")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise BadInput(f"{label}: the index must be a DatetimeIndex of daily bars")
    lower = {str(c).lower(): c for c in bars.columns}
    missing = [n for n in needed if n not in lower]
    if missing:
        raise BadInput(f"{label}: missing column(s) {missing}")
    keep = [n for n in (*needed, *optional) if n in lower]
    try:
        raw = pd.DataFrame({n: bars[lower[n]].astype(float) for n in keep}, index=bars.index)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"{label}: values are not numeric") from exc
    flag = bars[lower["closed"]].astype(bool) if "closed" in lower else None
    if raw.empty:
        return None, 0, (f"{label}: no bars",)
    if not raw.index.is_monotonic_increasing or raw.index.has_duplicates:
        return None, 0, (f"{label}: the index must be increasing and unique",)
    gated, dropped, warns = closed_bars(raw, as_of, closed_flag=flag)
    if gated.empty:
        return None, dropped, warns + (f"{label}: no closed bar at as_of",)
    frame, problems = bars_frame(gated[list(needed)], needed, min_bars=min_bars, label=label)
    if frame is None:
        return None, dropped, warns + problems
    for n in optional:
        if n in gated.columns:
            frame[n] = gated[n]
    return frame, dropped, warns


def stale_reason(frame: pd.DataFrame, as_of: Any, max_bar_age: int) -> str | None:
    """A reason when the latest closed bar is older than max_bar_age sessions at as_of."""
    if as_of is None:
        return None
    stamp = pd.Timestamp(as_of)
    age = sessions_between(frame.index[-1], stamp.date() if isinstance(stamp, pd.Timestamp) else stamp)
    if age > max_bar_age:
        return f"stale: the latest closed bar is {age} sessions old, above max_bar_age={max_bar_age}"
    return None


def as_float(name: str, value: Any, *, low: float | None = None, positive: bool = False) -> float:
    """A finite number; the wrong type raises BadInput, a value outside the domain raises OutOfDomain (callers turn it into a result)."""
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadInput(f"{name} must be a number, got {type(value).__name__}")
    if not math.isfinite(float(value)):
        raise OutOfDomain(f"{name} must be finite")
    if positive and value <= 0:
        raise OutOfDomain(f"{name} must be positive")
    if low is not None and value < low:
        raise OutOfDomain(f"{name} must be at least {low}")
    return float(value)


def as_date(value: Any) -> pd.Timestamp:
    """A date, string or timestamp as a naive normalised Timestamp."""
    if isinstance(value, bool) or not isinstance(value, (str, date, pd.Timestamp)):
        raise BadInput(f"expected a date, got {type(value).__name__}")
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"not a date: {value!r}") from exc
    return stamp.tz_localize(None).normalize() if stamp.tzinfo else stamp.normalize()

"""Higher timeframes without looking at the future (SPEC-020: B-30).

The index of the input is the open time of each bar. A row sees a higher bar only when that bar has closed at or before the open
of the row. The bar that is still forming does not exist for the row.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from hokma.quant.errors import BadInput
from hokma.quant.limits import check_inputs

_RULE = re.compile(r"^(\d{1,4})(min|h|D|W)$")
_UNITS = {"min": pd.Timedelta(minutes=1), "h": pd.Timedelta(hours=1), "D": pd.Timedelta(days=1), "W": pd.Timedelta(days=7)}
_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
_WEEK_ORIGIN = pd.Timestamp("1970-01-05")     # a Monday
_DAY_ORIGIN = pd.Timestamp("1970-01-01")


def resample_closed(frame: pd.DataFrame, rule: str, *, offset: str = "0min") -> pd.DataFrame:
    """The latest closed bar of `rule` (5min, 15min, 1h, 1D, 1W) on every row of `frame`.

    Rows before the first closed bar hold NaN. The result has the columns of the input that are bar fields, plus
    `htf_start` and `htf_end` (NaT where nothing has closed; wall clock of the index). Weeks start on Monday; `offset` shifts the start of the bins
    (for example 18h for a trading day that opens at 18:00).
    """
    if not isinstance(frame, pd.DataFrame) or not isinstance(frame.index, pd.DatetimeIndex):
        raise BadInput("frame must be a DataFrame indexed by the open time of each bar")
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise BadInput("the bars must be in time order with no repeated time")
    found = _RULE.match(str(rule))
    if not found or int(found.group(1)) < 1:
        raise BadInput(f"rule must look like 5min, 15min, 1h, 1D or 1W, got {rule!r}")
    check_inputs({"frame": frame})
    fields = [c for c in _AGG if c in frame.columns]
    if "close" not in fields:
        raise BadInput("frame needs a close column")
    try:
        shift = pd.Timedelta(offset)
    except ValueError:
        raise BadInput(f"offset must be a time span such as 18h, got {offset!r}") from None
    step = int(found.group(1)) * _UNITS[found.group(2)]
    origin = _WEEK_ORIGIN if found.group(2) == "W" else _DAY_ORIGIN
    index = frame.index
    wall = (index.tz_localize(None) if index.tz is not None else index).as_unit("ns")
    bins = np.asarray((wall - shift - origin) // step, dtype=np.int64)
    grouped = frame[fields].groupby(bins, sort=True).agg({c: _AGG[c] for c in fields})
    first = np.unique(bins)
    bin_start = (origin + pd.to_timedelta(first * step.value, unit="ns") + shift).as_unit("ns")
    bin_end = (bin_start + step).to_numpy("datetime64[ns]")
    pos = np.searchsorted(bin_end, wall.to_numpy("datetime64[ns]"), side="right") - 1
    ok = pos >= 0
    values = np.full((len(frame), len(fields)), np.nan)
    values[ok] = grouped.to_numpy(dtype=float)[pos[ok]]
    out = pd.DataFrame(values, index=index, columns=fields)
    nat = np.datetime64("NaT", "ns")
    out["htf_start"] = np.where(ok, bin_start.to_numpy("datetime64[ns]")[np.where(ok, pos, 0)], nat)
    out["htf_end"] = np.where(ok, bin_end[np.where(ok, pos, 0)], nat)
    return out

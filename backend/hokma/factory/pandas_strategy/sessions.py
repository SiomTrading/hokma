"""The columns that Hokma gives the table of bars (1.3.0, task 3433): session_id and is_rth, the same for every strategy.

session_id  the Globex session of the bar: the one that starts at 18:00 New York the evening before and ends at 17:00. It is the date of the session as a whole number (20261127 is the
            session of Friday 27 November, whose first bar is Thursday 26 November at 18:00 New York). Use it to start a daily value over, like a VWAP that begins with the session.
is_rth      the regular trading hours, 9:30 to 16:00 New York on a weekday, by the open time of the bar. Anchor a daily value at the regular open with is_rth.

Each strategy chooses where it anchors what is daily: Hokma only gives both columns. They are computed from the open time of the bar in UTC and the clock of New York, with its summer time.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ZONE = "America/New_York"
RTH_OPEN, RTH_CLOSE = 9 * 60 + 30, 16 * 60                 # minutes of the day, New York
GLOBEX_OPEN_SHIFT = pd.Timedelta(hours=6)                   # 18:00 plus 6 hours is midnight: the date of the session
DAY_SECONDS = 86_400


def session_columns(open_times_ns: np.ndarray, bar_seconds: int) -> tuple[np.ndarray, np.ndarray]:
    """(session_id as int64, is_rth as bool) for bars that open at `open_times_ns` (nanoseconds since 1970, UTC). Bars of a day or more are always regular hours."""
    local = pd.DatetimeIndex(np.asarray(open_times_ns, dtype="int64"), tz="UTC").tz_convert(ZONE)
    shifted = local + GLOBEX_OPEN_SHIFT
    session = (shifted.year * 10_000 + shifted.month * 100 + shifted.day).to_numpy(dtype="int64")
    if bar_seconds >= DAY_SECONDS:
        return session, np.ones(len(local), dtype=bool)
    minute = (local.hour * 60 + local.minute).to_numpy()
    rth = (minute >= RTH_OPEN) & (minute < RTH_CLOSE) & (local.day_of_week.to_numpy() < 5)
    return session, rth


def frame_of(open_times_ns: np.ndarray, columns: dict[str, np.ndarray], bar_seconds: int) -> pd.DataFrame:
    """The table that a strategy receives: open, high, low, close, volume, session_id and is_rth, indexed by the open time in UTC."""
    session, rth = session_columns(open_times_ns, bar_seconds)
    index = pd.DatetimeIndex(np.asarray(open_times_ns, dtype="int64"), tz="UTC", name="time")
    data = {name: np.asarray(columns[name], dtype="float64") for name in ("open", "high", "low", "close", "volume")}
    return pd.DataFrame({**data, "session_id": session, "is_rth": rth}, index=index)

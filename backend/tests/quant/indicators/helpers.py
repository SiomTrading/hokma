"""Helpers of the indicators tests: small fixed bars, result readers and the parity check against the library functions."""
from __future__ import annotations

import numpy as np
import pandas as pd

from hokma.quant.indicators import _lib

CLOSES = [10, 11, 12, 11, 13, 14, 13, 15, 16, 15]
VOLUMES = [100, 200, 150, 300, 250, 400, 350, 500, 450, 600]


def tiny() -> pd.DataFrame:
    """Ten daily bars with hand friendly numbers: high = close + 1, low = close - 2, open = previous close."""
    idx = pd.bdate_range("2024-01-02", periods=len(CLOSES))
    close = np.array(CLOSES, dtype=float)
    return pd.DataFrame({"open": np.r_[close[0], close[:-1]], "high": close + 1, "low": close - 2, "close": close,
                         "volume": np.array(VOLUMES, dtype=float)}, index=idx)


def latest(result, column: str | None = None):
    assert result.ok, result.warnings
    row = result.value["latest"]
    return row[column] if column else row


def column(result, name: str) -> pd.Series:
    """One column of the rows a result returned, as a float Series with NaN for None."""
    assert result.ok, result.warnings
    values = [np.nan if v is None else v for v in result.value["values"][name]]
    return pd.Series(values, index=result.value["index"], dtype=float)


def assert_parity(result, name: str, expected: pd.Series, tol: float = 1e-8) -> None:
    """The rows of the result equal the library series (same tail, NaN where the library has NaN)."""
    got = column(result, name)
    want = expected.replace([np.inf, -np.inf], np.nan).iloc[-len(got):].to_numpy(dtype=float)
    np.testing.assert_allclose(got.to_numpy(), want, atol=tol, rtol=0, equal_nan=True)


M, O, V, B = _lib.ta_momentum, _lib.ta_overlap, _lib.ta_volatility, _lib.ta_breadth


def bars_from_closes(closes, start="2024-01-02", *, volume=1_000_000.0, half_range=0.5) -> pd.DataFrame:
    """Daily bars from closes: open = previous close, high = close + half_range, low = close - half_range (high and low cover the open)."""
    close = np.asarray(closes, dtype=float)
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": open_, "high": np.maximum(close, open_) + half_range, "low": np.minimum(close, open_) - half_range, "close": close,
                         "volume": np.full(len(close), float(volume))}, index=pd.bdate_range(start, periods=len(close)))


def weekly_from_closes(closes, start="2023-01-06", volume=1_000_000.0) -> pd.DataFrame:
    """Weekly bars labelled by their Friday: open = close, high = close + 0.5, low = close - 0.5."""
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": np.full(len(close), float(volume))},
                        index=pd.date_range(start, periods=len(close), freq="W-FRI"))


def expand_weekly(weekly: pd.DataFrame) -> pd.DataFrame:
    """Five daily bars per weekly bar (Monday to Friday). Their open is the week open, their close the week close, the week high is
    on Wednesday and the week low on Tuesday, so resampling them returns the weekly bar exactly."""
    rows, index = [], []
    for friday, week in weekly.iterrows():
        o, h, l_, c, v = (float(week[k]) for k in ("open", "high", "low", "close", "volume"))
        closes = [o + (c - o) * (k + 1) / 5 for k in range(5)]
        prev = o
        pad = 0.01 * (h - l_)
        for k, cl in enumerate(closes):
            hi = h if k == 2 else min(max(prev, cl) + pad, h)
            lo = l_ if k == 1 else max(min(prev, cl) - pad, l_)
            rows.append((prev, max(hi, prev, cl), min(lo, prev, cl), cl, v / 5.0))
            index.append(friday - pd.Timedelta(days=4 - k))
            prev = cl
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex(index))


def vcp_closes() -> tuple[np.ndarray, dict[str, float]]:
    """311 closes: a long steady rise to the high H1 (bar 250), then three contractions of about 20, 12 and 6 percent (bars 250 to 301)
    and a rise back to 99 percent of the last peak (the pivot)."""
    c = [50 * 1.003 ** i for i in range(251)]
    h1 = c[-1]

    def seg(a: float, b: float, k: int) -> list[float]:
        return [a + (b - a) * (j + 1) / k for j in range(k)]

    l1, h2 = 0.8 * h1, 0.97 * h1
    l2 = 0.88 * h2
    h3 = 0.99 * h2
    l3 = 0.94 * h3
    c += seg(h1, l1, 15) + seg(l1, h2, 12) + seg(h2, l2, 10) + seg(l2, h3, 8) + seg(h3, l3, 6) + seg(l3, 0.99 * h3, 9)
    return np.array(c), {"h1": h1, "l1": l1, "h2": h2, "l2": l2, "h3": h3, "l3": l3}


def vcp_bars(extra=(), volume_tail: float = 500.0) -> pd.DataFrame:
    """The VCP series as bars (high = close * 1.002, low = close * 0.998); volume dries up over the last ten bars."""
    closes, _ = vcp_closes()
    closes = np.r_[closes, np.asarray(extra, dtype=float)]
    volume = np.full(len(closes), 1000.0)
    volume[-10:] = volume_tail
    return pd.DataFrame({"open": np.r_[closes[0], closes[:-1]], "high": closes * 1.002, "low": closes * 0.998, "close": closes, "volume": volume},
                        index=pd.bdate_range("2023-01-02", periods=len(closes)))


def stamp(frame: pd.DataFrame, position: int, time: str = "17:00") -> str:
    """as_of text just after the close of the bar at the position."""
    return f"{frame.index[position].date()}T{time}"

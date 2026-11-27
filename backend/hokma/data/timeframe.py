"""Any time interval as a bar of a backtest (task 3427): the same grammar and the same folding as the charts, behind the data module.

The temporality of a run is chosen by the owner and is any interval of the chart (`2m`, `7m`, `4h`, `1d`, `1w`). The data module serves five lengths (1m, 5m, 15m, 1h, 1D); any other is
folded from the finest of those that divides it and whose bars start where the session of the instrument starts. The grammar, the bases and the folding are the
ones of the chart code, and this module is the one place of the data side that reads them, so the backtest never reaches the market package itself.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from hokma.market.intervals import IntervalError, TimeInterval, parse_time
from hokma.market.models import Candle
from hokma.market.service import aggregate, bases_for
from hokma.market.sessions import Session, clock_session, session_for
from hokma.market.tz import zone

SERVED = ("1m", "5m", "15m", "1h", "1D")        # the canonical lengths the data module serves as they are

__all__ = ["IntervalError", "SERVED", "TimeInterval", "bases_for", "fold", "parse_bar", "session_of"]


def parse_bar(bar: str) -> TimeInterval:
    """The interval of a bar text; raises `IntervalError` when it is not a time interval (months, ranges and ticks are not bars of a backtest)."""
    return parse_time(bar)


def session_of(meta: dict[str, Any]) -> Session:
    """The session grid of the instrument, as the market source described it; a source that did not (a fixture, a file) gets the clock in UTC."""
    info = meta.get("session")
    if not isinstance(info, dict):
        return clock_session(zone("UTC"))
    tz = zone(str(info.get("timezone") or "UTC"), int(info.get("utc_offset_seconds") or 0))
    return clock_session(tz) if info.get("clock_aligned") else session_for(str(info.get("kind") or "other"), tz)


def fold(frame: pd.DataFrame, interval: TimeInterval, session: Session, cut_ts: int) -> pd.DataFrame:
    """The bars of `interval` folded from the bars of a base, like the charts do. An intraday bar that has not closed at `cut_ts` is dropped; the last bar of days or
    weeks is kept (each base bar in it is closed)."""
    candles = [Candle(int(pd.Timestamp(ts).timestamp()), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))
               for ts, r in zip(frame.index, frame.itertuples(index=False))]
    folded = aggregate(candles, interval, session)
    if folded and not interval.is_session_based and session.bounds(folded[-1].t, interval)[1] > cut_ts:
        folded = folded[:-1]
    index = pd.DatetimeIndex([pd.Timestamp(c.t, unit="s", tz="UTC") for c in folded], name=frame.index.name)
    return pd.DataFrame({"open": [c.o for c in folded], "high": [c.h for c in folded], "low": [c.l for c in folded], "close": [c.c for c in folded],
                         "volume": [c.v for c in folded]}, index=index)

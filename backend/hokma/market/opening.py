"""The "opening high" line: the highest price in the first 30 minutes of the day's session.

Session open by kind of instrument:
- futures: 09:30 New York (the US cash open the equity-index futures traders watch). Their own session runs
  almost around the clock, so the first bar of the day is not the open.
- crypto and currencies: 00:00 of the exchange time zone (UTC for crypto).
- everything else (equities, ETFs, indices): the first bar of the local day. The sources return regular hours only.
"""
from datetime import datetime, timedelta

from hokma.market.models import OPENING_RANGE_MINUTES, Candle, OpeningRange
from hokma.market.tz import NEW_YORK

MAX_DAYS_BACK = 7
WINDOW_SECONDS = OPENING_RANGE_MINUTES * 60


def supports(timeframe_seconds: int) -> bool:
    """The window must be a whole number of bars and bars must not be longer than the window."""
    return 0 < timeframe_seconds <= WINDOW_SECONDS and WINDOW_SECONDS % timeframe_seconds == 0


def opening_range(candles: list[Candle], timeframe_seconds: int, kind: str, tz) -> OpeningRange | None:
    if not candles or not supports(timeframe_seconds):
        return None
    last = datetime.fromtimestamp(candles[-1].t, tz)
    if kind == "future":
        shifted = (last - timedelta(hours=9, minutes=30)).date()
    else:
        shifted = last.date()
    for back in range(MAX_DAYS_BACK):
        day = shifted - timedelta(days=back)
        start = _session_start(candles, day, kind, tz)
        if start is None:
            continue
        window = [c for c in candles if start <= c.t < start + WINDOW_SECONDS]
        if not window:
            continue
        end = start + WINDOW_SECONDS
        return OpeningRange(
            high=max(c.h for c in window),
            session_date=day.isoformat(),
            start=start,
            end=end,
            complete=window[-1].t + timeframe_seconds >= end,
        )
    return None


def _session_start(candles: list[Candle], day, kind: str, tz) -> int | None:
    if kind == "future":
        return int(datetime(day.year, day.month, day.day, 9, 30, tzinfo=NEW_YORK).timestamp())
    if kind in ("crypto", "currency"):
        return int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp())
    for candle in candles:
        if datetime.fromtimestamp(candle.t, tz).date() == day:
            return candle.t
    return None

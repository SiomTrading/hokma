"""Builds what the chart shows from a source's raw series: visible bars, last price, change, market state,
data delay and the opening high. Knows nothing about any particular provider."""
import time
from collections.abc import Callable

from hokma.market import opening
from hokma.market.errors import MarketError
from hokma.market.models import TIMEFRAME_SECONDS, TIMEFRAMES, VISIBLE_BARS, ChartData, OpeningRange, Series
from hokma.market.source import MarketDataSource
from hokma.market.tz import zone

# A last trade older than this, while the market is open, is shown as delayed data.
DELAY_THRESHOLD_SECONDS = 300
# Bars of 5 minutes give the exact opening high for timeframes longer than the window itself.
OPENING_FALLBACK_TIMEFRAME = "5m"


def market_is_open(series: Series, timeframe: str, now: int) -> bool:
    """Open when the source says the regular session covers `now` and, for intraday bars, the data is not stale.

    The session only says "current or next". After the close it points at the next open, so `now` falls outside."""
    if series.session is None or not series.candles:
        return False
    if not (series.session.start <= now < series.session.end):
        return False
    if timeframe == "1D":
        return True
    limit = max(2 * TIMEFRAME_SECONDS[timeframe] + 900, 1800)
    reference = max(series.candles[-1].t, series.last_trade_at or 0)
    return now - reference <= limit


def build_chart(source: MarketDataSource, symbol: str, timeframe: str, now: Callable[[], float] = time.time) -> ChartData:
    if timeframe not in TIMEFRAMES:
        raise MarketError("bad_timeframe", 400)
    series = source.history(symbol, timeframe)
    moment = int(now())
    open_now = market_is_open(series, timeframe, moment)

    candles = series.candles
    last_price = candles[-1].c if candles else None
    previous_close = _previous_close(source, series, timeframe)
    change = change_pct = None
    if last_price is not None and previous_close:
        change = last_price - previous_close
        change_pct = change / previous_close * 100

    age = moment - series.last_trade_at if series.last_trade_at else None
    delayed = bool(open_now and age is not None and age > DELAY_THRESHOLD_SECONDS and timeframe != "1D")

    range_ = _opening_range(source, series, timeframe)
    visible = candles[-VISIBLE_BARS[timeframe]:]
    return ChartData(
        symbol=series.symbol, name=series.name, kind=series.kind, exchange=series.exchange, currency=series.currency,
        timezone=series.timezone, timeframe=timeframe, candles=visible, last_price=last_price,
        previous_close=previous_close, change=change, change_pct=change_pct, market_open=open_now,
        last_trade_at=series.last_trade_at, data_age_seconds=age, delayed=delayed, opening_range=range_,
        source=series.source, stale=series.stale, fetched_at=series.fetched_at,
    )


def _previous_close(source: MarketDataSource, series: Series, timeframe: str) -> float | None:
    """Close of the daily bar before the last one, so the change means the same on every timeframe. Providers
    report their own "previous close" with different meanings (24 hours ago for crypto, the settlement for futures),
    so it is only the fallback when daily bars are not available."""
    daily: Series | None = series
    if timeframe != "1D":
        try:
            daily = source.history(series.symbol, "1D")
        except MarketError:
            daily = None
    if daily is not None and len(daily.candles) >= 2:
        return daily.candles[-2].c
    if series.previous_close is not None:
        return series.previous_close
    return _previous_day_close(series) if series.candles and timeframe != "1D" else None


def _previous_day_close(series: Series) -> float | None:
    tz = zone(series.timezone, series.utc_offset_seconds)
    from datetime import datetime

    last_day = datetime.fromtimestamp(series.candles[-1].t, tz).date()
    for candle in reversed(series.candles):
        if datetime.fromtimestamp(candle.t, tz).date() < last_day:
            return candle.c
    return None


def _opening_range(source: MarketDataSource, series: Series, timeframe: str) -> OpeningRange | None:
    if timeframe == "1D":
        return None
    tz = zone(series.timezone, series.utc_offset_seconds)
    seconds = TIMEFRAME_SECONDS[timeframe]
    if opening.supports(seconds):
        return opening.opening_range(series.candles, seconds, series.kind, tz)
    try:
        finer = source.history(series.symbol, OPENING_FALLBACK_TIMEFRAME)
    except MarketError:
        return None  # the line is optional: without finer bars it is hidden, the chart still works
    return opening.opening_range(finer.candles, TIMEFRAME_SECONDS[OPENING_FALLBACK_TIMEFRAME], finer.kind, tz)

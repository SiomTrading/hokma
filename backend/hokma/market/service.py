"""Builds what the chart shows from a source's raw series: visible bars, last price, change, market state,
data delay and the opening high. Knows nothing about any particular provider.

Any time interval of `intervals.py` is built from one of the five bases a source serves (1m, 5m, 15m, 1h, 1D): the
coarsest whose length divides the interval exactly and whose bars start where the session of the instrument starts
(`sessions.py`), aggregated here. Days and weeks come from daily bars. Hourly bars are used for 4 hours and longer
(and for 1 hour itself); 2 and 3 hours come from 15-minute bars."""
import time
from collections.abc import Callable
from dataclasses import replace

from hokma.market import opening
from hokma.market.errors import BAD_RESPONSE, INTERVAL_UNAVAILABLE, SECONDS_NEED_TICKS, UPSTREAM, MarketError
from hokma.market.intervals import IntervalError, TimeInterval, of_seconds, parse_time, visible_bars
from hokma.market.models import Candle, ChartData, OpeningRange, Series
from hokma.market.sessions import Session, session_for
from hokma.market.source import MarketDataSource
from hokma.market.tz import zone

# The bases a source serves, coarsest first, with their length in seconds. "1D" is only for days and weeks.
BASES = (("1D", 86400), ("1h", 3600), ("15m", 900), ("5m", 300), ("1m", 60))
HOURLY_FROM = 4 * 3600                 # below this, hourly bars are only used for the 1h interval itself
ALIGNED_SHARE = 0.5                    # share of base bars that must start on the session grid; a few stray bars are fine
# A last trade older than this, while the market is open, is shown as delayed data.
DELAY_THRESHOLD_SECONDS = 300
# Bars of 5 minutes give the exact opening high for timeframes longer than the window itself.
OPENING_FALLBACK_TIMEFRAME = "5m"
OPENING_FALLBACK_SECONDS = 300


def market_is_open(series: Series, timeframe: str, now: int) -> bool:
    """Open when the source says the regular session covers `now` and, for intraday bars, the data is not stale.

    The session only says "current or next". After the close it points at the next open, so `now` falls outside."""
    if series.session is None or not series.candles:
        return False
    if not (series.session.start <= now < series.session.end):
        return False
    interval = _interval(timeframe)
    if interval.is_session_based:
        return True
    limit = max(2 * interval.seconds + 900, 1800)
    reference = max(series.candles[-1].t, series.last_trade_at or 0)
    return now - reference <= limit


def _interval(timeframe: str) -> TimeInterval:
    try:
        return parse_time(timeframe)
    except IntervalError:
        raise MarketError("bad_timeframe", 400) from None


def bases_for(interval: TimeInterval) -> list[str]:
    """Bases that can build `interval`, best first. Days and weeks only use daily bars; seconds have none."""
    if interval.unit == "s":
        return []
    if interval.is_session_based:
        return ["1D"]
    return [name for name, length in BASES[1:]
            if interval.seconds % length == 0 and (name != "1h" or interval.seconds == 3600 or interval.seconds >= HOURLY_FROM)]


def aggregate(candles: list[Candle], interval: TimeInterval, session: Session) -> list[Candle]:
    """Fold sorted bars into the bars of `interval`. A stray bar (a closing print after the last regular bar) joins the
    bar it falls in."""
    out: dict[int, Candle] = {}
    for candle in candles:
        start = session.bounds(candle.t, interval)[0]
        held = out.get(start)
        out[start] = (Candle(start, candle.o, candle.h, candle.l, candle.c, candle.v) if held is None else
                      Candle(start, held.o, max(held.h, candle.h), min(held.l, candle.l), candle.c, held.v + candle.v))
    return list(out.values())


def _aligned(candles: list[Candle], base_seconds: int, session: Session) -> bool:
    tail = candles[-400:]
    if not tail:
        return True
    base = of_seconds(base_seconds)
    on_grid = sum(1 for candle in tail if session.bounds(candle.t, base)[0] == candle.t)
    return on_grid >= ALIGNED_SHARE * len(tail)


def history_for(source: MarketDataSource, symbol: str, interval: TimeInterval) -> Series:
    """The series of `interval`: the source's own bars when it serves that length, else aggregated from a finer base."""
    bases = bases_for(interval)
    if not bases:
        raise MarketError(SECONDS_NEED_TICKS, 400)
    lengths = dict(BASES)
    for base in bases:
        try:
            raw = source.history(symbol, base)
        except MarketError as error:
            if error.code in (UPSTREAM, BAD_RESPONSE):
                continue                               # this source has no bars of that size for the symbol: next one
            raise
        if base == interval.canonical:
            return raw
        session = session_for(raw.kind, zone(raw.timezone, raw.utc_offset_seconds))
        if base != "1D" and not _aligned(raw.candles, lengths[base], session):
            continue
        return replace(raw, candles=aggregate(raw.candles, interval, session))
    raise MarketError(INTERVAL_UNAVAILABLE, 422, {"interval": interval.canonical, "base": bases[-1],
                                                  "source": getattr(source, "name", ""), "symbol": symbol})


def build_chart(source: MarketDataSource, symbol: str, timeframe: str, now: Callable[[], float] = time.time,
                keep: int | None = None) -> ChartData:
    """What the chart shows. `keep` is how many of the newest bars stay in `candles` (default: the count of the
    interval). Range and seconds charts ask for the 1-minute bars with a large `keep`: they build their own bars from
    them and from the ticks."""
    interval = _interval(timeframe)
    timeframe = interval.canonical
    series = history_for(source, symbol, interval)
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
    delayed = bool(open_now and age is not None and age > DELAY_THRESHOLD_SECONDS and not interval.is_session_based)

    range_ = _opening_range(source, series, interval)
    visible = candles[-(keep or visible_bars(timeframe)):]
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


def _opening_range(source: MarketDataSource, series: Series, interval: TimeInterval) -> OpeningRange | None:
    if interval.seconds >= 86400:
        return None
    tz = zone(series.timezone, series.utc_offset_seconds)
    if opening.supports(interval.seconds):
        return opening.opening_range(series.candles, interval.seconds, series.kind, tz)
    try:
        finer = source.history(series.symbol, OPENING_FALLBACK_TIMEFRAME)
    except MarketError:
        return None  # the line is optional: without finer bars it is hidden, the chart still works
    return opening.opening_range(finer.candles, OPENING_FALLBACK_SECONDS, finer.kind, tz)

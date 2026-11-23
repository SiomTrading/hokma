"""Bars (SPEC-017: TA-61, TA-75, OA-10, FF-025).

`bars()` takes its bars from `hokma.market`, the same origin as the chart (streaming when the provider streams). This
module opens no connection of its own for prices and never polls in a loop: one call to the market source per request.

What comes back is cut at `as_of` (only closed bars: a bar still forming is not the close), has the rows with a bad OHLC
excluded, is rejected when it is too old, and is never filled. A series that is delayed or historical is never presented
as live.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from hokma.data import freshness, quality, sentinels
from hokma.data.providers import BarRef, BarService, NoDataError, NotConfiguredError, ProviderError, RateLimitError, RawBars
from hokma.data.sentinels import DataResult, parse_time, utcnow
from hokma.quant.errors import BadInput

MARKET_PROVIDER = "hokma_market"
INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1D": 86400}
_ALIASES = {"1d": "1D", "1H": "1h", "1M": "1m", "d": "1D", "1day": "1D"}
COLUMNS = ("open", "high", "low", "close", "volume")


def normalize_interval(interval: str) -> str:
    key = _ALIASES.get(interval, interval)
    if key not in INTERVAL_SECONDS:
        raise BadInput(f"unsupported interval {interval!r}; expected one of {sorted(INTERVAL_SECONDS)}")
    return key


# ---------------------------------------------------------------------------------------------------- the market source
def market_fetcher(symbol: str, interval: str) -> RawBars:
    """The bar source behind `bars()`: the public API of `hokma.market`. One call, no connection of its own."""
    from hokma.market import api as market_api
    from hokma.market.errors import (BAD_RESPONSE, INTERVAL_UNAVAILABLE, NETWORK, RATE_LIMITED, UNKNOWN_SYMBOL, UPSTREAM,
                                     MarketError)

    try:
        series = market_api.get_source().history(symbol, interval)
    except MarketError as error:
        if error.code in (UNKNOWN_SYMBOL, INTERVAL_UNAVAILABLE):
            raise NoDataError(symbol, None, error.code) from error
        reason = sentinels.REASON_RATE_LIMITED if error.code == RATE_LIMITED else sentinels.REASON_UNREACHABLE
        if error.code not in (RATE_LIMITED, NETWORK, UPSTREAM, BAD_RESPONSE):
            reason = error.code
        raise RateLimitError(f"market source failed: {error.code}", reason=reason) from error
    rows = [{"time": pd.Timestamp(c.t, unit="s", tz="UTC"), "open": c.o, "high": c.h, "low": c.l, "close": c.c, "volume": c.v}
            for c in series.candles]
    frame = pd.DataFrame(rows, columns=["time", *COLUMNS]).set_index("time") if rows else pd.DataFrame(columns=COLUMNS)
    received = datetime.fromtimestamp(series.fetched_at, tz=sentinels.timezone.utc) if series.fetched_at else utcnow()
    return RawBars(frame, received, stale=bool(series.stale), meta={"market_source": series.source,
                                                                     "last_trade_at": series.last_trade_at})


DEFAULT_SERVICE = BarService({MARKET_PROVIDER: market_fetcher})


# ---------------------------------------------------------------------------------------------------- TA-61, TA-75, OA-10
def bars(symbol: str, interval: str, *, start: datetime, end: datetime, as_of: datetime,
         service: BarService | None = None, clock: Callable[[], datetime] = utcnow,
         max_stale_days: int = freshness.MAX_STALE_DAYS) -> DataResult:
    """OHLCV bars of `symbol` between `start` and `end`, as known at `as_of`.

    `symbol` is a plain symbol (served by `hokma.market`) or a bar id `provider|symbol`. The value is a DataFrame indexed by
    UTC bar start with open, high, low, close, volume. `meta` carries `freshness`, `live` (never true for a historical
    request, a delayed source or a stale cache), `excluded_rows` and the bar id.
    """
    key = normalize_interval(interval)
    cut, begin, stop, now = parse_time(as_of), parse_time(start), parse_time(end), clock()
    ref = BarRef.parse(symbol) if "|" in symbol else BarRef(MARKET_PROVIDER, symbol)
    bar_id = str(ref)
    warnings: list[str] = []
    if cut > now:
        warnings.append("as_of_in_future_clamped_to_now")
        cut = now
    stop = min(stop, cut)
    try:
        raw = (service or DEFAULT_SERVICE).fetch(ref, key)
    except NoDataError as error:
        return sentinels.no_data(ref.provider, error.detail or sentinels.REASON_EMPTY, as_of=cut, received_at=now, meta={"bar_id": bar_id})
    except RateLimitError as error:
        return sentinels.unavailable(ref.provider, error.reason, as_of=cut, received_at=now, warnings=(str(error),), meta={"bar_id": bar_id})
    except NotConfiguredError as error:
        return sentinels.unavailable(ref.provider, sentinels.REASON_KEY_MISSING, as_of=cut, received_at=now, warnings=(str(error),), meta={"bar_id": bar_id})
    frame = _shape(raw.frame)
    received = raw.received_at
    if frame.empty:
        return sentinels.no_data(ref.provider, sentinels.REASON_EMPTY, as_of=cut, received_at=received, meta={"bar_id": bar_id})
    frame, report = quality.drop_invalid_bars(frame)
    if report.excluded_rows:
        warnings.append(f"excluded_rows_with_invalid_ohlc:{report.excluded_rows}")
    if frame.empty:
        return sentinels.no_data(ref.provider, "all_rows_have_invalid_ohlc", as_of=cut, received_at=received,
                                 warnings=tuple(warnings), meta={"bar_id": bar_id, "quality": report.to_dict()})
    earliest = frame.index[0]
    closed = frame[frame.index.map(lambda stamp: _closes_at(stamp, key)) <= cut]
    if len(closed) < len(frame[frame.index <= cut]):
        warnings.append("partial_bar_excluded")
    window = closed[(closed.index >= begin) & (closed.index <= stop)]
    if window.empty:
        if earliest > stop:   # the source's coverage does not reach the window: not an absence, a gap
            return sentinels.unavailable(ref.provider, sentinels.REASON_NOT_OBSERVED, as_of=cut, received_at=received,
                                         warnings=tuple(warnings), meta={"bar_id": bar_id})
        return sentinels.no_data(ref.provider, "no_closed_bars_in_window", as_of=cut, received_at=received,
                                 warnings=tuple(warnings), meta={"bar_id": bar_id})
    if earliest > begin:
        warnings.append(f"coverage_starts_{earliest.date()}")  # a gap before the first bar: reported, never filled
    try:
        freshness.reject_stale(window, stop, symbol=ref.symbol, max_stale_days=max_stale_days)   # against the end of the window asked
    except NoDataError as error:
        return sentinels.stale(ref.provider, sentinels.REASON_STALE, as_of=cut, received_at=received,
                               warnings=(*warnings, error.detail), meta={"bar_id": bar_id})
    historical = cut < now - timedelta(seconds=INTERVAL_SECONDS[key] * 2)
    description = freshness.describe_bar_freshness(
        window.index[-1].isoformat(), historical, now=now, earliest=window.index[0].isoformat(),
        capability="delayed" if raw.stale else None)
    age = description.record_age_seconds
    live = (not historical) and (not raw.stale) and age is not None and age <= INTERVAL_SECONDS[key] * 2
    if raw.stale:
        warnings.append("served_from_old_cache")
    if not live:
        warnings.append("not_live")
    meta = {"bar_id": bar_id, "interval": key, "freshness": description.to_dict(), "live": live,
            "excluded_rows": report.excluded_rows, "quality": report.to_dict(), **raw.meta}
    return sentinels.ok(window, source=ref.provider, as_of=cut, received_at=received, warnings=tuple(warnings), meta=meta)


def _closes_at(stamp: pd.Timestamp, interval: str) -> pd.Timestamp:
    """When the bar that starts at `stamp` is closed. A daily bar is closed when its UTC day has ended."""
    if interval == "1D":
        return stamp.normalize() + pd.Timedelta(days=1)
    return stamp + pd.Timedelta(seconds=INTERVAL_SECONDS[interval])


def _shape(frame: Any) -> pd.DataFrame:
    """Standard frame: lower-case columns, UTC time index, sorted, one row per bar, no rows with an empty close."""
    out = frame.copy()
    out.columns = [str(c).lower() for c in out.columns]
    for name in COLUMNS:
        if name not in out.columns:
            out[name] = float("nan")
    out = out[list(COLUMNS)]
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index, utc=True)
    out.index = out.index.tz_localize("UTC") if out.index.tz is None else out.index.tz_convert("UTC")
    out.index.name = "time"
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


# ---------------------------------------------------------------------------------------------------- FF-025
def load_test_bars(source: str | Path, *, interval: str = "1D") -> DataResult:
    """Load bars from a CSV file or CSV text for tests and examples. The result is labelled `test_file`, carries
    `purpose=test_only` and is never to be mixed with production data, which comes from `bars()`."""
    text = Path(source).read_text(encoding="utf-8") if isinstance(source, Path) or ("\n" not in str(source) and Path(str(source)).exists()) else str(source)
    raw = pd.read_csv(io.StringIO(text))
    stamp = next((c for c in raw.columns if str(c).lower() in ("time", "date", "datetime", "timestamp")), None)
    if stamp is None:
        raise BadInput("the CSV needs a time, date, datetime or timestamp column")
    raw.index = pd.to_datetime(raw.pop(stamp), utc=True)
    frame = _shape(raw)
    if frame.empty:
        return sentinels.no_data("test_file", sentinels.REASON_EMPTY)
    return sentinels.ok(frame, source="test_file", as_of=frame.index[-1].to_pydatetime(warn=False),
                        warnings=("test_data_only",), meta={"purpose": "test_only", "interval": normalize_interval(interval)})

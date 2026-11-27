"""Bars for a backtest, through `hokma.data` (SPEC-020: B-16, N-39).

The only source of bars is `hokma.data.bars`: it applies the quality controls of `hokma.data.quality` (rows with a missing, not finite or
inconsistent OHLC are excluded and counted) and the retries of the providers live in `hokma.data.providers`; this module repeats
neither. A simulation must not run on repaired history, so what the data module excluded is turned into a failed load that says why
and on which date. Minutes with no trade stay missing; nothing is filled.

The strategy has no timeframe: any time interval of the chart (`7m`, `2h`, `1d`, `1w`) is a valid bar. The five lengths the data module serves (1m, 5m, 15m, 1h, 1D) are asked
as they are; any other is built like the charts do (`hokma.data.timeframe`) from the best base that divides it, with the session grid of the
instrument. How far back the data goes depends on the provider: when it does not reach, the run says so with the base used, the provider and the bars it got; it never
refuses to build the timeframe.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from hokma.backtest.catalog import Catalog
from hokma.data import ohlcv
from hokma.data.sentinels import DataResult
from hokma.i18n import t
from hokma.data.timeframe import SERVED, IntervalError, TimeInterval, bases_for, fold, parse_bar, session_of
from hokma.quant import limits
from hokma.quant.errors import BadInput

MIN_BUILT_BARS = 2                              # a built timeframe with fewer bars than this is "not enough data"


class DataFeedError(ValueError):
    """The bars cannot be used. `code` is stable: unsupported_bar, insufficient_data, no_data, data_unavailable, stale_data, invalid_ohlc,
    negative_volume. `at` is the date of the first bad bar when there is one."""

    def __init__(self, code: str, detail: str, at: str | None = None) -> None:
        self.code, self.at, self.detail = code, at, detail
        super().__init__(f"{code}{' at ' + at if at else ''}: {detail}")


@dataclass(frozen=True)
class LoadedBars:
    symbol: str
    bar: str
    frame: pd.DataFrame
    data_hash: str
    source: str
    warnings: tuple[str, ...]
    from_catalog: bool = False


def data_hash(frame: pd.DataFrame) -> str:
    """The fingerprint of the bars of a run: the same rows give the same hash on any machine (shortest round-trip text of each value)."""
    return hashlib.sha256(frame.to_csv().encode("utf-8")).hexdigest()


def market_symbol(symbol: str) -> str:
    """The symbol the market source understands: a share or an ETF canonical id (`EQ:AAPL`, `ETF:SPY`) is asked as its ticker. Any other id goes as it is
    (futures need a source that serves contracts; the market source answers unknown_symbol and the run says so)."""
    kind, _, rest = symbol.partition(":")
    return rest if kind in ("EQ", "ETF") and rest and ":" not in rest else symbol


def _default_fetch(symbol: str, interval: str, *, start: datetime, end: datetime, as_of: datetime) -> DataResult:
    import hokma.data as hokma_data

    return hokma_data.bars(market_symbol(symbol), interval, start=start, end=end, as_of=as_of)


def _checked(symbol: str, interval: str, *, start: datetime, end: datetime, as_of: datetime, fetch: Callable[..., DataResult] | None) -> tuple[pd.DataFrame, DataResult]:
    """The bars of one length the data module serves, with the controls of the feed. Raises `DataFeedError` as `load` documents."""
    try:
        result = (fetch or _default_fetch)(symbol, interval, start=start, end=end, as_of=as_of)
    except BadInput as error:
        raise DataFeedError("unsupported_bar", str(error)) from error
    quality = result.meta.get("quality") or {}
    if quality.get("excluded_rows"):
        raise DataFeedError("invalid_ohlc", f"{quality['excluded_rows']} bars with a bad {', '.join(quality.get('latest_excluded_fields') or ())} "
                            f"were excluded by the data module", quality.get("latest_excluded_record_at"))
    if not result.ok:
        raise DataFeedError(result.status.lower(), result.reason)
    frame = result.value
    if len(frame) * (len(frame.columns) + 1) > limits.MAX_INPUT_CELLS:
        raise BadInput("the bars of the request are above the limit of cells")
    negative = frame.index[frame["volume"] < 0]
    if len(negative):
        raise DataFeedError("negative_volume", f"{len(negative)} bars with a negative volume", negative[0].isoformat())
    return frame, result


def _interval(bar: str) -> TimeInterval:
    try:
        return parse_bar(bar)
    except IntervalError:
        raise DataFeedError("unsupported_bar", f"{bar!r} is not a time interval: use one such as 2m, 7m, 1h, 4h, 1d or 1w") from None


def _insufficient(timeframe: str, base: str, provider: str, bars: int, detail: str) -> DataFeedError:
    return DataFeedError("insufficient_data", t("backtest.insufficient_data", timeframe=timeframe, base=base, provider=provider, bars=bars, detail=detail).strip())


def load(symbol: str, bar: str, *, start: datetime, end: datetime, as_of: datetime,
         fetch: Callable[..., DataResult] | None = None, catalog: Catalog | None = None) -> LoadedBars:
    """The closed bars of `symbol` between `start` and `end` as known at `as_of`, in any time interval.

    With a `catalog`, bars already loaded for the same request are read from it and `hokma.data` is not called. Raises
    `DataFeedError` when the data module says there is no data, excluded bad rows, or a row has a negative volume. A length the data module does not
    serve is built from the best base the provider has; if no base gives enough bars the error is `insufficient_data` and says which base and provider.
    """
    interval = _interval(bar)
    key = catalog.key(symbol, interval.canonical, start, end, as_of) if catalog is not None else None
    if catalog is not None and key is not None:
        cached = catalog.get(key)
        if cached is not None:
            return LoadedBars(symbol, bar, cached, data_hash(cached), "catalog", (), True)
    if interval.canonical in SERVED:
        frame, result = _checked(symbol, interval.canonical, start=start, end=end, as_of=as_of, fetch=fetch)
        warnings: tuple[str, ...] = tuple(result.warnings)
    else:
        frame, result, warnings = _derived(symbol, interval, start=start, end=end, as_of=as_of, fetch=fetch)
    if catalog is not None and key is not None:
        catalog.put(key, frame)
    return LoadedBars(symbol, bar, frame, data_hash(frame), result.source, warnings)


def _derived(symbol: str, interval: TimeInterval, *, start: datetime, end: datetime, as_of: datetime,
             fetch: Callable[..., DataResult] | None) -> tuple[pd.DataFrame, DataResult, tuple[str, ...]]:
    """A time interval the data module does not serve, folded from the first base (best first) that gives enough bars."""
    bases = bases_for(interval)
    if not bases:
        raise _insufficient(interval.canonical, "none", "none", 0, "a length in seconds needs ticks, which the bar sources do not give")
    cut = min(end, as_of)
    last: DataFeedError | None = None
    seen = ("none", "none", 0)
    for base in bases:
        try:
            frame, result = _checked(symbol, base, start=start, end=end, as_of=as_of, fetch=fetch)
        except DataFeedError as error:
            if error.code in ("invalid_ohlc", "negative_volume", "unsupported_bar"):
                raise
            last = error
            seen = (base, "none", 0)
            continue
        built = fold(frame, interval, session_of(result.meta), int(cut.timestamp()))
        provider = str(result.meta.get("market_source") or result.source)
        if len(built) >= MIN_BUILT_BARS:
            note = f"built_from_{base}:{len(frame)}_bars_to_{len(built)}_{interval.canonical}"
            return built, result, (*result.warnings, note)
        seen = (base, provider, len(built))
    detail = f"{last.code}: {last.detail}" if last is not None else ""
    raise _insufficient(interval.canonical, seen[0], seen[1], seen[2], detail)

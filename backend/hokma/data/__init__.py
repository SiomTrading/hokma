"""Data module (SPEC-017, SVC-data): point in time data with source and received_at, never filled.

Every function returns a `DataResult` (or a `Snapshot` of them): the value, the source, when it was received, the `as_of` cut
it respects, a status (`ok`, `NO_DATA`, `DATA_UNAVAILABLE`, `STALE`) and a reason. A gap is a sentinel, never a number.
Provider keys come from Connections; a broker account identifier never enters a result. Prices come only from `hokma.market`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from hokma.data import sentinels
from hokma.data.sentinels import DataResult, utcnow

__all__ = ["bars", "snapshot", "fundamentals", "filings", "macro", "universe", "DataResult"]


def bars(symbol: str, interval: str, *, start: datetime, end: datetime, as_of: datetime) -> DataResult:
    """OHLCV bars from `hokma.market`, closed bars only, cut at `as_of`."""
    from hokma.data import ohlcv

    return ohlcv.bars(symbol, interval, start=start, end=end, as_of=as_of)


def fundamentals(symbol: str, *, as_of: datetime, statement: str, period: str) -> DataResult:
    """Fundamentals that were public on `as_of`, selected by filing date."""
    from hokma.data import fundamentals_src

    return fundamentals_src.fundamentals(symbol, as_of=as_of, statement=statement, period=period)


def filings(symbol: str, *, form: str, as_of: datetime, client: Any = None) -> DataResult:
    """SEC filings of a form that were public on `as_of`, newest first (delegates to `hokma.data.sec`). A filing filed after
    `as_of` never appears; without the saved SEC identity the answer is `DATA_UNAVAILABLE`. `client` is a `SecClient`; the
    default is one built from the saved identity."""
    try:
        from hokma.data.sec import SecClient
        from hokma.data.sec.filings import list_filings
    except ImportError as error:
        return sentinels.unavailable("sec_edgar", "sec_module_unavailable", as_of=as_of, received_at=utcnow(), warnings=(str(error)[:200],))
    found = list_filings(client or SecClient(), symbol, as_of=as_of, form=form)
    cut = sentinels.parse_time(found.as_of) if found.as_of is not None else sentinels.parse_time(as_of)
    return DataResult(found.value, found.source, found.received_at, cut, found.status, found.reason, tuple(found.warnings), {})


def __getattr__(name: str) -> Any:
    """`macro`, `universe` and `snapshot` are functions of the package AND modules of it: the module answers the call
    (`hokma.data.macro("CPIAUCSL", as_of=...)`), see `sentinels.make_module_callable`."""
    if name in ("macro", "universe", "snapshot"):
        import importlib

        return importlib.import_module(f"hokma.data.{name}")
    raise AttributeError(f"module 'hokma.data' has no attribute {name!r}")

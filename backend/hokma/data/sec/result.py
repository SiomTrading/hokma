"""Result type of the SEC module: the data module result (`DataResult`) with the SEC as its source.

Same fields and same rules for every data function: value, source, received_at, as_of, status, reason. A result that is not ok
carries no value and says why.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from hokma.data import sentinels
from hokma.data.sentinels import DATA_UNAVAILABLE, NO_DATA, OK, STALE, DataResult, parse_time, utcnow

SecResult = DataResult
SOURCE = "sec_edgar"

Clock = Callable[[], datetime]
utc_now = utcnow

__all__ = ["OK", "NO_DATA", "DATA_UNAVAILABLE", "STALE", "SOURCE", "SecResult", "Clock", "utc_now", "ok", "no_data", "unavailable", "stale"]


def _cut(as_of: datetime | date | None) -> datetime | None:
    return parse_time(as_of) if as_of is not None else None


def ok(value: Any, as_of: datetime | date | None, *, now: Clock = utcnow, warnings: tuple[str, ...] = ()) -> DataResult:
    return sentinels.ok(value, source=SOURCE, as_of=_cut(as_of), received_at=now(), warnings=tuple(warnings))


def no_data(reason: str, as_of: datetime | date | None, *, now: Clock = utcnow, warnings: tuple[str, ...] = ()) -> DataResult:
    return sentinels.no_data(SOURCE, reason, as_of=_cut(as_of), received_at=now(), warnings=tuple(warnings))


def unavailable(reason: str, as_of: datetime | date | None, *, now: Clock = utcnow) -> DataResult:
    return sentinels.unavailable(SOURCE, reason, as_of=_cut(as_of), received_at=now())


def stale(reason: str, as_of: datetime | date | None, *, now: Clock = utcnow) -> DataResult:
    return sentinels.stale(SOURCE, reason, as_of=_cut(as_of), received_at=now())

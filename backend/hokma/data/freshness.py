"""Freshness of data (SPEC-017: OA-58, TA-62, TA-63).

The age of the newest record is not the latency of the feed: sessions, bar boundaries and publication rules differ. This
module describes freshness without claiming a delay it cannot prove, rejects data that is too old, and decides whether a
cached download can still serve a request (a partial candle is not the close).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from hokma.data.providers import NoDataError
from hokma.data.sentinels import parse_time, to_utc, utcnow

MAX_STALE_DAYS = 10          # newest row this many calendar days before the request is stale (long holiday weekends fit)
CACHE_TTL_SECONDS = 900      # a same-day cache that does not reach the requested day is reused this long

_INSTANT = re.compile(r"T.*(?:Z|[+-]\d{2}:?\d{2})$", re.IGNORECASE)
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ZONE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$", re.IGNORECASE)


def _kind(value: str) -> str:
    if _INSTANT.search(value) and _parses(value):
        return "instant"
    return "date" if _DATE_ONLY.match(value) else "unknown"


def _parses(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class BarFreshness:
    latest_record_at: str | None
    earliest_record_at: str | None
    timestamp_kind: str                 # instant | date | unknown
    earliest_timestamp_kind: str
    latest_timezone: str | None
    earliest_timezone: str | None
    record_age_seconds: int | None
    historical: bool
    fetched_at: str
    delay_status: str                   # possible | unknown
    delay_basis: str                    # source_classification | insufficient_evidence | historical_request | no_records
    delay_estimated_seconds: None
    delay_explanation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def describe_bar_freshness(latest: str, historical: bool, *, now: datetime | None = None, earliest: str | None = None,
                           capability: str | None = None) -> BarFreshness:
    """Freshness of a series from the timestamps of its first and last record. `capability` is how the source is
    classified (`delayed` or `live`); it is a classification of the source, not a per-response declaration."""
    moment = to_utc(now) if now else utcnow()
    kind = _kind(latest) if latest else "unknown"
    age = (moment - parse_time(latest)).total_seconds() if kind == "instant" else None
    first = earliest if earliest is not None else latest
    first_kind = _kind(first) if first else "unknown"
    if not latest:
        basis = "no_records"
    elif historical:
        basis = "historical_request"
    elif capability == "delayed":
        basis = "source_classification"
    else:
        basis = "insufficient_evidence"
    if basis == "historical_request":
        explanation = "Historical request; record age is not a live delay signal."
    elif basis == "no_records":
        explanation = "No returned records to assess."
    else:
        prefix = ("This source is classified as potentially delayed; this is not a per-response provider declaration. "
                  if basis == "source_classification" else "")
        explanation = (prefix + "Record age is not feed latency. Trading sessions and bar timestamp boundaries are not "
                       "verified; actual delay is unknown.")
    return BarFreshness(
        latest_record_at=latest or None, earliest_record_at=first or None, timestamp_kind=kind,
        earliest_timestamp_kind=first_kind,
        latest_timezone=_ZONE.search(latest).group(1) if kind == "instant" else None,
        earliest_timezone=_ZONE.search(first).group(1) if first_kind == "instant" else None,
        record_age_seconds=int(age) if age is not None and age >= 0 else None, historical=historical,
        fetched_at=moment.isoformat(), delay_status="possible" if basis == "source_classification" else "unknown",
        delay_basis=basis, delay_estimated_seconds=None, delay_explanation=explanation,
    )


def _utc_stamp(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def latest_row_date(frame: Any) -> pd.Timestamp | None:
    """Newest date of a price frame, read from a `Date`/`Datetime` column or from the index. None when it has none."""
    if frame is None or len(frame) == 0:
        return None
    for column in ("Date", "Datetime", "date", "datetime", "time"):
        if column in getattr(frame, "columns", ()):
            parsed = pd.to_datetime(frame[column], errors="coerce", utc=True).dropna()
            return _utc_stamp(parsed.max()) if not parsed.empty else None
    if isinstance(frame.index, pd.DatetimeIndex):
        return _utc_stamp(frame.index.max())
    return None


def reject_stale(frame: Any, as_of: datetime | date | str, *, symbol: str, canonical: str | None = None,
                 max_stale_days: int = MAX_STALE_DAYS) -> None:
    """Raise `NoDataError` when the newest row is more than `max_stale_days` calendar days before `as_of`.

    This guards the dangerous case of present-but-old rows (a provider returning a year-old frame that would feed wrong
    prices to the agents). An empty frame is left to the caller's no-data handling.
    """
    latest = latest_row_date(frame)
    if latest is None:
        return
    requested = pd.Timestamp(parse_time(as_of)).normalize()
    newest = _utc_stamp(latest).normalize()
    stale_days = (requested - newest).days
    if stale_days > max_stale_days:
        raise NoDataError(symbol, canonical, f"latest row is {newest.date()}, {stale_days} days before the requested "
                                              f"{requested.date()} (stale); refusing to use it")


def cache_is_fresh(written_at: datetime, requested_day: date | datetime, now: datetime, *,
                   ttl_seconds: float = CACHE_TTL_SECONDS) -> bool:
    """Whether a cached download can serve a request.

    The file holds the download made on the day it was written, so it serves only that day. A request for the current day
    also refetches once the file is older than the TTL: providers publish a partial daily candle during the session, whose
    close is not the closing price, and inspecting rows cannot tell it from a final one.
    """
    written, current = to_utc(written_at), to_utc(now)
    if written.date() != current.date():
        return False
    requested = requested_day.date() if isinstance(requested_day, datetime) else requested_day
    return requested < current.date() or (current - written).total_seconds() <= ttl_seconds


def trading_days_between(start: date | str, end: date | str) -> int:
    """Weekdays between two dates (holidays are not known here, so a holiday inflates the gap by at most one: acceptable
    for a staleness signal, not for a calendar)."""
    a = parse_time(start).date() if not isinstance(start, date) else start
    b = parse_time(end).date() if not isinstance(end, date) else end
    days, cursor = 0, a
    while cursor < b:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days

"""Point-in-time rules (SPEC-017: TA-27, TA-64, TA-65, TA-66, TA-67).

The orchestrator fixes the date of a run; a model never chooses it. Everything dated after `as_of` is cut, a window the
source never observed is reported as not available (never as an absence), and profile endpoints that only serve today's
values are withheld from historical runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from hokma.data import sentinels
from hokma.data.sentinels import DataResult, parse_time, to_utc, utcnow

__all__ = ["validate_run_date", "in_window", "CoverageGap", "coverage_gap", "as_of", "as_of_window",
           "withhold_live_profile", "guard_live_profile", "filter_financials_by_date", "to_utc"]


def _today(today: date | str | None) -> str:
    if today is None:
        return utcnow().strftime("%Y-%m-%d")
    return today if isinstance(today, str) else today.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------------------------------- TA-27
def validate_run_date(value: Any, *, today: date | str | None = None) -> str:
    """The run date as a canonical `YYYY-MM-DD` text no later than today. Raises ValueError for a future date or any
    text that is not exactly that format (`2026-9-10`, `2026-09-10 00:00`, `Sept 10`, None)."""
    text = str(value)
    try:
        canonical = datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d") == text
    except ValueError:
        canonical = False
    if not canonical:
        raise ValueError(f"run date must be a date in YYYY-MM-DD format, got {value!r}")
    if text > _today(today):
        raise ValueError(f"run date cannot be in the future: {text}")
    return text


# ---------------------------------------------------------------------------------------------------- TA-65
def in_window(published: datetime | None, start: datetime, end: datetime, *, now: datetime | None = None) -> bool:
    """Whether an item belongs in the half-open window [start, end + 1 day). An undated item is kept only when the window
    reaches the present: in a historical window it cannot be proven that it is not from the future."""
    end_utc = to_utc(end)
    if published is not None:
        return to_utc(start) <= to_utc(published) < end_utc + timedelta(days=1)
    return end_utc >= (to_utc(now) if now else utcnow()) - timedelta(days=1)


@dataclass(frozen=True)
class CoverageGap:
    reason: str
    message: str


def coverage_gap(dates: Iterable[datetime | None], start_date: str, end_date: str, source: str, subject: str, *,
                 now: datetime | None = None) -> CoverageGap | None:
    """A gap record when the source did not observe the window, else None.

    Feeds that return their latest items whatever window is asked for would claim an absence nobody saw. A window is
    observed when coverage reaches its first day and it ends by today; only then is an empty result a real absence.
    `dates` are the timestamps of the returned items (plus the lookback start for a feed with a fixed lookback).
    """
    moment = to_utc(now) if now else utcnow()
    oldest = min((to_utc(d) for d in dates if d is not None), default=moment)
    if datetime.strptime(end_date, "%Y-%m-%d").date() > moment.date():
        reason = "the window extends past today"
    elif oldest.date() > datetime.strptime(start_date, "%Y-%m-%d").date():
        reason = f"it only serves recent items (coverage starts {oldest:%Y-%m-%d})"
    else:
        return None
    return CoverageGap(sentinels.REASON_NOT_OBSERVED,
                       f"<{source} unavailable for {start_date}..{end_date}: {reason}, so this is not an absence of {subject}>")


# ---------------------------------------------------------------------------------------------------- TA-66
def _parse(day: str | None) -> datetime | None:
    try:
        return datetime.strptime(day, "%Y-%m-%d")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def as_of(requested: str | None, run_date: str) -> str | None:
    """The date a tool serves: the requested date, but never later than the run's. A missing or malformed request becomes
    the run date; an empty `run_date` (a direct call outside a run) passes the request through."""
    if not run_date:
        return requested
    parsed, bound = _parse(requested), _parse(run_date)
    return requested if parsed is not None and bound is not None and parsed <= bound else run_date


def as_of_window(start_date: str, end_date: str, run_date: str) -> tuple[str, str]:
    """[start, end] with its end clamped to the run date. A window wholly after the run date keeps its length and moves
    back to end there."""
    end = as_of(end_date, run_date)
    start, old_end = _parse(start_date), _parse(end_date)
    if end == end_date or start is None or end is None or start <= _parse(end):  # type: ignore[operator]
        return start_date, end  # type: ignore[return-value]
    span = (old_end - start) if old_end is not None and old_end >= start else timedelta(0)
    return f"{_parse(end) - span:%Y-%m-%d}", end  # type: ignore[operator]


# ---------------------------------------------------------------------------------------------------- TA-67
def withhold_live_profile(run_as_of: str | None, label: str, *, today: date | str | None = None) -> str | None:
    """Notice to serve instead of a live-only company profile, or None when the profile may be served.

    Profile endpoints carry no historical vintage, not even name, sector and industry (companies rename and are
    reclassified), so serving one into a run dated in the past leaks information from after the decision.
    """
    if not run_as_of:
        return None
    current = _today(today)
    if run_as_of >= current:
        return None
    return (f"Profile for {label} withheld for {run_as_of}: the provider serves only present-day values ({current}) with no "
            f"historical vintage. Market cap, multiples, the 52-week range and the name, sector and industry reflect today. "
            f"Point-in-time figures come from the filed statements.")


def guard_live_profile(profile: Any, *, as_of: datetime, label: str, source: str, today: date | str | None = None,
                       clock=utcnow) -> DataResult:
    """The profile as an ok result for a live run; for an `as_of` in the past the profile is retained and the result says
    so in `warnings`."""
    notice = withhold_live_profile(to_utc(as_of).strftime("%Y-%m-%d"), label, today=today)
    if notice is None:
        return sentinels.ok(profile, source=source, as_of=as_of, received_at=clock())
    return sentinels.no_data(source, sentinels.REASON_LIVE_PROFILE, as_of=as_of, received_at=clock(), warnings=(notice,))


# ---------------------------------------------------------------------------------------------------- TA-64
def _filing_day(value: Any) -> date | None:
    if value is None or value == "":
        return None
    try:
        if hasattr(value, "to_pydatetime"):
            return value.to_pydatetime().date()
        return parse_time(value).date()
    except (ValueError, TypeError):
        return None


def filter_financials_by_date(records: Sequence[dict] | Any, as_of_time: datetime | str, *,
                              filing_field: str = "filing_date") -> Any:
    """Keep the statements that were public on `as_of`: those whose FILING date is on or before it.

    The fiscal period end is not the date the numbers became public (a quarter ends weeks before its report). A record
    without a filing date is excluded: it cannot be proven that it was known. Works on a list of dicts or on a DataFrame
    with a `filing_date` column and returns the same kind of object.
    """
    cutoff = parse_time(as_of_time).date()
    if hasattr(records, "columns"):
        if filing_field not in records.columns:
            return records.iloc[0:0]
        days = records[filing_field].map(_filing_day)
        return records[days.map(lambda d: d is not None and d <= cutoff)]
    return [r for r in records if (day := _filing_day(r.get(filing_field))) is not None and day <= cutoff]

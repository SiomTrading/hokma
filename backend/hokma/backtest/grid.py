"""The dates a desk runs on (SPEC-020: TA-80, AH-35).

`date_grid` is the grid of a backtest: every session of the calendar for DT and SWC, only the dates of the events for SWO. It never goes
past `as_of` or today and it does not depend on the data provider: the sessions come from the calendar of the contract
(`hokma.contracts.sessions`), so two providers of one symbol give the same grid. `rebalance_grid` is the grid of a rebalance, read from
the dates that the bars really have: a day with no bar moves the rebalance to the next bar, never to a date that was made up.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from hokma.backtest.spec import SpecError
from hokma.contracts import sessions
from hokma.desks.core.signal import LINES
from hokma.econ_calendar.events import CalendarResult, EconEvent

Cadence = Literal["weekly", "monthly", "event"]
DENSE_LINES = ("DT", "SWC")
EVENT_LINES = ("SWO",)
LINE_CADENCE: dict[str, Cadence] = {"SWC": "weekly", "SWO": "event", "LP": "monthly"}      # the rebalance of each line (AH-35)
MAX_DAYS = 20_000


def _day(value: Any) -> date:
    """A date or a datetime as a calendar date (a datetime with a time zone counts on its UTC date)."""
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc) if value.tzinfo is not None else value).date()
    if isinstance(value, date):
        return value
    raise SpecError("bad_spec: a grid date is a date or a datetime")


def session_days(root: str, start: date, end: date) -> tuple[date, ...]:
    """The days of the calendar of `root` between `start` and `end`: the weekdays that the exchange does not close for the whole day."""
    if end < start or (end - start).days > MAX_DAYS:
        raise SpecError(f"bad_spec: a grid spans at most {MAX_DAYS} days and ends after it starts")
    closed = sessions.holidays(root)
    return tuple(start + timedelta(days=n) for n in range((end - start).days + 1)
                 if (start + timedelta(days=n)).weekday() < 5 and (start + timedelta(days=n)) not in closed)


def date_grid(line: str, *, start: datetime | date, end: datetime | date, as_of: datetime | date, root: str = "NQ",
              events: Iterable[Any] = (), today: date | None = None, calendar: Iterable[Any] | None = None) -> tuple[date, ...]:
    """The dates of a backtest of `line`, in order, none after `as_of` or today.

    DT and SWC: every session of the calendar (a dense grid). SWO: only the dates of `events` (dates or datetimes, for example the scheduled
    times of `hokma.econ_calendar` or the report dates of the PEAD source) that fall on a session. Another line has no grid here.
    The calendar is the one of the exchange: `calendar` when the caller has the sessions (for example the session table of the data
    store), otherwise the weekdays that `hokma.contracts.sessions` does not close for `root`. No data provider enters the grid.
    """
    if line not in LINES:
        raise SpecError(f"bad_spec: unknown line {line!r}")
    first, last = _day(start), min(_day(end), _day(as_of), today or datetime.now(timezone.utc).date())
    if last < first:
        return ()
    days = session_days(root, first, last) if calendar is None else tuple(sorted({d for d in map(_day, calendar) if first <= d <= last}))
    if line in DENSE_LINES:
        return days
    if line in EVENT_LINES:
        wanted = {_day(e.scheduled_at if hasattr(e, "scheduled_at") else e) for e in events}
        return tuple(d for d in days if d in wanted)
    raise SpecError(f"bad_spec: the {line} line has no date grid: it rebalances on the bars (rebalance_grid)")


def calendar_events(answer: CalendarResult) -> tuple[EconEvent, ...]:
    """The events of an answer of the economic calendar (`hokma.econ_calendar.events.events`), ready for `date_grid(events=...)` or `rebalance_grid`.

    An answer that could not be verified (`UNVERIFIED_EVENT_RISK`: no source answered) is refused: an empty grid made of an unread calendar would look like
    a calendar with no events. `NO_EVENTS` is an answer, and gives no event."""
    if not answer.verified:
        raise SpecError(f"events_unverified: the economic calendar did not answer ({', '.join(answer.reasons) or 'no reason given'}); no event grid is built")
    return tuple(sorted(answer.events, key=lambda e: (e.scheduled_at, e.event_id)))


def gaps(dates: Iterable[date], bar_dates: Iterable[Any]) -> tuple[date, ...]:
    """The days of a grid that a provider has no bar for. They are reported to the caller and never filled."""
    have = {_day(d) for d in bar_dates}
    return tuple(d for d in dates if d not in have)


def _first_on_or_after(days: Sequence[date], nominal: date) -> date | None:
    lo, hi = 0, len(days)
    while lo < hi:
        mid = (lo + hi) // 2
        if days[mid] < nominal:
            lo = mid + 1
        else:
            hi = mid
    return days[lo] if lo < len(days) else None


def rebalance_grid(bar_dates: Iterable[Any], cadence: Cadence | None = None, *, line: str | None = None, events: Iterable[Any] = ()) -> tuple[date, ...]:
    """The days a rebalance happens, taken from the dates of the real bars. `cadence` is weekly (the Monday of each week), monthly (the first
    of each month) or event (the days of `events`); with no cadence the one of `line` is used: SWC weekly, SWO by event, LP monthly.

    A nominal day that has no bar moves to the next bar, so every date returned is a date of the bars. A nominal day before the first bar,
    or after the last, is dropped; two nominal days that land on the same bar give one rebalance.
    """
    chosen = cadence or LINE_CADENCE.get(line or "")
    if chosen is None:
        raise SpecError("bad_spec: a rebalance needs a cadence or one of the lines SWC, SWO, LP")
    days = sorted({_day(d) for d in bar_dates})
    if not days:
        return ()
    if chosen == "weekly":
        nominal = [days[0] - timedelta(days=days[0].weekday()) + timedelta(weeks=n)
                   for n in range(((days[-1] - days[0]).days // 7) + 2)]
    elif chosen == "monthly":
        nominal, year, month = [], days[0].year, days[0].month
        while date(year, month, 1) <= days[-1]:
            nominal.append(date(year, month, 1))
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    elif chosen == "event":
        nominal = sorted({_day(e.scheduled_at if hasattr(e, "scheduled_at") else e) for e in events})
    else:
        raise SpecError(f"bad_spec: unknown cadence {chosen!r}")
    out: list[date] = []
    for day in nominal:
        if day < days[0]:
            continue
        found = _first_on_or_after(days, day)
        if found is not None and found not in out:
            out.append(found)
    return tuple(out)


__all__ = ["date_grid", "gaps", "rebalance_grid", "session_days"]

"""Schedule expressions and the next run time. Pure functions, no database.

kind       expr                              meaning
once       2026-11-06T07:30                  one time, local time of `timezone`
daily      07:30                             every day
weekly     mon,wed,fri 07:30 | mon-fri 07:30 the listed weekdays
interval   30m | 2h | 1d                     every so often, counted from the last run
`cron` exists in the database but is not offered by the app.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KINDS = ("once", "daily", "weekly", "interval")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MIN_INTERVAL = timedelta(minutes=5)
MAX_INTERVAL = timedelta(days=31)
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_INTERVAL = re.compile(r"^(\d{1,4})([mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


class ScheduleError(ValueError):
    """`code` is translated by the API layer."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ScheduleError("timezone_invalid", name) from exc


def _time(text: str) -> tuple[int, int]:
    match = _TIME.match(text.strip())
    if not match:
        raise ScheduleError("schedule_invalid", text)
    return int(match.group(1)), int(match.group(2))


def _days(text: str) -> list[int]:
    chosen: set[int] = set()
    for part in text.lower().split(","):
        part = part.strip()
        if "-" in part:
            first, _, last = part.partition("-")
            if first not in DAYS or last not in DAYS:
                raise ScheduleError("schedule_invalid", part)
            a, b = DAYS.index(first), DAYS.index(last)
            chosen.update(range(a, b + 1) if a <= b else list(range(a, 7)) + list(range(0, b + 1)))
        elif part in DAYS:
            chosen.add(DAYS.index(part))
        else:
            raise ScheduleError("schedule_invalid", part)
    if not chosen:
        raise ScheduleError("schedule_invalid", text)
    return sorted(chosen)


def _interval(text: str) -> timedelta:
    match = _INTERVAL.match(text.strip().lower())
    if not match:
        raise ScheduleError("schedule_invalid", text)
    delta = timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})
    if delta < MIN_INTERVAL or delta > MAX_INTERVAL:
        raise ScheduleError("interval_out_of_range", text)
    return delta


def _once(text: str, tz: ZoneInfo) -> datetime:
    try:
        naive = datetime.strptime(text.strip(), "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ScheduleError("schedule_invalid", text) from exc
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


def normalize(kind: str, expr: str, tz_name: str) -> str:
    """The canonical expression, or ScheduleError."""
    if kind not in KINDS:
        raise ScheduleError("kind_invalid", kind)
    tz = zone(tz_name)
    expr = (expr or "").strip()
    if kind == "once":
        _once(expr, tz)
        return expr
    if kind == "daily":
        hour, minute = _time(expr)
        return f"{hour:02d}:{minute:02d}"
    if kind == "weekly":
        days_text, _, time_text = expr.rpartition(" ")
        hour, minute = _time(time_text)
        days = _days(days_text)
        return f"{','.join(DAYS[d] for d in days)} {hour:02d}:{minute:02d}"
    delta = _interval(expr)
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 1440}d" if minutes % 1440 == 0 else f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"


def next_run(kind: str, expr: str, tz_name: str, after: datetime) -> datetime | None:
    """First run strictly after `after` (aware, any zone), in UTC. None when a `once` job is in the past."""
    tz = zone(tz_name)
    after = after.astimezone(timezone.utc)
    if kind == "once":
        moment = _once(expr, tz)
        return moment if moment > after else None
    if kind == "interval":
        return after + _interval(expr)
    local_after = after.astimezone(tz)
    if kind == "daily":
        hour, minute = _time(expr)
        days, allowed = 2, None
    elif kind == "weekly":
        days_text, _, time_text = expr.rpartition(" ")
        hour, minute = _time(time_text)
        days, allowed = 9, set(_days(days_text))
    else:
        raise ScheduleError("kind_invalid", kind)
    for offset in range(days):
        day = (local_after + timedelta(days=offset)).date()
        if allowed is not None and day.weekday() not in allowed:
            continue
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).astimezone(timezone.utc)
        if candidate > after:
            return candidate
    return None

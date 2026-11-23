"""Schedule expressions and the next run time. Pure functions, no database.

kind       expr                              meaning
once       2026-11-06T07:30                  one time, local time of `timezone` (an offset or Z in the text wins)
at         2026-11-06T07:30:00-05:00         same as `once`; stored as `once`
daily      07:30                             every day
weekly     mon,wed,fri 07:30 | mon-fri 07:30 the listed weekdays
interval   30m | 2h | 1d | 5m30s             every so often, counted from the last run (at least 5 minutes)
every      1h30m                             same as `interval`; stored as `interval`
cron       30 9 * * mon-fri                  five fields (minute hour day month weekday) in the IANA zone of the job

`at` and `every` are the names agents use; the database keeps `once` and `interval` (`stored_kind`). `parse_expression` reads
one line, `<kind> <expr> [tz=<IANA zone>]`.

Daylight saving, for every kind that names a clock time:
  - a time the clock skips (02:30 on the day the clocks go forward) runs once, at the same instant the offset before the change
    gives (03:30 local that day);
  - a time the clock repeats (01:30 on the day the clocks go back) runs once, the first time it happens.
A cron that can fire more often than every 5 minutes is refused (`interval_out_of_range`); a cron that is not five valid fields,
or that never fires (31 February), is `schedule_invalid`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KINDS = ("once", "daily", "weekly", "interval", "cron", "at", "every")
STORED_KINDS = ("once", "daily", "weekly", "interval", "cron")
_ALIASES = {"at": "once", "every": "interval"}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MIN_INTERVAL = timedelta(minutes=5)
MAX_INTERVAL = timedelta(days=31)
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_INTERVAL = re.compile(r"^(?:\d{1,4}[dhms])+$")
_INTERVAL_PART = re.compile(r"(\d{1,4})([dhms])")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
_ONCE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?$")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
CRON_HORIZON_DAYS = 366 * 9                      # far enough for a 29 February
CRON_GAP_SAMPLES = 50


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


def stored_kind(kind: str) -> str:
    """The kind the database keeps: `at` is `once` and `every` is `interval`."""
    if kind not in KINDS:
        raise ScheduleError("kind_invalid", str(kind))
    return _ALIASES.get(kind, kind)


def _interval(text: str) -> timedelta:
    clean = text.strip().lower()
    if not _INTERVAL.match(clean):
        raise ScheduleError("schedule_invalid", text)
    delta = sum((timedelta(**{_UNITS[unit]: int(number)}) for number, unit in _INTERVAL_PART.findall(clean)), timedelta())
    if delta < MIN_INTERVAL or delta > MAX_INTERVAL:
        raise ScheduleError("interval_out_of_range", text)
    return delta


def _once(text: str, tz: ZoneInfo) -> datetime:
    raw = text.strip()
    if not _ONCE.match(raw):
        raise ScheduleError("schedule_invalid", text)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduleError("schedule_invalid", text) from exc
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)).astimezone(timezone.utc)


# ---------------------------------------------------------------------------------------------- cron

@dataclass(frozen=True)
class Cron:
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]             # 0 is Sunday (7 is read as 0)
    day_star: bool                       # the day field starts with * (then only the weekday decides, and the reverse)
    weekday_star: bool

    def day_matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        in_month, in_week = day.day in self.days, (day.weekday() + 1) % 7 in self.weekdays
        return (in_month or in_week) if not self.day_star and not self.weekday_star else (in_month and in_week)


def _cron_field(text: str, low: int, high: int, names: tuple[str, ...] = (), first_name: int = 0) -> set[int]:
    def number(token: str) -> int:
        token = token.lower()
        if token in names:
            return names.index(token) + first_name
        if not token.isdigit() or len(token) > 2:
            raise ScheduleError("schedule_invalid", text)
        return int(token)

    chosen: set[int] = set()
    for part in text.split(","):
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError("schedule_invalid", text)
            step = int(step_text)
        if base == "*":
            first, last = low, high
        elif "-" in base:
            a, _, b = base.partition("-")
            first, last = number(a), number(b)
        else:
            first = number(base)
            last = high if slash else first
        if not low <= first <= high or not low <= last <= high or first > last:
            raise ScheduleError("schedule_invalid", text)
        chosen.update(range(first, last + 1, step))
    if not chosen:
        raise ScheduleError("schedule_invalid", text)
    return chosen


def parse_cron(expr: str) -> Cron:
    """Five fields: minute hour day-of-month month day-of-week. Lists, ranges, steps, and names for months and weekdays."""
    fields = (expr or "").split()
    if len(fields) != 5 or len(expr) > 100:
        raise ScheduleError("schedule_invalid", expr)
    minute, hour, day, month, weekday = fields
    weekdays = {d % 7 for d in _cron_field(weekday, 0, 7, DAYS[6:] + DAYS[:6], 0)}
    return Cron(tuple(sorted(_cron_field(minute, 0, 59))), tuple(sorted(_cron_field(hour, 0, 23))),
                frozenset(_cron_field(day, 1, 31)), frozenset(_cron_field(month, 1, 12, MONTHS, 1)), frozenset(weekdays),
                day.startswith("*"), weekday.startswith("*"))


def _cron_next(cron: Cron, tz: ZoneInfo, after: datetime) -> datetime | None:
    """The first instant strictly after `after` (UTC) at which the cron fires in `tz`, or None inside the horizon."""
    start = after.astimezone(tz).date() - timedelta(days=1)
    for offset in range(CRON_HORIZON_DAYS):
        day = start + timedelta(days=offset)
        if not cron.day_matches(day):
            continue
        found = [moment for moment in (datetime(day.year, day.month, day.day, h, m, tzinfo=tz).astimezone(timezone.utc)
                                       for h in cron.hours for m in cron.minutes) if moment > after]
        if found:
            return min(found)
    return None


def _cron_checked(expr: str, tz: ZoneInfo) -> str:
    """The canonical text of a cron that fires, and not more often than every MIN_INTERVAL."""
    cron = parse_cron(expr)
    moment = _cron_next(cron, tz, datetime.now(timezone.utc))
    if moment is None:
        raise ScheduleError("schedule_invalid", expr)                  # for example 31 February: it never fires
    for _ in range(CRON_GAP_SAMPLES):
        following = _cron_next(cron, tz, moment)
        if following is None:
            break
        if following - moment < MIN_INTERVAL:
            raise ScheduleError("interval_out_of_range", expr)
        moment = following
    return " ".join(expr.split()).lower()


# ---------------------------------------------------------------------------------------------- one line

@dataclass(frozen=True)
class Parsed:
    kind: str                            # the kind the database keeps
    expr: str                            # canonical
    timezone: str


def parse_expression(text: str, default_timezone: str = "UTC") -> Parsed:
    """`<kind> <expr> [tz=<IANA zone>]`, for example `cron 30 9 * * mon-fri tz=America/New_York` or `every 2h` or
    `at 2026-11-06T07:30`. The kind is one of KINDS. Raises ScheduleError (kind_invalid, schedule_invalid, timezone_invalid...)."""
    tokens = (text or "").split()
    if len(tokens) < 2 or len(text) > 200:
        raise ScheduleError("schedule_invalid", text)
    tz_name = default_timezone
    if tokens[-1].lower().startswith("tz="):
        tz_name = tokens.pop()[3:]
    kind, expr = tokens[0].lower(), " ".join(tokens[1:])
    return Parsed(stored_kind(kind), normalize(kind, expr, tz_name), zone(tz_name).key)


def normalize(kind: str, expr: str, tz_name: str) -> str:
    """The canonical expression, or ScheduleError."""
    if kind not in KINDS:
        raise ScheduleError("kind_invalid", kind)
    tz = zone(tz_name)
    expr = (expr or "").strip()
    kind = _ALIASES.get(kind, kind)
    if kind == "cron":
        return _cron_checked(expr, tz)
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
    seconds = int(delta.total_seconds())
    if seconds % 60:
        return f"{seconds // 60}m{seconds % 60}s"
    minutes = seconds // 60
    return f"{minutes // 1440}d" if minutes % 1440 == 0 else f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"


def next_run(kind: str, expr: str, tz_name: str, after: datetime) -> datetime | None:
    """First run strictly after `after` (aware, any zone), in UTC. None when a `once` job is in the past."""
    tz = zone(tz_name)
    after = after.astimezone(timezone.utc)
    kind = _ALIASES.get(kind, kind)
    if kind == "cron":
        return _cron_next(parse_cron(expr), tz, after)
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

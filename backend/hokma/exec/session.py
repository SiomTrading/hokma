"""The session of an account (SPEC-026, piece 3390c, section 4.4). The day of a loss is the SESSION of the account, not the UTC date: a futures
account starts its day at 17:00 in Chicago, an account of shares and options at the regular open (09:30 in New York). Daylight saving time
is the one of the zone. A session is named by the local date of its start, so the key changes exactly when the session does."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

CALENDARS: dict[str, tuple[str, time]] = {
    "cme": ("America/Chicago", time(17, 0)),
    "us_equity": ("America/New_York", time(9, 30)),
}
DEFAULT_CALENDAR = "us_equity"
FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Session:
    calendar: str
    key: str
    started_at: datetime        # UTC
    ends_at: datetime           # UTC: when the next session opens

    @property
    def started(self) -> str:
        return self.started_at.strftime(FORMAT)

    @property
    def ends(self) -> str:
        return self.ends_at.strftime(FORMAT)


def _at(zone: ZoneInfo, day, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=zone)


def session_at(calendar: str, now: datetime) -> Session:
    """The session that contains `now` (aware or naive UTC)."""
    if calendar not in CALENDARS:
        raise ValueError(f"unknown calendar {calendar!r}")
    name, clock = CALENDARS[calendar]
    zone = ZoneInfo(name)
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    local = now.astimezone(zone)
    start_day = local.date() if local >= _at(zone, local.date(), clock) else local.date() - timedelta(days=1)
    start = _at(zone, start_day, clock)
    end = _at(zone, start_day + timedelta(days=1), clock)
    return Session(calendar, f"{calendar}:{start_day.isoformat()}", start.astimezone(timezone.utc), end.astimezone(timezone.utc))


def calendar_for_assets(asset_types) -> str:
    """Futures and CFDs run on the CME session; everything else on the equity session."""
    return "cme" if any(t in ("future", "cfd") for t in asset_types) else DEFAULT_CALENDAR

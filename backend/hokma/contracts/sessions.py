"""Trading hours of the futures Hokma operates, for the history (SPEC-020: L-03).

The table is `data/market_hours.json`: per schedule, the weekly segments in the exchange time zone (regular and extended), the
full day closures, the early closes and the late opens. A holiday closes the whole date, an early close ends the day at its time and a
late open starts it at its time (with both, the exchange closes early and reopens late). The bank holidays are the dates that the expiration rules count as non-business days, and `holidays` is that calendar: a product that trades on a bank holiday
(the equity index on Columbus Day, Veterans Day and the morning of Thanksgiving, checked against the real daily sessions of NQ) has no such date in its
schedule. The daily halt (17:00 to 18:00 ET) is simply the hour with no segment. The regular session is the
`market` segment of the table; the extended session adds `premarket` and `postmarket`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from zoneinfo import ZoneInfo

from hokma.contracts.identity import NoContract

HOURS_PATH = Path(__file__).with_name("data") / "market_hours.json"
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
State = Literal["closed", "extended", "regular"]
Session = Literal["ETH", "RTH"]


@dataclass(frozen=True)
class Schedule:
    tz: str
    week: tuple[tuple[tuple[int, int, str], ...], ...]      # per weekday (Monday first): (start minute, end minute, state)
    holidays: frozenset[date]
    bank_holidays: frozenset[date]
    no_regular_session: frozenset[date]            # days whose regular session is cut away entirely by an early close or a late open
    early_closes: "MappingProxyType[date, int]"
    late_opens: "MappingProxyType[date, int]"


def _minutes(text: str) -> int:
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def _days(values: list[str]) -> frozenset[date]:
    return frozenset(date.fromisoformat(v) for v in values)


@lru_cache(maxsize=1)
def _table() -> tuple[dict[str, Schedule], dict[str, str]]:
    raw = json.loads(HOURS_PATH.read_text(encoding="utf-8"))
    schedules = {}
    for key, s in raw["schedules"].items():
        week = tuple(tuple((_minutes(a), _minutes(b), state) for a, b, state in s["week"][day]) for day in DAYS)
        early = {date.fromisoformat(k): _minutes(v) for k, v in s["early_closes"].items()}
        late = {date.fromisoformat(k): _minutes(v) for k, v in s["late_opens"].items()}
        cut = frozenset(d for d in early.keys() | late.keys()
                        if not any(max(a, lo) < min(b, hi) for a, b, kind in week[d.weekday()] if kind == "market"
                                   for lo, hi in _allowed(early.get(d), late.get(d), _regular_end(week[d.weekday()]))))
        schedules[key] = Schedule(s["exchange_tz"], week, _days(s["holidays"]), _days(s["bank_holidays"]), cut,
                                  MappingProxyType(early), MappingProxyType(late))
    return schedules, dict(raw["roots"])


def schedule(root: str) -> Schedule:
    """The schedule of a future root. Raises NoContract for a root the table does not carry."""
    schedules, roots = _table()
    key = roots.get(str(root).upper())
    if key is None:
        raise NoContract(str(root)[:80], "no trading hours for this root")
    return schedules[key]


def holidays(root: str) -> frozenset[date]:
    """Dates that the exchange does not count as business days: full closures, bank holidays and days with no regular session. It is the
    calendar of the expirations."""
    s = schedule(root)
    return s.holidays | s.bank_holidays | s.no_regular_session


def _allowed(early: int | None, late: int | None, regular_end: int) -> tuple[tuple[int, int], ...]:
    """The minutes of a day that an early close and a late open leave open. With both, the exchange closes early and reopens late; an
    early close at or after the end of the regular session is the usual close, so the day is closed until the late open."""
    if early is None and late is None:
        return ((0, 1440),)
    if late is None:
        return ((0, early),)
    if early is None:
        return ((late, 1440),)
    if late < early:
        return ((late, early),)
    return ((late, 1440),) if early >= regular_end else ((0, early), (late, 1440))


def _regular_end(day: tuple[tuple[int, int, str], ...]) -> int:
    return max((end for _, end, kind in day if kind == "market"), default=0)


def state(root: str, at: datetime) -> State:
    """`regular`, `extended` or `closed` at an instant. `at` must carry a time zone."""
    if at.tzinfo is None:
        raise ValueError("at must carry a time zone")
    s = schedule(root)
    local = at.astimezone(ZoneInfo(s.tz))
    day = local.date()
    if day in s.holidays:
        return "closed"
    now = local.hour * 60 + local.minute + local.second / 60
    segments = s.week[local.weekday()]
    allowed = _allowed(s.early_closes.get(day), s.late_opens.get(day), _regular_end(segments))
    for start, end, kind in segments:
        if start <= now < end and any(a <= now < b for a, b in allowed):
            return "regular" if kind == "market" else "extended"
    return "closed"


def is_open(root: str, at: datetime, *, session: Session | None = None) -> bool:
    """Whether the market takes orders at `at`. `session="RTH"` counts only the regular session; `"ETH"` or None counts both."""
    if session not in (None, "ETH", "RTH"):
        raise ValueError("session is ETH, RTH or None")
    found = state(root, at)
    return found == "regular" if session == "RTH" else found != "closed"

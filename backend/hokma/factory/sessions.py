"""Trading session filters (SPEC-020: V-25).

The schedule is a parameter: the caller takes it from the contracts module or from the data provider. RTH is the regular session.
ETH is the whole electronic session, so it excludes only the daily break, the weekend and the closed days. A bar belongs to the
session of its open time.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from hokma.factory.orders import OrderIntent
from hokma.quant.limits import check_inputs

Session = Literal["ETH", "RTH"]
SESSIONS = ("ETH", "RTH")


class BadSession(ValueError):
    code = "bad_session"


def _seconds(value: time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


@dataclass(frozen=True)
class SessionSchedule:
    tz: str                                   # IANA name of the exchange clock
    rth_open: time
    rth_close: time
    break_start: time | None = None           # daily break of the electronic session
    break_end: time | None = None
    holidays: frozenset[date] = frozenset()   # local dates with no trading at all
    early_closes: Mapping[date, time] = field(default_factory=dict)   # local dates that close earlier (RTH and ETH)

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.tz)
        except Exception:
            raise BadSession(f"unknown time zone {self.tz!r}") from None
        if not self.rth_open < self.rth_close:
            raise BadSession("rth_open must be before rth_close")
        if (self.break_start is None) != (self.break_end is None):
            raise BadSession("a break needs both break_start and break_end")
        if self.break_start is not None and self.break_end is not None and not self.break_start < self.break_end:
            raise BadSession("break_start must be before break_end")
        for day, close in self.early_closes.items():
            if not self.rth_open < close < self.rth_close:
                raise BadSession(f"the early close of {day} must be inside the regular session")


def session_mask(index: pd.DatetimeIndex, schedule: SessionSchedule, session: Session) -> np.ndarray:
    """True for the bars (by open time) inside the session. The index must carry a time zone."""
    if session not in SESSIONS:
        raise BadSession(f"session is ETH or RTH, got {session!r}")
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise BadSession("the bar times must carry a time zone")
    check_inputs({"index": index})
    local = index.tz_convert(schedule.tz)
    sec = (local.hour * 3600 + local.minute * 60 + local.second).to_numpy()
    weekday = local.day_of_week.to_numpy()
    days = local.tz_localize(None).normalize().to_numpy("datetime64[D]")
    closed_day = np.isin(days, np.array(sorted(schedule.holidays), dtype="datetime64[D]")) if schedule.holidays else np.zeros(len(sec), bool)
    rth_close = np.full(len(sec), _seconds(schedule.rth_close))
    early_start = np.full(len(sec), -1)
    for day, close in schedule.early_closes.items():
        hit = days == np.datetime64(day, "D")
        rth_close[hit] = _seconds(close)
        early_start[hit] = _seconds(close)
    if session == "RTH":
        return (~closed_day) & (weekday < 5) & (sec >= _seconds(schedule.rth_open)) & (sec < rth_close)
    if schedule.break_start is None or schedule.break_end is None:
        return (~closed_day) & (weekday < 5)
    brk_start = np.where(early_start >= 0, early_start, _seconds(schedule.break_start))
    brk_end = _seconds(schedule.break_end)
    in_break = (sec >= brk_start) & (sec < brk_end)
    friday_closed = (weekday == 4) & (sec >= brk_start)
    sunday_closed = (weekday == 6) & (sec < brk_end)
    return (~closed_day) & (weekday != 5) & ~in_break & ~friday_closed & ~sunday_closed


def in_session(moment: datetime, schedule: SessionSchedule, session: Session) -> bool:
    """True when a bar that opens at `moment` is inside the session."""
    return bool(session_mask(pd.DatetimeIndex([moment]), schedule, session)[0])


def filter_session(frame: pd.DataFrame, schedule: SessionSchedule, session: Session) -> pd.DataFrame:
    """The rows of a bar frame that belong to the session."""
    return frame[session_mask(frame.index, schedule, session)]  # type: ignore[arg-type]


def session_gate(intents: Sequence[OrderIntent], moment: datetime, schedule: SessionSchedule,
                 session: Session | None) -> tuple[tuple[OrderIntent, ...], tuple[str, ...]]:
    """What a strategy with `session` set may send at `moment`: exits always, entries only inside the session."""
    if session is None or in_session(moment, schedule, session):
        return tuple(intents), ()
    kept = tuple(i for i in intents if i.reduce_only)
    dropped = len(intents) - len(kept)
    return kept, ((f"entry_outside_{session.lower()}:{dropped}",) if dropped else ())

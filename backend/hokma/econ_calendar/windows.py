"""Event windows of a desk: the time around a macro event in which it opens nothing (SPEC-018: CTS-045).

A window comes from the `event_windows` rules of the limits of the desk: `{match, minutes_before, minutes_after}`, where `match` is
a word of the event name. `windows(desk_id, at=...)` asks the calendar (`hokma.econ_calendar.events`); a calendar that cannot be
read gives ONE window with `unverified=True`, never an empty list (an empty answer is only the answer of a calendar that was read)."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class EventWindow:
    match: str
    event: str | None            # the name of the event, None when the window is the unverified one
    starts_at: datetime
    ends_at: datetime
    unverified: bool = False


def rules_of(limits: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = limits.get("event_windows") if isinstance(limits, Mapping) else None
    return [r for r in raw if isinstance(r, Mapping)] if isinstance(raw, list) else []


def windows(desk_id: int, *, at: datetime, limits: Mapping[str, Any] | None = None, db: Any = None) -> tuple[EventWindow, ...]:
    """The windows of a desk that are active at `at`. Desks with no `event_windows` rule have none."""
    if limits is None:
        from hokma.db import get_database

        with (db or get_database()).read() as u:
            limits = u.broker.get_limits(desk_id)
    rules = rules_of(limits)
    if not rules:
        return ()
    from hokma.econ_calendar import events as calendar

    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    widest = max(int(r.get("minutes_before", 0)) for r in rules), max(int(r.get("minutes_after", 0)) for r in rules)
    result = calendar.events(start=at - timedelta(minutes=widest[1]), end=at + timedelta(minutes=widest[0]), countries=["US"],
                             names=None, db=db)
    if result.status == "UNVERIFIED_EVENT_RISK":
        return (EventWindow("*", None, at, at, unverified=True),)
    out: list[EventWindow] = []
    for rule in rules:
        word = str(rule.get("match") or "").lower()
        before, after = timedelta(minutes=int(rule.get("minutes_before", 0))), timedelta(minutes=int(rule.get("minutes_after", 0)))
        for event in result.events:
            if word and word in event.name.lower() and event.scheduled_at - before <= at <= event.scheduled_at + after:
                out.append(EventWindow(word, event.name, event.scheduled_at - before, event.scheduled_at + after))
    return tuple(out)


def active(wins: Sequence[EventWindow], at: datetime) -> list[EventWindow]:
    return [w for w in wins if w.unverified or w.starts_at <= at <= w.ends_at]

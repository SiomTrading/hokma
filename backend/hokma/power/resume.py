"""Resume detection: a wall-clock gap between two scheduler ticks (15 s apart) of more than 2 minutes means the
computer slept or hibernated. Runs that fell due while it slept are then executed once each instead of skipped."""
from __future__ import annotations

from datetime import datetime, timedelta

JUMP = timedelta(minutes=2)
WINDOW = timedelta(hours=24)


def slept(previous: datetime | None, now: datetime) -> timedelta | None:
    """How late a run may be and still be executed after this tick: the whole gap (capped), or None when the tick
    was on time."""
    if previous is None or now - previous <= JUMP:
        return None
    return min(now - previous, WINDOW)

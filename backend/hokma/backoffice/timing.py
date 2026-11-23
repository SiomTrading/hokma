"""Seconds per analyst and the time budget of a line (SPEC-018: TA-22). The timer measures each analyst of a run; `flag` compares the
seconds with the budget of the line of the desk and names every analyst that went over. The result goes into the run record
(`runs.record(analyst_seconds=...)`), so the desk of the intraday line can watch the speed of its context."""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from hokma.db import Database, get_database
from hokma.exec.money import ZERO, dec, text

# Seconds an analyst may take, by line of the desk. A budget is a limit on time, not on money: nothing here moves an order.
BUDGET_SECONDS: dict[str, Decimal] = {"DT": Decimal(60), "SWC": Decimal(600), "SWO": Decimal(600), "RF": Decimal(900), "LP": Decimal(900)}
MAX_ANALYSTS = 64
_MILLI = Decimal("0.001")


class TimingError(ValueError):
    """A timer was used wrongly (an analyst stopped that never started, a name that is not plain text)."""


def _key(name: str) -> str:
    if not isinstance(name, str) or not 1 <= len(name) <= 60 or not name.replace("_", "").replace("-", "").isalnum():
        raise TimingError(f"analyst name {name!r} must be 1 to 60 letters, digits, hyphens or underscores")
    return name


class AnalystTimer:
    """Wall time of each analyst of one run. `clock` is injectable so tests do not sleep."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started: dict[str, float] = {}
        self._seconds: dict[str, Decimal] = {}

    def start(self, name: str) -> None:
        key = _key(name)
        if len(self._started) >= MAX_ANALYSTS:
            raise TimingError(f"at most {MAX_ANALYSTS} analysts are timed in one run")
        self._started.setdefault(key, self._clock())

    def stop(self, name: str) -> Decimal:
        key = _key(name)
        if key not in self._started:
            raise TimingError(f"analyst {key!r} was never started")
        if key not in self._seconds:                       # the first stop counts: a second one does not move it
            self._seconds[key] = max(ZERO, dec(self._clock() - self._started[key])).quantize(_MILLI)
        return self._seconds[key]

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        self.start(name)
        try:
            yield
        finally:
            self.stop(name)

    def seconds(self) -> dict[str, Decimal]:
        return dict(self._seconds)


def budget_of(line: str | None) -> Decimal | None:
    """The budget of a line, or None when the line has none (an unknown line is never given a made-up limit)."""
    return BUDGET_SECONDS.get(line or "")


def line_of_desk(desk_id: int, *, db: Database | None = None) -> str | None:
    with (db or get_database()).read() as u:
        row = u.session.one("SELECT line FROM desk_profile WHERE desk_id = :d", {"d": desk_id})
    return None if row is None else row["line"]


def flag(seconds: Mapping[str, Any], *, line: str | None = None, budget: Any = None) -> dict[str, Any]:
    """The timing block of a run record: seconds per analyst as text, the budget and the names that went over it.
    With no budget (an unknown line and none given) nothing is flagged and `budget_seconds` is None: it is said, not guessed."""
    if len(seconds) > MAX_ANALYSTS:
        raise TimingError(f"at most {MAX_ANALYSTS} analysts are timed in one run")
    values = {_key(k): dec(v, f"seconds of {k}") for k, v in seconds.items()}
    if any(v < 0 for v in values.values()):
        raise TimingError("seconds cannot be negative")
    limit = dec(budget, "budget") if budget is not None else budget_of(line)
    over = sorted(k for k, v in values.items() if limit is not None and v > limit)
    return {"line": line, "seconds": {k: text(v) for k, v in sorted(values.items())}, "total_seconds": text(sum(values.values(), ZERO)),
            "budget_seconds": text(limit), "over_budget": over}

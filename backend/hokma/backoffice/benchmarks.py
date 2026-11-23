"""The reference index of a desk (SPEC-018: TA-46). Each desk declares its benchmark in its profile (`desk_profile.benchmark`). A desk that
declared none gets no report of relative return, and the answer says so: nothing is picked for it.

`declared(desk_id)` is the one place the rest of the back office reads the benchmark from."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hokma.db import Database, get_database
from hokma.desks.core.safety import is_safe_ticker
from hokma.exec.money import dec, text

MIN_POINTS = 2


def declared(desk_id: int, *, db: Database | None = None) -> str | None:
    """The benchmark symbol the desk declared in its profile, or None (no profile, or no benchmark in it)."""
    with (db or get_database()).read() as u:
        row = u.session.one("SELECT benchmark FROM desk_profile WHERE desk_id = :d", {"d": desk_id})
    value = None if row is None else row["benchmark"]
    return value if isinstance(value, str) and value.strip() and is_safe_ticker(value.strip()) else None


@dataclass(frozen=True)
class RelativeReport:
    desk_id: int
    benchmark: str | None
    status: str                                # ok | no_benchmark | no_data
    message: str
    desk_return: Decimal | None = None
    benchmark_return: Decimal | None = None
    excess_return: Decimal | None = None
    points: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"desk_id": self.desk_id, "benchmark": self.benchmark, "status": self.status, "message": self.message,
                "desk_return": text(self.desk_return), "benchmark_return": text(self.benchmark_return), "excess_return": text(self.excess_return),
                "points": self.points}


def _series(values: Sequence[tuple[str, Any]], what: str) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for day, value in values:
        number = dec(value, what)
        if number <= 0:
            raise ValueError(f"{what} must be above zero")
        out[str(day)[:10]] = number
    return out


def relative_report(desk_id: int, desk_values: Sequence[tuple[str, Any]], benchmark_values: Sequence[tuple[str, Any]], *,
                    db: Database | None = None) -> RelativeReport:
    """The return of the desk against its declared benchmark over the dates both series share (equity of the desk, price or level of the
    benchmark). A fraction: 0.02 is two per cent. A desk with no declared benchmark gets `no_benchmark` and no number."""
    symbol = declared(desk_id, db=db)
    if symbol is None:
        return RelativeReport(desk_id, None, "no_benchmark", f"Desk {desk_id} declares no benchmark in its profile, so there is no report of relative return.")
    desk, bench = _series(desk_values, "desk value"), _series(benchmark_values, f"{symbol} value")
    common = sorted(set(desk) & set(bench))
    if len(common) < MIN_POINTS:
        return RelativeReport(desk_id, symbol, "no_data", f"Fewer than {MIN_POINTS} dates are shared by the desk and {symbol}: no relative return.", points=len(common))
    first, last = common[0], common[-1]
    r_desk, r_bench = desk[last] / desk[first] - 1, bench[last] / bench[first] - 1
    return RelativeReport(desk_id, symbol, "ok", f"{first} to {last} against {symbol}.", r_desk, r_bench, r_desk - r_bench, len(common))


__all__ = ["RelativeReport", "declared", "relative_report"]

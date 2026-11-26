"""Contract properties and margins for the history (SPEC-020: L-01, L-02).

`sheet` serves the multiplier and the tick of a future root for a past date from `data/futures_properties.csv`; in live trading the
sheet of the provider decides (decision 1251) and `differences` says where the two disagree. `margin` serves the exchange margin of a
past date from `data/margins/<ROOT>.csv` until the series ends; after that it answers with the current sheet value and the warning
`historical_margin_missing`, never with a guess.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from collections.abc import Mapping

from hokma.contracts.identity import ContractSheet, NoContract, Parsed, parse_canonical

DATA = Path(__file__).with_name("data")
PROPERTIES_PATH = DATA / "futures_properties.csv"
MARGINS_DIR = DATA / "margins"
SERIES_END = date(2022, 1, 21)                  # the last day the margin series covers (L-02)
HISTORICAL_MARGIN_MISSING = "historical_margin_missing"


class MarginUnavailable(LookupError):
    """No margin can be given for that root and date. `code` is stable: margin_before_series."""

    code = "margin_before_series"


@dataclass(frozen=True)
class HistoricalSheet:
    root: str
    on: date
    exchange: str
    description: str
    multiplier: Decimal
    tick_size: Decimal
    currency: str
    source: str = "history_table"

    @property
    def tick_value(self) -> Decimal:
        return self.multiplier * self.tick_size


@dataclass(frozen=True)
class Margin:
    root: str
    on: date
    initial: Decimal
    maintenance: Decimal
    source: str                         # series or current_sheet
    effective_from: date | None         # the date of the series row that applies
    warnings: tuple[str, ...] = ()


@lru_cache(maxsize=1)
def _properties() -> dict[str, tuple[str, str, Decimal, Decimal, str]]:
    with PROPERTIES_PATH.open(encoding="utf-8", newline="") as handle:
        return {r["root"]: (r["exchange"], r["description"], Decimal(r["multiplier"]), Decimal(r["tick_size"]), r["currency"])
                for r in csv.DictReader(handle)}


def sheet(root: str, *, on: date) -> HistoricalSheet:
    """The multiplier and tick of a future root on a past date. Raises NoContract for a root the table does not carry."""
    row = _properties().get(str(root).upper())
    if row is None:
        raise NoContract(str(root)[:80], "no historical properties for this root")
    exchange, description, multiplier, tick, currency = row
    return HistoricalSheet(str(root).upper(), on, exchange, description, multiplier, tick, currency)


def differences(live: ContractSheet) -> tuple[str, ...]:
    """The fields in which a live sheet of a future disagrees with the history table; empty when they agree."""
    parsed: Parsed = parse_canonical(live.contract_id)
    if parsed.asset_type != "future" or parsed.root is None:
        raise NoContract(live.contract_id, "only a future has historical properties")
    mine = sheet(parsed.root, on=live.as_of.date())
    out = []
    if live.multiplier != mine.multiplier:
        out.append("multiplier")
    if live.tick_size != mine.tick_size:
        out.append("tick_size")
    return tuple(out)


@lru_cache(maxsize=None)
def _series(root: str) -> tuple[tuple[date, Decimal, Decimal], ...]:
    path = MARGINS_DIR / f"{root}.csv"
    if not path.is_file():
        raise NoContract(root, "no margin series for this root")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = tuple((date.fromisoformat(r["date"]), Decimal(r["initial"]), Decimal(r["maintenance"])) for r in csv.DictReader(handle))
    return tuple(sorted(rows))


def margin(root: str, on: date, *, current: Mapping[str, tuple[Decimal, Decimal]] | None = None) -> Margin:
    """The initial and maintenance margin per contract on a date.

    Until `SERIES_END` the value is the row of the series in force that day. After it, the value is the current sheet margin
    (`current[root]` as (initial, maintenance); without it, the last value of the series) with `source="current_sheet"` and the
    warning `historical_margin_missing`: the result of a backtest must carry it.
    """
    key = str(root).upper()
    rows = _series(key)
    if on > SERIES_END:
        initial, maintenance = (current or {}).get(key) or rows[-1][1:]
        return Margin(key, on, Decimal(initial), Decimal(maintenance), "current_sheet", None, (HISTORICAL_MARGIN_MISSING,))
    applicable = [r for r in rows if r[0] <= on]
    if not applicable:
        raise MarginUnavailable(f"{key}: the margin series starts on {rows[0][0].isoformat()}, not before {on.isoformat()}")
    effective, initial, maintenance = applicable[-1]
    return Margin(key, on, initial, maintenance, "series", effective)

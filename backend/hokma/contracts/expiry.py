"""Expiration cycles, expiration rules and business day calendar of the futures Hokma operates (SPEC-020: L-04 to L-06, L-55).

A rule answers one question: on what date does the contract of a given month stop trading. The table `data/futures_roots.csv` says,
per root, the listed months (the cycle) and the name of its rule; the holidays come from `contracts.sessions`. Nothing here asks a
provider: the live expiration of a sheet is the provider's (decision 1251), this module serves the history.
"""
from __future__ import annotations

import csv
import calendar
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta
from functools import lru_cache
from pathlib import Path

from hokma.contracts import sessions
from hokma.contracts.identity import MONTH_CODES, NoContract

ROOTS_PATH = Path(__file__).with_name("data") / "futures_roots.csv"
FRIDAY, WEDNESDAY = 4, 2


# ------------------------------------------------------------------------------------------------------ calendar (L-06)
def is_business_day(day: date, holidays: Collection[date] = ()) -> bool:
    """Monday to Friday and not a holiday."""
    return day.weekday() < 5 and day not in holidays


def business_days(first: date, last: date, holidays: Collection[date] = ()) -> tuple[date, ...]:
    """The business days from `first` to `last`, both included."""
    if last < first:
        return ()
    return tuple(first + timedelta(days=i) for i in range((last - first).days + 1) if is_business_day(first + timedelta(days=i), holidays))


def add_business_days(day: date, n: int, holidays: Collection[date] = ()) -> date:
    """`n` business days after (n above 0) or before (n below 0) `day`; the day itself is never counted."""
    step = 1 if n > 0 else -1
    current, left = day, abs(n)
    while left:
        current += timedelta(days=step)
        if is_business_day(current, holidays):
            left -= 1
    return current


def nth_last_business_day(year: int, month: int, n: int, holidays: Collection[date] = ()) -> date:
    """The n-th business day counting back from the last day of the month (n=1 is the last business day)."""
    current = date(year, month, calendar.monthrange(year, month)[1])
    left = n
    while True:
        if is_business_day(current, holidays):
            left -= 1
            if left == 0:
                return current
        current -= timedelta(days=1)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th (1 to 5) given weekday of the month (Monday is 0)."""
    if not 1 <= n <= 5:
        raise ValueError("n is between 1 and 5")
    first = date(year, month, 1)
    day = first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    if day.month != month:
        raise ValueError(f"the month has no weekday number {n}")
    return day


def third_friday(year: int, month: int, holidays: Collection[date] = ()) -> date:
    """The third Friday of the month; when it is a holiday, the business day before it."""
    day = nth_weekday(year, month, FRIDAY, 3)
    return add_business_days(day, -1, holidays) if day in holidays else day


# ------------------------------------------------------------------------------------------------------ the table (L-04, L-08)
@dataclass(frozen=True)
class RootInfo:
    root: str
    exchange: str
    asset_class: str
    cycle: str                  # month letters of the listed months
    month_policy: str           # yes: a month outside the cycle is no contract; forward: it moves to the next listed month; no: any month lists
    rule: str


@lru_cache(maxsize=1)
def _roots() -> dict[str, RootInfo]:
    with ROOTS_PATH.open(encoding="utf-8", newline="") as handle:
        return {r["root"]: RootInfo(r["root"], r["exchange"], r["asset_class"], r["cycle"], r["strict_cycle"], r["expiry_rule"])
                for r in csv.DictReader(handle)}


def root_info(root: str) -> RootInfo:
    """The row of a root. Raises NoContract for a root the table does not carry."""
    info = _roots().get(str(root).upper())
    if info is None:
        raise NoContract(str(root)[:80], "unknown futures root")
    return info


def roots() -> tuple[str, ...]:
    return tuple(_roots())


def cycles(root: str) -> tuple[int, ...]:
    """The calendar months in which the root lists contracts (NQ: 3, 6, 9, 12)."""
    return tuple(sorted(MONTH_CODES.index(letter) + 1 for letter in root_info(root).cycle))


def listed_months(root: str, first: date, last: date) -> tuple[date, ...]:
    """The first day of every listed contract month from the month of `first` to the month of `last`."""
    months = cycles(root)
    out, year, month = [], first.year, first.month
    while (year, month) <= (last.year, last.month):
        if month in months:
            out.append(date(year, month, 1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return tuple(out)


# ------------------------------------------------------------------------------------------------------ rules (L-05)
@dataclass(frozen=True)
class Expiry:
    day: date                   # last day of trading
    time_et: time | None        # the cut of that day in New York time, None when the rule has no cut inside the day


Rule = Callable[[int, int, frozenset[date]], Expiry]


def _third_friday_open(year: int, month: int, h: frozenset[date]) -> Expiry:
    return Expiry(third_friday(year, month, h), time(9, 30))            # settles on the open of the third Friday


def _crude(year: int, month: int, h: frozenset[date], back: int = 3) -> Expiry:
    """Three business days before the 25th of the month before the contract month; one more when the 25th is not a business day."""
    prior_year, prior_month = (year - 1, 12) if month == 1 else (year, month - 1)
    twenty_fifth = date(prior_year, prior_month, 25)
    return Expiry(add_business_days(twenty_fifth, -(back if is_business_day(twenty_fifth, h) else back + 1), h), None)


def _micro_crude(year: int, month: int, h: frozenset[date]) -> Expiry:
    return _crude(year, month, h, back=4)


def _natural_gas(year: int, month: int, h: frozenset[date]) -> Expiry:
    return Expiry(add_business_days(date(year, month, 1), -3, h), None)


def _third_last(year: int, month: int, h: frozenset[date]) -> Expiry:
    return Expiry(nth_last_business_day(year, month, 3, h), None)


def _third_last_noon_central(year: int, month: int, h: frozenset[date]) -> Expiry:
    return Expiry(nth_last_business_day(year, month, 3, h), time(13, 0))


def _seventh_before_last(year: int, month: int, h: frozenset[date]) -> Expiry:
    return Expiry(add_business_days(nth_last_business_day(year, month, 1, h), -7, h), time(13, 1))


def _last_business(year: int, month: int, h: frozenset[date]) -> Expiry:
    return Expiry(nth_last_business_day(year, month, 1, h), time(13, 1))


def _euro_fx(year: int, month: int, h: frozenset[date]) -> Expiry:
    """The second business day before the third Wednesday of the contract month."""
    return Expiry(add_business_days(nth_weekday(year, month, WEDNESDAY, 3), -2, h), time(10, 16))


RULES: Mapping[str, Rule] = {
    "third_friday_0930_et": _third_friday_open, "crude_oil": _crude, "micro_crude_oil": _micro_crude, "natural_gas": _natural_gas,
    "third_last_business_day": _third_last, "third_last_business_day_1300_et": _third_last_noon_central,
    "seventh_business_day_before_last_1301_et": _seventh_before_last, "last_business_day_1301_et": _last_business,
    "second_business_day_before_third_wednesday_1016_et": _euro_fx,
}


@dataclass(frozen=True)
class ExpiryRule:
    info: RootInfo

    def expiry(self, year: int, month: int) -> Expiry:
        """When the contract of that month stops trading, with the holidays of the exchange of the root."""
        if not 1 <= month <= 12:
            raise ValueError("month is between 1 and 12")
        months = cycles(self.info.root)
        if month not in months:
            if self.info.month_policy == "yes":
                raise NoContract(f"{self.info.root}:{year}-{month:02d}", "the month is not in the cycle of the root")
            if self.info.month_policy == "forward":
                year, month = next(((year, m) for m in months if m > month), (year + 1, months[0]))
        return RULES[self.info.rule](year, month, sessions.holidays(self.info.root))


def rules(root: str) -> ExpiryRule:
    """The expiration rule of a root. Raises NoContract for an unknown root."""
    return ExpiryRule(root_info(root))


# ------------------------------------------------------------------------------------------------------ L-55
def front_by_open_interest(open_interest: Mapping[str, Mapping[date, int]], order: Sequence[str]) -> dict[date, str]:
    """The most liquid contract of each date. `order` lists the contract ids from the nearest expiration to the farthest;
    the front starts at the first one and moves to the next the day its open interest exceeds the front's, and never moves back."""
    if not order:
        raise ValueError("order needs at least one contract")
    dates = sorted({d for series in open_interest.values() for d in series})
    front, out = 0, {}
    for day in dates:
        while front + 1 < len(order):
            current, nxt = open_interest.get(order[front], {}).get(day), open_interest.get(order[front + 1], {}).get(day)
            if current is None or nxt is None or nxt <= current:
                break
            front += 1
        out[day] = order[front]
    return out

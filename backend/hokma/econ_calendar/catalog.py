"""Catalog of macro releases (SPEC-018: ECT-063): when each one comes out (New York time), what its report holds and how it is
classified, plus the table of a release with its four figures and the standardized surprise.

The catalog states WHEN and WHAT. It states nothing about how a market reacts: that is measured from data by the event studies,
never written here. Figures come from the calendar (`hokma.econ_calendar.events`); a figure the provider does not have is None
and the table says NO_DATA, never a number made up in its place.

Surprise = (actual - consensus) / historical standard deviation of that release's surprises. The deviation comes from the caller
(`history_std`); without it, or without a consensus or an actual, the answer is NO_DATA.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
FIELDS = ("actual", "consensus", "previous", "revised")
CLASSES = ("leading", "coincident", "lagging", "policy")
SURPRISE_PLACES = Decimal("0.000001")


@dataclass(frozen=True)
class Release:
    key: str
    name: str
    country: str
    classification: str                  # leading | coincident | lagging | policy
    frequency: str                       # weekly | monthly | quarterly | eight_a_year
    time_et: time                        # release time, New York time
    rule: str                            # first_friday | weekly_thursday | business_day:N | around_day:N | last_week | published
    components: tuple[str, ...]          # what the report holds and the reader should look at
    match: tuple[str, ...]               # words that find it in the name an event has in the calendar (any case)

    @property
    def exact(self) -> bool:
        """True when the rule alone gives the day. The others are nominal: holidays, publication calendars and revisions move them."""
        return self.rule in ("first_friday", "weekly_thursday")


def _t(hour: int, minute: int = 0) -> time:
    return time(hour, minute)


CATALOG: tuple[Release, ...] = (
    Release("nfp", "Nonfarm payrolls", "US", "coincident", "monthly", _t(8, 30), "first_friday",
            ("headline change in jobs", "revision of the two prior months", "unemployment rate (U-3)", "underemployment rate (U-6)",
             "average hourly earnings", "average weekly hours", "labor force participation rate"),
            ("non-farm employment", "nonfarm payroll", "non-farm payroll")),
    Release("cpi", "Consumer price index", "US", "lagging", "monthly", _t(8, 30), "around_day:13",
            ("headline index", "core index (without food and energy)", "core services without housing", "shelter and owners' equivalent rent",
             "trimmed mean"),
            ("cpi",)),
    Release("ppi", "Producer price index", "US", "lagging", "monthly", _t(8, 30), "around_day:14",
            ("final demand headline", "core without food and energy", "services", "goods"), ("ppi",)),
    Release("retail_sales", "Retail sales", "US", "coincident", "monthly", _t(8, 30), "around_day:15",
            ("headline", "without autos", "control group"), ("retail sales",)),
    Release("pce", "Personal income and outlays (PCE price index)", "US", "lagging", "monthly", _t(8, 30), "last_week",
            ("headline price index", "core price index", "personal spending", "personal income"),
            ("pce price", "core pce", "personal spending", "personal income")),
    Release("gdp", "Gross domestic product", "US", "coincident", "quarterly", _t(8, 30), "last_week",
            ("annualized quarterly growth", "consumer spending", "business investment", "price index"), ("gdp",)),
    Release("fomc", "FOMC rate decision", "US", "policy", "eight_a_year", _t(14, 0), "published",
            ("target range", "statement", "projections (some meetings)", "press conference"), ("fomc statement", "federal funds rate", "fomc press conference")),
    Release("jobless_claims", "Initial jobless claims", "US", "leading", "weekly", _t(8, 30), "weekly_thursday",
            ("initial claims", "four week average", "continuing claims"), ("unemployment claims", "jobless claims")),
    Release("ism_manufacturing", "Manufacturing purchasing managers index", "US", "leading", "monthly", _t(10, 0), "business_day:1",
            ("headline index (50 separates growth from contraction)", "new orders", "production", "employment", "prices paid",
             "supplier deliveries", "inventories", "new export orders"), ("ism manufacturing",)),
    Release("ism_services", "Services purchasing managers index", "US", "leading", "monthly", _t(10, 0), "business_day:3",
            ("headline index (50 separates growth from contraction)", "business activity", "new orders", "employment", "prices"),
            ("ism services",)),
    Release("industrial_production", "Industrial production", "US", "coincident", "monthly", _t(9, 15), "around_day:16",
            ("total index", "manufacturing", "mining", "utilities", "capacity utilization"), ("industrial production",)),
    Release("housing_permits", "Building permits", "US", "leading", "monthly", _t(8, 30), "around_day:18",
            ("permits", "housing starts"), ("building permits", "housing starts")),
)
BY_KEY = {r.key: r for r in CATALOG}


class CatalogError(ValueError):
    """An argument that is not valid (unknown key, a month out of range)."""


def get(key: str) -> Release:
    try:
        return BY_KEY[key]
    except (KeyError, TypeError):
        raise CatalogError(f"unknown release {key!r}") from None


def find(event_name: str, country: str = "US") -> Release | None:
    """The release an event of the calendar is, by whole words of its name and its country; None when it is not in the catalog
    (the catalog holds the releases of the United States: the same name in another country is another release)."""
    text = str(event_name or "").casefold()
    for release in CATALOG:
        if release.country == str(country).upper() and any(re.search(rf"\b{re.escape(word)}\b", text) for word in release.match):
            return release
    return None


# ---------------------------------------------------------------------------------------------- when

def _next_weekday(day: date) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _business_day(year: int, month: int, number: int) -> date:
    day, seen = date(year, month, 1), 0
    while True:
        if day.weekday() < 5:
            seen += 1
            if seen == number:
                return day
        day += timedelta(days=1)


def release_days(key: str, year: int, month: int) -> tuple[date, ...]:
    """The nominal days of a release in a month. The rule of `first_friday` and `weekly_thursday` is exact; the others ignore
    holidays and the publisher's own calendar (check the calendar for the real day). `published` has no rule: ()."""
    release = get(key)
    if not isinstance(year, int) or not isinstance(month, int) or not 1 <= month <= 12 or not 1990 <= year <= 2200:
        raise CatalogError("year and month must be a real month")
    first = date(year, month, 1)
    rule, _, number = release.rule.partition(":")
    if rule == "first_friday":
        return (first + timedelta(days=(4 - first.weekday()) % 7),)
    if rule == "weekly_thursday":
        thursday = first + timedelta(days=(3 - first.weekday()) % 7)
        out = []
        while thursday.month == month:
            out.append(thursday)
            thursday += timedelta(days=7)
        return tuple(out)
    if rule == "business_day":
        return (_business_day(year, month, int(number)),)
    if rule == "around_day":
        return (_next_weekday(date(year, month, int(number))),)
    if rule == "last_week":
        last = (date(year + (month == 12), month % 12 + 1, 1)) - timedelta(days=1)
        while last.weekday() >= 5:
            last -= timedelta(days=1)
        return (last,)
    return ()


def release_at(key: str, day: date) -> datetime:
    """The release time of `key` on `day`, as UTC (New York daylight saving applies on that day)."""
    return datetime.combine(day, get(key).time_et, tzinfo=NEW_YORK).astimezone(timezone.utc)


def annualize_monthly(percent: Any) -> Decimal:
    """A monthly percent change as an annual one: ((1 + p / 100) ^ 12 - 1) * 100, to two places (0.2 gives 2.43)."""
    rate = _decimal(percent)
    if rate is None:
        raise CatalogError("a number is required")
    return (((1 + rate / 100) ** 12 - 1) * 100).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------------------------- figures

def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


@dataclass(frozen=True)
class Surprise:
    status: str                          # ok | NO_DATA
    value: Decimal | None = None         # (actual - consensus) / history_std; None when NO_DATA, never zero
    reason: str | None = None            # no_actual | no_consensus | no_history


def surprise(*, actual: Any, consensus: Any, history_std: Any) -> Surprise:
    """The standardized surprise. A consensus the provider does not have is NO_DATA, never a number."""
    consensus_, actual_, std = _decimal(consensus), _decimal(actual), _decimal(history_std)
    if consensus_ is None:
        return Surprise("NO_DATA", None, "no_consensus")
    if actual_ is None:
        return Surprise("NO_DATA", None, "no_actual")
    if std is None or std <= 0:
        return Surprise("NO_DATA", None, "no_history")
    return Surprise("ok", ((actual_ - consensus_) / std).quantize(SURPRISE_PLACES))


@dataclass(frozen=True)
class ReleaseRow:
    release: str | None                  # the catalog key, None when the event is not in the catalog
    name: str
    country: str
    scheduled_at: datetime
    actual: float | None
    consensus: float | None
    previous: float | None
    revised: float | None
    consensus_status: str                # ok | NO_DATA
    surprise: Surprise


def table(events: Sequence[Any], history_std: Mapping[str, Any] | None = None) -> tuple[ReleaseRow, ...]:
    """The rows of the events of the calendar, oldest first, with the four figures and the surprise of each. `history_std` maps a
    catalog key to the historical deviation of its surprises; a release without one has NO_DATA for the surprise."""
    deviations = history_std or {}
    rows = []
    for event in events:
        release = find(event.name, event.country)
        key = release.key if release else None
        rows.append(ReleaseRow(key, event.name, event.country, event.scheduled_at, event.actual, event.consensus, event.previous,
                               event.revised, "NO_DATA" if event.consensus is None else "ok",
                               surprise(actual=event.actual, consensus=event.consensus, history_std=deviations.get(key))))
    return tuple(sorted(rows, key=lambda r: (r.scheduled_at, r.name)))

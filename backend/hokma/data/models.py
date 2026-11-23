"""Typed records of the data module (SPEC-017: AH-45).

Fundamentals and filings carry their own filing date, which is what makes them point in time: a figure is known from the
day it was filed, not from the end of its fiscal period. Parsing is strict about identity (a record without its ticker or
period is rejected) and tolerant about the rest (unknown keys are ignored, a number that does not parse becomes None).
"""
from __future__ import annotations

import math
from dataclasses import MISSING, dataclass, field, fields, make_dataclass
from typing import Any, Mapping


class Record:
    """Base of the records: `from_record` builds one from a provider's dict."""

    @classmethod
    def from_record(cls, row: Mapping[str, Any]):
        known = {f.name: f for f in fields(cls)}
        data: dict[str, Any] = {}
        for key, value in row.items():
            if key in known:
                data[key] = _coerce(str(known[key].type), value)
        missing = [name for name, f in known.items()
                   if f.default is MISSING and f.default_factory is MISSING and name not in data]
        if missing:
            raise ValueError(f"{cls.__name__} needs {', '.join(missing)}")
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}  # type: ignore[arg-type]


def _coerce(annotation: str, value: Any) -> Any:
    if value is None or value == "":
        return None
    if "float" in annotation:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None
    if "bool" in annotation:
        return value if isinstance(value, bool) else str(value).strip().lower() in {"true", "1", "yes"}
    if "int" in annotation:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return str(value)


_FLOAT = "float | None"
_PERIOD_NUMBERS = (
    "market_cap", "price_to_earnings_ratio", "return_on_equity", "gross_margin", "operating_margin", "net_margin",
    "debt_to_equity", "current_ratio", "revenue_growth", "earnings_per_share", "book_value_per_share",
    "free_cash_flow_per_share",
)
_METRIC_EXTRAS = (
    "enterprise_value", "price_to_book_ratio", "price_to_sales_ratio", "enterprise_value_to_ebitda_ratio",
    "enterprise_value_to_revenue_ratio", "free_cash_flow_yield", "peg_ratio", "return_on_assets",
    "return_on_invested_capital", "asset_turnover", "inventory_turnover", "receivables_turnover",
    "days_sales_outstanding", "operating_cycle", "working_capital_turnover", "quick_ratio", "cash_ratio",
    "operating_cash_flow_ratio", "debt_to_assets", "interest_coverage", "earnings_growth", "book_value_growth",
    "earnings_per_share_growth", "free_cash_flow_growth", "operating_income_growth", "ebitda_growth", "payout_ratio",
)
_IDENTITY = [("ticker", "str"), ("report_period", "str")]
_PERIOD_HEAD = [("period", "str", field(default="ttm")), ("currency", "str | None", field(default=None)),
                ("filing_date", "str | None", field(default=None)), ("filing_datetime", "str | None", field(default=None))]

# What an analyst may know about a company for one period. `filing_date` is the day it became public.
PeriodFundamentals = make_dataclass(
    "PeriodFundamentals",
    [*_IDENTITY, *_PERIOD_HEAD, *((name, _FLOAT, field(default=None)) for name in _PERIOD_NUMBERS)],
    bases=(Record,), frozen=True,
)
FinancialMetrics = make_dataclass(
    "FinancialMetrics",
    [*_IDENTITY, *_PERIOD_HEAD, *((name, _FLOAT, field(default=None)) for name in (*_PERIOD_NUMBERS, *_METRIC_EXTRAS))],
    bases=(Record,), frozen=True,
)


@dataclass(frozen=True)
class InsiderTrade(Record):
    ticker: str
    name: str
    filing_date: str | None = None
    transaction_date: str | None = None
    title: str | None = None
    is_board_director: bool = False
    transaction_type: str | None = None
    transaction_shares: float | None = None
    transaction_price_per_share: float | None = None
    transaction_value: float | None = None
    shares_owned_after_transaction: float | None = None
    security_title: str | None = None


@dataclass(frozen=True)
class CompanyNews(Record):
    ticker: str
    title: str
    source: str = ""
    date: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class CompanyFacts(Record):
    """Slow-moving attributes. They carry no vintage: do not use them to describe a past date."""

    ticker: str
    name: str | None = None
    cik: str | None = None
    sector: str | None = None
    industry: str | None = None
    exchange: str | None = None
    market_cap: float | None = None
    is_active: bool = True
    sic_code: str | None = None


@dataclass(frozen=True)
class EarningsData(Record):
    revenue: float | None = None
    estimated_revenue: float | None = None
    earnings_per_share: float | None = None
    estimated_earnings_per_share: float | None = None
    net_income: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    free_cash_flow: float | None = None
    total_assets: float | None = None
    total_liabilities: float | None = None
    shareholders_equity: float | None = None


@dataclass(frozen=True)
class Filing(Record):
    ticker: str
    filing_type: str
    filing_date: str
    accession_number: str | None = None
    report_period: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class EarningsRecord(Record):
    """One filing that reported earnings; the same period can appear in several filings."""

    ticker: str
    report_period: str
    source_type: str = ""
    filing_date: str | None = None
    fiscal_period: str | None = None
    currency: str | None = None
    accession_number: str | None = None
    quarterly: EarningsData | None = field(default=None)
    annual: EarningsData | None = field(default=None)

    @classmethod
    def from_record(cls, row: Mapping[str, Any]):
        base = {k: v for k, v in row.items() if k not in ("quarterly", "annual")}
        item = super().from_record(base)
        extra = {name: EarningsData.from_record(row[name]) for name in ("quarterly", "annual") if isinstance(row.get(name), Mapping)}
        return cls(**{**item.to_dict(), **extra})

"""Fundamentals sources (SPEC-017: AH-43, FT-101, TA-76, TA-78) and the point-in-time `fundamentals` call.

Every source answers "what was public on `as_of`": records are selected by their filing date, never by the end of their
fiscal period. A record without a filing date is excluded unless the caller states an explicit filing lag, and then the
result says the date was estimated. Infrastructure failures raise the provider errors; an empty answer is "does not
exist". Keys come from Connections and never appear in a result.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from hokma.data import pit, sentinels
from hokma.data.models import CompanyFacts, FinancialMetrics, InsiderTrade
from hokma.data.providers import (KeyResolver, NoDataError, NotConfiguredError, ProviderConfig, RateLimitError, Transport,
                                  check_status, connections_key, http_transport, route)
from hokma.data.sentinels import DataResult, parse_time, utcnow

# ---------------------------------------------------------------------------------------------------- FT-101
# Common account names and the names each provider uses for them. Matching ignores case, spaces and punctuation.
ACCOUNT_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "income": {
        "revenue": ("Total Revenue", "Revenues", "totalRevenue", "revenue", "SalesRevenueNet", "Operating Revenue"),
        "cost_of_revenue": ("Cost Of Revenue", "costOfRevenue", "cost_of_revenue", "CostOfGoodsAndServicesSold"),
        "gross_profit": ("Gross Profit", "grossProfit", "gross_profit", "GrossProfit"),
        "operating_income": ("Operating Income", "operatingIncome", "operating_income", "OperatingIncomeLoss"),
        "net_income": ("Net Income", "netIncome", "net_income", "NetIncomeLoss", "Net Income Common Stockholders"),
        "ebitda": ("EBITDA", "ebitda", "Normalized EBITDA"),
        "eps_diluted": ("Diluted EPS", "dilutedEPS", "earnings_per_share_diluted", "EarningsPerShareDiluted"),
        "interest_expense": ("Interest Expense", "interestExpense", "interest_expense", "InterestExpense"),
    },
    "balance": {
        "total_assets": ("Total Assets", "totalAssets", "total_assets", "Assets"),
        "total_liabilities": ("Total Liabilities Net Minority Interest", "totalLiabilities", "total_liabilities", "Liabilities"),
        "shareholders_equity": ("Stockholders Equity", "totalShareholderEquity", "shareholders_equity", "StockholdersEquity"),
        "cash_and_equivalents": ("Cash And Cash Equivalents", "cashAndCashEquivalentsAtCarryingValue", "cash_and_equivalents",
                                 "CashAndCashEquivalentsAtCarryingValue"),
        "total_debt": ("Total Debt", "shortLongTermDebtTotal", "total_debt", "DebtInstrumentCarryingAmount"),
        "current_assets": ("Current Assets", "totalCurrentAssets", "current_assets", "AssetsCurrent"),
        "current_liabilities": ("Current Liabilities", "totalCurrentLiabilities", "current_liabilities", "LiabilitiesCurrent"),
    },
    "cashflow": {
        "operating_cash_flow": ("Operating Cash Flow", "operatingCashflow", "net_cash_flow_from_operations",
                                "NetCashProvidedByUsedInOperatingActivities"),
        "capital_expenditure": ("Capital Expenditure", "capitalExpenditures", "capital_expenditure",
                                "PaymentsToAcquirePropertyPlantAndEquipment"),
        "free_cash_flow": ("Free Cash Flow", "free_cash_flow", "freeCashFlow"),
        "investing_cash_flow": ("Investing Cash Flow", "cashflowFromInvestment", "net_cash_flow_from_investing"),
        "financing_cash_flow": ("Financing Cash Flow", "cashflowFromFinancing", "net_cash_flow_from_financing"),
    },
    "statistics": {
        "market_cap": ("Market Cap", "MarketCapitalization", "market_cap", "marketCap"),
        "price_to_earnings": ("Trailing PE", "PERatio", "price_to_earnings_ratio", "trailingPE"),
        "price_to_book": ("Price To Book", "PriceToBookRatio", "price_to_book_ratio", "priceToBook"),
        "dividend_yield": ("Dividend Yield", "DividendYield", "dividendYield"),
        "beta": ("Beta", "beta"),
    },
}
STATEMENTS = tuple(ACCOUNT_ALIASES)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _lookup(statement: str) -> dict[str, str]:
    return {_squash(alias): common for common, aliases in ACCOUNT_ALIASES[statement].items() for alias in (common, *aliases)}


def normalize_accounts(raw: Mapping[str, Any], statement: str) -> tuple[dict[str, float | None], list[str]]:
    """Translate provider account names to the common names. Returns (values, unmapped): the accounts that have no common
    name are listed, never dropped silently and never guessed."""
    if statement not in ACCOUNT_ALIASES:
        raise ValueError(f"unknown statement {statement!r}; expected one of {STATEMENTS}")
    table = _lookup(statement)
    values: dict[str, float | None] = {}
    unmapped: list[str] = []
    for name, value in raw.items():
        common = table.get(_squash(name))
        if common is None:
            unmapped.append(str(name))
            continue
        try:
            number = float(value)
            values[common] = number if number == number and abs(number) != float("inf") else None
        except (TypeError, ValueError):
            values[common] = None
    return values, unmapped


# ---------------------------------------------------------------------------------------------------- AH-43
class FilingDateSource:
    """Stock fundamentals, news and insider trades from a provider that filters by FILING date. The provider is named
    `financial_datasets`. Failures raise the provider errors; 404 or no rows is an empty answer."""

    name = "financial_datasets"
    BASE_URL = "https://api.financialdatasets.ai"
    RETRY_DELAYS = (5, 15, 30)

    def __init__(self, *, key: KeyResolver = connections_key, transport: Transport = http_transport,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._key, self._transport, self._sleep = key, transport, sleep

    def _get(self, path: str, params: Mapping[str, Any], response_key: str) -> list[dict] | None:
        secret = self._key("financial_datasets")
        if not secret:
            raise NotConfiguredError(self.name)
        url, query, rows = self.BASE_URL + path, dict(params), None
        while True:
            for delay in (*self.RETRY_DELAYS, None):
                status, body = self._transport(url, query, {"X-API-Key": secret})
                if status == 429 and delay is not None:
                    self._sleep(delay)
                    continue
                break
            if status == 404:
                return rows
            check_status(self.name, status, body, secret=secret)
            payload = json.loads(body or b"{}")
            page = payload.get(response_key)
            rows = (rows or []) + list(page or []) if page is not None or rows is not None else None
            next_url = payload.get("next_page_url")
            if not next_url:
                return rows
            url, query = next_url, {}

    def get_financial_metrics(self, ticker: str, as_of: str, period: str = "ttm", limit: int = 10) -> list[FinancialMetrics]:
        """Metrics that were public on `as_of`: the provider filters on the filing date, and the result is filtered again
        here so a provider that ignores the bound cannot leak the future."""
        rows = self._get("/financial-metrics/", {"ticker": ticker, "filing_date_lte": as_of, "period": period, "limit": limit},
                         "financial_metrics")
        records = sorted(pit.filter_financials_by_date(rows or [], as_of), key=lambda r: str(r.get("filing_date")), reverse=True)
        return [FinancialMetrics.from_record(r) for r in records]

    def get_insider_trades(self, ticker: str, as_of: str, start: str | None = None, limit: int = 1000) -> list[InsiderTrade]:
        params: dict[str, Any] = {"ticker": ticker, "filing_date_lte": as_of, "limit": limit}
        if start:
            params["filing_date_gte"] = start
        rows = self._get("/insider-trades/", params, "insider_trades")
        return [InsiderTrade.from_record(r) for r in pit.filter_financials_by_date(rows or [], as_of)]

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        secret = self._key("financial_datasets")
        if not secret:
            raise NotConfiguredError(self.name)
        status, body = self._transport(self.BASE_URL + "/company/facts/", {"ticker": ticker}, {"X-API-Key": secret})
        if status == 404:
            return None
        check_status(self.name, status, body, secret=secret)
        facts = json.loads(body or b"{}").get("company_facts")
        return CompanyFacts.from_record(facts) if facts else None

    def fetch(self, category: str, **params: Any) -> Any:
        methods = {"metrics": self.get_financial_metrics, "insider_trades": self.get_insider_trades,
                   "company_facts": self.get_company_facts}
        if category not in methods:
            raise NoDataError(params.get("ticker", ""), None, f"{self.name} does not serve {category!r}")
        return methods[category](**params)


# ---------------------------------------------------------------------------------------------------- TA-76
class AlphaVantageSource:
    """Paid alternative: statements by fiscal period. The provider does not publish filing dates, so a record is only usable
    for a past `as_of` when the caller gives an explicit `filing_lag_days`; the date is then marked as estimated."""

    name = "alpha_vantage"
    URL = "https://www.alphavantage.co/query"
    FUNCTIONS = {"income": "INCOME_STATEMENT", "balance": "BALANCE_SHEET", "cashflow": "CASH_FLOW"}

    def __init__(self, *, key: KeyResolver = connections_key, transport: Transport = http_transport) -> None:
        self._key, self._transport = key, transport

    def _call(self, function: str, symbol: str) -> dict:
        secret = self._key("alpha_vantage")
        if not secret:
            raise NotConfiguredError(self.name)
        status, body = self._transport(self.URL, {"function": function, "symbol": symbol, "apikey": secret}, {})
        check_status(self.name, status, body, secret=secret)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise NoDataError(symbol, None, "provider answered with something that is not JSON") from error
        notice = payload.get("Information") or payload.get("Note") if isinstance(payload, dict) else None
        if notice:
            low = str(notice).lower()
            if any(marker in low for marker in ("rate limit", "requests per day", "call frequency", "premium")):
                raise RateLimitError(f"{self.name} rate limit: {sentinels.scrub(str(notice), secret)}")
            if "api key" in low or "apikey" in low:
                raise NotConfiguredError(self.name, f"{self.name} key invalid or missing")
        return payload if isinstance(payload, dict) else {}

    def get_statement(self, symbol: str, statement: str, period: str = "quarterly", *, filing_lag_days: int | None = None) -> list[dict]:
        if statement not in self.FUNCTIONS:
            raise NoDataError(symbol, None, f"{self.name} does not serve statement {statement!r}")
        payload = self._call(self.FUNCTIONS[statement], symbol)
        reports = payload.get("annualReports" if period == "annual" else "quarterlyReports") or []
        out = []
        for report in reports:
            values, unmapped = normalize_accounts({k: v for k, v in report.items() if k not in ("fiscalDateEnding", "reportedCurrency")}, statement)
            end = report.get("fiscalDateEnding")
            record = {"ticker": symbol, "report_period": end, "period": period, "currency": report.get("reportedCurrency"),
                      "filing_date": None, "filing_date_estimated": False, "unmapped_accounts": unmapped, **values}
            if filing_lag_days is not None and end:
                record["filing_date"] = (parse_time(end) + timedelta(days=filing_lag_days)).strftime("%Y-%m-%d")
                record["filing_date_estimated"] = True
            out.append(record)
        return out

    def get_overview(self, symbol: str, as_of: datetime, *, today=None) -> DataResult:
        """Company overview. Serves only today's values, so for a past `as_of` it is withheld."""
        notice = pit.withhold_live_profile(as_of.strftime("%Y-%m-%d"), symbol, today=today)
        if notice:
            return sentinels.no_data(self.name, sentinels.REASON_LIVE_PROFILE, as_of=as_of, warnings=(notice,))
        return sentinels.ok(self._call("OVERVIEW", symbol), source=self.name, as_of=as_of)

    def fetch(self, category: str, **params: Any) -> Any:
        if category != "statement":
            raise NoDataError(params.get("symbol", ""), None, f"{self.name} does not serve {category!r}")
        return self.get_statement(**params)


# ---------------------------------------------------------------------------------------------------- TA-78
def statements_from_table(symbol: str, statement: str, table: Mapping[str, Mapping[str, Any]], *, period: str = "quarterly",
                          filing_lag_days: int | None = None) -> list[dict]:
    """Turn a table keyed by fiscal period end (the shape of a free provider's statement: account names by period) into
    common records. The table has no filing dates; see `AlphaVantageSource` for how that is handled."""
    out = []
    for end, accounts in table.items():
        values, unmapped = normalize_accounts(accounts, statement)
        record = {"ticker": symbol, "report_period": str(end)[:10], "period": period, "filing_date": None,
                  "filing_date_estimated": False, "unmapped_accounts": unmapped, **values}
        if filing_lag_days is not None:
            record["filing_date"] = (parse_time(str(end)[:10]) + timedelta(days=filing_lag_days)).strftime("%Y-%m-%d")
            record["filing_date_estimated"] = True
        out.append(record)
    return out


def insider_transactions(symbol: str, rows: Sequence[Mapping[str, Any]], as_of: datetime | str) -> list[InsiderTrade]:
    """Insider transactions known on `as_of`. A free provider lists them by transaction date; a row with no usable date is
    excluded."""
    cutoff = parse_time(as_of).date()
    out = []
    for row in rows:
        raw = row.get("filing_date") or row.get("Start Date") or row.get("transaction_date")
        try:
            day = parse_time(str(raw)[:10]).date()
        except ValueError:
            continue
        if day > cutoff:
            continue
        shares = row.get("Shares", row.get("transaction_shares"))
        value = row.get("Value", row.get("transaction_value"))
        out.append(InsiderTrade.from_record({
            "ticker": symbol, "name": row.get("Insider") or row.get("name") or "", "title": row.get("Position") or row.get("title"),
            "filing_date": str(raw)[:10], "transaction_date": str(row.get("Start Date") or row.get("transaction_date") or "")[:10] or None,
            "transaction_type": row.get("Transaction") or row.get("transaction_type"), "transaction_shares": shares,
            "transaction_value": value,
        }))
    return sorted(out, key=lambda t: t.filing_date or "", reverse=True)


# ---------------------------------------------------------------------------------------------------- the call
def fundamentals(symbol: str, *, as_of: datetime, statement: str, period: str,
                 sources: Mapping[str, Callable[..., Any]] | None = None, config: ProviderConfig | None = None,
                 filing_lag_days: int | None = None, clock: Callable[[], datetime] = utcnow) -> DataResult:
    """Point-in-time fundamentals: what was public on `as_of`, newest first, from the configured provider chain.

    `statement` is `metrics` (ratios by filing date) or one of income, balance, cashflow. `sources` maps a provider name to
    a callable `(symbol, statement, period, as_of) -> list[dict]`; the defaults are the real providers.
    """
    cut = parse_time(as_of)
    impls = dict(sources) if sources is not None else default_sources(filing_lag_days=filing_lag_days)
    chain = (config or ProviderConfig()).chain_for("fundamentals", "fundamentals", available=impls)
    result = route("fundamentals", impls, chain, symbol, statement, period, cut, category="fundamentals", as_of=cut, clock=clock)
    if not result.ok:
        return result
    visible = pit.filter_financials_by_date(list(result.value), cut)
    if not visible:
        return sentinels.no_data(result.source, sentinels.REASON_NOT_PUBLISHED, as_of=cut, received_at=result.received_at)
    visible.sort(key=lambda r: str(r.get("filing_date") or ""), reverse=True)
    estimated = any(r.get("filing_date_estimated") for r in visible)
    warnings = ("filing_date_estimated_from_lag",) if estimated else ()
    return sentinels.ok(visible, source=result.source, as_of=cut, received_at=result.received_at, warnings=warnings)


def default_sources(*, filing_lag_days: int | None = None) -> dict[str, Callable[..., Any]]:
    fd, av = FilingDateSource(), AlphaVantageSource()

    def from_financial_datasets(symbol: str, statement: str, period: str, as_of: datetime) -> list[dict]:
        if statement != "metrics":
            raise NoDataError(symbol, None, f"financial_datasets serves metrics, not {statement!r}")
        return [m.to_dict() for m in fd.get_financial_metrics(symbol, as_of.strftime("%Y-%m-%d"), period="ttm" if period == "ttm" else period)]

    def from_alpha_vantage(symbol: str, statement: str, period: str, as_of: datetime) -> list[dict]:
        return av.get_statement(symbol, statement, period, filing_lag_days=filing_lag_days)

    return {"financial_datasets": from_financial_datasets, "alpha_vantage": from_alpha_vantage}

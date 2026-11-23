"""Fundamentals sources by filing date and the data models (SPEC-017: AH-43, FT-101, TA-76, TA-78, AH-45)."""
import json
from datetime import timezone

import pytest

from hokma.data import fundamentals_src as fs, sentinels
from hokma.data.models import (CompanyFacts, CompanyNews, EarningsRecord, Filing, FinancialMetrics, InsiderTrade, PeriodFundamentals)
from hokma.data.providers import NoDataError, NotConfiguredError, RateLimitError

from .support import RecordedTransport, constant, fixture_bytes, fixture_json, no_network, when  # noqa: F401


def key(provider: str) -> str | None:
    return "SECRET-KEY"


# ------------------------------------------------------------------------------------------------ AH-43
def metrics_answer(url: str, params: dict, headers: dict):
    rows = fixture_json("fd_financial_metrics.json")["financial_metrics"]
    bound = params.get("filing_date_lte")
    return 200, json.dumps({"financial_metrics": [r for r in rows if r.get("filing_date") and r["filing_date"] <= bound]}).encode()


def test_filing_date_source_filters_by_filing_not_by_period_AH_43() -> None:
    transport = RecordedTransport(metrics_answer)
    source = fs.FilingDateSource(key=key, transport=transport)
    rows = source.get_financial_metrics("ACME", "2024-04-30")
    assert [r.filing_date for r in rows][0] == "2024-02-01" and [r.filing_date for r in rows][-1] == "2023-02-02" and "2024-05-02" not in [r.filing_date for r in rows]
    assert transport.calls[0][1]["filing_date_lte"] == "2024-04-30" and transport.calls[0][2]["X-API-Key"] == "SECRET-KEY"
    assert all(isinstance(r, FinancialMetrics) for r in rows) and len(rows) == 5


def test_a_provider_that_ignores_the_bound_cannot_leak_the_future_AH_43() -> None:
    everything = RecordedTransport(constant(200, fixture_bytes("fd_financial_metrics.json")))
    rows = fs.FilingDateSource(key=key, transport=everything).get_financial_metrics("ACME", "2024-04-30")
    assert "2024-05-02" not in [r.filing_date for r in rows] and len(rows) == 5


def test_failures_raise_and_absence_is_empty_AH_43() -> None:
    sleeps: list[float] = []
    assert fs.FilingDateSource(key=key, transport=RecordedTransport(constant(404))).get_financial_metrics("X", "2024-01-01") == []
    with pytest.raises(NotConfiguredError):
        fs.FilingDateSource(key=lambda p: None, transport=RecordedTransport(constant(200, b"{}"))).get_financial_metrics("X", "2024-01-01")
    with pytest.raises(NotConfiguredError):
        fs.FilingDateSource(key=key, transport=RecordedTransport(constant(401))).get_financial_metrics("X", "2024-01-01")
    limited = RecordedTransport(constant(429))
    with pytest.raises(RateLimitError):
        fs.FilingDateSource(key=key, transport=limited, sleep=sleeps.append).get_financial_metrics("X", "2024-01-01")
    assert sleeps == [5, 15, 30] and len(limited.calls) == 4                      # three waits, then it gives up
    with pytest.raises(RateLimitError):
        fs.FilingDateSource(key=key, transport=RecordedTransport(constant(503))).get_financial_metrics("X", "2024-01-01")


def test_pagination_is_followed_to_the_end_AH_43() -> None:
    pages = [json.dumps({"insider_trades": [{"ticker": "ACME", "name": "A", "filing_date": "2024-01-02"}], "next_page_url": "https://next.test/p2"}),
             json.dumps({"insider_trades": [{"ticker": "ACME", "name": "B", "filing_date": "2024-01-03"}]})]
    transport = RecordedTransport(lambda url, p, h: (200, pages.pop(0).encode()))
    trades = fs.FilingDateSource(key=key, transport=transport).get_insider_trades("ACME", "2024-02-01")
    assert [t.name for t in trades] == ["A", "B"] and transport.calls[1][0] == "https://next.test/p2"
    facts = RecordedTransport(constant(200, fixture_bytes("fd_company_facts.json")))
    assert fs.FilingDateSource(key=key, transport=facts).get_company_facts("ACME").sector == "Technology"
    assert fs.FilingDateSource(key=key, transport=RecordedTransport(constant(404))).get_company_facts("NOPE") is None


def test_fundamentals_call_returns_only_what_was_public_with_source_and_reception_AH_43() -> None:
    transport = RecordedTransport(metrics_answer)
    sources = {"financial_datasets": lambda s, st, p, a: [m.to_dict() for m in fs.FilingDateSource(key=key, transport=transport).get_financial_metrics(s, a.strftime("%Y-%m-%d"))]}
    result = fs.fundamentals("ACME", as_of=when("2024-04-30"), statement="metrics", period="ttm", sources=sources)
    assert result.ok and result.source == "financial_datasets" and result.received_at and result.as_of == when("2024-04-30")
    assert [r["filing_date"] for r in result.value][0] == "2024-02-01"
    early = fs.fundamentals("ACME", as_of=when("2020-01-01"), statement="metrics", period="ttm", sources=sources)
    assert early.status == sentinels.NO_DATA and early.value is None


def test_fundamentals_without_keys_is_unavailable_with_the_provider_and_reason_AH_43() -> None:
    sources = fs.default_sources()
    result = fs.fundamentals("ACME", as_of=when("2024-04-30"), statement="metrics", period="ttm",
                             sources={n: (lambda *a, _f=f: _f(*a)) for n, f in sources.items()})
    # the real providers read their keys from Connections; with none saved the answer is a sentinel, not an error
    assert result.status in (sentinels.DATA_UNAVAILABLE, sentinels.NO_DATA) and result.value is None and result.source in ("financial_datasets", "alpha_vantage")


# ------------------------------------------------------------------------------------------------ FT-101
def test_accounts_are_translated_to_common_names_and_the_rest_is_listed_FT_101() -> None:
    values, unmapped = fs.normalize_accounts({"Total Revenue": 100, "totalRevenue": 100, "Net Income": "25.5", "Gross Profit": None, "weirdField": 1}, "income")
    assert values == {"revenue": 100.0, "net_income": 25.5, "gross_profit": None} and unmapped == ["weirdField"]
    balance, _ = fs.normalize_accounts({"Total Assets": 10, "totalShareholderEquity": 4, "Cash And Cash Equivalents": 1}, "balance")
    assert balance == {"total_assets": 10.0, "shareholders_equity": 4.0, "cash_and_equivalents": 1.0}
    cash, _ = fs.normalize_accounts({"Operating Cash Flow": 9, "Capital Expenditure": -2, "Free Cash Flow": 7}, "cashflow")
    assert cash["free_cash_flow"] == 7.0
    stats, _ = fs.normalize_accounts({"Market Cap": 5, "Trailing PE": "x"}, "statistics")
    assert stats == {"market_cap": 5.0, "price_to_earnings": None}
    with pytest.raises(ValueError):
        fs.normalize_accounts({}, "nonsense")
    assert set(fs.STATEMENTS) == {"income", "balance", "cashflow", "statistics"}


# ------------------------------------------------------------------------------------------------ TA-76
def av_source(answer=None):
    transport = RecordedTransport(answer or constant(200, fixture_bytes("av_income_statement.json")))
    return fs.AlphaVantageSource(key=key, transport=transport), transport


def test_alpha_vantage_statements_have_no_filing_date_unless_a_lag_is_stated_TA_76() -> None:
    source, transport = av_source()
    plain = source.get_statement("ACME", "income", "quarterly")
    assert [r["report_period"] for r in plain] == ["2024-03-31", "2023-12-31", "2023-09-30"] and all(r["filing_date"] is None for r in plain)
    assert plain[0]["revenue"] == 1200.0 and plain[0]["unmapped_accounts"] == ["weirdField"]
    assert transport.calls[0][1]["function"] == "INCOME_STATEMENT" and transport.calls[0][1]["apikey"] == "SECRET-KEY"
    lagged = source.get_statement("ACME", "income", "quarterly", filing_lag_days=45)
    assert lagged[0]["filing_date"] == "2024-05-15" and lagged[0]["filing_date_estimated"] is True


def test_fundamentals_from_alpha_vantage_follow_the_filing_rule_TA_76() -> None:
    source, _ = av_source()
    sources = {"alpha_vantage": lambda s, st, p, a: source.get_statement(s, st, p, filing_lag_days=45)}
    result = fs.fundamentals("ACME", as_of=when("2024-04-30"), statement="income", period="quarterly", sources=sources)
    assert result.ok and [r["report_period"] for r in result.value] == ["2023-12-31", "2023-09-30"]       # the March quarter is not filed yet
    assert "filing_date_estimated_from_lag" in result.warnings
    undated = {"alpha_vantage": lambda s, st, p, a: source.get_statement(s, st, p)}
    refused = fs.fundamentals("ACME", as_of=when("2024-04-30"), statement="income", period="quarterly", sources=undated)
    assert refused.status == sentinels.NO_DATA and refused.reason == sentinels.REASON_NOT_PUBLISHED      # no filing date: cannot be proven public


def test_alpha_vantage_failures_are_classified_TA_76() -> None:
    limited, _ = av_source(constant(200, json.dumps({"Information": "Our standard API rate limit is 25 requests per day"})))
    with pytest.raises(RateLimitError):
        limited.get_statement("ACME", "income")
    keyed, _ = av_source(constant(200, json.dumps({"Information": "the parameter apikey is invalid or missing"})))
    with pytest.raises(NotConfiguredError):
        keyed.get_statement("ACME", "income")
    with pytest.raises(NoDataError):
        av_source()[0].get_statement("ACME", "dividends")
    with pytest.raises(NoDataError):
        av_source(constant(200, b"not json"))[0].get_statement("ACME", "income")
    with pytest.raises(NotConfiguredError):
        fs.AlphaVantageSource(key=lambda p: None, transport=RecordedTransport(constant(200, b"{}"))).get_statement("ACME", "income")


def test_overview_is_withheld_for_a_past_as_of_TA_76() -> None:
    source, transport = av_source(constant(200, json.dumps({"Symbol": "ACME", "Sector": "Technology"})))
    past = source.get_overview("ACME", when("2024-03-01"), today="2026-11-22")
    assert past.status == sentinels.NO_DATA and past.reason == sentinels.REASON_LIVE_PROFILE and transport.calls == []
    live = source.get_overview("ACME", when("2026-11-22"), today="2026-11-22")
    assert live.ok and live.value["Sector"] == "Technology"


# ------------------------------------------------------------------------------------------------ TA-78
def test_statement_tables_and_insiders_are_cut_by_date_TA_78() -> None:
    table = fixture_json("yf_income_table.json")
    records = fs.statements_from_table("ACME", "income", table, filing_lag_days=40)
    assert {r["report_period"]: r["revenue"] for r in records} == {"2024-03-31": 1200.0, "2023-12-31": 1100.0}
    assert records[0]["unmapped_accounts"] == ["Odd Account"] and records[0]["filing_date"] == "2024-05-10"
    assert all(r["filing_date"] is None for r in fs.statements_from_table("ACME", "income", table))
    trades = fs.insider_transactions("ACME", fixture_json("yf_insiders.json"), when("2024-05-01"))
    assert [t.name for t in trades] == ["A. Director"]                    # the May sale is after as_of; the row with no date is excluded
    assert trades[0].transaction_type == "Purchase" and trades[0].transaction_shares == 1000.0 and isinstance(trades[0], InsiderTrade)
    assert len(fs.insider_transactions("ACME", fixture_json("yf_insiders.json"), when("2024-06-01"))) == 2


# ------------------------------------------------------------------------------------------------ AH-45
def test_models_parse_strictly_on_identity_and_tolerantly_on_the_rest_AH_45() -> None:
    row = {"ticker": "ACME", "report_period": "2023-12-31", "filing_date": "2024-02-01", "market_cap": "1.4e11", "net_margin": "n/a", "surprise_field": 1}
    parsed = PeriodFundamentals.from_record(row)
    assert parsed.market_cap == 1.4e11 and parsed.net_margin is None and parsed.period == "ttm" and parsed.filing_date == "2024-02-01"
    with pytest.raises(ValueError, match="report_period"):
        PeriodFundamentals.from_record({"ticker": "ACME"})
    assert FinancialMetrics.from_record({**row, "peg_ratio": 1.5}).peg_ratio == 1.5
    assert "peg_ratio" not in PeriodFundamentals.from_record(row).to_dict()
    assert InsiderTrade.from_record({"ticker": "A", "name": "N", "is_board_director": "true"}).is_board_director is True
    assert CompanyNews.from_record({"ticker": "A", "title": "T"}).source == ""
    assert CompanyFacts.from_record({"ticker": "A", "market_cap": float("nan")}).market_cap is None
    assert Filing.from_record({"ticker": "A", "filing_type": "10-Q", "filing_date": "2024-05-02"}).filing_type == "10-Q"
    earnings = EarningsRecord.from_record({"ticker": "A", "report_period": "2024-03-31", "quarterly": {"revenue": "10", "net_income": 2}})
    assert earnings.quarterly.revenue == 10.0 and earnings.annual is None

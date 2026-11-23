"""ED-025, ED-026, ED-028 and ED-029: statements, ratios without mixed periods, growth, entity facts and TTM, point in time."""
from __future__ import annotations

from datetime import date

import pytest

from hokma.data.sec import financials as fin
from hokma.data.sec.result import DATA_UNAVAILABLE, NO_DATA, OK
from tests.data.sec.support import DownTransport, make_client

AS_OF = date(2024, 3, 1)


def test_statement_getters_latest_annual_period_ED_028():
    got = fin.statement(make_client(), "NWND", as_of=AS_OF)
    snap = got.value
    assert got.status == OK and snap.period == "2023-FY" and snap.period_end == date(2023, 12, 31)
    values = {k: o.value for k, o in snap.operands.items()}
    assert values["revenue"] == 412.0e6 and values["net_income"] == 38.4e6 and values["gross_profit"] == 171.0e6
    assert values["operating_income"] == 50.0e6 and values["total_assets"] == 338.0e6 and values["cash"] == 61.5e6
    assert values["total_liabilities"] == 120.0e6 and values["shareholders_equity"] == 218.0e6
    assert values["operating_cash_flow"] == 55.0e6 and values["capex"] == 14.0e6
    assert snap.free_cash_flow == 55.0e6 - 14.0e6
    assert snap.operands["revenue"].period_start == date(2023, 1, 1) and snap.operands["total_assets"].period_start is None
    assert "missing:current_assets" in snap.warnings


def test_statement_does_not_see_the_next_annual_report_ED_028():
    snap = fin.statement(make_client(), "NWND", as_of=date(2023, 3, 1)).value
    assert snap.period == "2022-FY" and snap.operands["revenue"].value == 367.9e6 and snap.operands["total_assets"].value == 301.0e6


def test_statement_quarter_takes_the_quarter_not_the_year_to_date_ED_028():
    snap = fin.statement(make_client(), "NWND", as_of=date(2024, 5, 10), period="2024-Q1").value
    assert snap.operands["revenue"].value == 108.2e6
    assert (snap.operands["revenue"].period_end - snap.operands["revenue"].period_start).days == 90
    assert snap.operands["total_assets"].value == 344.5e6
    assert snap.free_cash_flow is None                       # cash flow is only reported for the year


def test_ratios_same_period_ED_029():
    got = fin.calculate_ratios(make_client(), "NWND", as_of=AS_OF)
    ratios = got.value.ratios
    assert got.status == OK and got.value.period == "2023-FY"
    assert ratios["gross_margin"] == pytest.approx(171.0 / 412.0, abs=1e-12)
    assert ratios["net_margin"] == pytest.approx(38.4 / 412.0, abs=1e-12)
    assert ratios["return_on_assets"] == pytest.approx(38.4 / 338.0, abs=1e-12)
    assert ratios["return_on_equity"] == pytest.approx(38.4 / 218.0, abs=1e-12)
    assert ratios["liabilities_to_assets"] == pytest.approx(120.0 / 338.0, abs=1e-12)
    assert ratios["cash_conversion"] == pytest.approx(55.0 / 38.4, abs=1e-12)
    assert ratios["current_ratio"] is None and any(w.startswith("current_ratio:missing_operand") for w in got.warnings)


def test_ratios_never_mix_periods_ED_025():
    got = fin.calculate_ratios(make_client(), "NWND", as_of=date(2024, 5, 10), period="2024-Q1")
    ratios = got.value.ratios
    assert ratios["net_margin"] == pytest.approx(9.7 / 108.2, abs=1e-12)
    assert ratios["gross_margin"] is None                    # the annual gross profit is not divided by quarterly revenue
    assert "gross_margin:missing_operand:gross_profit" in got.warnings
    assert ratios["capex_to_revenue"] is None


def test_ratio_of_refuses_mismatched_periods_ED_025():
    annual_ni = fin.Operand("net_income", 38.4e6, period_start=date(2023, 1, 1), period_end=date(2023, 12, 31))
    quarter_rev = fin.Operand("revenue", 108.2e6, period_start=date(2024, 1, 1), period_end=date(2024, 3, 31))
    assert fin.ratio_of(annual_ni, quarter_rev) == (None, "period_mismatch:net_income/revenue")
    ytd = fin.Operand("net_income", 18.0e6, period_start=date(2023, 1, 1), period_end=date(2023, 6, 30))
    quarter = fin.Operand("revenue", 103.0e6, period_start=date(2023, 4, 1), period_end=date(2023, 6, 30))
    assert fin.ratio_of(ytd, quarter)[1] == "period_mismatch:net_income/revenue"      # same end date, different length
    balance = fin.Operand("total_assets", 338.0e6, period_end=date(2023, 12, 31))
    assert fin.ratio_of(annual_ni, balance)[0] == pytest.approx(38.4 / 338.0, abs=1e-12)   # an instant matches by end date
    assert fin.ratio_of(annual_ni, fin.Operand("revenue", None))[1] == "missing_operand:revenue"
    assert fin.ratio_of(annual_ni, fin.Operand("revenue", 0.0, period_start=date(2023, 1, 1), period_end=date(2023, 12, 31)))[1] == "zero_denominator:revenue"


def test_growth_year_over_year_ED_029():
    got = fin.calculate_growth(make_client(), "NWND", "revenue", as_of=AS_OF)
    rows = got.value
    assert [r["fiscal_year"] for r in rows] == [2022, 2023]
    assert rows[0]["growth"] is None
    assert rows[1]["growth"] == pytest.approx(412.0 / 367.9 - 1.0, abs=1e-12) and rows[1]["value"] == 412.0e6


def test_growth_none_when_the_previous_year_is_missing_ED_029():
    rows = fin.calculate_growth(make_client(), "NWND", "total_liabilities", as_of=AS_OF).value
    assert [r["fiscal_year"] for r in rows] == [2023] and rows[0]["growth"] is None


def test_entity_summary_and_concept_facts_ED_029():
    client = make_client()
    summary = fin.entity_summary(client, "NWND", as_of=date(2024, 5, 10)).value
    assert summary["name"] == "Northwind Instruments Inc." and summary["cik"] == 1000001
    assert set(summary["forms"]) == {"10-K", "10-Q", "10-Q/A"} and summary["latest_filing_date"] == "2024-05-02"
    rows = fin.concept_facts(client, "NWND", "Revenues", as_of=date(2024, 5, 10)).value
    assert len(rows) == 14 and all(r["filed"] <= "2024-05-10" for r in rows)       # the 2024 Q2 fact was filed 2024-08-01
    assert fin.concept_facts(client, "NWND", "us-gaap:Revenues", as_of=date(2024, 5, 10)).value == rows
    assert fin.concept_facts(client, "NWND", "Nonexistent", as_of=AS_OF).status == NO_DATA


def test_ttm_four_quarters_with_derived_fourth_quarter_ED_026():
    got = fin.ttm(make_client(), "NWND", "Revenues", as_of=date(2024, 5, 10))
    data = got.value
    assert got.status == OK
    assert data["value"] == pytest.approx(103.0e6 + 104.5e6 + 104.5e6 + 108.2e6, abs=1e-6)       # Q2 23 + Q3 23 + Q4 23 + Q1 24
    assert data["periods"] == [[2023, "Q2"], [2023, "Q3"], [2023, "Q4"], [2024, "Q1"]]
    assert data["window_end"] == "2024-03-31" and data["has_calculated_q4"] is True


def test_ttm_hides_facts_filed_after_as_of_ED_026():
    data = fin.ttm(make_client(), "NWND", "us-gaap:Revenues", as_of=date(2024, 3, 1)).value
    assert data["window_end"] == "2023-12-31"
    assert data["value"] == pytest.approx(100.4e6 + 103.0e6 + 104.5e6 + 104.5e6, abs=1e-6)       # Q1 2023 as restated on 2023-12-15


def test_ttm_gap_inside_the_window_is_no_data_and_not_filled_ED_026():
    got = fin.ttm(make_client(), "GAPC", "Revenues", as_of=date(2023, 9, 1))
    assert got.status == NO_DATA and got.value is None and got.reason == "no_gap_free_four_quarter_window"


def test_ttm_window_older_than_185_days_is_no_data_ED_026():
    got = fin.ttm(make_client(), "NWND", "Revenues", as_of=date(2025, 6, 30))
    assert got.status == NO_DATA and got.reason.startswith("ttm_window_ends_") and got.value is None
    near = fin.ttm(make_client(), "NWND", "Revenues", as_of=date(2024, 9, 28))               # 181 days after the window end
    assert near.status == OK and near.value["window_end"] == "2024-06-30"


def test_ttm_share_counts_are_put_on_the_split_basis_ED_026():
    got = fin.ttm(make_client(), "SPLT", "WeightedAverageNumberOfDilutedSharesOutstanding", as_of=date(2024, 3, 1))
    assert got.status == OK
    assert got.value["value"] == pytest.approx(20.0e6, abs=1e-6) and 17.5e6 != got.value["value"]       # Q1 filed before the split (10, doubled) + Q2, Q3, Q4 already on the new basis (20 each), averaged
    assert got.value["splits"] == [{"date": "2023-07-10", "ratio": 2.0}]
    flat = fin.ttm(make_client(), "SPLT", "Revenues", as_of=date(2024, 3, 1))
    assert flat.value["value"] == pytest.approx(26.0e6, abs=1e-6) and flat.value["splits"] == []


def test_financial_functions_report_missing_identity_and_provider_down_ED_028():
    assert fin.statement(make_client(identity=None), "NWND", as_of=AS_OF).reason == "sec_identity_missing"
    for got in (fin.statement(make_client(DownTransport()), "NWND", as_of=AS_OF), fin.ttm(make_client(DownTransport()), 1000001, "Revenues", as_of=AS_OF)):
        assert got.status == DATA_UNAVAILABLE and got.value is None and got.reason.startswith("sec_network")


def test_company_without_facts_is_no_data_ED_028():
    got = fin.statement(make_client(), 933972, as_of=AS_OF)
    assert got.status == NO_DATA and got.reason == "no_company_facts"

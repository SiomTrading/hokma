"""ED-012 to ED-016: fund filings, BDC portfolios, asset backed data, ATS-N notices, N-PX votes and proxy statements."""
from __future__ import annotations

from datetime import date

import pytest

from hokma.data.sec import funds
from hokma.data.sec.result import DATA_UNAVAILABLE, NO_DATA, OK
from hokma.data.sec.xbrl import parse_instance
from tests.data.sec.support import FIXTURES, DownTransport, make_client


def test_nport_portfolio_ED_012():
    got = funds.fund_filings(make_client(), 1000002, as_of=date(2024, 6, 1), form="NPORT-P")
    fund = got.value[0]
    assert got.status == OK and fund.kind == "portfolio" and fund.name == "Timeliness Rule Testing" and fund.period == date(2017, 10, 22)
    assert fund.data["series_id"] == "S000002112" and fund.data["position_count"] == 1
    position = fund.data["positions"][0]
    assert (position.name, position.cusip, position.balance, position.units, position.value_usd, position.pct_value) == ("3F708", "379006355", 189.0, "NS", 1134.0, 0.17)
    assert (position.asset_category, position.issuer_category, position.country) == ("EC", "CORP", "US")
    assert fund.data["invested_value_usd"] == 1134.0


def test_nport_not_visible_before_filing_ED_012():
    assert funds.fund_filings(make_client(), 1000002, as_of=date(2024, 5, 27), form="NPORT-P").status == NO_DATA


def test_money_market_report_ED_012():
    fund = funds.fund_filings(make_client(), 1000002, as_of=date(2026, 3, 1), form="N-MFP3").value[0]
    assert fund.kind == "money_market" and fund.name == "Test Series" and fund.period == date(2026, 1, 31)
    assert fund.data["net_assets"] == 1000000.0 and fund.data["security_count"] == 2 and fund.data["fund_category"] == "Government"
    assert fund.data["weighted_average_maturity"] == 30 and fund.data["weighted_average_life"] == 60
    assert [s["issuer"] for s in fund.data["securities"]] == ["US Treasury", "Repo Bank"] and fund.data["securities"][1]["has_repo"] is True


def test_census_report_N_CEN_ED_012():
    got = funds.fund_filings(make_client(), 81443, as_of=date(2026, 3, 1), form="N-CEN")
    census = got.value[0]
    assert got.status == OK and census.kind == "census" and census.name == "AB CAP FUND, INC." and census.period == date(2025, 11, 30)
    assert census.data["num_series"] == 3 and census.data["classification_type"] == "N-1A" and census.data["is_etf_company"] is False
    assert [s["name"] for s in census.data["series"]][0] == "AB All China Equity Portfolio" and census.data["service_provider_count"] == 52
    assert funds.fund_filings(make_client(), 81443, as_of=date(2026, 2, 11), form="N-CEN").status == NO_DATA


def test_unknown_form_is_not_read_ED_012():
    text = (FIXTURES / "sgml" / "0001000001-24-000030.txt").read_text()
    with pytest.raises(ValueError):
        funds.parse_fund_text(text)


def test_bdc_investments_non_accrual_and_industry_ED_013():
    summary = funds.bdc_investments(parse_instance((FIXTURES / "bdc_instance.xml").read_text()))
    by_id = {i.identifier: i for i in summary["investments"]}
    assert set(by_id) == {"AlphaSoftTermLoan", "BetaHealthRevolver", "GammaSoftNote"}
    assert by_id["GammaSoftNote"].non_accrual is True and by_id["AlphaSoftTermLoan"].non_accrual is False
    assert (by_id["AlphaSoftTermLoan"].fair_value, by_id["AlphaSoftTermLoan"].cost, by_id["AlphaSoftTermLoan"].principal) == (20000000.0, 21000000.0, 21000000.0)
    assert summary["total_fair_value"] == 34000000.0 and summary["total_cost"] == 40000000.0
    assert summary["non_accrual_cost_share"] == pytest.approx(9.0 / 40.0, abs=1e-12)
    assert summary["non_accrual_fair_value_share"] == pytest.approx(4.0 / 34.0, abs=1e-12)
    assert summary["fair_value_by_industry"] == {"Healthcare": 10000000.0, "Software": 24000000.0}


def test_bdc_portfolio_service_ED_013():
    got = funds.bdc_portfolio(make_client(), 1000030, as_of=date(2024, 3, 1))
    assert got.status == OK and got.value["ref"].accession == "0001000030-24-000007" and len(got.value["investments"]) == 3
    assert funds.bdc_portfolio(make_client(), 1000030, as_of=date(2024, 2, 27)).status == NO_DATA


def test_abs_loan_level_summary_ED_014():
    fund = funds.fund_filings(make_client(), 1000020, as_of=date(2024, 7, 1), form="ABS-EE").value[0]
    data = fund.data
    assert fund.kind == "asset_data" and data["asset_count"] == 3 and fund.period == date(2024, 5, 31)
    assert data["pool_balance"] == 23400 + 30250 + 0
    assert data["weighted_average_rate"] == pytest.approx((23400 * 5.9 + 30250 * 7.4) / (23400 + 30250), abs=1e-9)
    assert data["delinquent_count"] == 1 and data["delinquent_share"] == pytest.approx(1 / 3, abs=1e-12) and data["paid_off_count"] == 1
    assert data["fields"]["obligorCreditScore"] == {"count": 3, "sum": 2147.0, "mean": 2147.0 / 3, "min": 655.0, "max": 780.0}
    assert data["categories"]["vehicleNewUsedCode"] == {"1": 2, "2": 1}


def test_abs_without_records_is_an_error_ED_014():
    with pytest.raises(ValueError):
        funds.parse_abs_assets("<assetData/>")
    with pytest.raises(ValueError):
        funds.parse_abs_assets("")


def test_ats_notice_withdrawal_ED_015():
    got = funds.fund_filings(make_client(), 782124, as_of=date(2025, 1, 1), form="ATS-N-W")
    notice = got.value[0]
    assert got.status == OK and notice.kind == "ats_notice" and notice.name == "JPB-X"
    assert notice.data["mpid"] == "JPBX" and notice.data["notice_kind"] == "withdrawal" and notice.data["withdrawn_accession"] == "0000019617-24-000662"
    assert notice.data["operator"] == "J.P. MORGAN SECURITIES LLC" and notice.data["operator_cik"] == 782124 and notice.period == date(2024, 12, 23)
    assert funds.fund_filings(make_client(), 782124, as_of=date(2024, 12, 22), form="ATS-N-W").status == NO_DATA


def test_ats_kind_ED_015():
    assert [funds.ats_kind(f) for f in ("ATS-N", "ATS-N/UA", "ATS-N/CA", "ATS-N-W", "ATS-N/MA", "ATS-N/W")] == [
        "initial", "updating_amendment", "amendment", "withdrawal", "amendment", "withdrawal"]


def test_npx_votes_ED_016():
    got = funds.proxy_votes(make_client(), 1000002, as_of=date(2024, 9, 1))
    data = got.value[0].data
    assert got.status == OK and data["vote_count"] == 7 and data["by_vote"] == {"ABSTAIN": 1, "FOR": 4, "3 YEARS": 1, "AGAINST": 1}
    first = data["votes"][0]
    assert (first["issuer"], first["cusip"], first["how_voted"], first["management_recommendation"]) == ("BIGCOMMERCE HOLDINGS, INC.", "08975P108", "ABSTAIN", "AGAINST")
    assert first["categories"] == ["SECTION 14A SAY-ON-PAY VOTES"] and data["against_management"] == 0
    assert funds.proxy_votes(make_client(), 1000002, as_of=date(2024, 8, 29)).status == NO_DATA


def test_proxy_statement_ED_016():
    got = funds.proxy_statement(make_client(), "NWND", as_of=date(2024, 6, 1))
    info = got.value["info"]
    assert got.status == OK and info.meeting_date == date(2024, 6, 12) and info.record_date == date(2024, 4, 15)
    assert [n for n, _ in info.proposals] == [1, 2, 3] and info.proposals[0][1] == "Election of seven directors"
    assert info.say_on_pay and info.auditor_ratification and not info.contested
    ceo = info.executive_compensation[0]
    assert ceo["salary"] == 650000.0 and ceo["bonus"] == 400000.0 and ceo["total"] == 2150000.0 and ceo["year"] == 2023
    assert funds.proxy_statement(make_client(), "NWND", as_of=date(2024, 4, 4)).status == NO_DATA


def test_proxy_contest_signal_ED_016():
    info = funds.parse_proxy_statement("<html><body><p>Proposal 1: Election of directors</p><p>Please use the universal proxy card to vote for the dissident nominees.</p></body></html>")
    assert info.contested and info.proposals == ((1, "Election of directors"),) and info.meeting_date is None and info.executive_compensation == ()


def test_fund_services_without_identity_or_provider_ED_012():
    assert funds.fund_filings(make_client(identity=None), 1000002, as_of=date(2024, 6, 1)).reason == "sec_identity_missing"
    got = funds.proxy_statement(make_client(DownTransport()), "NWND", as_of=date(2024, 6, 1))
    assert got.status == DATA_UNAVAILABLE and got.value is None

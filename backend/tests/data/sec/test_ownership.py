"""ED-005, ED-006 and ED-007: insiders (Forms 3, 4, 5), notices of proposed sale (Form 144), 13D and 13G, point in time."""
from __future__ import annotations

from datetime import date

import pytest

from hokma.data.sec import ownership as own
from hokma.data.sec.result import DATA_UNAVAILABLE, NO_DATA, OK
from tests.data.sec.support import DownTransport, make_client


def test_form4_award_and_option_grant_ED_005():
    got = own.insider_filings(make_client(), "SCWO", as_of=date(2025, 6, 1))
    report = got.value[0]
    assert got.status == OK and report.form == "4" and report.ref.accession == "0002064133-25-000001"
    assert (report.issuer_name, report.ticker, report.issuer_cik) == ("374Water Inc.", "SCWO", "0000933972")
    assert report.owner_name == "Melkote Rajesh Ramaswamy" and report.is_officer and not report.is_director
    assert report.officer_title == "Chief Technology Officer" and report.period == date(2025, 4, 30)
    award, option = report.transactions
    assert (award.code, award.shares, award.price, award.acquired_disposed, award.derivative) == ("A", 757756.0, 0.0, "A", False)
    assert award.remaining == 757756.0 and award.direct_indirect == "D"
    assert (option.derivative, option.shares, option.exercise_price, option.expiration) == (True, 757576.0, 0.33, "2035-04-29")
    assert option.underlying == "Common Stock" and option.underlying_shares == 757576.0
    assert report.shares_bought() == 0.0 and report.shares_sold() == 0.0           # an award is not an open market purchase


def test_form3_holdings_and_form5_gift_ED_005():
    got = own.insider_filings(make_client(), "SNOW", as_of=date(2025, 6, 1))
    by_form = {r.form: r for r in got.value}
    assert set(by_form) == {"3", "5"}
    form3 = by_form["3"]
    assert form3.is_director and form3.period == date(2020, 9, 15) and len(form3.holdings) == 6 and form3.transactions == ()
    assert form3.holdings[1].exercise_price == "3.74" and form3.holdings[1].underlying_shares == 383594.0 and form3.holdings[1].derivative
    gift = by_form["5"].transactions[0]
    assert (gift.code, gift.transaction_type, gift.shares, gift.remaining) == ("G", "Gift", 61386.0, 54666.0)


def test_insider_forms_filed_after_as_of_are_not_visible_ED_005():
    got = own.insider_filings(make_client(), "SCWO", as_of=date(2025, 5, 1))           # filed 2025-05-02
    assert got.status == NO_DATA and got.value is None


def test_insider_activity_counts_only_open_market_trades_in_the_window_ED_005():
    got = own.insider_activity(make_client(), "NWND", as_of=date(2024, 5, 30), window_days=90)
    act = got.value
    assert got.status == OK and act.window_start == date(2024, 3, 1)
    assert act.shares_bought == 5000.0 and act.shares_sold == 2000.0 and act.net_shares == 3000.0
    assert act.value_bought == pytest.approx(5000 * 24.10, abs=1e-9) and act.value_sold == pytest.approx(2000 * 26.00, abs=1e-9)
    assert act.insiders == ("Okafor Daniel T", "Reyes Marta L") and act.reports == 2       # the 2023 purchase is outside the window; code M is ignored


def test_insider_activity_before_the_sale_was_filed_ED_005():
    act = own.insider_activity(make_client(), "NWND", as_of=date(2024, 5, 10), window_days=90).value
    assert act.shares_sold == 0.0 and act.shares_bought == 5000.0 and act.reports == 1


def test_insider_activity_longer_window_includes_the_older_purchase_ED_005():
    act = own.insider_activity(make_client(), "NWND", as_of=date(2024, 5, 30), window_days=240).value
    assert act.shares_bought == 6000.0 and act.reports == 3


def test_form144_notice_ED_006():
    got = own.sale_notices(make_client(), 1118676, as_of=date(2025, 1, 1))
    notice = got.value[0]
    assert got.status == OK
    assert (notice.issuer_name, notice.issuer_cik, notice.seller) == ("RUFUS TEST CO", "0001118676", "van der Velden Jan")
    assert notice.relationships == ("Officer", "Director", "Stakeholder") and notice.notice_date == date(2022, 9, 8)
    assert notice.units_to_be_sold == 17087 + 18000 and notice.securities_to_be_sold[0]["exchange_name"] == "CHX"
    assert notice.sold_past_three_months[0]["seller_name"] == "Virtu Financial" and notice.remarks == "This is new 144 form"


def test_form144_not_visible_before_its_filing_day_ED_006():
    assert own.sale_notices(make_client(), 1118676, as_of=date(2022, 9, 7)).status == NO_DATA
    assert own.sale_notices(make_client(), 1118676, as_of=date(2022, 9, 8)).status == OK


def test_schedule13d_amendment_with_two_reporting_persons_ED_007():
    got = own.beneficial_owners(make_client(), 1422142, as_of=date(2025, 6, 1))
    report = got.value[0]
    assert got.status == OK and report.kind == "13D" and report.is_amendment
    assert (report.issuer_name, report.cusip, report.event_date) == ("Aadi Bioscience, Inc.", "00032Q104", date(2024, 12, 31))
    fund, person = report.persons
    assert (fund.name, fund.aggregate_amount, fund.percent_of_class, fund.shared_voting_power) == ("BML Investment Partners, L.P.", 2100000.0, 8.5, 2100000.0)
    assert (person.name, person.aggregate_amount, person.percent_of_class, person.sole_voting_power) == ("Leonard Braden Michael", 2435000.0, 9.9, 335000.0)
    assert report.max_percent == 9.9


def test_schedule13g_passive_holder_ED_007():
    report = own.beneficial_owners(make_client(), 1909747, as_of=date(2025, 12, 1), kinds=("13G",)).value[0]
    assert report.kind == "13G" and not report.is_amendment and report.rule_designation == "Rule 13d-1(c)"
    assert report.max_percent == 5.1 and report.event_date == date(2025, 11, 19) and report.persons[0].aggregate_amount == 10000000.0
    assert own.beneficial_owners(make_client(), 1909747, as_of=date(2025, 12, 1), kinds=("13D",)).status == NO_DATA


def test_schedule13g_filed_after_as_of_is_hidden_ED_007():
    assert own.beneficial_owners(make_client(), 1909747, as_of=date(2025, 11, 19)).status == NO_DATA


def test_ownership_without_identity_or_provider_is_data_unavailable_ED_005():
    assert own.insider_filings(make_client(identity=None), "SCWO", as_of=date(2025, 6, 1)).reason == "sec_identity_missing"
    got = own.beneficial_owners(make_client(DownTransport()), 1422142, as_of=date(2025, 6, 1))
    assert got.status == DATA_UNAVAILABLE and got.value is None

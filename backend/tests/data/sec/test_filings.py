"""ED-008, ED-009, ED-010, ED-011, ED-030 and TA-69: typed filings, point in time, the document parser, fundamentals as filed."""
from __future__ import annotations

from datetime import date, datetime, timezone

from hokma.data.sec import filings
from hokma.data.sec.result import DATA_UNAVAILABLE, NO_DATA, OK
from tests.data.sec.support import DownTransport, make_client


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_list_filings_hides_later_filings_ED_009():
    client = make_client()
    got = filings.list_filings(client, "NWND", as_of=date(2024, 5, 10))
    assert got.status == OK and got.source == "sec_edgar" and got.received_at is not None
    forms = [(r.form, r.filing_date.isoformat()) for r in got.value]
    assert ("10-Q", "2024-05-02") in forms
    assert all(r.filing_date <= date(2024, 5, 10) for r in got.value)
    assert not any(r.form == "S-3" for r in got.value)          # filed 2024-06-10
    assert got.warnings and "hidden by as_of" in got.warnings[0]


def test_filing_of_the_same_day_after_as_of_time_is_not_visible_ED_009():
    client = make_client()
    morning = filings.list_filings(client, "NWND", as_of=_at("2024-05-02T12:00:00"), form="10-Q")
    assert morning.status == NO_DATA                             # the 10-Q was accepted at 16:05 UTC
    evening = filings.list_filings(client, "NWND", as_of=_at("2024-05-02T17:00:00"), form="10-Q")
    assert evening.status == OK and evening.value[0].accession == "0001000001-24-000020"


def test_8k_typed_earnings_report_ED_008():
    client = make_client()
    got = filings.current_report(client, "NWND", as_of=date(2024, 6, 1))
    report = got.value
    assert got.status == OK
    assert report.ref.accession == "0001000001-24-000021"
    assert [i.number for i in report.items] == ["2.02", "9.01"]
    assert report.is_earnings is True
    assert report.event_date == date(2024, 5, 2)
    assert len(report.press_releases) == 1 and "Revenue of $108.2 million" in report.press_releases[0]
    assert report.items[0].title.startswith("Results of Operations")


def test_8k_filed_after_as_of_does_not_appear_ED_008():
    client = make_client()
    got = filings.current_report(client, "NWND", as_of=date(2024, 5, 1))
    assert got.status == NO_DATA and got.value is None
    later = filings.current_report(client, "NWND", as_of=date(2024, 7, 20))
    assert later.value.ref.accession == "0001000001-24-000030"
    assert [i.number for i in later.value.items] == ["5.02"] and later.value.is_earnings is False


def test_periodic_report_sections_and_exhibits_ED_009():
    client = make_client()
    got = filings.periodic_report(client, "NWND", as_of=date(2024, 3, 1), form="10-K")
    report = got.value
    assert got.status == OK and report.form == "10-K" and report.period == date(2023, 12, 31)
    assert report.sections["business"].startswith("Northwind designs and sells precision sensors")
    assert "five largest customers account for 48 percent" in report.sections["risk_factors"]
    assert "Revenue grew 12 percent to 412.0 million dollars" in report.sections["mdna"]
    assert report.exhibits == ("EX-21.1:nwnd-ex211.htm", "EX-101.INS:nwnd-20231231.xml")


def test_periodic_report_not_visible_before_filing_ED_009():
    client = make_client()
    got = filings.periodic_report(client, "NWND", as_of=date(2023, 2, 20), form="10-K")
    assert got.status == NO_DATA and got.reason == "no_filing_visible_at_as_of"


def test_quarterly_report_uses_quarterly_item_names_ED_009():
    client = make_client()
    report = filings.periodic_report(client, "NWND", as_of=date(2024, 5, 10), form="10-Q").value
    assert "108.2 million dollars" in report.sections["mdna"]
    assert report.auditor is None


def test_auditor_and_subsidiaries_ED_010():
    client = make_client()
    got = filings.auditor_and_subsidiaries(client, "NWND", as_of=date(2024, 3, 1))
    auditor = got.value["auditor"]
    assert auditor.name == "Harlow & Finch LLP" and auditor.auditor_since == 2009 and auditor.pcaob_id == "1234"
    assert auditor.city == "Boston, Massachusetts" and auditor.report_date == date(2024, 2, 20)
    subs = {s.name: s.jurisdiction for s in got.value["subsidiaries"]}
    assert subs == {"Northwind Sensors GmbH": "Germany", "Northwind Asia Pte. Ltd.": "Singapore", "Northwind Labs LLC": "Delaware"}


def test_parse_subsidiaries_from_lines_ED_010():
    html = "<html><body><p>Subsidiaries of Example Corp</p><p>Example Europe Ltd. (United Kingdom)</p><p>Example Labs LLC (Delaware)</p></body></html>"
    subs = filings.parse_subsidiaries(html)
    assert [(s.name, s.jurisdiction) for s in subs] == [("Example Europe Ltd.", "United Kingdom"), ("Example Labs LLC", "Delaware")]


def test_offerings_shelf_and_prospectus_ED_011():
    client = make_client()
    shelf = filings.offerings(client, "NWND", as_of=date(2024, 6, 11), form="S-3").value
    assert shelf.kind == "shelf_registration" and shelf.amounts == (150000000.0,)
    assert filings.offerings(client, "NWND", as_of=date(2024, 6, 11), form="424B5").status == NO_DATA   # filed 2024-06-12
    prospectus = filings.offerings(client, "NWND", as_of=date(2024, 6, 12), form="424B5").value
    assert prospectus.kind == "debt_or_equity_prospectus" and "3,000,000 shares" in prospectus.text_excerpt


def test_offering_form_d_has_issuer_and_amounts_ED_011():
    client = make_client()
    got = filings.offerings(client, 1296490, as_of=date(2024, 2, 1), form="D")
    offering = got.value
    assert got.status == OK and offering.kind == "private_placement"
    assert offering.details["issuer"] and offering.details["offering"]
    assert "INVESTURE OPPORTUNITY FUND, LP" in str(offering.details["issuer"])
    sales = offering.details["offering"]["offering_sales_amounts"]
    assert sales["total_amount_sold"] == "561224695" and sales["total_offering_amount"] == "Indefinite"
    assert offering.details["offering"]["investors"]["total_already_invested"] == "45"


def test_parse_document_sections_tables_and_repeated_items_ED_030():
    html = (
        "<html><head><style>p{}</style></head><body><p>Item 1. Business</p><p>Table of contents entry</p>"
        "<p>Item 1A. Risk Factors</p><p>Item 1. Business</p><p>The real business text is long enough to win.</p>"
        "<p>Item 1A. Risk Factors</p><p>The real risk text.</p><table><tr><td>a</td><td>1</td></tr><tr><td>b</td><td>2</td></tr></table></body></html>"
    )
    doc = filings.parse_document(html)
    assert doc.sections["item_1"] == "The real business text is long enough to win."
    assert doc.sections["item_1a"].startswith("The real risk text.")
    assert doc.tables == ((("a", "1"), ("b", "2")),)
    assert "p{}" not in doc.text


def test_parse_document_plain_text_ED_030():
    doc = filings.parse_document("ITEM 7. MD&A\nRevenue rose.\nITEM 8. Financial statements\nSee below.")
    assert doc.sections["item_7"] == "Revenue rose." and doc.section_titles["item_8"] == "Financial statements"


def test_parse_auditor_without_report_gives_none_fields_ED_010():
    info = filings.parse_auditor("No audit report here.")
    assert info.name is None and info.pcaob_id is None


def test_fundamentals_as_filed_quarterly_with_filing_dates_TA_69():
    client = make_client()
    got = filings.fundamentals_as_filed(client, "NWND", as_of=date(2024, 5, 10), statement="income_statement", freq="quarterly")
    data = got.value
    revenue = data["lines"]["revenue"]
    assert got.status == OK
    assert revenue["2024-03-31"] == {"value": 108.2e6, "filed": "2024-05-02", "form": "10-Q", "accession": "0001000001-24-000020", "unit": "USD", "tag": "Revenues"}
    assert "2024-06-30" not in revenue                        # filed 2024-08-01, after as_of
    assert revenue["2023-03-31"]["value"] == 100.4e6          # restated by the 10-Q/A filed 2023-12-15
    assert "2022-12-31" not in revenue                        # that duration spans a year, not a quarter


def test_fundamentals_as_filed_amendment_counts_from_its_filing_day_TA_69():
    client = make_client()
    before = filings.fundamentals_as_filed(client, "NWND", as_of=date(2023, 12, 14)).value["lines"]["revenue"]["2023-03-31"]
    after = filings.fundamentals_as_filed(client, "NWND", as_of=date(2023, 12, 15)).value["lines"]["revenue"]["2023-03-31"]
    assert before["value"] == 100.0e6 and before["filed"] == "2023-05-02"
    assert after["value"] == 100.4e6 and after["filed"] == "2023-12-15"


def test_fundamentals_as_filed_annual_cash_flow_TA_69():
    client = make_client()
    data = filings.fundamentals_as_filed(client, "NWND", as_of=date(2024, 3, 1), statement="cash_flow", freq="annual").value
    assert data["lines"]["capital_expenditure"]["2023-12-31"]["value"] == 14.0e6
    assert data["lines"]["investing_cash_flow"] == {}         # the filer did not tag it
    assert data["periods"] == ["2022-12-31", "2023-12-31"]
    annual = filings.fundamentals_as_filed(client, "NWND", as_of=date(2024, 3, 1), freq="annual").value
    assert annual["units"]["diluted_eps"] == "USD/shares" and annual["lines"]["diluted_eps"]["2023-12-31"]["value"] == 1.25


def test_fundamentals_as_filed_before_any_filing_is_no_data_TA_69():
    client = make_client()
    got = filings.fundamentals_as_filed(client, "NWND", as_of=date(2020, 1, 1))
    assert got.status == NO_DATA and got.value is None


def test_missing_identity_is_data_unavailable_with_reason_ED_008():
    client = make_client(identity=None)
    got = filings.current_report(client, "NWND", as_of=date(2024, 6, 1))
    assert got.status == DATA_UNAVAILABLE and got.reason == "sec_identity_missing" and got.value is None


def test_provider_down_is_data_unavailable_never_a_value_ED_009():
    client = make_client(DownTransport())
    for call in (lambda: filings.list_filings(client, "NWND", as_of=date(2024, 6, 1)),
                 lambda: filings.fundamentals_as_filed(client, "NWND", as_of=date(2024, 6, 1)),
                 lambda: filings.periodic_report(client, 1000001, as_of=date(2024, 6, 1))):
        got = call()
        assert got.status == DATA_UNAVAILABLE and got.value is None and got.reason.startswith("sec_network")


def test_unknown_ticker_is_no_data_ED_009():
    got = filings.list_filings(make_client(), "ZZZZ", as_of=date(2024, 6, 1))
    assert got.status == NO_DATA and got.reason == "ticker_not_in_sec_table"

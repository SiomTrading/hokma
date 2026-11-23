"""ED-018: ticker to CIK, form catalog and the identity of the User-Agent; plus the transport and index plumbing every row relies on."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from hokma.data import _lib
from hokma.data.sec import identity, reference
from hokma.data.sec.client import SecClient
from hokma.data.sec.index import FilingRef, is_visible, matches_form, parse_submissions
from hokma.data.sec.result import DATA_UNAVAILABLE, OK
from hokma.data.sec.transport import LiveTransport, TransportError, company_facts_url, submission_text_url, submissions_url
from tests.data.sec.support import FIXTURES, RecordedTransport, make_client


def test_ticker_to_cik_from_the_reference_table_ED_018():
    table = reference.TickerTable.from_payload(json.loads((FIXTURES / "company_tickers.json").read_text()))
    assert table.cik_for("NWND") == 1000001 and table.cik_for("nwnd") == 1000001
    assert table.cik_for("BRK.B") == 1000003 and table.cik_for("BRK-B") == 1000003          # one ticker, two spellings
    assert table.cik_for("ZZZZ") is None
    assert table.tickers_for(1000001) == ["NWND", "NWNDW"] and table.name_for(1000001) == "Northwind Instruments Inc."
    assert len(table) == 9


def test_columnar_table_layout_ED_018():
    payload = {"fields": ["cik", "name", "ticker", "exchange"], "data": [[1000001, "Northwind Instruments Inc.", "NWND", "Nasdaq"]]}
    assert reference.TickerTable.from_payload(payload).cik_for("NWND") == 1000001


def test_resolve_returns_cik_tickers_and_name_ED_018():
    got = reference.resolve(make_client(), "NWND", date(2024, 6, 1))
    assert got.status == OK and got.value == {"cik": 1000001, "tickers": ["NWND", "NWNDW"], "name": "Northwind Instruments Inc."}
    assert reference.resolve(make_client(), 1000001, date(2024, 6, 1)).value["cik"] == 1000001
    assert reference.resolve(make_client(), "ZZZZ", date(2024, 6, 1)).reason == "ticker_not_in_sec_table"


def test_no_identity_is_data_unavailable_with_the_reason_ED_018():
    got = reference.resolve(make_client(identity=None), "NWND", date(2024, 6, 1))
    assert got.status == DATA_UNAVAILABLE and got.reason == "sec_identity_missing" and got.value is None


def test_identity_comes_from_the_connections_store_ED_018(monkeypatch: pytest.MonkeyPatch):
    asked: list[str] = []

    class FakeService:
        def read_secret(self, provider_id: str) -> str | None:
            asked.append(provider_id)
            return "Ana Perez ana@example.invalid"

    import hokma.connections.runtime as runtime

    monkeypatch.setattr(runtime, "get_service", lambda: FakeService())
    assert identity.resolve_identity() == "Ana Perez ana@example.invalid" and asked == ["sec_edgar"]
    client = SecClient(RecordedTransport())
    assert client.identity() == "Ana Perez ana@example.invalid" and client.preflight(date(2024, 1, 1)) is None


def test_identity_store_failure_means_no_identity_ED_018(monkeypatch: pytest.MonkeyPatch):
    import hokma.connections.runtime as runtime

    def boom() -> None:
        raise RuntimeError("keyring locked")

    monkeypatch.setattr(runtime, "get_service", boom)
    assert identity.resolve_identity() is None
    assert SecClient(RecordedTransport()).preflight(date(2024, 1, 1)).reason == "sec_identity_missing"


@pytest.mark.parametrize("text,expected", [("Ana ana@example.invalid", "Ana ana@example.invalid"), ("  Ana   ana@example.invalid ", "Ana ana@example.invalid"),
                                           ("no email here", None), ("", None), (None, None), ("a@b", None)])
def test_identity_must_declare_a_contact_ED_018(text, expected):
    assert identity.valid_identity(text) == expected


def test_form_catalog_and_families_ED_018():
    info = reference.form_info("10-K")
    assert info.family == "periodic_report" and "annual report" in info.description.lower()
    assert reference.form_family("10-K/A") == "periodic_report" and reference.form_family("4") == "insider"
    assert reference.form_family("SC 13D/A") == "beneficial_ownership" and reference.form_family("13F-HR") == "institutional_holdings"
    assert reference.form_family("424B5") == "offering" and reference.form_family("NPORT-P") == "fund" and reference.form_family("ZZZ") == "other"
    assert reference.form_info("NOT-A-FORM").description == ""


def test_urls_ED_018():
    assert submissions_url(1000001) == "https://data.sec.gov/submissions/CIK0001000001.json"
    assert company_facts_url(320193) == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    assert submission_text_url(1000001, "0001000001-24-000021") == "https://www.sec.gov/Archives/edgar/data/1000001/000100000124000021/0001000001-24-000021.txt"


def test_live_transport_maps_every_failure_to_a_transport_error_ED_018(monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[str, str]] = []

    def fake(url: str, who: str) -> str:
        seen.append((url, who))
        if "missing" in url:
            raise _lib.SecFetchError("not_found", "404")
        if "limited" in url:
            raise _lib.SecFetchError("rate_limited", "429")
        return "not json" if "company_tickers" in url else json.dumps({"cik": 1})

    monkeypatch.setattr(_lib, "sec_download_text", fake)
    live = LiveTransport("Ana ana@example.invalid")
    assert live.submissions(1000001) == {"cik": 1} and seen[0][1] == "Ana ana@example.invalid"
    with pytest.raises(TransportError) as bad_json:
        live.company_tickers()
    assert bad_json.value.kind == "network"
    with pytest.raises(TransportError) as missing:
        live.submissions_file("missing.json")
    assert missing.value.kind == "not_found"
    with pytest.raises(TransportError) as limited:
        live.submissions_file("limited.json")
    assert limited.value.kind == "rate_limited"


def test_client_builds_the_live_transport_only_with_an_identity_ED_018():
    with pytest.raises(TransportError) as err:
        SecClient(identity_reader=lambda: None).transport
    assert err.value.kind == "identity"
    assert isinstance(SecClient(identity_reader=lambda: "Ana ana@example.invalid").transport, LiveTransport)


def test_sec_fetch_error_kinds_come_from_the_library_errors_ED_018():
    class TooManyRequestsError(Exception):
        pass

    class IdentityNotSetError(Exception):
        pass

    assert _lib._sec_kind(TooManyRequestsError()) == "rate_limited" and _lib._sec_kind(IdentityNotSetError()) == "identity"
    assert _lib._sec_kind(RuntimeError("HTTP 404 Not Found")) == "not_found" and _lib._sec_kind(RuntimeError("boom")) == "network"


def test_submissions_index_older_pages_and_dedup_ED_018():
    recent = json.loads((FIXTURES / "submissions_1000001.json").read_text())
    older = {"accessionNumber": ["0001000001-19-000001", recent["filings"]["recent"]["accessionNumber"][0]], "filingDate": ["2019-03-01", "2020-01-01"],
             "reportDate": ["2018-12-31", ""], "acceptanceDateTime": ["2019-03-01T10:00:00.000Z", ""], "form": ["10-K", "10-K"], "items": ["", ""],
             "primaryDocument": ["a.htm", "b.htm"], "primaryDocDescription": ["10-K", "10-K"]}
    refs = parse_submissions(recent, extra_pages=[older])
    accessions = [r.accession for r in refs]
    assert len(accessions) == len(set(accessions)) and "0001000001-19-000001" in accessions and refs[-1].accession == "0001000001-19-000001"
    assert refs[0].filing_date >= refs[1].filing_date


def test_visibility_rules_ED_018():
    ref = FilingRef(cik=1, accession="0000000001-24-000001", form="10-K/A", filing_date=date(2024, 5, 2), accepted=datetime(2024, 5, 2, 16, 0, tzinfo=timezone.utc))
    assert is_visible(ref, date(2024, 5, 2)) and not is_visible(ref, date(2024, 5, 1))
    assert not is_visible(ref, datetime(2024, 5, 2, 15, 59, tzinfo=timezone.utc)) and is_visible(ref, datetime(2024, 5, 2, 16, 0, tzinfo=timezone.utc))
    assert not is_visible(ref, datetime(2024, 5, 2, 12, 0))                # a naive time is read as UTC: 12:00 is before the 16:00 acceptance
    assert matches_form(ref, "10-K") and not matches_form(ref, "10-K", amendments=False) and matches_form(ref, ["10-Q", "10-K/A"])

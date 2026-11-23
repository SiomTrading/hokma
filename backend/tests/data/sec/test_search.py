"""ED-017: full text search and text search inside a filing."""
from __future__ import annotations

from datetime import date

import pytest

from hokma.data.sec import search
from hokma.data.sec.result import DATA_UNAVAILABLE, NO_DATA, OK
from tests.data.sec.support import DownTransport, RecordedTransport, make_client


def test_full_text_hits_newest_first_and_typed_ED_017():
    got = search.full_text(make_client(), "guidance", as_of=date(2024, 8, 1))
    assert got.status == OK
    assert [(h.accession, h.form, h.filed.isoformat()) for h in got.value] == [
        ("0001000001-24-000030", "8-K", "2024-07-15"), ("0001000001-24-000021", "8-K", "2024-05-02"), ("0001000001-24-000010", "10-K", "2024-02-20")]
    first = got.value[1]
    assert (first.company, first.cik, first.document, first.document_type, first.items) == ("Northwind Instruments Inc.", 1000001, "nwnd-ex991.htm", "EX-99.1", ("2.02", "9.01"))


def test_full_text_drops_hits_filed_after_as_of_and_caps_the_window_ED_017():
    transport = RecordedTransport()
    got = search.full_text(make_client(transport), "guidance", as_of=date(2024, 6, 1), symbol="NWND", forms=("8-K",), start=date(2024, 1, 1))
    assert [h.accession for h in got.value] == ["0001000001-24-000021", "0001000001-24-000010"]
    assert got.warnings == ("1 hit(s) filed after as_of were dropped",)
    call = [c for c in transport.calls if c[0] == "full_text_search"][0]
    assert call[2:] == (("8-K",), 1000001, "2024-01-01", "2024-06-01")          # the window ends at as_of


def test_full_text_no_visible_hit_ED_017():
    assert search.full_text(make_client(), "guidance", as_of=date(2024, 1, 1)).status == NO_DATA


def test_full_text_requires_a_query_ED_017():
    with pytest.raises(ValueError):
        search.full_text(make_client(), "  ", as_of=date(2024, 6, 1))


def test_full_text_url_is_built_with_the_window_ED_017():
    from hokma.data.sec.transport import efts_url

    url = efts_url("raises guidance", forms=("8-K", "10-K"), cik=1000001, start="2024-01-01", end="2024-06-01")
    assert url == ("https://efts.sec.gov/LATEST/search-index?q=raises+guidance&forms=8-K%2C10-K&ciks=0001000001&dateRange=custom&startdt=2024-01-01&enddt=2024-06-01")


def test_grep_filing_finds_text_in_each_document_ED_017():
    got = search.grep_filing(make_client(), "NWND", "guidance", as_of=date(2024, 6, 1), form="8-K")
    assert got.status == OK and got.value["ref"].accession == "0001000001-24-000021"
    match = got.value["matches"][0]
    assert match["location"] == "EX-99.1" and match["match"].lower() == "guidance" and "raises full year revenue guidance to $440 million" in match["context"]


def test_grep_filing_regex_and_case_ED_017():
    got = search.grep_filing(make_client(), "NWND", r"\$\d+\.\d million", as_of=date(2024, 6, 1), form="8-K", regex=True, context_chars=10)
    assert [m["match"] for m in got.value["matches"]] == ["$108.2 million", "$9.7 million"]
    assert search.grep_filing(make_client(), "NWND", "GUIDANCE", as_of=date(2024, 6, 1), form="8-K").status == OK


def test_grep_filing_respects_as_of_and_missing_pattern_ED_017():
    assert search.grep_filing(make_client(), "NWND", "retire", as_of=date(2024, 6, 1), form="8-K").reason == "pattern_not_found"
    later = search.grep_filing(make_client(), "NWND", "retire", as_of=date(2024, 7, 20), form="8-K")
    assert later.status == OK and later.value["ref"].accession == "0001000001-24-000030"
    assert search.grep_filing(make_client(), "NWND", "guidance", as_of=date(2024, 6, 1), form="8-K", accession="0001000001-24-000030").reason == "accession_not_visible_at_as_of"


def test_grep_filing_bad_patterns_ED_017():
    with pytest.raises(ValueError):
        search.grep_filing(make_client(), "NWND", "", as_of=date(2024, 6, 1))
    with pytest.raises(ValueError):
        search.grep_filing(make_client(), "NWND", "a" * 201, as_of=date(2024, 6, 1))
    assert search.grep_filing(make_client(), "NWND", "([unclosed", as_of=date(2024, 6, 1), form="8-K", regex=True).reason == "pattern_not_found"


def test_search_without_identity_or_provider_ED_017():
    assert search.full_text(make_client(identity=None), "x", as_of=date(2024, 6, 1)).reason == "sec_identity_missing"
    got = search.grep_filing(make_client(DownTransport()), "NWND", "x", as_of=date(2024, 6, 1))
    assert got.status == DATA_UNAVAILABLE and got.value is None

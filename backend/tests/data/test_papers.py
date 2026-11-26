"""Paper search (SPEC-020: SKL-011).

The answers in tests/data/fixtures/papers_* are written by hand from the documented shapes of the two public services: the Atom feed of the arXiv
query API (feed, entry, id, published, title, summary, author) and the JSON of the Semantic Scholar paper endpoint (paperId, citationCount). The
papers in them are made up for the tests and are labelled as fixtures. No test opens a connection: the transport is a recorded one.
"""
import re
from pathlib import Path

import pytest

from hokma.data import papers, sentinels
from hokma.data.papers import BadPaperRequest
from hokma.data.providers import RateLimitError, http_transport

from .support import RecordedTransport, fixture_bytes, when

NOW = when("2024-04-10T12:00:00")
clock = lambda: NOW  # noqa: E731
SCHOLAR = {"2101.00001": "papers_scholar_2101.00001.json", "2102.00002": "papers_scholar_2102.00002.json", "q-fin.TR/1907003": "papers_scholar_unknown.json"}


def answer(url, params, headers):
    if url == papers.ARXIV_URL:
        return 200, fixture_bytes("papers_arxiv_search.xml")
    ident = url.split("ARXIV:")[1]
    return 200, fixture_bytes(SCHOLAR[ident])


def test_a_search_returns_title_abstract_date_and_citations_SKL_011():
    transport = RecordedTransport(answer)
    result = papers.search_papers("momentum futures", transport=transport, clock=clock)
    assert result.ok and result.source == "arxiv" and result.received_at == NOW
    first, second, third = result.value
    assert first["arxiv_id"] == "2101.00001" and first["title"] == "Fixture Paper A: momentum in futures"
    assert first["abstract"] == "We study trailing returns of futures and find a persistent drift."
    assert first["published"] == "2021-01-05" and first["citations"] == 412 and first["authors"] == ["Author One", "Author Two"]
    assert first["url"] == "https://arxiv.org/abs/2101.00001"
    assert (second["published"], second["citations"]) == ("2021-02-10", 0)                    # zero citations is a count, not a gap
    assert third["arxiv_id"] == "q-fin.TR/1907003" and third["citations"] is None
    assert "citation_counts_are_live" in result.warnings and "citations_unknown:q-fin.TR/1907003" in result.warnings


def test_the_query_is_reduced_to_plain_words_and_goes_to_fixed_hosts_SKL_011():
    transport = RecordedTransport(answer)
    papers.search_papers('momentum" OR ti:futures; all:evil', limit=3, transport=transport, clock=clock)
    url, params, headers = transport.calls[0]
    assert url == "https://export.arxiv.org/api/query" and params["max_results"] == 3 and params["sortBy"] == "relevance"
    assert params["search_query"] == "all:momentum AND all:OR AND all:ti AND all:futures AND all:all AND all:evil"
    assert all(c[0].startswith("https://api.semanticscholar.org/graph/v1/paper/ARXIV:") for c in transport.calls[1:])
    assert transport.calls[1][1] == {"fields": "citationCount"} and headers["User-Agent"] == "hokma-data/1.0"


def test_without_network_the_answer_is_data_unavailable_not_no_papers_SKL_011():
    def offline(url, params, headers):
        raise RateLimitError("network failure: URLError", reason=sentinels.REASON_UNREACHABLE)

    result = papers.search_papers("momentum futures", transport=offline, clock=clock)
    assert (result.status, result.reason, result.value) == (sentinels.DATA_UNAVAILABLE, sentinels.REASON_UNREACHABLE, None)
    assert papers.read_paper("2101.00001", transport=offline, clock=clock).status == sentinels.DATA_UNAVAILABLE


def test_a_throttled_citation_service_keeps_the_papers_and_says_so_SKL_011():
    def throttled(url, params, headers):
        return (429, b"") if "semanticscholar" in url else (200, fixture_bytes("papers_arxiv_search.xml"))

    result = papers.search_papers("momentum futures", transport=throttled, clock=clock)
    assert result.ok and [p["citations"] for p in result.value] == [None, None, None]
    assert any(w.startswith("citations_unavailable:2101.00001:RateLimitError") for w in result.warnings)


def test_a_search_that_matches_nothing_is_no_data_and_a_bad_answer_is_unavailable():
    empty = papers.search_papers("zzzz zzzz", transport=RecordedTransport(lambda u, p, h: (200, fixture_bytes("papers_arxiv_empty.xml"))), clock=clock)
    assert (empty.status, empty.reason) == (sentinels.NO_DATA, sentinels.REASON_EMPTY)
    broken = papers.search_papers("momentum futures", transport=RecordedTransport(lambda u, p, h: (200, b"<feed")), clock=clock)
    assert (broken.status, broken.reason) == (sentinels.DATA_UNAVAILABLE, "bad_response")
    bomb = b'<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY a "x">]><feed xmlns="http://www.w3.org/2005/Atom"/>'
    assert papers.search_papers("momentum futures", transport=RecordedTransport(lambda u, p, h: (200, bomb)), clock=clock).reason == "bad_response"
    huge = b" " * (papers.MAX_BODY_BYTES + 1)
    assert papers.search_papers("momentum futures", transport=RecordedTransport(lambda u, p, h: (200, huge)), clock=clock).reason == "bad_response"
    assert papers.search_papers("momentum futures", transport=RecordedTransport(lambda u, p, h: (500, b"")), clock=clock).status == sentinels.DATA_UNAVAILABLE


def test_papers_published_after_as_of_are_left_out_SKL_011():
    result = papers.search_papers("momentum futures", as_of=when("2021-01-31"), transport=RecordedTransport(answer), clock=clock)
    assert [p["arxiv_id"] for p in result.value] == ["2101.00001", "q-fin.TR/1907003"] and "papers_after_as_of_removed:1" in result.warnings
    none_left = papers.search_papers("momentum futures", as_of=when("2018-01-01"), transport=RecordedTransport(answer), clock=clock)
    assert none_left.status == sentinels.NO_DATA


def test_one_paper_is_read_by_its_id():
    transport = RecordedTransport(answer)
    result = papers.read_paper("2101.00001v2", transport=transport, clock=clock)
    assert result.ok and [p["arxiv_id"] for p in result.value] == ["2101.00001"] and result.value[0]["citations"] == 412
    assert transport.calls[0][1] == {"id_list": "2101.00001", "max_results": 1}


@pytest.mark.parametrize("query", ["", "ab", "x" * 201, 5, None, "!!! ???", " ".join(["word"] * 13)])
def test_bad_queries_are_refused(query):
    with pytest.raises(BadPaperRequest) as caught:
        papers.search_papers(query, transport=RecordedTransport(answer), clock=clock)
    assert caught.value.code == "bad_input"


@pytest.mark.parametrize("limit", [0, 11, -1, 2.5, True, "3"])
def test_the_number_of_results_is_capped(limit):
    with pytest.raises(BadPaperRequest):
        papers.search_papers("momentum futures", limit=limit, transport=RecordedTransport(answer), clock=clock)


@pytest.mark.parametrize("ident", ["", "2101", "../etc/passwd", "2101.00001/../x", "http://arxiv.org/abs/2101.00001", 7])
def test_an_arxiv_id_has_to_look_like_one(ident):
    with pytest.raises(BadPaperRequest):
        papers.read_paper(ident, transport=RecordedTransport(answer), clock=clock)
    assert papers.arxiv_id("2101.00001v3") == "2101.00001" and papers.arxiv_id("q-fin.TR/0101001") == "q-fin.TR/0101001"


def test_the_module_uses_the_shared_http_helper_and_has_no_bibtex():
    assert papers.search_papers.__kwdefaults__["transport"] is http_transport and papers.read_paper.__kwdefaults__["transport"] is http_transport
    source = Path(papers.__file__).read_text(encoding="utf-8")
    assert not re.search(r"urlopen|import requests|httpx|bibtex|\.bib\b", source, re.I)
    result = papers.search_papers("momentum futures", transport=RecordedTransport(answer), clock=clock)
    assert not any("bib" in key for paper in result.value for key in paper)

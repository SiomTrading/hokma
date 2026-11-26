"""Research papers for a strategy hypothesis (SPEC-020: SKL-011): search by words, read one, count its citations.

Titles, abstracts and dates come from the public arXiv query API; the number of citations from the public Semantic Scholar graph API. Both are
reached through the transport of `hokma.data.providers` (the one HTTP helper of the data package) with fixed https hosts, a size cap and
validated inputs: a query is reduced to plain words, an arXiv id has to look like one, and the number of results is capped. A failed fetch is
`DATA_UNAVAILABLE`, never "no papers"; `NO_DATA` means the search ran and found nothing. A paper whose citations could not be read keeps its
other fields and says so in the warnings. There is no reference export here: an answer is data for a hypothesis, not a bibliography.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Callable, Mapping

from hokma.data import sentinels
from hokma.data.providers import NoDataError, RateLimitError, Transport, check_status, http_transport
from hokma.data.sentinels import DataResult, parse_time, utcnow

ARXIV_URL = "https://export.arxiv.org/api/query"
SCHOLAR_URL = "https://api.semanticscholar.org/graph/v1/paper/ARXIV:{arxiv_id}"
USER_AGENT = "hokma-data/1.0"
MAX_BODY_BYTES = 2_000_000
MAX_RESULTS = 10
DEFAULT_RESULTS = 5
MAX_QUERY_WORDS = 12
MAX_WORD = 40
MAX_TITLE = 300
MAX_ABSTRACT = 2_000
_ATOM = {"atom": "http://www.w3.org/2005/Atom"}
_WORD = re.compile(r"[A-Za-z0-9]+")
_NEW_ID = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$")
_OLD_ID = re.compile(r"^[a-z]+(?:-[a-z]+)?(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class BadPaperRequest(ValueError):
    """A query, an id or a size that is not accepted. `code` is stable: bad_input."""

    code = "bad_input"


def query_terms(query: Any) -> tuple[str, ...]:
    """The words of a query: letters and digits only, at most 12 words. Quotes, operators and field prefixes are not passed on."""
    if not isinstance(query, str) or not 3 <= len(query) <= 200:
        raise BadPaperRequest("the query is text of 3 to 200 characters")
    words = tuple(w[:MAX_WORD] for w in _WORD.findall(query))
    if not words or len(words) > MAX_QUERY_WORDS:
        raise BadPaperRequest(f"the query has 1 to {MAX_QUERY_WORDS} words")
    return words


def arxiv_id(value: Any) -> str:
    """The id of an arXiv paper without its version, or `BadPaperRequest`."""
    if not isinstance(value, str) or not (_NEW_ID.match(value) or _OLD_ID.match(value)):
        raise BadPaperRequest("an arXiv id looks like 2101.00001 or q-fin.TR/0101001")
    return re.sub(r"v\d+$", "", value)


def _limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_RESULTS:
        raise BadPaperRequest(f"limit is a whole number from 1 to {MAX_RESULTS}")
    return value


def _get(transport: Transport, source: str, url: str, params: Mapping[str, Any]) -> bytes:
    status, body = transport(url, params, {"User-Agent": USER_AGENT})
    check_status(source, status, body)
    if len(body) > MAX_BODY_BYTES:
        raise NoDataError(source, None, "answer larger than the allowed size")
    return body


def _plain(text: str | None, limit: int) -> str:
    return " ".join(_CONTROL.sub(" ", text or "").split())[:limit]


def parse_arxiv(body: bytes) -> list[dict[str, Any]]:
    """The entries of an arXiv Atom answer: id, title, abstract, published date and authors. A feed that declares a type or entities is refused."""
    if b"<!DOCTYPE" in body[:4096].upper() or b"<!ENTITY" in body.upper():
        raise ET.ParseError("feeds that declare a document type or entities are refused")
    root = ET.fromstring(body)
    rows = []
    for entry in root.findall("atom:entry", _ATOM):
        link = entry.findtext("atom:id", default="", namespaces=_ATOM) or ""
        found = re.search(r"/abs/(.+)$", link.strip())
        if not found:
            continue                                                  # an error entry of the API carries no paper
        try:
            ident = arxiv_id(found.group(1))
            published = parse_time(entry.findtext("atom:published", default="", namespaces=_ATOM))
        except (BadPaperRequest, ValueError):
            continue
        rows.append({"arxiv_id": ident, "title": _plain(entry.findtext("atom:title", default="", namespaces=_ATOM), MAX_TITLE),
                     "abstract": _plain(entry.findtext("atom:summary", default="", namespaces=_ATOM), MAX_ABSTRACT),
                     "published": published.strftime("%Y-%m-%d"), "published_at": published,
                     "authors": [_plain(a.findtext("atom:name", default="", namespaces=_ATOM), 80) for a in entry.findall("atom:author", _ATOM)][:20],
                     "url": f"https://arxiv.org/abs/{ident}"})
    return rows


def citation_count(ident: str, *, transport: Transport = http_transport) -> int | None:
    """How many papers cite this one, from Semantic Scholar. None when the service does not know the paper. A failure raises the errors of the providers."""
    body = _get(transport, "semanticscholar", SCHOLAR_URL.format(arxiv_id=arxiv_id(ident)), {"fields": "citationCount"})
    try:
        count = json.loads(body).get("citationCount")
    except (json.JSONDecodeError, AttributeError):
        raise NoDataError("semanticscholar", None, "the answer is not a paper") from None
    return count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None


def _with_citations(rows: list[dict[str, Any]], transport: Transport) -> list[str]:
    """Add `citations` to every row (None when it could not be read) and return the warnings that say why."""
    warnings: list[str] = []
    for row in rows:
        try:
            row["citations"] = citation_count(row["arxiv_id"], transport=transport)
        except (RateLimitError, NoDataError) as error:
            row["citations"] = None
            warnings.append(f"citations_unavailable:{row['arxiv_id']}:{type(error).__name__}")
        else:
            if row["citations"] is None:
                warnings.append(f"citations_unknown:{row['arxiv_id']}")
    return warnings


def _result(rows: list[dict[str, Any]], warnings: list[str], cut: datetime, now: datetime, source: str) -> DataResult:
    kept = [r for r in rows if r["published_at"] <= cut]
    if len(kept) < len(rows):
        warnings.append(f"papers_after_as_of_removed:{len(rows) - len(kept)}")
    if not kept:
        return sentinels.no_data(source, sentinels.REASON_EMPTY, as_of=cut, received_at=now, warnings=tuple(warnings))
    out = [{k: v for k, v in r.items() if k != "published_at"} for r in kept]
    return sentinels.ok(out, source=source, as_of=cut, received_at=now, warnings=("citation_counts_are_live", *warnings))


def search_papers(query: str, *, limit: int = DEFAULT_RESULTS, as_of: datetime | None = None, transport: Transport = http_transport,
                  clock: Callable[[], datetime] = utcnow) -> DataResult:
    """Papers that match the words of `query`, best match first: title, abstract, date, authors and number of citations.

    Papers published after `as_of` are left out. Citation counts are as of today, which the warning `citation_counts_are_live` says."""
    words, count = query_terms(query), _limit(limit)
    now = clock()
    cut = parse_time(as_of) if as_of is not None else now
    source = "arxiv"
    params = {"search_query": " AND ".join(f"all:{w}" for w in words), "start": 0, "max_results": count, "sortBy": "relevance"}
    try:
        rows = parse_arxiv(_get(transport, source, ARXIV_URL, params))
        warnings = _with_citations(rows, transport)
    except RateLimitError as error:
        return sentinels.unavailable(source, error.reason, as_of=cut, received_at=now, warnings=(f"arxiv unavailable: {error}",))
    except (NoDataError, ET.ParseError) as error:
        return sentinels.unavailable(source, "bad_response", as_of=cut, received_at=now, warnings=(str(error),))
    return _result(rows, warnings, cut, now, source)


def read_paper(ident: str, *, as_of: datetime | None = None, transport: Transport = http_transport,
               clock: Callable[[], datetime] = utcnow) -> DataResult:
    """One paper by arXiv id: the same fields as a search result (the abstract, not the full text)."""
    wanted = arxiv_id(ident)
    now = clock()
    cut = parse_time(as_of) if as_of is not None else now
    source = "arxiv"
    try:
        rows = [r for r in parse_arxiv(_get(transport, source, ARXIV_URL, {"id_list": wanted, "max_results": 1})) if r["arxiv_id"] == wanted]
        warnings = _with_citations(rows, transport)
    except RateLimitError as error:
        return sentinels.unavailable(source, error.reason, as_of=cut, received_at=now, warnings=(f"arxiv unavailable: {error}",))
    except (NoDataError, ET.ParseError) as error:
        return sentinels.unavailable(source, "bad_response", as_of=cut, received_at=now, warnings=(str(error),))
    return _result(rows, warnings, cut, now, source)

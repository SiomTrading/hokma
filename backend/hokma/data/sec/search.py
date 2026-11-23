"""Search inside filings (ED-017): full text search across filings and text search inside one filing.

Both are cut at `as_of`: a hit from a filing filed after `as_of` is dropped, and the search window is capped at `as_of`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from hokma.data import _lib
from hokma.data.sec import result, sgml
from hokma.data.sec.client import SecClient
from hokma.data.sec.filings import _cik, list_filings, parse_document
from hokma.data.sec.transport import TransportError

MAX_PATTERN = 200
MAX_TEXT = 5_000_000


@dataclass(frozen=True)
class SearchHit:
    accession: str
    form: str
    filed: date
    company: str | None
    cik: int | None
    period: str | None
    document: str | None
    document_type: str | None
    description: str | None
    items: tuple[str, ...]
    score: float


def full_text(client: SecClient, query: str, *, as_of: datetime | date, symbol: str | int | None = None, forms: tuple[str, ...] = (),
              start: date | None = None) -> result.SecResult:
    """Filings that mention `query`, newest first, filed no later than `as_of`."""
    if not query.strip():
        raise ValueError("query must not be empty")
    blocked = client.preflight(as_of)
    if blocked is not None:
        return blocked
    day = as_of.date() if isinstance(as_of, datetime) else as_of
    cik: int | None = None
    if symbol is not None:
        cik, bad = _cik(client, symbol, as_of)
        if bad is not None:
            return bad
    try:
        payload = client.transport.full_text_search(query, forms=forms, cik=cik, start=start.isoformat() if start else None, end=day.isoformat())
    except TransportError as exc:
        return client.fail(exc, as_of)
    raw = (payload.get("hits") or {}).get("hits") or []
    hits: list[SearchHit] = []
    hidden = 0
    for item in raw:
        found = _lib.sec_parse_efts_hit(item)
        try:
            filed = date.fromisoformat(str(found["filed"])[:10])
        except ValueError:
            continue
        if filed > day:
            hidden += 1
            continue
        hits.append(SearchHit(accession=found["accession"], form=found["form"], filed=filed, company=found["company"],
                              cik=int(found["cik"]) if found["cik"] else None, period=found["period"], document=found["document_id"],
                              document_type=found["file_type"], description=found["file_description"], items=tuple(found["items"]),
                              score=float(found["score"])))
    if not hits:
        return result.no_data("no_hit_visible_at_as_of", as_of, now=client.now)
    hits.sort(key=lambda h: (h.filed, h.accession), reverse=True)
    warnings = (f"{hidden} hit(s) filed after as_of were dropped",) if hidden else ()
    return result.ok(hits, as_of, now=client.now, warnings=warnings)


def grep_filing(client: SecClient, symbol: str | int, pattern: str, *, as_of: datetime | date, form: str | tuple[str, ...] | None = None,
                accession: str | None = None, regex: bool = False, context_chars: int = 100) -> result.SecResult:
    """Finds `pattern` inside the latest filing (or the one named) visible at `as_of`. The location of each match is the document type."""
    if not pattern:
        raise ValueError("pattern must not be empty")
    if len(pattern) > MAX_PATTERN:
        raise ValueError(f"pattern is longer than {MAX_PATTERN} characters")
    listed = list_filings(client, symbol, as_of=as_of, form=form)
    if not listed.ok:
        return listed
    refs = [r for r in listed.value if accession in (None, r.accession)]
    if not refs:
        return result.no_data("accession_not_visible_at_as_of", as_of, now=client.now)
    ref = refs[0]
    try:
        text = client.submission_text(ref.cik, ref.accession)
    except TransportError as exc:
        return client.fail(exc, as_of)
    matches: list[dict] = []
    for doc in sgml.parse_documents(text):
        body = parse_document(doc.text).text if doc.is_html else doc.text
        matches += _lib.sec_grep_text(body[:MAX_TEXT], pattern, doc.type, regex, context_chars)
    if not matches:
        return result.no_data("pattern_not_found", as_of, now=client.now)
    return result.ok({"ref": ref, "matches": matches}, as_of, now=client.now, warnings=listed.warnings)

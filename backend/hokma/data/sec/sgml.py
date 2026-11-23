"""Reads a full submission text file (the SGML envelope the SEC serves as `<accession>.txt`): header and documents."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

_DOCUMENT = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.S | re.I)
_TAG = re.compile(r"<(TYPE|SEQUENCE|FILENAME|DESCRIPTION)>([^\n]*)", re.I)
_TEXT = re.compile(r"<TEXT>(.*?)</TEXT>", re.S | re.I)
_XML = re.compile(r"<XML>(.*?)</XML>", re.S | re.I)
_HEADER = re.compile(r"<SEC-HEADER>(.*?)</SEC-HEADER>", re.S | re.I)
_ITEM_NUMBER = re.compile(r"\bitem\s+(\d{1,2}\.\d{2})", re.I)


@dataclass(frozen=True)
class SgmlDocument:
    type: str
    sequence: int
    filename: str
    description: str
    text: str

    @property
    def xml(self) -> str | None:
        """The XML payload of the document, or None when the document is not XML."""
        found = _XML.search(self.text)
        body = found.group(1) if found else self.text
        body = body.strip()
        if body.startswith(("<?xml", "<!--")) or re.match(r"<[A-Za-z][\w:.-]*[\s>]", body) and "<html" not in body[:400].lower():
            return body
        return None

    @property
    def is_html(self) -> bool:
        head = self.text[:2000].lower()
        return "<html" in head or "<body" in head or "<div" in head or "<p>" in head


@dataclass(frozen=True)
class SgmlHeader:
    accession: str
    form: str
    filed: date | None
    accepted: datetime | None
    period: date | None
    cik: int | None
    company: str
    items: tuple[str, ...]


def _header_value(header: str, key: str) -> str:
    found = re.search(rf"^\s*{re.escape(key)}:\s*(.*)$", header, re.M | re.I)
    return found.group(1).strip() if found else ""


def _ymd(text: str) -> date | None:
    text = text.strip()
    if len(text) >= 8 and text[:8].isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except ValueError:
            return None
    return None


def parse_header(text: str) -> SgmlHeader:
    block = _HEADER.search(text)
    header = block.group(1) if block else text[:6000]
    accepted = None
    stamp = re.search(r"<ACCEPTANCE-DATETIME>\s*(\d{14})", text)
    if stamp:
        raw = stamp.group(1)
        accepted = datetime(int(raw[:4]), int(raw[4:6]), int(raw[6:8]), int(raw[8:10]), int(raw[10:12]), int(raw[12:14]), tzinfo=timezone.utc)
    cik_text = _header_value(header, "CENTRAL INDEX KEY")
    items = tuple(dict.fromkeys(_ITEM_NUMBER.findall(" ".join(re.findall(r"ITEM INFORMATION:\s*(.*)", header)))))
    return SgmlHeader(accession=_header_value(header, "ACCESSION NUMBER"), form=_header_value(header, "CONFORMED SUBMISSION TYPE"),
                      filed=_ymd(_header_value(header, "FILED AS OF DATE")), accepted=accepted,
                      period=_ymd(_header_value(header, "CONFORMED PERIOD OF REPORT")),
                      cik=int(cik_text) if cik_text.isdigit() else None, company=_header_value(header, "COMPANY CONFORMED NAME"), items=items)


def parse_documents(text: str) -> list[SgmlDocument]:
    documents: list[SgmlDocument] = []
    for match in _DOCUMENT.finditer(text):
        body = match.group(1)
        tags = {k.upper(): v.strip() for k, v in _TAG.findall(body.split("<TEXT>", 1)[0])}
        content = _TEXT.search(body)
        seq = tags.get("SEQUENCE", "0")
        documents.append(SgmlDocument(type=tags.get("TYPE", ""), sequence=int(seq) if seq.isdigit() else 0, filename=tags.get("FILENAME", ""),
                                      description=tags.get("DESCRIPTION", ""), text=content.group(1) if content else ""))
    return documents


def find_document(documents: list[SgmlDocument], *types: str) -> SgmlDocument | None:
    """First document whose type is one of `types` (prefix match, so `EX-21` finds `EX-21.1`)."""
    wanted = tuple(t.upper() for t in types)
    for doc in documents:
        if doc.type.upper().startswith(wanted):
            return doc
    return None


def find_xml(documents: list[SgmlDocument], *, contains: str = "") -> str | None:
    """First XML payload of the submission (optionally one that contains a marker)."""
    for doc in documents:
        body = doc.xml
        if body and contains in body:
            return body
    return None

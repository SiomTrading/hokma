"""Filings of a company, point in time: the index, typed current reports, periodic reports, offerings, the auditor and the
subsidiaries (EX-21), the HTML document parser, and the fundamentals as they were presented to the SEC.

Rows: ED-008 (8-K), ED-009 (10-K, 10-Q, 20-F, 40-F, 6-K), ED-010 (auditor, EX-21), ED-011 (offerings), ED-030 (document
parser), TA-69 (fundamentals as filed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from hokma.data import _lib
from hokma.data.sec import result, sgml
from hokma.data.sec.client import SecClient
from hokma.data.sec.index import FilingRef, is_visible, matches_form, visible
from hokma.data.sec.plain import to_plain
from hokma.data.sec.transport import TransportError

# ---------------------------------------------------------------------------------------------------------------------
# Document parser (ED-030)
# ---------------------------------------------------------------------------------------------------------------------
_ITEM_LINE = re.compile(r"^\s*item\s+(\d{1,2}[A-C]?)\s*[\.:\-]?\s*(.*)$", re.I)
_WS = re.compile(r"[ \t\u00a0]+")


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    lines: tuple[str, ...]
    tables: tuple[tuple[tuple[str, ...], ...], ...]
    sections: dict[str, str]                      # "item_1a" -> text of Item 1A (the longest block when the item repeats)
    section_titles: dict[str, str]


def _clean(text: str) -> str:
    return _WS.sub(" ", text.replace("\r", "")).strip()


def parse_document(html: str) -> ParsedDocument:
    """Text, tables and the Item sections of a filing document written in HTML (or plain text)."""
    from lxml import html as lxml_html

    if "<" not in html[:2000]:
        lines = [_clean(line) for line in html.splitlines()]
        tables: list[tuple[tuple[str, ...], ...]] = []
    else:
        root = lxml_html.fromstring(html)
        for bad in root.xpath("//script|//style|//head"):
            bad.getparent().remove(bad)
        tables = []
        for table in root.xpath("//table"):
            rows = []
            for tr in table.xpath(".//tr"):
                cells = tuple(_clean(td.text_content()) for td in tr.xpath("./td|./th"))
                if any(cells):
                    rows.append(cells)
            if rows:
                tables.append(tuple(rows))
        for br in root.xpath("//br"):
            br.tail = "\n" + (br.tail or "")
        for el in root.xpath("//p|//div|//tr|//li|//h1|//h2|//h3|//h4|//h5|//h6|//table"):
            el.tail = "\n" + (el.tail or "")
        lines = [_clean(line) for line in root.text_content().splitlines()]
    lines = [line for line in lines if line]
    sections: dict[str, str] = {}
    titles: dict[str, str] = {}
    starts = [(i, m.group(1).upper(), m.group(2)) for i, line in enumerate(lines) if (m := _ITEM_LINE.match(line)) and len(line) < 220]
    for n, (index, key, title) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        body = "\n".join(lines[index + 1:end])
        name = f"item_{key.lower()}"
        if len(body) >= len(sections.get(name, "")):
            sections[name] = body
            titles[name] = title
    return ParsedDocument(text="\n".join(lines), lines=tuple(lines), tables=tuple(tables), sections=sections, section_titles=titles)


# ---------------------------------------------------------------------------------------------------------------------
# Auditor and subsidiaries (ED-010)
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AuditorInfo:
    name: str | None
    city: str | None = None
    auditor_since: int | None = None
    pcaob_id: str | None = None
    report_date: date | None = None


@dataclass(frozen=True)
class Subsidiary:
    name: str
    jurisdiction: str | None


_AUDITOR_SIGNED = re.compile(r"/s/\s*([A-Z][^\n/]{2,80}?(?:LLP|LLC|P\.C\.|Ltd\.?|Limited|S\.A\.|GmbH|AG|PLLC|CPAs?))", re.I)
_AUDITOR_SINCE = re.compile(r"auditor\s+since\s+(\d{4})", re.I)
_PCAOB = re.compile(r"PCAOB\s+(?:ID|Firm ID)[:\s#]*([0-9]{1,5})", re.I)
_LONG_DATE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})\b")
_MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                                       "November", "December"], start=1)}


def parse_auditor(text: str) -> AuditorInfo:
    """The auditor named in the report of the registered public accounting firm. Fields not present are None."""
    anchor = text.lower().find("report of independent registered public accounting firm")
    scope = text[anchor:] if anchor >= 0 else text
    signed = _AUDITOR_SIGNED.search(scope)
    name = signed.group(1).strip().replace("&amp;", "&") if signed else None
    since = _AUDITOR_SINCE.search(scope)
    pcaob = _PCAOB.search(scope)
    city = None
    report_date = None
    if signed:
        tail = scope[signed.end():signed.end() + 400]
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        if lines and not _LONG_DATE.search(lines[0]) and not lines[0].upper().startswith("PCAOB"):
            city = lines[0]
        found = _LONG_DATE.search(tail)
        if found:
            report_date = date(int(found.group(3)), _MONTHS[found.group(1)], int(found.group(2)))
    return AuditorInfo(name=name, city=city, auditor_since=int(since.group(1)) if since else None,
                       pcaob_id=pcaob.group(1) if pcaob else None, report_date=report_date)


def parse_subsidiaries(html: str) -> list[Subsidiary]:
    """The subsidiaries listed in an EX-21 exhibit: a table of name and jurisdiction, or lines like `Name (Jurisdiction)`."""
    doc = parse_document(html)
    found: list[Subsidiary] = []
    for table in doc.tables:
        for row in table:
            cells = [c for c in row if c]
            if not cells or cells[0].lower().startswith(("name", "subsidiar", "legal entity", "entity")):
                continue
            found.append(Subsidiary(cells[0], cells[1] if len(cells) > 1 else None))
    if found:
        return found
    for line in doc.lines:
        match = re.match(r"^(.{3,120}?)\s*[\(\-:,]\s*([A-Za-z .]{3,40})\)?$", line)
        if match and not line.lower().startswith(("exhibit", "subsidiaries")):
            found.append(Subsidiary(match.group(1).strip(), match.group(2).strip()))
    return found


# ---------------------------------------------------------------------------------------------------------------------
# Typed filings (ED-008, ED-009, ED-011)
# ---------------------------------------------------------------------------------------------------------------------
PERIODIC_FORMS = ("10-K", "10-Q", "20-F", "40-F", "6-K", "10-KT", "10-QT")
OFFERING_FORMS = ("D", "S-1", "S-3", "S-4", "424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8", "DRS", "C", "EFFECT")
SECTION_NAMES = {"item_1": "business", "item_1a": "risk_factors", "item_7": "mdna", "item_7a": "market_risk",
                 "item_8": "financial_statements", "item_9a": "controls"}
SECTION_NAMES_QUARTERLY = {"item_1": "financial_statements", "item_2": "mdna", "item_3": "market_risk", "item_4": "controls",
                           "item_1a": "risk_factors"}
OFFERING_KINDS = {"D": "private_placement", "C": "crowdfunding", "S-1": "ipo_registration", "DRS": "draft_registration",
                  "S-3": "shelf_registration", "S-4": "business_combination", "EFFECT": "effectiveness"}


@dataclass(frozen=True)
class FilingDocument:
    """Common head of every typed filing."""
    ref: FilingRef
    company: str
    form: str


@dataclass(frozen=True)
class CurrentReportItem:
    number: str
    title: str
    text: str


@dataclass(frozen=True)
class CurrentReport(FilingDocument):
    """8-K: the items it reports and the press release exhibits."""
    items: tuple[CurrentReportItem, ...] = ()
    press_releases: tuple[str, ...] = ()
    is_earnings: bool = False                    # Item 2.02, results of operations
    event_date: date | None = None


@dataclass(frozen=True)
class PeriodicReport(FilingDocument):
    """10-K, 10-Q, 20-F, 40-F, 6-K with its sections and exhibits."""
    period: date | None = None
    sections: dict[str, str] = field(default_factory=dict)
    exhibits: tuple[str, ...] = ()
    auditor: AuditorInfo | None = None
    subsidiaries: tuple[Subsidiary, ...] = ()
    text_length: int = 0


@dataclass(frozen=True)
class Offering(FilingDocument):
    """Offering related filings: Form D, S-1, S-3, S-4, 424B, DRS, Form C, EFFECT."""
    kind: str = "other"
    amounts: tuple[float, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)
    text_excerpt: str = ""


_ITEM_8K = re.compile(r"^\s*item\s+(\d\.\d{2})\s*[\.:\-]?\s*(.*)$", re.I)
_MONEY = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(million|billion|thousand)?", re.I)
_SCALE = {"million": 1e6, "billion": 1e9, "thousand": 1e3}


def _amounts(text: str, limit: int = 8) -> tuple[float, ...]:
    values: list[float] = []
    for m in _MONEY.finditer(text):
        value = float(m.group(1).replace(",", "")) * _SCALE.get((m.group(2) or "").lower(), 1.0)
        values.append(value)
        if len(values) >= limit:
            break
    return tuple(values)


def _submission(text: str) -> tuple[sgml.SgmlHeader, list[sgml.SgmlDocument]]:
    return sgml.parse_header(text), sgml.parse_documents(text)


def build_current_report(ref: FilingRef, text: str) -> CurrentReport:
    header, docs = _submission(text)
    main = next((d for d in docs if d.type.upper().startswith(("8-K", "6-K")) and d.is_html), docs[0] if docs else None)
    parsed = parse_document(main.text) if main else parse_document("")
    items: list[CurrentReportItem] = []
    starts = [(i, m.group(1), m.group(2)) for i, line in enumerate(parsed.lines) if (m := _ITEM_8K.match(line))]
    for n, (index, number, title) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(parsed.lines)
        items.append(CurrentReportItem(number, title, "\n".join(parsed.lines[index + 1:end])))
    numbers = tuple(dict.fromkeys([*ref.items, *(i.number for i in items)]))
    for number in numbers:
        if number not in {i.number for i in items}:
            items.append(CurrentReportItem(number, "", ""))
    releases = tuple(parse_document(d.text).text for d in docs if d.type.upper().startswith("EX-99") and d.is_html)
    event = header.period or ref.report_date
    return CurrentReport(ref=ref, company=header.company, form=ref.form, items=tuple(items), press_releases=releases,
                         is_earnings="2.02" in numbers, event_date=event)


def build_periodic_report(ref: FilingRef, text: str) -> PeriodicReport:
    header, docs = _submission(text)
    main = next((d for d in docs if d.type.upper().startswith(PERIODIC_FORMS) and d.is_html), docs[0] if docs else None)
    parsed = parse_document(main.text) if main else parse_document("")
    names = SECTION_NAMES_QUARTERLY if ref.form.upper().startswith(("10-Q", "10-QT")) else SECTION_NAMES
    sections = {names[k]: v for k, v in parsed.sections.items() if k in names}
    ex21 = sgml.find_document(docs, "EX-21")
    subs = tuple(parse_subsidiaries(ex21.text)) if ex21 and ex21.is_html else ()
    auditor = parse_auditor(parsed.text) if ref.form.upper().startswith(("10-K", "20-F", "40-F")) else None
    if auditor is not None and auditor.name is None:
        auditor = None
    return PeriodicReport(ref=ref, company=header.company, form=ref.form, period=header.period or ref.report_date, sections=sections,
                          exhibits=tuple(f"{d.type}:{d.filename}" for d in docs[1:]), auditor=auditor, subsidiaries=subs,
                          text_length=len(parsed.text))


def build_offering(ref: FilingRef, text: str) -> Offering:
    header, docs = _submission(text)
    base = ref.form.upper().split("/")[0]
    kind = OFFERING_KINDS.get(base, "debt_or_equity_prospectus" if base.startswith("424B") else "other")
    details: dict[str, Any] = {}
    excerpt = ""
    amounts: tuple[float, ...] = ()
    xml = sgml.find_xml(docs)
    if base == "D" and xml:
        form = _lib.sec_form_d(xml)
        details = {"issuer": to_plain(getattr(form, "primary_issuer", None)), "offering": to_plain(getattr(form, "offering_data", None)),
                   "related_persons": to_plain(getattr(form, "related_persons", None))}
    elif base == "C" and xml:
        details = {"form_c": to_plain(_lib.sec_form_c(xml))}
    else:
        main = next((d for d in docs if d.is_html), None)
        if main:
            parsed = parse_document(main.text)
            excerpt = parsed.text[:600]
            amounts = _amounts(parsed.text)
    return Offering(ref=ref, company=header.company, form=ref.form, kind=kind, amounts=amounts, details=details, text_excerpt=excerpt)


# ---------------------------------------------------------------------------------------------------------------------
# Services: everything takes a SecClient and an as_of cut
# ---------------------------------------------------------------------------------------------------------------------
def _cik(client: SecClient, symbol: str | int, as_of: Any) -> tuple[int | None, result.SecResult | None]:
    blocked = client.preflight(as_of)
    if blocked is not None:
        return None, blocked
    try:
        cik = client.cik_of(symbol)
    except TransportError as exc:
        return None, client.fail(exc, as_of)
    if cik is None:
        return None, result.no_data("ticker_not_in_sec_table", as_of, now=client.now)
    return cik, None


def list_filings(client: SecClient, symbol: str | int, *, as_of: datetime | date, form: str | Iterable[str] | None = None,
                 limit: int | None = None, amendments: bool = True) -> result.SecResult:
    """Filings of the company visible at `as_of`, newest first. A filing filed after `as_of` never appears."""
    cik, bad = _cik(client, symbol, as_of)
    if bad is not None:
        return bad
    try:
        refs = client.refs(cik)
    except TransportError as exc:
        if exc.kind == "not_found":
            return result.no_data("company_not_found", as_of, now=client.now)
        return client.fail(exc, as_of)
    chosen = [r for r in visible(refs, as_of) if matches_form(r, form, amendments=amendments)]
    if limit is not None:
        chosen = chosen[:limit]
    if not chosen:
        return result.no_data("no_filing_visible_at_as_of", as_of, now=client.now)
    hidden = sum(1 for r in refs if matches_form(r, form, amendments=amendments) and not is_visible(r, as_of))
    warnings = (f"{hidden} later filing(s) hidden by as_of",) if hidden else ()
    return result.ok(chosen, as_of, now=client.now, warnings=warnings)


def _load(client: SecClient, ref: FilingRef) -> str:
    return client.submission_text(ref.cik, ref.accession)


def _typed(client: SecClient, symbol: str | int, as_of: datetime | date, form: str | Iterable[str], builder: Any,
           accession: str | None) -> result.SecResult:
    listed = list_filings(client, symbol, as_of=as_of, form=form)
    if not listed.ok:
        return listed
    refs: list[FilingRef] = listed.value
    if accession is not None:
        refs = [r for r in refs if r.accession == accession]
        if not refs:
            return result.no_data("accession_not_visible_at_as_of", as_of, now=client.now)
    ref = refs[0]
    try:
        text = _load(client, ref)
    except TransportError as exc:
        return client.fail(exc, as_of)
    return result.ok(builder(ref, text), as_of, now=client.now, warnings=listed.warnings)


def current_report(client: SecClient, symbol: str | int, *, as_of: datetime | date, accession: str | None = None) -> result.SecResult:
    """The latest 8-K (or the one named) visible at `as_of`, typed (ED-008)."""
    return _typed(client, symbol, as_of, ("8-K", "6-K"), build_current_report, accession)


def periodic_report(client: SecClient, symbol: str | int, *, as_of: datetime | date, form: str | Iterable[str] = PERIODIC_FORMS,
                    accession: str | None = None) -> result.SecResult:
    """The latest periodic report of the form given, visible at `as_of`, typed (ED-009, ED-010)."""
    return _typed(client, symbol, as_of, form, build_periodic_report, accession)


def offerings(client: SecClient, symbol: str | int, *, as_of: datetime | date, form: str | Iterable[str] = OFFERING_FORMS,
              accession: str | None = None) -> result.SecResult:
    """The latest offering filing of the form given, visible at `as_of`, typed (ED-011)."""
    return _typed(client, symbol, as_of, form, build_offering, accession)


def auditor_and_subsidiaries(client: SecClient, symbol: str | int, *, as_of: datetime | date) -> result.SecResult:
    """Auditor and subsidiaries from the latest annual report visible at `as_of` (ED-010)."""
    report = periodic_report(client, symbol, as_of=as_of, form=("10-K", "20-F", "40-F"))
    if not report.ok:
        return report
    r: PeriodicReport = report.value
    return result.ok({"auditor": r.auditor, "subsidiaries": list(r.subsidiaries), "filing": r.ref}, as_of, now=client.now,
                     warnings=report.warnings)


# ---------------------------------------------------------------------------------------------------------------------
# Fundamentals as they were presented to the SEC (TA-69)
# ---------------------------------------------------------------------------------------------------------------------
# Each line lists the tags filers use for it, best first. A period keeps the first tag that reports it, values are never
# added across tags.
STATEMENT_LINES: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "balance_sheet": [
        ("total_assets", ("Assets",)),
        ("current_assets", ("AssetsCurrent",)),
        ("cash_and_equivalents", ("CashAndCashEquivalentsAtCarryingValue",)),
        ("total_liabilities", ("Liabilities",)),
        ("current_liabilities", ("LiabilitiesCurrent",)),
        ("stockholders_equity", ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")),
    ],
    "income_statement": [
        ("revenue", ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet")),
        ("cost_of_revenue", ("CostOfRevenue", "CostOfGoodsAndServicesSold")),
        ("gross_profit", ("GrossProfit",)),
        ("operating_income", ("OperatingIncomeLoss",)),
        ("net_income", ("NetIncomeLoss",)),
        ("diluted_eps", ("EarningsPerShareDiluted",)),
    ],
    "cash_flow": [
        ("operating_cash_flow", ("NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")),
        ("investing_cash_flow", ("NetCashProvidedByUsedInInvestingActivities",)),
        ("financing_cash_flow", ("NetCashProvidedByUsedInFinancingActivities",)),
        ("capital_expenditure", ("PaymentsToAcquirePropertyPlantAndEquipment",)),
    ],
}
# A statement figure covers a span: a quarter is about 90 days, a year about 365. A filing reports both the quarter and the
# year to date under the same end date, so matching the end date alone could read half a year as a quarter.
SPANS = {"quarterly": (60, 115), "annual": (300, 400)}


def as_filed_statement(facts: dict[str, Any], statement: str, freq: str, as_of: datetime | date) -> dict[str, Any]:
    """Statement lines as filed on or before `as_of`: for each period the latest filing visible then, with its filing date.

    An amendment counts from the day it was filed, never before. Lines the filer did not tag are returned empty.
    """
    if statement not in STATEMENT_LINES:
        raise ValueError(f"unknown statement {statement!r}; expected one of {sorted(STATEMENT_LINES)}")
    if freq not in SPANS:
        raise ValueError(f"unknown frequency {freq!r}; expected one of {sorted(SPANS)}")
    day = as_of.date() if isinstance(as_of, datetime) else as_of
    low, high = SPANS[freq]
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    lines: dict[str, dict[str, dict[str, Any]]] = {}
    units: dict[str, str] = {}
    for label, tags in STATEMENT_LINES[statement]:
        values: dict[str, dict[str, Any]] = {}
        for tag in tags:
            for unit, entries in ((gaap.get(tag) or {}).get("units") or {}).items():
                latest: dict[str, dict[str, Any]] = {}
                for fact in entries:
                    filed = date.fromisoformat(fact["filed"])
                    if filed > day or fact["end"] in values:
                        continue
                    if "start" in fact:
                        days = (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days
                        if not low <= days <= high:
                            continue
                    seen = latest.get(fact["end"])
                    if seen is None or fact["filed"] >= seen["filed"]:
                        latest[fact["end"]] = fact
                for end, fact in latest.items():
                    values[end] = {"value": fact["val"], "filed": fact["filed"], "form": fact.get("form"), "accession": fact.get("accn"),
                                   "unit": unit, "tag": tag}
                    units[label] = unit
        lines[label] = dict(sorted(values.items()))
    periods = sorted({end for values in lines.values() for end in values})
    return {"statement": statement, "freq": freq, "periods": periods, "lines": lines, "units": units, "as_of": day.isoformat()}


def fundamentals_as_filed(client: SecClient, symbol: str | int, *, as_of: datetime | date, statement: str = "income_statement",
                          freq: str = "quarterly") -> result.SecResult:
    """Fundamentals with the filing date of every figure, as known at `as_of` (TA-69)."""
    cik, bad = _cik(client, symbol, as_of)
    if bad is not None:
        return bad
    try:
        facts = client.facts(cik)
    except TransportError as exc:
        if exc.kind == "not_found":
            return result.no_data("no_company_facts", as_of, now=client.now)
        return client.fail(exc, as_of)
    if not (facts.get("facts") or {}).get("us-gaap"):
        return result.no_data("no_us_gaap_facts", as_of, now=client.now)
    data = as_filed_statement(facts, statement, freq, as_of)
    if not data["periods"]:
        return result.no_data(f"no_{freq}_{statement}_filed_by_as_of", as_of, now=client.now)
    return result.ok(data, as_of, now=client.now)

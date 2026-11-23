"""XBRL instance documents: parsing and queries, our own readers of the instance and the linkbases, standardization with a
registry of what was not mapped, multi period stitching and validation with deduplication.

Rows: ED-021 (parse and query), ED-022 (parsers), ED-023 (standardization), ED-024 (stitching), ED-027 (validation).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from lxml import etree

from hokma.data import _lib
from hokma.data.sec import result, sgml
from hokma.data.sec.client import SecClient
from hokma.data.sec.filings import list_filings
from hokma.data.sec.index import FilingRef
from hokma.data.sec.plain import to_plain
from hokma.data.sec.transport import TransportError

XBRLI = "http://www.xbrl.org/2003/instance"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
XBRLDI = "http://xbrl.org/2006/xbrldi"


# ---------------------------------------------------------------------------------------------------------------------
# Parsers (ED-022): instance and linkbases
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Context:
    id: str
    entity: str
    start: date | None
    end: date | None
    instant: date | None
    dimensions: tuple[tuple[str, str], ...] = ()

    @property
    def period_key(self) -> str:
        return f"instant_{self.instant}" if self.instant else f"duration_{self.start}_{self.end}"


@dataclass(frozen=True)
class XFact:
    concept: str
    value: str
    numeric: float | None
    context: str
    unit: str | None
    decimals: str | None
    is_nil: bool = False


@dataclass(frozen=True)
class Instance:
    contexts: dict[str, Context]
    units: dict[str, str]
    facts: tuple[XFact, ...]

    def context_of(self, fact: XFact) -> Context | None:
        return self.contexts.get(fact.context)


@dataclass(frozen=True)
class Arc:
    role: str
    parent: str
    child: str
    order: float
    weight: float | None = None        # calculation arcs only


def _parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=False)


def _qname(el: etree._Element) -> str:
    prefix = el.prefix
    local = etree.QName(el).localname
    return f"{prefix}:{local}" if prefix else local


def _date(text: str | None) -> date | None:
    try:
        return date.fromisoformat((text or "").strip()[:10])
    except ValueError:
        return None


def _number(text: str) -> float | None:
    try:
        return float(Decimal(text.strip().replace(",", "")))
    except (InvalidOperation, ValueError):
        return None


def parse_instance(xml: str | bytes) -> Instance:
    """Contexts, units and facts of an XBRL instance. Raises ValueError when the text is not an XBRL instance."""
    data = xml.encode("utf-8") if isinstance(xml, str) else xml
    try:
        root = etree.fromstring(data, _parser())
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"not well formed XML: {exc}") from exc
    if etree.QName(root).localname != "xbrl":
        raise ValueError("the document root is not xbrl")
    contexts: dict[str, Context] = {}
    units: dict[str, str] = {}
    facts: list[XFact] = []
    for el in root:
        if not isinstance(el.tag, str):
            continue
        tag = etree.QName(el)
        if tag.namespace == XBRLI and tag.localname == "context":
            ident = el.find(f".//{{{XBRLI}}}identifier")
            period = el.find(f"{{{XBRLI}}}period")
            dims = [(m.get("dimension", ""), (m.text or "").strip()) for m in el.iter(f"{{{XBRLDI}}}explicitMember")]
            dims += [(m.get("dimension", ""), "".join(m.itertext()).strip()) for m in el.iter(f"{{{XBRLDI}}}typedMember")]
            def child(name: str) -> str | None:
                found = period.find(f"{{{XBRLI}}}{name}") if period is not None else None
                return found.text if found is not None else None
            contexts[el.get("id", "")] = Context(id=el.get("id", ""), entity=(ident.text or "").strip() if ident is not None else "",
                                                 start=_date(child("startDate")), end=_date(child("endDate")), instant=_date(child("instant")),
                                                 dimensions=tuple(sorted(dims)))
        elif tag.namespace == XBRLI and tag.localname == "unit":
            measures = [(m.text or "").split(":")[-1] for m in el.iter(f"{{{XBRLI}}}measure")]
            divide = el.find(f"{{{XBRLI}}}divide")
            if divide is not None and len(measures) >= 2:
                units[el.get("id", "")] = f"{measures[0]}/{measures[1]}"
            else:
                units[el.get("id", "")] = "*".join(measures)
        elif el.get("contextRef") is not None:
            text = "".join(el.itertext()).strip()
            nil = el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") in ("true", "1")
            facts.append(XFact(concept=_qname(el), value=text, numeric=None if nil else _number(text) if el.get("unitRef") else None,
                               context=el.get("contextRef", ""), unit=el.get("unitRef"), decimals=el.get("decimals"), is_nil=nil))
    return Instance(contexts=contexts, units=units, facts=tuple(facts))


def parse_linkbase(xml: str | bytes, kind: str) -> list[Arc]:
    """Arcs of a presentation or calculation linkbase: role, parent concept, child concept, order and (calculation) weight."""
    if kind not in ("presentation", "calculation"):
        raise ValueError("kind must be 'presentation' or 'calculation'")
    data = xml.encode("utf-8") if isinstance(xml, str) else xml
    root = etree.fromstring(data, _parser())
    arc_tag = "presentationArc" if kind == "presentation" else "calculationArc"
    link_tag = "presentationLink" if kind == "presentation" else "calculationLink"
    arcs: list[Arc] = []
    for link in root.iter(f"{{{LINK}}}{link_tag}"):
        role = link.get(f"{{{XLINK}}}role", "")
        locs = {}
        for loc in link.iter(f"{{{LINK}}}loc"):
            href = loc.get(f"{{{XLINK}}}href", "")
            label = loc.get(f"{{{XLINK}}}label", "")
            fragment = href.split("#")[-1]
            locs[label] = fragment.replace("_", ":", 1) if "_" in fragment else fragment
        for arc in link.iter(f"{{{LINK}}}{arc_tag}"):
            parent, child = locs.get(arc.get(f"{{{XLINK}}}from", "")), locs.get(arc.get(f"{{{XLINK}}}to", ""))
            if parent is None or child is None:
                continue
            weight = arc.get("weight")
            arcs.append(Arc(role=role, parent=parent, child=child, order=float(arc.get("order", "0")),
                            weight=float(weight) if weight is not None else None))
    arcs.sort(key=lambda a: (a.role, a.parent, a.order))
    return arcs


# ---------------------------------------------------------------------------------------------------------------------
# Parse and query with the library (ED-021)
# ---------------------------------------------------------------------------------------------------------------------
class XbrlDocument:
    """An instance read by the filing library: standardized facts you can query by concept, dimension and period."""

    def __init__(self, typed: Any) -> None:
        self._xbrl = typed

    @classmethod
    def from_instance(cls, xml: str) -> "XbrlDocument":
        return cls(_lib.sec_xbrl_instance(xml))

    @property
    def entity_name(self) -> str | None:
        return self._xbrl.entity_name

    @property
    def document_type(self) -> str | None:
        return self._xbrl.document_type

    def facts(self, concept: str | None = None, *, dimensioned: bool | None = None, fiscal_year: int | None = None) -> list[dict[str, Any]]:
        """Facts as plain rows. `Revenues` and `us-gaap:Revenues` are the same concept; `dimensioned` filters by segment."""
        query = self._xbrl.facts.query()
        if concept:
            query = query.by_concept(concept)
        if fiscal_year is not None:
            query = query.by_fiscal_year(fiscal_year)
        rows = to_plain(query.to_dataframe())
        if dimensioned is not None:
            rows = [r for r in rows if bool(r.get("is_dimensioned")) == dimensioned]
        keep = ("concept", "value", "numeric_value", "period_start", "period_end", "period_instant", "is_dimensioned", "decimals", "context_ref",
                "unit_ref", "currency", "period_type", "fiscal_period", "fiscal_year")
        return [{k: r.get(k) for k in keep} for r in rows]

    def concepts(self) -> list[str]:
        return sorted({r["concept"] for r in self.facts()})


# ---------------------------------------------------------------------------------------------------------------------
# Standardization with a registry of what is not mapped (ED-023)
# ---------------------------------------------------------------------------------------------------------------------
DEFAULT_MAPPING: dict[str, tuple[str, ...]] = {
    "revenue": ("us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap:SalesRevenueNet"),
    "net_income": ("us-gaap:NetIncomeLoss", "us-gaap:ProfitLoss"),
    "operating_income": ("us-gaap:OperatingIncomeLoss",),
    "total_assets": ("us-gaap:Assets",),
    "total_liabilities": ("us-gaap:Liabilities",),
    "shareholders_equity": ("us-gaap:StockholdersEquity", "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    "cash": ("us-gaap:CashAndCashEquivalentsAtCarryingValue",),
    "diluted_eps": ("us-gaap:EarningsPerShareDiluted",),
}


@dataclass(frozen=True)
class StandardizedFacts:
    values: dict[str, dict[str, float]]          # standard name -> period key -> value
    unmapped: dict[str, int]                     # concept -> how many facts of this call were left out


class Standardizer:
    """Maps company concepts to standard names. What it cannot map is counted in `unmapped`, never dropped silently or guessed."""

    def __init__(self, mapping: dict[str, tuple[str, ...]] | None = None) -> None:
        self.mapping = dict(mapping or DEFAULT_MAPPING)
        self._reverse = {concept: (name, rank) for name, concepts in self.mapping.items() for rank, concept in enumerate(concepts)}
        self.unmapped: Counter[str] = Counter()          # accumulates across calls: the registry

    def standardize(self, instance: Instance) -> StandardizedFacts:
        values: dict[str, dict[str, tuple[int, float]]] = {}
        left: Counter[str] = Counter()
        for fact in instance.facts:
            ctx = instance.context_of(fact)
            if fact.numeric is None or fact.is_nil or ctx is None or ctx.dimensions:
                continue
            mapped = self._reverse.get(fact.concept)
            if mapped is None:
                left[fact.concept] += 1
                continue
            name, rank = mapped
            slot = values.setdefault(name, {})
            if ctx.period_key not in slot or rank < slot[ctx.period_key][0]:
                slot[ctx.period_key] = (rank, fact.numeric)          # the preferred concept of the name wins; values are never added
        self.unmapped.update(left)
        return StandardizedFacts(values={n: {k: v[1] for k, v in s.items()} for n, s in values.items()}, unmapped=dict(left))

    def registry(self) -> list[tuple[str, int, bool]]:
        """(concept, count, is_custom_extension) of everything that was not mapped, most frequent first."""
        base = {"us-gaap", "ifrs-full", "dei", "srt"}
        return sorted(((c, n, c.split(":")[0] not in base) for c, n in self.unmapped.items()), key=lambda r: (-r[1], r[0]))


# ---------------------------------------------------------------------------------------------------------------------
# Multi period stitching (ED-024)
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class FilingFacts:
    accession: str
    filed: date
    instance: Instance


@dataclass(frozen=True)
class StitchedPoint:
    period_start: date | None
    period_end: date
    value: float
    source_accession: str
    filed: date
    restated_from: float | None = None      # the value first reported, when a later filing changed it


SPAN_DAYS = {"annual": (300, 400), "quarterly": (60, 115)}


def stitch(filings: Iterable[FilingFacts], concept: str, *, period: str = "annual") -> list[StitchedPoint]:
    """One series of a concept across several filings. Each period takes the value of the latest filing that reports it.

    `period` is annual, quarterly or instant. A period whose value changed between filings carries the first reported value
    in `restated_from`. Dimensioned facts are not part of the series.
    """
    if period not in (*SPAN_DAYS, "instant"):
        raise ValueError("period must be annual, quarterly or instant")
    seen: dict[tuple[date | None, date], list[tuple[date, str, float]]] = {}
    for filing in filings:
        for fact in filing.instance.facts:
            ctx = filing.instance.context_of(fact)
            if fact.concept != concept or fact.numeric is None or fact.is_nil or ctx is None or ctx.dimensions:
                continue
            if period == "instant":
                if ctx.instant is None:
                    continue
                key: tuple[date | None, date] = (None, ctx.instant)
            else:
                if ctx.start is None or ctx.end is None:
                    continue
                low, high = SPAN_DAYS[period]
                if not low <= (ctx.end - ctx.start).days <= high:
                    continue
                key = (ctx.start, ctx.end)
            seen.setdefault(key, []).append((filing.filed, filing.accession, fact.numeric))
    points: list[StitchedPoint] = []
    for (start, end), entries in seen.items():
        entries.sort(key=lambda e: (e[0], e[1]))
        filed, accession, value = entries[-1]
        first = entries[0][2]
        points.append(StitchedPoint(start, end, value, accession, filed, restated_from=first if first != value else None))
    points.sort(key=lambda p: p.period_end)
    return points


# ---------------------------------------------------------------------------------------------------------------------
# Validation and deduplication (ED-027)
# ---------------------------------------------------------------------------------------------------------------------
NON_NEGATIVE = frozenset({"us-gaap:Assets", "us-gaap:AssetsCurrent", "us-gaap:Liabilities", "us-gaap:LiabilitiesCurrent",
                          "us-gaap:CashAndCashEquivalentsAtCarryingValue"})


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str            # error or warning
    concept: str
    context: str
    detail: str


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[Issue, ...]
    facts: tuple[XFact, ...]            # facts that passed: no conflicts, identical duplicates collapsed
    duplicates_removed: int

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def codes(self) -> set[str]:
        return {i.code for i in self.issues}


def _tolerance(decimals: str | None, value: float) -> float:
    if decimals in (None, "INF"):
        return 0.0
    try:
        return 0.5 * 10 ** (-int(decimals))
    except ValueError:
        return 0.0


def validate_instance(instance: Instance, *, calculations: Iterable[Arc] = ()) -> ValidationReport:
    """Checks an instance and returns the clean facts: identical duplicates are collapsed, conflicting ones are removed and reported."""
    issues: list[Issue] = []
    groups: dict[tuple[str, str, str | None], list[XFact]] = {}
    for fact in instance.facts:
        ctx = instance.contexts.get(fact.context)
        if ctx is None:
            issues.append(Issue("undefined_context", "error", fact.concept, fact.context, "the context is not declared"))
            continue
        if ctx.start and ctx.end and ctx.end < ctx.start:
            issues.append(Issue("period_order", "error", fact.concept, fact.context, "the period ends before it starts"))
        if fact.numeric is not None and (fact.unit is None or fact.unit not in instance.units):
            issues.append(Issue("undefined_unit", "error", fact.concept, fact.context, f"unit {fact.unit!r} is not declared"))
            continue
        if fact.numeric is not None and fact.numeric < 0 and fact.concept in NON_NEGATIVE:
            issues.append(Issue("negative_value", "warning", fact.concept, fact.context, f"{fact.numeric} is negative for a non negative concept"))
        groups.setdefault((fact.concept, fact.context, fact.unit), []).append(fact)
    clean: list[XFact] = []
    removed = 0
    for (concept, context, _unit), group in groups.items():
        distinct = {(f.value if f.numeric is None else f.numeric) for f in group}
        if len(distinct) == 1:
            clean.append(group[0])
            removed += len(group) - 1
            continue
        numeric = [f.numeric for f in group if f.numeric is not None]
        if numeric and max(numeric) - min(numeric) <= max(_tolerance(f.decimals, f.numeric or 0.0) for f in group):
            clean.append(group[0])                         # the values agree within the rounding they declare
            removed += len(group) - 1
            issues.append(Issue("duplicate_within_rounding", "warning", concept, context, f"values {sorted(distinct)} agree within rounding"))
            continue
        issues.append(Issue("duplicate_conflict", "error", concept, context, f"values {sorted(map(str, distinct))} for the same fact"))
    by_key = {(f.concept, f.context): f for f in clean if f.numeric is not None}
    contexts = {f.context for f in clean}
    parents: dict[tuple[str, str], list[Arc]] = {}
    for arc in calculations:
        if arc.weight is not None:
            parents.setdefault((arc.role, arc.parent), []).append(arc)
    for (_role, parent), arcs in parents.items():
        for context in sorted(contexts):
            total = by_key.get((parent, context))
            parts = [(by_key.get((a.child, context)), a.weight) for a in arcs]
            if total is None or any(p is None for p, _ in parts):
                continue
            expected = sum(p.numeric * w for p, w in parts)            # type: ignore[union-attr]
            allowed = max([_tolerance(total.decimals, total.numeric)] + [_tolerance(p.decimals, p.numeric) for p, _ in parts])  # type: ignore[union-attr]
            if abs(expected - total.numeric) > allowed:
                issues.append(Issue("calculation_mismatch", "error", parent, context, f"children add to {expected}, the filing says {total.numeric}"))
    return ValidationReport(issues=tuple(issues), facts=tuple(clean), duplicates_removed=removed)


# ---------------------------------------------------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------------------------------------------------
def _instance_text(text: str) -> str | None:
    doc = sgml.find_document(sgml.parse_documents(text), "EX-101.INS")
    return doc.xml if doc is not None else None


def load_instance(client: SecClient, ref: FilingRef) -> Instance | None:
    xml = _instance_text(client.submission_text(ref.cik, ref.accession))
    return parse_instance(xml) if xml else None


def filing_instance(client: SecClient, symbol: str | int, *, as_of: datetime | date, form: str = "10-K", accession: str | None = None) -> result.SecResult:
    """The XBRL instance of the latest filing of `form` visible at `as_of`, parsed (ED-021, ED-022)."""
    listed = list_filings(client, symbol, as_of=as_of, form=form, amendments=False)
    if not listed.ok:
        return listed
    refs = [r for r in listed.value if accession in (None, r.accession)]
    if not refs:
        return result.no_data("accession_not_visible_at_as_of", as_of, now=client.now)
    try:
        instance = load_instance(client, refs[0])
    except TransportError as exc:
        return client.fail(exc, as_of)
    except ValueError as exc:
        return result.no_data(f"instance_not_readable: {exc}", as_of, now=client.now)
    if instance is None:
        return result.no_data("no_xbrl_instance_document", as_of, now=client.now)
    return result.ok({"ref": refs[0], "instance": instance}, as_of, now=client.now)


def stitched_series(client: SecClient, symbol: str | int, concept: str, *, as_of: datetime | date, form: str = "10-K", filings: int = 3,
                    period: str = "annual") -> result.SecResult:
    """A multi year series of a concept from the last `filings` reports visible at `as_of` (ED-024)."""
    listed = list_filings(client, symbol, as_of=as_of, form=form, amendments=False)
    if not listed.ok:
        return listed
    pieces: list[FilingFacts] = []
    for ref in listed.value[:filings]:
        try:
            instance = load_instance(client, ref)
        except TransportError as exc:
            return client.fail(exc, as_of)
        except ValueError:
            continue
        if instance is not None:
            pieces.append(FilingFacts(ref.accession, ref.filing_date, instance))
    if not pieces:
        return result.no_data("no_xbrl_instance_document", as_of, now=client.now)
    points = stitch(pieces, concept if ":" in concept else f"us-gaap:{concept}", period=period)
    if not points:
        return result.no_data(f"concept_not_reported:{concept}", as_of, now=client.now)
    warnings = tuple(f"restated:{p.period_end}:{p.restated_from}->{p.value}" for p in points if p.restated_from is not None)
    return result.ok(points, as_of, now=client.now, warnings=warnings)

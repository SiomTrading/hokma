"""Tim, the news and macro analyst (SPEC-019: TA-03, DTS-02; the sector rotation reading OA-03 lives in his agent file).

Tim joins the news, the macro series at their fixed vintage, the calendar and the filings of the company into one report where every
line has a date and a source. A filing is the record and a headline is a hint: `eight_k_items` reads the items of the current reports
and sets each headline against them. Prediction market probabilities appear only when the data module returned them; otherwise they
are listed as missing. Tim gives no signal and no window to trade.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from hokma.desks.agents.analyst_kit import AnalystAgent, EvidenceLog, OutputSpec, RunContext
from hokma.desks.plantel import AnalystReport

CODE = "A04"
KEY = "news"
SNAPSHOT_CATEGORIES = ("news", "global_news", "prediction_markets")
EVENT_CATEGORIES = ("filing", "news", "macro", "calendar", "other")
PREDICTION_STATES = ("available", "data_missing")
HEADLINE_WINDOW_DAYS = 3

# ---- the table of items of a current report (DTS-02) ---------------------------------------------------------------

ITEM_KINDS: Mapping[str, tuple[str, str]] = {
    "1.01": ("material_agreement", "A binding agreement outside the ordinary course was signed."),
    "1.02": ("agreement_ended", "A material agreement was terminated."),
    "1.03": ("bankruptcy", "Bankruptcy or receivership."),
    "2.01": ("acquisition_or_disposal", "A deal for assets was completed."),
    "2.02": ("results", "Results of operations."),
    "2.03": ("new_obligation", "A direct financial obligation was created."),
    "2.04": ("acceleration", "A debt was accelerated or a trigger event happened."),
    "2.05": ("exit_costs", "Restructuring or exit activity with costs."),
    "2.06": ("impairment", "A material impairment was recorded."),
    "3.01": ("listing_notice", "A listing rule notice or a delisting."),
    "3.02": ("unregistered_sale", "Equity was sold outside a registration."),
    "3.03": ("rights_change", "The rights of security holders changed."),
    "4.01": ("auditor_change", "The auditor was dismissed or resigned."),
    "4.02": ("non_reliance", "Past financial statements should no longer be relied on."),
    "5.01": ("control_change", "Control of the company changed."),
    "5.02": ("officer_or_director_change", "A senior officer or director left, arrived or changed pay."),
    "5.03": ("governing_documents", "Charter or bylaws were amended."),
    "5.07": ("vote_result", "Results of a vote of holders."),
    "7.01": ("disclosure", "A Regulation FD disclosure."),
    "8.01": ("other_event", "An event the company chose to report; read the text."),
    "9.01": ("exhibits", "Financial statements and exhibits of the other items."),
}
# Kinds that only carry material of another item: they never confirm a headline by themselves.
CARRIER_KINDS = frozenset({"exhibits", "disclosure"})

HEADLINE_HINTS: Mapping[str, tuple[str, ...]] = {
    "results": ("earnings", "results", "quarterly", "revenue", "eps", "guidance"),
    "officer_or_director_change": ("ceo", "cfo", "chief", "resign", "steps down", "step down", "appoint", "director", "board"),
    "material_agreement": ("agreement", "contract", "partnership", "deal signed"),
    "acquisition_or_disposal": ("acquire", "acquisition", "merger", "buyout", "divest", "sells unit"),
    "auditor_change": ("auditor",),
    "non_reliance": ("restate", "restatement", "non-reliance"),
    "impairment": ("impairment", "write-down", "writedown"),
    "exit_costs": ("restructuring", "layoff", "layoffs", "job cuts"),
    "bankruptcy": ("bankruptcy", "chapter 11"),
    "listing_notice": ("delist", "delisting", "listing notice"),
    "unregistered_sale": ("private placement",),
    "new_obligation": ("notes offering", "debt offering", "credit facility", "loan"),
    "vote_result": ("shareholder vote", "annual meeting"),
}


@dataclass(frozen=True)
class EightKItem:
    number: str
    kind: str
    meaning: str


@dataclass(frozen=True)
class EightKRow:
    accession: str
    form: str
    filing_date: date | None
    report_date: date | None
    amends: bool
    items: tuple[EightKItem, ...]

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(i.kind for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {"accession": self.accession, "form": self.form, "filing_date": self.filing_date.isoformat() if self.filing_date else None,
                "report_date": self.report_date.isoformat() if self.report_date else None, "amends": self.amends,
                "items": [{"number": i.number, "kind": i.kind, "meaning": i.meaning} for i in self.items]}


@dataclass(frozen=True)
class HeadlineRead:
    text: str
    headline_kind: str | None
    status: str                       # confirmed, conflict, not_filed or unclassified
    accession: str | None
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "headline_kind": self.headline_kind, "status": self.status, "accession": self.accession, "note": self.note}


@dataclass(frozen=True)
class EightKTable:
    rows: tuple[EightKRow, ...]
    headlines: tuple[HeadlineRead, ...] = ()
    data_gaps: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {"filings": [r.to_dict() for r in self.rows], "headlines": [h.to_dict() for h in self.headlines],
                "data_gaps": list(self.data_gaps)}


def classify_item(number: str) -> EightKItem:
    """The kind of an item number. A number that is not in the table is `unknown`, never guessed."""
    key = str(number).strip()
    kind, meaning = ITEM_KINDS.get(key, ("unknown", "An item that is not in the table; read the filing."))
    return EightKItem(key, kind, meaning)


def headline_kind(text: str) -> str | None:
    """The kind of event a headline claims, from its words; None when no word matches."""
    low = " " + re.sub(r"[^a-z0-9\- ]", " ", (text or "").lower()) + " "
    best, hits = None, 0
    for kind, words in HEADLINE_HINTS.items():
        n = sum(1 for w in words if f" {w} " in low or (" " in w and w in low))
        if n > hits:
            best, hits = kind, n
    return best


def _day(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _row(filing: Any) -> EightKRow | None:
    get = (lambda k: filing.get(k)) if isinstance(filing, Mapping) else (lambda k: getattr(filing, k, None))
    form = str(get("form") or "").upper()
    if not form.startswith("8-K"):
        return None
    items = get("items") or ()
    if isinstance(items, str):
        items = [x for x in items.split(",")]
    numbers = tuple(dict.fromkeys(str(i).strip() for i in items if str(i).strip()))
    return EightKRow(str(get("accession") or ""), form, _day(get("filing_date")), _day(get("report_date")), form.endswith("/A"),
                     tuple(classify_item(n) for n in numbers))


def _rows_of(filings: Any) -> tuple[list[EightKRow], tuple[str, ...]]:
    """Rows from the answer of the filings tool (a `DataResult`), a list of filing records or a list of their dicts."""
    gaps: list[str] = []
    if hasattr(filings, "status") and hasattr(filings, "value"):
        if not filings.ok:
            return [], (f"filings_{str(filings.status).lower()}: {getattr(filings, 'reason', '')}".strip(),)
        filings = filings.value
    rows = [r for r in (_row(f) for f in (filings or ())) if r is not None]
    if not rows:
        gaps.append("no_8k_visible_at_cut_off")
    return rows, tuple(gaps)


def eight_k_items(filings: Any, headlines: Iterable[Mapping[str, Any] | str] = ()) -> EightKTable:
    """The table of items of the 8-K filings and each headline set against it (DTS-02).

    The filing is the record: the kind of every row comes from its item numbers, and a headline can only be `confirmed` by a filing
    that carries the kind it claims. A headline that claims another kind than the filing it points to is a `conflict` and the filing
    is reported as the fact. No filing visible for the headline is `not_filed`. A headline of no known kind is `unclassified`."""
    rows, gaps = _rows_of(filings)
    reads: list[HeadlineRead] = []
    for raw in headlines:
        item = {"text": raw} if isinstance(raw, str) else dict(raw)
        text = str(item.get("text") or item.get("title") or "")
        kind = headline_kind(text)
        accession = item.get("accession")
        when = _day(item.get("date") or item.get("published_at"))
        if accession:
            candidates = [r for r in rows if r.accession == accession]
        elif when is not None:
            candidates = [r for r in rows if r.filing_date is not None and timedelta(0) <= (r.filing_date - when) <= timedelta(days=HEADLINE_WINDOW_DAYS)]
        else:
            candidates = list(rows)
        if kind is None:
            reads.append(HeadlineRead(text, None, "unclassified", None, "The headline names no kind of event; the filings decide."))
            continue
        carrying = [r for r in candidates if kind in {k for k in r.kinds if k not in CARRIER_KINDS}]
        if carrying:
            reads.append(HeadlineRead(text, kind, "confirmed", carrying[0].accession, f"The filing carries {kind}."))
        elif candidates:
            carried = sorted({k for r in candidates for k in r.kinds})
            reads.append(HeadlineRead(text, kind, "conflict", candidates[0].accession,
                                      f"The headline hints {kind}; the filing carries {', '.join(carried) or 'no item'}. The filing rules."))
        else:
            reads.append(HeadlineRead(text, kind, "not_filed", None, "No filing visible at the cut-off carries this event."))
    return EightKTable(tuple(rows), tuple(reads), gaps)


# ---- the report -----------------------------------------------------------------------------------------------------

_TEXT = {"type": "string", "maxLength": 600}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "events", "macro", "calendar", "prediction_markets", "sector_rotation", "data_gaps"],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 1200},
        "events": {"type": "array", "items": {
            "type": "object", "required": ["date", "source", "category", "text"],
            "properties": {"date": {"type": "string", "minLength": 8}, "source": {"type": "string", "minLength": 1},
                           "category": {"type": "string", "enum": list(EVENT_CATEGORIES)}, "text": _TEXT}}},
        "macro": {"type": "array", "items": {
            "type": "object", "required": ["series_id", "vintage", "value", "source"],
            "properties": {"series_id": {"type": "string", "minLength": 1}, "vintage": {"type": "string", "minLength": 8},
                           "value": {"type": "number"}, "source": {"type": "string", "minLength": 1}}}},
        "calendar": {"type": "array", "items": {
            "type": "object", "required": ["event", "when", "source"],
            "properties": {"event": _TEXT, "when": {"type": "string", "minLength": 8}, "source": {"type": "string", "minLength": 1}}}},
        "prediction_markets": {"type": "object", "required": ["status", "items"], "properties": {
            "status": {"type": "string", "enum": list(PREDICTION_STATES)},
            "items": {"type": "array", "items": {"type": "object", "required": ["question", "probability", "source"],
                                                 "properties": {"question": _TEXT, "probability": {"type": "number"},
                                                                "source": {"type": "string", "minLength": 1}}}}}},
        "sector_rotation": {"type": "object", "required": ["regime", "note", "ranking"], "properties": {
            "regime": _TEXT, "note": _TEXT,
            "ranking": {"type": "array", "items": {"type": "object", "required": ["sector", "horizon"],
                                                   "properties": {"sector": _TEXT, "horizon": {"type": "string", "enum": ["short", "long"]}}}}}},
        "data_gaps": {"type": "array", "items": _TEXT},
    },
}


def _find_sections(node: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(node, Mapping):
        if isinstance(node.get("sections"), Mapping):
            yield node["sections"]
        for item in node.values():
            yield from _find_sections(item)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _find_sections(item)


def prediction_markets_state(evidence: EvidenceLog) -> str:
    """`available` only when a snapshot of this run holds a prediction market section the data module read; else `data_missing`."""
    for sections in _find_sections([evidence.snapshot, evidence.results]):
        section = sections.get("prediction_markets")
        if isinstance(section, Mapping) and section.get("status") == "ok":
            return "available"
    return "data_missing"


def _prediction_markets_honest(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    stated = value["prediction_markets"]
    state = prediction_markets_state(evidence)
    if state == "data_missing" and (stated["status"] != "data_missing" or stated["items"]):
        return ["prediction_markets: the data module did not return them, so they are data_missing and carry no probability"]
    return []


def _dated_and_sourced(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    problems: list[str] = []
    cut = ctx.as_of.astimezone(timezone.utc).date()
    for where, rows, key in (("events", value["events"], "date"), ("macro", value["macro"], "vintage")):
        for row in rows:
            day = _day(row[key])
            if day is None:
                problems.append(f"{where}: {row[key]!r} is not a date")
            elif day > cut:
                problems.append(f"{where}: {row[key]} is after the cut-off, nothing published later may be used")
    for row in value["calendar"]:
        if _day(row["when"]) is None:
            problems.append(f"calendar: {row['when']!r} is not a date")
    return problems


def _required_gaps(ctx: RunContext, evidence: EvidenceLog) -> tuple[str, ...]:
    return ("prediction_markets",) if prediction_markets_state(evidence) == "data_missing" else ()


def data_sources() -> dict[str, Any]:
    from hokma.data import news as news_data
    from hokma.data import social as social_data

    return {"news": lambda symbol, cut: news_data.news(symbol, as_of=cut),
            "global_news": lambda symbol, cut: news_data.global_news(as_of=cut),
            "prediction_markets": lambda symbol, cut: social_data.prediction_markets(symbol, as_of=cut)}


def render(value: dict) -> AnalystReport:
    findings = [f"{e['date']} ({e['category']}, source {e['source']}): {e['text']}" for e in value["events"]]
    findings += [f"Macro {m['series_id']} as published on {m['vintage']}: {m['value']} (source {m['source']})." for m in value["macro"]]
    findings += [f"Calendar {c['event']} at {c['when']} (source {c['source']})." for c in value["calendar"]]
    pm = value["prediction_markets"]
    findings.append("Prediction markets: " + ("; ".join(f"{i['question']} {i['probability']} (source {i['source']})" for i in pm["items"])
                                              if pm["status"] == "available" and pm["items"] else "missing data."))
    rot = value["sector_rotation"]
    findings.append(f"Sector picture: {rot['regime']}. {rot['note']}")
    return AnalystReport(value["summary"], tuple(findings), tuple(value["data_gaps"]))


SPEC = OutputSpec(SCHEMA, extra_forbidden=frozenset({"window", "direction", "bias"}), checks=(_prediction_markets_honest, _dated_and_sourced),
                  required_gaps=_required_gaps)
AGENT = AnalystAgent(CODE, KEY, SPEC, render, snapshot_categories=SNAPSHOT_CATEGORIES, sources=data_sources)

system_prompt = AGENT.system_prompt
toolbox = AGENT.toolbox
run = AGENT.run

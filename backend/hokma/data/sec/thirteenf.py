"""Institutional holdings (Form 13F-HR), point in time.

Rows: ED-001 (positions, comparison, history), ED-002 (unit of the values: dollars or thousands, with a warning when the
evidence is ambiguous instead of a silent guess), ED-003 (who manages a portfolio: signer heuristic).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from hokma.data import _lib
from hokma.data.sec import result, sgml
from hokma.data.sec.client import SecClient
from hokma.data.sec.filings import list_filings
from hokma.data.sec.index import FilingRef
from hokma.data.sec.plain import to_plain
from hokma.data.sec.transport import TransportError

HOLDINGS_FORMS = ("13F-HR",)
UNIT_OVERRIDES = ("dollars", "thousands")


# ---------------------------------------------------------------------------------------------------------------------
# Unit of the values (ED-002)
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class UnitResolution:
    unit: str                    # dollars or thousands
    multiplier: int              # applied to the reported value to get dollars
    source: str                  # override, implied_prices, schema, report_period or default
    ambiguous: bool
    priceable_rows: int
    warning: str | None


def resolve_unit(frame: Any, schema_version: str | None, report_period: date | None, override: str | None = None) -> UnitResolution:
    """The unit of the value column. When the evidence is ambiguous the result says so in `warning`: the caller decides, nothing is guessed silently."""
    if override is not None and override not in UNIT_OVERRIDES:
        raise ValueError(f"unit must be one of {UNIT_OVERRIDES}, got {override!r}")
    period = datetime(report_period.year, report_period.month, report_period.day) if report_period else None
    res = _lib.sec_resolve_13f_unit(frame, schema_version, period, override)
    warning = None
    if res.ambiguous:
        warning = (f"13F value unit is ambiguous ({res.priceable_rows} priceable rows, evidence: {res.source}); read as {res.unit}. "
                   f"Pass unit='dollars' or unit='thousands' to decide.")
    return UnitResolution(unit=res.unit, multiplier=int(res.multiplier), source=res.source, ambiguous=bool(res.ambiguous),
                          priceable_rows=int(res.priceable_rows), warning=warning)


# ---------------------------------------------------------------------------------------------------------------------
# Who manages the portfolio (ED-003)
# ---------------------------------------------------------------------------------------------------------------------
_DECISION_MAKER = re.compile(r"portfolio manager|chief investment|\bcio\b|investment (?:officer|director|manager)|managing (?:partner|director|member)|"
                             r"founder|principal|\bpartner\b|chairman|chief executive|\bceo\b|president", re.I)
_CONTROL_FUNCTION = re.compile(r"compliance|operations|legal|counsel|controller|chief financial|\bcfo\b|\bcco\b|treasurer|administrat|"
                               r"secretary|analyst|associate|assistant", re.I)


@dataclass(frozen=True)
class SignerRole:
    name: str | None
    title: str | None
    role: str                    # investment_decision_maker, control_function or unknown
    likely_portfolio_manager: bool


def classify_signer(name: str | None, title: str | None) -> SignerRole:
    """Heuristic on the title of the person who signed the filing: decision makers sign for the firm, control functions only file.

    A heuristic, not a fact: `likely_portfolio_manager` is True only when the title names an investment decision role and no
    control function word.
    """
    text = (title or "").strip()
    if not text:
        return SignerRole(name, title, "unknown", False)
    control = bool(_CONTROL_FUNCTION.search(text))
    decision = bool(_DECISION_MAKER.search(text))
    if decision and not control:
        return SignerRole(name, title, "investment_decision_maker", True)
    if control:
        return SignerRole(name, title, "control_function", False)
    return SignerRole(name, title, "unknown", False)


# ---------------------------------------------------------------------------------------------------------------------
# Positions (ED-001)
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Holding:
    issuer: str
    cusip: str
    ticker: str | None
    class_title: str
    put_call: str | None
    shares: float
    shares_type: str
    value_reported: float          # as written in the filing
    value_usd: float               # reported value in dollars after the unit decision
    sole_voting: float
    shared_voting: float
    non_voting: float


@dataclass(frozen=True)
class ThirteenFReport:
    ref: FilingRef | None
    manager_name: str
    manager_cik: int | None
    period: date | None
    holdings: tuple[Holding, ...]
    total_value_usd: float
    entry_count: int
    unit: UnitResolution
    signer: SignerRole

    def by_cusip(self) -> dict[str, Holding]:
        return {h.cusip: h for h in self.holdings}


def _f(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if number != number else number


def _s(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def build_report(text: str, ref: FilingRef | None = None, *, unit: str | None = None) -> ThirteenFReport | None:
    """A holdings report from the submission text, or None when the filing is a notice without a holdings table."""
    typed = _lib.sec_thirteenf(text)
    if typed is None or not typed.has_infotable:
        return None
    docs = sgml.parse_documents(text)
    primary = sgml.find_xml(docs, contains="schemaVersion") or ""
    version = re.search(r"<schemaVersion>\s*([^<\s]+)", primary)
    period = date.fromisoformat(str(typed.report_period)[:10]) if typed.report_period else None
    resolution = resolve_unit(typed.infotable, version.group(1) if version else None, period, unit)
    rows = to_plain(typed.holdings)
    holdings = tuple(Holding(issuer=str(r.get("Issuer") or ""), cusip=str(r.get("Cusip") or ""), ticker=_s(r.get("Ticker")),
                             class_title=str(r.get("Class") or ""), put_call=_s(r.get("PutCall")), shares=_f(r.get("SharesPrnAmount")),
                             shares_type=str(r.get("Type") or ""), value_reported=_f(r.get("Value")),
                             value_usd=_f(r.get("Value")) * resolution.multiplier, sole_voting=_f(r.get("SoleVoting")),
                             shared_voting=_f(r.get("SharedVoting")), non_voting=_f(r.get("NonVoting"))) for r in rows)
    signer = classify_signer(_s(typed.filing_signer_name), _s(typed.filing_signer_title))
    return ThirteenFReport(ref=ref, manager_name=str(typed.management_company_name), manager_cik=ref.cik if ref else None, period=period,
                           holdings=holdings, total_value_usd=sum(h.value_usd for h in holdings), entry_count=len(holdings), unit=resolution,
                           signer=signer)


def _latest_per_period(refs: list[FilingRef]) -> list[FilingRef]:
    """One filing per report period: the latest filed visible one, so an amendment replaces the original from its filing day."""
    chosen: dict[Any, FilingRef] = {}
    for ref in sorted(refs, key=lambda r: (r.filing_date, r.accession)):
        chosen[ref.report_date or ref.filing_date] = ref
    return sorted(chosen.values(), key=lambda r: (r.report_date or r.filing_date), reverse=True)


def holdings_history(client: SecClient, manager: str | int, *, as_of: datetime | date, quarters: int = 4, unit: str | None = None) -> result.SecResult:
    """The last `quarters` holdings reports of a manager visible at `as_of`, newest first (ED-001)."""
    listed = list_filings(client, manager, as_of=as_of, form=HOLDINGS_FORMS, amendments=True)
    if not listed.ok:
        return listed
    reports: list[ThirteenFReport] = []
    warnings = list(listed.warnings)
    for ref in _latest_per_period(listed.value)[:quarters]:
        try:
            text = client.submission_text(ref.cik, ref.accession)
        except TransportError as exc:
            return client.fail(exc, as_of)
        report = build_report(text, ref, unit=unit)
        if report is None:
            warnings.append(f"no_holdings_table:{ref.accession}")
            continue
        if report.unit.warning:
            warnings.append(f"{ref.accession}: {report.unit.warning}")
        reports.append(report)
    if not reports:
        return result.no_data("no_holdings_report_visible_at_as_of", as_of, now=client.now, warnings=tuple(warnings))
    return result.ok(reports, as_of, now=client.now, warnings=tuple(warnings))


def latest_report(client: SecClient, manager: str | int, *, as_of: datetime | date, unit: str | None = None) -> result.SecResult:
    """The newest holdings report visible at `as_of` (ED-001)."""
    got = holdings_history(client, manager, as_of=as_of, quarters=1, unit=unit)
    if not got.ok:
        return got
    return result.ok(got.value[0], as_of, now=client.now, warnings=got.warnings)


@dataclass(frozen=True)
class PositionChange:
    cusip: str
    issuer: str
    status: str                  # NEW, CLOSED, INCREASED, DECREASED, UNCHANGED
    shares_previous: float
    shares_current: float
    shares_change: float
    pct_change: float | None     # None when there was no previous position
    value_previous_usd: float
    value_current_usd: float
    weight_current: float        # share of the current portfolio value


def compare_reports(current: ThirteenFReport, previous: ThirteenFReport) -> list[PositionChange]:
    """Position by position change between two reports of the same manager, ordered by current value."""
    cur, prev = current.by_cusip(), previous.by_cusip()
    total = current.total_value_usd or 0.0
    changes: list[PositionChange] = []
    for cusip in {*cur, *prev}:
        c, p = cur.get(cusip), prev.get(cusip)
        shares_c, shares_p = (c.shares if c else 0.0), (p.shares if p else 0.0)
        if p is None:
            status = "NEW"
        elif c is None:
            status = "CLOSED"
        elif shares_c > shares_p:
            status = "INCREASED"
        elif shares_c < shares_p:
            status = "DECREASED"
        else:
            status = "UNCHANGED"
        value_c = c.value_usd if c else 0.0
        changes.append(PositionChange(cusip=cusip, issuer=(c or p).issuer, status=status, shares_previous=shares_p, shares_current=shares_c,
                                      shares_change=shares_c - shares_p, pct_change=(shares_c / shares_p - 1.0) if shares_p else None,
                                      value_previous_usd=p.value_usd if p else 0.0, value_current_usd=value_c,
                                      weight_current=(value_c / total) if total else 0.0))
    changes.sort(key=lambda x: (-x.value_current_usd, x.cusip))
    return changes


def position_changes(client: SecClient, manager: str | int, *, as_of: datetime | date, unit: str | None = None) -> result.SecResult:
    """Changes between the two newest holdings reports visible at `as_of` (ED-001)."""
    got = holdings_history(client, manager, as_of=as_of, quarters=2, unit=unit)
    if not got.ok:
        return got
    if len(got.value) < 2:
        return result.no_data("only_one_holdings_report_visible", as_of, now=client.now, warnings=got.warnings)
    current, previous = got.value[0], got.value[1]
    warnings = list(got.warnings)
    if current.unit.unit != previous.unit.unit:
        warnings.append(f"units differ between periods ({previous.unit.unit} then {current.unit.unit}); values are not comparable")
    return result.ok({"current_period": current.period, "previous_period": previous.period, "changes": compare_reports(current, previous)},
                     as_of, now=client.now, warnings=tuple(warnings))


def manager_profile(client: SecClient, manager: str | int, *, as_of: datetime | date) -> result.SecResult:
    """Who files and signs for the portfolio: manager name, signer and the role heuristic (ED-003)."""
    got = latest_report(client, manager, as_of=as_of)
    if not got.ok:
        return got
    report: ThirteenFReport = got.value
    return result.ok({"manager_name": report.manager_name, "manager_cik": report.manager_cik, "period": report.period, "signer": report.signer,
                      "entry_count": report.entry_count, "total_value_usd": report.total_value_usd}, as_of, now=client.now, warnings=got.warnings)

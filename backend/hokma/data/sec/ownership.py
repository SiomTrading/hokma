"""Ownership filings, point in time: insiders (Forms 3, 4, 5), notices of proposed sale (Form 144) and beneficial owners above
5 percent (Schedules 13D and 13G).

Rows: ED-005 (Forms 3, 4, 5), ED-006 (Form 144), ED-007 (Schedule 13D and 13G).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from hokma.data import _lib
from hokma.data.sec import result, sgml
from hokma.data.sec.client import SecClient
from hokma.data.sec.filings import list_filings
from hokma.data.sec.index import FilingRef
from hokma.data.sec.plain import to_plain
from hokma.data.sec.transport import TransportError

INSIDER_FORMS = ("3", "4", "5")
BENEFICIAL_FORMS = ("SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G")
OPEN_MARKET_BUY = "P"
OPEN_MARKET_SELL = "S"


# ---------------------------------------------------------------------------------------------------------------------
# Forms 3, 4, 5 (ED-005)
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class InsiderTransaction:
    security: str
    date: date | None
    code: str
    transaction_type: str
    shares: float | None
    price: float | None
    acquired_disposed: str          # A or D
    remaining: float | None
    direct_indirect: str
    derivative: bool = False
    underlying: str | None = None
    underlying_shares: float | None = None
    exercise_price: float | None = None
    expiration: str | None = None


@dataclass(frozen=True)
class InsiderHolding:
    security: str
    shares: float | None
    direct_indirect: str
    nature: str | None
    derivative: bool
    underlying: str | None = None
    underlying_shares: float | None = None
    exercise_price: str | None = None
    expiration: str | None = None


@dataclass(frozen=True)
class OwnershipReport:
    ref: FilingRef | None
    form: str
    issuer_cik: str
    issuer_name: str
    ticker: str | None
    owner_name: str                  # as written in the filing
    owner_cik: str
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    officer_title: str | None
    period: date | None
    transactions: tuple[InsiderTransaction, ...] = ()
    holdings: tuple[InsiderHolding, ...] = ()
    aff10b5_one: bool = False

    def shares_bought(self) -> float:
        return sum(t.shares or 0.0 for t in self.transactions if not t.derivative and t.code == OPEN_MARKET_BUY)

    def shares_sold(self) -> float:
        return sum(t.shares or 0.0 for t in self.transactions if not t.derivative and t.code == OPEN_MARKET_SELL)


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _text(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and value != value):
        return None
    return str(value)


def _day(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _records(table: Any, kind: str) -> list[dict[str, Any]]:
    """Rows of a transactions or holdings table of the parsed form, as plain dicts."""
    try:
        has = table.has_transactions if kind == "transactions" else table.has_holdings
        if not has:
            return []
        frame = (table.transactions if kind == "transactions" else table.holdings).data
        return to_plain(frame)
    except AttributeError:
        return []


def parse_ownership_xml(xml: str, form: str, ref: FilingRef | None = None) -> OwnershipReport:
    """A Form 3, 4 or 5 from its XML. Nothing is looked up on the network."""
    code = form.upper().split("/")[0].replace("FORM ", "").strip()
    parsed = _lib.sec_parse_ownership(code, xml)
    owner = parsed.reporting_owners.owners[0]
    transactions: list[InsiderTransaction] = []
    for row in _records(parsed.non_derivative_table, "transactions"):
        transactions.append(InsiderTransaction(security=row.get("Security") or "", date=_day(row.get("Date")), code=row.get("Code") or "",
                                               transaction_type=row.get("TransactionType") or "", shares=_num(row.get("Shares")),
                                               price=_num(row.get("Price")), acquired_disposed=row.get("AcquiredDisposed") or "",
                                               remaining=_num(row.get("Remaining")), direct_indirect=row.get("DirectIndirect") or ""))
    for row in _records(parsed.derivative_table, "transactions"):
        transactions.append(InsiderTransaction(security=row.get("Security") or "", date=_day(row.get("Date")), code=row.get("Code") or "",
                                               transaction_type=row.get("TransactionType") or "", shares=_num(row.get("Shares")),
                                               price=_num(row.get("Price")), acquired_disposed=row.get("AcquiredDisposed") or "",
                                               remaining=_num(row.get("Remaining")), direct_indirect=row.get("DirectIndirect") or "",
                                               derivative=True, underlying=_text(row.get("Underlying")),
                                               underlying_shares=_num(row.get("UnderlyingShares")), exercise_price=_num(row.get("ExercisePrice")),
                                               expiration=_text(row.get("ExpirationDate"))))
    holdings: list[InsiderHolding] = []
    for row in _records(parsed.non_derivative_table, "holdings"):
        holdings.append(InsiderHolding(security=row.get("Security") or "", shares=_num(row.get("Shares")),
                                       direct_indirect=row.get("DirectIndirect") or "", nature=_text(row.get("Nature Of Ownership")), derivative=False))
    for row in _records(parsed.derivative_table, "holdings"):
        holdings.append(InsiderHolding(security=row.get("Security") or "", shares=None, direct_indirect=row.get("DirectIndirect") or "",
                                       nature=_text(row.get("Nature Of Ownership")), derivative=True, underlying=_text(row.get("Underlying")),
                                       underlying_shares=_num(row.get("UnderlyingShares")), exercise_price=_text(row.get("ExercisePrice")),
                                       expiration=_text(row.get("ExpirationDate"))))
    issuer = parsed.issuer
    return OwnershipReport(ref=ref, form=code, issuer_cik=str(issuer.cik), issuer_name=str(issuer.name), ticker=_text(issuer.ticker),
                           owner_name=str(owner.name_unreversed), owner_cik=str(owner.cik), is_director=bool(owner.is_director),
                           is_officer=bool(owner.is_officer), is_ten_percent_owner=bool(owner.is_ten_pct_owner),
                           officer_title=_text(owner.officer_title), period=_day(parsed.reporting_period),
                           transactions=tuple(transactions), holdings=tuple(holdings), aff10b5_one=bool(parsed.aff10b5_one))


def _xml_of(text: str) -> str | None:
    return sgml.find_xml(sgml.parse_documents(text))


def insider_filings(client: SecClient, symbol: str | int, *, as_of: datetime | date, forms: Iterable[str] = INSIDER_FORMS,
                    since: date | None = None, limit: int | None = None) -> result.SecResult:
    """Forms 3, 4 and 5 filed for the issuer and visible at `as_of`, newest first, parsed (ED-005)."""
    listed = list_filings(client, symbol, as_of=as_of, form=tuple(forms), amendments=True)
    if not listed.ok:
        return listed
    reports: list[OwnershipReport] = []
    warnings = list(listed.warnings)
    for ref in listed.value:
        if since is not None and ref.filing_date < since:
            continue
        if limit is not None and len(reports) >= limit:
            break
        try:
            text = client.submission_text(ref.cik, ref.accession)
        except TransportError as exc:
            return client.fail(exc, as_of)
        xml = _xml_of(text)
        if xml is None:
            warnings.append(f"no_xml_in:{ref.accession}")
            continue
        reports.append(parse_ownership_xml(xml, ref.form, ref))
    if not reports:
        return result.no_data("no_insider_filing_in_window", as_of, now=client.now, warnings=tuple(warnings))
    return result.ok(reports, as_of, now=client.now, warnings=tuple(warnings))


@dataclass(frozen=True)
class InsiderActivity:
    window_start: date
    window_end: date
    reports: int
    insiders: tuple[str, ...]
    shares_bought: float
    shares_sold: float
    net_shares: float
    value_bought: float
    value_sold: float


def insider_activity(client: SecClient, symbol: str | int, *, as_of: datetime | date, window_days: int = 90) -> result.SecResult:
    """Open market buys (code P) and sells (code S) of insiders in the window that ends at `as_of` (ED-005).

    Awards, gifts, option exercises and other codes are not counted as buying or selling.
    """
    day = as_of.date() if isinstance(as_of, datetime) else as_of
    start = day - timedelta(days=window_days)
    got = insider_filings(client, symbol, as_of=as_of, forms=("4",), since=start)
    if not got.ok:
        return got
    bought = sold = vbought = vsold = 0.0
    insiders: set[str] = set()
    for report in got.value:
        for t in report.transactions:
            if t.derivative or t.date is None or not (start <= t.date <= day):
                continue
            if t.code == OPEN_MARKET_BUY:
                bought += t.shares or 0.0
                vbought += (t.shares or 0.0) * (t.price or 0.0)
                insiders.add(report.owner_name)
            elif t.code == OPEN_MARKET_SELL:
                sold += t.shares or 0.0
                vsold += (t.shares or 0.0) * (t.price or 0.0)
                insiders.add(report.owner_name)
    summary = InsiderActivity(window_start=start, window_end=day, reports=len(got.value), insiders=tuple(sorted(insiders)), shares_bought=bought,
                              shares_sold=sold, net_shares=bought - sold, value_bought=vbought, value_sold=vsold)
    return result.ok(summary, as_of, now=client.now, warnings=got.warnings)


# ---------------------------------------------------------------------------------------------------------------------
# Form 144 (ED-006)
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SaleNotice:
    ref: FilingRef | None
    issuer_cik: str | None
    issuer_name: str | None
    seller: str | None
    relationships: tuple[str, ...]
    securities_to_be_sold: tuple[dict[str, Any], ...]
    sold_past_three_months: tuple[dict[str, Any], ...]
    notice_date: date | None
    remarks: str | None

    @property
    def units_to_be_sold(self) -> float:
        return sum(_num(row.get("units_to_be_sold")) or 0.0 for row in self.securities_to_be_sold)


def _us_date(text: Any) -> date | None:
    try:
        month, day, year = str(text).split("/")
        return date(int(year), int(month), int(day))
    except (ValueError, AttributeError):
        return None


def parse_form144_xml(xml: str, ref: FilingRef | None = None) -> SaleNotice:
    data = _lib.sec_parse_form144(xml)
    signature = data.get("notice_signature")
    notice = _us_date(getattr(signature, "notice_date", None))
    return SaleNotice(ref=ref, issuer_cik=_text(data.get("issuer_cik")), issuer_name=_text(data.get("issuer_name")),
                      seller=_text(data.get("person_selling")), relationships=tuple(data.get("relationships") or ()),
                      securities_to_be_sold=tuple(to_plain(data.get("securities_information")) or ()),
                      sold_past_three_months=tuple(to_plain(data.get("securities_sold_past_3_months")) or ()),
                      notice_date=notice, remarks=_text(data.get("remarks")))


def sale_notices(client: SecClient, symbol: str | int, *, as_of: datetime | date, since: date | None = None) -> result.SecResult:
    """Form 144 notices visible at `as_of` (ED-006)."""
    listed = list_filings(client, symbol, as_of=as_of, form="144", amendments=True)
    if not listed.ok:
        return listed
    notices: list[SaleNotice] = []
    for ref in listed.value:
        if since is not None and ref.filing_date < since:
            continue
        try:
            xml = _xml_of(client.submission_text(ref.cik, ref.accession))
        except TransportError as exc:
            return client.fail(exc, as_of)
        if xml:
            notices.append(parse_form144_xml(xml, ref))
    if not notices:
        return result.no_data("no_sale_notice_in_window", as_of, now=client.now)
    return result.ok(notices, as_of, now=client.now, warnings=listed.warnings)


# ---------------------------------------------------------------------------------------------------------------------
# Schedules 13D and 13G (ED-007)
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ReportingPerson:
    name: str
    cik: str | None
    aggregate_amount: float | None
    percent_of_class: float | None
    sole_voting_power: float | None
    shared_voting_power: float | None
    sole_dispositive_power: float | None
    shared_dispositive_power: float | None
    citizenship: str | None = None


@dataclass(frozen=True)
class BeneficialOwnerReport:
    ref: FilingRef | None
    kind: str                         # 13D (activist or control intent) or 13G (passive)
    issuer_cik: str | None
    issuer_name: str | None
    security_title: str | None
    cusip: str | None
    event_date: date | None
    persons: tuple[ReportingPerson, ...]
    rule_designation: str | None = None
    is_amendment: bool = False

    @property
    def max_percent(self) -> float | None:
        values = [p.percent_of_class for p in self.persons if p.percent_of_class is not None]
        return max(values) if values else None


def parse_schedule13_xml(xml: str, form: str, ref: FilingRef | None = None) -> BeneficialOwnerReport:
    kind = "13D" if "13D" in form.upper() else "13G"
    data = _lib.sec_parse_schedule13(kind, xml)
    issuer = data.get("issuer_info")
    security = data.get("security_info")
    persons = tuple(ReportingPerson(name=str(p.name), cik=_text(p.cik) or None, aggregate_amount=_num(p.aggregate_amount),
                                    percent_of_class=_num(p.percent_of_class), sole_voting_power=_num(p.sole_voting_power),
                                    shared_voting_power=_num(p.shared_voting_power), sole_dispositive_power=_num(p.sole_dispositive_power),
                                    shared_dispositive_power=_num(p.shared_dispositive_power), citizenship=_text(p.citizenship))
                    for p in data.get("reporting_persons") or ())
    event = _us_date(data.get("date_of_event") or data.get("event_date"))
    return BeneficialOwnerReport(ref=ref, kind=kind, issuer_cik=_text(getattr(issuer, "cik", None)), issuer_name=_text(getattr(issuer, "name", None)),
                                 security_title=_text(getattr(security, "title", None)), cusip=_text(getattr(security, "cusip", None)),
                                 event_date=event, persons=persons, rule_designation=_text(data.get("rule_designation")),
                                 is_amendment="/A" in form.upper())


def beneficial_owners(client: SecClient, symbol: str | int, *, as_of: datetime | date, kinds: Iterable[str] = ("13D", "13G")) -> result.SecResult:
    """Schedule 13D and 13G reports about the issuer, visible at `as_of`, newest first (ED-007)."""
    wanted = {k.upper() for k in kinds}
    forms = tuple(f for f in BENEFICIAL_FORMS if any(k in f for k in wanted))
    listed = list_filings(client, symbol, as_of=as_of, form=forms, amendments=True)
    if not listed.ok:
        return listed
    reports: list[BeneficialOwnerReport] = []
    for ref in listed.value:
        try:
            xml = _xml_of(client.submission_text(ref.cik, ref.accession))
        except TransportError as exc:
            return client.fail(exc, as_of)
        if xml:
            reports.append(parse_schedule13_xml(xml, ref.form, ref))
    if not reports:
        return result.no_data("no_beneficial_ownership_report", as_of, now=client.now)
    return result.ok(reports, as_of, now=client.now, warnings=listed.warnings)

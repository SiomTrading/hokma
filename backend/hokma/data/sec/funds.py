"""Funds and other collective or structured filings, point in time: fund portfolios (N-PORT), money market funds (N-MFP3), fund
census (N-CEN), shareholder reports (N-CSR) and prospectuses (497K), business development company portfolios, asset backed
loan data, alternative trading system notices, proxy votes (N-PX) and proxy statements.

Rows: ED-012 (funds), ED-013 (business development companies), ED-014 (asset backed securities), ED-015 (ATS-N),
ED-016 (N-PX and proxy).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from lxml import etree

from hokma.data import _lib
from hokma.data.sec import result, sgml
from hokma.data.sec.client import SecClient
from hokma.data.sec.filings import list_filings, parse_document
from hokma.data.sec.index import FilingRef
from hokma.data.sec.plain import pick, to_plain
from hokma.data.sec.transport import TransportError
from hokma.data.sec.xbrl import Instance, parse_instance

FUND_FORMS = ("NPORT-P", "N-CEN", "N-CSR", "N-CSRS", "N-MFP3", "497K")


@dataclass(frozen=True)
class FundFiling:
    ref: FilingRef | None
    form: str
    kind: str                        # portfolio, money_market, census, shareholder_report, prospectus, ats_notice, proxy_votes, asset_data
    name: str | None
    period: date | None
    data: dict[str, Any] = field(default_factory=dict)


def _day(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------------------------------
# Fund portfolios and reports (ED-012)
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PortfolioPosition:
    name: str
    cusip: str | None
    ticker: str | None
    isin: str | None
    balance: float | None
    units: str | None
    value_usd: float | None
    pct_value: float | None
    asset_category: str | None
    issuer_category: str | None
    country: str | None


def parse_nport(xml: str, ref: FilingRef | None = None) -> FundFiling:
    """N-PORT: the fund, its totals and every position."""
    parts = _lib.sec_parse_nport(xml)
    info, fund = parts["general_info"], parts["fund_info"]
    positions = tuple(PortfolioPosition(name=str(i.name), cusip=i.cusip, ticker=i.identifiers.ticker if i.identifiers else None,
                                        isin=i.identifiers.isin if i.identifiers else None, balance=_f(i.balance), units=i.units,
                                        value_usd=_f(i.value_usd), pct_value=_f(i.pct_value), asset_category=i.asset_category,
                                        issuer_category=i.issuer_category, country=i.investment_country) for i in parts["investments"])
    data = {"series_id": info.series_id, "total_assets": _f(fund.total_assets), "net_assets": _f(fund.net_assets), "positions": positions,
            "position_count": len(positions), "invested_value_usd": sum(p.value_usd or 0.0 for p in positions)}
    return FundFiling(ref, "NPORT-P", "portfolio", str(info.name), _day(info.rep_period_date), data)


def parse_money_market(xml: str, ref: FilingRef | None = None) -> FundFiling:
    """N-MFP3: the money market fund, its maturities and its securities."""
    fund = _lib.sec_parse_money_market(xml)
    securities = to_plain(fund.portfolio_data())
    data = {"net_assets": _f(fund.net_assets), "fund_category": fund.fund_category, "weighted_average_maturity": fund.average_maturity_wam,
            "weighted_average_life": fund.average_maturity_wal, "security_count": int(fund.num_securities), "securities": securities}
    return FundFiling(ref, "N-MFP3", "money_market", str(fund.name), _day(fund.report_date), data)


def _typed_summary(text: str, ref: FilingRef | None, form: str, kind: str) -> FundFiling:
    typed = _lib.sec_typed_filing(text)
    if typed is None:
        raise ValueError(f"the filing library does not read {form}")
    data = pick(typed, "name", "report_date", "num_series", "classification_type", "is_etf_company", "series_id", "ats_name", "mpid",
                "withdrawn_accession_number", "form_type")
    context = typed.to_context() if hasattr(typed, "to_context") else None
    if context:
        data["context"] = context
    data["object_type"] = type(typed).__name__
    name = data.get("name") or data.get("ats_name")
    return FundFiling(ref, form, kind, str(name) if name else None, _day(data.get("report_date")), data)


def parse_census(text: str, ref: FilingRef | None = None) -> FundFiling:
    """N-CEN: the registrant, its series and service providers."""
    filing = _typed_summary(text, ref, "N-CEN", "census")
    typed = _lib.sec_typed_filing(text)
    series = to_plain(typed.series_data())
    providers = typed.service_providers()
    return FundFiling(ref, "N-CEN", "census", filing.name, filing.period,
                      {**filing.data, "series": series, "service_provider_count": len(providers)})


def parse_fund_text(text: str, ref: FilingRef | None = None) -> FundFiling:
    """Dispatches a fund filing (full submission text) to its reader by the form in the header."""
    header = sgml.parse_header(text)
    docs = sgml.parse_documents(text)
    form = (header.form or (ref.form if ref else "")).upper()
    base = form.split("/")[0]
    if base.startswith("NPORT"):
        xml = sgml.find_xml(docs, contains="invstOrSec") or sgml.find_xml(docs)
        return parse_nport(xml, ref)
    if base == "N-MFP3":
        return parse_money_market(sgml.find_xml(docs) or "", ref)
    if base == "N-CEN":
        return parse_census(text, ref)
    if base in ("N-CSR", "N-CSRS"):
        return _typed_summary(text, ref, base, "shareholder_report")
    if base == "497K":
        return _typed_summary(text, ref, base, "prospectus")
    if base.startswith("N-PX"):
        return parse_npx(sgml.find_xml(docs, contains="proxyVoteTable") or "", ref)
    if base.startswith("ATS-N"):
        return parse_ats_notice(text, ref)
    if base == "ABS-EE":
        return parse_abs_assets(sgml.find_xml(docs, contains="<assets>") or "", ref)
    raise ValueError(f"no reader for form {form}")


def fund_filings(client: SecClient, symbol: str | int, *, as_of: datetime | date, form: str | Iterable[str] = FUND_FORMS, limit: int = 1) -> result.SecResult:
    """The latest fund filings of the forms given, visible at `as_of`, parsed (ED-012)."""
    listed = list_filings(client, symbol, as_of=as_of, form=form, limit=limit)
    if not listed.ok:
        return listed
    out: list[FundFiling] = []
    warnings = list(listed.warnings)
    for ref in listed.value:
        try:
            text = client.submission_text(ref.cik, ref.accession)
            out.append(parse_fund_text(text, ref))
        except TransportError as exc:
            return client.fail(exc, as_of)
        except (ValueError, KeyError, AttributeError) as exc:
            warnings.append(f"unreadable:{ref.accession}:{exc}")
    if not out:
        return result.no_data("no_readable_fund_filing", as_of, now=client.now, warnings=tuple(warnings))
    return result.ok(out, as_of, now=client.now, warnings=tuple(warnings))


# ---------------------------------------------------------------------------------------------------------------------
# Business development companies (ED-013): investments, non accrual, industry
# ---------------------------------------------------------------------------------------------------------------------
INVESTMENT_AXIS = "InvestmentIdentifierAxis"
INDUSTRY_AXIS = "IndustryAxis"
NON_ACCRUAL_MARKERS = ("nonaccrual", "non_accrual", "non-accrual")
BDC_TAGS = {"fair_value": "InvestmentOwnedAtFairValue", "cost": "InvestmentOwnedAtCost", "principal": "InvestmentOwnedBalancePrincipalAmount"}


@dataclass(frozen=True)
class BdcInvestment:
    identifier: str
    industry: str | None
    fair_value: float | None
    cost: float | None
    principal: float | None
    non_accrual: bool


def _local(text: str) -> str:
    return text.split(":")[-1]


def bdc_investments(instance: Instance) -> dict[str, Any]:
    """The schedule of investments of a BDC from its XBRL instance: one row per investment, with industry and non accrual status.

    An investment is on non accrual when its context carries a member whose name says so. Totals and the non accrual share
    (of cost and of fair value) are returned with the rows.
    """
    rows: dict[str, dict[str, Any]] = {}
    by_tag = {tag: name for name, tag in BDC_TAGS.items()}
    for fact in instance.facts:
        ctx = instance.context_of(fact)
        name = by_tag.get(_local(fact.concept))
        if ctx is None or name is None or fact.numeric is None:
            continue
        dims = {_local(axis): member for axis, member in ctx.dimensions}
        member = dims.get(INVESTMENT_AXIS)
        if member is None:
            continue
        row = rows.setdefault(_local(member), {"industry": None, "non_accrual": False})
        row[name] = fact.numeric
        if INDUSTRY_AXIS in dims:
            row["industry"] = _local(dims[INDUSTRY_AXIS]).removesuffix("Member")
        if any(m in (_local(axis) + _local(mem)).lower() for axis, mem in ctx.dimensions for m in NON_ACCRUAL_MARKERS):
            row["non_accrual"] = True
    investments = tuple(BdcInvestment(identifier=k.removesuffix("Member"), industry=v["industry"], fair_value=v.get("fair_value"), cost=v.get("cost"),
                                      principal=v.get("principal"), non_accrual=v["non_accrual"]) for k, v in sorted(rows.items()))
    cost = sum(i.cost or 0.0 for i in investments)
    fair = sum(i.fair_value or 0.0 for i in investments)
    bad = [i for i in investments if i.non_accrual]
    industries: dict[str, float] = {}
    for i in investments:
        industries[i.industry or "unknown"] = industries.get(i.industry or "unknown", 0.0) + (i.fair_value or 0.0)
    return {"investments": investments, "total_fair_value": fair, "total_cost": cost,
            "non_accrual_cost_share": (sum(i.cost or 0.0 for i in bad) / cost) if cost else None,
            "non_accrual_fair_value_share": (sum(i.fair_value or 0.0 for i in bad) / fair) if fair else None,
            "fair_value_by_industry": dict(sorted(industries.items()))}


def bdc_portfolio(client: SecClient, symbol: str | int, *, as_of: datetime | date, form: str = "10-K") -> result.SecResult:
    """The investments of a BDC from its latest report visible at `as_of` (ED-013)."""
    listed = list_filings(client, symbol, as_of=as_of, form=form, amendments=False, limit=1)
    if not listed.ok:
        return listed
    ref = listed.value[0]
    try:
        doc = sgml.find_document(sgml.parse_documents(client.submission_text(ref.cik, ref.accession)), "EX-101.INS")
    except TransportError as exc:
        return client.fail(exc, as_of)
    if doc is None or doc.xml is None:
        return result.no_data("no_xbrl_instance_document", as_of, now=client.now)
    summary = bdc_investments(parse_instance(doc.xml))
    if not summary["investments"]:
        return result.no_data("no_schedule_of_investments_in_instance", as_of, now=client.now)
    return result.ok({"ref": ref, **summary}, as_of, now=client.now)


# ---------------------------------------------------------------------------------------------------------------------
# Asset backed securities (ED-014): loan level data summary
# ---------------------------------------------------------------------------------------------------------------------
CATEGORICAL_FIELDS = ("zeroBalanceCode", "vehicleNewUsedCode", "interestRateTypeCode", "interestRateTypeCode")


def parse_abs_assets(xml: str, ref: FilingRef | None = None) -> FundFiling:
    """Loan level asset data (ABS-EE exhibit): pool size, balances, weighted rate, credit scores, delinquency and what left the pool.

    The reader is schema agnostic: every numeric child of an `assets` record is summarized.
    """
    if not xml:
        raise ValueError("no asset data document")
    root = etree.fromstring(xml.encode("utf-8"), etree.XMLParser(resolve_entities=False, no_network=True))
    records = [el for el in root.iter() if isinstance(el.tag, str) and etree.QName(el).localname == "assets"]
    if not records:
        raise ValueError("the document has no assets records")
    rows: list[dict[str, str]] = [{etree.QName(c).localname: (c.text or "").strip() for c in rec if isinstance(c.tag, str)} for rec in records]
    numeric: dict[str, list[float]] = {}
    for row in rows:
        for key, value in row.items():
            number = _f(value) if value != "" else None
            if number is not None and key not in CATEGORICAL_FIELDS:
                numeric.setdefault(key, []).append(number)
    stats = {k: {"count": len(v), "sum": sum(v), "mean": sum(v) / len(v), "min": min(v), "max": max(v)} for k, v in numeric.items()}
    balance_key = "reportingPeriodActualEndBalanceAmount"
    rate_key = "originalInterestRatePercentage"
    balances = [(_f(r.get(balance_key)) or 0.0, _f(r.get(rate_key))) for r in rows]
    weighted = sum(b * r for b, r in balances if r is not None)
    pool = sum(b for b, r in balances if r is not None)
    delinquent = [r for r in rows if (_f(r.get("currentDelinquencyStatus")) or 0) > 0]
    data = {"asset_count": len(rows), "fields": stats, "pool_balance": sum(b for b, _ in balances),
            "weighted_average_rate": (weighted / pool) if pool else None, "delinquent_count": len(delinquent),
            "delinquent_share": len(delinquent) / len(rows), "paid_off_count": sum(1 for r in rows if r.get("zeroBalanceCode")),
            "categories": {k: dict(Counter(r.get(k, "") for r in rows if r.get(k))) for k in ("vehicleNewUsedCode", "interestRateTypeCode", "zeroBalanceCode")}}
    return FundFiling(ref, "ABS-EE", "asset_data", None, _day(rows[0].get("reportingPeriodEndDate")), data)


# ---------------------------------------------------------------------------------------------------------------------
# ATS-N (ED-015)
# ---------------------------------------------------------------------------------------------------------------------
def ats_kind(form: str) -> str:
    parts = form.upper().replace("ATS-N-", "ATS-N/").split("/")
    suffix = parts[1] if len(parts) > 1 else ""
    if suffix == "W":
        return "withdrawal"
    if suffix == "UA":
        return "updating_amendment"
    if suffix in ("A", "CA", "MA", "OA"):
        return "amendment"
    return "initial"


def parse_ats_notice(text: str, ref: FilingRef | None = None) -> FundFiling:
    """An ATS-N notice of an alternative trading system: its name, MPID, operator and what the notice does (initial, amendment, withdrawal)."""
    header = sgml.parse_header(text)
    base = _typed_summary(text, ref, header.form or "ATS-N", "ats_notice")
    data = {**base.data, "notice_kind": ats_kind(header.form), "operator": header.company, "operator_cik": header.cik,
            "filed": header.filed, "withdrawn_accession": base.data.get("withdrawn_accession_number")}
    return FundFiling(ref, header.form, "ats_notice", base.name, header.filed, data)


# ---------------------------------------------------------------------------------------------------------------------
# N-PX votes and proxy statements (ED-016)
# ---------------------------------------------------------------------------------------------------------------------
def parse_npx(xml: str, ref: FilingRef | None = None) -> FundFiling:
    """How a fund voted: every vote with its record and the management recommendation, and a summary."""
    if not xml:
        raise ValueError("no proxy vote table")
    table = _lib.sec_parse_proxy_votes(xml)
    votes: list[dict[str, Any]] = []
    for row in table.proxy_tables:
        for record in row.vote_records:
            votes.append({"issuer": " ".join(str(row.issuer_name).split()), "cusip": row.cusip, "meeting_date": row.meeting_date,
                          "description": row.vote_description, "categories": [c.category_type for c in row.vote_categories],
                          "how_voted": record.how_voted, "shares_voted": record.shares_voted,
                          "management_recommendation": record.management_recommendation})
    against = [v for v in votes if v["how_voted"] in ("FOR", "AGAINST") and v["management_recommendation"] in ("FOR", "AGAINST")
               and v["how_voted"] != v["management_recommendation"]]
    summary = {"vote_count": len(votes), "by_vote": dict(Counter(v["how_voted"] for v in votes)), "against_management": len(against),
               "votes": votes}
    return FundFiling(ref, "N-PX", "proxy_votes", None, None, summary)


_MEETING = re.compile(r"meeting[^.]{0,120}?held on\s+([A-Z][a-z]+ \d{1,2}, \d{4})", re.I)
_RECORD = re.compile(r"record[^.]{0,80}?(?:on|as of)\s+([A-Z][a-z]+ \d{1,2}, \d{4})", re.I)
_PROPOSAL = re.compile(r"^proposal\s+(?:no\.\s*)?(\d+)\s*[:.\-]\s*(.+)$", re.I)
_MONEY = re.compile(r"^\$?\s*([\d,]+(?:\.\d+)?)$")
_LONG = re.compile(r"([A-Z][a-z]+) (\d{1,2}), (\d{4})")
_MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}


@dataclass(frozen=True)
class ProxyStatementInfo:
    meeting_date: date | None
    record_date: date | None
    proposals: tuple[tuple[int, str], ...]
    say_on_pay: bool
    auditor_ratification: bool
    contested: bool
    executive_compensation: tuple[dict[str, Any], ...]


def _long_date(text: str | None) -> date | None:
    match = _LONG.search(text or "")
    if not match or match.group(1) not in _MONTHS:
        return None
    return date(int(match.group(3)), _MONTHS[match.group(1)], int(match.group(2)))


def parse_proxy_statement(html: str) -> ProxyStatementInfo:
    """What a proxy statement asks of shareholders: meeting and record dates, proposals, say on pay, auditor ratification, contest
    signals and the rows of the summary compensation table."""
    doc = parse_document(html)
    flat = " ".join(doc.lines)
    meeting = _MEETING.search(flat)
    record = _RECORD.search(flat)
    proposals = tuple((int(m.group(1)), m.group(2).strip()) for line in doc.lines if (m := _PROPOSAL.match(line)))
    lowered = flat.lower()
    comp: list[dict[str, Any]] = []
    for table in doc.tables:
        header = [c.lower() for c in table[0]]
        if "salary" in header and "total" in header:
            for row in table[1:]:
                cells = dict(zip(header, row))
                amounts = {k: float(m.group(1).replace(",", "")) for k, v in cells.items() if k in ("salary", "bonus", "total") and (m := _MONEY.match(v))}
                comp.append({"name_and_position": row[0], "year": int(cells["year"]) if cells.get("year", "").isdigit() else None, **amounts})
    return ProxyStatementInfo(meeting_date=_long_date(meeting.group(1)) if meeting else None, record_date=_long_date(record.group(1)) if record else None,
                              proposals=proposals, say_on_pay="say on pay" in lowered or "say-on-pay" in lowered,
                              auditor_ratification="ratification of" in lowered and "accounting firm" in lowered,
                              contested=any(k in lowered for k in ("dissident", "universal proxy card", "proxy contest", "contested election")),
                              executive_compensation=tuple(comp))


def proxy_statement(client: SecClient, symbol: str | int, *, as_of: datetime | date) -> result.SecResult:
    """The latest definitive proxy statement visible at `as_of`, read (ED-016)."""
    listed = list_filings(client, symbol, as_of=as_of, form=("DEF 14A", "DEFA14A"), amendments=False, limit=1)
    if not listed.ok:
        return listed
    ref = listed.value[0]
    try:
        docs = sgml.parse_documents(client.submission_text(ref.cik, ref.accession))
    except TransportError as exc:
        return client.fail(exc, as_of)
    main = next((d for d in docs if d.is_html), None)
    if main is None:
        return result.no_data("no_html_document", as_of, now=client.now)
    return result.ok({"ref": ref, "info": parse_proxy_statement(main.text)}, as_of, now=client.now, warnings=listed.warnings)


def proxy_votes(client: SecClient, fund: str | int, *, as_of: datetime | date) -> result.SecResult:
    """The latest N-PX of a fund visible at `as_of` (ED-016)."""
    return fund_filings(client, fund, as_of=as_of, form="N-PX", limit=1)

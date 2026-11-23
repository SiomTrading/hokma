"""Financial statements, ratios, growth and trailing twelve months from the company facts, point in time.

Rows: ED-025 (never mix periods between the operands of a ratio), ED-026 (TTM), ED-028 (statement getters), ED-029 (entity,
facts, ratios, growth).

Every value is computed from facts filed on or before `as_of`. A gap is never filled: a missing operand or a period
mismatch gives None for that figure and a warning that says why.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from hokma.data import _lib
from hokma.data.sec import result
from hokma.data.sec.client import SecClient
from hokma.data.sec.filings import _cik
from hokma.data.sec.plain import to_plain
from hokma.data.sec.transport import TransportError

STALE_DAYS = 185                      # a window that ends this long before as_of is not today's number
ANNUAL_DAYS = (300, 400)
QUARTER_DAYS = (60, 115)

# field -> (tags in order of preference, is_duration). The tags of a field are tried in order; values are never added.
FIELDS: dict[str, tuple[tuple[str, ...], bool]] = {
    "revenue": (("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"), True),
    "net_income": (("NetIncomeLoss",), True),
    "gross_profit": (("GrossProfit",), True),
    "operating_income": (("OperatingIncomeLoss",), True),
    "operating_cash_flow": (("NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"), True),
    "capex": (("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"), True),
    "total_assets": (("Assets",), False),
    "current_assets": (("AssetsCurrent",), False),
    "cash": (("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"), False),
    "total_liabilities": (("Liabilities",), False),
    "current_liabilities": (("LiabilitiesCurrent",), False),
    "shareholders_equity": (("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"), False),
}
STANDARD = {"revenue", "net_income", "gross_profit", "operating_income", "total_assets", "total_liabilities", "shareholders_equity"}

# ratio -> (numerator, denominator). Both operands must describe the same period.
RATIOS: dict[str, tuple[str, str]] = {
    "gross_margin": ("gross_profit", "revenue"),
    "operating_margin": ("operating_income", "revenue"),
    "net_margin": ("net_income", "revenue"),
    "return_on_assets": ("net_income", "total_assets"),
    "return_on_equity": ("net_income", "shareholders_equity"),
    "liabilities_to_assets": ("total_liabilities", "total_assets"),
    "current_ratio": ("current_assets", "current_liabilities"),
    "capex_to_revenue": ("capex", "revenue"),
    "cash_conversion": ("operating_cash_flow", "net_income"),
}


@dataclass(frozen=True)
class Operand:
    field: str
    value: float | None
    unit: str = "USD"
    period_end: date | None = None
    period_start: date | None = None     # None for balance sheet (instant) figures
    tag: str | None = None


@dataclass(frozen=True)
class FinancialSnapshot:
    period: str                                           # `2023-FY` or `2024-Q1`
    period_end: date | None
    operands: dict[str, Operand]
    free_cash_flow: float | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Ratios:
    period: str
    ratios: dict[str, float | None]
    warnings: tuple[str, ...] = ()
    operands: dict[str, Operand] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------------------------------
# Point in time facts
# ---------------------------------------------------------------------------------------------------------------------
def facts_as_of(payload: dict[str, Any], as_of: datetime | date) -> dict[str, Any]:
    """The company facts payload with every fact filed after `as_of` removed."""
    day = as_of.date() if isinstance(as_of, datetime) else as_of
    kept: dict[str, Any] = {"cik": payload.get("cik"), "entityName": payload.get("entityName"), "facts": {}}
    for taxonomy, concepts in (payload.get("facts") or {}).items():
        out_concepts: dict[str, Any] = {}
        for tag, body in concepts.items():
            units: dict[str, list[dict[str, Any]]] = {}
            for unit, entries in (body.get("units") or {}).items():
                rows = [e for e in entries if e.get("filed") and date.fromisoformat(e["filed"]) <= day]
                if rows:
                    units[unit] = rows
            if units:
                out_concepts[tag] = {**{k: v for k, v in body.items() if k != "units"}, "units": units}
        if out_concepts:
            kept["facts"][taxonomy] = out_concepts
    return kept


def _entity(client: SecClient, symbol: str | int, as_of: datetime | date) -> tuple[Any, result.SecResult | None]:
    """(entity facts visible at as_of, None) or (None, the result to return)."""
    cik, bad = _cik(client, symbol, as_of)
    if bad is not None:
        return None, bad
    day = as_of.date() if isinstance(as_of, datetime) else as_of
    key = ("entity_facts", cik, day)
    if key not in client.cache:
        try:
            payload = client.facts(cik)
        except TransportError as exc:
            if exc.kind == "not_found":
                return None, result.no_data("no_company_facts", as_of, now=client.now)
            return None, client.fail(exc, as_of)
        visible = facts_as_of(payload, as_of)
        client.cache[key] = _lib.sec_entity_facts(visible) if visible["facts"] else None
    entity = client.cache[key]
    if entity is None:
        return None, result.no_data("no_facts_filed_by_as_of", as_of, now=client.now)
    return entity, None


# ---------------------------------------------------------------------------------------------------------------------
# Operands and the same period rule (ED-025)
# ---------------------------------------------------------------------------------------------------------------------
def _days(fact: Any) -> int | None:
    if fact.period_start is None or fact.period_end is None:
        return None
    return (fact.period_end - fact.period_start).days


def _span_ok(fact: Any, is_duration: bool, period: str) -> bool:
    days = _days(fact)
    if not is_duration:
        return days is None
    if days is None:
        return False
    low, high = ANNUAL_DAYS if period.upper().endswith("FY") else QUARTER_DAYS
    return low <= days <= high


def latest_annual_period(entity: Any) -> str | None:
    years = {f.fiscal_year for f in entity.get_all_facts() if f.fiscal_period == "FY" and f.form_type.upper().startswith(("10-K", "20-F", "40-F"))}
    return f"{max(years)}-FY" if years else None


def get_operand(entity: Any, name: str, period: str) -> Operand:
    """One statement field for one fiscal period. The period of the fact that backs it is checked, not assumed."""
    tags, is_duration = FIELDS[name]
    backing = None
    for tag in tags:
        fact = _lib.sec_fact(entity, f"us-gaap:{tag}", period)
        if fact is not None and fact.numeric_value is not None and _span_ok(fact, is_duration, period):
            backing = (tag, fact)
            break
    value: float | None = None
    if name in STANDARD:
        value = _lib.sec_standard_value(entity, name, period, annual=period.upper().endswith("FY"))
    if value is None and backing is not None:
        value = float(backing[1].numeric_value)
    if backing is None:
        return Operand(name, value, period_end=None)
    tag, fact = backing
    return Operand(name, value, unit=str(fact.unit or "USD"), period_end=fact.period_end, period_start=fact.period_start, tag=tag)


def same_period(a: Operand, b: Operand) -> bool:
    """True when both figures describe one period: same end date and, when both are durations, the same start date."""
    if a.period_end is None or b.period_end is None or a.period_end != b.period_end:
        return False
    if a.period_start is not None and b.period_start is not None:
        return a.period_start == b.period_start
    return True


def ratio_of(numerator: Operand, denominator: Operand) -> tuple[float | None, str | None]:
    """numerator / denominator, or None with the reason. Never mixes periods."""
    if numerator.value is None:
        return None, f"missing_operand:{numerator.field}"
    if denominator.value is None:
        return None, f"missing_operand:{denominator.field}"
    if not same_period(numerator, denominator):
        return None, f"period_mismatch:{numerator.field}/{denominator.field}"
    if denominator.value == 0:
        return None, f"zero_denominator:{denominator.field}"
    return numerator.value / denominator.value, None


def _free_cash_flow(ocf: Operand, capex: Operand) -> tuple[float | None, str | None]:
    if ocf.value is None or capex.value is None:
        return None, "free_cash_flow:missing_operand"
    if not same_period(ocf, capex):
        return None, "free_cash_flow:period_mismatch"
    return ocf.value - abs(capex.value), None


# ---------------------------------------------------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------------------------------------------------
def statement(client: SecClient, symbol: str | int, *, as_of: datetime | date, period: str | None = None) -> result.SecResult:
    """The statement fields of one fiscal period (default: the latest annual period visible at `as_of`) (ED-028)."""
    entity, bad = _entity(client, symbol, as_of)
    if bad is not None:
        return bad
    chosen = period or latest_annual_period(entity)
    if chosen is None:
        return result.no_data("no_annual_period_visible", as_of, now=client.now)
    operands = {name: get_operand(entity, name, chosen) for name in FIELDS}
    warnings = [f"missing:{n}" for n, o in operands.items() if o.value is None]
    fcf, why = _free_cash_flow(operands["operating_cash_flow"], operands["capex"])
    if why:
        warnings.append(why)
    ends = {o.period_end for o in operands.values() if o.period_end is not None}
    if not any(o.value is not None for o in operands.values()):
        return result.no_data(f"no_values_for_period:{chosen}", as_of, now=client.now)
    end = max(ends) if ends else None
    return result.ok(FinancialSnapshot(chosen, end, operands, fcf, tuple(warnings)), as_of, now=client.now, warnings=tuple(warnings))


def calculate_ratios(client: SecClient, symbol: str | int, *, as_of: datetime | date, period: str | None = None) -> result.SecResult:
    """Margins and returns of one fiscal period. A ratio whose operands do not share the period is None with a warning (ED-025, ED-029)."""
    entity, bad = _entity(client, symbol, as_of)
    if bad is not None:
        return bad
    chosen = period or latest_annual_period(entity)
    if chosen is None:
        return result.no_data("no_annual_period_visible", as_of, now=client.now)
    names = {n for pair in RATIOS.values() for n in pair}
    operands = {n: get_operand(entity, n, chosen) for n in names}
    ratios: dict[str, float | None] = {}
    warnings: list[str] = []
    for ratio, (num, den) in RATIOS.items():
        value, why = ratio_of(operands[num], operands[den])
        ratios[ratio] = value
        if why:
            warnings.append(f"{ratio}:{why}")
    if all(v is None for v in ratios.values()):
        return result.no_data(f"no_ratio_computable_for_period:{chosen}", as_of, now=client.now, warnings=tuple(warnings))
    return result.ok(Ratios(chosen, ratios, tuple(warnings), operands), as_of, now=client.now, warnings=tuple(warnings))


def calculate_growth(client: SecClient, symbol: str | int, field_name: str, *, as_of: datetime | date, years: int = 4) -> result.SecResult:
    """Year over year growth of an annual figure, oldest first. Growth is None when the previous fiscal year is missing (ED-029)."""
    if field_name not in FIELDS:
        raise ValueError(f"unknown field {field_name!r}; expected one of {sorted(FIELDS)}")
    entity, bad = _entity(client, symbol, as_of)
    if bad is not None:
        return bad
    tags, _ = FIELDS[field_name]
    fiscal_years = sorted({f.fiscal_year for f in entity.get_all_facts()
                           if f.fiscal_period == "FY" and f.concept.split(":")[-1] in tags and f.form_type.upper().startswith(("10-K", "20-F", "40-F"))})
    fiscal_years = fiscal_years[-years:]
    rows: list[dict[str, Any]] = []
    previous: tuple[int, float] | None = None
    for fy in fiscal_years:
        operand = get_operand(entity, field_name, f"{fy}-FY")
        if operand.value is None:
            continue
        growth = None
        if previous is not None and previous[0] == fy - 1 and previous[1] != 0:
            growth = operand.value / previous[1] - 1.0
        rows.append({"fiscal_year": fy, "period_end": operand.period_end, "value": operand.value, "growth": growth})
        previous = (fy, operand.value)
    if not rows:
        return result.no_data(f"no_annual_values_for:{field_name}", as_of, now=client.now)
    return result.ok(rows, as_of, now=client.now)


def entity_summary(client: SecClient, symbol: str | int, *, as_of: datetime | date) -> result.SecResult:
    """Entity name, CIK and what the facts cover at `as_of` (ED-029)."""
    entity, bad = _entity(client, symbol, as_of)
    if bad is not None:
        return bad
    facts = entity.get_all_facts()
    forms = sorted({f.form_type for f in facts if f.form_type})
    filed = [f.filing_date for f in facts if f.filing_date]
    return result.ok({"cik": int(entity.cik), "name": entity.name, "fact_count": len(facts),
                      "concept_count": len({f.concept for f in facts}), "forms": forms,
                      "latest_filing_date": max(filed).isoformat() if filed else None}, as_of, now=client.now)


def concept_facts(client: SecClient, symbol: str | int, concept: str, *, as_of: datetime | date) -> result.SecResult:
    """Every fact of a concept filed by `as_of` as plain rows. `Revenues` and `us-gaap:Revenues` are the same concept (ED-029)."""
    entity, bad = _entity(client, symbol, as_of)
    if bad is not None:
        return bad
    name = concept if ":" in concept else f"us-gaap:{concept}"
    facts = _lib.sec_concept_facts(entity, name)
    if not facts:
        return result.no_data(f"concept_not_reported:{name}", as_of, now=client.now)
    rows = [{"concept": f.concept, "value": f.numeric_value, "unit": f.unit, "period_start": f.period_start, "period_end": f.period_end,
             "fiscal_year": f.fiscal_year, "fiscal_period": f.fiscal_period, "form": f.form_type, "filed": f.filing_date,
             "accession": f.accession} for f in facts]
    rows.sort(key=lambda r: (r["period_end"], r["filed"]))
    return result.ok(to_plain(rows), as_of, now=client.now)


def ttm(client: SecClient, symbol: str | int, concept: str, *, as_of: datetime | date) -> result.SecResult:
    """Trailing twelve months of a concept at `as_of`: four consecutive quarters, splits applied to per share figures.

    No gap free window, or a window that ends more than 185 days before `as_of`, is NO_DATA: the gap is never filled (ED-026).
    """
    entity, bad = _entity(client, symbol, as_of)
    if bad is not None:
        return bad
    day = as_of.date() if isinstance(as_of, datetime) else as_of
    name = concept if ":" in concept else f"us-gaap:{concept}"
    facts = _lib.sec_concept_facts(entity, name)
    if not facts:
        return result.no_data(f"concept_not_reported:{name}", as_of, now=client.now)
    per_share = "share" in str(facts[0].unit or "").lower()
    if per_share:
        facts = facts + _split_facts(entity)
    try:
        data = _lib.sec_ttm(facts, as_of=day, filed_by=day, split_adjust=per_share)
    except _lib.SecTtmError:
        return result.no_data("no_gap_free_four_quarter_window", as_of, now=client.now)
    lag = (day - data["window_end"]).days
    if lag > STALE_DAYS:
        return result.no_data(f"ttm_window_ends_{lag}_days_before_as_of", as_of, now=client.now)
    warnings = (data["warning"],) if data["warning"] else ()
    return result.ok(to_plain({k: data[k] for k in ("value", "unit", "window_end", "periods", "has_calculated_q4", "splits", "concept")}),
                     as_of, now=client.now, warnings=warnings)


def _split_facts(entity: Any) -> list:
    return [f for f in entity.get_all_facts() if "StockSplitConversionRatio" in f.concept]

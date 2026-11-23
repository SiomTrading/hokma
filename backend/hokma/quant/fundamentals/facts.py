"""Facts of the quarter of an event: Q4 derived as the year minus three quarters, filing lag warnings (DTS-10), the fiscal quarter
read from the name of the call and valuation multiples labelled LTM or NTM (AFS-028).

Rule of AFS-028: the fiscal quarter is never inferred from a calendar date. If the name of the call does not say it, there is no
quarter. Every valuation multiple carries its basis, and NTM is the sum of the next four quarterly estimates, nothing else.
"""
from __future__ import annotations

import math
import re
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _common as c

QUARTER_DAYS = (70, 120)
ANNUAL_DAYS = (330, 420)
LATE_FILING_DAYS = 60            # a quarterly report filed later than this after the period ended is flagged
STALE_DAYS = 185
FACT_COLUMNS = ("period_start", "period_end", "value", "filed")


def _kind(start: pd.Timestamp, end: pd.Timestamp) -> str:
    days = (end - start).days
    if QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1]:
        return "quarter"
    if ANNUAL_DAYS[0] <= days <= ANNUAL_DAYS[1]:
        return "annual"
    return "other"


@registry.register("fundamentals.quarter_facts", module="fundamentals", piece_ids=("DTS-10",),
                   inputs={"facts": {"type": "dataframe", "required": True}, "as_of": {"type": "date", "required": True}},
                   summary="Quarterly figures of the latest fiscal year filed by the cut-off, with Q4 derived as the year minus three quarters (marked derived) and warnings for late filings and a stale latest period.")
def quarter_facts(facts: Any, as_of: Any):
    name, pieces, method = "fundamentals.quarter_facts", ("DTS-10",), "duration classes; Q4 = FY - (Q1 + Q2 + Q3)"
    if not isinstance(facts, pd.DataFrame):
        raise BadInput(f"facts: expected a DataFrame, got {type(facts).__name__}")
    missing = [col for col in FACT_COLUMNS if col not in facts.columns]
    if missing:
        raise BadInput(f"facts: missing column(s) {missing}")
    try:
        cut = pd.Timestamp(as_of)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"as_of must be a date, got {as_of!r}") from exc
    frame = facts[list(FACT_COLUMNS)].copy()
    for col in ("period_start", "period_end", "filed"):
        frame[col] = pd.to_datetime(frame[col], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    usable = frame.dropna(subset=["period_start", "period_end", "filed", "value"])
    warns: list[str] = []
    if len(usable) != len(frame):
        warns.append(f"ignored {len(frame) - len(usable)} fact(s) without dates or value")
    visible = usable[usable["filed"] <= cut]
    if len(visible) != len(usable):
        warns.append(f"excluded {len(usable) - len(visible)} fact(s) filed after as_of")
    if visible.empty:
        return c.empty(name, "usd", method, tuple(warns) + ("no fact filed by as_of",), piece_ids=pieces)
    # Keep the latest filing of each period (a restatement replaces the first figure only when it was filed by the cut-off).
    visible = visible.sort_values("filed").drop_duplicates(["period_start", "period_end"], keep="last")
    visible = visible.assign(kind=[_kind(s, e) for s, e in zip(visible["period_start"], visible["period_end"])])
    annual = visible[visible["kind"] == "annual"].sort_values("period_end")
    quarters = visible[visible["kind"] == "quarter"].sort_values("period_end")
    last_end = visible["period_end"].max()
    lag = (cut - last_end).days
    if lag > STALE_DAYS:
        warns.append(f"stale: the latest period ended {lag} days before as_of")
    if quarters.empty and annual.empty:
        return c.empty(name, "usd", method, tuple(warns) + ("no quarterly or annual figure",), piece_ids=pieces)
    labelled: dict[pd.Timestamp, str] = {}
    rows: list[dict[str, Any]] = []
    for _, year in annual.iterrows():
        start, end = year["period_start"], year["period_end"]
        inside = quarters[(quarters["period_start"] >= start - pd.Timedelta(days=5)) & (quarters["period_end"] <= end)].sort_values("period_end")
        first_three = inside.head(3)
        consecutive = len(first_three) == 3 and abs((first_three.iloc[0]["period_start"] - start).days) <= 5 and all(
            0 < (first_three.iloc[i + 1]["period_start"] - first_three.iloc[i]["period_end"]).days <= 5 for i in range(2))
        if consecutive and not (inside["period_end"] == end).any():
            for i, (_, q) in enumerate(first_three.iterrows()):
                labelled[q["period_end"]] = f"Q{i + 1}"
            q4 = float(year["value"]) - float(first_three["value"].sum())
            rows.append({"fiscal_period": "Q4", "period_end": end.date().isoformat(), "value": q4, "derived": True,
                         "filed": year["filed"].date().isoformat(), "filing_lag_days": int((year["filed"] - end).days)})
        else:
            absent = 3 - len(first_three)
            warns.append(f"q4_not_derivable:{end.date().isoformat()}: " + (f"{absent} of the three quarters are missing" if absent > 0 else "the quarters are not consecutive"))
    for _, q in quarters.iterrows():
        rows.append({"fiscal_period": labelled.get(q["period_end"]), "period_end": q["period_end"].date().isoformat(), "value": float(q["value"]),
                     "derived": False, "filed": q["filed"].date().isoformat(), "filing_lag_days": int((q["filed"] - q["period_end"]).days)})
    rows = sorted(rows, key=lambda r: r["period_end"])[-8:]
    for r in rows:
        if not r["derived"] and r["filing_lag_days"] > LATE_FILING_DAYS:
            warns.append(f"late_filing:{r['fiscal_period'] or r['period_end']}:{r['filing_lag_days']}d")
    value = {"quarters": rows, "latest_period_end": last_end.date().isoformat(), "days_since_latest_period": int(lag)}
    return c.build(name, value, unit="usd", method=method, params={"late_filing_days": LATE_FILING_DAYS, "stale_days": STALE_DAYS}, piece_ids=pieces,
                   inputs=(facts.to_csv(index=False),), warnings=tuple(dict.fromkeys(warns)), as_of=cut)


# ---------------------------------------------------------------------------------------------------------------------
# AFS-028: fiscal quarter from the text of the call name, LTM and NTM bases
# ---------------------------------------------------------------------------------------------------------------------
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_YEAR = r"(?:fy\s*)?'?(\d{4}|\d{2})"
_PATTERNS = (
    (re.compile(rf"\bq([1-4])\s*{_YEAR}\b", re.I), 1, 2),
    (re.compile(rf"\b([1-4])q\s*{_YEAR}\b", re.I), 1, 2),
    (re.compile(r"\bfy\s*'?(\d{4}|\d{2})\s*q([1-4])\b", re.I), 2, 1),
    (re.compile(r"\b(first|second|third|fourth)\s+quarter\s+(?:of\s+)?(?:fiscal\s+)?(?:year\s+)?(?:fy\s*)?'?(\d{4}|\d{2})\b", re.I), 1, 2),
)
MAX_CALL_NAME = 200


def _year(text: str) -> int:
    n = int(text)
    return n if n >= 100 else 2000 + n


@registry.register("fundamentals.fiscal_quarter", module="fundamentals", piece_ids=("AFS-028",),
                   inputs={"call_name": {"type": "str", "required": True}, "calendar_date": {"type": "str", "required": False, "default": None}},
                   summary="Fiscal quarter and year read from the text of the name of an earnings call (Q3 FY2024, 3Q24, Third Quarter 2024). It never infers the quarter from a calendar date: if the name does not say it, there is no value.")
def fiscal_quarter(call_name: str, calendar_date: str | None = None):
    name, pieces, method = "fundamentals.fiscal_quarter", ("AFS-028",), "read from the name of the call, never from the calendar"
    if not isinstance(call_name, str):
        raise BadInput(f"call_name must be text, got {type(call_name).__name__}")
    if len(call_name) > MAX_CALL_NAME:
        raise BadInput(f"call_name is longer than {MAX_CALL_NAME} characters")
    found: set[tuple[int, int]] = set()
    for pattern, q_group, y_group in _PATTERNS:
        for m in pattern.finditer(call_name):
            q = m.group(q_group)
            quarter = _ORDINALS[q.lower()] if q.lower() in _ORDINALS else int(q)
            found.add((_year(m.group(y_group)), quarter))
    notes = ("calendar_date_ignored",) if calendar_date is not None else ()
    if not found:
        return c.empty(name, "label", method, notes + ("fiscal_quarter_not_in_call_name",), piece_ids=pieces)
    if len(found) > 1:
        return c.empty(name, "label", method, notes + ("ambiguous_call_name",), piece_ids=pieces)
    (year, quarter), = found
    return c.build(name, {"fiscal_year": year, "fiscal_quarter": quarter, "label": f"Q{quarter} FY{year}", "source": "call_name"},
                   unit="label", method=method, piece_ids=pieces, inputs=(call_name,), warnings=notes)


BASES = ("LTM", "NTM")


@registry.register("fundamentals.valuation_multiple", module="fundamentals", piece_ids=("AFS-028",),
                   inputs={"price": {"type": "float", "required": True}, "quarterly_values": {"type": "list[float]", "required": True},
                           "basis": {"type": "str", "required": True}, "kind": {"type": "str", "required": False, "default": "price_to_earnings"}},
                   summary="Valuation multiple with its basis written on it. LTM is the sum of the last four reported quarters; NTM is the sum of the next four quarterly consensus estimates, oldest first. Anything else is refused.")
def valuation_multiple(price: float, quarterly_values: list[float], basis: str, kind: str = "price_to_earnings"):
    name, pieces = "fundamentals.valuation_multiple", ("AFS-028",)
    if basis not in BASES:
        raise BadInput(f"basis must be one of {list(BASES)}: a multiple without LTM or NTM is not accepted")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(float(price)):
        raise BadInput("price must be a finite number")
    if not isinstance(quarterly_values, (list, tuple)) or len(quarterly_values) > 40:
        raise BadInput("quarterly_values must be a list of at most 40 numbers")
    if not isinstance(kind, str) or not re.fullmatch(r"[a-z_]{1,40}", kind):
        raise BadInput("kind must be a lowercase label such as price_to_earnings")
    method = f"{basis}: " + ("sum of the last four reported quarters" if basis == "LTM" else "sum of the next four quarterly estimates")
    params = {"basis": basis, "kind": kind}
    values = [float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else float("nan") for v in quarterly_values]
    if any(not math.isfinite(v) for v in values):
        return c.empty(name, "ratio", method, "quarterly_values must be finite numbers", params=params, piece_ids=pieces)
    if basis == "NTM":
        if len(values) != 4:
            return c.empty(name, "ratio", method, f"ntm_needs_exactly_four_quarterly_estimates, got {len(values)}", params=params, piece_ids=pieces)
        window = values
    else:
        if len(values) < 4:
            return c.empty(name, "ratio", method, f"ltm_needs_four_reported_quarters, got {len(values)}", params=params, piece_ids=pieces)
        window = values[-4:]
    total = float(sum(window))
    if total <= 0:
        return c.empty(name, "ratio", method, "non_positive_denominator", params=params, piece_ids=pieces)
    value = {"multiple": float(price) / total, "basis": basis, "label": f"{kind} {basis}", "denominator": total, "quarters_used": window}
    return c.build(name, value, unit="ratio", method=method, params=params, piece_ids=pieces, inputs=(list(values), float(price)))

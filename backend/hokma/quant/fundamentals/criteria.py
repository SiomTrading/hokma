"""Criteria of the eight investor profiles, computed from fundamentals (INV-001 to INV-007, SHF-004, SHF-006 to SHF-009).

A profile reasons over criteria that code has already computed. Each function here returns the criteria in the shape of the Signal
contract (name, value, threshold, passed, source) and a lean in {-1, 0, +1} that follows the written rules of the profile. The lean is
a view, never a size. When a required input is missing the result is empty and its warnings are `data_missing:<field>`: the profile
abstains and nothing is invented. Thresholds that came from a published source are constants below; the ones that no source gives are
parameters, and without them the profile stays neutral and says so.

Written for the desk: no sizing, no exits, no timing. Conventions: docs/quant/fundamentals-conventions.md.
"""
from __future__ import annotations

import csv
import math
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _common as c
from hokma.quant.fundamentals import _lib

# ---- published thresholds (source of each in the docs) ----
ROE_AVERAGE_MIN = 0.15           # SHF-006: ROE sustained above 15 percent
ROE_FLOOR_MIN = 0.10             # INV-001: no period under 10 percent
DEBT_TO_EQUITY_MAX = 0.5         # SHF-006
SAFETY_MARGIN = 0.25             # SHF-004: margin of safety of 25 percent on owner earnings
GAP_MIN = 0.30                   # SHF-008: value gap above 30 percent
PEG_BULLISH_BELOW = 1.0          # SHF-009
PEG_BEARISH_ABOVE = 2.0          # SHF-009
CLASSIC_DEFENSIVE = {"pe_max": 15.0, "pb_max": 1.5, "pe_x_pb_max": 22.5, "current_ratio_strict": 2.0, "current_ratio_minimum": 1.5, "debt_to_net_current_assets_max": 1.0}
FAST_GROWTH = 0.20               # INV-004: fast growers grow EPS 20 percent or more per year
STALWART_GROWTH = 0.10           # INV-004: stalwarts 10 to 12 percent; the band up to 20 percent is assigned to them (declared in the result)
RUTH_CONFIDENCE_CAP = 69         # INV-007: TAM, first mover and team are unknown
LIMITED_DEPTH_CAP = 89           # INV-001, INV-002, INV-005, INV-006: the series is shorter than ten periods
# ---- defaults of values that are calibration parameters (no source; printed in the result) ----
DEFAULTS = {"discount_rate": 0.10, "growth_rate": 0.03, "min_periods": 4, "max_age_days": 150}
UNCALIBRATED = {"margin_stability_max": 0.05, "roe_drop_max": 0.05, "debt_rise_max": 0.10, "perfection_ratio": 2.0, "eps_jump_max": 1.0,
                "earnings_quality_min": 0.8, "earnings_quality_failing": 0.5, "revenue_accel_min": 0.0, "priced_in_max": 0.25, "recognized_move_max": 0.20}
THEME_NAMES = ("artificial_intelligence", "genomics", "robotics_automation", "energy_storage", "financial_technology", "blockchain")
REGIMES = ("favorable", "neutral", "hostile")
MIN_BARS = 200

PROFILE_PIECES = {
    "owner_earnings": ("SHF-004",), "quality_owner": ("INV-001", "SHF-006"), "inversion_veto": ("INV-002",), "safety_margin": ("INV-003",),
    "fair_growth": ("INV-004", "SHF-009"), "inflection": ("INV-005",), "contrarian_gap": ("INV-006", "SHF-008"), "disruption": ("INV-007", "SHF-007"),
}
PROFILE_CODES = {"quality_owner": "A16", "inversion_veto": "A17", "safety_margin": "A18", "fair_growth": "A19", "inflection": "A20",
                 "contrarian_gap": "A21", "disruption": "A22"}


# ---------------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------------
def crit(name: str, value: float | None, threshold: float | None, passed: bool | None, source: str) -> dict[str, Any]:
    """One criterion in the shape of the Signal contract. A criterion without a value has no verdict."""
    ok = value is not None and math.isfinite(float(value))
    return {"name": name, "value": float(value) if ok else None, "threshold": threshold, "passed": (None if not ok else passed), "source": source}


def peg_points(peg: float | None) -> int:
    """+1 below 1, -1 above 2, 0 on the edges and in between (SHF-009); None when the PEG does not exist."""
    if peg is None or not math.isfinite(peg):
        return 0
    if peg < PEG_BULLISH_BELOW:
        return 1
    if peg > PEG_BEARISH_ABOVE:
        return -1
    return 0


def _whole(label: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or not low <= int(value) <= high:
        raise BadInput(f"{label} must be a whole number between {low} and {high}")
    return int(value)


def _rate(label: str, value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not low <= float(value) <= high:
        raise BadInput(f"{label} must be a number between {low} and {high}")
    return float(value)


def _gate(items: c.Items, min_periods: int, max_age_days: int) -> list[str]:
    out: list[str] = []
    n = len(items.frame)
    if n < min_periods:
        out.append(f"data_missing:periods_at_least_{min_periods}_have_{n}")
    if items.filed is not None and items.cut is not None and items.filed.notna().any():
        age = (items.cut - items.filed.max()).days
        if age > max_age_days:
            out.append(f"data_missing:filing_newer_than_{max_age_days}_days_is_{age}")
    return out


def _last(series: pd.Series | None) -> float | None:
    return c.latest_value(series)


def _first(series: pd.Series | None) -> float | None:
    if series is None:
        return None
    cleaned = series.replace([np.inf, -np.inf], np.nan).dropna()
    return float(cleaned.iloc[0]) if len(cleaned) else None


def _change(series: pd.Series | None) -> float | None:
    a, b = _first(series), _last(series)
    if a is None or b is None or series is None or series.replace([np.inf, -np.inf], np.nan).dropna().size < 2:
        return None
    return b - a


def _ratio(num: pd.Series | None, den: pd.Series | None, *, positive_den: bool = True) -> pd.Series | None:
    if num is None or den is None:
        return None
    return num / (den.where(den > 0) if positive_den else den)


def _roe(items: c.Items) -> pd.Series | None:
    return items.call(_lib.ft_profitability.get_return_on_equity)[0]


def _op_margin(items: c.Items) -> pd.Series | None:
    return _ratio(items.get("operating_income"), items.get("revenue"))


def _debt_to_equity(items: c.Items) -> pd.Series | None:
    return _ratio(items.get("total_debt"), items.get("total_equity"))


def _fcf_per_share(items: c.Items) -> pd.Series | None:
    return _ratio(items.get("free_cash_flow"), items.get("shares_outstanding"))


def _net_margin(items: c.Items) -> pd.Series | None:
    return _ratio(items.get("net_income"), items.get("revenue"))


def _growth(series: pd.Series | None) -> pd.Series | None:
    if series is None:
        return None
    prior = series.shift(1)
    return series / prior.where(prior > 0) - 1.0


def owner_earnings_core(items: c.Items, discount_rate: float, growth_rate: float, safety_margin: float) -> dict[str, Any] | None:
    """Owner earnings = net income + depreciation and amortization - abs(capex) - change in net working capital (current assets - current
    liabilities). Value = owner earnings / (discount rate - growth rate). Returns None when an input is missing, with the gaps in `missing`."""
    ni, da, capex, wc = items.get("net_income"), items.get("depreciation_amortization"), items.get("capex"), items.get("working_capital")
    missing = [n for n, s in (("net_income", ni), ("depreciation_amortization", da), ("capex", capex), ("current_assets", items.get("current_assets")),
                              ("current_liabilities", items.get("current_liabilities"))) if s is None]
    if missing:
        return {"missing": missing}
    oe = ni + da - capex.abs() - wc.diff()
    last = _last(oe)
    if last is None:
        return {"missing": ["net_working_capital_change"]}
    period = oe.replace([np.inf, -np.inf], np.nan).dropna().index[-1]
    out: dict[str, Any] = {"owner_earnings": last, "period": period, "intrinsic_value": None, "buy_below_value": None, "market_cap": None,
                           "margin_of_safety": None, "price_to_value": None, "passes_margin": None, "missing": []}
    if last > 0:
        value = last / (discount_rate - growth_rate)
        out["intrinsic_value"] = value
        out["buy_below_value"] = value * (1.0 - safety_margin)
        cap = items.get("market_cap")
        if cap is not None and period in cap.index and math.isfinite(float(cap.loc[period])):
            mc = float(cap.loc[period])
            out["market_cap"] = mc
            out["margin_of_safety"] = 1.0 - mc / value
            out["price_to_value"] = mc / value
            out["passes_margin"] = bool(mc <= value * (1.0 - safety_margin))
    return out


def _assumptions(discount_rate: float, growth_rate: float, safety_margin: float) -> dict[str, Any]:
    return {"discount_rate": discount_rate, "growth_rate": growth_rate, "safety_margin": safety_margin,
            "formula": "owner earnings = net income + depreciation and amortization - abs(capex) - change in (current assets - current liabilities)",
            "value_model": "intrinsic value = owner earnings / (discount rate - growth rate); buy below = value x (1 - safety margin)"}


def _validate_common(min_periods: Any, max_age_days: Any) -> tuple[int, int]:
    return _whole("min_periods", min_periods, 2, 40), _whole("max_age_days", max_age_days, 1, 1000)


def _validate_valuation(discount_rate: Any, growth_rate: Any, safety_margin: Any) -> tuple[float, float, float]:
    d, g, m = _rate("discount_rate", discount_rate, 0.001, 1.0), _rate("growth_rate", growth_rate, -0.5, 0.5), _rate("safety_margin", safety_margin, 0.0, 0.99)
    if g >= d:
        raise BadInput("growth_rate must be below discount_rate")
    return d, g, m


def _finish(fn: str, items: c.Items, warns: tuple[str, ...], criteria: list[dict[str, Any]], lean: int, code: str, cap: int, params: dict[str, Any],
            extra: dict[str, Any] | None = None, unit: str = "criteria"):
    pieces = PROFILE_PIECES[fn]
    value = {"profile": PROFILE_CODES.get(fn), "criteria": criteria, "lean": lean, "reason_code": code, "confidence_cap": cap,
             "data_missing": [], "n_periods": len(items.frame)}
    if extra:
        value.update(extra)
    return c.build(f"fundamentals.{fn}", value, unit=unit, method=f"{fn} criteria", params=params, piece_ids=pieces, inputs=(items.frame,),
                   warnings=warns + tuple(items.notes), as_of=items.frame.index[-1] if len(items.frame) else None)


def _abstain(fn: str, warnings: list[str] | tuple[str, ...], params: dict[str, Any]):
    return c.empty(f"fundamentals.{fn}", "criteria", f"{fn} criteria", tuple(dict.fromkeys(warnings)), params=params, piece_ids=PROFILE_PIECES[fn])


_IN = {"statements": c.stmt_input(), "as_of": c.AS_OF_INPUT, "min_periods": {"type": "int", "required": False, "default": 4},
       "max_age_days": {"type": "int", "required": False, "default": 150}}
_VAL_IN = {"discount_rate": {"type": "float", "required": False, "default": 0.10}, "growth_rate": {"type": "float", "required": False, "default": 0.03}}


# ---------------------------------------------------------------------------------------------------------------------
# SHF-004: owner earnings with a margin of safety
# ---------------------------------------------------------------------------------------------------------------------
@registry.register("fundamentals.owner_earnings", module="fundamentals", piece_ids=("SHF-004",),
                   inputs={"statements": c.stmt_input(), "as_of": c.AS_OF_INPUT, **_VAL_IN, "safety_margin": {"type": "float", "required": False, "default": 0.25}},
                   summary="Owner earnings (net income + D&A - capex - change in working capital), the value they imply with the assumptions printed in the params, the price that keeps a margin of safety (25 percent by default) and the margin of the current market capitalization.")
def owner_earnings(statements: Any, as_of: Any = None, discount_rate: float = DEFAULTS["discount_rate"], growth_rate: float = DEFAULTS["growth_rate"],
                   safety_margin: float = SAFETY_MARGIN):
    name, pieces = "fundamentals.owner_earnings", PROFILE_PIECES["owner_earnings"]
    d, g, m = _validate_valuation(discount_rate, growth_rate, safety_margin)
    params = _assumptions(d, g, m)
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return c.empty(name, "usd", "owner earnings", warns + ("no periods in the statements",), params=params, piece_ids=pieces)
    core = owner_earnings_core(items, d, g, m)
    if core is None or core.get("missing"):
        return c.empty(name, "usd", "owner earnings", warns + tuple(f"data_missing:{x}" for x in core["missing"]), params=params, piece_ids=pieces)
    extra = ("owner_earnings_not_positive: no value can be built from it",) if core["owner_earnings"] <= 0 else ()
    value = {k: v for k, v in core.items() if k != "missing"}
    value["period"] = str(value["period"])
    return c.build(name, value, unit="usd", method="owner earnings", params=params, piece_ids=pieces, inputs=(items.frame,),
                   warnings=warns + tuple(items.notes) + extra, as_of=items.frame.index[-1])


# ---------------------------------------------------------------------------------------------------------------------
# INV-001 (and SHF-006): quality owner
# ---------------------------------------------------------------------------------------------------------------------
@registry.register("fundamentals.quality_owner", module="fundamentals", piece_ids=PROFILE_PIECES["quality_owner"],
                   inputs={**_IN, **_VAL_IN},
                   summary="Criteria of the quality owner profile: ROE sustained (average 15 percent, floor 10 percent), debt to equity at most 0.5, margin stability, positive and rising FCF per share, book value growth and the margin of safety on owner earnings. Missing a required criterion gives no value; a price that demands perfection gives a negative lean.")
def quality_owner(statements: Any, as_of: Any = None, min_periods: int = 4, max_age_days: int = 150, discount_rate: float = DEFAULTS["discount_rate"],
                  growth_rate: float = DEFAULTS["growth_rate"]):
    fn, src = "quality_owner", "fundamentals.quality_owner"
    n_min, age = _validate_common(min_periods, max_age_days)
    d, g, m = _validate_valuation(discount_rate, growth_rate, SAFETY_MARGIN)
    params = {**_assumptions(d, g, m), "min_periods": n_min, "max_age_days": age, "roe_average_min": ROE_AVERAGE_MIN, "roe_floor_min": ROE_FLOOR_MIN,
              "debt_to_equity_max": DEBT_TO_EQUITY_MAX, **{k: UNCALIBRATED[k] for k in ("margin_stability_max", "roe_drop_max", "debt_rise_max", "perfection_ratio")}}
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return _abstain(fn, warns + ("no periods in the statements",), params)
    gaps = _gate(items, n_min, age)
    ni = items.get("net_income")
    if ni is not None and (ni < 0).sum() > len(ni.dropna()) / 2:
        gaps.append("not_applicable:net_income_negative_in_most_periods")
    roe, de, margin = _roe(items), _debt_to_equity(items), _op_margin(items)
    bv = _ratio(items.get("total_equity"), items.get("shares_outstanding"))
    fcfps = _fcf_per_share(items)
    core = owner_earnings_core(items, d, g, m)
    if roe is None:
        gaps.append("data_missing:net_income_or_total_equity_for_roe")
    if de is None:
        gaps.append("data_missing:total_debt_or_total_equity")
    if bv is None:
        gaps.append("data_missing:book_value")
    if core is None or core.get("missing"):
        gaps.extend(f"data_missing:{x}" for x in (core or {}).get("missing", ["owner_earnings"]))
    elif core["margin_of_safety"] is None:
        gaps.append("data_missing:market_cap_or_positive_owner_earnings")
    if gaps:
        return _abstain(fn, warns + tuple(gaps), params)
    roe_clean = roe.replace([np.inf, -np.inf], np.nan).dropna()
    if roe_clean.empty:
        return _abstain(fn, warns + ("data_missing:roe_needs_two_periods",), params)
    roe_avg, roe_min = float(roe_clean.mean()), float(roe_clean.min())
    de_last = _last(de)
    stability = float(margin.std(ddof=0)) if margin is not None and margin.dropna().size >= 2 else None
    fcf_last, fcf_first = _last(fcfps), _first(fcfps)
    fcf_value = fcf_last
    fcf_pass = None if fcf_last is None or fcf_first is None else bool(fcf_last > 0 and fcf_last > fcf_first)
    bv_clean = bv.dropna()
    bv_cagr = None
    if len(bv_clean) >= 2 and bv_clean.iloc[0] > 0 and bv_clean.iloc[-1] > 0:
        bv_cagr = float((bv_clean.iloc[-1] / bv_clean.iloc[0]) ** (1.0 / (len(bv_clean) - 1)) - 1.0)
    roe_trend, debt_trend = _change(roe), _change(de)
    mos, ptv = core["margin_of_safety"], core["price_to_value"]
    criteria = [
        crit("roe_average", roe_avg, ROE_AVERAGE_MIN, roe_avg >= ROE_AVERAGE_MIN, src),
        crit("roe_minimum", roe_min, ROE_FLOOR_MIN, roe_min >= ROE_FLOOR_MIN, src),
        crit("debt_to_equity", de_last, DEBT_TO_EQUITY_MAX, None if de_last is None else de_last <= DEBT_TO_EQUITY_MAX, src),
        crit("margin_stability_std", stability, UNCALIBRATED["margin_stability_max"], None if stability is None else stability <= UNCALIBRATED["margin_stability_max"], src),
        crit("free_cash_flow_per_share", fcf_value, None, fcf_pass, src),
        crit("book_value_cagr", bv_cagr, 0.0, None if bv_cagr is None else bv_cagr > 0.0, src),
        crit("margin_of_safety", mos, SAFETY_MARGIN, mos >= SAFETY_MARGIN, "fundamentals.owner_earnings"),
        crit("roe_trend", roe_trend, -UNCALIBRATED["roe_drop_max"], None if roe_trend is None else roe_trend >= -UNCALIBRATED["roe_drop_max"], src),
        crit("debt_trend", debt_trend, UNCALIBRATED["debt_rise_max"], None if debt_trend is None else debt_trend <= UNCALIBRATED["debt_rise_max"], src),
        crit("price_to_value", ptv, UNCALIBRATED["perfection_ratio"], ptv < UNCALIBRATED["perfection_ratio"], "fundamentals.owner_earnings"),
    ]
    by = {x["name"]: x for x in criteria}
    quality_ok = all(by[n]["passed"] for n in ("roe_average", "roe_minimum", "debt_to_equity")) and all(
        by[n]["passed"] is not False for n in ("margin_stability_std", "free_cash_flow_per_share"))
    bearish = by["roe_trend"]["passed"] is False or by["debt_trend"]["passed"] is False or by["price_to_value"]["passed"] is False
    if bearish:
        lean, code = -1, "deterioration_or_price_demands_perfection"
    elif quality_ok and by["margin_of_safety"]["passed"]:
        lean, code = 1, "quality_with_margin_of_safety"
    elif quality_ok:
        lean, code = 0, "quality_without_margin_of_safety"
    else:
        lean, code = 0, "quality_criteria_not_all_met"
    depth_cap = LIMITED_DEPTH_CAP if len(items.frame) < 10 else 100
    return _finish(fn, items, warns, criteria, lean, code, depth_cap, params,
                   extra={"depth_periods": len(items.frame), "cannot_judge": ["competitive moat", "management honesty"],
                          "owner_earnings": {k: (str(v) if k == "period" else v) for k, v in core.items() if k != "missing"}})


# ---------------------------------------------------------------------------------------------------------------------
# INV-002: inversion veto
# ---------------------------------------------------------------------------------------------------------------------
@registry.register("fundamentals.inversion_veto", module="fundamentals", piece_ids=PROFILE_PIECES["inversion_veto"],
                   inputs={**_IN, **_VAL_IN, "veto_enabled": {"type": "bool", "required": False, "default": False}},
                   summary="Failure causes as criteria: falling margins, rising debt to equity, falling ROE, cash flow below 0.8 of net income and losses in the series. The lean is neutral by default; the veto counts only when the mandate of the desk turns it on.")
def inversion_veto(statements: Any, as_of: Any = None, min_periods: int = 4, max_age_days: int = 150, discount_rate: float = DEFAULTS["discount_rate"],
                   growth_rate: float = DEFAULTS["growth_rate"], veto_enabled: bool = False):
    fn, src = "inversion_veto", "fundamentals.inversion_veto"
    n_min, age = _validate_common(min_periods, max_age_days)
    d, g, m = _validate_valuation(discount_rate, growth_rate, SAFETY_MARGIN)
    params = {"min_periods": n_min, "max_age_days": age, "veto_enabled": bool(veto_enabled), **{k: UNCALIBRATED[k] for k in (
        "margin_stability_max", "roe_drop_max", "debt_rise_max", "earnings_quality_min", "earnings_quality_failing")}, **_assumptions(d, g, m)}
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return _abstain(fn, warns + ("no periods in the statements",), params)
    gaps = _gate(items, n_min, age)
    margin, de, roe = _op_margin(items), _debt_to_equity(items), _roe(items)
    ni, fcf = items.get("net_income"), items.get("free_cash_flow")
    for label, s in (("operating_margin", margin), ("debt_to_equity", de), ("roe", roe), ("net_income", ni), ("free_cash_flow", fcf)):
        if s is None:
            gaps.append(f"data_missing:{label}")
    if gaps:
        return _abstain(fn, warns + tuple(gaps), params)
    margin_change, debt_change, roe_change = _change(margin), _change(de), _change(roe)
    quality = _ratio(fcf, ni)
    q_mean = None if quality is None or quality.dropna().empty else float(quality.dropna().mean())
    losses = float((ni.dropna() < 0).sum())
    criteria = [
        crit("margin_change", margin_change, -0.02, None if margin_change is None else margin_change >= -0.02, src),
        crit("debt_to_equity_change", debt_change, UNCALIBRATED["debt_rise_max"], None if debt_change is None else debt_change <= UNCALIBRATED["debt_rise_max"], src),
        crit("roe_change", roe_change, -UNCALIBRATED["roe_drop_max"], None if roe_change is None else roe_change >= -UNCALIBRATED["roe_drop_max"], src),
        crit("earnings_quality_fcf_to_net_income", q_mean, UNCALIBRATED["earnings_quality_min"], None if q_mean is None else q_mean >= UNCALIBRATED["earnings_quality_min"], src),
        crit("loss_periods", losses, 0.0, losses == 0.0, src),
    ]
    core = owner_earnings_core(items, d, g, m)
    mos = None if core is None or core.get("missing") else core["margin_of_safety"]
    if mos is not None:
        criteria.append(crit("margin_of_safety", mos, SAFETY_MARGIN, mos >= SAFETY_MARGIN, "fundamentals.owner_earnings"))
    failed = [x["name"] for x in criteria if x["passed"] is False and x["name"] != "margin_of_safety"]
    too_hard = losses > 0
    strong = (q_mean is not None and q_mean < UNCALIBRATED["earnings_quality_failing"]) or len(failed) >= 2
    if strong:
        lean, code = -1, "strong_failure_signal"
    elif not failed and mos is not None and mos >= SAFETY_MARGIN:
        lean, code = 1, "no_failure_found_and_margin_of_safety"
    elif too_hard:
        lean, code = 0, "too_hard_series_is_inconsistent"
    else:
        lean, code = 0, "no_strong_failure_neutral_by_default"
    return _finish(fn, items, warns, criteria, lean, code, LIMITED_DEPTH_CAP, params,
                   extra={"failed_criteria": failed, "too_hard": too_hard, "veto_enabled": bool(veto_enabled),
                          "veto": (lean == -1) if veto_enabled else None,
                          "cannot_judge": ["management incentives", "corporate governance"]})


# ---------------------------------------------------------------------------------------------------------------------
# INV-003: safety margin
# ---------------------------------------------------------------------------------------------------------------------
@registry.register("fundamentals.safety_margin", module="fundamentals", piece_ids=PROFILE_PIECES["safety_margin"], inputs=_IN,
                   summary="Criteria of the safety margin profile: P/E at most 15, P/B at most 1.5 or their product at most 22.5, current ratio (2 strict, 1.5 minimum), long term debt within net current assets, profits in every period and no jumps in EPS. A loss makes the lean negative instead of abstaining.")
def safety_margin(statements: Any, as_of: Any = None, min_periods: int = 4, max_age_days: int = 150):
    fn, src = "safety_margin", "fundamentals.safety_margin"
    n_min, age = _validate_common(min_periods, max_age_days)
    params = {"min_periods": n_min, "max_age_days": age, **{f"classic_{k}": v for k, v in CLASSIC_DEFENSIVE.items()}, "eps_jump_max": UNCALIBRATED["eps_jump_max"]}
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return _abstain(fn, warns + ("no periods in the statements",), params)
    gaps = _gate(items, n_min, age)
    price, eps, bvps = items.get("price"), items.get("eps"), items.get("book_value_per_share")
    for label, s in (("price", price), ("eps", eps), ("book_value", bvps)):
        if s is None:
            gaps.append(f"data_missing:{label}")
    if gaps:
        return _abstain(fn, warns + tuple(gaps), params)
    p_last, e_last, b_last = _last(price), _last(eps), _last(bvps)
    if p_last is None or e_last is None or b_last is None:
        return _abstain(fn, warns + ("data_missing:latest_price_eps_or_book_value",), params)
    pe = p_last / e_last if e_last > 0 else None
    pb = p_last / b_last if b_last > 0 else None
    product = pe * pb if pe is not None and pb is not None else None
    cr = _last(_ratio(items.get("current_assets"), items.get("current_liabilities")))
    wc = _last(items.get("working_capital"))
    ltd = _last(items.get("long_term_debt"))
    nca_ratio = ltd / wc if ltd is not None and wc is not None and wc > 0 else None
    ni = items.get("net_income")
    losses = float((ni.dropna() < 0).sum()) if ni is not None else None
    jumps = _growth(eps)
    jump_max = None if jumps is None or jumps.dropna().empty else float(jumps.dropna().abs().max())
    criteria = [
        crit("eps_positive", e_last, 0.0, e_last > 0, src),
        crit("price_to_earnings", pe, CLASSIC_DEFENSIVE["pe_max"], None if pe is None else pe <= CLASSIC_DEFENSIVE["pe_max"], src),
        crit("price_to_book", pb, CLASSIC_DEFENSIVE["pb_max"], None if pb is None else pb <= CLASSIC_DEFENSIVE["pb_max"], src),
        crit("pe_times_pb", product, CLASSIC_DEFENSIVE["pe_x_pb_max"], None if product is None else product <= CLASSIC_DEFENSIVE["pe_x_pb_max"], src),
        crit("current_ratio_strict", cr, CLASSIC_DEFENSIVE["current_ratio_strict"], None if cr is None else cr >= CLASSIC_DEFENSIVE["current_ratio_strict"], src),
        crit("current_ratio_minimum", cr, CLASSIC_DEFENSIVE["current_ratio_minimum"], None if cr is None else cr >= CLASSIC_DEFENSIVE["current_ratio_minimum"], src),
        crit("long_term_debt_to_net_current_assets", nca_ratio, CLASSIC_DEFENSIVE["debt_to_net_current_assets_max"],
             None if nca_ratio is None else nca_ratio <= CLASSIC_DEFENSIVE["debt_to_net_current_assets_max"], src),
        crit("loss_periods", losses, 0.0, None if losses is None else losses == 0.0, src),
        crit("eps_jump_max", jump_max, UNCALIBRATED["eps_jump_max"], None if jump_max is None else jump_max <= UNCALIBRATED["eps_jump_max"], src),
    ]
    by = {x["name"]: x for x in criteria}
    solid = all(by[n]["passed"] is True for n in ("current_ratio_strict", "long_term_debt_to_net_current_assets", "loss_periods"))
    cheap = bool((pe is not None and pb is not None and pe <= CLASSIC_DEFENSIVE["pe_max"] and pb <= CLASSIC_DEFENSIVE["pb_max"]) or (product is not None and product <= CLASSIC_DEFENSIVE["pe_x_pb_max"]))
    hope = product is not None and product > 2.0 * CLASSIC_DEFENSIVE["pe_x_pb_max"]
    weak = (cr is not None and cr < CLASSIC_DEFENSIVE["current_ratio_minimum"]) or (losses or 0) > 0 or by["eps_jump_max"]["passed"] is False or hope
    if e_last <= 0:
        lean, code = -1, "eps_not_positive_disqualifies"
    elif weak:
        lean, code = -1, "weak_finances_unstable_earnings_or_price_capitalizes_hope"
    elif solid and cheap:
        lean, code = 1, "solid_balance_sheet_and_price_inside_the_margin"
    else:
        lean, code = 0, "solid_without_margin_or_not_all_met"
    return _finish(fn, items, warns, criteria, lean, code, 100, params,
                   extra={"disqualified": bool(e_last <= 0), "cannot_judge": ["dividend record of twenty years", "assets outside the balance sheet"],
                          "classic_thresholds": dict(CLASSIC_DEFENSIVE)})


# ---------------------------------------------------------------------------------------------------------------------
# INV-004 (and SHF-009): fair growth
# ---------------------------------------------------------------------------------------------------------------------
@registry.register("fundamentals.fair_growth", module="fundamentals", piece_ids=PROFILE_PIECES["fair_growth"],
                   inputs={**_IN, "periods_per_year": {"type": "int", "required": False, "default": 1},
                           "insider_net_buying": {"type": "float", "required": False, "default": None},
                           "institutional_ownership": {"type": "float", "required": False, "default": None}},
                   summary="Category by EPS growth (fast 20 percent or more, stalwart, slow, turnaround), cyclical flag, PEG (below 1 adds, above 2 subtracts, 1 and 2 add nothing), earnings conversion and debt. Without a PEG there is no value. Insiders and institutional ownership count only if given.")
def fair_growth(statements: Any, as_of: Any = None, min_periods: int = 4, max_age_days: int = 150, periods_per_year: int = 1,
                insider_net_buying: float | None = None, institutional_ownership: float | None = None):
    fn, src = "fair_growth", "fundamentals.fair_growth"
    n_min, age = _validate_common(min_periods, max_age_days)
    ppy = _whole("periods_per_year", periods_per_year, 1, 12)
    params = {"min_periods": n_min, "max_age_days": age, "periods_per_year": ppy, "peg_bullish_below": PEG_BULLISH_BELOW, "peg_bearish_above": PEG_BEARISH_ABOVE,
              "fast_growth": FAST_GROWTH, "stalwart_growth": STALWART_GROWTH, "debt_to_equity_max": DEBT_TO_EQUITY_MAX,
              "category_band_assumption": "growth between 12 and 20 percent is classified as stalwart: the source gives no band for it"}
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return _abstain(fn, warns + ("no periods in the statements",), params)
    gaps = _gate(items, n_min, age)
    eps, price = items.get("eps"), items.get("price")
    for label, s in (("eps", eps), ("price", price)):
        if s is None:
            gaps.append(f"data_missing:{label}")
    if gaps:
        return _abstain(fn, warns + tuple(gaps), params)
    e = eps.dropna()
    e_first, e_last, p_last = float(e.iloc[0]), float(e.iloc[-1]), _last(price)
    if p_last is None or len(e) < 2:
        return _abstain(fn, warns + ("data_missing:price_or_two_eps_values",), params)
    years = (len(e) - 1) / ppy
    category = None
    cagr = None
    if e_first <= 0 < e_last:
        category = "turnaround"
    elif e_first > 0 and e_last > 0:
        cagr = float((e_last / e_first) ** (1.0 / years) - 1.0)
        category = "fast" if cagr >= FAST_GROWTH else ("stalwart" if cagr >= STALWART_GROWTH else "slow")
    pe = p_last / e_last if e_last > 0 else None
    peg = pe / (cagr * 100.0) if pe is not None and cagr is not None and cagr > 0 else None
    near_zero_base = e_first > 0 and e_last > 0 and e_first < 0.05 * e_last
    if peg is None or near_zero_base:
        reason = "turnaround_has_no_peg" if category == "turnaround" else ("base_near_zero" if near_zero_base else "growth_not_positive_or_eps_not_positive")
        return _abstain(fn, warns + ("data_missing:peg", f"peg_not_defined:{reason}"), params)
    yoy = _growth(eps)
    swings = 0 if yoy is None else int((np.sign(yoy.dropna()).diff().abs() > 0).sum())
    cyclical = bool(swings >= 2)
    conversion = _change(_net_margin(items))
    de_last = _last(_debt_to_equity(items))
    criteria = [
        crit("eps_growth_cagr", cagr, STALWART_GROWTH, cagr >= STALWART_GROWTH, src),
        crit("peg", peg, PEG_BULLISH_BELOW, peg < PEG_BULLISH_BELOW, src),
        crit("peg_not_excessive", peg, PEG_BEARISH_ABOVE, peg <= PEG_BEARISH_ABOVE, src),
        crit("earnings_conversion_margin_change", conversion, 0.0, None if conversion is None else conversion >= 0.0, src),
        crit("debt_to_equity", de_last, DEBT_TO_EQUITY_MAX, None if de_last is None else de_last <= DEBT_TO_EQUITY_MAX, src),
    ]
    not_available = []
    if insider_net_buying is None:
        not_available.append("insider_transactions")
    else:
        criteria.append(crit("insider_net_buying", _rate("insider_net_buying", insider_net_buying, -1e15, 1e15), 0.0, insider_net_buying > 0, src))
    if institutional_ownership is None:
        not_available.append("institutional_ownership")
    else:
        criteria.append(crit("institutional_ownership", _rate("institutional_ownership", institutional_ownership, 0.0, 1.0), None, None, src))
    points = peg_points(peg)
    by = {x["name"]: x for x in criteria}
    if points > 0 and (category == "slow" or by["earnings_conversion_margin_change"]["passed"] is not True or by["debt_to_equity"]["passed"] is not True):
        lean, code = 0, "peg_attractive_but_category_conversion_or_debt_do_not_support"
    elif points > 0:
        lean, code = 1, "visible_growth_with_attractive_peg"
    elif points < 0:
        lean, code = -1, "growth_does_not_justify_the_multiple"
    else:
        lean, code = 0, "fair_price_or_in_between"
    return _finish(fn, items, warns, criteria, lean, code, 100, params,
                   extra={"category": category, "cyclical": cyclical, "peg": peg, "peg_points": points, "not_available": not_available,
                          "cannot_judge": ["investing in what you know", "the qualitative story"]})


# ---------------------------------------------------------------------------------------------------------------------
# INV-005: inflection
# ---------------------------------------------------------------------------------------------------------------------
def _complete_candles(candles: Any, cut: pd.Timestamp) -> tuple[pd.DataFrame | None, list[str]]:
    if not isinstance(candles, pd.DataFrame):
        raise BadInput(f"candles: expected a DataFrame, got {type(candles).__name__}")
    if not isinstance(candles.index, pd.DatetimeIndex):
        raise BadInput("candles need a datetime index")
    frame = candles.rename(columns=str.lower)
    need = ["open", "high", "low", "close"]
    missing = [n for n in need if n not in frame.columns]
    if missing:
        return None, [f"data_missing:candle_{n}" for n in missing]
    frame = frame[need].astype(float).replace([np.inf, -np.inf], np.nan)
    index = frame.index.tz_localize(None) if frame.index.tz is not None else frame.index
    frame = frame.set_axis(index)
    frame = frame[frame.index.normalize() <= cut.normalize()].dropna()
    if (frame["high"] < frame[["open", "close"]].max(axis=1)).any() or (frame["low"] > frame[["open", "close"]].min(axis=1)).any() or (frame <= 0).any().any():
        return None, ["candles_inconsistent"]
    if len(frame) < MIN_BARS:
        return None, [f"data_missing:candles_at_least_{MIN_BARS}_complete_bars_have_{len(frame)}"]
    return frame, []


@registry.register("fundamentals.inflection", module="fundamentals", piece_ids=PROFILE_PIECES["inflection"],
                   inputs={"statements": c.stmt_input(), "candles": {"type": "dataframe", "required": True}, "regime": {"type": "str", "required": True},
                           "as_of": {"type": "date", "required": True}, "min_periods": {"type": "int", "required": False, "default": 4},
                           "max_age_days": {"type": "int", "required": False, "default": 150}},
                   summary="Inflection of the fundamentals: acceleration of revenue growth, margin inflection, EPS momentum, the multiple against its own history and the price trend on complete daily candles cut at the date, with the regime as a veto. The default is to wait (lean 0 with a reason).")
def inflection(statements: Any, candles: Any, regime: str, as_of: Any, min_periods: int = 4, max_age_days: int = 150):
    fn, src = "inflection", "fundamentals.inflection"
    n_min, age = _validate_common(min_periods, max_age_days)
    if as_of is None:
        raise BadInput("as_of is required: the candles are cut at it")
    if regime is not None and regime not in REGIMES:
        raise BadInput(f"regime must be one of {list(REGIMES)} (as read from the regime of SVC-stats)")
    params = {"min_periods": n_min, "max_age_days": age, "calibrated": False,
              **{k: UNCALIBRATED[k] for k in ("revenue_accel_min", "priced_in_max", "recognized_move_max")}}
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return _abstain(fn, warns + ("no periods in the statements",), params)
    gaps = _gate(items, n_min, age)
    cut = pd.Timestamp(as_of)
    bars, bad = _complete_candles(candles, cut)
    gaps.extend(bad)
    if regime is None:
        gaps.append("data_missing:regime")
    rev, eps = items.get("revenue"), items.get("eps")
    margin = _op_margin(items)
    if rev is None:
        gaps.append("data_missing:revenue")
    if margin is None:
        gaps.append("data_missing:operating_margin")
    g = _growth(rev)
    if rev is not None and (g is None or g.dropna().size < 2):
        gaps.append("data_missing:revenue_growth_needs_three_periods")
    if gaps:
        return _abstain(fn, warns + tuple(gaps), params)
    gv = g.dropna().to_numpy()
    k = max(1, min(2, len(gv) // 2))
    accel = float(gv[-k:].mean() - gv[-2 * k:-k].mean()) if len(gv) >= 2 * k else float(gv[-1] - gv[-2])
    mvals = margin.dropna().to_numpy()
    margin_delta = float(mvals[-1] - mvals[-2]) if len(mvals) >= 2 else None
    eps_g = _last(_growth(eps)) if eps is not None else None
    pe = items.get("price_earnings")
    pe_clean = None if pe is None else pe.dropna()
    vs_trend = float(pe_clean.iloc[-1] / pe_clean.iloc[:-1].mean() - 1.0) if pe_clean is not None and len(pe_clean) >= 2 else None
    close = bars["close"]
    ret63 = float(close.iloc[-1] / close.iloc[-64] - 1.0) if len(close) > 63 else None
    sma200 = float(close.iloc[-200:].mean())
    criteria = [
        crit("revenue_growth_acceleration", accel, UNCALIBRATED["revenue_accel_min"], accel > UNCALIBRATED["revenue_accel_min"], src),
        crit("operating_margin_inflection", margin_delta, 0.0, None if margin_delta is None else margin_delta > 0.0, src),
        crit("eps_growth_latest", eps_g, 0.0, None if eps_g is None else eps_g > 0.0, src),
        crit("multiple_vs_own_history", vs_trend, UNCALIBRATED["priced_in_max"], None if vs_trend is None else vs_trend <= UNCALIBRATED["priced_in_max"], src),
        crit("price_move_63_bars", ret63, UNCALIBRATED["recognized_move_max"], None if ret63 is None else ret63 <= UNCALIBRATED["recognized_move_max"], src),
        crit("price_vs_sma_200", float(close.iloc[-1] / sma200 - 1.0), None, None, src),
    ]
    by = {x["name"]: x for x in criteria}
    up_setup = (by["revenue_growth_acceleration"]["passed"] is True and by["operating_margin_inflection"]["passed"] is True
                and by["multiple_vs_own_history"]["passed"] is True and by["price_move_63_bars"]["passed"] is True)
    down_setup = (accel < 0 and margin_delta is not None and margin_delta < 0 and vs_trend is not None and vs_trend > 0)
    if up_setup and regime == "hostile":
        lean, code = 0, "wait:regime_is_hostile"
    elif up_setup:
        lean, code = 1, "inflection_up_not_yet_recognized"
    elif down_setup:
        lean, code = -1, "inflection_down_priced_for_the_old_path"
    else:
        lean, code = 0, "wait:no_inflection_or_price_and_trend_agree"
    return _finish(fn, items, warns, criteria, lean, code, LIMITED_DEPTH_CAP, params,
                   extra={"default": "wait", "regime": regime, "bars_used": int(len(bars)), "last_bar": str(bars.index[-1])[:10],
                          "cannot_judge": ["central bank policy", "liquidity", "bonds, currencies and commodities", "how much to commit"]})


# ---------------------------------------------------------------------------------------------------------------------
# INV-006 (and SHF-008): contrarian value gap with a dated catalyst
# ---------------------------------------------------------------------------------------------------------------------
def _catalyst_date(catalyst: Any) -> date | None:
    if catalyst is None:
        return None
    if not isinstance(catalyst, dict) or "date" not in catalyst:
        raise BadInput("catalyst must be a dict with a date (and a description)")
    text = catalyst.get("description", "")
    if not isinstance(text, str) or len(text) > 300:
        raise BadInput("catalyst description must be text of at most 300 characters")
    try:
        return pd.Timestamp(catalyst["date"]).date()
    except (TypeError, ValueError) as exc:
        raise BadInput(f"catalyst date is not a date: {catalyst['date']!r}") from exc


@registry.register("fundamentals.contrarian_gap", module="fundamentals", piece_ids=PROFILE_PIECES["contrarian_gap"],
                   inputs={**_IN, **_VAL_IN, "catalyst": {"type": "dict", "required": False, "default": None}},
                   summary="Value gap between price and the conservative value on owner earnings (above 30 percent), the bubble side (price over twice the value, multiple at the top of its history) and the catalyst, which needs a date on or after the cut-off. Without a valid catalyst the lean cannot be positive.")
def contrarian_gap(statements: Any, as_of: Any = None, min_periods: int = 4, max_age_days: int = 150, discount_rate: float = DEFAULTS["discount_rate"],
                   growth_rate: float = DEFAULTS["growth_rate"], catalyst: dict | None = None):
    fn, src = "contrarian_gap", "fundamentals.contrarian_gap"
    n_min, age = _validate_common(min_periods, max_age_days)
    d, g, m = _validate_valuation(discount_rate, growth_rate, SAFETY_MARGIN)
    catalyst_day = _catalyst_date(catalyst)
    params = {**_assumptions(d, g, m), "min_periods": n_min, "max_age_days": age, "gap_min": GAP_MIN, "bubble_price_to_value": UNCALIBRATED["perfection_ratio"],
              "bubble_multiple_percentile": 0.9}
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return _abstain(fn, warns + ("no periods in the statements",), params)
    gaps = _gate(items, n_min, age)
    core = owner_earnings_core(items, d, g, m)
    if core is None or core.get("missing"):
        gaps.extend(f"data_missing:{x}" for x in (core or {}).get("missing", ["owner_earnings"]))
    elif core["intrinsic_value"] is None:
        gaps.append("data_missing:positive_owner_earnings")
    elif core["margin_of_safety"] is None:
        gaps.append("data_missing:market_cap")
    if gaps:
        return _abstain(fn, warns + tuple(gaps), params)
    gap, ptv = core["margin_of_safety"], core["price_to_value"]
    pe = items.get("price_earnings")
    pe_clean = None if pe is None else pe.dropna()
    percentile = float((pe_clean <= pe_clean.iloc[-1]).mean()) if pe_clean is not None and len(pe_clean) >= 4 else None
    cut_day = pd.Timestamp(as_of).date() if as_of is not None else None
    days = None if catalyst_day is None or cut_day is None else float((catalyst_day - cut_day).days)
    valid = days is not None and days >= 0
    criteria = [
        crit("value_gap", gap, GAP_MIN, gap > GAP_MIN, "fundamentals.owner_earnings"),
        crit("price_to_value", ptv, UNCALIBRATED["perfection_ratio"], ptv < UNCALIBRATED["perfection_ratio"], "fundamentals.owner_earnings"),
        crit("multiple_percentile_of_own_history", percentile, 0.9, None if percentile is None else percentile < 0.9, src),
        crit("catalyst_days_ahead", days, 0.0, None if days is None else days >= 0.0, src),
    ]
    bubble = ptv >= UNCALIBRATED["perfection_ratio"] or (percentile is not None and percentile >= 0.9 and gap < 0)
    if bubble:
        lean, code = -1, "overvalued_or_bubble_conditions"
    elif gap > GAP_MIN and valid:
        lean, code = 1, "value_gap_with_dated_catalyst"
    elif gap > GAP_MIN:
        lean, code = 0, "value_gap_without_a_valid_dated_catalyst"
    else:
        lean, code = 0, "fair_value_or_no_catalyst"
    missing = [] if catalyst is not None else ["catalyst"]
    return _finish(fn, items, warns, criteria, lean, code, LIMITED_DEPTH_CAP, params,
                   extra={"catalyst_valid": bool(valid), "data_missing": missing, "value_gap": gap,
                          "cannot_judge": ["notes of the annual report", "issuance documents", "short positioning"],
                          "owner_earnings": {k: (str(v) if k == "period" else v) for k, v in core.items() if k != "missing"}})


# ---------------------------------------------------------------------------------------------------------------------
# INV-007 (and SHF-007): disruption builder
# ---------------------------------------------------------------------------------------------------------------------
THEMES_FILE = Path(__file__).resolve().parents[4] / "kit" / "template" / "desk" / "data" / "themes.csv"
_SYMBOL = re.compile(r"[A-Za-z][A-Za-z0-9.\-]{0,9}")


@lru_cache(maxsize=1)
def load_themes() -> dict[str, str]:
    """The curated table of themes: ticker to theme. It is the only source of a theme."""
    table: dict[str, str] = {}
    with THEMES_FILE.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            sym, theme = (row.get("symbol") or "").strip().upper(), (row.get("theme") or "").strip()
            if sym and theme in THEME_NAMES:
                table[sym] = theme
    return table


@registry.register("fundamentals.disruption", module="fundamentals", piece_ids=PROFILE_PIECES["disruption"],
                   inputs={**_IN, "symbol": {"type": "str", "required": True}, "rd_intensity_min": {"type": "float", "required": False, "default": None},
                           "runway_months_min": {"type": "float", "required": False, "default": None},
                           "dilution_max": {"type": "float", "required": False, "default": None},
                           "revenue_accel_min": {"type": "float", "required": False, "default": None}},
                   summary="Criteria of the disruption profile: theme from the curated table, R&D intensity, revenue growth and acceleration, gross margin trend, cash runway and dilution. TAM, first mover and team are declared unknown and the confidence never passes 69. Thresholds that no source gives are parameters: without them the lean is neutral and says so.")
def disruption(statements: Any, symbol: str, as_of: Any = None, min_periods: int = 4, max_age_days: int = 150, rd_intensity_min: float | None = None,
               runway_months_min: float | None = None, dilution_max: float | None = None, revenue_accel_min: float | None = None):
    fn, src = "disruption", "fundamentals.disruption"
    n_min, age = _validate_common(min_periods, max_age_days)
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise BadInput("symbol must be a ticker such as NVDA")
    th = {k: (None if v is None else _rate(k, v, -1e6, 1e6)) for k, v in (("rd_intensity_min", rd_intensity_min), ("runway_months_min", runway_months_min),
                                                                        ("dilution_max", dilution_max), ("revenue_accel_min", revenue_accel_min))}
    params = {"min_periods": n_min, "max_age_days": age, "symbol": symbol.upper(), **th, "thresholds_calibrated": all(v is not None for v in th.values())}
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return _abstain(fn, warns + ("no periods in the statements",), params)
    gaps = _gate(items, n_min, age)
    theme = load_themes().get(symbol.upper())
    if theme is None:
        gaps.append("data_missing:theme_not_in_the_curated_table")
    rev, rd = items.get("revenue"), items.get("rd_expenses")
    if rev is None:
        gaps.append("data_missing:revenue")
    if rd is None:
        gaps.append("data_missing:rd_expenses")
    if gaps:
        return _abstain(fn, warns + tuple(gaps), params)
    rd_int = _last(_ratio(rd, rev))
    g = _growth(rev)
    g_last = _last(g)
    gv = None if g is None else g.dropna().to_numpy()
    accel = float(gv[-1] - gv[-2]) if gv is not None and len(gv) >= 2 else None
    gm = _ratio(items.get("gross_profit"), rev)
    gm_change = _change(gm)
    fcf, cash = items.get("free_cash_flow"), items.get("cash_and_short_term_investments")
    runway = None
    if fcf is not None and cash is not None:
        f_last, c_last = _last(fcf), _last(cash)
        if f_last is not None and c_last is not None:
            runway = 999.0 if f_last >= 0 else min(999.0, float(c_last / (-f_last / 12.0)))
    shares = items.get("shares_outstanding")
    dilution = None
    if shares is not None and shares.dropna().size >= 2 and shares.dropna().iloc[0] > 0:
        sh = shares.dropna()
        dilution = float((sh.iloc[-1] / sh.iloc[0]) ** (1.0 / (len(sh) - 1)) - 1.0)
    ps = _last(_ratio(items.get("market_cap"), rev))

    def judged(value: float | None, thr: float | None, ok) -> bool | None:
        return None if value is None or thr is None else bool(ok(value, thr))

    criteria = [
        crit("theme_in_curated_table", 1.0, None, True, src),
        crit("rd_intensity", rd_int, th["rd_intensity_min"], judged(rd_int, th["rd_intensity_min"], lambda v, t: v >= t), src),
        crit("revenue_growth_latest", g_last, None, None, src),
        crit("revenue_growth_acceleration", accel, th["revenue_accel_min"], judged(accel, th["revenue_accel_min"], lambda v, t: v > t), src),
        crit("gross_margin_change", gm_change, 0.0, None if gm_change is None else gm_change >= 0.0, src),
        crit("cash_runway_months", runway, th["runway_months_min"], judged(runway, th["runway_months_min"], lambda v, t: v >= t), src),
        crit("dilution_per_period", dilution, th["dilution_max"], judged(dilution, th["dilution_max"], lambda v, t: v <= t), src),
        crit("price_to_sales", ps, None, None, src),
    ]
    by = {x["name"]: x for x in criteria}
    if not params["thresholds_calibrated"]:
        lean, code = 0, "thresholds_not_calibrated"
    elif (by["cash_runway_months"]["passed"] is False and by["dilution_per_period"]["passed"] is False):
        lean, code = -1, "short_cash_runway_with_dilution"
    elif (by["revenue_growth_acceleration"]["passed"] is False and by["rd_intensity"]["passed"] is not True):
        lean, code = -1, "revenue_slows_without_rd_support"
    elif all(by[n]["passed"] is True for n in ("rd_intensity", "revenue_growth_acceleration", "cash_runway_months")) and by["dilution_per_period"]["passed"] is not False:
        lean, code = 1, "valid_theme_rd_acceleration_and_runway"
    else:
        lean, code = 0, "valid_theme_without_clear_acceleration"
    return _finish(fn, items, warns, criteria, lean, code, RUTH_CONFIDENCE_CAP, params,
                   extra={"theme": theme, "tam": "unknown", "first_mover": "unknown", "team_vision": "unknown",
                          "data_missing": ["tam", "first_mover", "team_vision"],
                          "cannot_judge": ["total addressable market", "first mover advantage", "vision of the team"]})

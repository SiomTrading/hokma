"""Formulas that the desks read: enterprise value, buyback yield, FCF after SBC, simplified ROIC, shareholder yield and TTM
(FM-13 to FM-16, FM-18, FT-006). The convention of each one is in docs/quant/fundamentals-conventions.md.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _common as c
from hokma.quant.fundamentals import _lib

_IN = {"statements": c.stmt_input(), "as_of": c.AS_OF_INPUT}
STALE_DAYS = 185                      # a window that ends this long before the cut-off is not the current number
_CONCEPT = re.compile(r"[A-Za-z0-9_:\-]{1,80}")


def _needs(items: c.Items, *names: str) -> list[str]:
    return [n for n in names if items.get(n) is None]


def enterprise_value_series(items: c.Items) -> pd.Series | None:
    cap, debt, cash = items.get("market_cap"), items.get("total_debt"), items.get("cash_and_short_term_investments")
    if cap is None or debt is None or cash is None:
        return None
    return _lib.ft_valuation.get_enterprise_value(cap, debt, items._optional("minority_interest"), items._optional("preferred_equity"), cash)


def enterprise_value_library(items: c.Items) -> pd.Series | None:
    """The same formula with cash only, as the library controller reads it."""
    cap, debt, cash = items.get("market_cap"), items.get("total_debt"), items.get("cash")
    if cap is None or debt is None or cash is None:
        return None
    return _lib.ft_valuation.get_enterprise_value(cap, debt, items._optional("minority_interest"), items._optional("preferred_equity"), cash)


def buyback_yield_series(items: c.Items) -> pd.Series | None:
    rep, cap = items.get("repurchases"), items.get("market_cap")
    if rep is None or cap is None:
        return None
    iss = items.get("issuance")
    issued = pd.Series(0.0, index=rep.index) if iss is None else iss.fillna(0.0)
    return _lib.ft_valuation.get_buyback_yield(-rep.abs(), issued, cap)


def fcf_after_sbc_series(items: c.Items) -> pd.Series | None:
    fcf, sbc = items.get("free_cash_flow"), items.get("sbc")
    if fcf is None or sbc is None:
        return None
    return _lib.ft_valuation.get_sbc_adjusted_free_cash_flow(fcf, sbc.abs())


def roic_simple_series(items: c.Items) -> pd.Series | None:
    oi, rate, capital = items.get("operating_income"), items.get("effective_tax_rate"), items.get("invested_capital")
    if oi is None or rate is None or capital is None:
        return None
    nopat = _lib.ft_eva.get_net_operating_profit_after_taxes(oi, rate)
    return nopat / capital.where(capital > 0)


def shareholder_yield_series(items: c.Items) -> pd.Series | None:
    dy, by = items.get("dividend_yield"), buyback_yield_series(items)
    if dy is None or by is None:
        return None
    return _lib.ft_valuation.get_shareholder_yield(dy, by)


def shareholder_yield_library(items: c.Items) -> pd.Series | None:
    dy, by = items.get("dividend_yield"), items.call(_lib.ft_valuation.get_buyback_yield)[0]
    if dy is None or by is None:
        return None
    return _lib.ft_valuation.get_shareholder_yield(dy, by)


def _single(name: str, label: str, statements: Any, as_of: Any, needs: tuple[str, ...], series_fn, pieces: tuple[str, ...], unit: str, method: str):
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return c.empty(name, unit, method, warns + ("no periods in the statements",), piece_ids=pieces)
    series = series_fn(items)
    if series is None:
        gaps = tuple(f"data_missing:{m}" for m in _needs(items, *needs)) or ("not computable",)
        return c.empty(name, unit, method, warns + gaps, piece_ids=pieces)
    return c.build(name, c.series_table({label: series.replace([np.inf, -np.inf], np.nan)}), unit=unit, method=method, piece_ids=pieces,
                   inputs=(items.frame,), warnings=warns + tuple(items.notes), as_of=items.frame.index[-1])


@registry.register("fundamentals.enterprise_value", module="fundamentals", inputs=_IN, piece_ids=("FM-13",),
                   summary="Enterprise value by period: market capitalization + debt + minority interest + preferred equity - cash and short term investments.")
def enterprise_value(statements: Any, as_of: Any = None):
    return _single("fundamentals.enterprise_value", "enterprise_value", statements, as_of, ("market_cap", "total_debt", "cash"),
                   enterprise_value_series, ("FM-13",), "usd", "market cap + debt + minority + preferred - cash")


@registry.register("fundamentals.buyback_yield", module="fundamentals", inputs=_IN, piece_ids=("FM-14",),
                   summary="Buyback yield by period: (repurchases - issuance) over market capitalization.")
def buyback_yield(statements: Any, as_of: Any = None):
    return _single("fundamentals.buyback_yield", "buyback_yield", statements, as_of, ("repurchases", "market_cap"),
                   buyback_yield_series, ("FM-14",), "ratio", "(abs(repurchases) - issuance) / market cap")


@registry.register("fundamentals.fcf_after_sbc", module="fundamentals", inputs=_IN, piece_ids=("FM-15",),
                   summary="Free cash flow after stock based compensation by period: operating cash flow - capex - SBC.")
def fcf_after_sbc(statements: Any, as_of: Any = None):
    return _single("fundamentals.fcf_after_sbc", "fcf_after_sbc", statements, as_of, ("operating_cash_flow", "capex", "sbc"),
                   fcf_after_sbc_series, ("FM-15",), "usd", "operating cash flow - capex - SBC")


@registry.register("fundamentals.roic_simple", module="fundamentals", inputs=_IN, piece_ids=("FM-16",),
                   summary="Simplified ROIC by period: NOPAT over total equity plus total debt at the closing balance (convention in the docs).")
def roic_simple(statements: Any, as_of: Any = None):
    return _single("fundamentals.roic_simple", "roic_simple", statements, as_of, ("operating_income", "effective_tax_rate", "invested_capital"),
                   roic_simple_series, ("FM-16",), "ratio", "NOPAT / (equity + debt), closing balance")


@registry.register("fundamentals.shareholder_yield", module="fundamentals", inputs=_IN, piece_ids=("FT-006",),
                   summary="Shareholder yield by period: dividend yield + buyback yield over the same market capitalization.")
def shareholder_yield(statements: Any, as_of: Any = None):
    return _single("fundamentals.shareholder_yield", "shareholder_yield", statements, as_of, ("dividend_yield", "buyback_yield"),
                   shareholder_yield_series, ("FT-006",), "ratio", "dividend yield + buyback yield")


# ---------------------------------------------------------------------------------------------------------------------
# FM-18: trailing twelve months (the routine of SVC-data, ED-026, is reused)
# ---------------------------------------------------------------------------------------------------------------------
FACT_COLUMNS = ("period_start", "period_end", "value", "filed", "fiscal_year", "fiscal_period")


def _day(value: Any) -> date | None:
    if value is None or (isinstance(value, float) and np.isnan(value)) or value is pd.NaT:
        return None
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return None


@registry.register("fundamentals.ttm", module="fundamentals", piece_ids=("FM-18",),
                   inputs={"facts": {"type": "dataframe", "required": True}, "as_of": {"type": "date", "required": True},
                           "concept": {"type": "str", "required": False, "default": "us-gaap:Revenues"},
                           "unit": {"type": "str", "required": False, "default": "USD"}},
                   summary="Trailing twelve months of one concept: periods classified by duration, Q4 derived as the year minus nine months, no gaps, and a window that ends more than 185 days before the cut-off is refused.")
def ttm(facts: Any, as_of: Any, concept: str = "us-gaap:Revenues", unit: str = "USD"):
    name, pieces, method = "fundamentals.ttm", ("FM-18",), "four consecutive quarters, duration classes, 185 day lag"
    if not isinstance(facts, pd.DataFrame):
        raise BadInput(f"facts: expected a DataFrame, got {type(facts).__name__}")
    missing = [col for col in FACT_COLUMNS if col not in facts.columns]
    if missing:
        raise BadInput(f"facts: missing column(s) {missing}")
    if not isinstance(concept, str) or not _CONCEPT.fullmatch(concept):
        raise BadInput("concept must be a taxonomy name such as us-gaap:Revenues")
    if not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9_/]{1,20}", unit):
        raise BadInput("unit must be a short unit name such as USD")
    cut = _day(as_of)
    if cut is None:
        raise BadInput(f"as_of must be a date, got {as_of!r}")
    if "share" in unit.lower():
        return c.empty(name, "usd", method, "per_share_ttm_is_not_supported", piece_ids=pieces)
    rows = []
    warns: list[str] = []
    for r in facts.itertuples(index=False):
        end, filed, val = _day(r.period_end), _day(r.filed), r.value
        if end is None or filed is None or val is None or (isinstance(val, float) and np.isnan(val)):
            warns.append("a fact without period end, filing date or value was ignored")
            continue
        fiscal_year = 0 if pd.isna(r.fiscal_year) else int(r.fiscal_year)
        form = "10-K" if str(r.fiscal_period) == "FY" else "10-Q"
        rows.append(_lib.fact(concept, unit, float(val), _day(r.period_start), end, filed, fiscal_year, str(r.fiscal_period), form))
    if not rows:
        return c.empty(name, "usd", method, tuple(dict.fromkeys(warns)) + ("no usable facts",), piece_ids=pieces)
    try:
        data = _lib.sec_ttm(rows, as_of=cut, filed_by=cut)
    except _lib.SecTtmError:
        return c.empty(name, "usd", method, tuple(dict.fromkeys(warns)) + ("no_gap_free_four_quarter_window",), piece_ids=pieces)
    lag = (cut - data["window_end"]).days
    if lag > STALE_DAYS:
        return c.empty(name, "usd", method, tuple(dict.fromkeys(warns)) + (f"ttm_window_ends_{lag}_days_before_as_of",), piece_ids=pieces,
                       params={"window_end": data["window_end"].isoformat()})
    value = {"value": data["value"], "window_end": data["window_end"].isoformat(), "periods": [f"{y}-{p}" for y, p in data["periods"]],
             "has_calculated_q4": bool(data["has_calculated_q4"]), "lag_days": lag}
    extra = (data["warning"],) if data["warning"] else ()
    return c.build(name, value, unit="usd", method=method, params={"concept": concept, "unit": unit, "stale_days": STALE_DAYS},
                   piece_ids=pieces, inputs=(facts.to_csv(index=False),), warnings=tuple(dict.fromkeys(warns)) + extra, as_of=cut)

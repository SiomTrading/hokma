"""Cost of capital: cost of equity (CAPM), cost of debt, corporate tax rate and WACC (FT-014)."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _common as c
from hokma.quant.fundamentals import _lib

PIECES = ("FT-014",)


def _rate(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise BadInput(f"{label} must be a finite number, got {value!r}")
    return float(value)


@registry.register("fundamentals.cost_of_equity", module="fundamentals", piece_ids=PIECES,
                   inputs={"risk_free_rate": {"type": "float", "required": True}, "beta": {"type": "float", "required": True},
                           "market_return": {"type": "float", "required": True}},
                   summary="Cost of equity by CAPM: risk free rate + beta x (market return - risk free rate). Rates as fractions per year.")
def cost_of_equity(risk_free_rate: float, beta: float, market_return: float):
    name, method = "fundamentals.cost_of_equity", "capm"
    rf, b, rm = _rate(risk_free_rate, "risk_free_rate"), _rate(beta, "beta"), _rate(market_return, "market_return")
    params = {"risk_free_rate": rf, "beta": b, "market_return": rm}
    if not (-0.5 < rf < 1.0 and -0.5 < rm < 1.0):
        return c.empty(name, "ratio", method, "rates must be fractions per year between -0.5 and 1", params=params, piece_ids=PIECES)
    value = float(_lib.ft_performance.get_capital_asset_pricing_model(rf, b, rm))
    return c.build(name, {"cost_of_equity": value}, unit="ratio", method=method, params=params, piece_ids=PIECES)


@registry.register("fundamentals.cost_of_debt", module="fundamentals", piece_ids=PIECES,
                   inputs={"statements": c.stmt_input(), "as_of": c.AS_OF_INPUT},
                   summary="Cost of debt by period: interest expense over total debt (zero interest against debt is not reported, so it gives no value) and the corporate tax rate.")
def cost_of_debt(statements: Any, as_of: Any = None):
    name, method = "fundamentals.cost_of_debt", "interest expense / total debt"
    items, warns = c.prepare(statements, as_of)
    ie, debt, tax = items.get("interest_expense"), items.get("total_debt"), items.get("effective_tax_rate")
    miss = [n for n, s in (("interest_expense", ie), ("total_debt", debt), ("effective_tax_rate", tax)) if s is None]
    if len(items.frame) == 0 or miss:
        gaps = tuple(f"data_missing:{m}" for m in miss) or ("no periods in the statements",)
        return c.empty(name, "ratio", method, warns + gaps, piece_ids=PIECES)
    pre_tax = _lib.ft_wacc.get_cost_of_debt(ie, debt).replace([np.inf, -np.inf], np.nan)
    if not pre_tax.notna().any():
        return c.empty(name, "ratio", method, warns + ("interest_expense_of_zero_against_debt_is_not_reported",), piece_ids=PIECES)
    rate = _lib.ft_wacc.get_corporate_tax_rate(items.get("income_tax_expense"), items.get("income_before_tax"))
    table = c.series_table({"cost_of_debt": pre_tax, "tax_rate": rate, "after_tax_cost_of_debt": pre_tax * (1.0 - rate)})
    return c.build(name, table, unit="ratio", method=method, piece_ids=PIECES, inputs=(items.frame,), warnings=warns + tuple(items.notes),
                   as_of=items.frame.index[-1])


@registry.register("fundamentals.wacc", module="fundamentals", piece_ids=PIECES,
                   inputs={"statements": c.stmt_input(), "risk_free_rate": {"type": "float", "required": True}, "beta": {"type": "float", "required": True},
                           "market_return": {"type": "float", "required": True}, "as_of": c.AS_OF_INPUT},
                   summary="Weighted average cost of capital by period: market capitalization and total debt as weights, CAPM cost of equity, cost of debt after tax. Without debt the WACC is the cost of equity.")
def wacc(statements: Any, risk_free_rate: float, beta: float, market_return: float, as_of: Any = None):
    name, method = "fundamentals.wacc", "wacc, market value weights"
    rf, b, rm = _rate(risk_free_rate, "risk_free_rate"), _rate(beta, "beta"), _rate(market_return, "market_return")
    params = {"risk_free_rate": rf, "beta": b, "market_return": rm}
    items, warns = c.prepare(statements, as_of)
    cap, debt, ie = items.get("market_cap"), items.get("total_debt"), items.get("interest_expense")
    tax = items.get("effective_tax_rate")
    miss = [n for n, s in (("market_cap", cap), ("total_debt", debt), ("interest_expense", ie), ("effective_tax_rate", tax)) if s is None]
    if len(items.frame) == 0 or miss:
        gaps = tuple(f"data_missing:{m}" for m in miss) or ("no periods in the statements",)
        return c.empty(name, "ratio", method, warns + gaps, params=params, piece_ids=PIECES)
    coe = float(_lib.ft_performance.get_capital_asset_pricing_model(rf, b, rm))
    total = cap + debt
    equity_weight, debt_weight = cap / total, debt / total
    pre_tax = _lib.ft_wacc.get_cost_of_debt(ie, debt).replace([np.inf, -np.inf], np.nan)
    without_debt = debt == 0
    pre_tax = pre_tax.mask(without_debt & pre_tax.isna(), 0.0)
    after_tax_debt = (debt_weight * pre_tax * (1.0 - tax)).mask(without_debt, 0.0)
    value = equity_weight * coe + after_tax_debt
    table = c.series_table({"wacc": value, "equity_weight": equity_weight, "debt_weight": debt_weight, "cost_of_debt": pre_tax, "tax_rate": tax})
    table["cost_of_equity"] = coe
    return c.build(name, table, unit="ratio", method=method, params=params, piece_ids=PIECES, inputs=(items.frame,),
                   warnings=warns + tuple(items.notes), as_of=items.frame.index[-1])

"""Ratio families: profitability, liquidity, efficiency, solvency and valuation (FT-001 to FT-005) and the reader of
formulas with its documented convention (FT-006).

Each family runs every function of its library model whose inputs the statements hold. A ratio that cannot be built is
listed in `value["skipped"]` with the missing lines: it is never filled. Conventions: docs/quant/fundamentals-conventions.md.
"""
from __future__ import annotations

import inspect
from types import ModuleType
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _common as c
from hokma.quant.fundamentals import _lib

# Library functions left out on purpose, with the reason.
EXCLUDED = {
    "get_weighted_dividend_yield": "the library divides dividends paid, which carry a negative sign: the yield comes out negative; use dividend_yield",
    "get_basket_market_cap": "needs a panel of companies",
    "get_market_cap_to_gdp": "needs the gross domestic product",
    "get_cyclically_adjusted_price_to_earnings_ratio": "needs ten years of prices and a price index",
    "get_estimated_eps_growth_rate": "needs analyst estimates",
    "get_degree_of_leverage": "needs a result and a driver chosen by the caller",
    "get_cash_conversion_efficiency": "the library function takes no period alignment",
}
# Different units per function in the library: per function the series that matches what it expects.
OVERRIDES = {
    "get_dividend_yield": {"dividends": "dividends_per_share"},
    "get_price_to_earnings_growth_ratio": {"growth_rate": "eps_growth_pct"},          # percent points: PEG below 1 is cheap
    "get_dividend_adjusted_price_to_earnings_growth_ratio": {"growth_rate": "eps_growth"},   # a fraction: the library multiplies by 100
}
# Ratios that are meaningless when the denominator is a loss or zero: masked, never negative.
POSITIVE_ONLY = {"get_price_to_earnings_ratio"}

_MODELS: dict[str, tuple[ModuleType, tuple[str, ...]]] = {
    "profitability": (_lib.ft_profitability, ("FT-001",)),
    "liquidity": (_lib.ft_liquidity, ("FT-002",)),
    "efficiency": (_lib.ft_efficiency, ("FT-003",)),
    "solvency": (_lib.ft_solvency, ("FT-004",)),
    "valuation": (_lib.ft_valuation, ("FT-005",)),
}


def _functions(module: ModuleType) -> dict[str, Any]:
    return {n[4:]: f for n, f in inspect.getmembers(module, inspect.isfunction)
            if n.startswith("get_") and f.__module__ == module.__name__ and n not in EXCLUDED}


def ratio_names(family: str) -> tuple[str, ...]:
    """The ratio names a family can compute (a statements frame may hold fewer inputs)."""
    return tuple(sorted(_functions(_MODELS[family][0])))


def compute_ratios(items: c.Items, family: str, only: list[str] | None = None) -> tuple[dict[str, pd.Series], dict[str, list[str]]]:
    functions = _functions(_MODELS[family][0])
    if only is not None:
        unknown = [n for n in only if n not in functions]
        if unknown:
            raise BadInput(f"unknown ratio(s) for {family}: {unknown}")
    out: dict[str, pd.Series] = {}
    skipped: dict[str, list[str]] = {}
    for name, fn in functions.items():
        if only is not None and name not in only:
            continue
        result, missing = items.call(fn, OVERRIDES.get(f"get_{name}"))
        if result is None or not isinstance(result, pd.Series):
            skipped[name] = missing or ["not a series"]
            continue
        if f"get_{name}" in POSITIVE_ONLY:
            guard = items.get("eps")
            if guard is not None:
                result = result.where(guard > 0)
        result = result.replace([np.inf, -np.inf], np.nan)
        if not result.notna().any():
            skipped[name] = ["no finite value"]
            continue
        out[name] = result.astype(float)
    return out, skipped


def _family(family: str, statements: Any, as_of: Any, ratios: list[str] | None) -> Any:
    name = f"fundamentals.{family}"
    pieces = _MODELS[family][1]
    method = f"{family} ratio family, closing balances averaged over two periods where a ratio asks for averages"
    items, warns = c.prepare(statements, as_of)
    if ratios is not None and (not isinstance(ratios, (list, tuple)) or not all(isinstance(r, str) for r in ratios)):
        raise BadInput("ratios must be a list of ratio names")
    if len(items.frame) == 0:
        return c.empty(name, "ratio", method, warns + ("no periods in the statements",), piece_ids=pieces)
    out, skipped = compute_ratios(items, family, list(ratios) if ratios is not None else None)
    if not out:
        return c.empty(name, "ratio", method, warns + ("no ratio could be built",) + tuple(f"data_missing:{m}" for m in sorted({x for v in skipped.values() for x in v})[:20]),
                       params={"skipped": skipped}, piece_ids=pieces)
    table = c.series_table(out)
    table["skipped"] = skipped
    notes = tuple(items.notes)
    return c.build(name, table, unit="ratio", method=method, params={"family": family, "n_ratios": len(out)}, piece_ids=pieces,
                   inputs=(items.frame,), warnings=warns + notes, as_of=items.frame.index[-1] if len(items.frame) else None)


_IN = {"statements": c.stmt_input(), "as_of": c.AS_OF_INPUT, "ratios": {"type": "list[str]", "required": False, "default": None}}


@registry.register("fundamentals.profitability", module="fundamentals", inputs=_IN, piece_ids=("FT-001",),
                   summary="Profitability ratios from the statements (margins, returns, tax rates, income quality); the latest value and the series by period.")
def profitability(statements: Any, as_of: Any = None, ratios: list[str] | None = None):
    return _family("profitability", statements, as_of, ratios)


@registry.register("fundamentals.liquidity", module="fundamentals", inputs=_IN, piece_ids=("FT-002",),
                   summary="Liquidity ratios from the statements (current, quick, cash, operating cash flow ratios, working capital).")
def liquidity(statements: Any, as_of: Any = None, ratios: list[str] | None = None):
    return _family("liquidity", statements, as_of, ratios)


@registry.register("fundamentals.efficiency", module="fundamentals", inputs=_IN, piece_ids=("FT-003",),
                   summary="Efficiency ratios from the statements (turnover, days outstanding, expense ratios, capex intensity).")
def efficiency(statements: Any, as_of: Any = None, ratios: list[str] | None = None):
    return _family("efficiency", statements, as_of, ratios)


@registry.register("fundamentals.solvency", module="fundamentals", inputs=_IN, piece_ids=("FT-004",),
                   summary="Solvency ratios from the statements (debt to equity, coverage, leverage, net debt to EBITDA).")
def solvency(statements: Any, as_of: Any = None, ratios: list[str] | None = None):
    return _family("solvency", statements, as_of, ratios)


@registry.register("fundamentals.valuation", module="fundamentals", inputs=_IN, piece_ids=("FT-005",),
                   summary="Valuation ratios from the statements and the price (P/E, P/B, EV multiples, yields, PEG); a loss gives no P/E.")
def valuation(statements: Any, as_of: Any = None, ratios: list[str] | None = None):
    return _family("valuation", statements, as_of, ratios)


# ---------------------------------------------------------------------------------------------------------------------
# FT-006: the controller of ratios and the formulas that are read, with the convention written down
# ---------------------------------------------------------------------------------------------------------------------
CONVENTIONS: dict[str, str] = {
    "periods": "One row per fiscal period, oldest first. Averages use the closing balance of the period and of the one before; the first period has none and stays empty.",
    "signs": "Cash flow lines keep the sign of the statement: capex, dividends paid and repurchases are negative, issuance is positive. Formulas that need a magnitude take the absolute value.",
    "missing": "A line that is not reported is a missing column or NaN, never zero. Only minority interest and preferred equity are optional in sums, and the result says assumed_zero.",
    "enterprise_value": "Market capitalization + total debt + minority interest + preferred equity - cash and short term investments, at the closing balance.",
    "buyback_yield": "(abs(repurchases) - issuance) / market capitalization. The library reads repurchases as negative; this form gives the same value for that sign and does not flip when a source reports them as positive.",
    "shareholder_yield": "Dividend yield + buyback yield, both over the same market capitalization.",
    "fcf_after_sbc": "Operating cash flow - abs(capex) - stock based compensation.",
    "roic_simple": "NOPAT / (total equity + total debt) at the closing balance. NOPAT = operating income x (1 - effective tax rate), with the rate clipped to [0, 1] and zero for a pre tax loss. The library averages the capital over two periods and does not deduct cash: the difference is the denominator only.",
    "peg": "P/E divided by EPS growth in percent points: below 1 is cheap. The dividend adjusted form of the library takes growth as a fraction and multiplies by 100.",
    "price_to_earnings": "Price / EPS; a loss (EPS <= 0) gives no P/E (the library returns a negative number).",
}


def _roic_library(items: c.Items) -> pd.Series | None:
    result, _ = items.call(_lib.ft_profitability.get_return_on_invested_capital)
    return result


@registry.register("fundamentals.read_formulas", module="fundamentals",
                   inputs={"statements": c.stmt_input(), "as_of": c.AS_OF_INPUT}, piece_ids=("FT-006",),
                   summary="The formulas that are read (simplified ROIC, enterprise value, buyback yield, shareholder yield, FCF after SBC) with the written convention, the value of this module and the value of the library, and why they differ.")
def read_formulas(statements: Any, as_of: Any = None):
    from hokma.quant.fundamentals import formulas

    name, pieces = "fundamentals.read_formulas", ("FT-006",)
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return c.empty(name, "mixed", "convention reader", warns + ("no periods in the statements",), piece_ids=pieces)
    ours = {
        "roic_simple": formulas.roic_simple_series(items),
        "enterprise_value": items.get("enterprise_value"),
        "buyback_yield": items.get("buyback_yield"),
        "shareholder_yield": formulas.shareholder_yield_series(items),
        "fcf_after_sbc": formulas.fcf_after_sbc_series(items),
    }
    library = {"roic_simple": _roic_library(items), "enterprise_value": formulas.enterprise_value_library(items),
               "buyback_yield": items.call(_lib.ft_valuation.get_buyback_yield)[0],
               "shareholder_yield": formulas.shareholder_yield_library(items), "fcf_after_sbc": items.call(_lib.ft_valuation.get_sbc_adjusted_free_cash_flow)[0]}
    explained = {
        "roic_simple": "The library averages equity plus debt over two periods; this module uses the closing balance. The numerator is the same.",
        "enterprise_value": "Same formula. The library takes cash only; this module deducts cash and short term investments.",
        "buyback_yield": "Same value while repurchases are negative; this module also reads positive repurchases as outflows.",
        "shareholder_yield": "Same formula, built from the two yields above.",
        "fcf_after_sbc": "Same formula; the library takes a free cash flow already net of capex with the sign of the statement.",
    }
    table = {}
    for key in ours:
        o, l = ours[key], library[key]
        table[key] = {"module": c.latest_value(o), "library": c.latest_value(l), "difference_explained": explained[key]}
    value = {"conventions": CONVENTIONS, "comparison": table}
    return c.build(name, value, unit="mixed", method="convention reader", piece_ids=pieces, inputs=(items.frame,),
                   warnings=warns + tuple(items.notes), as_of=items.frame.index[-1])

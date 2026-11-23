"""Value creation and intrinsic value: EVA and MVA (FT-015), intrinsic value models (FT-016), enterprise value breakdown and Tobin's Q
(FT-017), growth rates and PVGO (FT-018). Models that diverge (the discount rate at or below the growth rate) return an empty result.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import limits, registry
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _common as c
from hokma.quant.fundamentals import _lib

_IN = {"statements": c.stmt_input(), "as_of": c.AS_OF_INPUT}
MAX_PROJECTION_YEARS = 100


def _num(value: Any, label: str, *, low: float | None = None, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise BadInput(f"{label} must be a finite number, got {value!r}")
    f = float(value)
    if (low is not None and f < low) or (high is not None and f > high):
        raise BadInput(f"{label}={f} is outside [{low}, {high}]")
    return f


@registry.register("fundamentals.eva", module="fundamentals", piece_ids=("FT-015",),
                   inputs={**_IN, "wacc": {"type": "float", "required": True}},
                   summary="Economic value added by period: NOPAT - WACC x invested capital, with NOPAT, invested capital and market value added (market capitalization + debt - invested capital).")
def eva(statements: Any, wacc: float, as_of: Any = None):
    name, pieces, method = "fundamentals.eva", ("FT-015",), "nopat - wacc x invested capital"
    rate = _num(wacc, "wacc", low=0.0, high=1.0)
    items, warns = c.prepare(statements, as_of)
    nopat, capital = items.get("nopat"), items.get("invested_capital")
    miss = [n for n, s in (("operating_income", items.get("operating_income")), ("effective_tax_rate", items.get("effective_tax_rate")),
                           ("total_equity", items.get("total_equity")), ("total_debt", items.get("total_debt"))) if s is None]
    if len(items.frame) == 0 or nopat is None or capital is None:
        gaps = tuple(f"data_missing:{m}" for m in miss) or ("no periods in the statements",)
        return c.empty(name, "usd", method, warns + gaps, params={"wacc": rate}, piece_ids=pieces)
    nopat = _lib.ft_eva.get_net_operating_profit_after_taxes(items.get("operating_income"), items.get("effective_tax_rate"))
    capital = _lib.ft_eva.get_invested_capital(items.get("total_equity"), items.get("total_debt"))
    out = {"nopat": nopat, "invested_capital": capital, "eva": _lib.ft_eva.get_economic_value_added(nopat, rate, capital)}
    cap = items.get("market_cap")
    if cap is not None:
        out["mva"] = _lib.ft_eva.get_market_value_added(cap, items.get("total_debt"), capital)
    return c.build(name, c.series_table(out), unit="usd", method=method, params={"wacc": rate}, piece_ids=pieces, inputs=(items.frame,),
                   warnings=warns + tuple(items.notes), as_of=items.frame.index[-1])


INTRINSIC_METHODS = ("gordon", "growth_multiple", "defensive_number", "fcff", "fcfe", "ddm_two_stage", "h_model", "residual_income", "dcf")


@registry.register("fundamentals.intrinsic", module="fundamentals", piece_ids=("FT-016",),
                   inputs={"method": {"type": "str", "required": True}, "params": {"type": "dict", "required": True}},
                   summary="Intrinsic value by method: gordon, growth_multiple, defensive_number, fcff, fcfe, ddm_two_stage, h_model, residual_income or dcf. A rate at or below the growth rate gives no value.")
def intrinsic(method: str, params: dict):
    name, pieces = "fundamentals.intrinsic", ("FT-016",)
    if method not in INTRINSIC_METHODS:
        raise BadInput(f"method must be one of {list(INTRINSIC_METHODS)}")
    if not isinstance(params, dict):
        raise BadInput("params must be a dict of numbers")
    p = {str(k): _num(v, f"params.{k}") for k, v in params.items()}
    m = _lib.ft_intrinsic

    def need(*keys: str) -> list[str] | None:
        gone = [k for k in keys if k not in p]
        return gone or None

    spec = {
        "gordon": ("dividends_per_share", "rate_of_return", "growth_rate"),
        "growth_multiple": ("earnings_per_share", "growth_rate", "bond_yield"),
        "defensive_number": ("earnings_per_share", "book_value_per_share"),
        "fcff": ("nopat", "depreciation_and_amortization", "capex", "change_in_net_working_capital"),
        "fcfe": ("net_income", "depreciation_and_amortization", "capex", "change_in_net_working_capital", "net_borrowing"),
        "ddm_two_stage": ("dividends_per_share", "rate_of_return", "high_growth_rate", "stable_growth_rate"),
        "h_model": ("dividends_per_share", "rate_of_return", "high_growth_rate", "stable_growth_rate"),
        "residual_income": ("net_income", "cost_of_equity", "book_value_of_equity"),
        "dcf": ("cash_flow", "growth_rate", "perpetual_growth_rate", "wacc", "cash", "total_debt", "shares_outstanding"),
    }[method]
    gone = need(*spec)
    if gone:
        return c.empty(name, "usd", method, tuple(f"data_missing:{g}" for g in gone), params={"method": method}, piece_ids=pieces)
    periods = limits.as_int("periods", p.get("periods", 5)) if "periods" in p else 5
    if not 1 <= periods <= MAX_PROJECTION_YEARS:
        raise BadInput(f"periods must be between 1 and {MAX_PROJECTION_YEARS}")
    try:
        if method == "gordon":
            value = float(m.get_gordon_growth_model(p["dividends_per_share"], p["rate_of_return"], p["growth_rate"]))
            detail = {"value": value}
        elif method == "growth_multiple":
            value = float(_lib.growth_multiple_value(p["earnings_per_share"], p["growth_rate"], p["bond_yield"]))
            detail = {"value": value}
        elif method == "defensive_number":
            value = float(_lib.defensive_number(pd.Series([p["earnings_per_share"]]), pd.Series([p["book_value_per_share"]])).iloc[0])
            detail = {"value": value}
        elif method == "fcff":
            value = float(m.get_free_cash_flow_to_firm(p["nopat"], p["depreciation_and_amortization"], abs(p["capex"]), p["change_in_net_working_capital"]))
            detail = {"value": value}
        elif method == "fcfe":
            value = float(m.get_free_cash_flow_to_equity(p["net_income"], p["depreciation_and_amortization"], abs(p["capex"]),
                                                         p["change_in_net_working_capital"], p["net_borrowing"]))
            detail = {"value": value}
        elif method == "ddm_two_stage":
            transition = limits.as_int("transition_periods", p.get("transition_periods", 0))
            high = limits.as_int("high_growth_periods", p.get("high_growth_periods", 5))
            if not (0 <= transition <= MAX_PROJECTION_YEARS and 1 <= high <= MAX_PROJECTION_YEARS):
                raise BadInput(f"projection periods must be between 0 and {MAX_PROJECTION_YEARS}")
            frame = m.get_two_stage_dividend_discount_model(p["dividends_per_share"], p["rate_of_return"], p["high_growth_rate"],
                                                            p["stable_growth_rate"], high, transition)
            detail = {k: float(frame.iloc[i, 0]) for i, k in enumerate(frame.index)}
            value = float(frame.loc["Intrinsic Value"].iloc[0])
        elif method == "h_model":
            hp = limits.as_int("high_growth_periods", p.get("high_growth_periods", 10))
            if not 1 <= hp <= MAX_PROJECTION_YEARS:
                raise BadInput(f"high_growth_periods must be between 1 and {MAX_PROJECTION_YEARS}")
            value = float(m.get_h_model(p["dividends_per_share"], p["rate_of_return"], p["high_growth_rate"], p["stable_growth_rate"], hp))
            detail = {"value": value}
        elif method == "residual_income":
            value = float(m.get_residual_income(p["net_income"], p["cost_of_equity"], p["book_value_of_equity"]))
            detail = {"value": value}
        else:
            frame = m.get_intrinsic_value(p["cash_flow"], p["growth_rate"], p["perpetual_growth_rate"], p["wacc"], p["cash"], p["total_debt"],
                                          p["shares_outstanding"], periods, p.get("short_term_investments", 0.0), p.get("minority_interest", 0.0),
                                          p.get("preferred_stock", 0.0))
            detail = {k: float(frame.iloc[i, 0]) for i, k in enumerate(frame.index)}
            value = float(frame.loc["Intrinsic Value"].iloc[0])
    except ZeroDivisionError:
        return c.empty(name, "usd", method, "division by zero in the model", params={"method": method}, piece_ids=pieces)
    except ValueError as exc:
        return c.empty(name, "usd", method, str(exc)[:200], params={"method": method}, piece_ids=pieces)
    if not math.isfinite(value):
        return c.empty(name, "usd", method, "the model has no finite value for these inputs", params={"method": method}, piece_ids=pieces)
    return c.build(name, {"value": value, "detail": detail}, unit="usd", method=method, params={"method": method, **p}, piece_ids=pieces)


@registry.register("fundamentals.ev_breakdown", module="fundamentals", piece_ids=("FT-017",), inputs=_IN,
                   summary="Enterprise value by period broken into its parts (market capitalization, debt, minority interest, preferred equity, cash) and Tobin's Q.")
def ev_breakdown(statements: Any, as_of: Any = None):
    name, pieces, method = "fundamentals.ev_breakdown", ("FT-017",), "market cap + debt + minority + preferred - cash and short term investments"
    items, warns = c.prepare(statements, as_of)
    price, shares, debt, cash = items.get("price"), items.get("shares_outstanding"), items.get("total_debt"), items.get("cash")
    miss = [n for n, s in (("price", price), ("shares_outstanding", shares), ("total_debt", debt), ("cash", cash)) if s is None]
    if len(items.frame) == 0 or miss:
        gaps = tuple(f"data_missing:{m}" for m in miss) or ("no periods in the statements",)
        return c.empty(name, "usd", method, warns + gaps, piece_ids=pieces)
    sti = items.get("short_term_investments")
    frame = _lib.ft_enterprise.get_enterprise_value_breakdown(price, shares, debt, items._optional("minority_interest"),
                                                              items._optional("preferred_equity"), cash, sti)
    out = {str(k): frame.loc[k].astype(float) for k in frame.index}
    q = items.call(_lib.ft_enterprise.get_tobins_q_ratio, {"market_value_of_equity": "market_cap"})[0]
    if q is not None:
        out["Tobin Q"] = q
    return c.build(name, c.series_table(out), unit="usd", method=method, piece_ids=pieces, inputs=(items.frame,),
                   warnings=warns + tuple(items.notes), as_of=items.frame.index[-1])


@registry.register("fundamentals.growth", module="fundamentals", piece_ids=("FT-018",),
                   inputs={**_IN, "cost_of_equity": {"type": "float", "required": False, "default": None}},
                   summary="Sustainable growth rate (ROE x retention), internal growth rate (ROA x retention) and, with a cost of equity, the present value of growth opportunities (price - EPS / cost of equity).")
def growth(statements: Any, as_of: Any = None, cost_of_equity: float | None = None):
    name, pieces, method = "fundamentals.growth", ("FT-018",), "sustainable and internal growth, pvgo"
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return c.empty(name, "ratio", method, warns + ("no periods in the statements",), piece_ids=pieces)
    out: dict[str, pd.Series] = {}
    miss: list[str] = []
    roe, retention, ni, ta = None, items.get("retention_ratio"), items.get("net_income"), items.get("total_assets")
    equity = items.get("total_equity")
    if retention is None:
        miss.extend(["dividends_paid", "net_income"])
    else:
        if ni is not None and equity is not None:
            avg_eq = (equity + equity.shift(1)) / 2.0
            roe = ni / avg_eq
            out["sustainable_growth_rate"] = _lib.ft_growth.get_sustainable_growth_rate(roe, retention)
        else:
            miss.extend(n for n, s in (("net_income", ni), ("total_equity", equity)) if s is None)
        if ni is not None and ta is not None:
            roa = ni / ((ta + ta.shift(1)) / 2.0)
            out["internal_growth_rate"] = _lib.ft_growth.get_internal_growth_rate(roa, retention)
        else:
            miss.extend(n for n, s in (("net_income", ni), ("total_assets", ta)) if s is None)
    params: dict[str, Any] = {}
    if cost_of_equity is not None:
        coe = _num(cost_of_equity, "cost_of_equity", low=0.0001, high=1.0)
        params["cost_of_equity"] = coe
        eps, price = items.get("eps"), items.get("price")
        if eps is None or price is None:
            miss.extend(n for n, s in (("eps", eps), ("price", price)) if s is None)
        else:
            frame_coe = c.wide(pd.Series(coe, index=eps.index))
            pvgo = _lib.ft_growth.get_present_value_of_growth_opportunities(frame_coe, c.wide(eps), pd.DataFrame({"X": price.to_numpy(dtype=float)}, index=list(price.index)))
            out["pvgo"] = pvgo["X"].astype(float)
    out = {k: v.replace([np.inf, -np.inf], np.nan) for k, v in out.items() if v.notna().any()}
    if not out:
        return c.empty(name, "ratio", method, warns + tuple(f"data_missing:{m}" for m in dict.fromkeys(miss)), params=params, piece_ids=pieces)
    return c.build(name, c.series_table(out), unit="ratio", method=method, params=params, piece_ids=pieces, inputs=(items.frame,),
                   warnings=warns + tuple(items.notes), as_of=items.frame.index[-1])

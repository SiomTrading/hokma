"""Bankruptcy and manipulation scores: Altman Z, Beneish M, Piotroski F, Ohlson O, Zmijewski, Springate, Grover and Fulmer H
(FT-007 to FT-012). The formulas are the ones of the library; this module builds their inputs from the statements and
refuses to score a period that lacks any of them.

Traps corrected here: the library counts a criterion whose input is missing as failed (Piotroski), so a period with a missing
input gets no score; a log of a negative ratio is not finite and the Fulmer score of that period stays empty; the Altman
model is chosen by name and each model carries its own cut-offs.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _common as c
from hokma.quant.fundamentals import _lib

_IN = {"statements": c.stmt_input(), "as_of": c.AS_OF_INPUT}

# Published cut-offs: (safe above, distress below). Between them is the grey zone.
ALTMAN_CUTOFFS = {"original": (2.99, 1.81), "private": (2.9, 1.23), "non-manufacturing": (2.6, 1.1), "emerging": (2.6, 1.1)}
SPRINGATE_CUTOFF = 0.862          # below it the firm is classified as failing
GROVER_CUTOFFS = (0.01, -0.02)    # at or above the first: healthy; at or below the second: failing
FULMER_CUTOFF = 0.0               # H below zero: failing


def _comp(items: c.Items, module: Any, fn: str, missing: list[str], overrides: dict[str, str] | None = None) -> pd.Series | None:
    with np.errstate(invalid="ignore", divide="ignore"):
        result, gaps = items.call(getattr(module, fn), overrides)
    if result is None:
        missing.extend(g for g in gaps if g not in missing)
        return None
    return result.replace([np.inf, -np.inf], np.nan)


def _finish(name: str, label: str, series: pd.Series | None, missing: list[str], items: c.Items, warns: tuple[str, ...], pieces: tuple[str, ...],
            method: str, params: dict | None = None, extra: dict | None = None, unit: str = "score"):
    if series is None or not series.notna().any():
        gaps = tuple(f"data_missing:{m}" for m in missing) or ("no period could be scored",)
        return c.empty(name, unit, method, warns + gaps, params=params, piece_ids=pieces)
    table = c.series_table({label: series})
    if extra:
        table.update(extra)
    return c.build(name, table, unit=unit, method=method, params=params, piece_ids=pieces, inputs=(items.frame,),
                   warnings=warns + tuple(items.notes), as_of=items.frame.index[-1])


def _zone(score: float | None, low: float, high: float) -> str | None:
    if score is None:
        return None
    return "safe" if score > high else ("distress" if score < low else "grey")


@registry.register("fundamentals.altman_z", module="fundamentals", piece_ids=("FT-007",),
                   inputs={**_IN, "model": {"type": "str", "required": False, "default": "original"}},
                   summary="Altman Z score by period with its zone; models original (market value of equity), private, non-manufacturing and emerging.")
def altman_z(statements: Any, as_of: Any = None, model: str = "original"):
    name, pieces = "fundamentals.altman_z", ("FT-007",)
    if model not in _lib.ALTMAN_MODELS:
        raise BadInput(f"model must be one of {list(_lib.ALTMAN_MODELS)}")
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return c.empty(name, "score", f"altman {model}", warns + ("no periods in the statements",), piece_ids=pieces)
    miss: list[str] = []
    m = _lib.ft_altman
    x1 = _comp(items, m, "get_working_capital_to_total_assets_ratio", miss)
    x2 = _comp(items, m, "get_retained_earnings_to_total_assets_ratio", miss)
    x3 = _comp(items, m, "get_earnings_before_interest_and_taxes_to_total_assets_ratio", miss)
    if model == "original":
        x4 = _comp(items, m, "get_market_value_of_equity_to_book_value_of_total_liabilities_ratio", miss)
    else:
        x4 = _comp(items, m, "get_book_value_of_equity_to_book_value_of_total_liabilities_ratio", miss, {"book_value_of_equity": "total_equity"})
    uses_sales = model in ("original", "private")
    x5 = _comp(items, m, "get_sales_to_total_assets_ratio", miss) if uses_sales else None
    if any(x is None for x in (x1, x2, x3, x4)) or (uses_sales and x5 is None):
        return _finish(name, "altman_z", None, miss, items, warns, pieces, f"altman {model}", {"model": model})
    score = m.get_altman_z_score(x1, x2, x3, x4, x5, model=model)
    high, low = ALTMAN_CUTOFFS[model][0], ALTMAN_CUTOFFS[model][1]
    last = c.latest_value(score)
    return _finish(name, "altman_z", score, miss, items, warns, pieces, f"altman {model}", {"model": model, "safe_above": high, "distress_below": low},
                   extra={"zone": _zone(last, low, high)})


@registry.register("fundamentals.beneish_m", module="fundamentals", piece_ids=("FT-008",), inputs=_IN,
                   summary="Beneish M score by period (eight indexes against the prior period); above -1.78 flags a likely manipulator.")
def beneish_m(statements: Any, as_of: Any = None):
    name, pieces = "fundamentals.beneish_m", ("FT-008",)
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return c.empty(name, "score", "beneish m", warns + ("no periods in the statements",), piece_ids=pieces)
    miss: list[str] = []
    m = _lib.ft_beneish
    wide = {}
    specs = {"dsri": "get_days_sales_in_receivables_index", "gmi": "get_gross_margin_index", "aqi": "get_asset_quality_index",
             "sgi": "get_sales_growth_index", "depi": "get_depreciation_index", "sgai": "get_selling_general_and_administrative_expenses_index",
             "lvgi": "get_leverage_index", "tata": "get_total_accruals_to_total_assets"}
    shifted = {"dsri", "gmi", "aqi", "sgi", "depi", "sgai", "lvgi"}
    for key, fn in specs.items():
        sig = getattr(m, fn)
        kwargs, ok = {}, True
        import inspect

        for pname, p in inspect.signature(sig).parameters.items():
            if pname == "lag":
                continue
            s = items.arg(pname)
            if s is None:
                ok = False
                miss.append(c.canon(pname))
                continue
            kwargs[pname] = c.wide(s) if key in shifted else s
        if not ok:
            continue
        value = sig(**kwargs)
        wide[key] = c.narrow(value).replace([np.inf, -np.inf], np.nan)
    if len(wide) < len(specs):
        return _finish(name, "beneish_m", None, list(dict.fromkeys(miss)), items, warns, pieces, "beneish m")
    score = m.get_beneish_m_score(wide["dsri"], wide["gmi"], wide["aqi"], wide["sgi"], wide["depi"], wide["sgai"], wide["lvgi"], wide["tata"])
    score = score.replace([np.inf, -np.inf], np.nan)
    last = c.latest_value(score)
    return _finish(name, "beneish_m", score, miss, items, warns, pieces, "beneish m", {"manipulator_above": -1.78},
                   extra={"indexes": {k: v.tolist() for k, v in wide.items()}, "likely_manipulator": None if last is None else bool(last > -1.78)})


PIOTROSKI_INPUTS = ("net_income", "operating_cash_flow", "total_assets", "revenue", "cost_of_revenue", "current_assets", "current_liabilities",
                    "long_term_debt", "issuance")


@registry.register("fundamentals.piotroski_f", module="fundamentals", piece_ids=("FT-009",), inputs=_IN,
                   summary="Piotroski F score by period (nine yes or no tests, 0 to 9). A period that lacks any input, or the assets two periods back, gets no score.")
def piotroski_f(statements: Any, as_of: Any = None):
    name, pieces = "fundamentals.piotroski_f", ("FT-009",)
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return c.empty(name, "points", "piotroski f", warns + ("no periods in the statements",), piece_ids=pieces)
    miss = [n for n in PIOTROSKI_INPUTS if items.get(n) is None]
    if miss:
        return _finish(name, "piotroski_f", None, miss, items, warns, pieces, "piotroski f", unit="points")
    s = {n: items.get(n) for n in PIOTROSKI_INPUTS}
    ta = s["total_assets"]
    # A period is scored only when every input of it, of the period before and the assets two periods back exist.
    now = pd.concat([s[n] for n in PIOTROSKI_INPUTS], axis=1).notna().all(axis=1)
    valid = now & now.shift(1, fill_value=False) & ta.shift(2).notna()
    m = _lib.ft_piotroski
    frame = {n: c.wide(v) for n, v in s.items()}
    beginning = c.wide(ta.shift(1))
    average_assets = c.wide((ta + ta.shift(1)) / 2.0)
    crit = {
        "roa_positive": m.get_return_on_assets_criteria(frame["net_income"], beginning),
        "cash_flow_positive": m.get_operating_cashflow_criteria(frame["operating_cash_flow"]),
        "roa_improves": m.get_change_in_return_on_asset_criteria(frame["net_income"], beginning),
        "cash_flow_above_income": m.get_accruals_criteria(frame["net_income"], frame["operating_cash_flow"], beginning),
        "leverage_falls": m.get_change_in_leverage_criteria(frame["long_term_debt"], average_assets),
        "current_ratio_improves": m.get_change_in_current_ratio_criteria(frame["current_assets"], frame["current_liabilities"]),
        "no_new_shares": m.get_number_of_shares_criteria(frame["issuance"]),
        "gross_margin_improves": m.get_gross_margin_criteria(frame["revenue"], frame["cost_of_revenue"]),
        "asset_turnover_improves": m.get_asset_turnover_ratio_criteria(frame["revenue"], beginning),
    }
    score = m.get_piotroski_score(crit["roa_positive"], crit["cash_flow_positive"], crit["roa_improves"], crit["cash_flow_above_income"],
                                  crit["leverage_falls"], crit["current_ratio_improves"], crit["no_new_shares"], crit["gross_margin_improves"],
                                  crit["asset_turnover_improves"])
    series = c.narrow(score).where(valid).astype(float)
    detail = {k: c.narrow(v).astype(float).where(valid).tolist() for k, v in crit.items()}
    return _finish(name, "piotroski_f", series, miss, items, warns, pieces, "piotroski f", {"strong_at_or_above": 8, "weak_at_or_below": 2},
                   extra={"criteria": detail}, unit="points")


@registry.register("fundamentals.ohlson_o", module="fundamentals", piece_ids=("FT-010",), inputs=_IN,
                   summary="Ohlson O score by period and the probability of bankruptcy (logistic of the score).")
def ohlson_o(statements: Any, as_of: Any = None):
    name, pieces = "fundamentals.ohlson_o", ("FT-010",)
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return c.empty(name, "score", "ohlson o", warns + ("no periods in the statements",), piece_ids=pieces)
    miss: list[str] = []
    m = _lib.ft_ohlson
    ni = items.get("net_income")
    parts = {
        "size": _comp(items, m, "get_log_of_total_assets", miss),
        "tl_ta": _comp(items, m, "get_total_liabilities_to_total_assets_ratio", miss),
        "wc_ta": _comp(items, m, "get_working_capital_to_total_assets_ratio", miss),
        "cl_ca": _comp(items, m, "get_current_liabilities_to_current_assets_ratio", miss),
        "negative_equity": _comp(items, m, "get_negative_equity_indicator", miss),
        "ni_ta": _comp(items, m, "get_net_income_to_total_assets_ratio", miss),
        "ffo_tl": _comp(items, m, "get_funds_from_operations_to_total_liabilities_ratio", miss),
    }
    if ni is None:
        miss.append("net_income")
    if ni is None or any(v is None for v in parts.values()):
        return _finish(name, "ohlson_o", None, miss, items, warns, pieces, "ohlson o")
    neg_income = c.narrow(m.get_negative_income_indicator(c.wide(ni)))
    delta = c.narrow(m.get_change_in_net_income_ratio(c.wide(ni)))
    score = m.get_ohlson_o_score(parts["size"], parts["tl_ta"], parts["wc_ta"], parts["cl_ca"], parts["negative_equity"], parts["ni_ta"],
                                 parts["ffo_tl"], neg_income, delta).astype(float)
    score = score.where(neg_income.notna() & delta.notna())
    probability = m.get_ohlson_bankruptcy_probability(score)
    return _finish(name, "ohlson_o", score, miss, items, warns, pieces, "ohlson o", extra={"bankruptcy_probability": probability.tolist()})


@registry.register("fundamentals.zmijewski", module="fundamentals", piece_ids=("FT-011",), inputs=_IN,
                   summary="Zmijewski score by period and the probability of bankruptcy (normal distribution of the score).")
def zmijewski(statements: Any, as_of: Any = None):
    name, pieces = "fundamentals.zmijewski", ("FT-011",)
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return c.empty(name, "score", "zmijewski", warns + ("no periods in the statements",), piece_ids=pieces)
    miss: list[str] = []
    m = _lib.ft_zmijewski
    a = _comp(items, m, "get_net_income_to_total_assets_ratio", miss)
    b = _comp(items, m, "get_total_liabilities_to_total_assets_ratio", miss)
    d = _comp(items, m, "get_current_assets_to_current_liabilities_ratio", miss)
    if a is None or b is None or d is None:
        return _finish(name, "zmijewski", None, miss, items, warns, pieces, "zmijewski")
    score = m.get_zmijewski_score(a, b, d)
    probability = m.get_zmijewski_bankruptcy_probability(score)
    return _finish(name, "zmijewski", score, miss, items, warns, pieces, "zmijewski", extra={"bankruptcy_probability": pd.Series(probability).tolist()})


@registry.register("fundamentals.springate_grover_fulmer", module="fundamentals", piece_ids=("FT-012",), inputs=_IN,
                   summary="Springate S, Grover G and Fulmer H scores by period with their published cut-offs; each one that cannot be built is listed as skipped.")
def springate_grover_fulmer(statements: Any, as_of: Any = None):
    name, pieces = "fundamentals.springate_grover_fulmer", ("FT-012",)
    items, warns = c.prepare(statements, as_of)
    if len(items.frame) == 0:
        return c.empty(name, "score", "springate grover fulmer", warns + ("no periods in the statements",), piece_ids=pieces)
    out: dict[str, pd.Series] = {}
    skipped: dict[str, list[str]] = {}
    miss: list[str] = []
    sp = _lib.ft_springate
    parts = [_comp(items, sp, "get_working_capital_to_total_assets_ratio", miss), _comp(items, sp, "get_ebit_to_total_assets_ratio", miss),
             _comp(items, sp, "get_ebt_to_current_liabilities_ratio", miss), _comp(items, sp, "get_sales_to_total_assets_ratio", miss)]
    if all(p is not None for p in parts):
        out["springate"] = sp.get_springate_score(*parts)
    else:
        skipped["springate"] = list(dict.fromkeys(miss))
    miss = []
    gr = _lib.ft_grover
    parts = [_comp(items, gr, "get_working_capital_to_total_assets_ratio", miss), _comp(items, gr, "get_ebit_to_total_assets_ratio", miss),
             _comp(items, gr, "get_return_on_assets_ratio", miss)]
    if all(p is not None for p in parts):
        out["grover"] = gr.get_grover_score(*parts)
    else:
        skipped["grover"] = list(dict.fromkeys(miss))
    miss = []
    fu = _lib.ft_fulmer
    parts = [_comp(items, fu, "get_retained_earnings_to_total_assets_ratio", miss), _comp(items, fu, "get_sales_to_total_assets_ratio", miss),
             _comp(items, fu, "get_ebt_to_equity_ratio", miss), _comp(items, fu, "get_cash_flow_to_total_liabilities_ratio", miss),
             _comp(items, fu, "get_debt_to_total_assets_ratio", miss), _comp(items, fu, "get_current_liabilities_to_total_assets_ratio", miss),
             _comp(items, fu, "get_log_of_tangible_total_assets", miss), _comp(items, fu, "get_working_capital_to_total_liabilities_ratio", miss),
             _comp(items, fu, "get_log_of_ebit_to_interest_expense_ratio", miss)]
    if all(p is not None for p in parts):
        out["fulmer_h"] = fu.get_fulmer_h_score(*parts).replace([np.inf, -np.inf], np.nan)
    else:
        skipped["fulmer_h"] = list(dict.fromkeys(miss))
    out = {k: v for k, v in out.items() if v.notna().any()}
    if not out:
        gaps = tuple(f"data_missing:{x}" for x in sorted({y for v in skipped.values() for y in v}))
        return c.empty(name, "score", "springate grover fulmer", warns + gaps, piece_ids=pieces)
    table = c.series_table(out)
    table["skipped"] = skipped
    last = table["latest"]
    table["failing"] = {"springate": None if last.get("springate") is None else bool(last["springate"] < SPRINGATE_CUTOFF),
                        "grover": None if last.get("grover") is None else bool(last["grover"] <= GROVER_CUTOFFS[1]),
                        "fulmer_h": None if last.get("fulmer_h") is None else bool(last["fulmer_h"] < FULMER_CUTOFF)}
    return c.build(name, table, unit="score", method="springate grover fulmer", piece_ids=pieces, inputs=(items.frame,),
                   params={"springate_cutoff": SPRINGATE_CUTOFF, "grover_cutoffs": list(GROVER_CUTOFFS), "fulmer_cutoff": FULMER_CUTOFF},
                   warnings=warns + tuple(items.notes), as_of=items.frame.index[-1])

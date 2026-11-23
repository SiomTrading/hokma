"""Asset descriptors (SK-090 to SK-102): value, earnings yield, dividend yield, profitability, growth, leverage, earnings quality, momentum and
reversal, volatility, sensitivity, liquidity and the rest. Each one is a registered function over a panel of wide frames (dates by assets)
named as the fields the descriptor reads. The panel is built by `panel.build_panel`: inactive assets are cleared, never filled.

A descriptor that lacks a field returns an empty result that names the field; a library validation error comes back as a warning.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _common as c
from hokma.quant.fundamentals import _lib
from hokma.quant.fundamentals.panel import build_panel

MAX_WHOLE = 100_000
MAX_HALF_LIFE = 10_000.0
FIELD_NAME_LIMIT = 40


@dataclass(frozen=True)
class Spec:
    public: str                                   # registry suffix, for example "book_to_price"
    cls: str                                      # class of the library
    piece: str
    fields: tuple[str, ...]                       # panel fields that are always needed
    params: dict[str, tuple[str, Any]] = field(default_factory=dict)   # name -> (kind, default)
    extra: tuple[str, ...] = ()                   # inputs that are not panel fields (reference_returns)
    field_params: tuple[str, ...] = ()            # parameters that name a panel field
    summary: str = ""


_W = {"half_life": ("pos_float", None), "min_periods": ("whole_or_none", None)}


def _s(public: str, cls: str, piece: str, fields: tuple[str, ...], summary: str, **kw: Any) -> Spec:
    return Spec(public, cls, piece, fields, summary=summary, **kw)


SPECS: tuple[Spec, ...] = (
    _s("book_to_price", "BookToPrice", "SK-091", ("book_equity", "market_cap"), "Book equity over market capitalization (value)."),
    _s("cash_flow_to_price", "CashFlowToPrice", "SK-091", ("operating_cash_flow_ttm", "market_cap"), "Operating cash flow (TTM) over market capitalization."),
    _s("sales_to_price", "SalesToPrice", "SK-091", ("sales_ttm", "market_cap"), "Sales (TTM) over market capitalization."),
    _s("earnings_to_price", "EarningsToPrice", "SK-092", ("net_income_ttm", "market_cap"), "Net income (TTM) over market capitalization (earnings yield)."),
    _s("ebitda_to_enterprise_value", "EbitdaToEnterpriseValue", "SK-092", ("ebitda_ttm", "enterprise_value"), "EBITDA (TTM) over enterprise value."),
    _s("forward_earnings_to_price", "ForwardEarningsToPrice", "SK-092", ("eps_ntm", "adj_close"), "Forward EPS (next twelve months) over price."),
    _s("dividend_to_price", "DividendToPrice", "SK-093", ("dividends_ttm", "market_cap"), "Dividends (TTM) over market capitalization."),
    _s("forward_dividend_to_price", "ForwardDividendToPrice", "SK-093", ("dps_ntm", "adj_close"), "Forward dividend per share over price."),
    _s("shareholder_yield_descriptor", "ShareholderYield", "SK-093", ("dividends_ttm", "net_buybacks_ttm", "market_cap"), "Dividends plus net buybacks (TTM) over market capitalization."),
    _s("gross_margin_descriptor", "GrossMargin", "SK-094", ("sales_ttm", "cost_of_revenue_ttm"), "Gross margin from sales and cost of revenue (TTM)."),
    _s("gross_profitability", "GrossProfitability", "SK-094", ("sales_ttm", "cost_of_revenue_ttm", "total_assets"), "Gross profit over total assets."),
    _s("return_on_assets_descriptor", "ReturnOnAssets", "SK-094", ("net_income_ttm", "total_assets"), "Net income (TTM) over total assets."),
    _s("return_on_equity_descriptor", "ReturnOnEquity", "SK-094", ("net_income_ttm", "book_equity"), "Net income (TTM) over book equity."),
    _s("cash_flow_to_assets", "CashFlowToAssets", "SK-094", ("operating_cash_flow_ttm", "total_assets"), "Operating cash flow (TTM) over total assets."),
    _s("asset_turnover_descriptor", "AssetTurnover", "SK-094", ("sales_ttm", "total_assets"), "Sales (TTM) over total assets."),
    _s("sales_to_enterprise_value", "SalesToEnterpriseValue", "SK-094", ("sales_ttm", "enterprise_value"), "Sales (TTM) over enterprise value."),
    _s("growth_rate", "GrowthRate", "SK-095", (), "Growth of a named field over a lag of observations.", params={"lag": ("lag", None)},
       field_params=("field",)),
    _s("sales_growth_rate", "SalesGrowthRate", "SK-095", ("sales_ttm",), "Growth of sales (TTM) over a lag of observations.", params={"lag": ("lag", 252)}),
    _s("assets_growth_rate", "AssetsGrowthRate", "SK-095", ("total_assets",), "Growth of total assets over a lag of observations.", params={"lag": ("lag", 252)}),
    _s("issuance_growth_rate", "IssuanceGrowthRate", "SK-095", ("adj_shares_outstanding",), "Growth of the adjusted share count over a lag.", params={"lag": ("lag", 252)}),
    _s("earnings_change_to_price", "EarningsChangeToPrice", "SK-095", ("net_income_ttm", "market_cap"), "Change in net income (TTM) over market capitalization.", params={"lag": ("lag", 252)}),
    _s("change_in_intensity", "ChangeInIntensity", "SK-095", (), "Change of a field over a scale field (intensity) across a lag.", params={"lag": ("lag", None)},
       field_params=("field", "scale_field")),
    _s("change_to_scale", "ChangeToScale", "SK-095", (), "Change of a field divided by a scale field across a lag.", params={"lag": ("lag", None)},
       field_params=("field", "scale_field")),
    _s("capex_to_assets_change_in_intensity", "CapexToAssetsChangeInIntensity", "SK-095", ("capex_ttm", "total_assets"), "Change in capex over assets.", params={"lag": ("lag", 252)}),
    _s("book_leverage", "BookLeverage", "SK-096", ("total_debt", "book_equity"), "Total debt over book equity."),
    _s("market_leverage", "MarketLeverage", "SK-096", ("total_debt", "market_cap"), "Total debt over market capitalization."),
    _s("debt_to_assets", "DebtToAssets", "SK-096", ("total_debt", "total_assets"), "Total debt over total assets."),
    _s("accruals_cash_flow", "AccrualsCashFlow", "SK-097", ("net_income_ttm", "operating_cash_flow_ttm", "total_assets"), "Accruals (net income minus operating cash flow) over total assets."),
    _s("analyst_dispersion_to_price", "AnalystDispersionToPrice", "SK-097", ("eps_ntm_std", "adj_close"), "Dispersion of analyst EPS estimates over price."),
    _s("ew_momentum", "EWMomentum", "SK-098", ("returns",), "Exponentially weighted momentum of log returns, skipping the latest observations.",
       params={"half_life": ("pos_float", 87.0), "skip": ("skip", 21), "min_periods": ("whole_or_none", None), "exponentiate": ("bool", False)}),
    _s("rolling_momentum", "RollingMomentum", "SK-098", ("returns",), "Cumulative log return over a window, skipping the latest observations.",
       params={"window": ("window", 252), "skip": ("skip", 21), "exponentiate": ("bool", False)}),
    _s("reversal", "Reversal", "SK-098", ("returns",), "Short term reversal: minus the cumulative log return of a window.", params={"window": ("window", 21)}),
    _s("ew_volatility", "EWVolatility", "SK-099", ("returns",), "Exponentially weighted volatility.", params={**_W, "half_life": ("pos_float", 40.0)}),
    _s("ew_downside_volatility", "EWDownsideVolatility", "SK-099", ("returns",), "Exponentially weighted downside volatility.",
       params={**_W, "half_life": ("pos_float", 40.0), "min_acceptable_return": ("float", 0.0)}),
    _s("ew_residual_volatility", "EWResidualVolatility", "SK-099", ("returns", "market_cap"), "Exponentially weighted volatility of returns net of the market.",
       params={**_W, "half_life": ("pos_float", 40.0), "beta_half_life": ("pos_float", 60.0)}),
    _s("ew_residual_downside_volatility", "EWResidualDownsideVolatility", "SK-099", ("returns", "market_cap"), "Exponentially weighted downside volatility of returns net of the market.",
       params={**_W, "half_life": ("pos_float", 40.0), "beta_half_life": ("pos_float", 60.0), "min_acceptable_return": ("float", 0.0)}),
    _s("ew_market_beta", "EWMarketBeta", "SK-100", ("returns", "market_cap"), "Exponentially weighted beta to the capitalization weighted market.",
       params={**_W, "half_life": ("pos_float", 60.0), "aggregation_period": ("window", 1)}),
    _s("ew_macro_sensitivity", "EWMacroSensitivity", "SK-100", ("returns", "market_cap"), "Exponentially weighted partial beta to a reference series (for example a currency basket).",
       params={**_W, "half_life": ("pos_float", 60.0), "aggregation_period": ("window", 1)}, extra=("reference_returns",)),
    _s("ew_downside_beta", "EWDownsideBeta", "SK-100", ("returns", "market_cap"), "Exponentially weighted beta in the days the market falls.",
       params={**_W, "half_life": ("pos_float", 60.0), "min_acceptable_return": ("float", 0.0)}),
    _s("ew_amihud_illiquidity", "EWAmihudIlliquidity", "SK-101", ("returns", "adj_close", "adj_volume"), "Exponentially weighted Amihud illiquidity (absolute return per dollar traded).",
       params={**_W, "half_life": ("pos_float", 63.0)}),
    _s("ew_share_turnover", "EWShareTurnover", "SK-101", ("adj_volume", "adj_shares_outstanding"), "Exponentially weighted share turnover (volume over shares outstanding).",
       params={**_W, "half_life": ("pos_float", 21.0)}),
    _s("max_return", "MaxReturn", "SK-102", ("returns",), "Largest return of a window (lottery demand).", params={"window": ("window", 21)}),
    _s("log_market_cap", "LogMarketCap", "SK-102", ("market_cap",), "Natural log of market capitalization (size)."),
    _s("short_interest_descriptor", "ShortInterest", "SK-102", ("short_interest", "adj_shares_outstanding"), "Shares sold short over shares outstanding."),
    _s("days_to_cover", "DaysToCover", "SK-102", ("short_interest", "adj_volume"), "Shares sold short over exponentially weighted volume.", params={**_W, "half_life": ("pos_float", 21.0)}),
    _s("passthrough", "Passthrough", "SK-102", (), "A field of the panel returned as it is (inactive assets cleared).", field_params=("field",)),
)
BY_NAME = {s.public: s for s in SPECS}
PIECES = tuple(sorted({s.piece for s in SPECS} | {"SK-090"}))


def _whole(label: str, value: Any, low: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise BadInput(f"{label} must be a whole number, got {value!r}")
    n = int(value)
    if not low <= n <= MAX_WHOLE:
        raise BadInput(f"{label}={n} is outside [{low}, {MAX_WHOLE}]")
    return n


def _clean_params(spec: Spec, given: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    allowed = set(spec.params) | set(spec.field_params)
    unknown = set(given) - allowed - set(spec.extra)
    if unknown:
        raise BadInput(f"{spec.public}: unknown parameter(s) {sorted(unknown)}")
    for name, (kind, default) in spec.params.items():
        value = given.get(name, default)
        if value is None:
            if kind in ("lag", "window") and name in given:
                raise BadInput(f"{name} is required")
            if kind in ("lag",) and default is None:
                raise BadInput(f"{spec.public}: {name} is required")
            continue
        if kind == "lag" or kind == "window":
            out[name] = _whole(name, value, 1)
        elif kind == "skip":
            out[name] = _whole(name, value, 0)
        elif kind == "whole_or_none":
            out[name] = _whole(name, value, 1)
        elif kind == "pos_float":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0.0 < float(value) <= MAX_HALF_LIFE:
                raise BadInput(f"{name} must be a number in (0, {MAX_HALF_LIFE:g}]")
            out[name] = float(value)
        elif kind == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise BadInput(f"{name} must be a finite number")
            out[name] = float(value)
        elif kind == "bool":
            out[name] = bool(value)
    for name in spec.field_params:
        value = given.get(name)
        if not isinstance(value, str) or not value or len(value) > FIELD_NAME_LIMIT or not all(ch.islower() or ch.isdigit() or ch == "_" for ch in value):
            raise BadInput(f"{spec.public}: {name} must be the name of a field of the panel")
        out[name] = value
    return out


def _infer_key(names: list[str]) -> str:
    for key in ("market_cap", "adj_close", "returns"):
        if key in names:
            return key
    return names[0]


def compute(spec: Spec, fields: Any, active: Any = None, **given: Any):
    """Run one descriptor. Returns a QuantResult."""
    name = f"fundamentals.{spec.public}"
    method = spec.cls
    pieces = (spec.piece,)
    if not isinstance(fields, dict):
        raise BadInput(f"fields must be a dict of DataFrames (dates by assets), got {type(fields).__name__}")
    if not fields or any(isinstance(f, pd.DataFrame) and f.size == 0 for f in fields.values()):
        return c.empty(name, "ratio", method, "fields is empty", piece_ids=pieces)
    extra = {k: given.pop(k) for k in list(given) if k in spec.extra}
    params = _clean_params(spec, given)
    needed = list(spec.fields) + [params[f] for f in spec.field_params]
    missing = [f for f in needed if f not in fields]
    if missing:
        return c.empty(name, "ratio", method, tuple(f"data_missing:{m}" for m in missing), params=params, piece_ids=pieces)
    for key in spec.extra:
        if key not in extra or extra[key] is None:
            return c.empty(name, "ratio", method, f"data_missing:{key}", params=params, piece_ids=pieces)
    used = {f: fields[f] for f in dict.fromkeys(needed)}
    p = build_panel(used, active, infer_from=_infer_key(list(used)))
    warns = (f"cleared {p.cleared_cells} value(s) of inactive assets",) if p.cleared_cells else ()
    numeric = p.numeric()
    try:
        skp = _lib.sk_panel(numeric, p.active)
        instance = getattr(_lib.sk_descriptor, spec.cls)(**params)
        fit_params: dict[str, Any] = {}
        if "reference_returns" in extra:
            ref = extra["reference_returns"]
            fit_params["reference_returns"] = np.asarray(ref.to_numpy() if isinstance(ref, (pd.Series, pd.DataFrame)) else ref, dtype=float).ravel()
        out = np.asarray(instance.fit_transform(skp, **fit_params), dtype=float)
    except (ValueError, TypeError) as exc:
        return c.empty(name, "ratio", method, warns + (f"{type(exc).__name__}: {str(exc)[:200]}",), params=params, piece_ids=pieces)
    out = np.where(np.isfinite(out), out, np.nan)
    frame = pd.DataFrame(out, index=p.index, columns=p.columns)
    if not frame.notna().to_numpy().any():
        return c.empty(name, "ratio", method, warns + ("no finite value (not enough history or inputs)",), params=params, piece_ids=pieces)
    value = {"dates": [str(i)[:10] for i in frame.index], "assets": [str(x) for x in frame.columns], "values": out,
             "latest": {str(k): (None if not np.isfinite(v) else float(v)) for k, v in frame.iloc[-1].items()}}
    return c.build(name, value, unit="ratio", method=method, params=params, piece_ids=pieces, inputs=(frame,), warnings=warns, as_of=p.index[-1])


def _register(spec: Spec) -> None:
    inputs: dict[str, Any] = {"fields": {"type": "dict", "required": True}, "active": {"type": "dataframe", "required": False, "default": None}}
    for pname, (kind, default) in spec.params.items():
        inputs[pname] = {"type": kind, "required": False, "default": default}
    for pname in spec.field_params:
        inputs[pname] = {"type": "str", "required": True}
    for pname in spec.extra:
        inputs[pname] = {"type": "series", "required": True}

    def fn(fields: Any, active: Any = None, **given: Any):
        return compute(spec, fields, active, **given)

    fn.__name__ = spec.public
    fn.__qualname__ = spec.public
    summary = f"{spec.summary} Reads the panel fields: {', '.join(spec.fields) or 'the fields named in the parameters'}."
    registry.register(f"fundamentals.{spec.public}", module="fundamentals", summary=summary, inputs=inputs, piece_ids=(spec.piece,))(fn)


for _spec in SPECS:
    _register(_spec)


# ---------------------------------------------------------------------------------------------------------------------
# SK-090: the base. The catalog says what exists and what each descriptor reads; a set runs several on the same panel.
# ---------------------------------------------------------------------------------------------------------------------
@registry.register("fundamentals.descriptor_catalog", module="fundamentals", piece_ids=("SK-090",), inputs={},
                   summary="Every descriptor with its family piece, the panel fields it reads and its parameters with defaults.")
def descriptor_catalog():
    name, pieces = "fundamentals.descriptor_catalog", ("SK-090",)
    value = {s.public: {"piece": s.piece, "fields": list(s.fields) + [f"<{f}>" for f in s.field_params], "params": {k: v[1] for k, v in s.params.items()},
                        "extra_inputs": list(s.extra), "summary": s.summary} for s in SPECS}
    return c.build(name, value, unit="catalog", method="descriptor base", piece_ids=pieces)


@registry.register("fundamentals.descriptor_set", module="fundamentals", piece_ids=("SK-090",),
                   inputs={"fields": {"type": "dict", "required": True}, "names": {"type": "list[str]", "required": True},
                           "active": {"type": "dataframe", "required": False, "default": None}, "params": {"type": "dict", "required": False, "default": None}},
                   summary="Runs several descriptors on the same panel and returns the latest value of each, plus the ones that could not be computed and why (a composition of descriptors).")
def descriptor_set(fields: Any, names: list[str], active: Any = None, params: dict | None = None):
    name, pieces = "fundamentals.descriptor_set", ("SK-090",)
    if not isinstance(names, (list, tuple)) or not names or len(names) > len(SPECS) or not all(isinstance(n, str) for n in names):
        raise BadInput("names must be a non-empty list of descriptor names")
    unknown = [n for n in names if n not in BY_NAME]
    if unknown:
        raise BadInput(f"unknown descriptor(s): {unknown}")
    if params is not None and not isinstance(params, dict):
        raise BadInput("params must be a dict of descriptor name to parameters")
    results: dict[str, Any] = {}
    skipped: dict[str, list[str]] = {}
    for n in dict.fromkeys(names):
        res = compute(BY_NAME[n], fields, active, **dict((params or {}).get(n, {})))
        if res.value is None:
            skipped[n] = list(res.warnings)
        else:
            results[n] = res.value["latest"]
    if not results:
        return c.empty(name, "ratio", "descriptor composition", tuple(f"{k}: {'; '.join(v)}" for k, v in skipped.items()), piece_ids=pieces)
    return c.build(name, {"latest": results, "skipped": skipped}, unit="ratio", method="descriptor composition", piece_ids=pieces,
                   inputs=(list(results), list(skipped)))

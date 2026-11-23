"""Shared plumbing of the fundamentals module: statements in, items out, results built.

Input convention (docs/quant/fundamentals-conventions.md): `statements` is a DataFrame with one row per fiscal period,
oldest first, and one column per line item. Cash flow items keep the sign of the statement (outflows are negative).
A line that is not reported is a missing column or a NaN, never a zero. Bad data never raises: a result with value None
and the reason in warnings comes back. BadInput is raised only for a wrong type or shape.
"""
from __future__ import annotations

import inspect
import math
from typing import Any, Callable

import numpy as np
import pandas as pd

from hokma.quant import helpers
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult

MODULE = "fundamentals"
MAX_PERIODS = 200

# Column names of `statements` (all optional; a function names what it needs when it is missing).
COLUMNS = (
    "revenue", "cost_of_revenue", "gross_profit", "sga_expenses", "ga_expenses", "marketing_expenses", "rd_expenses",
    "operating_income", "depreciation_amortization", "interest_expense", "interest_income", "income_before_tax",
    "income_tax_expense", "net_income", "preferred_dividends", "eps",
    "cash", "short_term_investments", "long_term_investments", "receivables", "inventory", "current_assets", "ppe",
    "goodwill_intangibles", "total_assets", "accounts_payable", "short_term_debt", "long_term_debt", "total_debt",
    "current_liabilities", "total_liabilities", "total_equity", "retained_earnings", "preferred_equity", "minority_interest",
    "shares_outstanding", "operating_cash_flow", "capex", "dividends_paid", "repurchases", "issuance", "sbc",
    "income_taxes_paid", "interest_paid", "price",
)

# Names that the library functions use for the same line.
ALIASES = {
    "cost_of_goods_sold": "cost_of_revenue", "total_revenue": "revenue", "net_sales": "revenue", "sales": "revenue",
    "net_credit_sales": "revenue",
    "cash_flow_from_operations": "operating_cash_flow", "cash_flow_from_operating_activities": "operating_cash_flow",
    "operating_cashflow": "operating_cash_flow", "operations_cash_flow": "operating_cash_flow",
    "capital_expenditure": "capex", "capital_expenditures": "capex",
    "depreciation_and_amortization": "depreciation_amortization", "depreciation": "depreciation_amortization",
    "selling_general_and_administrative_expenses": "sga_expenses", "general_and_administrative_expenses": "ga_expenses",
    "selling_and_marketing_expenses": "marketing_expenses", "research_and_development_expenses": "rd_expenses",
    "stock_based_compensation": "sbc", "total_current_assets": "current_assets", "total_current_liabilities": "current_liabilities",
    "total_shareholder_equity": "total_equity", "common_equity": "total_equity",
    "cash_and_equivalents": "cash", "cash_and_cash_equivalents": "cash_and_short_term_investments", "marketable_securities": "short_term_investments",
    "earnings_before_interest_and_taxes": "operating_income", "ebit": "operating_income", "earnings_before_tax": "income_before_tax",
    "ebt": "income_before_tax", "accounts_receivable": "receivables", "net_receivables": "receivables",
    "property_plant_and_equipment": "ppe", "goodwill_and_intangible_assets": "goodwill_intangibles",
    "intangible_assets": "goodwill_intangibles", "preferred_stock": "preferred_equity",
    "stock_price": "price", "share_price": "price", "price_per_share": "price", "market_price_per_share": "price",
    "total_shares_outstanding": "shares_outstanding", "common_shares_outstanding": "shares_outstanding", "shares": "shares_outstanding",
    "average_outstanding_shares": "shares_outstanding", "market_capitalization": "market_cap", "market_value_of_equity": "market_cap",
    "earnings_per_share": "eps", "cash_flow": "operating_cash_flow", "net_stock_issued": "issuance",
    "issuance_of_common_stock": "issuance", "repurchase_of_common_stock": "repurchases", "common_stock_purchased": "repurchases",
    "common_stock_issued": "issuance", "dividends": "dividends_paid",
}


def canon(param: str) -> str:
    """The column name behind a library parameter name (average_ and beginning_ stripped, aliases resolved)."""
    for prefix in ("average_", "beginning_"):
        if param.startswith(prefix):
            param = param[len(prefix):]
    return ALIASES.get(param, param)


class Items:
    """The line items of one company, with the derived ones computed on demand.

    `get(name)` returns a Series indexed by period or None when it cannot be built. `arg(param)` resolves the parameter name of
    a library function: `average_x` is the mean of the closing balance of the period and of the one before (the first period
    has none), `beginning_x` is the closing balance of the period before.
    """

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.notes: list[str] = []
        self.filed: pd.Series | None = None          # filing date of each period, when the statements carried one
        self.cut: pd.Timestamp | None = None         # the as_of cut applied
        self._cache: dict[str, pd.Series | None] = {}
        self._busy: set[str] = set()

    # ---- resolution ----
    def raw(self, name: str) -> pd.Series | None:
        if name in self.frame.columns:
            series = self.frame[name].astype(float)
            return series if series.notna().any() else None
        return None

    def get(self, name: str) -> pd.Series | None:
        name = ALIASES.get(name, name)
        if name in self._cache:
            return self._cache[name]
        if name in self._busy:
            return None
        self._busy.add(name)
        try:
            value = self.raw(name)
            if value is None:
                builder = getattr(self, f"_d_{name}", None)
                value = builder() if builder else None
            if isinstance(value, pd.Series):
                value = value.replace([np.inf, -np.inf], np.nan)
                if not value.notna().any():
                    value = None
        finally:
            self._busy.discard(name)
        self._cache[name] = value
        return value

    def arg(self, param: str, overrides: dict[str, str] | None = None) -> pd.Series | None:
        param = (overrides or {}).get(param, param)
        if param.startswith("average_"):
            base = self.get(param[8:])
            return None if base is None else (base + base.shift(1)) / 2.0
        if param.startswith("beginning_"):
            base = self.get(param[10:])
            return None if base is None else base.shift(1)
        return self.get(param)

    def call(self, fn: Callable[..., Any], overrides: dict[str, str] | None = None) -> tuple[Any, list[str]]:
        """Call a library function with every parameter resolved by name. Returns (result or None, missing parameters)."""
        kwargs: dict[str, Any] = {}
        missing: list[str] = []
        for pname, p in inspect.signature(fn).parameters.items():
            value = self.arg(pname, overrides)
            if value is None:
                if p.default is inspect.Parameter.empty:
                    missing.append(pname)
                continue
            kwargs[pname] = value
        if missing:
            return None, [canon(m) for m in missing]
        try:
            return fn(**kwargs), []
        except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
            return None, [f"{type(exc).__name__}: {exc}"[:120]]

    # ---- derived items ----
    def _d_gross_profit(self) -> pd.Series | None:
        rev, cost = self.get("revenue"), self.get("cost_of_revenue")
        return None if rev is None or cost is None else rev - cost

    def _d_ebitda(self) -> pd.Series | None:
        oi, da = self.get("operating_income"), self.get("depreciation_amortization")
        return None if oi is None or da is None else oi + da

    def _d_total_debt(self) -> pd.Series | None:
        std, ltd = self.get("short_term_debt"), self.get("long_term_debt")
        return None if std is None or ltd is None else std + ltd

    def _d_free_cash_flow(self) -> pd.Series | None:
        ocf, capex = self.get("operating_cash_flow"), self.get("capex")
        return None if ocf is None or capex is None else ocf - capex.abs()

    def _d_market_cap(self) -> pd.Series | None:
        price, shares = self.get("price"), self.get("shares_outstanding")
        return None if price is None or shares is None else price * shares

    def _d_cash_and_short_term_investments(self) -> pd.Series | None:
        cash = self.get("cash")
        if cash is None:
            return None
        sti = self.get("short_term_investments")
        return cash if sti is None else cash + sti.fillna(0.0)

    def _d_working_capital(self) -> pd.Series | None:
        ca, cl = self.get("current_assets"), self.get("current_liabilities")
        return None if ca is None or cl is None else ca - cl

    def _d_net_debt(self) -> pd.Series | None:
        debt, cash = self.get("total_debt"), self.get("cash_and_short_term_investments")
        return None if debt is None or cash is None else debt - cash

    def _optional(self, name: str) -> pd.Series:
        """A line that is optional in a sum: when it is absent the sum goes without it and says so."""
        series = self.get(name)
        if series is None:
            note = f"assumed_zero:{name}"
            if note not in self.notes:
                self.notes.append(note)
            return pd.Series(0.0, index=self.frame.index)
        return series.fillna(0.0)

    def _d_long_term_investments(self) -> pd.Series:
        return self._optional("long_term_investments")

    def _d_minority_interest(self) -> pd.Series:
        return self._optional("minority_interest")

    def _d_preferred_equity(self) -> pd.Series:
        return self._optional("preferred_equity")

    def _d_enterprise_value(self) -> pd.Series | None:
        cap, debt, cash = self.get("market_cap"), self.get("total_debt"), self.get("cash_and_short_term_investments")
        if cap is None or debt is None or cash is None:
            return None
        return cap + debt + self._optional("minority_interest") + self._optional("preferred_equity") - cash

    def _d_net_financial_obligations(self) -> pd.Series | None:
        debt, cash = self.get("total_debt"), self.get("cash_and_short_term_investments")
        if debt is None or cash is None:
            return None
        return debt + self._optional("preferred_equity") - cash - self._optional("long_term_investments")

    def _d_net_financial_expense(self) -> pd.Series | None:
        ie, ii, rate = self.get("interest_expense"), self.get("interest_income"), self.get("effective_tax_rate")
        return None if ie is None or ii is None or rate is None else (ie - ii) * (1.0 - rate)

    def _d_effective_tax_rate(self) -> pd.Series | None:
        tax, ebt = self.get("income_tax_expense"), self.get("income_before_tax")
        if tax is None or ebt is None:
            return None
        rate = (tax / ebt.where(ebt > 0)).clip(lower=0.0, upper=1.0)
        return rate.mask(ebt <= 0, 0.0)

    def _d_eps(self) -> pd.Series | None:
        ni, shares = self.get("net_income"), self.get("shares_outstanding")
        if ni is None or shares is None:
            return None
        pref = self.get("preferred_dividends")
        return (ni - (0.0 if pref is None else pref.fillna(0.0))) / shares

    def _d_price_earnings(self) -> pd.Series | None:
        price, eps = self.get("price"), self.get("eps")
        return None if price is None or eps is None else price / eps.where(eps > 0)

    def _d_dividends_per_share(self) -> pd.Series | None:
        paid, shares = self.get("dividends_paid"), self.get("shares_outstanding")
        return None if paid is None or shares is None else paid.abs() / shares

    def _d_dividend_yield(self) -> pd.Series | None:
        dps, price = self.get("dividends_per_share"), self.get("price")
        return None if dps is None or price is None else dps / price

    def _d_buyback_yield(self) -> pd.Series | None:
        rep, iss, cap = self.get("repurchases"), self.get("issuance"), self.get("market_cap")
        if rep is None or cap is None:
            return None
        return (rep.abs() - (0.0 if iss is None else iss.fillna(0.0))) / cap

    def _d_dividend_payout_ratio(self) -> pd.Series | None:
        paid, ni = self.get("dividends_paid"), self.get("net_income")
        return None if paid is None or ni is None else paid.abs() / ni

    def _d_retention_ratio(self) -> pd.Series | None:
        payout = self.get("dividend_payout_ratio")
        return None if payout is None else 1.0 - payout

    def _d_book_value_per_share(self) -> pd.Series | None:
        eq, shares = self.get("total_equity"), self.get("shares_outstanding")
        if eq is None or shares is None:
            return None
        return (eq - self._optional("preferred_equity")) / shares

    def _d_eps_growth(self) -> pd.Series | None:
        eps = self.get("eps")
        if eps is None:
            return None
        prior = eps.shift(1)
        return (eps / prior.where(prior > 0)) - 1.0

    def _d_eps_growth_pct(self) -> pd.Series | None:
        growth = self.get("eps_growth")
        return None if growth is None else growth * 100.0

    def _d_growth_rate(self) -> pd.Series | None:
        return self.get("eps_growth_pct")

    def _d_quick_assets(self) -> pd.Series | None:
        cash, rec = self.get("cash_and_short_term_investments"), self.get("receivables")
        return None if cash is None or rec is None else cash + rec

    def _d_funds_from_operations(self) -> pd.Series | None:
        ni, da = self.get("net_income"), self.get("depreciation_amortization")
        return None if ni is None or da is None else ni + da

    def _d_tangible_total_assets(self) -> pd.Series | None:
        ta, gi = self.get("total_assets"), self.get("goodwill_intangibles")
        return None if ta is None or gi is None else ta - gi

    def _d_invested_capital(self) -> pd.Series | None:
        eq, debt = self.get("total_equity"), self.get("total_debt")
        return None if eq is None or debt is None else eq + debt

    def _d_nopat(self) -> pd.Series | None:
        oi, rate = self.get("operating_income"), self.get("effective_tax_rate")
        return None if oi is None or rate is None else oi * (1.0 - rate)

    def _d_net_working_capital_change(self) -> pd.Series | None:
        wc = self.get("working_capital")
        return None if wc is None else wc.diff()


# ---------------------------------------------------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------------------------------------------------
def prepare(statements: Any, as_of: Any = None, *, name: str = "statements") -> tuple[Items, tuple[str, ...]]:
    """Items of a statements frame cut at `as_of` (rows filed after it are removed when a `filed` column exists)."""
    if not isinstance(statements, pd.DataFrame):
        raise BadInput(f"{name}: expected a DataFrame, got {type(statements).__name__}")
    if len(statements) > MAX_PERIODS:
        raise BadInput(f"{name}: more than {MAX_PERIODS} periods")
    frame = statements.copy()
    warns: list[str] = []
    if as_of is not None:
        try:
            cut = pd.Timestamp(as_of)
        except (TypeError, ValueError) as exc:
            raise BadInput(f"as_of must be a date, got {as_of!r}") from exc
        if "filed" in frame.columns:
            filed = pd.to_datetime(frame["filed"], errors="coerce")
            keep = filed.notna() & (filed <= cut)
            if (~keep).any():
                warns.append(f"excluded {int((~keep).sum())} period(s) filed after as_of or without a filing date")
            frame = frame[keep]
        else:
            warns.append("no filed column: the point in time cut was not applied")
    filed_all = None
    if "filed" in frame.columns:
        filed_all = pd.to_datetime(frame["filed"], errors="coerce")
        frame = frame.drop(columns=["filed"])
    labels = [str(i)[:10] if isinstance(i, pd.Timestamp) else str(i) for i in frame.index]
    if len(set(labels)) != len(labels):
        raise BadInput(f"{name}: duplicate periods in the index")
    frame.index = labels
    frame = frame.sort_index()
    numeric = frame.apply(pd.to_numeric, errors="coerce")
    items = Items(numeric)
    if filed_all is not None:
        filed_all.index = labels
        items.filed = filed_all.loc[frame.index]
    items.cut = None if as_of is None else pd.Timestamp(as_of)
    return items, tuple(warns)


def jsonable(value: Any) -> Any:
    """numpy and pandas containers to plain python; non finite floats to None."""
    if isinstance(value, (pd.Series, pd.Index)):
        return [jsonable(v) for v in value.tolist()]
    if isinstance(value, pd.DataFrame):
        return {str(c): jsonable(value[c]) for c in value.columns}
    if isinstance(value, np.ndarray):
        return jsonable(value.tolist())
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    return value


def empty(name: str, unit: str, method: str, warnings: tuple[str, ...] | str, *, params: dict | None = None,
          piece_ids: tuple[str, ...] = ()) -> QuantResult:
    if isinstance(warnings, str):
        warnings = (warnings,)
    return QuantResult(name=name, value=None, unit=unit, method=method, params=dict(params or {}), warnings=tuple(warnings),
                       piece_ids=tuple(piece_ids))


def build(name: str, value: Any, *, unit: str, method: str, params: dict | None = None, piece_ids: tuple[str, ...],
          inputs: tuple[Any, ...] = (), warnings: tuple[str, ...] = (), as_of: Any = None) -> QuantResult:
    """Final result. A value with nothing finite in it becomes an empty result."""
    clean = jsonable(value)

    def has_number(v: Any) -> bool:
        if isinstance(v, dict):
            return any(has_number(x) for x in v.values())
        if isinstance(v, list):
            return any(has_number(x) for x in v)
        return v is not None

    params = dict(params or {})
    if clean is None or not has_number(clean):
        return empty(name, unit, method, tuple(warnings) + ("result is not finite",), params=params, piece_ids=piece_ids)
    stamp = None
    if as_of is not None:
        try:
            stamp = pd.Timestamp(as_of).to_pydatetime()
        except (TypeError, ValueError):
            stamp = None
    return QuantResult(name=name, value=clean, unit=unit, method=method, params=params, inputs_hash=helpers.hash_inputs(*inputs, params),
                       as_of=stamp, warnings=tuple(warnings), piece_ids=tuple(piece_ids))


def series_table(series: dict[str, pd.Series]) -> dict[str, Any]:
    """{"periods": [...], "series": {name: [...]}, "latest": {name: value}} from named series on the same index."""
    periods = sorted({p for s in series.values() for p in s.index})
    out = {n: [s.get(p, np.nan) for p in periods] for n, s in series.items()}
    # "latest" is the value of the last period and nothing older: a gap in the last period is a gap, never the figure of a year ago.
    latest = {n: (None if not vals or vals[-1] is None or (isinstance(vals[-1], float) and not math.isfinite(vals[-1])) else vals[-1]) for n, vals in out.items()}
    return {"periods": list(periods), "series": out, "latest": latest}


def finite(series: pd.Series | None) -> pd.Series | None:
    return None if series is None else series.replace([np.inf, -np.inf], np.nan)


def latest_value(series: pd.Series | None) -> float | None:
    """The value of the last period, or None when that period has no finite value (an older figure is never used instead)."""
    if series is None or len(series) == 0:
        return None
    last = series.iloc[-1]
    try:
        f = float(last)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def wide(series: pd.Series, label: str = "X") -> pd.DataFrame:
    """The one-row frame (companies by periods) that the distress and quality models of the library expect."""
    return pd.DataFrame([series.to_numpy(dtype=float)], index=[label], columns=list(series.index))


def narrow(frame: Any) -> pd.Series | None:
    """The reverse of `wide`: the only row as a Series by period."""
    if frame is None:
        return None
    if isinstance(frame, pd.DataFrame):
        return frame.iloc[0].astype(float)
    return frame


def stmt_input() -> dict:
    return {"type": "dataframe", "required": True}


AS_OF_INPUT = {"type": "date", "required": False, "default": None}

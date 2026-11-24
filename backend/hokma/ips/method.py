"""The Hokma risk capacity method: the IPS of the fund as numbers (SPEC-024).

Pure functions, no file and no database. The owner's answers (the inputs) go in; the results, the limits of the fund and the
checks come out. Every parameter of the method (levels, recovery months, fallback percentages, cut points, signal table,
share of trading income that overrides the stability) lives in METHOD_TABLES and nowhere else.

Conventions of every result:
- None is "To be defined": an input is missing, and the key goes into 'missing'. It is not 0.
- "no_data" means the inputs are there but the denominator is zero or a figure of the funding history is missing. It
  does not go into 'missing'.
- round2 rounds to 2 decimals, half up, over 15 significant digits (the ROUND of a spreadsheet). It is applied only where
  the method says so.

All money is USD. Nothing here suggests a figure: an empty answer stays empty.
"""
from __future__ import annotations

import copy
import datetime
import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

SCHEMA = "hokma.ips/1"
CURRENCY = "USD"
NO_DATA = "no_data"

CLOSING = ("This is educational material, not investment advice. Trading involves risk of loss, including the capital you "
           "put at risk. Hokma does not promise or guarantee returns.")

METHOD_TABLES: dict[str, Any] = {
    # (level, months to recover a loss, fallback percentage of the investable capital, minimum score out of 10)
    "levels": (("low", 6, 5, 0), ("medium", 12, 10, 4), ("high", 24, 15, 7)),
    "asset_types": ("cash", "reserve", "trading_account", "investments", "real_estate", "vehicle", "other", "prop_account"),
    "reserve_type": "reserve",
    "prop_type": "prop_account",
    "stability_points": {"none": 0, "variable": 1, "fixed": 2},
    "variable_stability": "variable",
    "withdrawal_points": {"never": 0, "irregular": 1, "regular": 2},
    # (low cut, high cut) of each factor
    "cuts": {"c1": (3, 6), "c2": (0.15, 0.35), "c4": (1, 3), "c5": (0.10, 0.25), "w": (0.05, 0.20)},
    "trading_share_cut": 0.5,
    "percent_max": 100,
    "attempts_round_places": 9,
    "max_assets": 50,
    "max_debts": 30,
    "max_desks": 30,
    "max_list_items": 50,
    "max_text": 600,
    # signal of the funding: (color, minimum multiple). The action of each color is below.
    "signal": (("red", 0), ("amber", 1), ("green", 2)),
    "signal_actions": {
        "red": "Pause new purchases while you correct where it breaks. The way out of red is to take accounts to payout, "
               "not to leave funding.",
        "amber": "Profitable but fragile. Do not add accounts. Focus on turning funded accounts into payouts.",
        "green": "You can scale: your expanded budget allows {N} attempts a year.",
        "green_no_attempts_yet": "You can scale. Define what percentage of your payouts you reinvest to know how many more "
                                 "attempts you can pay this year.",
        "green_zero_attempts": "Funding pays off, but today there is no budget for new attempts. Do not add accounts.",
    },
    "blocks": ("profile", "assets", "debts", "cashflow", "income", "conduct", "prop", "risk_level", "pockets", "objectives",
               "restrictions"),
}

LEVEL_NAMES = tuple(row[0] for row in METHOD_TABLES["levels"])
CAPACITY_FACTORS = ("c1", "c2", "c3", "c4", "c5")
WILLINGNESS_FACTORS = ("w1", "w2", "w3", "w4", "w5")
CONDUCT_KEYS = ("overtrading_days_pct", "stop_moved_pct", "off_rule_size_pct", "revenge_reentry_pct")
HISTORY_KEYS = ("paid_12m_usd", "evaluations", "funded", "paid_out_accounts", "payouts_12m_usd", "reinvest_pct")
ESTIMATE_KEYS = ("evaluation_usd", "reset_usd", "activation_usd", "resets_per_attempt")
DERIVED_KEYS = ("schema", "version", "status", "created_at", "updated_at", "currency", "results", "limits", "checks", "missing", "history")
FUNDED_ONLY = ("funding_budget_usd", "avg_account_cost_usd", "prop_max_drawdown_per_account_usd")


class IpsInputError(ValueError):
    """An answer that cannot be used. The message is shown to the owner as written."""


# ------------------------------------------------------------------------------------------------ small helpers

def round2(x: float, places: int = 2) -> float:
    """ROUND of a spreadsheet: 15 significant digits first, then half up."""
    return float(Decimal(format(float(x), ".15g")).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _sum(values) -> float | int:
    """Sum where an empty answer counts as 0."""
    return sum(v for v in values if v is not None)


def _divide_nd(top: Any, bottom: Any) -> Any:
    """top / bottom, "no_data" when a figure is missing or the denominator is zero."""
    if not _is_num(top) or not _is_num(bottom) or bottom == 0:
        return NO_DATA
    return top / bottom


def _divide(top: Any, bottom: Any) -> Any:
    """top / bottom, None when a figure is missing, "no_data" when the denominator is zero."""
    if not _is_num(top) or not _is_num(bottom):
        return None
    return NO_DATA if bottom == 0 else top / bottom


def _text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise IpsInputError(f"{name}: must be text, got {value!r}.")
    value = " ".join(value.split())
    if len(value) > METHOD_TABLES["max_text"]:
        raise IpsInputError(f"{name}: the text is longer than {METHOD_TABLES['max_text']} characters.")
    return value or None


def _number(value: Any, name: str, *, signed: bool = False, whole: bool = False, maximum: float | None = None) -> float | int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IpsInputError(f"{name}: must be a number, got {value!r}.")
    if math.isnan(value) or math.isinf(value):
        raise IpsInputError(f"{name}: the number is not valid.")
    if value < 0 and not signed:
        raise IpsInputError(f"{name}: cannot be negative ({value}).")
    if whole and isinstance(value, float):
        if not value.is_integer():
            raise IpsInputError(f"{name}: must be a whole number, got {value}.")
        value = int(value)
    if maximum is not None and value > maximum:
        raise IpsInputError(f"{name}: is between 0 and {maximum}, got {value}.")
    return value


def _date(value: Any, name: str) -> str | None:
    value = _text(value, name)
    if value is None:
        return None
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise IpsInputError(f"{name}: the date must be YYYY-MM-DD, got {value!r}.") from None
    return value


def _object(value: Any, name: str, allowed: tuple[str, ...]) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise IpsInputError(f"{name}: must be an object.")
    extra = sorted(set(value) - set(allowed))
    if extra:
        raise IpsInputError(f"{name}: unknown field {', '.join(extra)}. Allowed: {', '.join(allowed)}.")
    return value


def _list(value: Any, name: str, limit: int) -> list | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise IpsInputError(f"{name}: must be a list.")
    if len(value) > limit:
        raise IpsInputError(f"{name}: has {len(value)} items; the maximum is {limit}.")
    return value


def _choice(value: Any, name: str, options) -> str | None:
    value = _text(value, name)
    if value is None:
        return None
    if value not in options:
        raise IpsInputError(f"{name}: {value!r} is not one of {', '.join(options)}.")
    return value


# ------------------------------------------------------------------------------------------------ currency

def normalize_fx(fx: Any) -> dict | None:
    """The exchange rate the owner wrote: units of the owner's currency per 1 USD, its source and its date."""
    if fx is None:
        return None
    obj = _object(fx, "fx", ("currency", "units_per_usd", "source", "rate_date"))
    currency = _text(obj.get("currency"), "fx.currency")
    rate = _number(obj.get("units_per_usd"), "fx.units_per_usd")
    source = _text(obj.get("source"), "fx.source")
    rate_date = _date(obj.get("rate_date"), "fx.rate_date")
    if currency is None or rate is None or source is None or rate_date is None:
        raise IpsInputError("fx: needs currency, units_per_usd (units of that currency for 1 USD), source and rate_date. "
                            "Ask the owner for the rate: Hokma does not look it up.")
    currency = currency.upper()
    if currency == CURRENCY:
        raise IpsInputError("fx: the amounts are already in USD; no rate is needed.")
    if rate <= 0:
        raise IpsInputError("fx.units_per_usd: must be greater than zero.")
    return {"currency": currency, "units_per_usd": rate, "source": source, "rate_date": rate_date}


def _convert(value: Any, rate: float) -> Any:
    if isinstance(value, dict):
        return {k: (round2(v / rate) if k.endswith("_usd") and _is_num(v) else _convert(v, rate)) for k, v in value.items()}
    if isinstance(value, list):
        return [_convert(v, rate) for v in value]
    return value


def to_usd(partial: Any, fx: Any = None) -> dict:
    """The answers of one call in USD. With 'fx', every field ending in _usd of this call is in the currency of the rate and is
    divided by it (2 decimals); the rate is recorded in profile.fx."""
    if partial is None:
        partial = {}
    if not isinstance(partial, dict):
        raise IpsInputError("inputs: must be an object.")
    rate = normalize_fx(fx)
    out = {k: copy.deepcopy(v) for k, v in partial.items() if k not in DERIVED_KEYS}    # what is calculated is never read from the caller
    if rate is None:
        return out
    out = _convert(out, rate["units_per_usd"])
    profile = out.get("profile")
    if profile is not None and not isinstance(profile, dict):
        raise IpsInputError("profile: must be an object.")
    out["profile"] = {**(profile or {}), "fx": rate}
    return out


def merge(base: dict | None, patch: dict) -> dict:
    """The stored answers with the new ones over them: objects merge field by field, lists and values are replaced."""
    def deep(a: Any, b: Any) -> Any:
        if isinstance(a, dict) and isinstance(b, dict):
            return {**a, **{k: deep(a.get(k), v) for k, v in b.items()}}
        return copy.deepcopy(b)
    return deep(base or {}, patch or {})


# ------------------------------------------------------------------------------------------------ inputs

TOP_KEYS = ("profile", "assets", "has_debts", "debts", "cashflow", "income", "conduct", "prop", "pockets", "objectives",
            "restrictions", "declared_limits")


def normalize(inputs: Any) -> tuple[dict, list[str]]:
    """The answers checked and complete (every field present, None when not answered), and the warnings."""
    if inputs is None:
        inputs = {}
    inputs = _object(inputs, "inputs", TOP_KEYS)
    tables = METHOD_TABLES
    warnings: list[str] = []

    profile = _object(inputs.get("profile"), "profile", ("owner_name", "adopted_on", "next_review_on", "fx"))
    clean: dict[str, Any] = {"profile": {
        "owner_name": _text(profile.get("owner_name"), "profile.owner_name"),
        "adopted_on": _date(profile.get("adopted_on"), "profile.adopted_on"),
        "next_review_on": _date(profile.get("next_review_on"), "profile.next_review_on"),
        "fx": normalize_fx(profile.get("fx")),
    }}

    assets = _list(inputs.get("assets"), "assets", tables["max_assets"])
    if assets is not None:
        rows = []
        for i, raw in enumerate(assets, start=1):
            a = _object(raw, f"Asset {i}", ("name", "type", "value_usd"))
            kind = _choice(a.get("type"), f"Asset {i}: type", tables["asset_types"])
            value = _number(a.get("value_usd"), f"Asset {i}: value_usd")
            if value is not None and kind is None:
                raise IpsInputError(f"Asset {i}: has a value but no type.")
            rows.append({"name": _text(a.get("name"), f"Asset {i}: name"), "type": kind, "value_usd": value})
        assets = rows
    clean["assets"] = assets

    has_debts = inputs.get("has_debts")
    if has_debts is not None and not isinstance(has_debts, bool):
        raise IpsInputError(f"has_debts: must be true or false, got {has_debts!r}.")
    debts = _list(inputs.get("debts"), "debts", tables["max_debts"])
    if debts is not None:
        rows = []
        for i, raw in enumerate(debts, start=1):
            d = _object(raw, f"Debt {i}", ("name", "balance_usd", "monthly_payment_usd", "annual_rate_pct", "funds_trading"))
            funds = d.get("funds_trading")
            if funds is not None and not isinstance(funds, bool):
                raise IpsInputError(f"Debt {i}: funds_trading must be true or false, got {funds!r}.")
            rows.append({"name": _text(d.get("name"), f"Debt {i}: name"),
                         "balance_usd": _number(d.get("balance_usd"), f"Debt {i}: balance_usd"),
                         "monthly_payment_usd": _number(d.get("monthly_payment_usd"), f"Debt {i}: monthly_payment_usd"),
                         "annual_rate_pct": _number(d.get("annual_rate_pct"), f"Debt {i}: annual_rate_pct"),
                         "funds_trading": funds})
        debts = rows
        listed = [d for d in debts if any(v is not None for v in d.values())]
        if has_debts is False and listed:
            raise IpsInputError(f"The owner said there are no debts, but the list has {len(listed)}. Ask which of the two is right.")
    clean["has_debts"], clean["debts"] = has_debts, debts

    cash = _object(inputs.get("cashflow"), "cashflow", ("monthly_spend_usd", "reserve_target_months", "monthly_savings_usd"))
    clean["cashflow"] = {k: _number(cash.get(k), f"cashflow.{k}") for k in ("monthly_spend_usd", "reserve_target_months", "monthly_savings_usd")}

    inc = _object(inputs.get("income"), "income", ("monthly_income_usd", "stability", "dependents", "withdrawal_habit"))
    clean["income"] = {
        "monthly_income_usd": _number(inc.get("monthly_income_usd"), "income.monthly_income_usd"),
        "stability": _choice(inc.get("stability"), "income.stability", tuple(tables["stability_points"])),
        "dependents": _number(inc.get("dependents"), "income.dependents", whole=True),
        "withdrawal_habit": _choice(inc.get("withdrawal_habit"), "income.withdrawal_habit", tuple(tables["withdrawal_points"])),
    }

    con = _object(inputs.get("conduct"), "conduct", CONDUCT_KEYS)
    clean["conduct"] = {k: _number(con.get(k), f"conduct.{k}", maximum=tables["percent_max"]) for k in CONDUCT_KEYS}
    values = list(clean["conduct"].values())
    if all(v is not None and v < 1 for v in values) and any(v > 0 for v in values):
        warnings.append("The four conduct percentages are below 1. If they were written as fractions, correct them: 5% is written 5, not 0.05.")

    prop = _object(inputs.get("prop"), "prop", ("history", "estimate", "own_accounts_net_12m_usd"))
    hist = _object(prop.get("history"), "prop.history", HISTORY_KEYS)
    est = _object(prop.get("estimate"), "prop.estimate", ESTIMATE_KEYS)
    history = {}
    for k in HISTORY_KEYS:
        history[k] = _number(hist.get(k), f"prop.history.{k}", whole=k in ("evaluations", "funded", "paid_out_accounts"),
                             maximum=tables["percent_max"] if k == "reinvest_pct" else None)
    estimate = {k: _number(est.get(k), f"prop.estimate.{k}") for k in ESTIMATE_KEYS}
    ev, fo, po, payouts = history["evaluations"], history["funded"], history["paid_out_accounts"], history["payouts_12m_usd"]
    if fo is not None and ev is not None and fo > ev:
        raise IpsInputError(f"prop.history: the accounts that passed ({fo}) cannot be more than the evaluations bought ({ev}).")
    if po is not None and fo is not None and po > fo:
        raise IpsInputError(f"prop.history: the accounts with a payout ({po}) cannot be more than the accounts that passed ({fo}).")
    if po is not None and po > 0 and payouts == 0:
        raise IpsInputError(f"prop.history: there are {po} accounts with a payout, but the payouts received are 0.")
    clean["prop"] = {"history": history, "estimate": estimate,
                     "own_accounts_net_12m_usd": _number(prop.get("own_accounts_net_12m_usd"), "prop.own_accounts_net_12m_usd", signed=True)}

    pk = ("operating_max_loss_usd", "operating_from_payouts_usd", "speculative_max_loss_usd", "speculative_from_payouts_usd")
    pockets = _object(inputs.get("pockets"), "pockets", pk)
    clean["pockets"] = {k: _number(pockets.get(k), f"pockets.{k}") for k in pk}

    obj = _object(inputs.get("objectives"), "objectives", ("goal", "target_amount_usd", "horizon_years", "desired_return_pct", "liquidity_needs"))
    clean["objectives"] = {"goal": _text(obj.get("goal"), "objectives.goal"),
                           "target_amount_usd": _number(obj.get("target_amount_usd"), "objectives.target_amount_usd"),
                           "horizon_years": _number(obj.get("horizon_years"), "objectives.horizon_years"),
                           "desired_return_pct": _number(obj.get("desired_return_pct"), "objectives.desired_return_pct"),
                           "liquidity_needs": _text(obj.get("liquidity_needs"), "objectives.liquidity_needs")}

    rest = _object(inputs.get("restrictions"), "restrictions", ("allowed", "forbidden"))
    clean["restrictions"] = {}
    for k in ("allowed", "forbidden"):
        items = _list(rest.get(k), f"restrictions.{k}", tables["max_list_items"])
        clean["restrictions"][k] = None if items is None else [t for t in (_text(x, f"restrictions.{k}") for x in items) if t]

    lim = _object(inputs.get("declared_limits"), "declared_limits", ("max_risk_per_trade_pct", "max_open_positions", "desk_allocation"))
    desks = _list(lim.get("desk_allocation"), "declared_limits.desk_allocation", tables["max_desks"])
    if desks is not None:
        desks = _normalize_desks(desks)
    clean["declared_limits"] = {
        "max_risk_per_trade_pct": _number(lim.get("max_risk_per_trade_pct"), "declared_limits.max_risk_per_trade_pct", maximum=tables["percent_max"]),
        "max_open_positions": _number(lim.get("max_open_positions"), "declared_limits.max_open_positions", whole=True),
        "desk_allocation": desks}
    return clean, warnings


def _normalize_desks(desks: list) -> list[dict]:
    rows, seen = [], set()
    for i, raw in enumerate(desks, start=1):
        d = _object(raw, f"Desk {i}", ("desk", "capital_source", "max_loss_usd") + FUNDED_ONLY)
        name = _text(d.get("desk"), f"Desk {i}: desk")
        if name is None:
            raise IpsInputError(f"Desk {i}: needs the name of the desk.")
        if name.lower() in seen:
            raise IpsInputError(f"Desk {name}: is listed twice.")
        seen.add(name.lower())
        source = _choice(d.get("capital_source"), f"Desk {name}: capital_source", ("own", "funded"))
        if source is None:
            raise IpsInputError(f"Desk {name}: capital_source is own or funded.")
        if source == "own":
            bad = [k for k in FUNDED_ONLY if d.get(k) is not None]
            if bad:
                raise IpsInputError(f"Desk {name}: an own-capital desk declares only max_loss_usd; remove {', '.join(bad)}.")
        elif d.get("max_loss_usd") is not None:
            raise IpsInputError(f"Desk {name}: a funded desk declares funding_budget_usd, not max_loss_usd.")
        rows.append({"desk": name, "capital_source": source,
                     "max_loss_usd": _number(d.get("max_loss_usd"), f"Desk {name}: max_loss_usd"),
                     "funding_budget_usd": _number(d.get("funding_budget_usd"), f"Desk {name}: funding_budget_usd"),
                     "avg_account_cost_usd": _number(d.get("avg_account_cost_usd"), f"Desk {name}: avg_account_cost_usd"),
                     "prop_max_drawdown_per_account_usd": _number(d.get("prop_max_drawdown_per_account_usd"),
                                                                  f"Desk {name}: prop_max_drawdown_per_account_usd")})
    return rows


# ------------------------------------------------------------------------------------------------ results

def _level_of(score: int) -> str:
    chosen = METHOD_TABLES["levels"][0][0]
    for name, _months, _pct, minimum in METHOD_TABLES["levels"]:
        if score >= minimum:
            chosen = name
    return chosen


def _factor(value: float, low: float, high: float, *, high_is_bad: bool, inclusive_high: bool = False) -> int:
    """0, 1 or 2. high_is_bad: above the high cut is 0 (from it when inclusive_high), from the low cut is 1, below it is 2.
    Otherwise the reverse: below the low cut is 0, up to the high cut is 1 (through it when inclusive_high), above it is 2."""
    if high_is_bad:
        return 0 if (value >= high if inclusive_high else value > high) else 1 if value >= low else 2
    return 0 if value < low else 1 if (value <= high if inclusive_high else value < high) else 2


def compute(inputs: dict) -> dict:
    """{'results', 'limits', 'checks', 'missing'} of normalized inputs."""
    tables = METHOD_TABLES
    r: dict[str, Any] = {}
    assets, debts = inputs["assets"], inputs["debts"] or []
    cash, inc, con, prop = inputs["cashflow"], inputs["income"], inputs["conduct"], inputs["prop"]
    hist, est, pockets = prop["history"], prop["estimate"], inputs["pockets"]

    # net worth and reserve
    if assets is None:
        r["counted_assets"] = r["prop_excluded"] = r["reserve_now"] = None
    else:
        r["counted_assets"] = _sum(a["value_usd"] for a in assets if a["type"] != tables["prop_type"])
        r["prop_excluded"] = _sum(a["value_usd"] for a in assets if a["type"] == tables["prop_type"])
        r["reserve_now"] = _sum(a["value_usd"] for a in assets if a["type"] == tables["reserve_type"])
    r["total_debt"] = _sum(d["balance_usd"] for d in debts)
    r["total_payment"] = _sum(d["monthly_payment_usd"] for d in debts)
    r["net_worth"] = None if r["counted_assets"] is None else r["counted_assets"] - r["total_debt"]

    spend, months_target, savings = cash["monthly_spend_usd"], cash["reserve_target_months"], cash["monthly_savings_usd"]
    reserve_now = r["reserve_now"]
    r["months_covered"] = None if reserve_now is None or spend is None or spend == 0 else reserve_now / spend
    if spend is None or months_target is None:
        r["reserve_target"] = r["reserve_gap"] = r["reserve_status"] = None
    else:
        r["reserve_target"] = spend * months_target
        if reserve_now is None:
            r["reserve_gap"] = r["reserve_status"] = None
        else:
            r["reserve_gap"] = max(0, r["reserve_target"] - reserve_now)
            r["reserve_status"] = "covers" if reserve_now >= r["reserve_target"] else "short"
    if r["net_worth"] is None:
        r["investable_capital"] = None
    else:
        held = reserve_now if r["reserve_target"] is None else max(reserve_now, r["reserve_target"])
        r["investable_capital"] = r["net_worth"] - held

    listed = [d for d in debts if any(v is not None for v in d.values())]
    r["debts_funding_trading"] = sum(1 for d in debts if d["funds_trading"] is True)
    unanswered = any(d["funds_trading"] is None for d in listed
                     if any(d[k] is not None for k in ("name", "balance_usd", "monthly_payment_usd", "annual_rate_pct")))
    if r["debts_funding_trading"] > 0:
        r["debt_status"] = "fail"
    elif unanswered or (not listed and inputs["has_debts"] is not False):
        r["debt_status"] = None
    else:
        r["debt_status"] = "pass"

    # income: the trading adds to the income
    paid, payouts, reinvest = hist["paid_12m_usd"], hist["payouts_12m_usd"], hist["reinvest_pct"]
    monthly_income = inc["monthly_income_usd"]
    r["trading_net_12m"] = round2((prop["own_accounts_net_12m_usd"] or 0) + (payouts or 0) - (paid or 0))
    r["trading_income_month"] = round2(max(0, r["trading_net_12m"]) / 12)
    r["total_income_month"] = None if monthly_income is None else round2(monthly_income + r["trading_income_month"])
    total = r["total_income_month"]
    r["trading_share"] = None if total is None else NO_DATA if total == 0 else r["trading_income_month"] / total
    r["savings_used"] = None if savings is None else round2(savings + r["trading_income_month"])
    share = r["trading_share"]
    if _is_num(share) and share > tables["trading_share_cut"]:
        r["stability_used"] = tables["variable_stability"]
    else:
        r["stability_used"] = inc["stability"]
    no_income = total is not None and total == 0

    # capacity and willingness
    r["debt_ratio"] = None if total is None else NO_DATA if no_income else r["total_payment"] / total
    r["savings_rate"] = (None if total is None else NO_DATA if no_income else None if r["savings_used"] is None
                         else r["savings_used"] / total)
    cuts = tables["cuts"]
    months = r["months_covered"]
    r["c1"] = None if months is None else _factor(months, *cuts["c1"], high_is_bad=False)
    if no_income:
        r["c2"] = 2 if r["total_payment"] == 0 else 0
    else:
        r["c2"] = None if not _is_num(r["debt_ratio"]) else _factor(r["debt_ratio"], *cuts["c2"], high_is_bad=True)
    r["c3"] = None if r["stability_used"] is None else tables["stability_points"][r["stability_used"]]
    r["c4"] = None if inc["dependents"] is None else _factor(inc["dependents"], *cuts["c4"], high_is_bad=True, inclusive_high=True)
    if no_income:
        r["c5"] = 0
    else:
        r["c5"] = None if not _is_num(r["savings_rate"]) else _factor(r["savings_rate"], *cuts["c5"], high_is_bad=False, inclusive_high=True)
    cap = [r[k] for k in CAPACITY_FACTORS]
    r["capacity_score"] = None if None in cap else sum(cap)
    r["capacity_level"] = None if r["capacity_score"] is None else _level_of(r["capacity_score"])

    for i, key in enumerate(CONDUCT_KEYS, start=1):
        v = con[key]
        r[f"w{i}"] = None if v is None else _factor(v / 100, *cuts["w"], high_is_bad=True)
    habit = inc["withdrawal_habit"]
    r["w5"] = None if habit is None else tables["withdrawal_points"][habit]
    will = [r[k] for k in WILLINGNESS_FACTORS]
    r["willingness_score"] = None if None in will else sum(will)
    r["willingness_level"] = None if r["willingness_score"] is None else _level_of(r["willingness_score"])

    if r["capacity_level"] is None or r["willingness_level"] is None:
        r["risk_level"] = r["recovery_months"] = r["fallback_pct"] = None
    else:
        r["risk_level"] = LEVEL_NAMES[min(LEVEL_NAMES.index(r["capacity_level"]), LEVEL_NAMES.index(r["willingness_level"]))]
        _name, r["recovery_months"], r["fallback_pct"], _minimum = next(x for x in tables["levels"] if x[0] == r["risk_level"])

    # tolerable loss
    used = r["savings_used"]
    if no_income:
        r["loss_method"] = "percent_of_investable"
    elif used is None:
        r["loss_method"] = None
    else:
        r["loss_method"] = "savings_x_months" if used > 0 else "percent_of_investable"
    investable = r["investable_capital"]
    if r["loss_method"] is None or r["risk_level"] is None or investable is None:
        r["loss_capacity"] = None
    else:
        top = max(0, investable)
        base = used * r["recovery_months"] if r["loss_method"] == "savings_x_months" else top * (r["fallback_pct"] / 100)
        r["loss_capacity"] = round2(min(top, base))

    _funding(r, prop, hist, est, pockets, reinvest)
    results = dict(r)
    limits = _limits(results, inputs)
    checks = _checks(results, limits, inputs)
    missing = [k for k, v in results.items() if v is None]
    return {"results": results, "limits": limits, "checks": checks, "missing": missing}


def _funding(r: dict, prop: dict, hist: dict, est: dict, pockets: dict, reinvest: Any) -> None:
    paid, evals, funded, paid_out, payouts = (hist["paid_12m_usd"], hist["evaluations"], hist["funded"],
                                              hist["paid_out_accounts"], hist["payouts_12m_usd"])
    estimated_ok = all(est[k] is not None for k in ESTIMATE_KEYS)
    if _is_num(paid) and _is_num(evals) and evals > 0:
        mode = "real"
    elif estimated_ok:
        mode = "estimated"
    elif _is_num(paid) and _is_num(evals) and evals == 0:
        mode = NO_DATA
    else:
        mode = None
    r["cost_mode"] = mode
    if mode == "real":
        r["cost_per_attempt"] = paid / evals
    elif mode == "estimated":
        r["cost_per_attempt"] = est["evaluation_usd"] + est["activation_usd"] + est["resets_per_attempt"] * est["reset_usd"]
    else:
        r["cost_per_attempt"] = mode
    cost = r["cost_per_attempt"]

    r["pass_rate"] = _divide_nd(funded, evals)
    r["payout_conversion"] = _divide_nd(paid_out, funded)
    r["net_prop_result"] = NO_DATA if not _is_num(payouts) or not _is_num(paid) else payouts - paid
    r["prop_multiple"] = _divide_nd(payouts, paid)
    r["avg_payout_per_account"] = _divide_nd(payouts, paid_out)
    r["cost_per_funded_account"] = _divide(paid, funded)
    r["cost_per_paid_out_account"] = _divide(paid, paid_out)

    # use of the payouts, only as an input of the expanded budget
    r["payouts_for_living"] = round2(min(max(0, (payouts or 0) - (paid or 0)), max(0, r["trading_net_12m"])))
    r["payouts_paying_funding"] = round2(min(paid or 0, payouts or 0))
    r["house_spend"] = NO_DATA if paid is None else round2(max(0, paid - r["payouts_paying_funding"]))
    r["reinvested_payouts"] = NO_DATA if payouts is None or reinvest is None else round2(payouts * (reinvest / 100))
    op_from, sp_from = pockets["operating_from_payouts_usd"] or 0, pockets["speculative_from_payouts_usd"] or 0
    r["payouts_in_pockets"] = op_from + sp_from
    r["free_payouts"] = round2(max(0, (payouts or 0) - r["payouts_for_living"] - r["payouts_paying_funding"] - r["payouts_in_pockets"]))
    op, sp = pockets["operating_max_loss_usd"], pockets["speculative_max_loss_usd"]
    r["house_loss_operating"] = None if op is None else max(0, op - op_from)
    r["house_loss_speculative"] = None if sp is None else max(0, sp - sp_from)

    loss = r["loss_capacity"]
    house_op, house_sp = r["house_loss_operating"], r["house_loss_speculative"]
    if loss is None or house_op is None or house_sp is None:
        r["funding_budget"] = None
    else:
        r["funding_budget"] = round2(max(0, loss - house_op - house_sp))
    budget = r["funding_budget"]

    def attempts(amount: Any) -> Any:
        if not _is_num(amount):
            return amount
        if not _is_num(cost):
            return cost
        return NO_DATA if cost == 0 else math.floor(round2(amount / cost, METHOD_TABLES["attempts_round_places"]))

    def expected(count: Any) -> Any:
        if _is_num(count) and _is_num(r["pass_rate"]):
            return count * r["pass_rate"]
        return r["pass_rate"] if _is_num(count) else count

    r["affordable_attempts"] = attempts(budget)
    r["expected_funded"] = expected(r["affordable_attempts"])

    if paid is None:
        r["funding_spend_test"] = r["funding_spend_excess"] = NO_DATA
    elif budget is None:
        r["funding_spend_test"] = r["funding_spend_excess"] = None
    else:
        diff = round2(r["house_spend"] - budget)
        r["funding_spend_test"] = "pass" if diff <= 0 else "fail"
        r["funding_spend_excess"] = max(0, diff)

    r["payout_per_evaluation"] = _divide_nd(payouts, evals)
    if mode == "real" and _is_num(r["payout_per_evaluation"]):
        r["value_per_evaluation"] = round2(r["payout_per_evaluation"] - cost)
    else:
        r["value_per_evaluation"] = NO_DATA
    if budget is None:
        r["expanded_budget"] = None
    elif r["investable_capital"] <= 0:
        r["expanded_budget"] = 0
    elif payouts is None or reinvest is None:
        r["expanded_budget"] = NO_DATA
    else:
        r["expanded_budget"] = round2(budget + min(r["reinvested_payouts"], r["free_payouts"]))
    r["expanded_attempts"] = attempts(r["expanded_budget"])
    r["expected_funded_expanded"] = expected(r["expanded_attempts"])
    avg = r["avg_payout_per_account"]
    r["evaluations_paid_by_payout_account"] = NO_DATA if not _is_num(avg) or not _is_num(cost) or cost == 0 else avg / cost

    # signal and action
    multiple = r["prop_multiple"]
    if not _is_num(multiple):
        r["prop_signal"] = r["prop_action"] = NO_DATA
    else:
        color = METHOD_TABLES["signal"][0][0]
        for name, minimum in METHOD_TABLES["signal"]:
            if multiple >= minimum:
                color = name
        actions = METHOD_TABLES["signal_actions"]
        action, n = actions[color], r["expanded_attempts"]
        if "{N}" in action:
            action = actions["green_zero_attempts"] if n == 0 else action.replace("{N}", str(n)) if _is_num(n) else actions["green_no_attempts_yet"]
        r["prop_signal"], r["prop_action"] = color, action

    if budget is None:
        r["coherence_sum"] = None
    else:
        r["coherence_sum"] = round2(budget + house_op + house_sp)
    if loss is None or r["coherence_sum"] is None:
        r["coherence_status"] = None
    else:
        r["coherence_status"] = "pass" if r["coherence_sum"] <= loss else "fail"
    r["coherence_slack"] = None if loss is None or house_op is None or house_sp is None else round2(loss - house_op - house_sp)


# ------------------------------------------------------------------------------------------------ limits and checks

def _limits(r: dict, inputs: dict) -> dict:
    declared = inputs["declared_limits"]
    parts = (r["house_loss_operating"], r["house_loss_speculative"], r["funding_budget"])
    pockets_total = None if any(p is None for p in parts) else round2(sum(parts))
    loss = r["loss_capacity"]
    max_loss = None if loss is None or pockets_total is None else min(loss, pockets_total)
    investable = r["investable_capital"]
    if max_loss is None or investable is None:
        floor = drawdown = None
    else:
        floor = max(0, investable - max_loss)
        drawdown = NO_DATA if investable <= 0 else max_loss / investable * 100
    return {
        "investable_capital_usd": investable,
        "max_loss_usd": max_loss,
        "capital_floor_usd": floor,
        "max_drawdown_pct": drawdown,
        "max_risk_per_trade_pct": declared["max_risk_per_trade_pct"],
        "max_open_positions": declared["max_open_positions"],
        "pockets_house_total_usd": pockets_total,
        "desk_allocation": [_desk_limits(d) for d in declared["desk_allocation"] or []],
    }


def _desk_limits(d: dict) -> dict:
    out = {"desk": d["desk"], "capital_source": d["capital_source"], "max_loss_usd": None, "funding_budget_usd": None,
           "avg_account_cost_usd": None, "prop_max_drawdown_per_account_usd": None, "house_limit_usd": None, "max_accounts": None}
    if d["capital_source"] == "own":
        out["max_loss_usd"] = d["max_loss_usd"]
        out["house_limit_usd"] = d["max_loss_usd"]
        return out
    out["funding_budget_usd"] = d["funding_budget_usd"]
    out["avg_account_cost_usd"] = d["avg_account_cost_usd"]
    out["prop_max_drawdown_per_account_usd"] = d["prop_max_drawdown_per_account_usd"]
    out["house_limit_usd"] = d["funding_budget_usd"]          # the drawdown rule of the funding firm protects the account, never the house
    out["max_accounts"] = _divide(d["funding_budget_usd"], d["avg_account_cost_usd"])
    if _is_num(out["max_accounts"]):
        out["max_accounts"] = math.floor(round2(out["max_accounts"], METHOD_TABLES["attempts_round_places"]))
    return out


def house_charge(desk: dict, event: dict) -> float:
    """What an event costs the house, in USD, for the desk's own limit (the running account is delivery 3).

    event = {"kind": "market_loss" | "account_cost", "amount_usd": >= 0, "paid_with": "house" (default) | "reinvested_payouts"}
    - own desk, market loss: the whole amount.
    - funded desk, market loss: 0. A loss inside a funded account is the funding firm's, the house lost what it paid for the account.
    - account cost paid by the house: the amount. Paid with reinvested payouts: 0.
    """
    source = desk.get("capital_source")
    if source not in ("own", "funded"):
        raise ValueError("desk.capital_source must be own or funded")
    kind, amount = event.get("kind"), event.get("amount_usd")
    if not _is_num(amount) or amount < 0:
        raise ValueError("event.amount_usd must be a number from 0")
    if kind == "market_loss":
        return amount if source == "own" else 0
    if kind == "account_cost":
        paid_with = event.get("paid_with", "house")
        if paid_with not in ("house", "reinvested_payouts"):
            raise ValueError("event.paid_with must be house or reinvested_payouts")
        return amount if paid_with == "house" else 0
    raise ValueError("event.kind must be market_loss or account_cost")


def _money(x: Any) -> str:
    return f"{x:,.2f}" if _is_num(x) else "to be defined" if x is None else "no data"


def _percent(x: Any) -> str:
    return f"{round2(x)}%" if _is_num(x) else "to be defined" if x is None else "no data"


def _check(check_id: str, status: str, numbers: str) -> dict:
    return {"id": check_id, "status": status, "numbers": numbers}


def _status(flag: bool | None) -> str:
    return "no_data" if flag is None else "pass" if flag else "fail"


def _checks(r: dict, limits: dict, inputs: dict) -> list[dict]:
    checks = []
    # prop accounts never add to the net worth
    if r["net_worth"] is None:
        checks.append(_check("prop_excluded_from_net_worth", "no_data", "Assets not answered yet."))
    else:
        checks.append(_check("prop_excluded_from_net_worth", "pass",
                             f"Funded accounts left out: {_money(r['prop_excluded'])}. Net worth: {_money(r['net_worth'])}."))
    if r["risk_level"] is None:
        checks.append(_check("risk_level_is_minimum", "no_data", "Capacity or willingness is not complete."))
    else:
        checks.append(_check("risk_level_is_minimum", "pass",
                             f"Capacity {r['capacity_level']} ({r['capacity_score']}/10), willingness {r['willingness_level']} "
                             f"({r['willingness_score']}/10), the lower governs: {r['risk_level']}."))
    loss, investable = r["loss_capacity"], r["investable_capital"]
    ok = None if loss is None else loss <= max(0, investable)
    checks.append(_check("loss_within_investable", _status(ok),
                         f"Loss capacity {_money(loss)} against investable capital {_money(investable)}."))
    checks.append(_check("pockets_within_loss_capacity", _status(None if r["coherence_status"] is None else r["coherence_status"] == "pass"),
                         f"Funding budget plus the house loss of both pockets: {_money(r['coherence_sum'])}. Loss capacity: {_money(loss)}."))
    spend = r["funding_spend_test"]
    checks.append(_check("funding_spend_within_budget", spend if spend in ("pass", "fail", "no_data") else "no_data",
                         f"House spend on funding {_money(r['house_spend'])}. Funding budget {_money(r['funding_budget'])}."))
    debt = r["debt_status"]
    checks.append(_check("no_debt_funds_trading", "no_data" if debt is None else debt,
                         f"Debts that fund trading: {r['debts_funding_trading']}."))
    reserve = r["reserve_status"]
    checks.append(_check("reserve_covers_target", "no_data" if reserve is None else "pass" if reserve == "covers" else "fail",
                         f"Reserve {_money(r['reserve_now'])}. Target {_money(r['reserve_target'])}."))
    risk, drawdown = limits["max_risk_per_trade_pct"], limits["max_drawdown_pct"]
    ok = risk <= drawdown if _is_num(risk) and _is_num(drawdown) else None
    checks.append(_check("risk_per_trade_within_drawdown", _status(ok),
                         f"Risk per trade {_percent(risk)}. Max drawdown {_percent(drawdown)}."))
    desks, max_loss = limits["desk_allocation"], limits["max_loss_usd"]
    limit_values = [d["house_limit_usd"] for d in desks]
    house_sum = round2(_sum(limit_values))
    if not desks or max_loss is None:
        status = "no_data"
    elif house_sum > max_loss:
        status = "fail"
    else:
        status = "no_data" if any(v is None for v in limit_values) else "pass"
    checks.append(_check("desk_house_limits_within_max_loss", status,
                         f"House limits of the desks add up to {_money(house_sum) if desks else 'to be defined'}. Fund max loss {_money(max_loss)}."))
    payouts, in_pockets = inputs["prop"]["history"]["payouts_12m_usd"], r["payouts_in_pockets"]
    if payouts is None and in_pockets == 0:
        status = "no_data"
    else:
        status = _status(in_pockets <= (payouts or 0))
    checks.append(_check("pocket_payouts_within_payouts", status,
                         f"Payouts placed in the pockets {_money(in_pockets)}. Payouts received {_money(payouts)}."))
    return checks


def run(inputs: Any, fx: Any = None) -> dict:
    """Normalize and compute: the convenient entry of ips_compute. Returns the clean inputs, the calculation and the warnings."""
    clean, warnings = normalize(to_usd(inputs, fx))
    return {"inputs": clean, **compute(clean), "warnings": warnings}

"""Early assignment model for the backtest of sold options (SPEC-020: L-56).

On each day it marks the short option legs whose holder has a reason to exercise, from prices and dates only (no random draw), so the
same bars give the same marks. A marked leg is assigned at the next open, the first moment a notice can be acted on. The rules, per short leg
that is in the money:

- Expiry: the leg expires today (days to expiration is 0). Exercise is automatic.
- No time value: the extrinsic value (price minus intrinsic) is zero or less, so exercising costs the holder nothing.
- Call before an ex-dividend date: an ex-dividend date falls 1 to ex_div_window_days days after as_of (inside the life of the option) and the extrinsic
  value is below the dividend (the early exercise rule of options.early_assignment_risk, used here as it is, including its necessary condition).
- Put: the interest on the strike until expiration, K * (1 - exp(-rate * years)), is above the extrinsic value. A dividend does not make a put
  more likely to be exercised: it raises the price of the put, which the extrinsic value already holds. The ex-dividend date is reported, not a rule.

The price of a leg is its "price" per share, or the Black-Scholes value from its "volatility" (no dividend yield). Long legs and stock are ignored.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.options import payoff as pay
from hokma.quant.options import trees
from hokma.quant.types import QuantResult

_PIECE = ("L-56",)
MAX_DIVIDENDS = 50
MAX_WINDOW_DAYS = 30
DAYS_PER_YEAR = 365.0


def _as_of_date(as_of: Any) -> date:
    """The New York calendar date of as_of (a datetime or an ISO text; a naive one is read as New York time)."""
    try:
        now = as_of if isinstance(as_of, datetime) else datetime.fromisoformat(str(as_of))
    except ValueError as exc:
        raise ValueError("as_of cannot be read: give a datetime or an ISO date text") from exc
    if now.tzinfo is None:
        return now.date()
    return now.astimezone(c.NEW_YORK).date()


def _day(label: str, value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"{label} must be a date, for example 2026-12-18") from exc


def _dividends(dividends: Any) -> list[tuple[date, float]]:
    if dividends is None:
        return []
    if not isinstance(dividends, (list, tuple)):
        raise BadInput(f"dividends must be a list of dicts, got {type(dividends).__name__}")
    if len(dividends) > MAX_DIVIDENDS:
        raise BadInput(f"dividends has more than {MAX_DIVIDENDS} entries")
    out: list[tuple[date, float]] = []
    for i, row in enumerate(dividends):
        if not isinstance(row, dict):
            raise BadInput(f"dividends[{i}] must be a dict")
        amount = c.num(f"dividends[{i}].amount", row.get("amount"))
        if not math.isfinite(amount) or amount < 0:
            raise ValueError(f"dividends[{i}]: amount must not be negative")
        out.append((_day(f"dividends[{i}].ex_date", row.get("ex_date")), amount))
    return sorted(out)


def assess(legs: Any, spot: float, as_of: Any, rate: float, dividends: Any = None, ex_div_window_days: int = 1) -> dict[str, Any]:
    """The marks of the short option legs (see the module text). Domain problems raise ValueError, wrong types BadInput."""
    if not isinstance(legs, (list, tuple)):
        raise BadInput(f"legs must be a list of dicts, got {type(legs).__name__}")
    if len(legs) > pay.MAX_LEGS:
        raise BadInput(f"legs has more than {pay.MAX_LEGS} entries")
    window = c.positive_int("ex_div_window_days", ex_div_window_days, low=1)
    if window > MAX_WINDOW_DAYS:
        raise BadInput(f"ex_div_window_days={window} is above the limit {MAX_WINDOW_DAYS}")
    divs = _dividends(dividends)
    today = _as_of_date(as_of)
    marks: list[dict[str, Any]] = []
    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            raise BadInput(f"legs[{i}] must be a dict")
        if leg.get("instrument", "option") != "option" or leg.get("position") != "short":
            continue
        kind = leg.get("kind", leg.get("type"))
        put = c.kind_flag(kind) if isinstance(kind, str) else None
        strike, qty = c.num(f"legs[{i}].strike", leg.get("strike")), c.num(f"legs[{i}].quantity", leg.get("quantity", 1))
        if put is None or not math.isfinite(strike) or strike <= 0 or not math.isfinite(qty) or qty <= 0:
            raise ValueError(f"legs[{i}]: kind must be 'call' or 'put'; strike and quantity must be positive numbers")
        expiration = _day(f"legs[{i}].expiration", leg.get("expiration"))
        dte = (expiration - today).days
        if dte < 0:
            raise ValueError(f"legs[{i}]: the expiration is before as_of")
        years = max(dte, 0) / DAYS_PER_YEAR
        intrinsic = max(strike - spot, 0.0) if put else max(spot - strike, 0.0)
        price = c.num(f"legs[{i}].price", leg.get("price"), allow_none=True)
        if price is None:
            vol = c.num(f"legs[{i}].volatility", leg.get("volatility"), allow_none=True)
            if vol is None:
                raise ValueError(f"legs[{i}]: give the price of the leg per share, or its volatility to price it")
            problem = c.bs_domain(spot, strike, rate, vol, max(years, 1e-6), 0.0)
            if problem:
                raise ValueError(f"legs[{i}]: {problem}")
            price = c.bs_price(spot, strike, rate, vol, years, 0.0, put)
        if not math.isfinite(price) or price < 0:
            raise ValueError(f"legs[{i}]: price must be a finite number that is not negative")
        extrinsic = price - intrinsic
        in_the_money = intrinsic > 0.0
        reasons: list[str] = []
        next_dividend = None
        if in_the_money:
            if dte == 0:
                reasons.append("expiry_in_the_money")
            if extrinsic <= 0.0:
                reasons.append("no_extrinsic_value")
            ahead = [(d, a) for d, a in divs if 0 < (d - today).days <= window and d <= expiration]
            next_dividend = ahead[0] if ahead else None
            if not put and next_dividend is not None:
                ex_date, amount = next_dividend
                later = [d for d, _ in divs if ex_date < d <= expiration]
                tau = ((later[0] if later else expiration) - ex_date).days / DAYS_PER_YEAR
                if tau > 0.0:
                    rule = trees.early_assignment_risk(spot=spot, strike=strike, rate=rate, dividend=amount, years_to_next_payment=tau, call_price=price)
                    if rule.value is not None and rule.value["at_risk"]:
                        reasons.append("extrinsic_below_ex_dividend")
            if put and years > 0.0 and extrinsic < strike * (1.0 - math.exp(-rate * years)):
                reasons.append("interest_on_strike_above_extrinsic")
        marks.append({"leg": i, "kind": "put" if put else "call", "strike": strike, "quantity": qty, "expiration": expiration.isoformat(),
                      "days_to_expiration": dte, "in_the_money": in_the_money, "intrinsic": intrinsic, "extrinsic": extrinsic,
                      "ex_dividend": None if next_dividend is None else {"ex_date": next_dividend[0].isoformat(), "amount": next_dividend[1]},
                      "at_risk": bool(reasons), "reasons": reasons, "assigned_at": "next_open" if reasons else None})
    return {"legs": marks, "any_at_risk": any(m["at_risk"] for m in marks), "n_short_legs": len(marks)}


@registry.register("options.assignment_risk", module="options",
                   summary="Early assignment marks for the short option legs of a position on one day, for the backtest of sold options. A short leg in the money is marked when it expires today, has no extrinsic value, is a call with its extrinsic value below the dividend of an ex-dividend date 1 to ex_div_window_days days ahead, or is a put whose extrinsic value is below the interest on its strike until expiration. A marked leg is assigned at the next open. legs: [{instrument option, position short, kind, strike, quantity, expiration, price per share or volatility}]; dividends: [{ex_date, amount}]. Long legs are ignored.",
                   inputs={"legs": {"type": "list", "required": True}, "spot": {"type": "float", "required": True},
                           "as_of": {"type": "str", "required": True}, "rate": {"type": "float", "required": True},
                           "dividends": {"type": "list", "required": False, "default": None},
                           "ex_div_window_days": {"type": "int", "required": False, "default": 1}}, piece_ids=_PIECE)
def assignment_risk(legs: list, spot: float, as_of: Any, rate: float, dividends: list | None = None, ex_div_window_days: int = 1) -> QuantResult:
    name, unit, method = "options.assignment_risk", "flags", "assignment_rules"
    s, r = c.num("spot", spot), c.num("rate", rate)
    window = c.positive_int("ex_div_window_days", ex_div_window_days, low=1)
    params = {"legs": legs if isinstance(legs, list) else [], "spot": s, "as_of": None if as_of is None else str(as_of), "rate": r,
              "dividends": dividends if isinstance(dividends, list) else [], "ex_div_window_days": window}
    if isinstance(legs, (list, tuple)) and not legs:
        return c.empty(name, unit, method, "legs must contain at least one leg", params=params, piece_ids=_PIECE)
    if not (math.isfinite(s) and math.isfinite(r)) or s <= 0 or abs(r) > 1.0:
        return c.empty(name, unit, method, "spot must be positive and finite; rate a decimal such as 0.04", params=params, piece_ids=_PIECE)
    return c.guarded(name, unit, method, _PIECE, params, lambda: assess(legs, s, as_of, r, dividends, window))

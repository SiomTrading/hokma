"""Option structures of the SWO desk: the matrix by direction and volatility view, payoff and greeks per structure,
credit or debit classification and the management plan written before entry (SPEC-019, B1; ECT-098).

The structure names are the ones of SWOProposal. Premium selling (credit structures and uncovered short legs) is closed by
a switch per desk until H4 calibrates a threshold: while it is closed the matrix offers only purchases and debit spreads.
The rules of the management plan are defaults from the playbook and are marked uncalibrated: they must come from a backtest.
"""
from __future__ import annotations

import math
from typing import Any

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.options import payoff as pay
from hokma.quant.options import position_greeks as pg
from hokma.quant.types import QuantResult

# structure -> (legs as (position, kind, strike index), strikes needed, premium class, qualitative greeks of the net position)
STRUCTURES: dict[str, dict[str, Any]] = {
    "long_call": {"legs": [("long", "call", 0)], "strikes": 1, "class": "debit", "greeks": {"delta": "+", "gamma": "+", "vega": "+", "theta": "-"}},
    "long_put": {"legs": [("long", "put", 0)], "strikes": 1, "class": "debit", "greeks": {"delta": "-", "gamma": "+", "vega": "+", "theta": "-"}},
    "debit_call_spread": {"legs": [("long", "call", 0), ("short", "call", 1)], "strikes": 2, "class": "debit", "greeks": {"delta": "+", "gamma": "~", "vega": "~", "theta": "~"}},
    "debit_put_spread": {"legs": [("long", "put", 1), ("short", "put", 0)], "strikes": 2, "class": "debit", "greeks": {"delta": "-", "gamma": "~", "vega": "~", "theta": "~"}},
    "short_put": {"legs": [("short", "put", 0)], "strikes": 1, "class": "credit", "greeks": {"delta": "+", "gamma": "-", "vega": "-", "theta": "+"}},
    "short_call": {"legs": [("short", "call", 0)], "strikes": 1, "class": "credit", "greeks": {"delta": "-", "gamma": "-", "vega": "-", "theta": "+"}},
    "credit_call_spread": {"legs": [("short", "call", 0), ("long", "call", 1)], "strikes": 2, "class": "credit", "greeks": {"delta": "-", "gamma": "-", "vega": "-", "theta": "+"}},
    "credit_put_spread": {"legs": [("short", "put", 1), ("long", "put", 0)], "strikes": 2, "class": "credit", "greeks": {"delta": "+", "gamma": "-", "vega": "-", "theta": "+"}},
    "iron_condor": {"legs": [("long", "put", 0), ("short", "put", 1), ("short", "call", 2), ("long", "call", 3)], "strikes": 4, "class": "credit",
                    "greeks": {"delta": "~", "gamma": "-", "vega": "-", "theta": "+"}},
    "short_straddle": {"legs": [("short", "call", 0), ("short", "put", 0)], "strikes": 1, "class": "credit", "greeks": {"delta": "~", "gamma": "-", "vega": "-", "theta": "+"}},
    "short_strangle": {"legs": [("short", "put", 0), ("short", "call", 1)], "strikes": 2, "class": "credit", "greeks": {"delta": "~", "gamma": "-", "vega": "-", "theta": "+"}},
    "covered_call": {"legs": [("stock", None, None), ("short", "call", 0)], "strikes": 1, "class": "credit", "greeks": {"delta": "+", "gamma": "-", "vega": "-", "theta": "+"}},
    "cash_secured_put": {"legs": [("short", "put", 0)], "strikes": 1, "class": "credit", "greeks": {"delta": "+", "gamma": "-", "vega": "-", "theta": "+"}},
}
DIRECTIONS = ("bullish", "bearish", "neutral")
VOL_VIEWS = ("rising", "flat", "falling")
# (direction, vol view) -> structures in order of preference
MATRIX: dict[tuple[str, str], tuple[str, ...]] = {
    ("bullish", "rising"): ("long_call", "debit_call_spread"),
    ("bullish", "flat"): ("debit_call_spread", "credit_put_spread", "cash_secured_put"),
    ("bullish", "falling"): ("debit_call_spread", "credit_put_spread", "short_put", "cash_secured_put", "covered_call"),
    ("bearish", "rising"): ("long_put", "debit_put_spread"),
    ("bearish", "flat"): ("debit_put_spread", "credit_call_spread"),
    ("bearish", "falling"): ("debit_put_spread", "credit_call_spread", "short_call"),
    ("neutral", "rising"): (),
    ("neutral", "flat"): ("iron_condor", "short_strangle"),
    ("neutral", "falling"): ("iron_condor", "short_strangle", "short_straddle", "covered_call"),
}
DEFAULT_PROFIT_TARGET = 0.50        # close at 50 percent of the maximum profit
DEFAULT_STOP_MULTIPLE = 2.0         # credit structures: exit when the loss reaches twice the credit received
DEFAULT_DEBIT_STOP = 0.50           # debit structures: exit when half of the premium paid is lost
DEFAULT_CLOSE_DTE = 7.0             # close with fewer days to expiration
DEFAULT_ROLL_DELTA = 0.30           # roll the threatened side when the short leg's absolute delta passes this
DEFAULT_RISK_PCT = 0.02             # of equity lost at the maximum loss of one position
MAX_RISK_PCT = 0.05
VEGA_LIMIT_PCT = 0.005              # of equity, per volatility point, for short vega


def _greek_text(g: dict[str, str]) -> dict[str, str]:
    names = {"+": "positive", "-": "negative", "~": "depends on strikes"}
    return {k: names[v] for k, v in g.items()}


@registry.register("options.structure_matrix", module="options",
                   summary="Option structures for a direction view (bullish, bearish, neutral) and a volatility view (rising, flat, falling), in order of preference, each with its premium class (debit or credit) and the signs of its net greeks. While premium_selling_open is false (the default, the state of every desk until H4 calibrates a threshold) only purchases and debit spreads are offered; the credit structures are listed as blocked with their reason.",
                   inputs={"direction": {"type": "str", "required": True}, "vol_view": {"type": "str", "required": True},
                           "premium_selling_open": {"type": "bool", "required": False, "default": False}},
                   piece_ids=("ECT-098",))
def structure_matrix(direction: str, vol_view: str, premium_selling_open: bool = False) -> QuantResult:
    name, unit, method, pieces = "options.structure_matrix", "structures", "view_matrix", ("ECT-098",)
    if not isinstance(direction, str) or not isinstance(vol_view, str):
        raise BadInput("direction and vol_view must be strings")
    if not isinstance(premium_selling_open, bool):
        raise BadInput("premium_selling_open must be true or false")
    params = {"direction": direction, "vol_view": vol_view, "premium_selling_open": premium_selling_open}
    if direction not in DIRECTIONS or vol_view not in VOL_VIEWS:
        return c.empty(name, unit, method, f"direction must be one of {DIRECTIONS} and vol_view one of {VOL_VIEWS}", params=params, piece_ids=pieces)

    def compute() -> dict:
        offered, blocked = [], []
        for st in MATRIX[(direction, vol_view)]:
            spec = STRUCTURES[st]
            item = {"structure": st, "premium_class": spec["class"], "strikes_needed": spec["strikes"], "greeks": _greek_text(spec["greeks"])}
            if spec["class"] == "credit" and not premium_selling_open:
                blocked.append({**item, "reason": "premium_selling_closed"})
            else:
                offered.append(item)
        note = None
        if not MATRIX[(direction, vol_view)]:
            note = "no structure of the desk benefits from a neutral view with rising volatility: stay out"
        elif not offered:
            note = "every structure for this view sells premium and the switch is closed: stay out or change the view"
        return {"offered": offered, "blocked": blocked, "note": note}

    return c.guarded(name, unit, method, pieces, params, compute)


def covered_short_legs(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Short option legs that no long leg of the same kind, at least the same quantity and the same or a later expiration covers."""
    longs = [dict(leg) for leg in legs if leg.get("position") == "long" and leg.get("instrument", "option") == "option"]
    uncovered = []
    for leg in legs:
        if leg.get("position") != "short" or leg.get("instrument", "option") != "option":
            continue
        match = next((x for x in longs if x.get("kind") == leg.get("kind") and float(x.get("quantity", 1)) >= float(leg.get("quantity", 1))
                      and str(x.get("expiration") or "9999-12-31")[:10] >= str(leg.get("expiration") or "0000-01-01")[:10]), None)
        if match is None:
            uncovered.append(leg)
        else:
            longs.remove(match)
    return uncovered


@registry.register("options.classify_structure", module="options",
                   summary="Classify the legs of one order as debit, credit or even by net premium, and say whether it sells premium: a net credit, or a short option leg that no long leg of the same kind, at least the same quantity and the same or a later expiration covers. Purchases and debit spreads do not sell premium. The premium switch of the desk reads this classification.",
                   inputs={"legs": {"type": "list", "required": True}}, piece_ids=("ECT-098",))
def classify_structure(legs: list) -> QuantResult:
    name, unit, method, pieces = "options.classify_structure", "flags", "net_premium_and_coverage", ("ECT-098",)
    if not isinstance(legs, (list, tuple)):
        raise BadInput(f"legs must be a list of dicts, got {type(legs).__name__}")
    if len(legs) > pay.MAX_LEGS:
        raise BadInput(f"legs has more than {pay.MAX_LEGS} entries")
    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            raise BadInput(f"legs[{i}] must be a dict")
    params = {"legs": list(legs)}
    if not legs:
        return c.empty(name, unit, method, "legs must contain at least one leg", params=params, piece_ids=pieces)

    def compute() -> dict:
        net = 0.0
        for i, leg in enumerate(legs):
            if leg.get("position") not in ("long", "short") or leg.get("instrument", "option") not in ("option", "stock"):
                raise ValueError(f"legs[{i}]: position must be 'long' or 'short' and instrument 'option' or 'stock'")
            if leg.get("instrument", "option") == "stock":
                continue
            premium, qty = c.num(f"legs[{i}].premium", leg.get("premium")), c.num(f"legs[{i}].quantity", leg.get("quantity", 1))
            if not (math.isfinite(premium) and math.isfinite(qty)) or premium < 0 or qty <= 0:
                raise ValueError(f"legs[{i}]: premium must not be negative and quantity must be positive")
            net += (1 if leg["position"] == "long" else -1) * premium * qty
        uncovered = covered_short_legs(list(legs))
        kind = "debit" if net > 1e-12 else ("credit" if net < -1e-12 else "even")
        return {"premium_type": kind, "net_premium_per_share_sum": net, "uncovered_short_legs": uncovered,
                "premium_selling": bool(kind == "credit" or uncovered)}

    return c.guarded(name, unit, method, pieces, params, compute)


def build_legs(structure: str, strikes: list[float], premiums: list[float], quantity: float, multiplier: float, spot: float | None) -> list[pay.Leg]:
    """The legs of a named structure from its ordered strikes (low to high) and the per-share premium of each option leg."""
    spec = STRUCTURES[structure]
    options = [x for x in spec["legs"] if x[0] != "stock"]
    if len(strikes) != spec["strikes"] or len(premiums) != len(options):
        raise ValueError(f"{structure} needs {spec['strikes']} strike(s) and {len(options)} premium(s), got {len(strikes)} and {len(premiums)}")
    if any(strikes[i] >= strikes[i + 1] for i in range(len(strikes) - 1)):
        raise ValueError("strikes must be strictly increasing")
    legs: list[pay.Leg] = []
    it = iter(premiums)
    for position, kind, idx in spec["legs"]:
        if position == "stock":
            if spot is None or spot <= 0:
                raise ValueError("covered_call needs the spot price as the entry price of the shares")
            legs.append(pay.Leg("stock", 1, False, None, spot, quantity * multiplier, 1.0))
            continue
        prem = next(it)
        if not math.isfinite(prem) or prem < 0:
            raise ValueError("premiums must be finite and not negative")
        legs.append(pay.Leg("option", 1 if position == "long" else -1, kind == "put", strikes[idx], prem, quantity, multiplier))
    return legs


@registry.register("options.structure_profile", module="options",
                   summary="Payoff profile of a named structure in dollars: net premium (positive = debit), maximum profit and loss (None when unbounded; cash secured put and covered call included), breakevens, premium class and, with volatility, rate and time, its net greeks. strikes go low to high; premiums are per share, one per option leg in the order of the legs field of the answer; quantity is in contracts and the contract multiplier is required. With p_profit it also gives the expected value and warns when it is not positive; with volatility and time it gives the distance of each short strike in standard deviations.",
                   inputs={"structure": {"type": "str", "required": True}, "strikes": {"type": "list", "required": True}, "premiums": {"type": "list", "required": True},
                           "quantity": {"type": "float", "required": True}, "multiplier": {"type": "float", "required": True},
                           "spot": {"type": "float", "required": False, "default": None}, "p_profit": {"type": "float", "required": False, "default": None},
                           "rate": {"type": "float", "required": False, "default": None}, "volatility": {"type": "float", "required": False, "default": None},
                           "time_to_expiration": {"type": "float", "required": False, "default": None}, "dividend_yield": {"type": "float", "required": False, "default": 0.0}},
                   piece_ids=("ECT-098",))
def structure_profile(structure: str, strikes: list, premiums: list, quantity: float, multiplier: float, spot: float | None = None,
                      p_profit: float | None = None, rate: float | None = None, volatility: float | None = None,
                      time_to_expiration: float | None = None, dividend_yield: float = 0.0) -> QuantResult:
    name, unit, method, pieces = "options.structure_profile", "usd", "structure_payoff_and_greeks", ("ECT-098",)
    if not isinstance(structure, str) or not isinstance(strikes, (list, tuple)) or not isinstance(premiums, (list, tuple)):
        raise BadInput("structure must be a string; strikes and premiums lists of numbers")
    ks, ps = [c.num("strikes[]", x) for x in strikes], [c.num("premiums[]", x) for x in premiums]
    qty, mult = c.num("quantity", quantity), c.num("multiplier", multiplier)
    s = c.num("spot", spot, allow_none=True)
    pw, r, v = c.num("p_profit", p_profit, allow_none=True), c.num("rate", rate, allow_none=True), c.num("volatility", volatility, allow_none=True)
    t, q = c.num("time_to_expiration", time_to_expiration, allow_none=True), c.num("dividend_yield", dividend_yield)
    params = {"structure": structure, "strikes": ks, "premiums": ps, "quantity": qty, "multiplier": mult, "spot": s, "p_profit": pw, "rate": r,
              "volatility": v, "time_to_expiration": t, "dividend_yield": q}
    if structure not in STRUCTURES:
        return c.empty(name, unit, method, f"structure must be one of {sorted(STRUCTURES)}", params=params, piece_ids=pieces)
    if not all(math.isfinite(x) for x in ks + ps + [qty, mult, q]) or qty <= 0 or mult <= 0 or any(k <= 0 for k in ks):
        return c.empty(name, unit, method, "strikes, quantity and multiplier must be positive finite numbers; premiums finite and not negative", params=params, piece_ids=pieces)
    if pw is not None and not 0.0 <= pw <= 1.0:
        return c.empty(name, unit, method, "p_profit must be in [0, 1]", params=params, piece_ids=pieces)
    warns: list[str] = []

    def compute() -> dict:
        legs = build_legs(structure, ks, ps, qty, mult, s)
        info = pay.profile(legs)
        net = pay.net_premium_usd(legs)
        spec = STRUCTURES[structure]
        out: dict[str, Any] = {**info, "legs": [{"instrument": leg.instrument, "position": "long" if leg.sign > 0 else "short",
                                                 "kind": None if leg.instrument == "stock" else ("put" if leg.put else "call"), "strike": leg.strike,
                                                 "premium": leg.premium, "quantity": leg.quantity} for leg in legs], "net_premium_usd": net, "premium_type": "debit" if net > 1e-12 else ("credit" if net < -1e-12 else "even"),
                               "expected_class": spec["class"], "greeks_signs": _greek_text(spec["greeks"])}
        if out["premium_type"] != spec["class"]:
            warns.append(f"the premiums given make this {out['premium_type']} while {structure} is normally a {spec['class']}: check the quotes")
        if pw is not None and info["max_profit_usd"] is not None and info["max_loss_usd"] is not None:
            ev = pw * info["max_profit_usd"] - (1.0 - pw) * info["max_loss_usd"]
            out["expected_value_usd"] = ev
            if ev <= 0:
                warns.append(f"expected value is {ev:.2f} dollars at p_profit={pw:g}: the structure loses money on its own numbers")
        elif pw is not None:
            warns.append("expected value needs bounded profit and loss: it was not computed")
        if v is not None and t is not None and s is not None and v > 0 and t > 0 and s > 0:
            out["short_strike_distance_sigma"] = [{"strike": leg.strike, "kind": "put" if leg.put else "call",
                                                   "sigma": abs(math.log(leg.strike / s)) / (v * math.sqrt(t))}
                                                  for leg in legs if leg.instrument == "option" and leg.sign < 0]
        if None not in (s, r, v, t) and v > 0 and t > 0:       # type: ignore[comparison-overlap]
            greek_legs = [{"instrument": "stock", "quantity": leg.quantity} if leg.instrument == "stock" else
                          {"kind": "put" if leg.put else "call", "strike": leg.strike, "volatility": v, "time_to_expiration": t,
                           "quantity": leg.sign * leg.quantity, "multiplier": leg.multiplier} for leg in legs]
            res = pg.position_greeks(greek_legs, s, r, q, mult)
            out["greeks"] = None if res.value is None else res.value["totals"]
            if res.value is None:
                warns.extend(res.warnings)
        return out

    result = c.guarded(name, unit, method, pieces, params, compute)
    return QuantResult(name=result.name, value=result.value, unit=result.unit, method=result.method, params=result.params,
                       inputs_hash=result.inputs_hash, as_of=result.as_of, warnings=result.warnings + tuple(warns), piece_ids=result.piece_ids)


@registry.register("options.management_plan", module="options",
                   summary="Management plan to write before entering a structure, from its dollar figures per unit: profit target (default 50 percent of maximum profit), stop (credit structures: loss of twice the credit; debit structures: loss of half the premium paid), time exit (default close with fewer than 7 days), roll rule (default roll the threatened side when the short delta passes 0.30), and size by maximum loss (default 2 percent of equity, never above 5 percent) with the short vega ceiling of 0.5 percent of equity per point. The defaults come from the playbook, not from a backtest: the result says so, and any threshold given by the desk replaces its default.",
                   inputs={"structure": {"type": "str", "required": True}, "net_premium_usd": {"type": "float", "required": True},
                           "max_profit_usd": {"type": "float", "required": True}, "max_loss_usd": {"type": "float", "required": True},
                           "days_to_expiration": {"type": "float", "required": True}, "equity_usd": {"type": "float", "required": True},
                           "risk_pct": {"type": "float", "required": False, "default": None}, "profit_target": {"type": "float", "required": False, "default": None},
                           "stop_multiple": {"type": "float", "required": False, "default": None}, "debit_stop": {"type": "float", "required": False, "default": None},
                           "close_dte": {"type": "float", "required": False, "default": None}, "roll_delta": {"type": "float", "required": False, "default": None}},
                   piece_ids=("ECT-098",))
def management_plan(structure: str, net_premium_usd: float, max_profit_usd: float, max_loss_usd: float, days_to_expiration: float,
                    equity_usd: float, risk_pct: float | None = None, profit_target: float | None = None, stop_multiple: float | None = None,
                    debit_stop: float | None = None, close_dte: float | None = None, roll_delta: float | None = None) -> QuantResult:
    name, unit, method, pieces = "options.management_plan", "usd", "pre_entry_management_plan", ("ECT-098",)
    if not isinstance(structure, str):
        raise BadInput("structure must be a string")
    net, prof, loss, dte, eq = (c.num("net_premium_usd", net_premium_usd), c.num("max_profit_usd", max_profit_usd), c.num("max_loss_usd", max_loss_usd),
                                c.num("days_to_expiration", days_to_expiration), c.num("equity_usd", equity_usd))
    given = {"risk_pct": c.num("risk_pct", risk_pct, allow_none=True), "profit_target": c.num("profit_target", profit_target, allow_none=True),
             "stop_multiple": c.num("stop_multiple", stop_multiple, allow_none=True), "debit_stop": c.num("debit_stop", debit_stop, allow_none=True),
             "close_dte": c.num("close_dte", close_dte, allow_none=True), "roll_delta": c.num("roll_delta", roll_delta, allow_none=True)}
    defaults = {"risk_pct": DEFAULT_RISK_PCT, "profit_target": DEFAULT_PROFIT_TARGET, "stop_multiple": DEFAULT_STOP_MULTIPLE,
                "debit_stop": DEFAULT_DEBIT_STOP, "close_dte": DEFAULT_CLOSE_DTE, "roll_delta": DEFAULT_ROLL_DELTA}
    used = {k: (given[k] if given[k] is not None else defaults[k]) for k in defaults}
    params = {"structure": structure, "net_premium_usd": net, "max_profit_usd": prof, "max_loss_usd": loss, "days_to_expiration": dte, "equity_usd": eq, **given}
    if structure not in STRUCTURES:
        return c.empty(name, unit, method, f"structure must be one of {sorted(STRUCTURES)}", params=params, piece_ids=pieces)
    if not all(math.isfinite(x) for x in [net, prof, loss, dte, eq] + list(used.values())) or loss <= 0 or prof <= 0 or eq <= 0 or dte < 0:
        return c.empty(name, unit, method, "max_profit_usd, max_loss_usd and equity_usd must be positive; days not negative; all finite", params=params, piece_ids=pieces)
    if not 0.0 < used["risk_pct"] <= MAX_RISK_PCT:
        return c.empty(name, unit, method, f"risk_pct must be in (0, {MAX_RISK_PCT:g}]", params=params, piece_ids=pieces)
    if not (0.0 < used["profit_target"] <= 1.0 and 0.0 < used["debit_stop"] <= 1.0 and used["stop_multiple"] > 0 and used["close_dte"] >= 0 and 0.0 < used["roll_delta"] < 1.0):
        return c.empty(name, unit, method, "profit_target and debit_stop must be in (0, 1], stop_multiple positive, close_dte not negative, roll_delta in (0, 1)",
                       params=params, piece_ids=pieces)

    def compute() -> dict:
        spec = STRUCTURES[structure]
        credit = spec["class"] == "credit"
        premium = abs(net)
        stop_loss = used["stop_multiple"] * premium if credit else used["debit_stop"] * premium
        stop_loss = min(stop_loss, loss)
        budget = eq * used["risk_pct"]
        contracts = int(math.floor(budget / loss))
        rules_source = {k: ("desk" if given[k] is not None else "default_uncalibrated") for k in used}
        return {"profit_target_usd": used["profit_target"] * prof, "stop_loss_usd": stop_loss, "close_at_or_below_dte": used["close_dte"],
                "must_close_now": dte <= used["close_dte"],
                "roll_rule": ({"roll_threatened_side_when_short_abs_delta_above": used["roll_delta"]} if credit else None),
                "risk_budget_usd": budget, "max_contracts": contracts, "size_ok": contracts >= 1,
                "short_vega_limit_usd_per_point": VEGA_LIMIT_PCT * eq if credit else None, "premium_class": spec["class"], "rules_source": rules_source}

    warns = ["management rules are playbook defaults, not backtested: calibrate them in H4 before relying on them"] if any(v is None for v in given.values()) else ()
    return c.guarded(name, unit, method, pieces, params, compute, warns)

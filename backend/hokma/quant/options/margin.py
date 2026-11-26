"""Margin of a group of option positions: spreads, condors, covered calls and uncovered shorts (SPEC-020: L-21).

The group is one underlying and one expiration. Dollars use the contract multiplier of the contract sheet, which is required (no default).
The rules, written from the usual Reg T style requirements, are:

- A long stock leg covers short calls, multiplier shares per contract: covered contracts add nothing.
- A short option paired with a long option of the same kind is a vertical spread. A short call with a long call at a higher strike needs
  that width; one with a long call at the same or a lower strike needs nothing. A short put needs the width to a long put at a lower
  strike and nothing against one at the same or a higher strike. Each short takes the long that needs the least.
- Put side and call side of spreads cannot both lose at expiry: the spreads need the larger of the two sides (an iron condor needs its wider wing).
- A short option left alone needs, per share, the premium plus the larger of naked_pct of the spot minus the out of the money amount, and
  naked_floor_pct of the strike (of the spot for a call). Short puts and short calls together (strangle, straddle) need the larger of the two
  sides plus the premium of the smaller side. A cash secured put needs its strike in cash.
- The margin is the requirement minus the net credit of the legs that are not covered by shares, never below zero. For a debit position there is
  no requirement and the margin is the net debit paid (the cash the position ties up). Spreads and naked legs add: the sum is conservative when
  both appear in one group.

The percentages are the usual Reg T numbers and are parameters: the broker of the account decides what applies.
"""
from __future__ import annotations

import math
from typing import Any

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.options import payoff as pay
from hokma.quant.options import structures
from hokma.quant.types import QuantResult

_PIECE = ("L-21",)
DEFAULT_NAKED_PCT = 0.20
DEFAULT_NAKED_FLOOR_PCT = 0.10


def _naked_per_share(put: bool, strike: float, spot: float, naked_pct: float, floor_pct: float, cash_secured: bool) -> float:
    if put and cash_secured:
        return strike
    out_of_money = max(spot - strike, 0.0) if put else max(strike - spot, 0.0)
    return max(naked_pct * spot - out_of_money, floor_pct * (strike if put else spot))


def _width(short_strike: float, long_strike: float, put: bool) -> float:
    """Points at risk of a short option against a long option of the same kind: zero when the long one is the same or further in the money."""
    return max(short_strike - long_strike, 0.0) if put else max(long_strike - short_strike, 0.0)


def _spread_requirement(shorts: list[list[Any]], longs: list[list[Any]], put: bool) -> float:
    """Dollars needed by the vertical spreads of one kind; the quantities in shorts and longs are used up as they are paired."""
    total = 0.0
    for short in sorted(shorts, key=lambda item: item[0].strike):
        while short[1] > 1e-12:
            open_longs = [item for item in longs if item[1] > 1e-12]
            if not open_longs:
                break
            long = min(open_longs, key=lambda item: _width(short[0].strike, item[0].strike, put))
            paired = min(short[1], long[1])
            total += _width(short[0].strike, long[0].strike, put) * paired * short[0].multiplier
            short[1] -= paired
            long[1] -= paired
    return total


def group_margin(legs: list[pay.Leg], spot: float | None, naked_pct: float = DEFAULT_NAKED_PCT, naked_floor_pct: float = DEFAULT_NAKED_FLOOR_PCT,
                 cash_secured: bool = False) -> dict[str, Any]:
    """Margin of parsed legs (see the module text). Raises ValueError for a short stock leg, or a spot that is missing when a short option is alone."""
    if any(leg.instrument == "stock" and leg.sign < 0 for leg in legs):
        raise ValueError("short stock legs are not margined by this function")
    shares = sum(leg.quantity for leg in legs if leg.instrument == "stock")
    shorts = [[leg, leg.quantity] for leg in legs if leg.instrument == "option" and leg.sign < 0]
    longs = [[leg, leg.quantity] for leg in legs if leg.instrument == "option" and leg.sign > 0]
    covered_credit, covered_contracts = 0.0, 0.0
    for item in sorted((s for s in shorts if not s[0].put), key=lambda s: s[0].strike):
        leg = item[0]
        cover = min(item[1], math.floor(shares / leg.multiplier + 1e-9))
        if cover > 0:
            shares -= cover * leg.multiplier
            item[1] -= cover
            covered_contracts += cover
            covered_credit += leg.premium * cover * leg.multiplier
    sides = {}
    for put in (False, True):
        sides[put] = _spread_requirement([s for s in shorts if s[0].put == put], [x for x in longs if x[0].put == put], put)
    spreads = max(sides.values())
    naked = {False: 0.0, True: 0.0}
    naked_premium = {False: 0.0, True: 0.0}
    for leg, left in shorts:
        if left <= 1e-12:
            continue
        if spot is None:
            raise ValueError("spot is required when a short option is not covered by a long option or by shares")
        per_share = _naked_per_share(leg.put, leg.strike, spot, naked_pct, naked_floor_pct, cash_secured)
        premium = leg.premium * left * leg.multiplier
        naked[leg.put] += (per_share + (0.0 if (leg.put and cash_secured) else leg.premium)) * left * leg.multiplier
        naked_premium[leg.put] += premium
    if naked[False] > 0 and naked[True] > 0 and not cash_secured:
        big = max(naked, key=naked.get)
        naked_total = naked[big] + naked_premium[not big]
    else:
        naked_total = naked[False] + naked[True]
    credit = sum(-leg.sign * leg.premium * leg.quantity * leg.multiplier for leg in legs if leg.instrument == "option") - covered_credit
    requirement = spreads + naked_total
    return {"margin_usd": max(requirement - credit, 0.0), "requirement_usd": requirement, "net_credit_usd": credit,
            "spread_requirement_usd": spreads, "naked_requirement_usd": naked_total, "covered_contracts": covered_contracts}


@registry.register("options.strategy_margin", module="options",
                   summary="Margin of a group of option legs of one underlying and one expiration, in dollars: spreads need their width, an iron condor its wider wing, covered calls nothing, a short option alone the Reg T style formula (naked_pct of the spot minus the out of the money amount, or naked_floor_pct of the strike, plus the premium), a cash secured put its strike. The margin is the requirement minus the net credit, never below zero; for debit positions it is the net debit paid. legs: [{instrument option or stock, position, kind, strike, premium per share, quantity in contracts (shares for stock), multiplier}]; the contract multiplier is required (no default); spot is required for a short option that is not in a spread or covered.",
                   inputs={"legs": {"type": "list", "required": True}, "multiplier": {"type": "float", "required": False, "default": None},
                           "spot": {"type": "float", "required": False, "default": None},
                           "naked_pct": {"type": "float", "required": False, "default": DEFAULT_NAKED_PCT},
                           "naked_floor_pct": {"type": "float", "required": False, "default": DEFAULT_NAKED_FLOOR_PCT},
                           "cash_secured": {"type": "bool", "required": False, "default": False}}, piece_ids=_PIECE)
def strategy_margin(legs: list, multiplier: float | None = None, spot: float | None = None, naked_pct: float = DEFAULT_NAKED_PCT,
                    naked_floor_pct: float = DEFAULT_NAKED_FLOOR_PCT, cash_secured: bool = False) -> QuantResult:
    name, unit, method = "options.strategy_margin", "usd", "group_margin"
    ref = c.num("spot", spot, allow_none=True)
    pct, floor_pct = c.num("naked_pct", naked_pct), c.num("naked_floor_pct", naked_floor_pct)
    if not isinstance(cash_secured, bool):
        raise BadInput("cash_secured must be true or false")
    params = {"legs": legs if isinstance(legs, list) else [], "multiplier": multiplier, "spot": ref, "naked_pct": pct, "naked_floor_pct": floor_pct,
              "cash_secured": cash_secured}
    if isinstance(legs, (list, tuple)) and not legs:
        return c.empty(name, unit, method, "legs must contain at least one leg", params=params, piece_ids=_PIECE)
    if not (math.isfinite(pct) and math.isfinite(floor_pct)) or not 0.0 < floor_pct <= pct <= 1.0 or (ref is not None and (not math.isfinite(ref) or ref <= 0)):
        return c.empty(name, unit, method, "naked_pct and naked_floor_pct must be in (0, 1] with the floor not above the pct; spot must be positive and finite",
                       params=params, piece_ids=_PIECE)
    try:
        parsed = pay.parse_legs(legs, multiplier)
    except BadInput:
        raise
    except ValueError as exc:
        return c.empty(name, unit, method, str(exc), params=params, piece_ids=_PIECE)

    def compute() -> dict:
        value = group_margin(parsed, ref, pct, floor_pct, cash_secured)
        try:
            found = structures.match(legs, multiplier)
        except ValueError:
            found = None
        value["structure"] = None if found is None else found.structure
        return value

    return c.guarded(name, unit, method, _PIECE, params, compute)

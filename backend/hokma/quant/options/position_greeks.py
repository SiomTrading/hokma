"""Greeks of a position in dollars, stress by full revaluation, delta and vega rebalancing rules and gamma scalping
arithmetic (SPEC-019, B1; ECT-031).

The contract multiplier always comes from the contract sheet (H2) and is required: no number is written here.
"""
from __future__ import annotations

import math
from typing import Any

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.options import greeks as g
from hokma.quant.types import QuantResult

MAX_LEGS = 40
MAX_SHOCKS = 25
DEFAULT_SPOT_SHOCKS = (-0.20, -0.10, 0.0, 0.10, 0.20)
DEFAULT_VOL_SHOCKS = (-5.0, 0.0, 5.0)
GREEK_KEYS = ("delta_shares", "delta_usd", "gamma_per_dollar", "gamma_usd_per_1pct", "vega_usd_per_point", "theta_usd_per_day",
              "rho_usd_per_point", "vanna_shares_per_point", "charm_shares_per_day", "vomma_usd_per_point")


def _parse_legs(legs: Any, multiplier: Any, as_of: Any) -> list[dict[str, Any]]:
    if not isinstance(legs, (list, tuple)):
        raise BadInput(f"legs must be a list of dicts, got {type(legs).__name__}")
    if len(legs) > MAX_LEGS:
        raise BadInput(f"legs has more than {MAX_LEGS} entries")
    top = c.num("multiplier", multiplier, allow_none=True)
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(legs):
        if not isinstance(raw, dict):
            raise BadInput(f"legs[{i}] must be a dict")
        instrument = raw.get("instrument", "option")
        qty = c.num(f"legs[{i}].quantity", raw.get("quantity"))
        if instrument not in ("option", "stock") or not math.isfinite(qty) or qty == 0:
            raise ValueError(f"legs[{i}]: instrument must be 'option' or 'stock' and quantity a signed number that is not zero (negative = short)")
        if instrument == "stock":
            out.append({"instrument": "stock", "quantity": qty, "multiplier": 1.0})
            continue
        put = c.kind_flag(raw.get("kind", ""))
        mult = c.num(f"legs[{i}].multiplier", raw.get("multiplier"), allow_none=True)
        mult = top if mult is None else mult
        strike, vol = c.num(f"legs[{i}].strike", raw.get("strike")), c.num(f"legs[{i}].volatility", raw.get("volatility"))
        t = c.num(f"legs[{i}].time_to_expiration", raw.get("time_to_expiration"), allow_none=True)
        if t is None:
            if raw.get("expiration") is None or as_of is None:
                raise ValueError(f"legs[{i}]: give time_to_expiration, or expiration with as_of")
            t = c.years_between(as_of, raw["expiration"])
            if t is None:
                raise ValueError(f"legs[{i}]: the expiration or as_of cannot be read")
        if put is None:
            raise ValueError(f"legs[{i}]: kind must be 'call' or 'put'")
        if mult is None:
            raise ValueError(f"legs[{i}]: the contract multiplier is required (take it from the contract sheet); there is no default")
        if not all(math.isfinite(x) for x in (strike, vol, t, mult)) or strike <= 0 or mult <= 0 or vol <= 0 or t <= 0:
            raise ValueError(f"legs[{i}]: strike, multiplier, volatility and time to expiration must be positive numbers")
        out.append({"instrument": "option", "put": put, "kind": "put" if put else "call", "strike": strike, "volatility": vol,
                    "time_to_expiration": t, "quantity": qty, "multiplier": mult})
    return out


def _shares(leg: dict[str, Any]) -> float:
    return leg["quantity"] * leg["multiplier"]


def _leg_greeks(leg: dict[str, Any], s: float, r: float, q: float) -> dict[str, float]:
    """Dollar greeks of one leg (signed quantity times multiplier)."""
    n = _shares(leg)
    if leg["instrument"] == "stock":
        return {"delta_shares": n, "delta_usd": n * s, "gamma_per_dollar": 0.0, "gamma_usd_per_1pct": 0.0, "vega_usd_per_point": 0.0,
                "theta_usd_per_day": 0.0, "rho_usd_per_point": 0.0, "vanna_shares_per_point": 0.0, "charm_shares_per_day": 0.0, "vomma_usd_per_point": 0.0}
    args = (s, leg["strike"], r, leg["volatility"], leg["time_to_expiration"], q)
    first = g.first_order(*args, kind=leg["kind"]).value
    second = g.second_order(*args, kind=leg["kind"]).value
    gamma = g.gamma(*args).value
    vanna = g.vanna(*args).value
    if first is None or second is None or gamma is None or vanna is None:
        raise ValueError("a greek of one leg is outside the Black-Scholes domain")
    return {"delta_shares": n * first["delta"], "delta_usd": n * first["delta"] * s, "gamma_per_dollar": n * gamma,
            "gamma_usd_per_1pct": n * gamma * s * s / 100.0, "vega_usd_per_point": n * first["vega"], "theta_usd_per_day": n * first["theta"],
            "rho_usd_per_point": n * first["rho"] / 100.0, "vanna_shares_per_point": n * vanna / 100.0, "charm_shares_per_day": n * second["charm"] / 365.0,
            "vomma_usd_per_point": n * second["vomma"] / 10_000.0}


def _revalue(leg: dict[str, Any], s: float, r: float, q: float, spot_shock: float, vol_shock_points: float) -> float:
    """Profit and loss of one leg after a spot shock (fraction) and a volatility shock (points), by full Black-Scholes revaluation."""
    n = _shares(leg)
    shocked = s * (1.0 + spot_shock)
    if leg["instrument"] == "stock":
        return n * (shocked - s)
    vol_after = max(leg["volatility"] + vol_shock_points / 100.0, 1e-4)
    base = c.bs_price(s, leg["strike"], r, leg["volatility"], leg["time_to_expiration"], q, leg["put"])
    after = c.bs_price(shocked, leg["strike"], r, vol_after, leg["time_to_expiration"], q, leg["put"])
    return n * (after - base)


def _shock_list(label: str, values: Any, default: tuple[float, ...]) -> list[float]:
    if values is None:
        return list(default)
    if not isinstance(values, (list, tuple)) or len(values) > MAX_SHOCKS:
        raise BadInput(f"{label} must be a list of at most {MAX_SHOCKS} numbers")
    return [c.num(f"{label}[]", x) for x in values]


@registry.register("options.position_greeks", module="options",
                   summary="Greeks of an option position in dollars. legs: [{instrument option or stock, kind, strike, volatility, quantity (signed: negative = short), time_to_expiration in years or expiration with as_of, multiplier}] with the contract multiplier of the contract sheet (required, no default). Returns total delta (shares and dollars), gamma, vega per point, theta per day, rho in dollars, vanna in shares per volatility point, charm in shares per day and vomma in dollars of vega per point; a full revaluation stress (spot shocks default -20, -10, 0, 10, 20 percent and volatility shocks -5, 0, 5 points); and a pin risk flag for short legs close to the strike near expiry.",
                   inputs={"legs": {"type": "list", "required": True}, "spot": {"type": "float", "required": True},
                           "rate": {"type": "float", "required": True}, "dividend_yield": {"type": "float", "required": False, "default": 0.0},
                           "multiplier": {"type": "float", "required": False, "default": None}, "as_of": {"type": "str", "required": False, "default": None},
                           "spot_shocks": {"type": "list", "required": False, "default": None}, "vol_shocks": {"type": "list", "required": False, "default": None},
                           "pin_days": {"type": "float", "required": False, "default": 3.0}, "pin_band": {"type": "float", "required": False, "default": 0.01}},
                   piece_ids=("ECT-031",))
def position_greeks(legs: list, spot: float, rate: float, dividend_yield: float = 0.0, multiplier: float | None = None, as_of: Any = None,
                    spot_shocks: list | None = None, vol_shocks: list | None = None, pin_days: float = 3.0, pin_band: float = 0.01) -> QuantResult:
    name, unit, method, pieces = "options.position_greeks", "usd", "position_greeks_and_revaluation_stress", ("ECT-031",)
    s, r, q = c.num("spot", spot), c.num("rate", rate), c.num("dividend_yield", dividend_yield)
    pdays, pband = c.num("pin_days", pin_days), c.num("pin_band", pin_band)
    spot_list = _shock_list("spot_shocks", spot_shocks, DEFAULT_SPOT_SHOCKS)
    vol_list = _shock_list("vol_shocks", vol_shocks, DEFAULT_VOL_SHOCKS)
    params = {"legs": legs if isinstance(legs, list) else [], "spot": s, "rate": r, "dividend_yield": q, "multiplier": multiplier,
              "as_of": None if as_of is None else str(as_of), "spot_shocks": spot_list, "vol_shocks": vol_list, "pin_days": pdays, "pin_band": pband}
    if isinstance(legs, (list, tuple)) and not legs:
        return c.empty(name, unit, method, "legs must contain at least one leg", params=params, piece_ids=pieces)
    if not all(math.isfinite(x) for x in (s, r, q, pdays, pband)) or s <= 0 or pdays < 0 or pband < 0:
        return c.empty(name, unit, method, "spot must be positive; pin_days and pin_band must not be negative; all finite", params=params, piece_ids=pieces)
    if any(not math.isfinite(x) or x <= -1.0 for x in spot_list) or any(not math.isfinite(x) for x in vol_list):
        return c.empty(name, unit, method, "spot shocks must be fractions above -1 and finite; vol shocks finite points", params=params, piece_ids=pieces)
    try:
        parsed = _parse_legs(legs, multiplier, as_of)
    except BadInput:
        raise
    except ValueError as exc:
        return c.empty(name, unit, method, str(exc), params=params, piece_ids=pieces)

    def compute() -> dict:
        per_leg, totals = [], {key: 0.0 for key in GREEK_KEYS}
        for leg in parsed:
            row = _leg_greeks(leg, s, r, q)
            for key in GREEK_KEYS:
                totals[key] += row[key]
            per_leg.append({"instrument": leg["instrument"], "kind": leg.get("kind"), "strike": leg.get("strike"), "quantity": leg["quantity"], **row})
        stress = []
        for sh in spot_list:
            for vs in vol_list:
                stress.append({"spot_shock": sh, "vol_shock_points": vs, "pnl_usd": sum(_revalue(leg, s, r, q, sh, vs) for leg in parsed)})
        worst = min(stress, key=lambda x: x["pnl_usd"])
        pins = [{"strike": leg["strike"], "kind": leg["kind"], "quantity": leg["quantity"], "days_to_expiration": leg["time_to_expiration"] * 365.0}
                for leg in parsed if leg["instrument"] == "option" and leg["quantity"] < 0
                and leg["time_to_expiration"] * 365.0 <= pdays and abs(s - leg["strike"]) / s <= pband]
        return {"totals": totals, "legs": per_leg, "stress": stress, "worst_case": worst, "pin_risk": bool(pins), "pin_legs": pins}

    return c.guarded(name, unit, method, pieces, params, compute)


@registry.register("options.rebalance_check", module="options",
                   summary="Rebalance rules for a position: when the absolute delta in shares is above the desk band the stock hedge that brings it to zero is returned; when the absolute vega in dollars per point is above the desk limit the vega to remove is returned. The band and the limit are the desk's own and are required: no default threshold exists.",
                   inputs={"delta_shares": {"type": "float", "required": True}, "vega_usd_per_point": {"type": "float", "required": True},
                           "delta_band_shares": {"type": "float", "required": True}, "vega_limit_usd": {"type": "float", "required": True}},
                   piece_ids=("ECT-031",))
def rebalance_check(delta_shares: float, vega_usd_per_point: float, delta_band_shares: float, vega_limit_usd: float) -> QuantResult:
    name, unit, method, pieces = "options.rebalance_check", "actions", "delta_vega_band_rules", ("ECT-031",)
    d, v, band, limit = (c.num("delta_shares", delta_shares), c.num("vega_usd_per_point", vega_usd_per_point),
                         c.num("delta_band_shares", delta_band_shares), c.num("vega_limit_usd", vega_limit_usd))
    params = {"delta_shares": d, "vega_usd_per_point": v, "delta_band_shares": band, "vega_limit_usd": limit}
    if not all(math.isfinite(x) for x in (d, v, band, limit)) or band < 0 or limit < 0:
        return c.empty(name, unit, method, "inputs must be finite; the band and the limit must not be negative", params=params, piece_ids=pieces)

    def compute() -> dict:
        out: dict[str, Any] = {"delta_action": None, "vega_action": None}
        if abs(d) > band:
            out["delta_action"] = {"side": "sell" if d > 0 else "buy", "shares": abs(d), "reason": "absolute delta is above the band"}
        if abs(v) > limit:
            out["vega_action"] = {"vega_to_remove_usd_per_point": abs(v) - limit, "reason": "absolute vega is above the limit"}
        out["rebalance"] = out["delta_action"] is not None or out["vega_action"] is not None
        return out

    return c.guarded(name, unit, method, pieces, params, compute)


@registry.register("options.gamma_scalp", module="options",
                   summary="Gamma scalping arithmetic with explicit units. Per share: gamma profit of a move is 0.5 * gamma * move^2 against theta per day. In dollars it is multiplied by quantity (contracts) and the contract multiplier (required, no default). Returns the daily P&L, the breakeven daily move sqrt(2 * |theta| / gamma) and how many days of theta one move pays for.",
                   inputs={"gamma": {"type": "float", "required": True}, "theta_per_day": {"type": "float", "required": True},
                           "quantity": {"type": "float", "required": True}, "multiplier": {"type": "float", "required": True},
                           "move": {"type": "float", "required": True}},
                   piece_ids=("ECT-031",))
def gamma_scalp(gamma: float, theta_per_day: float, quantity: float, multiplier: float, move: float) -> QuantResult:
    name, unit, method, pieces = "options.gamma_scalp", "usd", "gamma_theta_breakeven", ("ECT-031",)
    gm, th, qty, mult, mv = (c.num("gamma", gamma), c.num("theta_per_day", theta_per_day), c.num("quantity", quantity),
                             c.num("multiplier", multiplier), c.num("move", move))
    params = {"gamma": gm, "theta_per_day": th, "quantity": qty, "multiplier": mult, "move": mv}
    if not all(math.isfinite(x) for x in (gm, th, qty, mult, mv)) or gm <= 0 or th >= 0 or qty <= 0 or mult <= 0:
        return c.empty(name, unit, method, "gamma, quantity and multiplier must be positive and theta_per_day negative (a long gamma position); all finite",
                       params=params, piece_ids=pieces)

    def compute() -> dict:
        scale = qty * mult
        gain = 0.5 * gm * mv * mv
        return {"gamma_pnl_usd": gain * scale, "theta_usd_per_day": th * scale, "net_pnl_usd_per_day": (gain + th) * scale,
                "breakeven_daily_move": math.sqrt(2.0 * abs(th) / gm), "theta_days_paid_by_move": gain / abs(th)}

    return c.guarded(name, unit, method, pieces, params, compute)

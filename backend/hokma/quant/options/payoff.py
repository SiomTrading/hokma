"""Payoff at expiry of an option strategy: spreads, condors and any mix of option and stock legs (SPEC-019, B1; FT-036).

Dollars use the contract multiplier of the contract sheet that the caller passes in. There is no default of 100.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.options import _lib
from hokma.quant.types import QuantResult

MAX_LEGS = 20
MAX_GRID = 2_000


@dataclass(frozen=True)
class Leg:
    instrument: str          # "option" or "stock"
    sign: int                # +1 long, -1 short
    put: bool
    strike: float | None
    premium: float           # price per share paid (long) or received (short); the entry price for a stock leg
    quantity: float          # contracts for an option, shares for a stock
    multiplier: float        # shares per contract; 1 for a stock leg


def parse_legs(legs: Any, multiplier: Any) -> list[Leg]:
    """Legs from agent input. Wrong types raise BadInput; a value outside its domain raises ValueError (turned into a warning)."""
    if not isinstance(legs, (list, tuple)):
        raise BadInput(f"legs must be a list of dicts, got {type(legs).__name__}")
    if len(legs) > MAX_LEGS:
        raise BadInput(f"legs has more than {MAX_LEGS} entries")
    top = c.num("multiplier", multiplier, allow_none=True)
    out: list[Leg] = []
    for i, raw in enumerate(legs):
        if not isinstance(raw, dict):
            raise BadInput(f"legs[{i}] must be a dict")
        instrument = raw.get("instrument", "option")
        position = raw.get("position", "long")
        if instrument not in ("option", "stock") or position not in ("long", "short"):
            raise ValueError(f"legs[{i}]: instrument must be 'option' or 'stock' and position 'long' or 'short'")
        premium = c.num(f"legs[{i}].premium", raw.get("premium", 0.0))
        qty = c.num(f"legs[{i}].quantity", raw.get("quantity", 1))
        if not (math.isfinite(premium) and math.isfinite(qty)) or premium < 0 or qty <= 0:
            raise ValueError(f"legs[{i}]: premium must not be negative and quantity must be positive")
        sign = 1 if position == "long" else -1
        if instrument == "stock":
            out.append(Leg("stock", sign, False, None, premium, qty, 1.0))
            continue
        strike = c.num(f"legs[{i}].strike", raw.get("strike"))
        kind = raw.get("kind", raw.get("type"))
        put = c.kind_flag(kind) if isinstance(kind, str) else None
        if put is None:
            raise ValueError(f"legs[{i}]: kind must be 'call' or 'put'")
        mult = c.num(f"legs[{i}].multiplier", raw.get("multiplier"), allow_none=True)
        mult = top if mult is None else mult
        if mult is None:
            raise ValueError(f"legs[{i}]: the contract multiplier is required (take it from the contract sheet); there is no default")
        if not math.isfinite(strike) or strike <= 0 or not math.isfinite(mult) or mult <= 0:
            raise ValueError(f"legs[{i}]: strike and multiplier must be positive numbers")
        out.append(Leg("option", sign, put, strike, premium, qty, mult))
    return out


def pnl_at(legs: list[Leg], spots: np.ndarray) -> np.ndarray:
    """Total profit and loss in dollars at expiry for each spot, from the per-share payoff of the library times quantity and multiplier."""
    total = np.zeros_like(spots, dtype=float)
    for leg in legs:
        spec = ({"instrument": "stock", "position": "long" if leg.sign > 0 else "short", "premium": leg.premium} if leg.instrument == "stock" else
                {"instrument": "option", "position": "long" if leg.sign > 0 else "short", "strike_price": leg.strike,
                 "put_option": leg.put, "premium": leg.premium})
        per_share = np.asarray(_lib.ft_tree.get_strategy_payoff(spots, [spec]), dtype=float)
        total += per_share * leg.quantity * leg.multiplier
    return total


def tail_slope(legs: list[Leg]) -> float:
    """Dollars of profit per dollar of spot far above every strike: long calls and stock add, short calls subtract."""
    slope = 0.0
    for leg in legs:
        if leg.instrument == "stock" or not leg.put:
            slope += leg.sign * leg.quantity * leg.multiplier
    return slope


def net_premium_usd(legs: list[Leg]) -> float:
    """Dollars paid (positive, a debit) or received (negative, a credit) on the option legs."""
    return sum(leg.sign * leg.premium * leg.quantity * leg.multiplier for leg in legs if leg.instrument == "option")


def profile(legs: list[Leg]) -> dict[str, Any]:
    """Exact extremes and breakevens of a piecewise linear payoff: evaluated at zero and at every strike."""
    strikes = sorted({leg.strike for leg in legs if leg.strike is not None})
    points = np.array([0.0] + strikes, dtype=float)
    values = pnl_at(legs, points)
    slope = tail_slope(legs)
    best, worst = float(values.max()), float(values.min())
    unbounded_profit, unbounded_loss = slope > 1e-12, slope < -1e-12
    roots: list[float] = []
    for x0, x1, y0, y1 in zip(points[:-1], points[1:], values[:-1], values[1:]):
        if y0 == 0.0:
            roots.append(float(x0))
        elif y0 * y1 < 0.0:
            roots.append(float(x0 + (0.0 - y0) * (x1 - x0) / (y1 - y0)))
    if values[-1] == 0.0:
        roots.append(float(points[-1]))
    elif abs(slope) > 1e-12 and values[-1] * slope < 0.0:
        roots.append(float(points[-1] - values[-1] / slope))
    roots = sorted({round(x, 9) for x in roots if x > 0.0})
    return {"max_profit_usd": None if unbounded_profit else best, "unbounded_profit": unbounded_profit,
            "max_loss_usd": None if unbounded_loss else max(-worst, 0.0), "unbounded_loss": unbounded_loss,
            "breakevens": roots}


@registry.register("options.payoff", module="options",
                   summary="Payoff at expiry of an option strategy in dollars: legs [{instrument option or stock, position long or short, kind call or put, strike, premium per share, quantity in contracts (shares for stock), multiplier}] with the contract multiplier of the contract sheet (required, no default). Returns the P&L on a spot grid, net premium (positive = debit), maximum profit and loss (None when unbounded) and breakevens.",
                   inputs={"legs": {"type": "list", "required": True}, "multiplier": {"type": "float", "required": False, "default": None},
                           "spots": {"type": "list", "required": False, "default": None}, "spot": {"type": "float", "required": False, "default": None},
                           "grid_points": {"type": "int", "required": False, "default": 101},
                           "low_pct": {"type": "float", "required": False, "default": 0.7}, "high_pct": {"type": "float", "required": False, "default": 1.3}},
                   piece_ids=("FT-036",))
def payoff(legs: list, multiplier: float | None = None, spots: list | None = None, spot: float | None = None,
           grid_points: int = 101, low_pct: float = 0.7, high_pct: float = 1.3) -> QuantResult:
    name, unit, method, pieces = "options.payoff", "usd", "piecewise_linear_expiry_payoff", ("FT-036",)
    ref = c.num("spot", spot, allow_none=True)
    lo, hi = c.num("low_pct", low_pct), c.num("high_pct", high_pct)
    points = c.positive_int("grid_points", grid_points, low=2)
    if points > MAX_GRID:
        raise BadInput(f"grid_points={points} is above the limit {MAX_GRID}")
    params = {"legs": legs if isinstance(legs, list) else [], "multiplier": multiplier, "spot": ref, "grid_points": points, "low_pct": lo, "high_pct": hi}
    if isinstance(legs, (list, tuple)) and not legs:
        return c.empty(name, unit, method, "legs must contain at least one leg", params=params, piece_ids=pieces)
    try:
        parsed = parse_legs(legs, multiplier)
    except BadInput:
        raise
    except ValueError as exc:
        return c.empty(name, unit, method, str(exc), params=params, piece_ids=pieces)
    if spots is not None:
        if not isinstance(spots, (list, tuple)):
            raise BadInput(f"spots must be a list of prices, got {type(spots).__name__}")
        if len(spots) > MAX_GRID:
            raise BadInput(f"spots has more than {MAX_GRID} entries")
        grid = np.array([c.num("spots[]", x) for x in spots], dtype=float)
        if len(grid) == 0 or not np.isfinite(grid).all() or (grid < 0).any():
            return c.empty(name, unit, method, "spots must be non-empty, finite and not negative", params=params, piece_ids=pieces)
    else:
        strikes = [leg.strike for leg in parsed if leg.strike is not None]
        anchors = strikes + ([ref] if ref is not None and math.isfinite(ref) and ref > 0 else [])
        if not anchors or not (math.isfinite(lo) and math.isfinite(hi)) or not 0.0 <= lo < hi:
            return c.empty(name, unit, method, "give spots, or a spot or option strikes with 0 <= low_pct < high_pct", params=params, piece_ids=pieces)
        grid = np.linspace(min(anchors) * lo, max(anchors) * hi, points)

    def compute() -> dict:
        info = profile(parsed)
        pnl = pnl_at(parsed, grid)
        net = net_premium_usd(parsed)
        info.update({"spots": grid.tolist(), "pnl_usd": pnl.tolist(), "net_premium_usd": net,
                     "premium_type": "debit" if net > 1e-12 else ("credit" if net < -1e-12 else "even")})
        return info

    return c.guarded(name, unit, method, pieces, params, compute)

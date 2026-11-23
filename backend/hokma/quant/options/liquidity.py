"""Liquidity of option contracts: quoted, effective and stressed spread, days to liquidate and open interest share
(SPEC-019, B1; ECT-033). Built for options: the source material did not use open interest or option volume.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.types import QuantResult

DEFAULT_STRESS = (3.0, 4.0, 5.0)
MAX_STRESS_POINTS = 10
MIN_STRESS, MAX_STRESS = 1.0, 10.0
MAX_WORST_ROWS = 20


def _stress_list(values: Any) -> list[float]:
    if values is None:
        return list(DEFAULT_STRESS)
    if not isinstance(values, (list, tuple)) or not values or len(values) > MAX_STRESS_POINTS:
        raise BadInput(f"stress_multiples must be a list of 1 to {MAX_STRESS_POINTS} numbers")
    return [c.num("stress_multiples[]", x) for x in values]


@registry.register("options.liquidity", module="options",
                   summary="Liquidity of one option contract: quoted spread (absolute and relative to mid), effective spread when a trade price is given, the spread stressed 3 to 5 times (default 3, 4, 5) and what exiting costs in dollars at each, days to liquidate at a participation rate of the day's volume (default 20 percent), the position as a share of open interest and the flags: above one day of volume, above the open interest share cap. The contract multiplier is required when a quantity is given.",
                   inputs={"bid": {"type": "float", "required": True}, "ask": {"type": "float", "required": True},
                           "quantity": {"type": "float", "required": False, "default": None}, "multiplier": {"type": "float", "required": False, "default": None},
                           "open_interest": {"type": "float", "required": False, "default": None}, "volume": {"type": "float", "required": False, "default": None},
                           "trade_price": {"type": "float", "required": False, "default": None},
                           "bid_size": {"type": "float", "required": False, "default": None},
                           "participation": {"type": "float", "required": False, "default": 0.20},
                           "oi_share_cap": {"type": "float", "required": False, "default": 0.10},
                           "stress_multiples": {"type": "list", "required": False, "default": None}},
                   piece_ids=("ECT-033",))
def liquidity(bid: float, ask: float, quantity: float | None = None, multiplier: float | None = None, open_interest: float | None = None,
              volume: float | None = None, trade_price: float | None = None, bid_size: float | None = None, participation: float = 0.20,
              oi_share_cap: float = 0.10, stress_multiples: list | None = None) -> QuantResult:
    name, unit, method, pieces = "options.liquidity", "usd", "option_liquidity", ("ECT-033",)
    b, a = c.num("bid", bid), c.num("ask", ask)
    qty, mult = c.num("quantity", quantity, allow_none=True), c.num("multiplier", multiplier, allow_none=True)
    oi, vol = c.num("open_interest", open_interest, allow_none=True), c.num("volume", volume, allow_none=True)
    trade, depth = c.num("trade_price", trade_price, allow_none=True), c.num("bid_size", bid_size, allow_none=True)
    part, cap = c.num("participation", participation), c.num("oi_share_cap", oi_share_cap)
    stress = _stress_list(stress_multiples)
    params = {"bid": b, "ask": a, "quantity": qty, "multiplier": mult, "open_interest": oi, "volume": vol, "trade_price": trade,
              "bid_size": depth, "participation": part, "oi_share_cap": cap, "stress_multiples": stress}
    numbers = [b, a, part, cap] + stress + [x for x in (qty, mult, oi, vol, trade, depth) if x is not None]
    if not all(math.isfinite(x) for x in numbers):
        return c.empty(name, unit, method, "inputs must be finite numbers (NaN or infinity found)", params=params, piece_ids=pieces)
    if b < 0 or a <= 0 or a < b:
        return c.empty(name, unit, method, "need 0 <= bid <= ask and ask > 0", params=params, piece_ids=pieces)
    if not 0.0 < part <= 1.0 or not 0.0 < cap <= 1.0 or any(not MIN_STRESS <= x <= MAX_STRESS for x in stress):
        return c.empty(name, unit, method, f"participation and oi_share_cap must be in (0, 1]; stress multiples in [{MIN_STRESS:g}, {MAX_STRESS:g}]",
                       params=params, piece_ids=pieces)
    if (qty is not None and qty <= 0) or any(x is not None and x < 0 for x in (oi, vol, depth)):
        return c.empty(name, unit, method, "quantity must be positive; open interest, volume and sizes must not be negative", params=params, piece_ids=pieces)
    if qty is not None and (mult is None or mult <= 0):
        return c.empty(name, unit, method, "a quantity needs the contract multiplier of the contract sheet (required, no default)", params=params, piece_ids=pieces)

    warns: list[str] = []

    def compute() -> dict:
        mid = (a + b) / 2.0
        quoted = a - b
        out: dict[str, Any] = {"mid": mid, "quoted_spread": quoted, "relative_spread": quoted / mid if mid > 0 else None,
                               "effective_spread": None if trade is None else 2.0 * abs(trade - mid),
                               "relative_effective_spread": None if trade is None or mid <= 0 else 2.0 * abs(trade - mid) / mid}
        out["stressed"] = [{"multiple": k, "spread": quoted * k, "relative_spread": quoted * k / mid if mid > 0 else None,
                            "exit_cost_usd": None if qty is None else qty * mult * quoted * k / 2.0} for k in stress]       # type: ignore[operator]
        if qty is not None:
            out["position_value_usd"] = qty * mult * mid                                    # type: ignore[operator]
            out["above_one_day_of_volume"] = None
            out["days_to_liquidate"] = None
            out["oi_share"] = None
            out["above_oi_share_cap"] = None
            if vol is not None and vol > 0:
                days = qty / (part * vol)
                out["days_to_liquidate"] = days
                out["above_one_day_of_volume"] = days > 1.0
            elif vol is not None:
                warns.append("no volume today: days to liquidate is unknown, read the open interest share")
            if oi is not None and oi > 0:
                out["oi_share"] = qty / oi
                out["above_oi_share_cap"] = qty / oi > cap
            elif oi is not None:
                warns.append("open interest is zero: the position cannot be sized against it")
            if depth is not None:
                out["bid_depth_cover"] = depth / qty
        return out

    return c.guarded(name, unit, method, pieces, params, compute, warns)


@registry.register("options.chain_liquidity", module="options",
                   summary="Liquidity scan of an option chain (the contracts of the chain tool): median and 90th percentile of the relative quoted spread, how many contracts have no valid quote, and, when max_relative_spread or min_open_interest are given, the contracts that fail them (at most 20, worst spread first).",
                   inputs={"contracts": {"type": "list", "required": True}, "max_relative_spread": {"type": "float", "required": False, "default": None},
                           "min_open_interest": {"type": "float", "required": False, "default": None}},
                   piece_ids=("ECT-033",))
def chain_liquidity(contracts: list, max_relative_spread: float | None = None, min_open_interest: float | None = None) -> QuantResult:
    name, unit, method, pieces = "options.chain_liquidity", "ratio", "chain_spread_scan", ("ECT-033",)
    rows = c.contracts_list(contracts)
    max_spread = c.num("max_relative_spread", max_relative_spread, allow_none=True)
    min_oi = c.num("min_open_interest", min_open_interest, allow_none=True)
    params = {"n_contracts": len(rows), "max_relative_spread": max_spread, "min_open_interest": min_oi}
    if any(x is not None and (not math.isfinite(x) or x < 0) for x in (max_spread, min_oi)):
        return c.empty(name, unit, method, "thresholds must be finite and not negative", params=params, piece_ids=pieces)
    scored: list[dict[str, Any]] = []
    invalid = 0
    for row in rows:
        bid, ask = c.row_float(row, "bid"), c.row_float(row, "ask")
        if bid is None or ask is None or bid < 0 or ask <= 0 or ask < bid:
            invalid += 1
            continue
        mid = (bid + ask) / 2.0
        scored.append({"symbol": row.get("symbol"), "strike": row.get("strike"), "expiration": row.get("expiration"), "relative_spread": (ask - bid) / mid,
                       "open_interest": c.row_float(row, "open_interest")})
    if not scored:
        return c.empty(name, unit, method, f"no contract has a valid bid and ask ({invalid} of {len(rows)} rows invalid)", params=params, piece_ids=pieces)

    def compute() -> dict:
        spreads = sorted(x["relative_spread"] for x in scored)
        p90 = spreads[min(len(spreads) - 1, math.ceil(0.9 * len(spreads)) - 1)]
        failing = [x for x in scored if (max_spread is not None and x["relative_spread"] > max_spread)
                   or (min_oi is not None and (x["open_interest"] is None or x["open_interest"] < min_oi))]
        failing.sort(key=lambda x: -x["relative_spread"])
        return {"n_valid": len(scored), "n_invalid": invalid, "median_relative_spread": statistics.median(spreads), "p90_relative_spread": p90,
                "n_failing": len(failing), "failing": failing[:MAX_WORST_ROWS]}

    return c.guarded(name, unit, method, pieces, params, compute)

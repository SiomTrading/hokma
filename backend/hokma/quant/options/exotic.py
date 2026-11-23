"""Exotic options in closed form: binary, barrier and Asian (SPEC-019, B1).

Registered for the module and nothing more: no SWO agent asks for them and no structure of the desk uses them.
Written from the closed forms (Reiner-Rubinstein barriers, geometric average price).
"""
from __future__ import annotations

import math

from hokma.quant import registry
from hokma.quant.options import _common as c
from hokma.quant.types import QuantResult

_BASE = ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield", "kind")


@registry.register("options.binary", module="options",
                   summary="Price of a binary (digital) option per share: cash_or_nothing pays cash_payout, asset_or_nothing pays the asset. No desk line trades binaries; it is registered for the module only.",
                   inputs=c.pick(*_BASE, binary_type={"type": "str", "required": False, "default": "cash_or_nothing"},
                                 cash_payout={"type": "float", "required": False, "default": 1.0}),
                   piece_ids=("FT-024",))
def binary(spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float, dividend_yield: float = 0.0,
           kind: str = "call", binary_type: str = "cash_or_nothing", cash_payout: float = 1.0) -> QuantResult:
    name, unit, method, pieces = "options.binary", "price", "black_scholes_binary", ("FT-024",)
    s, k, r, v, t, q, payout = (c.num("spot", spot), c.num("strike", strike), c.num("rate", rate), c.num("volatility", volatility),
                                c.num("time_to_expiration", time_to_expiration), c.num("dividend_yield", dividend_yield),
                                c.num("cash_payout", cash_payout))
    put = c.kind_flag(kind)
    params = {"spot": s, "strike": k, "rate": r, "volatility": v, "time_to_expiration": t, "dividend_yield": q, "kind": kind,
              "binary_type": binary_type, "cash_payout": payout}
    if put is None:
        return c.empty(name, unit, method, "kind must be 'call' or 'put'", params=params, piece_ids=pieces)
    if binary_type not in ("cash_or_nothing", "asset_or_nothing"):
        return c.empty(name, unit, method, "binary_type must be 'cash_or_nothing' or 'asset_or_nothing'", params=params, piece_ids=pieces)
    problem = c.bs_domain(s, k, r, v, t, q)
    if problem or not math.isfinite(payout) or payout < 0:
        return c.empty(name, unit, method, problem or "cash_payout must be a finite number that is not negative", params=params, piece_ids=pieces)

    def compute() -> float:
        d1, d2 = c.d1_d2(s, k, r, v, t, q)
        sign = -1.0 if put else 1.0
        if binary_type == "cash_or_nothing":
            return payout * math.exp(-r * t) * c.norm_cdf(sign * d2)
        return s * math.exp(-q * t) * c.norm_cdf(sign * d1)

    return c.guarded(name, unit, method, pieces, params, compute)


def _barrier_terms(phi: int, eta: int, s: float, k: float, h: float, r: float, b: float, v: float, t: float) -> tuple[float, float, float, float]:
    root = v * math.sqrt(t)
    mu = (b - v * v / 2.0) / (v * v)
    carry = math.exp((b - r) * t)
    disc = math.exp(-r * t)
    x1 = math.log(s / k) / root + (1.0 + mu) * root
    x2 = math.log(s / h) / root + (1.0 + mu) * root
    y1 = math.log(h * h / (s * k)) / root + (1.0 + mu) * root
    y2 = math.log(h / s) / root + (1.0 + mu) * root
    n = c.norm_cdf
    a_ = phi * s * carry * n(phi * x1) - phi * k * disc * n(phi * x1 - phi * root)
    b_ = phi * s * carry * n(phi * x2) - phi * k * disc * n(phi * x2 - phi * root)
    c_ = phi * s * carry * (h / s) ** (2 * (mu + 1)) * n(eta * y1) - phi * k * disc * (h / s) ** (2 * mu) * n(eta * y1 - eta * root)
    d_ = phi * s * carry * (h / s) ** (2 * (mu + 1)) * n(eta * y2) - phi * k * disc * (h / s) ** (2 * mu) * n(eta * y2 - eta * root)
    return a_, b_, c_, d_


def _barrier_price(s: float, k: float, h: float, r: float, v: float, t: float, q: float, put: bool, direction: str, knock: str, rebate: float) -> float:
    b = r - q
    root = v * math.sqrt(t)
    mu = (b - v * v / 2.0) / (v * v)
    lam = math.sqrt(mu * mu + 2.0 * r / (v * v))
    phi = -1 if put else 1
    eta = 1 if direction == "down" else -1
    a_, b_, c_, d_ = _barrier_terms(phi, eta, s, k, h, r, b, v, t)
    n = c.norm_cdf
    x2 = math.log(s / h) / root + (1.0 + mu) * root
    y2 = math.log(h / s) / root + (1.0 + mu) * root
    z = math.log(h / s) / root + lam * root
    e_ = rebate * math.exp(-r * t) * (n(eta * x2 - eta * root) - (h / s) ** (2 * mu) * n(eta * y2 - eta * root))
    f_ = rebate * ((h / s) ** (mu + lam) * n(eta * z) + (h / s) ** (mu - lam) * n(eta * z - 2 * eta * lam * root))
    above = k > h                                                       # strike on the far side of the barrier from spot
    if not put:
        table = {("down", "in"): (c_ + e_) if above else (a_ - b_ + d_ + e_),
                 ("up", "in"): (a_ + e_) if above else (b_ - c_ + d_ + e_),
                 ("down", "out"): (a_ - c_ + f_) if above else (b_ - d_ + f_),
                 ("up", "out"): f_ if above else (a_ - b_ + c_ - d_ + f_)}
    else:
        table = {("down", "in"): (b_ - c_ + d_ + e_) if above else (a_ + e_),
                 ("up", "in"): (a_ - b_ + d_ + e_) if above else (c_ + e_),
                 ("down", "out"): (a_ - b_ + c_ - d_ + f_) if above else f_,
                 ("up", "out"): (b_ - d_ + f_) if above else (a_ - c_ + f_)}
    value = table[(direction, knock)]
    breached = s <= h if direction == "down" else s >= h
    if breached:
        vanilla = c.bs_price(s, k, r, v, t, q, put)
        return rebate if knock == "out" else vanilla
    return value


def _asian_price(s: float, k: float, r: float, v: float, t: float, q: float, put: bool) -> float:
    """Continuous geometric average price option (Kemna-Vorst): an exact formula with volatility v/sqrt(3)."""
    b = r - q
    va = v / math.sqrt(3.0)
    ba = 0.5 * (b - v * v / 6.0)
    root = va * math.sqrt(t)
    d1 = (math.log(s / k) + (ba + va * va / 2.0) * t) / root
    d2 = d1 - root
    if put:
        return k * math.exp(-r * t) * c.norm_cdf(-d2) - s * math.exp((ba - r) * t) * c.norm_cdf(-d1)
    return s * math.exp((ba - r) * t) * c.norm_cdf(d1) - k * math.exp(-r * t) * c.norm_cdf(d2)


@registry.register("options.barrier_asian", module="options",
                   summary="Price per share of a standard barrier option (exotic='barrier': direction down or up, knock in or out, optional rebate) or of a geometric average price Asian option (exotic='asian'). SWO and LP trade standard options; registered for the module only.",
                   inputs=c.pick(*_BASE, exotic={"type": "str", "required": True}, barrier={"type": "float", "required": False, "default": None},
                                 barrier_direction={"type": "str", "required": False, "default": "down"},
                                 knock_type={"type": "str", "required": False, "default": "out"},
                                 rebate={"type": "float", "required": False, "default": 0.0}),
                   piece_ids=("FT-037",))
def barrier_asian(exotic: str, spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float,
                  dividend_yield: float = 0.0, kind: str = "call", barrier: float | None = None, barrier_direction: str = "down",
                  knock_type: str = "out", rebate: float = 0.0) -> QuantResult:
    name, unit, method, pieces = "options.barrier_asian", "price", "closed_form_exotic", ("FT-037",)
    s, k, r, v, t, q, rb = (c.num("spot", spot), c.num("strike", strike), c.num("rate", rate), c.num("volatility", volatility),
                            c.num("time_to_expiration", time_to_expiration), c.num("dividend_yield", dividend_yield), c.num("rebate", rebate))
    h = c.num("barrier", barrier, allow_none=True)
    put = c.kind_flag(kind)
    params = {"exotic": exotic, "spot": s, "strike": k, "rate": r, "volatility": v, "time_to_expiration": t, "dividend_yield": q, "kind": kind,
              "barrier": h, "barrier_direction": barrier_direction, "knock_type": knock_type, "rebate": rb}
    if put is None:
        return c.empty(name, unit, method, "kind must be 'call' or 'put'", params=params, piece_ids=pieces)
    if exotic not in ("barrier", "asian"):
        return c.empty(name, unit, method, "exotic must be 'barrier' or 'asian'", params=params, piece_ids=pieces)
    problem = c.bs_domain(s, k, r, v, t, q)
    if problem:
        return c.empty(name, unit, method, problem, params=params, piece_ids=pieces)
    if exotic == "asian":
        return c.guarded(name, unit, "geometric_average_price", pieces, params, lambda: _asian_price(s, k, r, v, t, q, put))
    if h is None or not math.isfinite(h) or h <= 0:
        return c.empty(name, unit, method, "a barrier option needs a positive barrier", params=params, piece_ids=pieces)
    if barrier_direction not in ("down", "up") or knock_type not in ("in", "out"):
        return c.empty(name, unit, method, "barrier_direction must be 'down' or 'up' and knock_type 'in' or 'out'", params=params, piece_ids=pieces)
    if not math.isfinite(rb) or rb < 0:
        return c.empty(name, unit, method, "rebate must be a finite number that is not negative", params=params, piece_ids=pieces)
    return c.guarded(name, unit, "reiner_rubinstein_barrier", pieces, params,
                     lambda: _barrier_price(s, k, h, r, v, t, q, put, barrier_direction, knock_type, rb))

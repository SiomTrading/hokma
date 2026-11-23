"""Implied volatility by inversion of the option price (SPEC-019, B1)."""
from __future__ import annotations

import math

from hokma.quant import registry
from hokma.quant.options import _common as c
from hokma.quant.options import _lib
from hokma.quant.types import QuantResult

MIN_VOL = 1e-6
MAX_VOL = 5.0


def _bounds(s: float, k: float, r: float, t: float, q: float, put: bool, american: bool) -> tuple[float, float]:
    """No-arbitrage bounds of the option price: strictly between them an implied volatility can exist."""
    ds, dk = s * math.exp(-q * t), k * math.exp(-r * t)
    if american:
        return (max(k - s, 0.0), k) if put else (max(s - k, 0.0), s)
    return (max(dk - ds, 0.0), dk) if put else (max(ds - dk, 0.0), ds)


@registry.register("options.implied_volatility", module="options",
                   summary="Implied volatility (decimal, 0.25 = 25 percent) that reproduces a market option price. model='black_scholes' inverts the European formula; model='american' inverts the Bjerksund-Stensland approximation, which is the right one for US equity puts and dividend payers. A price outside the no-arbitrage bounds gives None with a warning.",
                   inputs=c.pick("spot", "strike", "rate", "time_to_expiration", "dividend_yield", "kind",
                                 market_price={"type": "float", "required": True},
                                 model={"type": "str", "required": False, "default": "black_scholes"}),
                   piece_ids=("FT-021",))
def implied_volatility(market_price: float, spot: float, strike: float, rate: float, time_to_expiration: float,
                       dividend_yield: float = 0.0, kind: str = "call", model: str = "black_scholes") -> QuantResult:
    name, unit, method, pieces = "options.implied_volatility", "vol", "bisection_brent", ("FT-021",)
    p, s, k, r, t, q = (c.num("market_price", market_price), c.num("spot", spot), c.num("strike", strike), c.num("rate", rate),
                        c.num("time_to_expiration", time_to_expiration), c.num("dividend_yield", dividend_yield))
    put = c.kind_flag(kind)
    if not isinstance(model, str):
        raise c.BadInput("model must be 'black_scholes' or 'american'")
    params = {"market_price": p, "spot": s, "strike": k, "rate": r, "time_to_expiration": t, "dividend_yield": q, "kind": kind, "model": model}
    if put is None:
        return c.empty(name, unit, method, "kind must be 'call' or 'put'", params=params, piece_ids=pieces)
    if model not in ("black_scholes", "american"):
        return c.empty(name, unit, method, "model must be 'black_scholes' or 'american'", params=params, piece_ids=pieces)
    problem = c.bs_domain(s, k, r, 0.2, t, q)           # volatility is the unknown: a placeholder inside the domain
    if problem:
        return c.empty(name, unit, method, problem, params=params, piece_ids=pieces)
    if not math.isfinite(p) or p <= 0:
        return c.empty(name, unit, method, "market_price must be a positive finite number", params=params, piece_ids=pieces)
    american = model == "american"
    low, high = _bounds(s, k, r, t, q, put, american)
    if not low < p < high:
        return c.empty(name, unit, method,
                       f"price {p:g} is outside the no-arbitrage bounds ({low:g}, {high:g}): no implied volatility exists", params=params, piece_ids=pieces)

    def compute() -> float:
        if not american:
            value = float(_lib.ft_bs.get_implied_volatility(p, s, k, r, t, q, put))
            if math.isnan(value):
                raise ValueError("the price cannot be matched by a volatility between 0.0001 percent and 500 percent")
            return value

        def gap(sigma: float) -> float:
            return float(_lib.ft_bs.get_bjerksund_stensland(s, k, r, sigma, t, q, put)) - p

        if gap(MIN_VOL) > 0 or gap(MAX_VOL) < 0:
            raise ValueError("the price cannot be matched by a volatility between 0.0001 percent and 500 percent")
        return float(_lib.brentq(gap, MIN_VOL, MAX_VOL, xtol=1e-14, rtol=1e-13))

    return c.guarded(name, unit, method, pieces, params, compute)

"""Option prices: Black-Scholes, parity, Garman-Kohlhagen, Bjerksund-Stensland and Monte Carlo (SPEC-019, B1).

Rates and volatilities are decimals (0.04 and 0.25). Time is in years. A price is per share, not per contract.
"""
from __future__ import annotations

import math
from typing import Any

from hokma.quant import limits, registry
from hokma.quant.options import _common as c
from hokma.quant.options import _lib
from hokma.quant.types import QuantResult

_BSM = ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield", "kind")


def _read(*values: tuple[str, Any]) -> list[float]:
    return [c.num(label, v) for label, v in values]  # type: ignore[misc]


@registry.register("options.d1_d2", module="options",
                   summary="Black-Scholes d1 and d2 for spot, strike, rate, volatility, time in years and dividend yield. The base of every price and greek.",
                   inputs=c.pick("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield"), piece_ids=("FT-019",))
def d1_d2(spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float, dividend_yield: float = 0.0) -> QuantResult:
    name, unit, method, pieces = "options.d1_d2", "ratio", "black_scholes_d1_d2", ("FT-019",)
    s, k, r, v, t, q = _read(("spot", spot), ("strike", strike), ("rate", rate), ("volatility", volatility),
                             ("time_to_expiration", time_to_expiration), ("dividend_yield", dividend_yield))
    params = {"spot": s, "strike": k, "rate": r, "volatility": v, "time_to_expiration": t, "dividend_yield": q}
    problem = c.bs_domain(s, k, r, v, t, q)
    if problem:
        return c.empty(name, unit, method, problem, params=params, piece_ids=pieces)

    def compute() -> dict:
        d1 = float(_lib.ft_bs.get_d1(s, k, r, v, t, q))
        return {"d1": d1, "d2": float(_lib.ft_bs.get_d2(d1, v, t))}

    return c.guarded(name, unit, method, pieces, params, compute)


@registry.register("options.black_scholes", module="options",
                   summary="Black-Scholes price per share of a European call or put with continuous dividend yield. At expiry (time 0) it returns the intrinsic value.",
                   inputs=c.pick(*_BSM), piece_ids=("FT-020",))
def black_scholes(spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float,
                  dividend_yield: float = 0.0, kind: str = "call") -> QuantResult:
    name, unit, method, pieces = "options.black_scholes", "price", "black_scholes", ("FT-020",)
    s, k, r, v, t, q = _read(("spot", spot), ("strike", strike), ("rate", rate), ("volatility", volatility),
                             ("time_to_expiration", time_to_expiration), ("dividend_yield", dividend_yield))
    put = c.kind_flag(kind)
    params = {"spot": s, "strike": k, "rate": r, "volatility": v, "time_to_expiration": t, "dividend_yield": q, "kind": kind}
    if put is None:
        return c.empty(name, unit, method, "kind must be 'call' or 'put'", params=params, piece_ids=pieces)
    problem = c.bs_domain(s, k, r, v, t, q, allow_zero_time=True)
    if problem:
        return c.empty(name, unit, method, problem, params=params, piece_ids=pieces)
    return c.guarded(name, unit, method, pieces, params, lambda: float(_lib.ft_bs.get_black_scholes(s, k, r, v, t, q, put)))


@registry.register("options.put_call_parity", module="options",
                   summary="Put-call parity check: call minus put against spot*exp(-q*T) minus strike*exp(-r*T). Exact for European options; for American options (all US equity options) it also checks the no-arbitrage bounds. Price gap in price units.",
                   inputs=c.pick("spot", "strike", "rate", "time_to_expiration", "dividend_yield",
                                 call_price={"type": "float", "required": True}, put_price={"type": "float", "required": True},
                                 tolerance={"type": "float", "required": False, "default": 0.05}),
                   piece_ids=("FT-022",))
def put_call_parity(spot: float, strike: float, rate: float, time_to_expiration: float, call_price: float, put_price: float,
                    dividend_yield: float = 0.0, tolerance: float = 0.05) -> QuantResult:
    name, unit, method, pieces = "options.put_call_parity", "price", "put_call_parity", ("FT-022",)
    s, k, r, t, cp, pp, q, tol = _read(("spot", spot), ("strike", strike), ("rate", rate), ("time_to_expiration", time_to_expiration),
                                       ("call_price", call_price), ("put_price", put_price), ("dividend_yield", dividend_yield),
                                       ("tolerance", tolerance))
    params = {"spot": s, "strike": k, "rate": r, "time_to_expiration": t, "dividend_yield": q, "call_price": cp, "put_price": pp, "tolerance": tol}
    if not all(math.isfinite(x) for x in (s, k, r, t, cp, pp, q, tol)):
        return c.empty(name, unit, method, "inputs must be finite numbers (NaN or infinity found)", params=params, piece_ids=pieces)
    if s <= 0 or k <= 0 or t < 0 or cp < 0 or pp < 0 or tol < 0:
        return c.empty(name, unit, method, "spot and strike must be positive; time, prices and tolerance must not be negative", params=params, piece_ids=pieces)

    def compute() -> dict:
        forward_diff = float(_lib.ft_bs.get_put_call_parity(s, k, r, t, q))
        gap = float(_lib.ft_bs.get_put_call_parity(s, k, r, t, q, call_price=cp, put_price=pp))
        lower = s * math.exp(-q * t) - k                      # C - P >= S e^{-qT} - K for American options
        upper = s - k * math.exp(-r * t)                      # C - P <= S - K e^{-rT}
        diff = cp - pp
        return {"forward_differential": forward_diff, "gap": gap, "within_tolerance": abs(gap) <= tol,
                "american_lower_bound": lower, "american_upper_bound": upper,
                "within_american_bounds": bool(lower - tol <= diff <= upper + tol)}

    warns = ("exact parity holds only for European options; US equity options are American, read within_american_bounds",)
    return c.guarded(name, unit, method, pieces, params, compute, warns)


@registry.register("options.garman_kohlhagen", module="options",
                   summary="Garman-Kohlhagen price per share (currency options). With the foreign rate equal to the risk-free rate it imitates Black-76 on a forward equal to spot; it is an approximation, the module has no explicit Black-76.",
                   inputs=c.pick("spot", "strike", "rate", "volatility", "time_to_expiration", "kind",
                                 foreign_rate={"type": "float", "required": True}),
                   piece_ids=("FT-023",))
def garman_kohlhagen(spot: float, strike: float, rate: float, foreign_rate: float, volatility: float, time_to_expiration: float,
                     kind: str = "call") -> QuantResult:
    name, unit, method, pieces = "options.garman_kohlhagen", "price", "garman_kohlhagen", ("FT-023",)
    s, k, r, fr, v, t = _read(("spot", spot), ("strike", strike), ("rate", rate), ("foreign_rate", foreign_rate),
                              ("volatility", volatility), ("time_to_expiration", time_to_expiration))
    put = c.kind_flag(kind)
    params = {"spot": s, "strike": k, "rate": r, "foreign_rate": fr, "volatility": v, "time_to_expiration": t, "kind": kind}
    if put is None:
        return c.empty(name, unit, method, "kind must be 'call' or 'put'", params=params, piece_ids=pieces)
    problem = c.bs_domain(s, k, r, v, t, fr)
    if problem:
        return c.empty(name, unit, method, problem, params=params, piece_ids=pieces)
    warns = ["approximation: Garman-Kohlhagen is Black-Scholes with the foreign rate as the dividend yield"]
    if math.isclose(fr, r, abs_tol=1e-12):
        warns.append("foreign rate equals the risk-free rate: this imitates Black-76 on a forward equal to spot, it is not an explicit Black-76")
    return c.guarded(name, unit, method, pieces, params,
                     lambda: float(_lib.ft_bs.get_garman_kohlhagen(s, k, r, fr, v, t, put)), warns)


@registry.register("options.bjerksund_stensland", module="options",
                   summary="Bjerksund-Stensland 2002 approximation of the American option price per share, with the European Black-Scholes value and the early exercise premium. US equity options are American. It is a lower bound of the exact American price and may understate it by about 1 percent; use binomial_tree when the early exercise premium decides the trade.",
                   inputs=c.pick(*_BSM), piece_ids=("FT-025",))
def bjerksund_stensland(spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float,
                        dividend_yield: float = 0.0, kind: str = "call") -> QuantResult:
    name, unit, method, pieces = "options.bjerksund_stensland", "price", "bjerksund_stensland_2002", ("FT-025",)
    s, k, r, v, t, q = _read(("spot", spot), ("strike", strike), ("rate", rate), ("volatility", volatility),
                             ("time_to_expiration", time_to_expiration), ("dividend_yield", dividend_yield))
    put = c.kind_flag(kind)
    params = {"spot": s, "strike": k, "rate": r, "volatility": v, "time_to_expiration": t, "dividend_yield": q, "kind": kind}
    if put is None:
        return c.empty(name, unit, method, "kind must be 'call' or 'put'", params=params, piece_ids=pieces)
    problem = c.bs_domain(s, k, r, v, t, q)
    if problem:
        return c.empty(name, unit, method, problem, params=params, piece_ids=pieces)

    def compute() -> dict:
        american = float(_lib.ft_bs.get_bjerksund_stensland(s, k, r, v, t, q, put))
        european = float(_lib.ft_bs.get_black_scholes(s, k, r, v, t, q, put))
        return {"american": american, "european": european, "early_exercise_premium": american - european}

    return c.guarded(name, unit, method, pieces, params, compute,
                     ("approximation: a lower bound of the exact American price, it may understate it by about 1 percent",))


@registry.register("options.monte_carlo", module="options",
                   summary="Monte Carlo price per share of a European call or put under geometric Brownian motion, with its standard error. Seeded: the same seed gives the same answer. n_simulations is capped at 100,000 and time_steps at 1,000.",
                   inputs=c.pick(*_BSM, n_simulations={"type": "int", "required": False, "default": 10_000},
                                 time_steps={"type": "int", "required": False, "default": 50},
                                 seed={"type": "int", "required": False, "default": 0}),
                   piece_ids=("FT-034",))
def monte_carlo(spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float, dividend_yield: float = 0.0,
                kind: str = "call", n_simulations: int = 10_000, time_steps: int = 50, seed: int = 0) -> QuantResult:
    name, unit, method, pieces = "options.monte_carlo", "price", "monte_carlo_gbm", ("FT-034",)
    s, k, r, v, t, q = _read(("spot", spot), ("strike", strike), ("rate", rate), ("volatility", volatility),
                             ("time_to_expiration", time_to_expiration), ("dividend_yield", dividend_yield))
    sims = c.positive_int("n_simulations", n_simulations)
    steps = c.positive_int("time_steps", time_steps)
    seed_i = limits.as_int("seed", seed)
    put = c.kind_flag(kind)
    params = {"spot": s, "strike": k, "rate": r, "volatility": v, "time_to_expiration": t, "dividend_yield": q, "kind": kind,
              "n_simulations": sims, "time_steps": steps, "seed": seed_i}
    if put is None:
        return c.empty(name, unit, method, "kind must be 'call' or 'put'", params=params, piece_ids=pieces)
    problem = c.bs_domain(s, k, r, v, t, q)
    if problem:
        return c.empty(name, unit, method, problem, params=params, piece_ids=pieces)
    if seed_i < 0:
        return c.empty(name, unit, method, "seed must not be negative", params=params, piece_ids=pieces)

    def compute() -> dict:
        price, error = _lib.ft_options.get_monte_carlo_option_price(s, k, r, v, t, q, put, sims, steps, seed_i)
        return {"price": float(price), "standard_error": float(error)}

    return c.guarded(name, unit, method, pieces, params, compute)

"""Greeks of a Black-Scholes option (SPEC-019, B1). One implementation per greek; gamma and vanna carry both ids.

Units: delta per 1 dollar of spot; gamma per 1 dollar of spot per dollar; vega per 1 volatility point (0.01); theta per
calendar day; rho per 1.00 of rate; epsilon per 1.00 of dividend yield; vanna per 1.00 of volatility; charm per year;
vomma per 1.00 of volatility; vera per 1.00 of rate; veta per volatility point per day. Per share, not per contract: the
contract multiplier enters in position_greeks.
"""
from __future__ import annotations

import math

from hokma.quant import registry
from hokma.quant.options import _common as c
from hokma.quant.options import _lib
from hokma.quant.types import QuantResult

_GREEK_INPUTS = ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield")


def _setup(name: str, unit: str, method: str, pieces: tuple[str, ...], spot, strike, rate, volatility, tte, dividend_yield,
           kind: str | None = None):
    """Read the shared inputs. Returns (numbers, put flag, params, early result or None)."""
    s, k, r, v, t, q = (c.num("spot", spot), c.num("strike", strike), c.num("rate", rate), c.num("volatility", volatility),
                        c.num("time_to_expiration", tte), c.num("dividend_yield", dividend_yield))
    params: dict = {"spot": s, "strike": k, "rate": r, "volatility": v, "time_to_expiration": t, "dividend_yield": q}
    put = None
    if kind is not None:
        put = c.kind_flag(kind)
        params["kind"] = kind
        if put is None:
            return (s, k, r, v, t, q), None, params, c.empty(name, unit, method, "kind must be 'call' or 'put'", params=params, piece_ids=pieces)
    problem = c.bs_domain(s, k, r, v, t, q)
    if problem:
        return (s, k, r, v, t, q), put, params, c.empty(name, unit, method, problem, params=params, piece_ids=pieces)
    return (s, k, r, v, t, q), put, params, None


@registry.register("options.first_order", module="options",
                   summary="First order greeks of a call or put: delta, dual_delta, vega (per vol point), theta (per day), rho, epsilon and lambda (elasticity). Per share.",
                   inputs=c.pick(*_GREEK_INPUTS, "kind"), piece_ids=("FT-026",))
def first_order(spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float,
                dividend_yield: float = 0.0, kind: str = "call") -> QuantResult:
    name, unit, method, pieces = "options.first_order", "greeks", "black_scholes_first_order", ("FT-026",)
    (s, k, r, v, t, q), put, params, early = _setup(name, unit, method, pieces, spot, strike, rate, volatility, time_to_expiration, dividend_yield, kind)
    if early is not None:
        return early
    g = _lib.ft_greeks

    def compute() -> dict:
        return {"delta": float(g.get_delta(s, k, t, r, v, q, put)), "dual_delta": float(g.get_dual_delta(s, k, t, r, v, q, put)),
                "vega": float(g.get_vega(s, k, t, r, v, q)), "theta": float(g.get_theta(s, k, t, r, v, q, put)),
                "rho": float(g.get_rho(s, k, t, r, v, q, put)), "epsilon": float(g.get_epsilon(s, k, t, r, v, q, put)),
                "lambda": float(g.get_lambda(s, k, t, r, v, q, put))}

    params["units"] = {"vega": "per vol point", "theta": "per calendar day", "rho": "per 1.00 rate", "epsilon": "per 1.00 dividend yield"}
    return c.guarded(name, unit, method, pieces, params, compute)


@registry.register("options.gamma", module="options",
                   summary="Black-Scholes gamma: change of delta per 1 dollar of spot, per share. The same for a call and a put. Single implementation shared by the rows for gamma.",
                   inputs=c.pick(*_GREEK_INPUTS), piece_ids=("FM-11", "FT-027"))
def gamma(spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float, dividend_yield: float = 0.0) -> QuantResult:
    name, unit, method, pieces = "options.gamma", "per_dollar", "black_scholes_gamma", ("FM-11", "FT-027")
    (s, k, r, v, t, q), _, params, early = _setup(name, unit, method, pieces, spot, strike, rate, volatility, time_to_expiration, dividend_yield)
    if early is not None:
        return early
    return c.guarded(name, unit, method, pieces, params, lambda: float(_lib.ft_greeks.get_gamma(s, k, t, r, v, q)))


@registry.register("options.dual_gamma", module="options",
                   summary="Dual gamma: second derivative of the option price with respect to the strike, per share.",
                   inputs=c.pick(*_GREEK_INPUTS), piece_ids=("FT-028",))
def dual_gamma(spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float, dividend_yield: float = 0.0) -> QuantResult:
    name, unit, method, pieces = "options.dual_gamma", "per_dollar", "black_scholes_dual_gamma", ("FT-028",)
    (s, k, r, v, t, q), _, params, early = _setup(name, unit, method, pieces, spot, strike, rate, volatility, time_to_expiration, dividend_yield)
    if early is not None:
        return early
    return c.guarded(name, unit, method, pieces, params, lambda: float(_lib.ft_greeks.get_dual_gamma(s, k, t, r, v, q)))


@registry.register("options.vanna", module="options",
                   summary="Vanna: sensitivity of delta to volatility (per 1.00 of volatility), per share. Single implementation shared by the rows for vanna.",
                   inputs=c.pick(*_GREEK_INPUTS), piece_ids=("FM-12", "FT-029"))
def vanna(spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float, dividend_yield: float = 0.0) -> QuantResult:
    name, unit, method, pieces = "options.vanna", "per_vol", "black_scholes_vanna", ("FM-12", "FT-029")
    (s, k, r, v, t, q), _, params, early = _setup(name, unit, method, pieces, spot, strike, rate, volatility, time_to_expiration, dividend_yield)
    if early is not None:
        return early
    return c.guarded(name, unit, method, pieces, params, lambda: float(_lib.ft_greeks.get_vanna(s, k, t, r, v, q)))


@registry.register("options.second_order", module="options",
                   summary="Second order greeks: charm (delta decay per year), vomma (vega convexity), vera (vega sensitivity to the rate) and veta (vega decay per vol point per day). Charm and veta explain the drift of a position through time.",
                   inputs=c.pick(*_GREEK_INPUTS, "kind"), piece_ids=("FT-030",))
def second_order(spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float,
                 dividend_yield: float = 0.0, kind: str = "call") -> QuantResult:
    name, unit, method, pieces = "options.second_order", "greeks", "black_scholes_second_order", ("FT-030",)
    (s, k, r, v, t, q), put, params, early = _setup(name, unit, method, pieces, spot, strike, rate, volatility, time_to_expiration, dividend_yield, kind)
    if early is not None:
        return early
    g = _lib.ft_greeks

    def compute() -> dict:
        return {"charm": float(g.get_charm(s, k, t, r, v, q, put)), "vomma": float(g.get_vomma(s, k, t, r, v, q)),
                "vera": float(g.get_vera(s, k, t, r, v, q)), "veta": float(g.get_veta(s, k, t, r, v, q))}

    params["units"] = {"charm": "delta per year", "vomma": "per 1.00 vol", "vera": "per 1.00 rate", "veta": "vol point per day"}
    return c.guarded(name, unit, method, pieces, params, compute)


@registry.register("options.second_order_partial", module="options",
                   summary="Second derivative of the option price with respect to the strike, d2C/dK2: exp(-r*T) times the risk-neutral lognormal density of the terminal price at the strike (Breeden-Litzenberger), per share.",
                   inputs=c.pick(*_GREEK_INPUTS), piece_ids=("FT-031",))
def second_order_partial(spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float,
                         dividend_yield: float = 0.0) -> QuantResult:
    name, unit, method, pieces = "options.second_order_partial", "per_dollar", "breeden_litzenberger_term", ("FT-031",)
    (s, k, r, v, t, q), _, params, early = _setup(name, unit, method, pieces, spot, strike, rate, volatility, time_to_expiration, dividend_yield)
    if early is not None:
        return early
    return c.guarded(name, unit, method, pieces, params,
                     lambda: float(_lib.ft_greeks.get_second_order_partial_derivative(s, k, t, r, v, q)))


@registry.register("options.third_order", module="options",
                   summary="Third order greeks written from the closed forms: speed (dgamma/dspot), zomma (dgamma/dvol), color (gamma decay per year) and ultima (dvomma/dvol). Reference values for convexity, per share.",
                   inputs=c.pick(*_GREEK_INPUTS), piece_ids=("FT-032",))
def third_order(spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float, dividend_yield: float = 0.0) -> QuantResult:
    name, unit, method, pieces = "options.third_order", "greeks", "black_scholes_third_order", ("FT-032",)
    (s, k, r, v, t, q), _, params, early = _setup(name, unit, method, pieces, spot, strike, rate, volatility, time_to_expiration, dividend_yield)
    if early is not None:
        return early

    def compute() -> dict:
        d1, d2 = c.d1_d2(s, k, r, v, t, q)
        root = v * math.sqrt(t)
        carry = math.exp(-q * t)
        pdf = c.norm_pdf(d1)
        speed = -carry * pdf / (s * s * root) * (d1 / root + 1.0)
        zomma = carry * pdf * (d1 * d2 - 1.0) / (s * v * v * math.sqrt(t))
        color = carry * pdf / (2.0 * s * t * root) * (2.0 * q * t + 1.0 + (2.0 * (r - q) * t - d2 * root) / root * d1)
        vega_unit = s * carry * pdf * math.sqrt(t)                      # per 1.00 of volatility
        ultima = -vega_unit / (v * v) * (d1 * d2 * (1.0 - d1 * d2) + d1 * d1 + d2 * d2)
        return {"speed": speed, "zomma": zomma, "color": color, "ultima": ultima}

    params["units"] = {"speed": "gamma per dollar", "zomma": "gamma per 1.00 vol", "color": "gamma per year", "ultima": "vomma per 1.00 vol"}
    return c.guarded(name, unit, method, pieces, params, compute)

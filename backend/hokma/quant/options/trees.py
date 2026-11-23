"""Binomial trees for European and American options (SPEC-019, B1; pieces FT-035 and ECT-045).

Four parametrizations (Cox-Ross-Rubinstein, Jarrow-Rudd, Tian, Leisen-Reimer), European and American exercise, the
exercise boundary, discrete cash dividends (escrowed model) and the convergence to Black-Scholes. Also the early
assignment rule of a short call before an ex-dividend date.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.types import QuantResult

METHODS = ("crr", "jarrow_rudd", "tian", "leisen_reimer")
STYLES = ("european", "american")
MAX_NODES_SHOWN = 12            # show_nodes only for trees this small
MAX_BOUNDARY_POINTS = 200
MAX_CONVERGENCE_POINTS = 20
MAX_DIVIDENDS = 40
_TREE = ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield", "kind")


def _peizer_pratt(z: float, n: int) -> float:
    """Leisen-Reimer inversion of the binomial distribution (Peizer-Pratt, method 2); n must be odd."""
    inner = 0.25 - 0.25 * math.exp(-((z / (n + 1.0 / 3.0 + 0.1 / (n + 1.0))) ** 2) * (n + 1.0 / 6.0))
    return 0.5 + math.copysign(math.sqrt(max(inner, 0.0)), z)


def tree_parameters(method: str, spot: float, strike: float, rate: float, volatility: float, tte: float, q: float, n: int) -> tuple[float, float, float]:
    """Up factor, down factor and up probability of one step. Raises ValueError when the probability leaves [0, 1]."""
    dt = tte / n
    growth = math.exp((rate - q) * dt)
    if method == "crr":
        u = math.exp(volatility * math.sqrt(dt))
        d = 1.0 / u
        p = (growth - d) / (u - d)
    elif method == "jarrow_rudd":
        drift = (rate - q - 0.5 * volatility ** 2) * dt
        u = math.exp(drift + volatility * math.sqrt(dt))
        d = math.exp(drift - volatility * math.sqrt(dt))
        p = 0.5
    elif method == "tian":
        v = math.exp(volatility ** 2 * dt)
        root = math.sqrt(v * v + 2.0 * v - 3.0)
        u = 0.5 * growth * v * (v + 1.0 + root)
        d = 0.5 * growth * v * (v + 1.0 - root)
        p = (growth - d) / (u - d)
    else:                                                                   # leisen_reimer
        d1, d2 = c.d1_d2(spot, strike, rate, volatility, tte, q)
        p = _peizer_pratt(d2, n)
        p_bar = _peizer_pratt(d1, n)
        u = growth * p_bar / p
        d = (growth - p * u) / (1.0 - p)
    if not (u > d > 0.0) or not 0.0 <= p <= 1.0:
        raise ValueError(f"the risk-neutral probability {p:.4f} or the factors (u={u:.6f}, d={d:.6f}) are not valid: use more steps or another method")
    return u, d, p


def _dividends(raw: Any, tte: float) -> list[tuple[float, float]]:
    """Discrete dividends as sorted (time in years, amount) pairs. Wrong shapes raise BadInput; dates outside (0, T) raise ValueError."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise BadInput(f"dividends must be a list of {{time, amount}} dicts, got {type(raw).__name__}")
    if len(raw) > MAX_DIVIDENDS:
        raise BadInput(f"dividends has more than {MAX_DIVIDENDS} entries")
    out: list[tuple[float, float]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise BadInput(f"dividends[{i}] must be a dict with time and amount")
        when, amount = c.num(f"dividends[{i}].time", item.get("time")), c.num(f"dividends[{i}].amount", item.get("amount"))
        if not (math.isfinite(when) and math.isfinite(amount)) or not 0.0 < when < tte or amount < 0.0:
            raise ValueError(f"dividends[{i}] needs a time inside (0, time_to_expiration) and an amount that is not negative")
        out.append((when, amount))
    return sorted(out)


def _price_tree(spot: float, strike: float, rate: float, vol: float, tte: float, q: float, put: bool, n: int, method: str,
                divs: list[tuple[float, float]], want_american: bool, want_european: bool, keep_nodes: bool) -> dict[str, Any]:
    """Backward induction. Returns the root values, first step delta, boundary and (optionally) the node values."""
    pv_divs = sum(a * math.exp(-rate * t) for t, a in divs)
    base = spot - pv_divs                                           # the tree runs on the stock net of the dividends it will pay
    if base <= 0.0:
        raise ValueError("the present value of the dividends is not below the spot price")
    u, d, p = tree_parameters(method, base, strike, rate, vol, tte, q, n)
    dt = tte / n
    disc = math.exp(-rate * dt)
    steps = np.arange(n + 1)
    terminal = base * np.power(u, steps) * np.power(d, n - steps)
    payoff = np.maximum(strike - terminal, 0.0) if put else np.maximum(terminal - strike, 0.0)
    eur = payoff.copy()
    amer = payoff.copy()
    boundary: list[tuple[int, float | None]] = []
    nodes_price: dict[int, list[float]] = {}
    nodes_value: dict[int, list[float]] = {}
    if keep_nodes:
        nodes_price[n], nodes_value[n] = terminal.tolist(), amer.tolist()
    delta = None
    for i in range(n - 1, -1, -1):
        j = np.arange(i + 1)
        prices = base * np.power(u, j) * np.power(d, i - j)
        eur = disc * (p * eur[1:] + (1.0 - p) * eur[:-1])
        cont = disc * (p * amer[1:] + (1.0 - p) * amer[:-1])
        t_i = i * dt
        actual = prices + sum(a * math.exp(-rate * (t - t_i)) for t, a in divs if t > t_i + 1e-12)
        exercise = np.maximum(strike - actual, 0.0) if put else np.maximum(actual - strike, 0.0)
        if want_american:
            hit = exercise > cont + 1e-12
            if i > 0 or n == 1:
                if hit.any():
                    boundary.append((i, float(actual[hit].max() if put else actual[hit].min())))
                else:
                    boundary.append((i, None))
            amer = np.where(hit, exercise, cont)
        else:
            amer = cont
        if i == 1:
            delta_values = (amer if want_american else eur)
            delta = float((delta_values[1] - delta_values[0]) / (prices[1] - prices[0]))
        if keep_nodes:
            nodes_price[i], nodes_value[i] = actual.tolist(), (amer if want_american else eur).tolist()
    out: dict[str, Any] = {"u": u, "d": d, "p": p, "european": float(eur[0]), "american": float(amer[0]) if want_american else None,
                           "delta": delta, "boundary": sorted(boundary), "base": base, "pv_dividends": pv_divs}
    if keep_nodes:
        out["node_prices"] = [nodes_price[i] for i in range(n + 1)]
        out["node_values"] = [nodes_value[i] for i in range(n + 1)]
    return out


def _thin(boundary: list[tuple[int, float | None]], dt: float) -> list[dict]:
    stride = max(1, math.ceil(len(boundary) / MAX_BOUNDARY_POINTS))
    return [{"step": i, "time": i * dt, "boundary": b} for i, b in boundary[::stride]]


@registry.register("options.binomial_tree", module="options",
                   summary="Option price per share from a binomial tree. method: crr, jarrow_rudd, tian or leisen_reimer (Leisen-Reimer needs an odd step count: an even one is raised by one). style: european or american. Optional discrete cash dividends [{time in years, amount}] by the escrowed model. Returns price, delta, u, d, p, the European tree value, the early exercise premium, the Black-Scholes reference (when no discrete dividends) and the exercise boundary of an American option. n_steps is capped at 5,000.",
                   inputs=c.pick(*_TREE, n_steps={"type": "int", "required": False, "default": 200},
                                 method={"type": "str", "required": False, "default": "crr"},
                                 style={"type": "str", "required": False, "default": "american"},
                                 dividends={"type": "list", "required": False, "default": None},
                                 show_nodes={"type": "bool", "required": False, "default": False}),
                   piece_ids=("ECT-045", "FT-035"))
def binomial_tree(spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float, dividend_yield: float = 0.0,
                  kind: str = "call", n_steps: int = 200, method: str = "crr", style: str = "american",
                  dividends: list | None = None, show_nodes: bool = False) -> QuantResult:
    name, unit, pieces = "options.binomial_tree", "price", ("ECT-045", "FT-035")
    s, k, r, v, t, q = (c.num("spot", spot), c.num("strike", strike), c.num("rate", rate), c.num("volatility", volatility),
                        c.num("time_to_expiration", time_to_expiration), c.num("dividend_yield", dividend_yield))
    n = c.positive_int("n_steps", n_steps)
    put = c.kind_flag(kind)
    if not isinstance(method, str) or not isinstance(style, str):
        raise BadInput("method and style must be strings")
    params = {"spot": s, "strike": k, "rate": r, "volatility": v, "time_to_expiration": t, "dividend_yield": q, "kind": kind,
              "n_steps": n, "method": method, "style": style, "dividends": dividends or [], "show_nodes": bool(show_nodes)}
    label = f"binomial_tree_{method}"
    if put is None:
        return c.empty(name, unit, label, "kind must be 'call' or 'put'", params=params, piece_ids=pieces)
    if method not in METHODS or style not in STYLES:
        return c.empty(name, unit, label, f"method must be one of {METHODS} and style one of {STYLES}", params=params, piece_ids=pieces)
    problem = c.bs_domain(s, k, r, v, t, q)
    if problem:
        return c.empty(name, unit, label, problem, params=params, piece_ids=pieces)
    warns: list[str] = []
    if method == "leisen_reimer" and n % 2 == 0:
        n += 1
        params["n_steps"] = n
        warns.append("Leisen-Reimer needs an odd number of steps: n_steps was raised by one")
    if show_nodes and n > MAX_NODES_SHOWN:
        return c.empty(name, unit, label, f"show_nodes is only for trees of at most {MAX_NODES_SHOWN} steps", params=params, piece_ids=pieces)

    def compute() -> dict:
        divs = _dividends(dividends, t)
        american = style == "american"
        res = _price_tree(s, k, r, v, t, q, put, n, method, divs, american, True, bool(show_nodes))
        base = res["base"]
        bsm = c.bs_price(base, k, r, v, t, q, put)
        price = res["american"] if american else res["european"]
        out = {"price": price, "delta": res["delta"], "u": res["u"], "d": res["d"], "p": res["p"], "european_tree": res["european"],
               "early_exercise_premium": (res["american"] - res["european"]) if american else 0.0,
               "bsm_price": bsm, "european_error_vs_bsm": res["european"] - bsm,
               "pv_dividends": res["pv_dividends"], "exercise_boundary": _thin(res["boundary"], t / n) if american else []}
        if show_nodes:
            out["node_prices"], out["node_values"] = res["node_prices"], res["node_values"]
        return out

    if dividends:
        warns.append("discrete dividends use the escrowed model: the tree runs on spot minus the present value of the dividends; bsm_price uses that same base")
    return c.guarded(name, unit, label, pieces, params, compute, warns)


@registry.register("options.tree_convergence", module="options",
                   summary="Binomial tree against Black-Scholes for a list of step counts (at most 20, each capped at 5,000): the price, the European Black-Scholes value and the error at each count. The convergence table of a tree, reproducible instead of quoted.",
                   inputs=c.pick(*_TREE, steps={"type": "list", "required": True}, method={"type": "str", "required": False, "default": "crr"},
                                 style={"type": "str", "required": False, "default": "european"}),
                   piece_ids=("ECT-045", "FT-035"))
def tree_convergence(spot: float, strike: float, rate: float, volatility: float, time_to_expiration: float, steps: list,
                     dividend_yield: float = 0.0, kind: str = "call", method: str = "crr", style: str = "european") -> QuantResult:
    name, unit, pieces = "options.tree_convergence", "price", ("ECT-045", "FT-035")
    s, k, r, v, t, q = (c.num("spot", spot), c.num("strike", strike), c.num("rate", rate), c.num("volatility", volatility),
                        c.num("time_to_expiration", time_to_expiration), c.num("dividend_yield", dividend_yield))
    if not isinstance(steps, (list, tuple)):
        raise BadInput(f"steps must be a list of step counts, got {type(steps).__name__}")
    if len(steps) > MAX_CONVERGENCE_POINTS:
        raise BadInput(f"steps has more than {MAX_CONVERGENCE_POINTS} entries")
    counts = [c.positive_int("n_steps", x) for x in steps]
    put = c.kind_flag(kind)
    params = {"spot": s, "strike": k, "rate": r, "volatility": v, "time_to_expiration": t, "dividend_yield": q, "kind": kind,
              "steps": counts, "method": method, "style": style}
    label = f"tree_convergence_{method}"
    if put is None:
        return c.empty(name, unit, label, "kind must be 'call' or 'put'", params=params, piece_ids=pieces)
    if not counts or method not in METHODS or style not in STYLES:
        return c.empty(name, unit, label, f"steps must not be empty, method one of {METHODS} and style one of {STYLES}", params=params, piece_ids=pieces)
    problem = c.bs_domain(s, k, r, v, t, q)
    if problem:
        return c.empty(name, unit, label, problem, params=params, piece_ids=pieces)

    def compute() -> dict:
        bsm = c.bs_price(s, k, r, v, t, q, put)
        rows = []
        for n in counts:
            n_eff = n + 1 if method == "leisen_reimer" and n % 2 == 0 else n
            res = _price_tree(s, k, r, v, t, q, put, n_eff, method, [], style == "american", style == "american", False)
            price = res["american"] if style == "american" else res["european"]
            rows.append({"n_steps": n_eff, "price": price, "bsm_price": bsm, "error": price - bsm,
                         "relative_error": (price - bsm) / bsm if bsm else None})
        return {"rows": rows, "bsm_price": bsm}

    return c.guarded(name, unit, label, pieces, params, compute,
                     ("for an American style the error includes the early exercise premium, which Black-Scholes does not have",) if style == "american" else ())


@registry.register("options.early_assignment_risk", module="options",
                   summary="Early assignment risk of a short call before an ex-dividend date. A necessary condition for exercising early: dividend > strike * (1 - exp(-rate * years_to_next_payment)), where years_to_next_payment runs from this ex-dividend date to the next dividend or to expiration. It is necessary, not sufficient. In practice an in-the-money short call is at risk when its remaining extrinsic value is below the dividend. Give call_price, or volatility and time_to_expiration to price it.",
                   inputs={"spot": {"type": "float", "required": True}, "strike": {"type": "float", "required": True},
                           "rate": {"type": "float", "required": True}, "dividend": {"type": "float", "required": True},
                           "years_to_next_payment": {"type": "float", "required": True},
                           "call_price": {"type": "float", "required": False, "default": None},
                           "volatility": {"type": "float", "required": False, "default": None},
                           "time_to_expiration": {"type": "float", "required": False, "default": None},
                           "dividend_yield": {"type": "float", "required": False, "default": 0.0}},
                   piece_ids=("ECT-045",))
def early_assignment_risk(spot: float, strike: float, rate: float, dividend: float, years_to_next_payment: float,
                          call_price: float | None = None, volatility: float | None = None, time_to_expiration: float | None = None,
                          dividend_yield: float = 0.0) -> QuantResult:
    name, unit, method, pieces = "options.early_assignment_risk", "flags", "ex_dividend_early_exercise_rule", ("ECT-045",)
    s, k, r, div, tau = (c.num("spot", spot), c.num("strike", strike), c.num("rate", rate), c.num("dividend", dividend),
                         c.num("years_to_next_payment", years_to_next_payment))
    price = c.num("call_price", call_price, allow_none=True)
    v = c.num("volatility", volatility, allow_none=True)
    t = c.num("time_to_expiration", time_to_expiration, allow_none=True)
    q = c.num("dividend_yield", dividend_yield)
    params = {"spot": s, "strike": k, "rate": r, "dividend": div, "years_to_next_payment": tau, "call_price": price,
              "volatility": v, "time_to_expiration": t, "dividend_yield": q}
    if not all(math.isfinite(x) for x in (s, k, r, div, tau, q)) or s <= 0 or k <= 0 or div < 0 or tau <= 0:
        return c.empty(name, unit, method, "spot and strike must be positive, the dividend not negative and years_to_next_payment positive; all finite",
                       params=params, piece_ids=pieces)
    if price is None and (v is None or t is None):
        return c.empty(name, unit, method, "give call_price, or volatility and time_to_expiration to price the call", params=params, piece_ids=pieces)
    if price is None:
        problem = c.bs_domain(s, k, r, v, t, q)
        if problem:
            return c.empty(name, unit, method, problem, params=params, piece_ids=pieces)
        price = c.bs_price(s, k, r, v, t, q, False)
    elif not math.isfinite(price) or price < 0:
        return c.empty(name, unit, method, "call_price must be a finite number that is not negative", params=params, piece_ids=pieces)
    threshold = k * (1.0 - math.exp(-r * tau))
    intrinsic = max(s - k, 0.0)
    extrinsic = price - intrinsic
    in_the_money = s > k
    necessary = div > threshold
    extrinsic_below = extrinsic < div
    value = {"threshold": threshold, "necessary_condition": necessary, "in_the_money": in_the_money, "intrinsic": intrinsic,
             "extrinsic": extrinsic, "extrinsic_below_dividend": extrinsic_below,
             "at_risk": bool(in_the_money and necessary and extrinsic_below)}
    warns = ["the dividend above the threshold is a necessary condition for early exercise, not a sufficient one"]
    if extrinsic < 0:
        warns.append("the call price is below its intrinsic value: check the quote")
    return c.guarded(name, unit, method, pieces, params, lambda: value, warns)

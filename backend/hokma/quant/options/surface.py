"""Volatility surface: SVI smile per expiry, calendar arbitrage check, risk-neutral density and the surface entry point
(SPEC-019, B1; pieces FT-038, FT-039 and FT-040).
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.options import _lib
from hokma.quant.types import QuantResult

MIN_POINTS = 5
MAX_POINTS = 5_000
MAX_EXPIRATIONS = 40
SVI_KEYS = ("a", "b", "rho", "m", "sigma")


def total_variance(k: np.ndarray | float, p: dict[str, float]) -> np.ndarray | float:
    """Raw SVI total variance w(k) = a + b * (rho * (k - m) + sqrt((k - m)^2 + sigma^2))."""
    return p["a"] + p["b"] * (p["rho"] * (k - p["m"]) + np.sqrt((k - p["m"]) ** 2 + p["sigma"] ** 2))


def butterfly_check(p: dict[str, float], k_min: float = -1.0, k_max: float = 1.0, points: int = 401) -> dict[str, Any]:
    """Static arbitrage across strikes: the density term g(k) must not be negative, and the wings must respect b*(1+|rho|) <= 4."""
    k = np.linspace(k_min, k_max, points)
    w = np.asarray(total_variance(k, p), dtype=float)
    r = np.sqrt((k - p["m"]) ** 2 + p["sigma"] ** 2)
    w1 = p["b"] * (p["rho"] + (k - p["m"]) / r)
    w2 = p["b"] * p["sigma"] ** 2 / r ** 3
    with np.errstate(divide="ignore", invalid="ignore"):
        g = (1.0 - k * w1 / (2.0 * w)) ** 2 - (w1 ** 2 / 4.0) * (1.0 / w + 0.25) + w2 / 2.0
    positive = w > 0
    g = np.where(positive, g, np.nan)
    min_g = float(np.nanmin(g)) if np.isfinite(g).any() else float("nan")
    return {"min_g": min_g, "butterfly_free": bool(positive.all() and min_g >= -1e-10), "wings_ok": bool(p["b"] * (1.0 + abs(p["rho"])) <= 4.0),
            "min_total_variance": float(np.min(w))}


def _params_dict(raw: Any, label: str = "svi_params") -> dict[str, float]:
    if not isinstance(raw, dict):
        raise BadInput(f"{label} must be a dict with a, b, rho, m, sigma")
    missing = [k for k in SVI_KEYS if k not in raw]
    if missing:
        raise BadInput(f"{label} is missing {missing}")
    return {k: c.num(f"{label}.{k}", raw[k]) for k in SVI_KEYS}


def svi_problem(p: dict[str, float]) -> str | None:
    """None when raw SVI parameters are usable: finite, b >= 0, |rho| < 1 and sigma > 0."""
    if not all(math.isfinite(v) for v in p.values()):
        return "svi parameters must be finite numbers (NaN or infinity found)"
    if p["b"] < 0 or abs(p["rho"]) >= 1.0 or p["sigma"] <= 0:
        return "svi parameters need b >= 0, |rho| < 1 and sigma > 0"
    return None


def _series(label: str, values: Any) -> np.ndarray:
    if not isinstance(values, (list, tuple, np.ndarray)):
        raise BadInput(f"{label} must be a list of numbers, got {type(values).__name__}")
    if len(values) > MAX_POINTS:
        raise BadInput(f"{label} has more than {MAX_POINTS} values")
    return np.array([c.num(f"{label}[]", x) for x in values], dtype=float)


def fit_svi(k: np.ndarray, iv: np.ndarray, tte: float) -> dict[str, Any]:
    """SVI fit of one expiry from log-moneyness and implied volatility. Raises ValueError when it cannot be fitted."""
    w = iv ** 2 * tte
    start = {key: float(val) for key, val in _lib.ft_svi.get_svi_parameters(k, w).items()}
    p = _lib.refine_svi(k, w, start)
    fitted_iv = np.asarray(_lib.ft_svi.get_svi_implied_volatility(k, tte, **p), dtype=float)
    rmse = float(np.sqrt(np.mean((fitted_iv - iv) ** 2)))
    out: dict[str, Any] = {"params": p, "rmse_iv": rmse, "fitted_iv": fitted_iv.tolist(), "n_points": int(len(k))}
    out.update(butterfly_check(p))
    return out


@registry.register("options.svi", module="options",
                   summary="Fit the raw SVI smile of one expiry from log-moneyness k = ln(strike / forward) and implied volatilities (decimals), with time in years. Returns the parameters a, b, rho, m, sigma, the fit error in volatility, the fitted volatilities and the static arbitrage check across strikes (butterfly) and wings. Needs at least 5 points.",
                   inputs={"log_moneyness": {"type": "list", "required": True}, "implied_vol": {"type": "list", "required": True},
                           "time_to_expiration": {"type": "float", "required": True}},
                   piece_ids=("FT-038",))
def svi(log_moneyness: list, implied_vol: list, time_to_expiration: float) -> QuantResult:
    name, unit, method, pieces = "options.svi", "vol", "svi_raw_fit", ("FT-038",)
    k, iv = _series("log_moneyness", log_moneyness), _series("implied_vol", implied_vol)
    t = c.num("time_to_expiration", time_to_expiration)
    params = {"log_moneyness": k.tolist(), "implied_vol": iv.tolist(), "time_to_expiration": t}
    if len(k) != len(iv):
        raise BadInput(f"log_moneyness and implied_vol must have the same length, got {len(k)} and {len(iv)}")
    keep = np.isfinite(k) & np.isfinite(iv) & (iv > 0)
    warns = (f"dropped {int((~keep).sum())} point(s) with NaN, infinite or non positive volatility",) if (~keep).any() else ()
    if not math.isfinite(t) or t <= 0 or t > c.MAX_YEARS:
        return c.empty(name, unit, method, warns + ("time_to_expiration must be positive years",), params=params, piece_ids=pieces)
    if int(keep.sum()) < MIN_POINTS:
        return c.empty(name, unit, method, warns + (f"need at least {MIN_POINTS} valid points, got {int(keep.sum())}",), params=params, piece_ids=pieces)
    return c.guarded(name, unit, method, pieces, params, lambda: fit_svi(k[keep], iv[keep], t), warns)


@registry.register("options.svi_calendar", module="options",
                   summary="Calendar arbitrage check between SVI slices: total variance must not fall as the expiry grows, at any log-moneyness in [k_min, k_max] (default -1 to 1; use the range the quotes cover, a fitted smile is not reliable far outside it). Input: {time in years: {a, b, rho, m, sigma}} for at least two expiries. Returns whether the surface is free of calendar arbitrage and the violations.",
                   inputs={"slices": {"type": "dict", "required": True}, "k_min": {"type": "float", "required": False, "default": -1.0},
                           "k_max": {"type": "float", "required": False, "default": 1.0}}, piece_ids=("FT-038",))
def svi_calendar(slices: dict, k_min: float = -1.0, k_max: float = 1.0) -> QuantResult:
    name, unit, method, pieces = "options.svi_calendar", "flags", "svi_calendar_arbitrage", ("FT-038",)
    if not isinstance(slices, dict):
        raise BadInput(f"slices must be a dict of time to SVI parameters, got {type(slices).__name__}")
    if len(slices) > MAX_EXPIRATIONS:
        raise BadInput(f"slices has more than {MAX_EXPIRATIONS} expiries")
    parsed: dict[float, dict[str, float]] = {}
    for key, raw in slices.items():
        try:
            parsed[float(key)] = _params_dict(raw, f"slices[{key}]")
        except (TypeError, ValueError) as exc:
            if isinstance(exc, BadInput):
                raise
            raise BadInput(f"slices key {key!r} is not a time in years") from exc
    lo, hi = c.num("k_min", k_min), c.num("k_max", k_max)
    params = {"slices": {str(k): v for k, v in parsed.items()}, "k_min": lo, "k_max": hi}
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo >= hi:
        return c.empty(name, unit, method, "k_min must be below k_max", params=params, piece_ids=pieces)
    if len(parsed) < 2:
        return c.empty(name, unit, method, "need at least two expiries", params=params, piece_ids=pieces)
    if any(not math.isfinite(t) or t <= 0 for t in parsed):
        return c.empty(name, unit, method, "every time must be positive years", params=params, piece_ids=pieces)
    for t, p in parsed.items():
        issue = svi_problem(p)
        if issue:
            return c.empty(name, unit, method, f"slice {t:g}: {issue}", params=params, piece_ids=pieces)

    def compute() -> dict:
        table = _lib.ft_svi.check_calendar_arbitrage(parsed, np.linspace(lo, hi, 401))
        count = int(len(table))
        worst = float(table["Total Variance Drop"].max()) if count else 0.0
        pairs = sorted({(float(a), float(b)) for a, b in zip(table["Shorter Expiration"], table["Longer Expiration"])}) if count else []
        return {"calendar_free": count == 0, "violations": count, "worst_variance_drop": worst,
                "violating_pairs": [{"shorter": a, "longer": b} for a, b in pairs]}

    return c.guarded(name, unit, method, pieces, params, compute)


@registry.register("options.risk_neutral_density", module="options",
                   summary="Risk-neutral density of the terminal price implied by an SVI smile (Breeden-Litzenberger). Give svi_params {a, b, rho, m, sigma}, or log_moneyness and implied_vol to fit one. Returns the strike grid, the density, its mass, mean and standard deviation, the probability of finishing below spot and below each of the optional levels, and flags negative density (arbitrage). n_strikes is capped at 2,000.",
                   inputs={"spot": {"type": "float", "required": True}, "rate": {"type": "float", "required": True},
                           "time_to_expiration": {"type": "float", "required": True}, "dividend_yield": {"type": "float", "required": False, "default": 0.0},
                           "svi_params": {"type": "dict", "required": False, "default": None},
                           "log_moneyness": {"type": "list", "required": False, "default": None}, "implied_vol": {"type": "list", "required": False, "default": None},
                           "strike_range": {"type": "float", "required": False, "default": 0.5},
                           "n_strikes": {"type": "int", "required": False, "default": 200},
                           "levels": {"type": "list", "required": False, "default": None}},
                   piece_ids=("FT-039",))
def risk_neutral_density(spot: float, rate: float, time_to_expiration: float, dividend_yield: float = 0.0, svi_params: dict | None = None,
                         log_moneyness: list | None = None, implied_vol: list | None = None, strike_range: float = 0.5,
                         n_strikes: int = 200, levels: list | None = None) -> QuantResult:
    name, unit, method, pieces = "options.risk_neutral_density", "density", "breeden_litzenberger_svi", ("FT-039",)
    s, r, t, q, width = (c.num("spot", spot), c.num("rate", rate), c.num("time_to_expiration", time_to_expiration),
                         c.num("dividend_yield", dividend_yield), c.num("strike_range", strike_range))
    n = c.positive_int("n_strikes", n_strikes, low=5)
    lv = _series("levels", levels) if levels is not None else np.array([])
    p_in = _params_dict(svi_params) if svi_params is not None else None
    ks = _series("log_moneyness", log_moneyness) if log_moneyness is not None else None
    ivs = _series("implied_vol", implied_vol) if implied_vol is not None else None
    params = {"spot": s, "rate": r, "time_to_expiration": t, "dividend_yield": q, "svi_params": p_in, "strike_range": width, "n_strikes": n,
              "levels": lv.tolist(), "log_moneyness": None if ks is None else ks.tolist(), "implied_vol": None if ivs is None else ivs.tolist()}
    problem = c.bs_domain(s, s, r, 0.2, t, q)
    if problem or not math.isfinite(width) or not 0.0 < width < 1.0:
        return c.empty(name, unit, method, problem or "strike_range must be a fraction in (0, 1)", params=params, piece_ids=pieces)
    if p_in is not None and svi_problem(p_in):
        return c.empty(name, unit, method, svi_problem(p_in), params=params, piece_ids=pieces)
    if p_in is None and (ks is None or ivs is None):
        return c.empty(name, unit, method, "give svi_params, or log_moneyness and implied_vol to fit one", params=params, piece_ids=pieces)
    if p_in is None and len(ks) != len(ivs):  # type: ignore[arg-type]
        raise BadInput("log_moneyness and implied_vol must have the same length")

    def compute() -> dict:
        p = p_in
        if p is None:
            keep = np.isfinite(ks) & np.isfinite(ivs) & (ivs > 0)           # type: ignore[arg-type]
            if int(keep.sum()) < MIN_POINTS:
                raise ValueError(f"need at least {MIN_POINTS} valid points to fit the smile")
            p = fit_svi(ks[keep], ivs[keep], t)["params"]                    # type: ignore[index]
        forward = s * math.exp((r - q) * t)
        dens = _lib.ft_rnd.get_risk_neutral_density(s, forward, t, r, q, p, width, n)
        strikes, values = dens.index.to_numpy(dtype=float), dens.to_numpy(dtype=float)
        step = strikes[1] - strikes[0]
        cum = np.concatenate([[0.0], np.cumsum(0.5 * (values[1:] + values[:-1]) * step)])
        mass = float(cum[-1])
        mean = float(np.trapezoid(strikes * values, strikes) / mass) if mass > 0 else float("nan")
        var = float(np.trapezoid((strikes - mean) ** 2 * values, strikes) / mass) if mass > 0 else float("nan")
        below = lambda x: float(np.interp(x, strikes, cum) / mass) if mass > 0 else float("nan")   # noqa: E731
        negative = int((values < -1e-12).sum())
        return {"strikes": strikes.tolist(), "density": values.tolist(), "mass": mass, "mean": mean, "std": math.sqrt(var) if var >= 0 else float("nan"),
                "forward": forward, "prob_below_spot": below(s), "prob_below_levels": {f"{x:g}": below(x) for x in lv.tolist()},
                "negative_density_points": negative, "svi_params": p}

    warns = ("the grid covers strikes within strike_range of the forward: mass below 1 means the tails are cut",)
    return c.guarded(name, unit, method, pieces, params, compute, warns)


def otm_smile(rows: list[dict[str, Any]], spot: float, rate: float, q: float, tte: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Strikes, log-moneyness and implied volatility of the out-of-the-money options of one expiry (puts below the forward, calls above).

    The volatility is the row's iv, or the inversion of its mid price when the row has none. Rows without either are skipped.
    """
    forward = spot * math.exp((rate - q) * tte)
    strikes, ivs = [], []
    for row in rows:
        k = c.row_float(row, "strike")
        put = c.row_kind(row)
        if k is None or put is None or k <= 0:
            continue
        if (put and k > forward) or (not put and k < forward):
            continue
        vol = c.row_float(row, "iv")
        if vol is None or vol <= 0:
            mid = c.row_float(row, "mid")
            bid, ask = c.row_float(row, "bid"), c.row_float(row, "ask")
            mid = mid if mid is not None else ((bid + ask) / 2.0 if bid is not None and ask is not None else None)
            if mid is None or mid <= 0:
                continue
            guess = float(_lib.ft_bs.get_implied_volatility(mid, spot, k, rate, tte, q, put))
            vol = guess if math.isfinite(guess) else None
        if vol is None or vol <= 0:
            continue
        strikes.append(k)
        ivs.append(vol)
    order = np.argsort(strikes)
    sk, iv = np.array(strikes, dtype=float)[order], np.array(ivs, dtype=float)[order]
    return sk, np.log(sk / forward) if len(sk) else sk, iv


def expirations_of(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Rows grouped by expiration date (ISO string), ordered by date."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        exp = row.get("expiration")
        if exp is None:
            continue
        groups.setdefault(str(exp)[:10], []).append(row)
    return dict(sorted(groups.items()))


@registry.register("options.surface", module="options",
                   summary="Volatility surface from an option chain: one SVI smile per expiry from the out-of-the-money options, the at-the-money volatility term structure, the static (butterfly) and calendar arbitrage checks and, optionally, the interpolated volatility at requested (strike, expiration) points. Give the chain rows (the contracts of the chain tool), spot, rate, dividend yield and as_of.",
                   inputs={"contracts": {"type": "list", "required": True}, "spot": {"type": "float", "required": True},
                           "rate": {"type": "float", "required": True}, "dividend_yield": {"type": "float", "required": False, "default": 0.0},
                           "as_of": {"type": "str", "required": True}, "query": {"type": "list", "required": False, "default": None}},
                   piece_ids=("FT-040",))
def surface(contracts: list, spot: float, rate: float, as_of: Any, dividend_yield: float = 0.0, query: list | None = None) -> QuantResult:
    name, unit, method, pieces = "options.surface", "vol", "svi_surface", ("FT-040",)
    rows = c.contracts_list(contracts)
    s, r, q = c.num("spot", spot), c.num("rate", rate), c.num("dividend_yield", dividend_yield)
    if query is not None and (not isinstance(query, (list, tuple)) or len(query) > MAX_POINTS):
        raise BadInput("query must be a list of {strike, expiration} dicts")
    params = {"spot": s, "rate": r, "dividend_yield": q, "as_of": str(as_of), "n_contracts": len(rows), "query": query or []}
    problem = c.bs_domain(s, s, r, 0.2, 1.0, q)
    if problem:
        return c.empty(name, unit, method, problem, params=params, piece_ids=pieces)
    groups = expirations_of(rows)
    if not groups:
        return c.empty(name, unit, method, "the chain has no rows with an expiration", params=params, piece_ids=pieces)
    if len(groups) > MAX_EXPIRATIONS:
        raise BadInput(f"the chain has more than {MAX_EXPIRATIONS} expirations")

    warns: list[str] = []

    def compute() -> dict:
        slices: dict[float, dict[str, float]] = {}
        ranges: list[tuple[float, float]] = []
        out: list[dict] = []
        for exp, group in groups.items():
            t = c.years_between(as_of, exp)
            if t is None or t <= 0:
                warns.append(f"{exp}: expired or unreadable date, skipped")
                continue
            sk, k, iv = otm_smile(group, s, r, q, t)
            if len(k) < MIN_POINTS:
                warns.append(f"{exp}: only {len(k)} usable out-of-the-money quote(s), skipped")
                continue
            fit = fit_svi(k, iv, t)
            forward = s * math.exp((r - q) * t)
            atm_w = float(total_variance(0.0, fit["params"]))
            slices[t] = fit["params"]
            ranges.append((float(k.min()), float(k.max())))
            out.append({"expiration": exp, "time_to_expiration": t, "forward": forward, "n_points": fit["n_points"], "svi_params": fit["params"],
                        "rmse_iv": fit["rmse_iv"], "atm_vol": math.sqrt(max(atm_w, 0.0) / t), "butterfly_free": fit["butterfly_free"],
                        "wings_ok": fit["wings_ok"]})
        if not out:
            raise ValueError("no expiry has enough quotes to fit a smile")
        calendar = None
        if len(slices) >= 2:
            low, high = max(a for a, _ in ranges), min(b for _, b in ranges)
            if low >= high:                                              # the quotes do not overlap: use a narrow band around the forward
                low, high = -0.05, 0.05
            table = _lib.ft_svi.check_calendar_arbitrage(slices, np.linspace(low, high, 201))
            calendar = {"calendar_free": bool(table.empty), "violations": int(len(table)), "k_min": low, "k_max": high}
        answers = []
        for item in query or []:
            if not isinstance(item, dict):
                raise BadInput("query entries must be dicts with strike and expiration")
            exp = str(item.get("expiration"))[:10]
            match = next((o for o in out if o["expiration"] == exp), None)
            strike = c.num("query.strike", item.get("strike"))
            if match is None or strike <= 0:
                answers.append({"strike": strike, "expiration": exp, "iv": None})
                continue
            kk = math.log(strike / match["forward"])
            w = float(total_variance(kk, match["svi_params"]))
            answers.append({"strike": strike, "expiration": exp, "iv": math.sqrt(max(w, 0.0) / match["time_to_expiration"])})
        return {"expirations": out, "calendar": calendar, "queries": answers,
                "atm_term_structure": [{"expiration": o["expiration"], "atm_vol": o["atm_vol"]} for o in out]}

    result = c.guarded(name, unit, method, pieces, params, compute)
    return QuantResult(name=result.name, value=result.value, unit=result.unit, method=result.method, params=result.params,
                       inputs_hash=result.inputs_hash, as_of=result.as_of, warnings=result.warnings + tuple(warns), piece_ids=result.piece_ids)

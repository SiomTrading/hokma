"""Profit and loss of an option strategy by date and price (SPEC-028, delivery 6, piece 3339-A): the table of the Option detail panel.

One function, options.pnl_grid, makes everything the panel draws from the legs of a strategy: the matrix of dollars by price (rows) and date
(columns), the exact breakevens at expiration, the curves at expiration and at half of the time left, and the figures of the header (net premium, maximum
profit and loss, probability of profit, expected value).

Conventions, the same as the Strategies panel (hokma.quant.options.strategies and structures, 3338-A, nothing is rewritten here):
- Dates before expiration are priced with Black-Scholes: the rate of the chain panel, dividend yield 0 unless the caller gives one, and the implied
  volatility of each leg (the one of the chain, or the one deduced from its mark; the caller resolves it) times iv_mult.
- The column of expiration is the payoff of the library (payoff.pnl_at), not a Black-Scholes at zero time, so each cell of it is the same number the
  Strategies cards show.
- The probability of profit is hokma.quant.options.strategies.probability_of_profit with the mean volatility of the option legs (times iv_mult) and
  the same rate; the expected value is structures.expected_pnl_usd (exact mean of the payoff under a lognormal price).
- Time: a column "days" is calendar days from now; the time left is years - days / 365 (the year of the module has 525600 minutes).
- Price rows are in percent of the current price, from -range_pct to +range_pct in equal steps; the row of the current price is exact (the middle one,
  so rows is odd). A leg without volatility leaves its columns before expiration empty (None) and says so in the warnings; the column of expiration
  never needs a volatility.
Limits of the grid are constants below: the panel reads them from here, it does not write its own.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.options import payoff as pay
from hokma.quant.options.strategies import DEFAULT_RATE, probability_of_profit
from hokma.quant.options.structures import expected_pnl_usd
from hokma.quant.types import QuantResult

RANGE_MIN, RANGE_MAX, DEFAULT_RANGE = 3.0, 20.0, 10.0          # percent each side of the price (the sliders of the panel)
IV_MULT_MIN, IV_MULT_MAX = 0.5, 2.0                            # multiple of the volatility of the chain
DEFAULT_ROWS, MIN_ROWS, MAX_ROWS = 13, 3, 61                   # price rows (odd: the middle one is the current price)
DEFAULT_COLUMNS, MAX_COLUMNS = 5, 12                           # date columns
DEFAULT_CURVE_POINTS, MIN_CURVE_POINTS, MAX_CURVE_POINTS = 101, 11, 201
DAYS_PER_YEAR = c.MINUTES_PER_YEAR / 1440.0                    # 365
EPS_DAYS = 1e-9


def default_days(total_days: float, columns: int = DEFAULT_COLUMNS) -> list[float]:
    """Columns spread evenly from today to expiration (both included)."""
    n = max(2, min(int(columns), MAX_COLUMNS))
    return [total_days * i / (n - 1) for i in range(n)]


def _roots(x: np.ndarray, y: np.ndarray) -> list[float]:
    """Prices where a sampled curve crosses zero, by linear interpolation between samples (approximate; the expiration ones are exact elsewhere)."""
    out: list[float] = []
    for x0, x1, y0, y1 in zip(x[:-1], x[1:], y[:-1], y[1:]):
        if y0 == 0.0:
            out.append(float(x0))
        elif y0 * y1 < 0.0:
            out.append(float(x0 + (0.0 - y0) * (x1 - x0) / (y1 - y0)))
    if y[-1] == 0.0:
        out.append(float(x[-1]))
    return sorted({round(v, 9) for v in out})


def _pnl_before_expiry(legs: list[pay.Leg], ivs: list[float], spots: np.ndarray, tte: float, rate: float, q: float) -> np.ndarray:
    """Dollars of profit or loss at a date before expiration: Black-Scholes value of each option leg minus what it cost (or plus what it paid)."""
    total = np.zeros(len(spots), dtype=float)
    it = iter(ivs)
    for leg in legs:
        if leg.instrument == "stock":
            total += leg.sign * (spots - leg.premium) * leg.quantity * leg.multiplier
            continue
        vol = next(it)
        values = np.array([c.bs_price(float(s), leg.strike, rate, vol, tte, q, leg.put) for s in spots], dtype=float)
        total += leg.sign * (values - leg.premium) * leg.quantity * leg.multiplier
    return total


@registry.register("options.pnl_grid", module="options",
                   summary="Profit and loss in dollars of an option strategy by date (columns) and price (rows), Black-Scholes before expiration and the payoff of the library at expiration. legs [{instrument option or stock, position long or short, kind call or put, strike, premium per share (what was paid or received), quantity in contracts (shares for stock), multiplier}] with the contract multiplier of the sheet (required, no default). spot is the current price, years the time to expiration, days the columns in calendar days from now (the last one at or past years x 365 is expiration), range_pct the percent each side of the price (3 to 20), rows an odd number of price rows (the middle one is the current price), iv_by_leg the volatility of each option leg in order (None when unknown) and iv_mult its multiple (0.5 to 2). Returns the matrix, the exact breakevens at expiration (one or two for the usual structures), the curves at expiration and at half of the time left with their breakevens, and net premium, maximum profit and loss, probability of profit and expected value.",
                   inputs={"legs": {"type": "list", "required": True}, "multiplier": {"type": "float", "required": False, "default": None},
                           "spot": {"type": "float", "required": True}, "years": {"type": "float", "required": True},
                           "range_pct": {"type": "float", "required": False, "default": DEFAULT_RANGE}, "rows": {"type": "int", "required": False, "default": DEFAULT_ROWS},
                           "days": {"type": "list", "required": False, "default": None}, "iv_by_leg": {"type": "list", "required": False, "default": None},
                           "iv_mult": {"type": "float", "required": False, "default": 1.0}, "rate": {"type": "float", "required": False, "default": DEFAULT_RATE},
                           "dividend_yield": {"type": "float", "required": False, "default": 0.0},
                           "curve_points": {"type": "int", "required": False, "default": DEFAULT_CURVE_POINTS}},
                   piece_ids=("FT-020", "FT-036"))
def pnl_grid(legs: list, spot: float, years: float, multiplier: float | None = None, range_pct: float = DEFAULT_RANGE, rows: int = DEFAULT_ROWS,
              days: list | None = None, iv_by_leg: list | None = None, iv_mult: float = 1.0, rate: float = DEFAULT_RATE,
             dividend_yield: float = 0.0, curve_points: int = DEFAULT_CURVE_POINTS) -> QuantResult:
    name, unit, method, pieces = "options.pnl_grid", "usd", "black_scholes_by_date_and_price", ("FT-020", "FT-036")
    s, t = c.num("spot", spot), c.num("years", years)
    rng, mult_iv = c.num("range_pct", range_pct), c.num("iv_mult", iv_mult)
    r, q = c.num("rate", rate), c.num("dividend_yield", dividend_yield)
    n_rows = c.positive_int("rows", rows, low=1)
    n_curve = c.positive_int("curve_points", curve_points, low=1)
    params = {"legs": legs if isinstance(legs, list) else [], "multiplier": multiplier, "spot": s, "years": t, "range_pct": rng, "rows": n_rows,
              "days": days, "iv_by_leg": iv_by_leg, "iv_mult": mult_iv, "rate": r, "dividend_yield": q, "curve_points": n_curve}
    if isinstance(legs, (list, tuple)) and not legs:
        return c.empty(name, unit, method, "legs must contain at least one leg", params=params, piece_ids=pieces)
    try:
        parsed = pay.parse_legs(legs, multiplier)
    except BadInput:
        raise
    except ValueError as exc:
        return c.empty(name, unit, method, str(exc), params=params, piece_ids=pieces)
    if not all(math.isfinite(x) for x in (s, t, rng, mult_iv, r, q)) or s <= 0 or t <= 0:
        return c.empty(name, unit, method, "spot and years must be positive and every number finite (the option may have expired)", params=params, piece_ids=pieces)
    if not RANGE_MIN <= rng <= RANGE_MAX:
        return c.empty(name, unit, method, f"range_pct must be from {RANGE_MIN:g} to {RANGE_MAX:g} percent", params=params, piece_ids=pieces)
    if not IV_MULT_MIN <= mult_iv <= IV_MULT_MAX:
        return c.empty(name, unit, method, f"iv_mult must be from {IV_MULT_MIN:g} to {IV_MULT_MAX:g}", params=params, piece_ids=pieces)
    if not MIN_ROWS <= n_rows <= MAX_ROWS or n_rows % 2 == 0:
        return c.empty(name, unit, method, f"rows must be an odd number from {MIN_ROWS} to {MAX_ROWS}", params=params, piece_ids=pieces)
    if not MIN_CURVE_POINTS <= n_curve <= MAX_CURVE_POINTS:
        return c.empty(name, unit, method, f"curve_points must be from {MIN_CURVE_POINTS} to {MAX_CURVE_POINTS}", params=params, piece_ids=pieces)
    total_days = t * DAYS_PER_YEAR
    if days is None:
        day_list = default_days(total_days)
    else:
        if not isinstance(days, (list, tuple)):
            raise BadInput(f"days must be a list of numbers, got {type(days).__name__}")
        if len(days) > MAX_COLUMNS:
            return c.empty(name, unit, method, f"days has more than {MAX_COLUMNS} columns", params=params, piece_ids=pieces)
        day_list = sorted({min(c.num("days[]", d), total_days) for d in days})     # clipped to the term first, then deduplicated
    if not day_list or not all(math.isfinite(d) and 0.0 <= d for d in day_list):
        return c.empty(name, unit, method, "days must be a non-empty list of numbers from 0 (today) on", params=params, piece_ids=pieces)
    option_legs = [leg for leg in parsed if leg.instrument == "option"]
    if iv_by_leg is None:
        base_ivs: list[float | None] = [None] * len(option_legs)
    else:
        if not isinstance(iv_by_leg, (list, tuple)):
            raise BadInput(f"iv_by_leg must be a list, got {type(iv_by_leg).__name__}")
        if len(iv_by_leg) != len(option_legs):
            return c.empty(name, unit, method, f"iv_by_leg needs one entry per option leg ({len(option_legs)}), got {len(iv_by_leg)}", params=params, piece_ids=pieces)
        base_ivs = [None if v is None else c.num("iv_by_leg[]", v) for v in iv_by_leg]
    for v in base_ivs:
        if v is not None and not (math.isfinite(v) and 0.0 < v * mult_iv <= c.MAX_VOL):
            return c.empty(name, unit, method, f"every volatility must be a decimal above zero and, times iv_mult, at most {c.MAX_VOL:g}", params=params, piece_ids=pieces)
    domain = next((m for m in (c.bs_domain(s, leg.strike, r, 0.2, 0.1, q) for leg in option_legs) if m), None)     # the volatility and time of the probe are inside the domain
    if domain:
        return c.empty(name, unit, method, domain, params=params, piece_ids=pieces)
    warns: list[str] = []

    def compute() -> dict[str, Any]:
        ivs = [None if v is None else v * mult_iv for v in base_ivs]
        priced = all(v is not None for v in ivs)
        if not priced:
            warns.append("a leg has no volatility: the columns before expiration, the curve at half of the time, the probability of profit and the expected value are empty; the column of expiration and the payoff figures do not need it")
        pct = np.linspace(-rng, rng, n_rows)
        mid_row = n_rows // 2
        pct[mid_row] = 0.0
        prices = s * (1.0 + pct / 100.0)
        prices[mid_row] = s
        columns: list[dict[str, Any]] = []
        matrix = np.full((n_rows, len(day_list)), np.nan)
        for j, d in enumerate(day_list):
            left = t - min(d, total_days) / DAYS_PER_YEAR
            expiry = d >= total_days - EPS_DAYS or left <= 0.0
            columns.append({"days": min(d, total_days) if expiry else d, "expiration": expiry, "years_left": 0.0 if expiry else left})
            if expiry:
                matrix[:, j] = pay.pnl_at(parsed, prices)
            elif priced:
                matrix[:, j] = _pnl_before_expiry(parsed, [float(v) for v in ivs], prices, left, r, q)     # type: ignore[arg-type]
        info = pay.profile(parsed)
        net = pay.net_premium_usd(parsed)
        vol = sum(float(v) for v in ivs) / len(ivs) if priced and ivs else None     # the mean of ALL the legs, or nothing: never the mean of the ones that have it
        prob = None if vol is None else probability_of_profit(parsed, info["breakevens"], s, vol, t, r)
        ev = None if vol is None else expected_pnl_usd(parsed, s, r, q, vol, t)
        # curves over the same range, finer: at expiration (exact payoff) and at half of the time left
        grid = np.linspace(s * (1.0 - rng / 100.0), s * (1.0 + rng / 100.0), n_curve)
        expiry_curve = pay.pnl_at(parsed, grid)
        mid_days = total_days / 2.0
        mid_curve = _pnl_before_expiry(parsed, [float(v) for v in ivs], grid, t - mid_days / DAYS_PER_YEAR, r, q) if priced else None   # type: ignore[arg-type]
        return {
            "spot": s, "years": t, "days_to_expiration": total_days, "rate": r, "dividend_yield": q, "iv_mult": mult_iv, "range_pct": rng,
            "iv_by_leg": ivs, "iv_mean": vol, "spot_row": mid_row,
            "rows": [{"pct": float(p), "price": float(x), "current": i == mid_row} for i, (p, x) in enumerate(zip(pct, prices))],
            "columns": columns, "pnl_usd": [[None if np.isnan(v) else float(v) for v in row] for row in matrix],
            "breakevens": info["breakevens"], "curves": {
                "spots": grid.tolist(), "expiration_pnl_usd": expiry_curve.tolist(), "mid_days": mid_days,
                "mid_pnl_usd": None if mid_curve is None else mid_curve.tolist(),
                "mid_breakevens": None if mid_curve is None else _roots(grid, mid_curve),
                "mid_breakevens_approximate": True,      # linear interpolation between samples of the grid, not exact
                "mid_breakevens_in_range_only": True,    # searched only between the first and last price of the grid; a crossing outside it is not reported
                "mid_breakevens_range": [float(grid[0]), float(grid[-1])]},
            "metrics": {"net_premium_usd": net, "premium_type": "debit" if net > 1e-12 else ("credit" if net < -1e-12 else "even"),
                        "max_profit_usd": info["max_profit_usd"], "unbounded_profit": info["unbounded_profit"],
                        "max_loss_usd": info["max_loss_usd"], "unbounded_loss": info["unbounded_loss"], "breakevens": info["breakevens"],
                        "p_profit": None if prob is None else prob["p_profit"], "p_profit_intervals": None if prob is None else prob["intervals"],
                        "expected_value_usd": ev},
        }

    return c.guarded(name, unit, method, pieces, params, compute, warns)

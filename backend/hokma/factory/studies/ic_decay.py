"""Information coefficient against horizon (SPEC-020: ECT-036).

How fast does a signal lose its power, and does what is left pay for the trades? Four pieces:

  * forward_returns: the return from the open of the next bar to the open `horizon` bars later (the fill of the house is the next open).
  * ic_curve: the rank correlation of the signal with that return at each horizon, the gross gain per bet and its break-even cost.
  * effective_breadth: bets that move together count less than independent ones, so the breadth of the fundamental law is reduced.
  * annual_cost and breakeven_cost_bps: the cost of turnover and the cost at which a gross edge is used up.

The rank correlation comes from `hokma.quant.portfolio.ic` (H3); a caller can inject its own function.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from hokma.factory.studies import _h3
from hokma.quant.errors import BadInput
from hokma.quant.limits import PARAM_CAPS, as_int

MAX_HORIZONS = 32
IcFn = Callable[[np.ndarray, np.ndarray], Any]


@dataclass(frozen=True)
class IcPoint:
    horizon: int
    ic: float | None                    # rank correlation of the signal with the forward return
    n_obs: int
    gross_per_bet: float | None         # mean of sign(signal) * forward return: one round trip per bet
    breakeven_bps: float | None         # round trip cost at which the gross gain is used up; None when there is no gain
    ir_per_year: float | None           # ic * sqrt(effective bets per year)


@dataclass(frozen=True)
class IcCurve:
    points: tuple[IcPoint, ...]
    best_horizon: int | None            # the horizon with the largest positive ir_per_year
    warnings: tuple[str, ...] = ()


def _number(name: str, value: Any, *, low: float, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < low or (high is not None and value > high):
        raise BadInput(f"{name} must be a number from {low}{'' if high is None else f' to {high}'}, got {value!r}")
    return float(value)


def _series(name: str, values: Any) -> np.ndarray:
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError):
        raise BadInput(f"{name} must be numbers") from None
    if array.ndim != 1 or array.size == 0:
        raise BadInput(f"{name} must be a non-empty one dimensional list")
    return array


def forward_returns(opens: Any, horizon: int) -> np.ndarray:
    """y[t] = open[t + 1 + horizon] / open[t + 1] - 1. The last 1 + horizon values are NaN: their future does not exist yet."""
    prices = _series("opens", opens)
    h = as_int("horizon", horizon)
    if h < 1 or h > PARAM_CAPS["horizon"]:
        raise BadInput(f"horizon must be from 1 to {PARAM_CAPS['horizon']}, got {h}")
    out = np.full(len(prices), np.nan)
    if len(prices) > 1 + h:
        entry, exit_ = prices[1:len(prices) - h], prices[1 + h:]
        with np.errstate(divide="ignore", invalid="ignore"):
            out[:len(prices) - 1 - h] = np.where(entry > 0, exit_ / entry - 1.0, np.nan)
    return out


def effective_breadth(n_bets: float, avg_correlation: float) -> float:
    """Independent bets that carry the same risk: n / (1 + (n - 1) * rho). Intraday bets on one index are far from independent."""
    n = _number("n_bets", n_bets, low=1.0)
    rho = _number("avg_correlation", avg_correlation, low=0.0, high=0.999999)
    return n / (1.0 + (n - 1.0) * rho)


def annual_cost(turnover_per_period: float, periods_per_year: float, cost_bps: float) -> float:
    """Yearly cost as a fraction of capital: one way turnover per period times periods times the cost of one unit of turnover."""
    return _number("turnover_per_period", turnover_per_period, low=0.0) * _number("periods_per_year", periods_per_year, low=0.0) \
        * _number("cost_bps", cost_bps, low=0.0) / 10_000.0


def breakeven_cost_bps(gross_alpha: float, turnover_per_period: float, periods_per_year: float = 1.0) -> float | None:
    """Cost per unit of turnover, in basis points, that leaves a net edge of zero: gross alpha over yearly turnover. None without turnover."""
    yearly = _number("turnover_per_period", turnover_per_period, low=0.0) * _number("periods_per_year", periods_per_year, low=0.0)
    if yearly == 0.0:
        return None
    return float(gross_alpha) / yearly * 10_000.0


def ic_curve(signal: Any, opens: Any, horizons: Sequence[int], *, bars_per_year: float, bet_correlation: float, min_obs: int,
             ic_fn: IcFn | None = None) -> IcCurve:
    """The curve of IC against horizon. `bet_correlation` and `min_obs` are stated by the caller: nothing is assumed about them.

    Rows where the signal or the forward return is not a number are dropped. A horizon with fewer than `min_obs` rows has no IC.
    """
    x, prices = _series("signal", signal), _series("opens", opens)
    if len(x) != len(prices):
        raise BadInput(f"signal and opens must have the same length, got {len(x)} and {len(prices)}")
    if not isinstance(horizons, (list, tuple)) or not 1 <= len(horizons) <= MAX_HORIZONS:
        raise BadInput(f"horizons is a list of 1 to {MAX_HORIZONS} whole numbers")
    hs = sorted({as_int("horizon", h) for h in horizons})
    minimum = as_int("min_obs", min_obs)
    if minimum < 3:
        raise BadInput("min_obs must be at least 3")
    bars = _number("bars_per_year", bars_per_year, low=1.0)
    rho = _number("bet_correlation", bet_correlation, low=0.0, high=0.999999)
    fn = ic_fn or _h3.information_coefficient
    points: list[IcPoint] = []
    warnings: list[str] = []
    for h in hs:
        y = forward_returns(prices, h)
        keep = np.isfinite(x) & np.isfinite(y)
        xs, ys = x[keep], y[keep]
        if len(xs) < minimum:
            warnings.append(f"horizon {h}: {len(xs)} rows, fewer than min_obs {minimum}")
            points.append(IcPoint(h, None, int(len(xs)), None, None, None))
            continue
        ic = _h3.scalar(fn(xs, ys))
        if ic is None:
            warnings.append(f"horizon {h}: the information coefficient is not defined")
        gross = float(np.mean(np.sign(xs) * ys))
        ir = None if ic is None else ic * math.sqrt(effective_breadth(max(bars / h, 1.0), rho))
        points.append(IcPoint(h, ic, int(len(xs)), gross, gross * 10_000.0 if gross > 0 else None, ir))
    ranked = [p for p in points if p.ir_per_year is not None and p.ir_per_year > 0]
    best = max(ranked, key=lambda p: p.ir_per_year).horizon if ranked else None   # type: ignore[arg-type,return-value]
    return IcCurve(tuple(points), best, tuple(warnings))

"""Trade-style statistics of a return series: streaks, exposure, win rate, payoff, profit factor, outlier ratios, Kelly cap
and the risk of ruin.

All figures come from period returns, not from discrete trades: a multi-day trade that spans several up and down days
counts once per day here. Use a trade journal for trade-level statistics.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.metrics import _lib
from hokma.quant.metrics.summary import _SERIES_IN, empty, finish, prepare_series, scalar
from hokma.quant.types import QuantResult


# --------------------------------------------------------------------------------------------------------------
# Plain values on an already prepared series (shared with the summary blocks)
# --------------------------------------------------------------------------------------------------------------
def streak_value(r: pd.Series, *, wins: bool) -> int | None:
    s = _lib.returns_stats()
    try:
        fn = s.consecutive_wins if wins else s.consecutive_losses
        return int(fn(r, prepare_returns=False))
    except (ValueError, TypeError):
        return None


def win_rate_value(r: pd.Series) -> float | None:
    return scalar(_lib.returns_stats().win_rate, r, prepare_returns=False)


def avg_return_value(r: pd.Series) -> float | None:
    return scalar(_lib.returns_stats().avg_return, r, prepare_returns=False)


def avg_win_value(r: pd.Series) -> float | None:
    return scalar(_lib.returns_stats().avg_win, r, prepare_returns=False)


def avg_loss_value(r: pd.Series) -> float | None:
    return scalar(_lib.returns_stats().avg_loss, r, prepare_returns=False)


def payoff_value(r: pd.Series) -> float | None:
    return scalar(_lib.returns_stats().payoff_ratio, r, prepare_returns=False)


def profit_ratio_value(r: pd.Series) -> float | None:
    return scalar(_lib.returns_stats().profit_ratio, r, prepare_returns=False)


def profit_factor_value(r: pd.Series) -> float | None:
    return scalar(_lib.returns_stats().profit_factor, r, prepare_returns=False)


def outlier_win_value(r: pd.Series) -> float | None:
    return scalar(_lib.returns_stats().outlier_win_ratio, r, prepare_returns=False)


def outlier_loss_value(r: pd.Series) -> float | None:
    return scalar(_lib.returns_stats().outlier_loss_ratio, r, prepare_returns=False)


def kelly_value(r: pd.Series) -> float | None:
    return scalar(_lib.returns_stats().kelly_criterion, r, prepare_returns=False)


# --------------------------------------------------------------------------------------------------------------
# QS-006: streaks
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.consecutive_streaks", module="metrics", summary="Longest run of winning periods and longest run of losing periods.",
                   inputs={"returns": _SERIES_IN}, piece_ids=("QS-006",))
def consecutive_streaks(returns: Any) -> QuantResult:
    name, pieces, method = "metrics.consecutive_streaks", ("QS-006",), "longest run of positive and of negative period returns"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "count", method, warns, pieces)
    value = {"max_consecutive_wins": streak_value(r, wins=True), "max_consecutive_losses": streak_value(r, wins=False)}
    return finish(name, value, unit="count", method=method, series=r, pieces=pieces, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# QS-007: exposure
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.exposure", module="metrics", summary="Time in market: share of periods with a non-zero return, rounded up to a whole percent.",
                   inputs={"returns": _SERIES_IN}, piece_ids=("QS-007",))
def exposure(returns: Any) -> QuantResult:
    name, pieces, method = "metrics.exposure", ("QS-007",), "non-zero periods over observed periods, rounded up to 1%"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "pct", method, warns, pieces)
    value = scalar(_lib.returns_stats().exposure, r, prepare_returns=False)
    return finish(name, value, unit="pct", method=method, series=r, pieces=pieces, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# QS-008: win rate and averages
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.win_rate_stats", module="metrics", summary="Win rate over non-zero periods with average return, average win and average loss.",
                   inputs={"returns": _SERIES_IN}, piece_ids=("QS-008",))
def win_rate_stats(returns: Any) -> QuantResult:
    name, pieces, method = "metrics.win_rate_stats", ("QS-008",), "positive over non-zero periods; means of non-zero, positive and negative returns"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "mixed", method, warns, pieces)
    value = {"win_rate": win_rate_value(r), "avg_return": avg_return_value(r), "avg_win": avg_win_value(r), "avg_loss": avg_loss_value(r)}
    return finish(name, value, unit="mixed", method=method, series=r, pieces=pieces, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# QS-025: payoff family
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.payoff_ratios", module="metrics", summary="Payoff ratio, win/loss ratio, profit ratio and profit factor.",
                   inputs={"returns": _SERIES_IN}, piece_ids=("QS-025",))
def payoff_ratios(returns: Any) -> QuantResult:
    name, pieces, method = "metrics.payoff_ratios", ("QS-025",), "average win over average loss; win frequency over loss frequency; gross win over gross loss"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "ratio", method, warns, pieces)
    payoff = payoff_value(r)
    value = {"payoff_ratio": payoff, "win_loss_ratio": payoff, "profit_ratio": profit_ratio_value(r), "profit_factor": profit_factor_value(r)}
    return finish(name, value, unit="ratio", method=method, series=r, pieces=pieces, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# QS-027: outlier dependence
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.outlier_ratios", module="metrics", summary="Outlier win and loss ratios: how much a few extreme days weigh against the typical day.",
                   inputs={"returns": _SERIES_IN}, piece_ids=("QS-027",))
def outlier_ratios(returns: Any) -> QuantResult:
    name, pieces, method = "metrics.outlier_ratios", ("QS-027",), "99th percentile over mean gain; 1st percentile over mean loss"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "ratio", method, warns, pieces)
    value = {"outlier_win_ratio": outlier_win_value(r), "outlier_loss_ratio": outlier_loss_value(r)}
    return finish(name, value, unit="ratio", method=method, series=r, pieces=pieces, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# QS-031: Kelly as a cap
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.kelly_cap", module="metrics",
                   summary="Kelly fraction of the return series, reported as a ceiling for position size (never as the size). The fraction scales the ceiling.",
                   inputs={"returns": _SERIES_IN, "fraction": {"type": "float", "required": False, "default": 1.0}}, piece_ids=("QS-031",))
def kelly_cap(returns: Any, fraction: float = 1.0) -> QuantResult:
    name, pieces, method = "metrics.kelly_cap", ("QS-031",), "(payoff * win rate - loss rate) / payoff; ceiling = max(0, kelly) * fraction"
    if not isinstance(fraction, (int, float)) or not 0.0 < fraction <= 1.0:
        raise BadInput("fraction must be in (0, 1]")
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "fraction", method, warns, pieces, {"fraction": fraction})
    k = kelly_value(r)
    warns = tuple(warns)
    if k is not None and k < 0:
        warns += ("negative Kelly fraction: no edge, the ceiling is 0",)
    value = {"kelly": k, "position_cap": None if k is None else max(0.0, k) * fraction}
    return finish(name, value, unit="fraction", method=method, series=r, pieces=pieces, params={"fraction": fraction}, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# QS-022a: risk of ruin from the real sizes of wins and losses
# --------------------------------------------------------------------------------------------------------------
def ruin_probability(p_win: float, avg_win: float, avg_loss: float, ruin_level: float) -> float:
    """Probability of ever losing ruin_level of the capital when each period wins avg_win with probability p_win and
    loses avg_loss otherwise.

    Wealth is multiplicative, so the walk is on log wealth with steps a = ln(1 + avg_win) and -c = ln(1 - avg_loss).
    The ruin barrier is B = -ln(1 - ruin_level). The probability is exp(-theta * B), where theta > 0 is the root of
    p * exp(-theta * a) + (1 - p) * exp(theta * c) = 1 (the adjustment coefficient). With no positive drift the
    probability is 1.
    """
    q = 1.0 - p_win
    if p_win <= 0.0:
        return 1.0
    if avg_loss <= 0.0:
        return 0.0
    if avg_loss >= 1.0:
        return 1.0
    a = math.log1p(avg_win)
    c = -math.log1p(-avg_loss)
    barrier = -math.log1p(-ruin_level)
    drift = p_win * a - q * c
    if drift <= 0.0:
        return 1.0
    if q <= 0.0:
        return 0.0

    def f(theta: float) -> float:
        return p_win * math.exp(-theta * a) + q * math.exp(theta * c) - 1.0

    hi = 1.0
    while f(hi) <= 0.0 and hi < 1e6:
        hi *= 2.0
    lo = 0.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0.0:
            hi = mid
        else:
            lo = mid
    theta = 0.5 * (lo + hi)
    return float(min(1.0, math.exp(-theta * barrier)))


def _check_ruin_level(ruin_level: float) -> None:
    if not isinstance(ruin_level, (int, float)) or not 0.0 < ruin_level < 1.0:
        raise BadInput("ruin_level must be a fraction of capital strictly between 0 and 1")


@registry.register("metrics.risk_of_ruin_params", module="metrics",
                   summary="Risk of ruin from a win rate, an average win and an average loss (fractions of capital) and a ruin level.",
                   inputs={"win_rate": {"type": "float", "required": True}, "avg_win": {"type": "float", "required": True},
                           "avg_loss": {"type": "float", "required": True}, "ruin_level": {"type": "float", "required": False, "default": 0.5}},
                   piece_ids=("QS-022a",))
def risk_of_ruin_params(win_rate: float, avg_win: float, avg_loss: float, ruin_level: float = 0.5) -> QuantResult:
    name, pieces = "metrics.risk_of_ruin_params", ("QS-022a",)
    method = "adjustment coefficient of the log-wealth walk with the real sizes of wins and losses"
    _check_ruin_level(ruin_level)
    params = {"win_rate": win_rate, "avg_win": avg_win, "avg_loss": abs(avg_loss) if isinstance(avg_loss, (int, float)) else avg_loss, "ruin_level": ruin_level}
    for key in ("win_rate", "avg_win", "avg_loss"):
        v = params[key]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            return empty(name, "prob", method, (f"{key} is not a finite number",), pieces, params)
    if not 0.0 <= win_rate <= 1.0:
        return empty(name, "prob", method, ("win_rate must be between 0 and 1",), pieces, params)
    if avg_win < 0:
        return empty(name, "prob", method, ("avg_win must not be negative",), pieces, params)
    warns: tuple[str, ...] = ()
    if params["avg_loss"] >= 1.0:
        warns = ("average loss is 100% of capital or more: one loss ruins the account",)
    value = ruin_probability(win_rate, avg_win, params["avg_loss"], ruin_level)
    return finish(name, value, unit="prob", method=method, series=None, pieces=pieces, params=params, warnings=warns)


@registry.register("metrics.risk_of_ruin", module="metrics",
                   summary="Probability of losing ruin_level of the capital, from the real win rate and the real average win and loss of the series.",
                   inputs={"returns": _SERIES_IN, "ruin_level": {"type": "float", "required": False, "default": 0.5}}, piece_ids=("QS-022a",))
def risk_of_ruin(returns: Any, ruin_level: float = 0.5) -> QuantResult:
    name, pieces = "metrics.risk_of_ruin", ("QS-022a",)
    method = "adjustment coefficient of the log-wealth walk with the real sizes of wins and losses"
    _check_ruin_level(ruin_level)
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "prob", method, warns, pieces, {"ruin_level": ruin_level})
    nonzero = r[r != 0]
    wins, losses = nonzero[nonzero > 0], nonzero[nonzero < 0]
    if nonzero.empty:
        return empty(name, "prob", method, tuple(warns) + ("no non-zero returns",), pieces, {"ruin_level": ruin_level})
    p_win = len(wins) / len(nonzero)
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0
    params = {"win_rate": p_win, "avg_win": avg_win, "avg_loss": avg_loss, "ruin_level": ruin_level}
    value = ruin_probability(p_win, avg_win, avg_loss, ruin_level)
    return finish(name, value, unit="prob", method=method, series=r, pieces=pieces, params=params, warnings=warns)

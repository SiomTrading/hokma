"""Trade-style statistics of a return series: streaks, exposure, win rate, payoff, profit factor, outlier ratios, Kelly cap
and the risk of ruin.

All figures come from period returns, not from discrete trades: a multi-day trade that spans several up and down days
counts once per day here. Use a trade journal for trade-level statistics.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from hokma.quant import helpers, registry
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


KELLY_NOTICE = "cap, not a target: size from risk limits and use Kelly only as a ceiling"


# --------------------------------------------------------------------------------------------------------------
# QS-031: Kelly as a cap
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.kelly_cap", module="metrics",
                   summary="Kelly fraction of the return series, reported as a ceiling for position size (never as the size). The fraction scales the ceiling.",
                   inputs={"returns": _SERIES_IN, "fraction": {"type": "float", "required": False, "default": 1.0}}, piece_ids=("QS-031", "B-22"))
def kelly_cap(returns: Any, fraction: float = 1.0) -> QuantResult:
    name, pieces, method = "metrics.kelly_cap", ("QS-031", "B-22"), "(payoff * win rate - loss rate) / payoff; ceiling = max(0, kelly) * fraction"
    if not isinstance(fraction, (int, float)) or not 0.0 < fraction <= 1.0:
        raise BadInput("fraction must be in (0, 1]")
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "fraction", method, warns, pieces, {"fraction": fraction})
    k = kelly_value(r)
    warns = tuple(warns) + (KELLY_NOTICE,)
    if k is not None and k < 0:
        warns += ("negative Kelly fraction: no edge, the ceiling is 0",)
    value = {"kelly": k, "position_cap": None if k is None else max(0.0, k) * fraction}
    return finish(name, value, unit="fraction", method=method, series=r, pieces=pieces, params={"fraction": fraction}, warnings=warns)


kelly = kelly_cap          # B-22: one implementation under both names; the cap is never negative and always says it is not a target


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


# --------------------------------------------------------------------------------------------------------------
# Trade level statistics (SPEC-020, 5.4.4): one row per closed trade, never per period.
# Win rate, payoff and profit factor reuse the functions above on the result of each trade: one implementation.
# --------------------------------------------------------------------------------------------------------------
_TABLE_IN = {"type": "table", "required": True}


def _clean_pnl(values: Any, label: str = "pnl") -> tuple[pd.Series, tuple[str, ...]]:
    """Result of each trade as a clean float series (NaN and infinite values dropped, with a warning)."""
    series = helpers.as_series(values, name=label)
    cleaned = helpers.clean(series).reset_index(drop=True)
    dropped = len(series) - len(cleaned)
    return cleaned, ((f"dropped {dropped} NaN or infinite value(s)",) if dropped else ())


def streak_runs(values: Any) -> tuple[int, int]:
    """Longest run of winning trades and longest run of losing trades. A trade with zero result cuts both runs."""
    best_win = best_loss = win = loss = 0
    for v in helpers.as_series(values, name="pnl").tolist():
        win = win + 1 if v > 0 else 0
        loss = loss + 1 if v < 0 else 0
        best_win, best_loss = max(best_win, win), max(best_loss, loss)
    return best_win, best_loss


def sqn_value(pnl: pd.Series) -> float | None:
    """System quality number: square root of N times the mean result over its sample standard deviation."""
    n = len(pnl)
    if n < 2:
        return None
    sd = float(pnl.std(ddof=1))
    if not math.isfinite(sd) or sd == 0.0:
        return None
    return helpers.finite_or_none(math.sqrt(n) * float(pnl.mean()) / sd)


def expectancy_r(win_rate: float | None, payoff: float | None) -> float | None:
    """Expected result per unit of average loss: win rate times payoff minus loss rate (losses and wins over non-zero trades)."""
    if win_rate is None or payoff is None:
        return None
    return win_rate * payoff - (1.0 - win_rate)


def mae_mfe(side: str, entry_price: Any, qty: Any, multiplier: Any, highs: Any, lows: Any) -> tuple[Any, Any]:
    """Worst open loss (at most 0) and best open gain (at least 0) of a trade in USD, from the highs and lows of the bars it lived through.

    The entry bar counts: a trade that is hurt in its first bar has a non-zero MAE. Numbers keep their type (Decimal in, Decimal out).
    """
    highs, lows = list(highs), list(lows)
    if not highs or len(highs) != len(lows):
        raise BadInput("mae_mfe needs the highs and lows of at least one bar, with the same length")
    if side not in ("long", "short"):
        raise BadInput("side must be 'long' or 'short'")
    size = qty * multiplier
    zero = size * 0
    if side == "long":
        adverse, favorable = (min(lows) - entry_price) * size, (max(highs) - entry_price) * size
    else:
        adverse, favorable = (entry_price - max(highs)) * size, (entry_price - min(lows)) * size
    return min(zero, adverse), max(zero, favorable)


def _trade_frame(trades: Any) -> pd.DataFrame:
    if isinstance(trades, pd.DataFrame):
        frame = trades.copy()
    elif isinstance(trades, (list, tuple)) and all(isinstance(t, dict) for t in trades):
        frame = pd.DataFrame(list(trades))
    else:
        raise BadInput("trades must be a table or a list of rows with a pnl_usd column")
    if frame.empty and "pnl_usd" not in frame.columns:
        frame = pd.DataFrame({"pnl_usd": []}, dtype=float)
    if "pnl_usd" not in frame.columns:
        raise BadInput("trades need a pnl_usd column")
    for column in ("pnl_usd", "entry_price", "qty", "multiplier", "costs_usd", "mae_usd", "mfe_usd"):
        if column in frame.columns:
            try:
                frame[column] = frame[column].astype(float)
            except (TypeError, ValueError) as exc:
                raise BadInput(f"trades: {column} is not numeric") from exc
    for column in ("entry_at", "exit_at"):
        if column in frame.columns:
            try:
                frame[column] = pd.to_datetime(frame[column], utc=True)
            except (TypeError, ValueError) as exc:
                raise BadInput(f"trades: {column} is not a date") from exc
    return frame


def _mean_or_none(series: pd.Series) -> float | None:
    return helpers.finite_or_none(series.mean()) if len(series) else None


def _exposure_share(entry: pd.Series, exit_: pd.Series) -> float | None:
    """Share of the span between the first entry and the last exit during which at least one trade was open."""
    spans = sorted(zip(entry.tolist(), exit_.tolist()))
    if not spans:
        return None
    total = (max(e for _, e in spans) - spans[0][0]).total_seconds()
    if total <= 0:
        return None
    covered, cursor = 0.0, spans[0][0]
    for start, end in spans:
        start = max(start, cursor)
        if end > start:
            covered += (end - start).total_seconds()
            cursor = end
    return covered / total


def _duration_figures(frame: pd.DataFrame, wins: pd.Series, losses: pd.Series) -> dict[str, float | None]:
    if "entry_at" not in frame.columns or "exit_at" not in frame.columns:
        return {}
    seconds = (frame["exit_at"] - frame["entry_at"]).dt.total_seconds()
    return {"avg_duration_seconds": _mean_or_none(seconds), "avg_win_duration_seconds": _mean_or_none(seconds[wins.index]),
            "avg_loss_duration_seconds": _mean_or_none(seconds[losses.index]), "exposure": _exposure_share(frame["entry_at"], frame["exit_at"])}


@registry.register("metrics.trade_stats", module="metrics",
                   summary="Statistics per closed trade: counts, win rate, payoff, profit factor, expectancy, SQN, streaks, durations, exposure, return per trade, MAE and MFE in USD.",
                   inputs={"trades": _TABLE_IN}, piece_ids=("B-19", "B-23", "V-17", "L-34"))
def trade_stats(trades: Any) -> QuantResult:
    name, pieces = "metrics.trade_stats", ("B-19", "B-23", "V-17", "L-34")
    method = "one row per closed trade; rates over non-zero trades; profit factor is gross profit over gross loss"
    frame = _trade_frame(trades)
    pnl_all = frame["pnl_usd"]
    frame = frame[pnl_all.notna() & ~pnl_all.isin([float("inf"), float("-inf")])].reset_index(drop=True)
    warns: list[str] = []
    if len(frame) != len(pnl_all):
        warns.append(f"dropped {len(pnl_all) - len(frame)} trade(s) with no finite result")
    if frame.empty:
        return empty(name, "mixed", method, tuple(warns) + ("no closed trades",), pieces)
    pnl = frame["pnl_usd"]
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    win_rate, payoff = win_rate_value(pnl), payoff_value(pnl)
    profit_factor = profit_factor_value(pnl)
    if profit_factor is None:
        warns.append("profit factor is undefined: there are no losing trades (not reported as infinite)" if losses.empty
                     else "profit factor is undefined for this input")
    best_win_run, best_loss_run = streak_runs(pnl)
    value: dict[str, Any] = {
        "n_trades": int(len(pnl)), "n_wins": int(len(wins)), "n_losses": int(len(losses)),
        "win_rate": win_rate, "loss_rate": None if win_rate is None else 1.0 - win_rate,
        "total_pnl_usd": float(pnl.sum()), "gross_profit_usd": float(wins.sum()), "gross_loss_usd": float(losses.sum()),
        "largest_win_usd": float(wins.max()) if len(wins) else 0.0, "largest_loss_usd": float(losses.min()) if len(losses) else 0.0,
        "avg_pnl_usd": float(pnl.mean()), "avg_win_usd": _mean_or_none(wins), "avg_loss_usd": _mean_or_none(losses),
        "payoff_ratio": payoff, "profit_factor": profit_factor, "expectancy_usd": avg_return_value(pnl),
        "expectancy_r": expectancy_r(win_rate, payoff), "sqn": sqn_value(pnl),
        "pnl_std_usd": helpers.finite_or_none(pnl.std(ddof=1)) if len(pnl) > 1 else None,
        "max_consecutive_wins": best_win_run, "max_consecutive_losses": best_loss_run,
    }
    value.update(_duration_figures(frame, wins, losses))
    if {"entry_price", "qty"} <= set(frame.columns):
        notional = (frame["entry_price"] * frame["qty"] * (frame["multiplier"] if "multiplier" in frame.columns else 1.0)).abs()
        returns = (pnl / notional).replace([float("inf"), float("-inf")], float("nan")).dropna()
        value.update({"avg_return_per_trade": _mean_or_none(returns), "best_return_per_trade": float(returns.max()) if len(returns) else None,
                      "worst_return_per_trade": float(returns.min()) if len(returns) else None})
    if "costs_usd" in frame.columns:
        value["total_costs_usd"] = float(frame["costs_usd"].sum())
    for column, label, pick in (("mae_usd", "mae", min), ("mfe_usd", "mfe", max)):
        if column in frame.columns and frame[column].notna().any():
            values = frame[column].dropna()
            value[f"avg_{label}_usd"], value[f"largest_{label}_usd"] = float(values.mean()), float(pick(values))
    return finish(name, value, unit="mixed", method=method, series=pnl, pieces=pieces, warnings=tuple(warns))


@registry.register("metrics.sqn", module="metrics",
                   summary="System quality number of the result of each trade: sqrt(N) * mean / sample standard deviation. Needs at least 2 trades.",
                   inputs={"pnl": _SERIES_IN}, piece_ids=("B-21", "V-17"))
def sqn(pnl: Any) -> QuantResult:
    name, pieces, method = "metrics.sqn", ("B-21", "V-17"), "sqrt(N) * mean(result per trade) / std(result per trade, ddof=1)"
    series, warns = _clean_pnl(pnl)
    if len(series) < 2:
        return empty(name, "ratio", method, tuple(warns) + (f"need at least 2 trades, got {len(series)}",), pieces)
    value = sqn_value(series)
    if value is None:
        return empty(name, "ratio", method, tuple(warns) + ("every trade has the same result: dispersion is zero",), pieces)
    return finish(name, value, unit="ratio", method=method, series=series, pieces=pieces, warnings=warns)


@registry.register("metrics.trade_streaks", module="metrics",
                   summary="Longest run of winning trades and of losing trades (a trade with zero result cuts a run); they weigh on funded accounts.",
                   inputs={"pnl": _SERIES_IN}, piece_ids=("V-18",))
def streaks(pnl: Any) -> QuantResult:
    name, pieces, method = "metrics.trade_streaks", ("V-18",), "longest consecutive trades with a result above zero and below zero"
    series, warns = _clean_pnl(pnl)
    if series.empty:
        return empty(name, "count", method, tuple(warns) + ("no trades",), pieces)
    wins, losses = streak_runs(series)
    return finish(name, {"max_consecutive_wins": wins, "max_consecutive_losses": losses}, unit="count", method=method, series=series,
                  pieces=pieces, warnings=warns)

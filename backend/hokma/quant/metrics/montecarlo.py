"""Monte Carlo views of a return series.

Permuting the returns changes the order and nothing else: the mean, the standard deviation and the compounded product are the
same in every path. So permutation is used for what it can inform, the drawdown and the probability of a bust. Sharpe, CAGR
and the probability of reaching a goal come from a stationary bootstrap, which resamples blocks with replacement and so changes
the totals. Every result declares its seed and its number of runs.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.metrics.summary import _PPY_IN, _SERIES_IN, empty, finish, prepare_series
from hokma.quant.types import QuantResult

DEFAULT_RUNS = 1000
DEFAULT_SEED = 0
DEFAULT_MEAN_BLOCK = 10
_MAX_RUNS = 200_000


def _check_runs(runs: int, seed: int) -> None:
    from hokma.quant import limits

    limits.check_param("runs", runs)
    if not isinstance(runs, int) or isinstance(runs, bool) or not 2 <= runs <= _MAX_RUNS:
        raise BadInput(f"runs must be an integer between 2 and {_MAX_RUNS}")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise BadInput("seed must be an integer")


def _percentiles(values: np.ndarray) -> dict[str, float]:
    s = pd.Series(values)
    return {"min": s.min(), "max": s.max(), "mean": s.mean(), "median": s.median(), "std": s.std(), "percentile_5": s.quantile(0.05),
            "percentile_50": s.quantile(0.50), "percentile_95": s.quantile(0.95)}


# --------------------------------------------------------------------------------------------------------------
# Permutation: maximum drawdown and bust probability (QS-037a, QS-059a)
# --------------------------------------------------------------------------------------------------------------
def permutation_max_drawdowns(values: np.ndarray, runs: int, seed: int, *, include_start_capital: bool = False) -> np.ndarray:
    """Maximum drawdown of each permuted path. The first path is the original order. By default the peak starts at the
    first path value; with include_start_capital the starting capital (1.0) is the first peak."""
    from hokma.quant import limits

    limits.check_param("runs", runs)
    limits.check_product("runs x length", runs, len(values))
    rng = np.random.default_rng(seed)
    n = len(values)
    paths = np.empty((n, runs))
    paths[:, 0] = values
    for i in range(1, runs):
        paths[:, i] = rng.permutation(values)
    wealth = np.cumprod(1.0 + paths, axis=0)
    if include_start_capital:
        wealth = np.vstack([np.ones((1, runs)), wealth])
    peak = np.maximum.accumulate(wealth, axis=0)
    return ((wealth - peak) / peak).min(axis=0)


def drawdown(returns: Any, runs: int = DEFAULT_RUNS, seed: int = DEFAULT_SEED, bust: float | None = None,
             statistic: str = "max_drawdown", include_start_capital: bool = False) -> QuantResult:
    name, pieces = "metrics.montecarlo_drawdown", ("QS-037a", "QS-059a", "CC-01")
    method = "permutation of the returns; maximum drawdown per path"
    if statistic != "max_drawdown":
        raise BadInput(f"permutation cannot inform {statistic!r}: it changes neither the mean, the deviation nor the compounded "
                       "product. Use metrics.montecarlo_sharpe, metrics.montecarlo_cagr or metrics.montecarlo_goal_probability.")
    _check_runs(runs, seed)
    if bust is not None and not (isinstance(bust, (int, float)) and -1.0 < bust < 0.0):
        raise BadInput("bust must be a negative drawdown threshold above -1 (for example -0.2)")
    params = {"runs": runs, "seed": seed, "bust": bust, "include_start_capital": include_start_capital}
    r, warns = prepare_series(returns, min_obs=5)
    if r is None:
        return empty(name, "pct", method, warns, pieces, params)
    dd = permutation_max_drawdowns(r.to_numpy(dtype=float), runs, seed, include_start_capital=include_start_capital)
    value: dict[str, Any] = dict(_percentiles(dd))
    value["bust_probability"] = None if bust is None else float((dd <= bust).sum() / len(dd))
    value["runs"], value["seed"] = runs, seed
    return finish(name, value, unit="pct", method=method, series=r, pieces=pieces, params=params, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# Stationary bootstrap: Sharpe, CAGR and goal probability (QS-037b, QS-059b)
# --------------------------------------------------------------------------------------------------------------
def bootstrap_paths(values: np.ndarray, runs: int, seed: int, mean_block: int = DEFAULT_MEAN_BLOCK) -> np.ndarray:
    """Matrix (runs, n) of resampled returns. Blocks start at random positions and last mean_block periods on average."""
    from hokma.quant import limits

    limits.check_param("runs", runs)
    limits.check_param("mean_block", mean_block)
    limits.check_product("runs x length", runs, len(values))
    rng = np.random.default_rng(seed)
    n = len(values)
    starts = rng.integers(0, n, size=(runs, n))
    jump = rng.random((runs, n)) < (1.0 / mean_block)
    jump[:, 0] = True
    idx = np.empty((runs, n), dtype=np.int64)
    idx[:, 0] = starts[:, 0]
    for t in range(1, n):
        idx[:, t] = np.where(jump[:, t], starts[:, t], (idx[:, t - 1] + 1) % n)
    return values[idx]


def _bootstrap_inputs(returns: Any, runs: int, seed: int, mean_block: int, name: str, unit: str, method: str, pieces: tuple[str, ...], params: dict):
    _check_runs(runs, seed)
    if not isinstance(mean_block, int) or isinstance(mean_block, bool) or mean_block < 1:
        raise BadInput("mean_block must be a positive integer")
    r, warns = prepare_series(returns, min_obs=10)
    if r is None:
        return None, None, empty(name, unit, method, warns, pieces, params)
    return r, warns, None


def sharpe(returns: Any, runs: int = DEFAULT_RUNS, seed: int = DEFAULT_SEED, mean_block: int = DEFAULT_MEAN_BLOCK,
           periods_per_year: int = 252, rf: float = 0.0) -> QuantResult:
    name, pieces = "metrics.montecarlo_sharpe", ("QS-037b", "QS-059b")
    method = "stationary bootstrap; annualised Sharpe per path"
    params = {"runs": runs, "seed": seed, "mean_block": mean_block, "periods_per_year": periods_per_year, "rf": rf}
    r, warns, bad = _bootstrap_inputs(returns, runs, seed, mean_block, name, "ratio", method, pieces, params)
    if bad is not None:
        return bad
    paths = bootstrap_paths(r.to_numpy(dtype=float), runs, seed, mean_block)
    excess = paths - rf / periods_per_year
    sd = excess.std(axis=1, ddof=1)
    sharpes = np.where(sd > 0, excess.mean(axis=1) / np.where(sd > 0, sd, 1.0) * math.sqrt(periods_per_year), np.nan)
    sharpes = sharpes[np.isfinite(sharpes)]
    value: dict[str, Any] = dict(_percentiles(sharpes))
    value["runs"], value["seed"] = runs, seed
    return finish(name, value, unit="ratio", method=method, series=r, pieces=pieces, params=params, warnings=warns)


def cagr(returns: Any, runs: int = DEFAULT_RUNS, seed: int = DEFAULT_SEED, mean_block: int = DEFAULT_MEAN_BLOCK,
         periods_per_year: int = 252) -> QuantResult:
    name, pieces = "metrics.montecarlo_cagr", ("QS-037b", "QS-059b")
    method = "stationary bootstrap; compound annual growth rate per path"
    params = {"runs": runs, "seed": seed, "mean_block": mean_block, "periods_per_year": periods_per_year}
    r, warns, bad = _bootstrap_inputs(returns, runs, seed, mean_block, name, "pct", method, pieces, params)
    if bad is not None:
        return bad
    paths = bootstrap_paths(r.to_numpy(dtype=float), runs, seed, mean_block)
    total = np.prod(1.0 + paths, axis=1)
    years = paths.shape[1] / periods_per_year
    growth = np.where(total > 0, np.power(np.where(total > 0, total, 1.0), 1.0 / years) - 1.0, -1.0)
    value: dict[str, Any] = dict(_percentiles(growth))
    value["runs"], value["seed"] = runs, seed
    return finish(name, value, unit="pct", method=method, series=r, pieces=pieces, params=params, warnings=warns)


def goal_probability(returns: Any, goal: float, runs: int = DEFAULT_RUNS, seed: int = DEFAULT_SEED, mean_block: int = DEFAULT_MEAN_BLOCK) -> QuantResult:
    name, pieces = "metrics.montecarlo_goal_probability", ("QS-037b", "QS-059b")
    method = "stationary bootstrap; share of paths whose compounded return reaches the goal"
    if not isinstance(goal, (int, float)) or isinstance(goal, bool) or not math.isfinite(goal):
        raise BadInput("goal must be a finite number (a compounded return, for example 0.5 for +50%)")
    params = {"goal": goal, "runs": runs, "seed": seed, "mean_block": mean_block}
    r, warns, bad = _bootstrap_inputs(returns, runs, seed, mean_block, name, "prob", method, pieces, params)
    if bad is not None:
        return bad
    paths = bootstrap_paths(r.to_numpy(dtype=float), runs, seed, mean_block)
    final = np.prod(1.0 + paths, axis=1) - 1.0
    value = {"goal_probability": float((final >= goal).sum() / runs), "runs": runs, "seed": seed}
    return finish(name, value, unit="prob", method=method, series=r, pieces=pieces, params=params, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# Container with every figure of one run (QS-059a, QS-059b)
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class MonteCarloResult:
    """Everything one run reports. Drawdown and bust probability come from permutation; the rest from the stationary bootstrap."""
    runs: int
    seed: int
    max_drawdown: dict[str, float] | None
    bust_probability: float | None
    goal_probability: float | None
    sharpe: dict[str, float] | None
    cagr: dict[str, float] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run(returns: Any, runs: int = DEFAULT_RUNS, seed: int = DEFAULT_SEED, bust: float | None = None, goal: float | None = None,
        mean_block: int = DEFAULT_MEAN_BLOCK, periods_per_year: int = 252) -> tuple[MonteCarloResult | None, tuple[str, ...]]:
    """All figures of a run, or None and the reasons when the data cannot be used."""
    dd = drawdown(returns, runs, seed, bust)
    sh = sharpe(returns, runs, seed, mean_block, periods_per_year)
    cg = cagr(returns, runs, seed, mean_block, periods_per_year)
    gp = goal_probability(returns, goal, runs, seed, mean_block) if goal is not None else None
    reasons = tuple(dict.fromkeys(w for res in (dd, sh, cg) for w in res.warnings))
    if dd.value is None and sh.value is None and cg.value is None:
        return None, reasons
    dd_stats = None if dd.value is None else {k: v for k, v in dd.value.items() if k not in ("bust_probability", "runs", "seed")}
    return MonteCarloResult(
        runs=runs, seed=seed, max_drawdown=dd_stats,
        bust_probability=None if dd.value is None else dd.value["bust_probability"],
        goal_probability=None if gp is None or gp.value is None else gp.value["goal_probability"],
        sharpe=None if sh.value is None else {k: v for k, v in sh.value.items() if k not in ("runs", "seed")},
        cagr=None if cg.value is None else {k: v for k, v in cg.value.items() if k not in ("runs", "seed")},
    ), reasons


_MC_IN = {"returns": _SERIES_IN, "runs": {"type": "int", "required": False, "default": DEFAULT_RUNS},
          "seed": {"type": "int", "required": False, "default": DEFAULT_SEED}}


@registry.register("metrics.montecarlo_drawdown", module="metrics",
                   summary="Distribution of the maximum drawdown over permuted paths, with the probability of a bust. Reports drawdown only.",
                   inputs={**_MC_IN, "bust": {"type": "float", "required": False, "default": None}, "statistic": {"type": "str", "required": False, "default": "max_drawdown"},
                           "include_start_capital": {"type": "bool", "required": False, "default": False}},
                   piece_ids=("QS-037a", "QS-059a", "CC-01"))
def _montecarlo_drawdown_entry(returns: Any, runs: int = DEFAULT_RUNS, seed: int = DEFAULT_SEED, bust: float | None = None,
                               statistic: str = "max_drawdown", include_start_capital: bool = False) -> QuantResult:
    return drawdown(returns, runs, seed, bust, statistic, include_start_capital)


@registry.register("metrics.montecarlo_sharpe", module="metrics", summary="Distribution of the annualised Sharpe over stationary bootstrap paths (percentiles 5, 50, 95).",
                   inputs={**_MC_IN, "mean_block": {"type": "int", "required": False, "default": DEFAULT_MEAN_BLOCK}, "periods_per_year": _PPY_IN,
                           "rf": {"type": "float", "required": False, "default": 0.0}}, piece_ids=("QS-037b", "QS-059b"))
def _montecarlo_sharpe_entry(returns: Any, runs: int = DEFAULT_RUNS, seed: int = DEFAULT_SEED, mean_block: int = DEFAULT_MEAN_BLOCK,
                             periods_per_year: int = 252, rf: float = 0.0) -> QuantResult:
    return sharpe(returns, runs, seed, mean_block, periods_per_year, rf)


@registry.register("metrics.montecarlo_cagr", module="metrics", summary="Distribution of the compound annual growth rate over stationary bootstrap paths (percentiles 5, 50, 95).",
                   inputs={**_MC_IN, "mean_block": {"type": "int", "required": False, "default": DEFAULT_MEAN_BLOCK}, "periods_per_year": _PPY_IN},
                   piece_ids=("QS-037b", "QS-059b"))
def _montecarlo_cagr_entry(returns: Any, runs: int = DEFAULT_RUNS, seed: int = DEFAULT_SEED, mean_block: int = DEFAULT_MEAN_BLOCK,
                           periods_per_year: int = 252) -> QuantResult:
    return cagr(returns, runs, seed, mean_block, periods_per_year)


@registry.register("metrics.montecarlo_goal_probability", module="metrics", summary="Probability that a stationary bootstrap path compounds to at least the goal.",
                   inputs={**_MC_IN, "goal": {"type": "float", "required": True}, "mean_block": {"type": "int", "required": False, "default": DEFAULT_MEAN_BLOCK}},
                   piece_ids=("QS-037b", "QS-059b"))
def _montecarlo_goal_entry(returns: Any, goal: float, runs: int = DEFAULT_RUNS, seed: int = DEFAULT_SEED, mean_block: int = DEFAULT_MEAN_BLOCK) -> QuantResult:
    return goal_probability(returns, goal, runs, seed, mean_block)


@registry.register("metrics.montecarlo_summary", module="metrics",
                   summary="One run with every figure: drawdown distribution and bust probability (permutation); Sharpe, CAGR and goal probability (bootstrap).",
                   inputs={**_MC_IN, "bust": {"type": "float", "required": False, "default": None}, "goal": {"type": "float", "required": False, "default": None},
                           "mean_block": {"type": "int", "required": False, "default": DEFAULT_MEAN_BLOCK}, "periods_per_year": _PPY_IN},
                   piece_ids=("QS-059a", "QS-059b"))
def _montecarlo_summary_entry(returns: Any, runs: int = DEFAULT_RUNS, seed: int = DEFAULT_SEED, bust: float | None = None, goal: float | None = None,
                              mean_block: int = DEFAULT_MEAN_BLOCK, periods_per_year: int = 252) -> QuantResult:
    name, pieces, method = "metrics.montecarlo_summary", ("QS-059a", "QS-059b"), "permutation for drawdown; stationary bootstrap for Sharpe, CAGR and goal"
    params = {"runs": runs, "seed": seed, "bust": bust, "goal": goal, "mean_block": mean_block, "periods_per_year": periods_per_year}
    result, reasons = run(returns, runs, seed, bust, goal, mean_block, periods_per_year)
    if result is None:
        return empty(name, "mixed", method, reasons, pieces, params)
    r, _ = prepare_series(returns, min_obs=5)
    return finish(name, result.to_dict(), unit="mixed", method=method, series=r, pieces=pieces, params=params, warnings=reasons)

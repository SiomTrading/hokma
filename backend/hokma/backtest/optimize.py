"""Search of parameters (SPEC-020, section 4.5): grid, TPE with a seed and step by step refinement, with a fast pre-sweep in front.

Every point that is tried, the ones of the pre-sweep and the failed ones included, goes into the trial ledger (`trials.record`): the
deflated Sharpe of any result is computed with `ledger.count`, never with a number that the caller passes. A point that repeats is
answered from a cache and is not a new trial. The pre-sweep (sweep.py) only orders: the K best points are confirmed in the engine under
the fill rules and the official result is always the engine's. The objective is always maximized. Nothing here approves a strategy.

Two entry points. `optimize(spec, space, ...)` is the public one (`hokma.backtest.optimize`): it loads the strategy and the bars of the spec,
runs the points and saves the optimization. `search(...)` is the search itself over any scoring function (`evaluate`, `fast`), which the public
one and the tests start. The module and the function share one name, so the module is callable: `from hokma.backtest import optimize` gives
this module whichever way it is reached, and `optimize(spec, space, ...)` calls the function below. A point is recorded in the ledger by the
search and by nothing else: the engine runs of its points (`run_one`) record no trial."""
from __future__ import annotations

import dataclasses
import itertools
import json
import math
import operator
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import cached_property
from typing import Any, Literal, Protocol

import pandas as pd

from hokma.backtest import _lib
from hokma.backtest._lib import Decide
from hokma.backtest.engine import RunOutput
from hokma.backtest.results import ResultError
from hokma.backtest.spec import BacktestSpec, SpecError
from hokma.backtest.ticks import Bar
from hokma.db import Database
from hokma.quant import limits, metrics, validation
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult

METHODS = ("grid", "tpe", "refine")
OBJECTIVES = ("net_pnl", "mtm_pnl", "sharpe")
MANY_TRIALS = 300                              # B-28: above this many configurations the warning `many_trials` is raised
DEFAULT_TOP_K = 20
REFINE_STEPS = 50
MAX_AXES = 16
MAX_OBJECTIVE_NAME = 40
UNLEARNABLE = -1e12                            # what the sampler is told about a point that is invalid or failed: far below any objective
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_OPS = {"lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge, "ne": operator.ne, "eq": operator.eq}


class TrialFailure(ValueError):
    """One point cannot be run (its strategy does not build or decides badly): a failed trial with this message, never a crash of the search."""

    code = "trial_failed"


class SearchError(ValueError):
    """The search cannot run as asked. `code` is stable: bad_search, bad_space, budget_too_small, ledger_missing, sr_std_needed, bad_run."""

    code = "bad_search"


class BadSpace(SearchError):
    code = "bad_space"


class BudgetTooSmall(SearchError):
    code = "budget_too_small"


class LedgerMissing(SearchError):
    code = "ledger_missing"


RECOVERABLE = (SpecError, ResultError, BadInput, ArithmeticError, TrialFailure)   # a run that fails with one of these is a failed trial, not a crash


# --------------------------------------------------------------------------------------------------------------
# The space
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Axis:
    """One parameter of the search: a lattice `low`, `low + step`, ... up to `high` (int or float) or a list of choices."""
    name: str
    kind: Literal["int", "float", "choice"] = "int"
    low: float | None = None
    high: float | None = None
    step: float | None = None
    choices: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise BadSpace(f"parameter name {self.name!r} must be lower case letters, digits and underscores")
        if self.kind == "choice":
            if not self.choices or len(set(self.choices)) != len(self.choices) or any(isinstance(c, (list, dict, set, tuple)) for c in self.choices):
                raise BadSpace(f"parameter {self.name}: a choice needs a list of different plain values")
            if any(v is not None for v in (self.low, self.high, self.step)):
                raise BadSpace(f"parameter {self.name}: a choice has no range")
            return
        if self.kind not in ("int", "float") or self.low is None or self.high is None:
            raise BadSpace(f"parameter {self.name}: a range needs kind int or float, low and high")
        if self.kind == "int":
            try:
                low, high, step = (limits.as_int(self.name, v) for v in (self.low, self.high, 1 if self.step is None else self.step))
            except BadInput as exc:
                raise BadSpace(f"parameter {self.name}: {exc}") from None
        else:
            if self.step is None:
                raise BadSpace(f"parameter {self.name}: a float range needs a step")
            low, high, step = float(self.low), float(self.high), float(self.step)
            if not all(math.isfinite(v) for v in (low, high, step)):
                raise BadSpace(f"parameter {self.name}: the range must be finite")
        if step <= 0 or low > high:
            raise BadSpace(f"parameter {self.name}: low must not be above high and the step is above zero")
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "step", step)
        limits.check_param("n_combinations", int(math.floor((high - low) / step + 1e-9)) + 1)

    @cached_property
    def values(self) -> tuple[Any, ...]:
        if self.kind == "choice":
            return tuple(self.choices)
        count = int(math.floor((self.high - self.low) / self.step + 1e-9)) + 1
        if self.kind == "int":
            return tuple(int(self.low) + i * int(self.step) for i in range(count))
        return tuple(round(self.low + i * self.step, 10) for i in range(count))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "low": self.low, "high": self.high, "step": self.step, "choices": list(self.choices)}


@dataclass(frozen=True)
class Constraint:
    """A rule between two parameters, or a parameter and a number, checked before a point runs: `Constraint("fast", "lt", "slow")`.
    Rules are data, never code that an agent writes."""
    left: str
    op: Literal["lt", "le", "gt", "ge", "ne", "eq"]
    right: str | float | int

    def __post_init__(self) -> None:
        if self.op not in _OPS:
            raise BadSpace(f"constraint operator {self.op!r} is not one of {', '.join(_OPS)}")
        if isinstance(self.right, bool) or not isinstance(self.right, (str, int, float)):
            raise BadSpace("a constraint compares with a parameter name or a number")

    def holds(self, params: Mapping[str, Any]) -> bool:
        other = params[self.right] if isinstance(self.right, str) else self.right
        return bool(_OPS[self.op](params[self.left], other))

    def to_dict(self) -> dict[str, Any]:
        return {"left": self.left, "op": self.op, "right": self.right}


@dataclass(frozen=True)
class ParamSpace:
    axes: tuple[Axis, ...]
    constraints: tuple[Constraint, ...] = ()

    def __post_init__(self) -> None:
        names = [a.name for a in self.axes]
        if not names:
            raise BadSpace("the space is empty: it needs at least one parameter")
        if len(set(names)) != len(names) or len(names) > MAX_AXES:
            raise BadSpace(f"a parameter appears twice or there are more than {MAX_AXES}")
        for c in self.constraints:
            if c.left not in names or (isinstance(c.right, str) and c.right not in names):
                raise BadSpace(f"constraint {c.left} {c.op} {c.right}: it names a parameter that is not in the space")
        limits.check_param("n_combinations", self.size)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(a.name for a in self.axes)

    @property
    def size(self) -> int:
        """The points of the lattice, before the constraints."""
        total = 1
        for a in self.axes:
            total *= len(a.values)
            if total > 10 * limits.PARAM_CAPS["n_combinations"]:
                break
        return total

    def valid(self, params: Mapping[str, Any]) -> bool:
        return all(c.holds(params) for c in self.constraints)

    def combinations(self) -> list[dict[str, Any]]:
        """Every valid point in a fixed order (the first parameter changes slowest). The constraints cut the invalid ones here, before any run."""
        points = [dict(zip(self.names, combo)) for combo in itertools.product(*(a.values for a in self.axes))]
        points = [p for p in points if self.valid(p)]
        if not points:
            raise BadSpace("the constraints leave no valid combination")
        return points

    def key(self, params: Mapping[str, Any]) -> tuple[Any, ...]:
        return tuple(params[a.name] for a in self.axes)

    def to_dict(self) -> dict[str, Any]:
        return {"axes": [a.to_dict() for a in self.axes], "constraints": [c.to_dict() for c in self.constraints]}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "ParamSpace":
        return ParamSpace(tuple(Axis(a["name"], a["kind"], a["low"], a["high"], a["step"], tuple(a["choices"])) for a in data["axes"]),
                          tuple(Constraint(c["left"], c["op"], c["right"]) for c in data["constraints"]))


def trial_warning(n_configurations: int) -> tuple[str, ...]:
    """B-28: `many_trials` when a search tries more than 300 configurations. The warning does not stop anything: the brake is the deflated
    Sharpe, which uses the number of trials of the ledger."""
    n = limits.as_int("n_configurations", n_configurations)
    return (f"many_trials:{n}",) if n > MANY_TRIALS else ()


def edge_of_space(space: ParamSpace, params: Mapping[str, Any]) -> tuple[str, ...]:
    """The parameters whose value sits on the border of their range: an optimum there says the range was too short, not that it was found.
    Only ranges of three values or more are judged (two values are always borders)."""
    return tuple(a.name for a in space.axes if a.kind != "choice" and len(a.values) >= 3 and params[a.name] in (a.values[0], a.values[-1]))


# --------------------------------------------------------------------------------------------------------------
# Trials and the ledger
# --------------------------------------------------------------------------------------------------------------
class Ledger(Protocol):
    """`hokma.backtest.trials` has this shape (spec 4.6); a test passes a fake."""

    def record(self, strategy_id: str, hypothesis_id: str, run_id: int | None, params: Mapping[str, Any], status: str, objective: float | None, *,
               error: str | None = None) -> int: ...

    def count(self, strategy_id: str, *, hypothesis_id: str | None = None) -> int: ...


def resolve_ledger(ledger: Ledger | None) -> Ledger:
    """The ledger that was passed, or the trial ledger of the service (`hokma.backtest.trials`)."""
    if ledger is not None:
        return ledger
    try:
        from hokma.backtest import trials
    except ImportError as exc:
        raise LedgerMissing("ledger_missing: the trial ledger (hokma.backtest.trials) is not available, so no search can be counted") from exc
    return trials


@dataclass(frozen=True)
class Evaluation:
    objective: float | None
    run_id: int | None = None          # the saved run of the point when the evaluation went through `runner.run`: that run is already one trial of the ledger


@dataclass(frozen=True)
class Trial:
    params: Mapping[str, Any]
    status: Literal["done", "failed"]
    objective: float | None
    run_id: int | None = None
    source: Literal["fast", "engine"] = "engine"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"params": dict(self.params), "status": self.status, "objective": self.objective, "run_id": self.run_id, "source": self.source,
                "error": self.error}


Score = Callable[[Mapping[str, Any]], "Evaluation | float | None"]


def failure_reasons(trials: Sequence[Trial]) -> list[tuple[str, int]]:
    """The reasons of the failed trials with how many times each happened, the most frequent first (a tie keeps the order of the search)."""
    counts: dict[str, int] = {}
    for trial in trials:
        if trial.status == "failed":
            counts[trial.error or "unknown"] = counts.get(trial.error or "unknown", 0) + 1
    return sorted(counts.items(), key=lambda item: -item[1])


class _Book:
    """The trials of one stage of a search: each new point is scored once, recorded in the ledger, and a repeat is answered from the cache."""

    def __init__(self, ledger: Ledger, space: ParamSpace, strategy_id: str, hypothesis_id: str, source: Literal["fast", "engine"]) -> None:
        self.ledger, self.space, self.strategy_id, self.hypothesis_id, self.source = ledger, space, strategy_id, hypothesis_id, source
        self.trials: list[Trial] = []
        self.cache: dict[tuple[Any, ...], Trial] = {}
        self.hits = 0

    def known(self, params: Mapping[str, Any]) -> Trial | None:
        return self.cache.get(self.space.key(params))

    def run(self, params: Mapping[str, Any], score: Score) -> Trial:
        key = self.space.key(params)
        if key in self.cache:
            self.hits += 1
            return self.cache[key]
        point = {name: params[name] for name in self.space.names}
        run_id: int | None = None
        value: float | None = None
        error: str | None = None
        try:
            out = score(dict(point))
            ev = out if isinstance(out, Evaluation) else Evaluation(out)
            run_id = ev.run_id
            if ev.objective is None or not math.isfinite(float(ev.objective)):
                error = "no_objective"
            else:
                value = float(ev.objective)
        except RECOVERABLE as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
        trial = Trial(point, "failed" if error else "done", value, run_id, self.source, error)
        if run_id is None:                                  # an evaluation that names its run came through `runner.run`, which already recorded this trial
            self.ledger.record(self.strategy_id, self.hypothesis_id, run_id, point, trial.status, value, error=error)
        self.trials.append(trial)
        self.cache[key] = trial
        return trial

    def best(self) -> Trial | None:
        done = [t for t in self.trials if t.objective is not None]
        return max(done, key=lambda t: t.objective) if done else None      # max keeps the first of equal values


@dataclass(frozen=True)
class OptimizationResult:
    strategy_id: str
    hypothesis_id: str
    method: str
    objective: str
    space: ParamSpace
    trials: tuple[Trial, ...]
    best: Trial | None                    # the best trial CONFIRMED in the engine (or the best of an engine search): the official result
    n_trials: int                         # the count of the ledger after the search: the one the deflated Sharpe uses
    cache_hits: int
    edge_of_space: tuple[str, ...]
    warnings: tuple[str, ...]
    presweep: bool
    seed: int
    run_id: int | None = None             # the row of backtest_runs that `save` wrote (None until it is saved)

    def to_dict(self) -> dict[str, Any]:
        best = None if self.best is None else self.trials.index(self.best)
        return {"strategy_id": self.strategy_id, "hypothesis_id": self.hypothesis_id, "method": self.method, "objective": self.objective,
                "space": self.space.to_dict(), "trials": [t.to_dict() for t in self.trials], "best": best, "n_trials": self.n_trials,
                "cache_hits": self.cache_hits, "edge_of_space": list(self.edge_of_space), "warnings": list(self.warnings), "presweep": self.presweep,
                "seed": self.seed}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "OptimizationResult":
        trials = tuple(Trial(t["params"], t["status"], t["objective"], t["run_id"], t["source"], t["error"]) for t in data["trials"])
        return OptimizationResult(data["strategy_id"], data["hypothesis_id"], data["method"], data["objective"], ParamSpace.from_dict(data["space"]), trials,
                                  None if data["best"] is None else trials[data["best"]], data["n_trials"], data["cache_hits"], tuple(data["edge_of_space"]),
                                  tuple(data["warnings"]), data["presweep"], data["seed"])


# --------------------------------------------------------------------------------------------------------------
# The three searches
# --------------------------------------------------------------------------------------------------------------
def _tpe(space: ParamSpace, book: _Book, score: Score, n_trials: int, seed: int) -> int:
    """TPE with a seed: `n_trials` proposals. A repeated point is a cache hit that still counts as a proposal. Returns the points that the
    constraints rejected before running (the sampler is told they are bad so it learns to avoid them)."""
    axes = [_lib.SearchAxis(a.name, a.kind, a.low, a.values[-1] if a.kind != "choice" else None, a.step, a.choices) for a in space.axes]   # the last value of a lattice may be below high
    search = _lib.TpeSearch(axes, seed)
    proposals = attempts = rejected = 0
    while proposals < n_trials and attempts < 20 * n_trials:
        attempts += 1
        point = search.ask()
        if not space.valid(point):
            rejected += 1
            search.tell(UNLEARNABLE)
            continue
        trial = book.run(point, score)
        proposals += 1
        search.tell(UNLEARNABLE if trial.objective is None else trial.objective)
    return rejected


def _refine(space: ParamSpace, book: _Book, score: Score, n_trials: int, steps: int) -> None:
    """A coarse grid, then moves around the best point: each step tries the neighbours one stride away along each axis, moves to the best one
    if it improves and halves the stride when none does. It stops at stride one with no improvement, at `steps` or at `n_trials` points."""
    limits.check_param("refine_steps", steps)
    axes = space.axes
    sizes = [len(a.values) for a in axes]
    numeric = [i for i, a in enumerate(axes) if a.kind != "choice"]
    choice_points = math.prod(sizes[i] for i, a in enumerate(axes) if a.kind == "choice")
    per = 5
    while per > 1 and per ** len(numeric) * choice_points > max(1, n_trials // 3):
        per -= 1
    marks: list[list[int]] = []
    for i, a in enumerate(axes):
        if a.kind == "choice":
            marks.append(list(range(sizes[i])))
        elif per == 1:
            marks.append([sizes[i] // 2])
        else:
            marks.append(sorted({int(round(k * (sizes[i] - 1) / (per - 1))) for k in range(per)}))
    at: dict[tuple[Any, ...], tuple[int, ...]] = {}

    def point(idx: Sequence[int]) -> dict[str, Any]:
        return {a.name: a.values[idx[i]] for i, a in enumerate(axes)}

    def evaluate(idx: tuple[int, ...]) -> Trial | None:
        p = point(idx)
        if not space.valid(p):
            return None
        if book.known(p) is None and len(book.trials) >= n_trials:
            return None
        at[space.key(p)] = idx
        return book.run(p, score)

    for idx in itertools.product(*marks):
        evaluate(idx)
    best = book.best()
    if best is None:
        return
    here, value = at[space.key(best.params)], best.objective
    stride = max([max(1, (sizes[i] - 1) // (per - 1 if per > 1 else 2)) for i in numeric], default=1)
    for _ in range(steps):
        found: tuple[tuple[int, ...], float] | None = None
        for i, a in enumerate(axes):
            moves = [j for j in range(sizes[i]) if j != here[i]] if a.kind == "choice" else \
                sorted({min(max(here[i] + d, 0), sizes[i] - 1) for d in (-stride, stride)} - {here[i]})
            for j in moves:
                idx = here[:i] + (j,) + here[i + 1:]
                trial = evaluate(idx)
                if trial is not None and trial.objective is not None and trial.objective > (found[1] if found else value):
                    found = (idx, trial.objective)
        if found is not None:
            here, value = found
        elif stride == 1:
            break
        else:
            stride = max(1, stride // 2)
        if len(book.trials) >= n_trials:
            break


def search(spec: BacktestSpec, space: ParamSpace, *, method: Literal["grid", "tpe", "refine"], n_trials: int, objective: str, evaluate: Score,
           presweep: bool = True, fast: Callable[[Sequence[Mapping[str, Any]]], Sequence[float | None]] | None = None,
           hypothesis_id: str | None = None, ledger: Ledger | None = None, top_k: int = DEFAULT_TOP_K, steps: int = REFINE_STEPS) -> OptimizationResult:
    """Search `space` for the best `objective` (maximized). `evaluate(params)` is one engine run; `fast(points)` is the pre-sweep, one score per
    point. With the pre-sweep, the search runs on the fast scores and the `top_k` best points are confirmed with `evaluate`. A grid needs
    `n_trials` of at least its number of valid points: a grid is never cut short without saying so."""
    if method not in METHODS:
        raise SearchError(f"method must be one of {', '.join(METHODS)}")
    if not isinstance(objective, str) or not 1 <= len(objective) <= MAX_OBJECTIVE_NAME:
        raise SearchError(f"the objective is a name of up to {MAX_OBJECTIVE_NAME} characters")
    n = limits.as_int("n_trials", n_trials)
    limits.check_param("n_combinations" if method == "grid" else "opt_trials", n)
    limits.check_param("top_k", top_k)
    if n < 1 or top_k < 1:
        raise SearchError("n_trials and top_k are at least 1")
    ledger = resolve_ledger(ledger)
    hypothesis = hypothesis_id or spec.strategy_id
    points = space.combinations() if method == "grid" else None
    if points is not None and len(points) > n:
        raise BudgetTooSmall(f"budget_too_small: the grid has {len(points)} valid points and n_trials is {n}")
    use_fast = bool(presweep and fast is not None)
    warnings: list[str] = [] if use_fast or not presweep else ["presweep_not_available"]
    first = _Book(ledger, space, spec.strategy_id, hypothesis, "fast" if use_fast else "engine")
    score: Score = evaluate
    if use_fast:
        if points is not None:
            scores = dict(zip((space.key(p) for p in points), fast(points)))
            score = lambda p: scores[space.key(p)]                                # noqa: E731 - the batch was scored once, in one pass
        else:
            score = lambda p: fast([p])[0]                                         # noqa: E731
    rejected = 0
    if method == "grid":
        for p in points:
            first.run(p, score)
    elif method == "tpe":
        rejected = _tpe(space, first, score, n, spec.seed)
    else:
        _refine(space, first, score, n, steps)
    trials = list(first.trials)
    hits = first.hits
    official = first
    if use_fast:
        ranked = sorted((t for t in first.trials if t.objective is not None), key=lambda t: -t.objective)[:top_k]   # stable: ties keep the search order
        official = _Book(ledger, space, spec.strategy_id, hypothesis, "engine")
        for t in ranked:
            official.run(t.params, evaluate)
        trials += official.trials
        hits += official.hits
        warnings.append(f"presweep_top_k:{len(ranked)}")
    best = official.best()
    warnings += trial_warning(len(first.trials))
    if rejected:
        warnings.append(f"constraint_rejections:{rejected}")
    failed = sum(t.status == "failed" for t in trials)
    if failed:
        warnings.append(f"failed_trials:{failed}")
        reason, times = failure_reasons(trials)[0]
        warnings.append(f"failed_reason:{reason} (x{times})")
    edge = () if best is None else edge_of_space(space, best.params)
    warnings += [f"edge_of_space:{name}" for name in edge]
    if best is None:
        warnings.append("no_valid_trial")
    return OptimizationResult(spec.strategy_id, hypothesis, method, objective, space, tuple(trials), best, ledger.count(spec.strategy_id), hits, edge,
                              tuple(warnings), use_fast, spec.seed)


def refine(space: ParamSpace, *, strategy_id: str, hypothesis_id: str, evaluate: Score, objective: str, n_trials: int, ledger: Ledger | None = None,
           steps: int = REFINE_STEPS) -> OptimizationResult:
    """L-41: the coarse grid and the steps around the best point, on any function `evaluate` (the engine, or a test function with a known
    maximum). Every point counts in the ledger."""
    n = limits.as_int("n_trials", n_trials)
    limits.check_param("opt_trials", n)
    if n < 1:
        raise SearchError("n_trials is at least 1")
    ledger = resolve_ledger(ledger)
    book = _Book(ledger, space, strategy_id, hypothesis_id, "engine")
    _refine(space, book, evaluate, n, steps)
    best = book.best()
    edge = () if best is None else edge_of_space(space, best.params)
    warnings = trial_warning(len(book.trials)) + tuple(f"edge_of_space:{name}" for name in edge) + (() if best else ("no_valid_trial",))
    return OptimizationResult(strategy_id, hypothesis_id, "refine", objective, space, tuple(book.trials), best, ledger.count(strategy_id), book.hits, edge,
                              warnings, False, 0)


def deflated(result: OptimizationResult, returns: Any, *, sr_std: float | None = None) -> QuantResult:
    """The deflated Sharpe ratio of the winner (`quant.validation.dsr`) with the `n_trials` of the ledger. The caller gives the spread of
    the Sharpe of the tried configurations, in the units of `returns`; the number of trials is never an argument."""
    if sr_std is None:
        raise SearchError("sr_std_needed: the deflated Sharpe ratio needs the spread of the Sharpe of the configurations that were tried")
    return validation.dsr(returns, n_trials=result.n_trials, sr_std=sr_std)


# --------------------------------------------------------------------------------------------------------------
# One engine run per point
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class EngineStrategy:
    """What the engine needs to run one point: the decision function and the instrument."""
    decide: Decide
    tick_size: Decimal
    multiplier: Decimal
    symbol: str
    protective_orders: bool = False
    asset_type: str = "future"


def run_one(spec: BacktestSpec, strategy: EngineStrategy, bars: Sequence[Bar]) -> RunOutput:
    """The ONE place where an optimization starts an engine run: `runner.run_with`, the start that the runner shares with it. It saves nothing and
    records no trial: the search records each point once. The runner is imported on use because it pulls in the factory and the data feed."""
    from hokma.backtest import runner
    return runner.run_with(spec, strategy, bars)


def engine_objective(output: RunOutput, spec: BacktestSpec, bars: Sequence[Bar], name: str) -> float | None:
    """The objective of a run: `net_pnl` (realized), `mtm_pnl`, or `sharpe` of the daily result (from `quant.metrics`, never the engine).
    None when it cannot be computed, for example a Sharpe with no trades."""
    if name == "net_pnl":
        return float(output.realized_pnl_usd)
    if name == "mtm_pnl":
        return float(output.mtm_pnl_usd)
    if name != "sharpe":
        raise SearchError(f"the engine objective is one of {', '.join(OBJECTIVES)}, not {name!r}")
    days = pd.bdate_range(bars[0].ts.date(), bars[-1].ts.date())
    daily = pd.Series(0.0, index=days)
    for t in output.trades:
        day = pd.Timestamp(t.exit_at.date())
        if day in daily.index:
            daily[day] += float(t.pnl_usd) / float(spec.capital_usd)      # a trade of the engine is net of its costs
    value = metrics.compute(daily, ["sharpe"])["sharpe"].value
    return None if value is None else float(value)


def engine_evaluator(spec: BacktestSpec, build: Callable[[Mapping[str, Any]], EngineStrategy], bars: Sequence[Bar], objective: str) -> Score:
    """`evaluate` for `optimize`: one engine run of `spec` with the point as its parameters."""
    if objective not in OBJECTIVES:
        raise SearchError(f"the engine objective is one of {', '.join(OBJECTIVES)}, not {objective!r}")

    def evaluate(params: Mapping[str, Any]) -> Evaluation:
        from hokma.backtest.runner import StageError

        point = dataclasses.replace(spec, params={**spec.params, **dict(params)})
        try:
            strategy = build(params)
            output = run_one(point, strategy, bars)
        except StageError as error:
            raise TrialFailure(str(error)) from error
        failure = getattr(strategy, "failure", None)             # a strategy that raised while the engine ran: the output is not the run of that strategy
        if failure is not None:
            raise TrialFailure(str(failure))
        return Evaluation(engine_objective(output, point, bars, objective))
    return evaluate


# --------------------------------------------------------------------------------------------------------------
# Saved optimizations
# --------------------------------------------------------------------------------------------------------------
def save(db: Database, spec: BacktestSpec, result: OptimizationResult, *, engine_version: str) -> int:
    """Store the optimization as a row of `backtest_runs` (kind optimize) with its trials in `summary_json`, and return its id."""
    from hokma.backtest import runner

    return runner.save_row(db, "optimize", spec, result.to_dict(), engine_version=engine_version)


def load(db: Database, opt_id: int) -> OptimizationResult:
    with db.read() as u:
        row = u.session.one("SELECT kind, status, summary_json FROM backtest_runs WHERE id = :i", {"i": int(opt_id)})
    if row is None or row["kind"] != "optimize" or row["status"] != "done" or not row["summary_json"]:
        raise SearchError(f"bad_run: {opt_id} is not a finished optimization")
    return OptimizationResult.from_dict(json.loads(row["summary_json"]))


# --------------------------------------------------------------------------------------------------------------
# The public entry point (spec 4.2)
# --------------------------------------------------------------------------------------------------------------
class DbLedger:
    """The trial ledger of the service on one database: `trials.record` and `trials.count` with the `db` of the caller."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, strategy_id: str, hypothesis_id: str, run_id: int | None, params: Mapping[str, Any], status: str, objective: float | None, *,
               error: str | None = None) -> int:
        return resolve_ledger(None).record(strategy_id, hypothesis_id, run_id, params, status, objective, error=error, db=self.db)   # type: ignore[call-arg]

    def count(self, strategy_id: str, *, hypothesis_id: str | None = None) -> int:
        return resolve_ledger(None).count(strategy_id, hypothesis_id=hypothesis_id, db=self.db)                                  # type: ignore[call-arg]


FAST_METRIC = {"net_pnl": "net_pnl", "mtm_pnl": "mtm_pnl"}      # what the pre-sweep ranks by, for each objective that it can stand for (a Sharpe has none)


def sweep_points(spec: BacktestSpec, services: Any, data: Any, points: Sequence[Mapping[str, Any]], *, per_bar: bool = False) -> tuple[Any, dict[int, str]]:
    """The fast sweep of `points` of a strategy without protective orders: its signal vector for each point (`runner.wish_signals`, the decisions of
    the strategy with no engine) filled in one pass by `sweep.signal_sweep`. Returns the sweep and, by position, the message of each point whose
    strategy failed (its signal is zero in the sweep and its score must be read as None)."""
    from hokma.backtest import runner, sweep
    from hokma.backtest.runner import StageError

    bars, sheet = data.bars, data.sheet
    costs = sweep.sweep_costs(spec.costs, tick_size=sheet.tick_size, multiplier=sheet.multiplier, price=bars[0].open, asset_type=sheet.asset_type)
    failed: dict[int, str] = {}
    counter = [0]

    def builder(_arrays: Any, params: Mapping[str, Any]) -> Any:
        at, counter[0] = counter[0], counter[0] + 1
        try:
            return runner.wish_signals(services.driver(spec, data, params))
        except (StageError, SpecError) as error:
            failed[at] = str(error)[:300]
            return [0] * len(bars)                                  # a zero signal keeps the batch whole
    return sweep.signal_sweep(bars, builder, list(points), costs, per_bar=per_bar), failed


def fast_scorer(spec: BacktestSpec, services: Any, data: Any, objective: str) -> Callable[[Sequence[Mapping[str, Any]]], Sequence[float | None]] | None:
    """The pre-sweep of a strategy: its signal vector for each point (`runner.wish_signals`, the decisions of the strategy with no engine) filled
    in one pass by `sweep`. None when it cannot stand for the engine run: the objective is a Sharpe, or the strategy uses stops or targets, or does not
    build. A point whose strategy fails scores None, which the search records as a failed trial."""
    from hokma.backtest.runner import StageError

    metric = FAST_METRIC.get(objective)
    if metric is None:
        return None
    try:
        if services.driver(spec, data).protective_orders:
            return None
    except StageError:
        return None

    def fast(points: Sequence[Mapping[str, Any]]) -> Sequence[float | None]:
        swept, failed = sweep_points(spec, services, data, points)
        return [None if i in failed else float(v) for i, v in enumerate(swept.scores(metric))]
    return fast


def optimize(spec: BacktestSpec, space: ParamSpace, *, method: Literal["grid", "tpe", "refine"], n_trials: int, objective: str, presweep: bool = True,
             services: Any = None) -> OptimizationResult:
    """Search `space` for the best `objective` of the strategy of `spec` (spec 4.2). The strategy comes from the registry by `spec.strategy_id`
    (or `services.strategy`) and the bars from the data feed (or `services.source`); every point of the space goes over the parameters of the spec.
    `objective` is `net_pnl`, `mtm_pnl` or `sharpe`, maximized. With `presweep` the points are ordered by the fast sweep when the strategy allows it and
    only the best K reach the engine; the official figure is always the engine's. Each executed point is one row of the trial ledger, once: the engine
    run of a point records nothing itself. The optimization is saved as a row of `backtest_runs` (`run_id` of the result). Answers
    `engine_unavailable` when the engine cannot run here, and a `StageError` of the data stage when the bars cannot be loaded."""
    from hokma.backtest import engine, runner

    if objective not in OBJECTIVES:
        raise SearchError(f"the objective is one of {', '.join(OBJECTIVES)}, not {objective!r}")
    _lib.require()
    services = services or runner.Services()
    db = services.database()
    data = services.load(spec)
    evaluate = engine_evaluator(spec, lambda params: services.driver(spec, data, params), data.bars, objective)
    fast = fast_scorer(spec, services, data, objective) if presweep else None
    result = search(spec, space, method=method, n_trials=n_trials, objective=objective, evaluate=evaluate, presweep=presweep, fast=fast,
                    ledger=DbLedger(db))
    version = engine.status().get("engine_version") or "unknown"
    return dataclasses.replace(result, run_id=save(db, spec, result, engine_version=version))


from hokma.data.sentinels import make_module_callable  # noqa: E402 - last: the module answers the call of its function (see the docstring)

make_module_callable(__name__, "optimize")

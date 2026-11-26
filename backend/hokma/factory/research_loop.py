"""The research loop of Ian (SPEC-020: SKL-008, CTS-006): one change per experiment, a judge that never moves, keep or discard.

The judge is an out of sample backtest whose whole configuration (the spec without its parameters, the objective and the end of the training data)
is frozen when the loop starts and hashed. The loop builds every experiment from that frozen spec; an evaluator with another hash, or a run that
answers for another spec, is `evaluator_changed`. Each experiment is one change to the parameters that were kept last, is scored by the judge and is
kept only if it beats the best so far; either way it is written to the ledger of trials (`backtest_trials`, append only) with its decision, so the
deflated Sharpe ratio counts it. Discarding is just not moving the kept parameters: nothing is undone, there is no repository and no reset.

The loop has no path to a desk, a broker or an order: it only calls the function that scores a spec. `detect_stall` reads the history and says when more
tuning is pointless; `pivot_proposals` then names changes of skeleton instead.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal, Protocol

from hokma.backtest.spec import BacktestSpec
from hokma.db import Database
from hokma.desks.core.hashing import content_hash
from hokma.factory.params import ParamSchema, validate
from hokma.factory.researcher import EvaluatorChanged, ResearchError
from hokma.quant import limits

DEFAULT_MAX_EXPERIMENTS = 100
MAX_OBJECTIVE_NAME = 60
KEEP, DISCARD = "keep", "discard"
STALL_KINDS = ("tail_risk", "costs_kill_edge", "overfit", "low_profit_factor", "plateau")
PIVOT_KINDS = ("invert_assumption", "change_archetype", "redefine_objective")


class LoopError(ResearchError):
    """A loop rule was broken. `code` is stable: bad_evaluator, one_change_only, bad_change, loop_stalled, loop_budget_spent."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------------------------------- the judge
@dataclass(frozen=True)
class Evaluator:
    """The fixed judge: the spec of an out of sample run, what to maximize and where the training data ends."""

    spec: BacktestSpec
    objective: str
    train_end: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.objective, str) or not 1 <= len(self.objective) <= MAX_OBJECTIVE_NAME:
            raise LoopError("bad_evaluator", f"the objective is a name of up to {MAX_OBJECTIVE_NAME} characters")
        if not isinstance(self.train_end, datetime) or self.train_end.tzinfo is None:
            raise LoopError("bad_evaluator", "train_end is a datetime with a time zone")
        if self.spec.start < self.train_end:
            raise LoopError("bad_evaluator", "the judge runs out of sample: its spec must start at or after the end of the training data")

    @property
    def hash(self) -> str:
        frame = {k: v for k, v in self.spec.canonical().items() if k != "params"}
        return content_hash({"spec": frame, "objective": self.objective, "train_end": self.train_end.isoformat()})


@dataclass(frozen=True)
class LoopEvaluation:
    """What the judge says about one spec. `spec_hash`, when given, is the fingerprint of the spec that was really run."""

    objective: float | None
    run_id: int | None = None
    in_sample_objective: float | None = None
    profit_factor: float | None = None
    max_drawdown: float | None = None
    warnings: tuple[str, ...] = ()
    spec_hash: str | None = None


Score = Callable[[BacktestSpec], "LoopEvaluation | float | None"]


class Ledger(Protocol):
    def record(self, strategy_id: str, hypothesis_id: str, run_id: int | None, params: Mapping[str, Any], status: str, objective: float | None, *,
               error: str | None = None) -> int: ...


class _TrialLedger:
    """`hokma.backtest.trials`, loaded when it is first used."""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db

    def record(self, strategy_id: str, hypothesis_id: str, run_id: int | None, params: Mapping[str, Any], status: str, objective: float | None, *,
               error: str | None = None) -> int:
        from hokma.backtest import trials

        return trials.record(strategy_id, hypothesis_id, run_id, params, status, objective, error=error, db=self.db)


@dataclass(frozen=True)
class Experiment:
    n: int
    change: Mapping[str, Any]
    params: Mapping[str, Any]
    evaluation: LoopEvaluation
    status: Literal["done", "failed"]
    decision: Literal["keep", "discard"]
    trial_id: int
    error: str | None = None

    @property
    def objective(self) -> float | None:
        return self.evaluation.objective


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


# ---------------------------------------------------------------------------------------------------- SKL-008
class ResearchLoop:
    """One loop of one hypothesis. `evaluate(spec)` scores a spec and must not write a trial itself: the loop writes it, with the decision."""

    def __init__(self, evaluator: Evaluator, evaluate: Score, *, hypothesis_id: str, params: Mapping[str, Any], schema: ParamSchema | None = None,
                 min_gain: float = 0.0, max_experiments: int = DEFAULT_MAX_EXPERIMENTS, ledger: Ledger | None = None,
                 limits_of_stall: "StallLimits | None" = None, db: Database | None = None) -> None:
        limits.check_param("opt_trials", max_experiments)
        if max_experiments < 1 or not math.isfinite(min_gain) or min_gain < 0:
            raise LoopError("bad_change", "max_experiments is at least 1 and min_gain is zero or more")
        self._evaluator, self._frozen = evaluator, evaluator.hash
        self._evaluate, self.hypothesis_id, self._schema = evaluate, hypothesis_id, schema
        self._kept = dict(validate(schema, params) if schema else params)
        self._best: float | None = None
        self._min_gain, self._max = min_gain, max_experiments
        self._ledger: Ledger = ledger or _TrialLedger(db)
        self._stall_limits = limits_of_stall or StallLimits()
        self.history: list[Experiment] = []

    @property
    def evaluator_hash(self) -> str:
        return self._frozen

    @property
    def kept_params(self) -> dict[str, Any]:
        return dict(self._kept)

    @property
    def best_objective(self) -> float | None:
        return self._best

    def baseline(self) -> Experiment:
        """The starting parameters, scored once by the judge. It is the first experiment and the number to beat."""
        if self.history:
            raise LoopError("one_change_only", "the baseline is the first experiment")
        return self._run({}, dict(self._kept))

    def experiment(self, change: Mapping[str, Any], *, evaluator: Evaluator | None = None) -> Experiment:
        """One change to the kept parameters, scored by the fixed judge. `evaluator` is for a caller that holds its own copy: a different one fails."""
        if evaluator is not None and evaluator.hash != self._frozen:
            raise EvaluatorChanged("the evaluator changed in the middle of the loop: the judge is fixed, start a new loop to change it")
        if not self.history:
            raise LoopError("bad_change", "run the baseline first: there is nothing to compare an experiment with")
        if not isinstance(change, Mapping) or len(change) != 1:
            raise LoopError("one_change_only", "an experiment changes exactly one parameter")
        (name, value), = change.items()
        if name not in self._kept:
            raise LoopError("bad_change", f"{name!r} is not a parameter of this strategy")
        if value == self._kept[name]:
            raise LoopError("bad_change", f"{name} already has the value {value!r}: that is not a change")
        stall = detect_stall(self.history, self._stall_limits)
        if stall is not None:
            raise LoopError("loop_stalled", f"the loop stalled ({stall}): no more parameter experiments; change the skeleton instead, e.g. "
                            + "; ".join(p.change for p in pivot_proposals(stall)))
        merged = {**self._kept, name: value}
        if self._schema is not None:
            merged = dict(validate(self._schema, merged))
        return self._run({name: merged[name]}, merged)

    def _run(self, change: Mapping[str, Any], params: dict[str, Any]) -> Experiment:
        if len(self.history) >= self._max:
            raise LoopError("loop_budget_spent", f"the loop has used its {self._max} experiments")
        spec = replace(self._evaluator.spec, params=params)
        expected = spec.fingerprint()
        error: str | None = None
        try:
            out = self._evaluate(spec)
        except (ValueError, ArithmeticError, LookupError) as exc:
            out, error = None, f"{type(exc).__name__}: {exc}"[:300]
        ev = out if isinstance(out, LoopEvaluation) else LoopEvaluation(_finite(out))
        if ev.spec_hash is not None and ev.spec_hash != expected:
            raise EvaluatorChanged("the run answered for another spec than the one the fixed judge built: the evaluator changed")
        objective = _finite(ev.objective)
        if objective is None and error is None:
            error = "no_objective"
        status: Literal["done", "failed"] = "failed" if objective is None else "done"
        keep = objective is not None and (self._best is None or objective > self._best + self._min_gain)
        decision: Literal["keep", "discard"] = KEEP if keep else DISCARD
        n = len(self.history) + 1
        trial_id = self._ledger.record(self._evaluator.spec.strategy_id, self.hypothesis_id, ev.run_id,
                                       {**params, "_decision": decision, "_experiment": n, "_evaluator": self._frozen[:16]}, status, objective, error=error)
        if keep:
            self._kept, self._best = dict(params), objective
        experiment = Experiment(n, dict(change), dict(params), replace(ev, objective=objective), status, decision, trial_id, error)
        self.history.append(experiment)
        return experiment

    def review(self) -> "Review":
        """The stall of the history, if any, and what to do about it."""
        stall = detect_stall(self.history, self._stall_limits)
        return Review(stall, () if stall is None else pivot_proposals(stall))


# ---------------------------------------------------------------------------------------------------- CTS-006
@dataclass(frozen=True)
class StallLimits:
    """Where a history counts as stalled. These are starting points for the owner to adjust, not laws of nature; the objective is a Sharpe ratio by default."""

    window: int = 10                     # experiments that are read
    plateau_gain: float = 0.05           # the best of the second half beats the best of the first half by less than this
    overfit_gap: float = 0.5             # mean in sample objective above mean out of sample objective by more than this
    min_profit_factor: float = 1.2       # the best experiment has a profit factor below this
    max_drawdown: float = 0.30           # the best experiment draws down more than this share of the capital
    costs_warning: str = "costs_kill_edge"


@dataclass(frozen=True)
class Pivot:
    change: Literal["invert_assumption", "change_archetype", "redefine_objective"]
    idea: str


@dataclass(frozen=True)
class Review:
    stall: str | None
    proposals: tuple[Pivot, ...]


def detect_stall(history: Sequence[Experiment], limits_of_stall: StallLimits | None = None) -> str | None:
    """Why the loop is stuck, or None when it is not (CTS-006). Reads the last `window` scored experiments; fewer than that is not a stall.

    In order: `tail_risk` (the best experiment draws down too much), `costs_kill_edge` (most experiments carry the warning of the cost model),
    `overfit` (in sample is far above out of sample), `low_profit_factor` and `plateau` (the second half of the window does not beat the first)."""
    limit = limits_of_stall or StallLimits()
    scored = [e for e in history if e.status == "done" and e.objective is not None][-limit.window:]
    if len(scored) < limit.window:
        return None
    best = max(scored, key=lambda e: e.objective)
    drawdown = _finite(best.evaluation.max_drawdown)
    if drawdown is not None and abs(drawdown) > limit.max_drawdown:
        return "tail_risk"
    if sum(limit.costs_warning in e.evaluation.warnings for e in scored) * 2 > len(scored):
        return "costs_kill_edge"
    gaps = [e.evaluation.in_sample_objective - e.objective for e in scored if _finite(e.evaluation.in_sample_objective) is not None]
    if gaps and sum(gaps) / len(gaps) > limit.overfit_gap:
        return "overfit"
    factor = _finite(best.evaluation.profit_factor)
    if factor is not None and factor < limit.min_profit_factor:
        return "low_profit_factor"
    half = len(scored) // 2
    if max(e.objective for e in scored[half:]) - max(e.objective for e in scored[:half]) < limit.plateau_gain:
        return "plateau"
    return None


_IDEAS: dict[str, tuple[str, str, str]] = {
    "plateau": ("Invert the premise: trade against the signal, or exit on the condition that used to enter.",
                "Change the archetype: from trend following to mean reversion, or from a level breakout to a volatility contraction.",
                "Redefine the objective: from the Sharpe ratio to the profit factor after costs, or to the result per unit of drawdown."),
    "overfit": ("Invert the premise: drop the rule that needs the most parameters and keep only the one that has a reason.",
                "Change the archetype: to one with two or three parameters, such as a single lookback and a single threshold.",
                "Redefine the objective: reward the out of sample result and the stability of neighbors, not the best single point."),
    "costs_kill_edge": ("Invert the premise: trade less often, entering only when the expected move is several times the round trip cost.",
                        "Change the archetype: from intraday bars to daily bars, or from an entry on every signal to a slow rebalance.",
                        "Redefine the objective: the result net of costs per trade, so that cheap and rare beats frequent."),
    "low_profit_factor": ("Invert the premise: let the winners run and cut the losers sooner, or the other way round, and keep what the data supports.",
                          "Change the archetype: from a fixed target and stop to a trailing exit, or the reverse.",
                          "Redefine the objective: maximize the profit factor with a floor on the number of trades."),
    "tail_risk": ("Invert the premise: stand aside when volatility is above its own history instead of sizing up.",
                  "Change the archetype: from a single large position to several small independent ones, or to a hedged structure.",
                  "Redefine the objective: maximize the result with a hard limit on the drawdown and on the worst trade."),
}


def pivot_proposals(stall: str) -> tuple[Pivot, ...]:
    """Changes of skeleton for a stalled loop: invert an assumption, change the archetype, redefine the objective. Never another parameter tweak."""
    if stall not in _IDEAS:
        raise LoopError("bad_change", f"stall is one of {', '.join(STALL_KINDS)}")
    return tuple(Pivot(kind, idea) for kind, idea in zip(PIVOT_KINDS, _IDEAS[stall]))

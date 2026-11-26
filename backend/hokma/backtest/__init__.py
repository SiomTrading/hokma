"""Backtest service (SPEC-020, H4): the public entry points of section 4.2.

`run`, `walk_forward`, `monte_carlo`, `evidence` and `status` are functions of this package; `optimize` is the module of the search, which answers the
call of its function (`hokma.backtest.optimize(spec, space, ...)`), so `from hokma.backtest import optimize`, `import hokma.backtest.optimize as m` and
`hokma.backtest.optimize(...)` give the same object, loaded when it is first asked for. Each entry point has its keyword `services` (the database,
the registry, the source of the bars: `runner.Services`) and by default uses those of the service. The engine is imported only in `_lib.py`, and
importing the spec or the results never loads it."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from hokma.backtest.optimize import OptimizationResult, ParamSpace
    from hokma.backtest.placebo import MonteCarloResult
    from hokma.backtest.queue import JobStatus
    from hokma.backtest.results import BacktestResult
    from hokma.backtest.runner import Services
    from hokma.backtest.spec import BacktestSpec
    from hokma.backtest.walkforward import WalkForwardResult
    from hokma.quant.validation.evidence import EvidenceBundle

__all__ = ["run", "status", "optimize", "walk_forward", "monte_carlo", "evidence"]


def __getattr__(name: str) -> Any:
    """`hokma.backtest.optimize` is the module of the search whether or not it was imported yet (see the docstring of the package)."""
    if name == "optimize":
        import importlib

        return importlib.import_module("hokma.backtest.optimize")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def run(spec: "BacktestSpec", *, services: "Services | None" = None) -> "BacktestResult":
    """Run a backtest of `spec` and save it: the strategy is loaded by `spec.strategy_id` from the registry of the factory and the bars from the data
    feed (or the continuous series for `CONT:ROOT`). A failure in a stage is a saved run with status `failed`; the engine being absent raises
    `engine_unavailable` (see `hokma.backtest.runner.run`)."""
    from hokma.backtest import runner

    return (services or runner.Services()).run(spec)


def walk_forward(spec: "BacktestSpec", space: "ParamSpace", *, scheme: Literal["rolling", "anchored", "cpcv"], train: int, test: int, purge: int, embargo: int,
                 services: "Services | None" = None) -> "WalkForwardResult":
    """Walk forward or combinatorial purged validation of the points of `space` (see `hokma.backtest.walkforward.walk_forward`)."""
    from hokma.backtest import walkforward

    return walkforward.walk_forward(spec, space, scheme=scheme, train=train, test=test, purge=purge, embargo=embargo, services=services)


def monte_carlo(run_id: int, *, n: int, method: Literal["bootstrap", "permutation", "placebo"], seed: int,
                services: "Services | None" = None) -> "MonteCarloResult":
    """Monte Carlo over a saved run (see `hokma.backtest.placebo.monte_carlo`)."""
    from hokma.backtest import placebo

    return placebo.monte_carlo(run_id, n=n, method=method, seed=seed, services=services)


def evidence(strategy_id: str, version: int, *, services: "Services | None" = None) -> "EvidenceBundle":
    """The evidence bundle of a strategy for the judge of its line (see `hokma.backtest.walkforward.evidence`)."""
    from hokma.backtest import walkforward

    return walkforward.evidence(strategy_id, version, services=services)


def status(job_id: int, *, db: Any = None) -> "JobStatus":
    """The state of a queued job (see `hokma.backtest.queue.status`); the state of the engine is `hokma.backtest.engine.status()`."""
    from hokma.backtest import queue

    return queue.status(job_id, db=db)

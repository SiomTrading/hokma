"""The pieces of the journey of the verdict of the optimizer (SPEC-032, OPT-33): a data folder of Hokma that is not the user's, the real moves of the NQ fixture of H4
laid on several days, and the steps that H4 runs before a verdict exists (backtest, grid, walk forward, Monte Carlo), taken through the same entry point as the MCP server.

The test of the journey and the loader of the screen (`test_optimizer_verdict_journey.py`) both use it, so the data that Cristian opens is the data that the test checked."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backtest import api_support
from e2e_h4.support import SPACE, caller, ok, run_args
from factory.test_evidence_bridge import multi_day_bars
from factory.test_spec import text

STRATEGY = "cross"
FOLDERS = ("home", "work", "data")


def point_at(monkeypatch, base: Path) -> Path:
    """Send HOME, HOKMA_HOME and HOKMA_DATA_DIR to folders under `base` (never the user's) and migrate the database there. Returns the data folder."""
    from hokma.db import migrate

    for name, folder in zip(("HOME", "HOKMA_HOME", "HOKMA_DATA_DIR"), FOLDERS):
        (base / folder).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(base / folder))
    monkeypatch.setenv("HOKMA_LANG", "en")
    migrate()
    return base / "data"


def use_nq_services(monkeypatch, base: Path):
    """The services of the tools and of the routes over the bars of the NQ fixture, . Returns (services, bars)."""
    from hokma.db import get_database
    from hokma.kit import backtest_tools

    bars = multi_day_bars()
    services = api_support.services(bars, get_database(), None)               # no folder of curves: the one of the data folder, where the server of Hokma looks
    monkeypatch.setattr(backtest_tools, "services", lambda: services)
    return services, bars


def build_runs(services, bars) -> dict[str, Any]:
    """The draft of the strategy and the four runs that a verdict reads, each one through its tool. Returns their ids, the steps and how long each took."""
    from hokma.factory import spec as factory_spec

    who = caller("hokma")
    took: dict[str, float] = {}

    def timed(name: str, tool: str, args: dict[str, Any]) -> Any:
        started = time.monotonic()
        answer = ok(tool, args, who)
        took[name] = round(time.monotonic() - started, 2)
        return answer

    factory_spec.save_draft(text(strategy_id=STRATEGY), db=services.database())
    run = timed("backtest_run", "backtest_run", run_args(bars))
    grid = timed("backtest_optimize", "backtest_optimize", run_args(bars, space=SPACE, method="grid", n_trials=9, objective="sharpe", presweep=False))
    walk = timed("backtest_walkforward", "backtest_walkforward", run_args(bars, space=SPACE, scheme="anchored", train=100, test=100, purge=2, embargo=1))
    carlo = timed("backtest_montecarlo", "backtest_montecarlo", {"run_id": run["run_id"], "n": 200, "method": "bootstrap", "seed": 1})
    return {"run": run, "grid": grid, "walk": walk, "carlo": carlo, "took": took}

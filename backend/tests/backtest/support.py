"""Shared helpers of the backtest tests: the real NQ scenarios, the bars as Decimal and a runner of a plan of decisions."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import warnings
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from hokma.backtest import _lib
from hokma.backtest.fills import Decision
from hokma.backtest.spec import BacktestSpec, CostModel, FillRules
from hokma.backtest.ticks import Bar

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nq_scenarios.json"
TICK, MULT = D("0.25"), D(20)
SCENARIOS = ("T1", "T2", "T3", "T4")

requires_engine = pytest.mark.skipif(not _lib.available(), reason="the backtest engine has no wheel on this platform; the service answers engine_unavailable there")


def _repo_rules():
    spec_ = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
    module = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(module)
    return module


def is_engine_module(name: str) -> bool:
    """Whether a dotted module name belongs to the backtest engine. The name is not written in any test: it is recognised by the hashes
    that scripts/check_repo_rules.py keeps, the same ones that police the repository."""
    hashes = _repo_rules().ENGINE_HASHES
    return any(hashlib.sha256(token.encode()).hexdigest() in hashes for token in re.findall(r"[a-z0-9]+", name.split(".")[0].lower()))


def imports_of(source: str, package: str = "hokma.backtest") -> list[tuple[int, str]]:
    """(line, dotted module) of every import in `source`; `from x import y` gives `x` and `x.y`; relative imports are resolved against `package`."""
    found: list[tuple[int, str]] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)                   # other files may carry old escapes: only their imports matter here
        tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join(parts + ([base] if base else []))
            found.append((node.lineno, base))
            found += [(node.lineno, f"{base}.{alias.name}") for alias in node.names if alias.name != "*"]
    return found


def load_fixture() -> dict[str, Any]:
    if not FIXTURE.is_file():
        pytest.skip(f"fixture {FIXTURE.name} is absent: real NQ bars of Trading Core are needed (nothing is invented)")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def bars_of(name: str) -> list[Bar]:
    return [Bar(datetime.fromisoformat(b["ts"].replace("Z", "+00:00")), D(b["open"]), D(b["high"]), D(b["low"]), D(b["close"]), D(b["volume"]))
            for b in load_fixture()["scenarios"][name]["bars"]]


def expected(name: str) -> Mapping[str, Any]:
    return load_fixture()["scenarios"][name]["expected"]


def spec(**over: Any) -> BacktestSpec:
    base: dict[str, Any] = dict(
        strategy_id="t", strategy_version=1, params={}, line="DT", instruments=("CONT:NQ",), bar="1m",
        start=datetime.fromisoformat("2025-11-01T00:00:00+00:00"), end=datetime.fromisoformat("2025-11-12T00:00:00+00:00"),
        as_of=datetime.fromisoformat("2025-11-12T00:00:00+00:00"), capital_usd=D(1_000_000), costs=CostModel("schwab"), fill=FillRules())
    base.update(over)
    return BacktestSpec(**base)


def plan(bars: Sequence[Bar], decisions: Mapping[str, Sequence[Decision]]):
    """A decision function that acts after the close of the bars named by their open time (`2025-11-10T14:59:00Z`)."""
    index = {b.ts.strftime("%Y-%m-%dT%H:%M:%SZ"): i for i, b in enumerate(bars)}
    by_bar = {index[ts]: list(d) for ts, d in decisions.items()}
    return lambda i, bar, side: by_bar.get(i, [])


def scenario_run(name: str, side: str = "long", **over: Any):
    """The acceptance scenario run through the service: returns (RunOutput, bars)."""
    from hokma.backtest import engine
    bars, d = bars_of(name), load_fixture()["scenarios"][name]["decisions"]
    if name in ("T1", "T4"):
        decisions = {d["buy_after_close_of"]: [Decision("enter", "long")], d["sell_after_close_of"]: [Decision("exit")]}
        protective = False
    else:
        stop = D(d["stop_points"])
        target = D(d["target_points"]) if "target_points" in d else None
        decisions = {d["enter_after_close_of"]: [Decision("enter", side, stop_points=stop, target_points=target)]}
        protective = True
    out = engine.run(spec(**over), bars, plan(bars, decisions), tick_size=TICK, multiplier=MULT, symbol="NQZ5", protective_orders=protective)
    return out, bars

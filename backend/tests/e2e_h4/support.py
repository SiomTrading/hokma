"""Shared pieces of the end to end proof of H4 (SPEC-020, section 9): the tools are called through the same entry point the MCP server uses
(`hokma_tools.call`), the services of the tools are pointed at real NQ bars and a registry that holds a test strategy, and Ian is a caller like any other."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from backtest import api_support, search_support as ss
from backtest.runner_support import NQ

from hokma.backtest import results
from hokma.kit import backtest_tools, hokma_tools

IAN = "strategy-researcher"

SIGNALS = """
    - name: fast_above_slow_is_long
      facts: {ema_fast: [101, 102, 103], ema_slow: [100, 100, 100]}
      expect: long
    - name: fast_below_slow_is_short
      facts: {ema_fast: [99, 98, 97], ema_slow: [100, 100, 100]}
      expect: short
"""
SIZES = """
    - name: risk_500_on_a_5_point_stop
      contract_id: FUT:NQ:2025-03
      asset_type: future
      multiplier: 20
      kind: risk_usd
      value: 500
      price: 20000
      equity: 100000
      stop_distance: 5
      expect_qty: 5
"""


def spec_text(strategy_id: str = "cross", *, signals: str = SIGNALS, sizes: str = SIZES, hypothesis: str = "A fast average above a slow one means the move continues.") -> str:
    tests = ("tests:\n" + (f"  signal:{signals}" if signals else "") + (f"  position_size:{sizes}" if sizes else "")) if (signals or sizes) else ""
    return f"strategy_id: {strategy_id}\nline: DT\nalpha: ema_cross\nparams: {{fast: 3, slow: 5}}\nhypothesis: {hypothesis}\n{tests}"


def caller(agent: str | None = IAN, conversation: int = 1, task: int = 1) -> hokma_tools.Caller:
    env = {"HOKMA_CONVERSATION_ID": str(conversation), "HOKMA_TASK_ID": str(task)}
    return hokma_tools.Caller({**env, "HOKMA_AGENT": agent} if agent else env)


def call(name: str, args: dict[str, Any], who: hokma_tools.Caller | None = None) -> tuple[Any, bool]:
    """(answer, is_error) of a tool through the server entry point. An error answer is its JSON; a refused schema is the text."""
    text, is_error = hokma_tools.call(name, args, who or caller("hokma"))
    try:
        return json.loads(text), is_error
    except ValueError:
        return text, is_error


def ok(name: str, args: dict[str, Any], who: hokma_tools.Caller | None = None) -> Any:
    answer, is_error = call(name, args, who)
    assert not is_error, answer
    return answer


def failed(name: str, args: dict[str, Any], who: hokma_tools.Caller | None = None) -> dict[str, Any]:
    answer, is_error = call(name, args, who)
    assert is_error and isinstance(answer, dict), answer
    return answer


def bars(n: int = 600, seed: int = 1):
    return ss.chained_bars(n, seed)


def use_services(monkeypatch, series, db, folder, **over) -> None:
    """Point the tools at real bars and at the registry of the tests (`cross`, `cross_stop`)."""
    monkeypatch.setattr(backtest_tools, "services", lambda: api_support.services(series, db, folder, **over))


def run_args(series, **over) -> dict[str, Any]:
    base = dict(strategy_id="cross", version=1, line="DT", instruments=[NQ], bar="1m", start=series[0].ts.isoformat(), end=series[-1].ts.isoformat(),
                capital_usd=1_000_000, fee_table="schwab", params={"fast": 5, "slow": 20})
    return {**base, **over}


SPACE = {"axes": [{"name": "fast", "kind": "int", "low": 3, "high": 5, "step": 1}, {"name": "slow", "kind": "int", "low": 10, "high": 30, "step": 10}],
         "constraints": [{"left": "fast", "op": "lt", "right": "slow"}]}


def same_bytes(first, second) -> bool:
    """Two results serialized and compared byte for byte; only the id of the row, which is the database's, differs."""
    return results.to_json(replace(first, run_id=0)) == results.to_json(replace(second, run_id=0))

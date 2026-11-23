"""Support of the analyst tests of the SWO desk (SPEC-019: B7): a scripted chat model, recorded data and the registry in process."""
from __future__ import annotations

import importlib
import itertools
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from hokma.desks.core.graph import ModelReply
from hokma.desks.core.state import Message, ToolCall
from hokma.quant import registry

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "kit" / "template" / "desk"
FIXTURES = Path(__file__).resolve().parent / "quant" / "fixtures"
AS_OF = datetime(2024, 6, 14, 20, 0, tzinfo=timezone.utc)


class ScriptedChat:
    """A fake chat model: it plays its replies in order (the last one repeats) and keeps what it was asked."""

    def __init__(self, *replies: ModelReply) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def chat(self, system: str, messages: Sequence[Message], tools: Sequence[Mapping[str, Any]] = ()) -> ModelReply:
        self.calls.append({"system": system, "messages": list(messages), "tools": [t["name"] for t in tools]})
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


_SERIAL = itertools.count(1)


def call(tool: str, **arguments: Any) -> ModelReply:
    """A reply of the model that asks for one tool."""
    return ModelReply("", (ToolCall(f"c{next(_SERIAL)}", tool, dict(arguments)),))


def answer(value: Mapping[str, Any]) -> ModelReply:
    return ModelReply(json.dumps(value))


FAMILIES = {
    "options": ("pricing", "iv", "greeks", "trees", "payoff", "surface", "position_greeks", "vol_analysis", "liquidity", "structures", "exotic", "chain"),
    "indicators": ("breadth", "momentum", "overlap", "volatility", "signal_processing", "catalog", "screens.momentum_burst", "screens.weekly_structure",
                   "screens.vcp", "screens.distribution_days", "screens.pead", "screens.earnings_reaction", "screens.episodic_pivot"),
    "stats": ("volatility", "hurst", "cointegration", "timeseries", "kalman", "regression", "causal", "events", "sentiment"),
    "fundamentals": ("formulas", "ratios", "scores", "dupont", "cost_of_capital", "value", "facts", "panel", "scalers", "descriptors", "criteria"),
}


def load_registry() -> None:
    """Calls run in this process and the modules the analysts reach are registered (other tests reset the registry)."""
    registry.IN_PROCESS = True
    for family, parts in FAMILIES.items():
        importlib.import_module(f"hokma.quant.{family}")
        if not registry.list(family):
            for part in parts:
                importlib.reload(importlib.import_module(f"hokma.quant.{family}.{part}"))


def spy_bars() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "ohlcv_daily_spy.csv", index_col=0, parse_dates=True)


def frame_payload(frame: pd.DataFrame) -> dict[str, Any]:
    """A table the way a model sends it to quant_call: the index as dates and one list per column."""
    out: dict[str, Any] = {"index": [d.strftime("%Y-%m-%d") for d in frame.index]}
    out.update({c: [float(x) for x in frame[c]] for c in frame.columns})
    return out


def skill_text(name: str) -> str:
    return (KIT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def cited_calculations(name: str) -> set[str]:
    """Registry names a skill tells the agent to call (`quant_call module.function` or a backticked module.function)."""
    import re

    text = skill_text(name)
    found = set(re.findall(r"quant_call `?((?:indicators|stats|options|fundamentals)\.[a-z_.]+)", text))
    found |= set(re.findall(r"`((?:indicators|stats|options|fundamentals)\.[a-z_]+(?:\.[a-z_]+)?)`", text))
    return found


def piece_ids(name: str) -> tuple[str, ...]:
    return registry.describe(name).piece_ids

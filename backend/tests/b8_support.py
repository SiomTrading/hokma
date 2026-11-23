"""Support of the investor and earnings drift tests (SPEC-019: B8): the registry in process, recorded statements, a scripted model that
follows the lean of the criteria, and a SWO desk built on the small fake kit."""
from __future__ import annotations

import importlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from hokma.desks.core.graph import ModelReply
from hokma.desks.core.state import Message
from hokma.quant import registry

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "kit" / "template" / "desk"
FIXTURES = Path(__file__).resolve().parent / "quant" / "fixtures"
AS_OF = datetime(2024, 7, 1, 20, 0, tzinfo=timezone.utc)      # statements filed through 2024-06-30 are public
CUT = "2024-06-30"

FAMILIES = {
    "fundamentals": ("formulas", "ratios", "scores", "dupont", "cost_of_capital", "value", "facts", "panel", "scalers", "descriptors", "criteria"),
    "indicators": ("breadth", "momentum", "overlap", "volatility", "signal_processing", "catalog", "screens.momentum_burst", "screens.weekly_structure",
                   "screens.vcp", "screens.distribution_days", "screens.pead", "screens.earnings_reaction", "screens.episodic_pivot"),
    "stats": ("volatility", "hurst", "cointegration", "timeseries", "kalman", "regression", "causal", "events", "sentiment"),
    "portfolio": ("blend", "optimize", "priors", "factors", "uncertainty", "preselect", "alpha", "container", "weights", "ic"),
}


def load_registry() -> None:
    """Calls run in this process and the modules the profiles reach are registered (other tests reset the registry)."""
    registry.IN_PROCESS = True
    for family, parts in FAMILIES.items():
        importlib.import_module(f"hokma.quant.{family}")
        if not registry.list(family):
            for part in parts:
                importlib.reload(importlib.import_module(f"hokma.quant.{family}.{part}"))


def statements(company: str = "ALFA", **last: Any) -> pd.DataFrame:
    """Point in time statements of one of the three recorded companies; `last` changes columns of the last period public at the cut."""
    pit = json.loads((FIXTURES / "statements_pit.json").read_text(encoding="utf-8"))
    frame = pd.DataFrame(pit["companies"][company]["rows"]).set_index("period_end")
    numeric = [c for c in frame.columns if c != "filed"]
    frame[numeric] = frame[numeric].astype(float)
    if last:
        row = frame[frame["filed"] <= CUT].index[-1]
        for key, value in last.items():
            frame.loc[row, key] = value
    return frame


def candles() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "ohlcv_daily_spy.csv", index_col=0, parse_dates=True)


def reply(stance: str, confidence: int = 70, reason: str = "The criteria point this way. The figures come from the module.",
          disagrees: bool = False) -> ModelReply:
    return ModelReply(json.dumps({"stance": stance, "confidence": confidence, "reason": reason, "disagrees_with_criteria": disagrees}))


class Chat:
    """A scripted chat model. It plays its replies in order (the last one repeats) and keeps what it was asked."""

    def __init__(self, *replies: ModelReply) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def chat(self, system: str, messages: Sequence[Message], tools: Sequence[Mapping[str, Any]] = ()) -> ModelReply:
        self.calls.append({"system": system, "messages": list(messages), "tools": [t["name"] for t in tools]})
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class LeanChat:
    """A model that follows the lean the code derived from the criteria, with a fixed confidence."""

    def __init__(self, confidence: int = 70) -> None:
        self.confidence, self.calls = confidence, []

    def chat(self, system: str, messages: Sequence[Message], tools: Sequence[Mapping[str, Any]] = ()) -> ModelReply:
        self.calls.append({"system": system, "messages": list(messages)})
        text = next(m.content for m in messages if m.role == "user")
        lean = int(re.search(r"Lean derived by code from the criteria: (-?\d)", text).group(1))
        return reply({1: "bullish", 0: "neutral", -1: "bearish"}[lean], self.confidence)

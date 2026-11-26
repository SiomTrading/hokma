"""Real NQ 12-25 bars of Trading Core for the run, grid and edge tests: a whole regular session of one minute bars, ten overnight bars and 70
daily sessions (fixtures/nq_session.json). Nothing here is made up: a test that needs data the file does not hold says what is missing."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from hokma.backtest import engine
from hokma.backtest.ticks import Bar

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nq_session.json"
TICK, MULT = D("0.25"), D(20)


def _load() -> dict:
    if not FIXTURE.is_file():
        pytest.skip(f"fixture {FIXTURE.name} is absent: real NQ bars of Trading Core are needed (nothing is invented)")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def session_bars(part: str) -> list[Bar]:
    """`day` (390 minute bars of the regular session of 2025-11-10), `night` (02:58 to 03:07 New York time of the same day) or `daily`."""
    return [Bar(datetime.fromisoformat(b["ts"].replace("Z", "+00:00")), D(b["open"]), D(b["high"]), D(b["low"]), D(b["close"]), D(b["volume"]))
            for b in _load()[part]["bars"]]


def multi_bars(root: str) -> list[Bar]:
    """Real 1 minute bars of ES, MES or CL (December 2025 contracts) from 14:58 to 15:11 UTC on 2025-11-10: 14 consecutive bars."""
    path = FIXTURE.with_name("multi_symbols.json")
    if not path.is_file():
        pytest.skip(f"fixture {path.name} is absent: real bars of Trading Core are needed (nothing is invented)")
    rows = json.loads(path.read_text(encoding="utf-8"))["instruments"][root]["bars"]
    return [Bar(datetime.fromisoformat(b["ts"].replace("Z", "+00:00")), D(b["open"]), D(b["high"]), D(b["low"]), D(b["close"]), D(b["volume"]))
            for b in rows]


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def run(name: str, decisions: dict, *, bars=None, protective=False, **over):
    """A run of the bars of a scenario (or of `bars`) with a plan of decisions: the keys are the open time of the bar after whose close the
    order is decided. `latency_ms`, `expiry_rule`, `root`, `asset_type`, `tick` and `multiplier` go to the engine, the rest to the spec."""
    from backtest import support

    series = bars if bars is not None else support.bars_of(name)
    extra = {k: over.pop(k) for k in ("latency_ms", "expiry_rule", "root", "asset_type") if k in over}
    kw = dict(tick_size=over.pop("tick", TICK), multiplier=over.pop("multiplier", MULT), symbol="NQZ5", protective_orders=protective, **extra)
    return engine.run(support.spec(**over), series, support.plan(series, decisions), **kw)

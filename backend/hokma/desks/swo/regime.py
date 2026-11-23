"""The market regime of one cut-off, read from the statistics module (SPEC-019: step 3 of the cycle, AH-22; CTS-024 for the exit regime).

The regime is a label the profile of Ray (A20) needs: `favorable`, `neutral` or `hostile`. It is derived here, by code, from two
registered functions of SVC-stats: the Yang-Zhang volatility of the benchmark by month and the Hurst exponent of its closes. No agent
calls this and no agent can write the label. When the bars or the calls are not enough, the answer is None: a regime that cannot be
read is never read as neutral.

Rule, written once so it can be tested and later calibrated:
- the rank of the latest monthly volatility inside its own history (0 to 1);
- `hostile` from rank 0.75; `favorable` up to rank 0.50 when the closes are not mean reverting; `neutral` otherwise.
The thresholds are not calibrated yet. They are printed in the facts so the next hit of a backtest can replace them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from hokma.quant import registry

HOSTILE_RANK = 0.75
FAVORABLE_RANK = 0.50
MIN_BARS = 120
REGIMES = ("favorable", "neutral", "hostile")


def closed_bars(bars: Any, as_of: datetime) -> pd.DataFrame | None:
    """The sessions before the cut-off day: the session of the cut-off day may still be open."""
    if bars is None or not isinstance(bars, pd.DataFrame) or bars.empty:
        return None
    cut = as_of.astimezone(timezone.utc).date()
    index = pd.to_datetime(bars.index)
    kept = bars[[d.date() < cut for d in index]]
    return kept if len(kept) else None


def _rank(values: list[float], latest: float) -> float:
    below = sum(1 for v in values if v < latest)
    equal = sum(1 for v in values if v == latest)
    return (below + 0.5 * equal) / len(values)


def derive_regime(bars: Any, as_of: datetime) -> tuple[str | None, dict[str, Any]]:
    """(regime, facts). `facts` always says what was read or why nothing was."""
    frame = closed_bars(bars, as_of)
    facts: dict[str, Any] = {"hostile_from_rank": HOSTILE_RANK, "favorable_up_to_rank": FAVORABLE_RANK}
    if frame is None or len(frame) < MIN_BARS or not {"open", "high", "low", "close"} <= set(frame.columns):
        facts["reason"] = "not_enough_closed_bars"
        return None, facts
    try:
        registry.load_all()
        vol = registry.call("stats.yang_zhang", open_=frame["open"], high=frame["high"], low=frame["low"], close=frame["close"], period="monthly")
        hurst = registry.call("stats.hurst", series=frame["close"])
    except Exception as exc:                                                 # noqa: BLE001  (a regime that cannot be read is None, never neutral)
        facts["reason"] = f"stats_call_failed:{type(exc).__name__}"
        return None, facts
    value, other = vol.value, hurst.value
    if not isinstance(value, dict) or not value.get("values") or value.get("latest") is None or not isinstance(other, dict):
        facts["reason"] = "stats_returned_no_value"
        return None, facts
    history = [float(v) for v in value["values"] if v is not None]
    if len(history) < 6:
        facts["reason"] = "not_enough_volatility_history"
        return None, facts
    rank = _rank(history, float(value["latest"]))
    reading = str(other.get("reading", ""))
    if rank >= HOSTILE_RANK:
        label = "hostile"
    elif rank <= FAVORABLE_RANK and reading != "mean_reverting":
        label = "favorable"
    else:
        label = "neutral"
    facts.update(volatility_rank=round(rank, 4), latest_volatility=float(value["latest"]), hurst_reading=reading,
                 sources=["stats.yang_zhang", "stats.hurst"], last_bar=str(frame.index[-1])[:10])
    return label, facts

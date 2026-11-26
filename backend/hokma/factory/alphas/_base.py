"""Shared pieces of the example alphas (SPEC-020: L-52). An alpha reads facts and gives a view; it never sizes, exits or times."""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.quant.errors import BadInput

CONFIDENCE = 50     # code alphas carry no calibrated confidence: the middle of the scale


def clip(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def fact(ctx: SignalContext, name: str, minimum: int = 1) -> np.ndarray | None:
    """A numeric array of facts, or None when it is missing, too short or holds a NaN in the part that is read."""
    raw = ctx.facts.get(name)
    if raw is None:
        return None
    try:
        array = np.asarray(raw, dtype=float)
    except (TypeError, ValueError):
        return None
    if array.ndim != 1 or len(array) < minimum or not np.all(np.isfinite(array[-minimum:])):
        return None
    return array


def view(ctx: SignalContext, source: str, value: float, criteria: Sequence[Criterion], reason: str) -> Signal:
    """A signal with the value clipped to [-1, +1]."""
    return Signal(source=source, desk_id=ctx.desk_id, line=ctx.line, symbol=ctx.symbol, as_of=ctx.as_of, horizon=ctx.horizon,
                  value=clip(value), confidence=CONFIDENCE, abstained=False, criteria=tuple(criteria), data_missing=(),
                  reason=reason, snapshot_hash=ctx.snapshot_hash)


def number(name: str, value: Any, *, minimum: float, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BadInput(f"{name} must be a number, got {value!r}")
    if integer and value != int(value):
        raise BadInput(f"{name} must be a whole number, got {value!r}")
    if value < minimum:
        raise BadInput(f"{name} must be at least {minimum}, got {value!r}")
    return float(value)

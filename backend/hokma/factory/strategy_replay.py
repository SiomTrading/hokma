"""The replay of a strategy over bars (SPEC-031 A2): what `user_api.replay` does, outside of `user_api` so that module names nothing of Hokma but its parameter schema.

It runs init once, computes the indicators the strategy asked for with the library of the factory (the same functions the backtest uses), reveals the bars one by one and
asks on_bar after the warm-up. The checks of a strategy (`rule_check`) and the tests that it brings run through it. Standard library, numpy and the library of indicators.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from hokma.factory import user_api
from hokma.factory.indicator_library import LIBRARY
from hokma.factory.user_api import BAR_FIELDS, Decision, IndicatorRequest, StrategyContext, StrategyInit


def bar_arrays(bars: Mapping[str, Any]) -> dict[str, np.ndarray]:
    if not isinstance(bars, Mapping):
        raise ValueError("bars is a mapping of open, high, low, close and optionally volume, each a list of numbers")
    arrays: dict[str, np.ndarray] = {}
    for key in ("open", "high", "low", "close"):
        if key not in bars:
            raise ValueError(f"the bars need {key}")
        arrays[key] = np.asarray(bars[key], dtype=float)
    size = len(arrays["close"])
    arrays["volume"] = np.asarray(bars["volume"], dtype=float) if "volume" in bars else np.zeros(size)
    for key, values in arrays.items():
        if values.ndim != 1 or len(values) != size or not np.all(np.isfinite(values)):
            raise ValueError(f"{key} must be {size} finite numbers, like close")
    return arrays


def indicators(requests: Sequence[IndicatorRequest], arrays: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    size = len(arrays["close"])
    for request in requests:
        if request.source == "bars":
            source: Any = {k: arrays[k] for k in BAR_FIELDS}
        elif request.source in arrays:
            source = arrays[request.source]
        elif request.source in out:
            source = out[request.source]
        else:
            raise ValueError(f"indicator {request.name!r}: source {request.source!r} is not a bar field or an earlier indicator")
        values = np.asarray(LIBRARY[request.kind](source, **dict(request.params)), dtype=float)
        if values.shape != (size,):
            raise ValueError(f"indicator {request.name!r} gave {values.shape} values for {size} bars")
        out[request.name] = values
    return out


def replay(strategy: Any, bars: Mapping[str, Any], params: Mapping[str, Any] | None = None, position_side: str | None = None) -> list[Decision]:
    if position_side not in (None, "long", "short"):
        raise ValueError("position_side is long, short or None")
    if isinstance(strategy, type):
        strategy = user_api.build_strategy(strategy, params)
    elif params:
        raise ValueError("params go with a class: an instance already has its own")
    arrays = bar_arrays(bars)
    size = len(arrays["close"])
    init = StrategyInit(dict(vars(strategy)) if hasattr(strategy, "__dict__") else {})
    strategy.init(init)
    seen = {**arrays, **indicators(init.requests, arrays)}
    wait = max(init.warmup, 1)
    out: list[Decision] = []
    for cursor in range(1, size + 1):
        if cursor < wait:
            out.append(user_api.hold())
            continue
        views = {}
        for name, values in seen.items():
            view = values[:cursor]
            view.flags.writeable = False
            views[name] = view
        context = StrategyContext(index=cursor, as_of=str(cursor), symbol="TEST", params=init.params, position_side=position_side, equity=100000.0, series=views)
        answer = strategy.on_bar(context)
        if not isinstance(answer, Decision):
            raise ValueError(f"on_bar must return a decision (long, short, flat or hold), got {answer!r} on bar {cursor}")
        out.append(answer)
    return out

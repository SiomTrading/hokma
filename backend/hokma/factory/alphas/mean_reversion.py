"""Mean reversion alpha on the z-score of a price or a spread (SPEC-020: ECT-053).

The rules, in the unit of the line (session minutes for the intraday line, days for the others):

  * the half-life of the reversion filters the view: it must lie inside [min_half_life, max_half_life]; the estimate comes from the
    statistics module of H3 (`hokma.quant.stats.timeseries.half_life`) and reaches the alpha as the fact half_life, in bars;
  * the z-score uses a window of `window_half_lives` half-lives (2 by default);
  * a z-score beyond -entry_z is a long view, beyond +entry_z a short view; the view turns flat when the z-score is back inside exit_z,
    when it goes past stop_z (the relationship may be broken) or when the view has lasted `time_stop_half_lives` half-lives (2 or 3);
  * after a stop or a time exit no new view is given until the z-score has come back inside entry_z;
  * no entry rests on an oscillator level (a relative strength reading of 30 or 70) without evidence: the schema has no such parameter.

The state is found by replaying the last `history` bars.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.factory.alphas._base import fact, view
from hokma.factory.features import rolling_zscore
from hokma.factory.params import Param, ParamSchema
from hokma.factory.strategy import IndicatorRequest
from hokma.factory.studies import _h3

UNITS = ("session_minutes", "days")


def estimate_half_life(values: Any, fn: Callable[[Any], Any] | None = None) -> float | None:
    """The half-life of a series in bars, from the H3 function (or the one injected). None when it is not defined."""
    return _h3.scalar((fn or _h3.half_life)(values))


def half_life_in_unit(half_life_bars: float, bar_size: float) -> float:
    """The half-life in the unit of the line: bars times the size of one bar (minutes of session per bar, or days per bar)."""
    return float(half_life_bars) * float(bar_size)


def reversion_positions(z: np.ndarray, entry_z: float, exit_z: float, stop_z: float, max_hold: int) -> np.ndarray:
    """The side held after each bar (1 long, -1 short, 0 flat) from a z-score series. A NaN z-score closes the view."""
    out = np.zeros(len(z), dtype=np.int8)
    side, held, blocked = 0, 0, False
    for t, value in enumerate(z):
        if not np.isfinite(value):
            side, held = 0, 0
            continue
        if abs(value) < entry_z:
            blocked = False
        if side != 0:
            held += 1
            back = value >= -exit_z if side > 0 else value <= exit_z
            broken = value < -stop_z if side > 0 else value > stop_z
            if back or broken or held >= max_hold:
                blocked = blocked or broken or (held >= max_hold and not back)
                side, held = 0, 0
        elif not blocked and abs(value) <= stop_z:
            side = 1 if value < -entry_z else -1 if value > entry_z else 0
            held = 0
        out[t] = side
    return out


@dataclass(frozen=True)
class MeanReversion:
    """Reads the facts close (a price or a spread) and half_life (bars). Flat view while the half-life is outside its range."""

    entry_z: float = 2.0
    exit_z: float = 0.5
    stop_z: float = 4.0
    window_half_lives: float = 2.0
    time_stop_half_lives: float = 2.5
    min_half_life: float = 5.0
    max_half_life: float = 60.0
    unit: str = "days"
    bar_size: float = 1.0
    history: int = 250

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((
        Param("entry_z", "float", 2.0, 0.1, 10.0), Param("exit_z", "float", 0.5, 0.0, 10.0), Param("stop_z", "float", 4.0, 0.2, 20.0),
        Param("window_half_lives", "float", 2.0, 0.5, 20.0), Param("time_stop_half_lives", "float", 2.5, 1.0, 10.0),
        Param("min_half_life", "float", 5.0, 0.1, 100000.0), Param("max_half_life", "float", 60.0, 0.1, 100000.0),
        Param("unit", "choice", "days", choices=UNITS), Param("bar_size", "float", 1.0, 0.1, 10000.0), Param("history", "int", 250, 10, 5000)))
    FACTS: ClassVar[tuple[str, ...]] = ("close", "half_life")

    def __post_init__(self) -> None:
        if not (0.0 <= self.exit_z < self.entry_z < self.stop_z):
            raise ValueError("the z levels need exit_z < entry_z < stop_z")
        if self.min_half_life >= self.max_half_life:
            raise ValueError("min_half_life must be below max_half_life")

    @property
    def lookback(self) -> int:
        return self.history

    def requests(self) -> tuple[IndicatorRequest, ...]:
        """The half-life of the trailing `history` closes: the runner supplies it from the `stats.half_life` function of H3."""
        return (IndicatorRequest("half_life", "half_life", "close", (("length", self.history),), self.history),)

    def signal(self, ctx: SignalContext) -> Signal:
        close, hl = fact(ctx, "close", 10), fact(ctx, "half_life", 1)
        if close is None or hl is None:
            return ctx.abstain("The series or its half-life is not available. No view is given until both are.", ("close", "half_life"))
        bars = float(hl[-1])
        if not math.isfinite(bars) or bars <= 0:
            return ctx.abstain("The half-life is not a positive number: the series does not revert. No view is given.", ("half_life",))
        unit_value = half_life_in_unit(bars, self.bar_size)
        in_range = self.min_half_life <= unit_value <= self.max_half_life
        window = max(3, int(round(self.window_half_lives * bars)))
        series = close[-self.history:]
        z = rolling_zscore(series, window) if len(series) >= window else np.full(len(series), np.nan)
        last = float(z[-1]) if np.isfinite(z[-1]) else None
        criteria = (Criterion("half_life", unit_value, self.max_half_life, in_range, "stats.half_life"),
                    Criterion("z_score", last, self.entry_z, None if last is None else abs(last) >= self.entry_z, "data.close"))
        if not in_range:
            return view(ctx, "alpha.mean_reversion", 0.0, criteria, f"The half-life is {unit_value:.1f} {self.unit.replace('_', ' ')}, outside the range that supports a reversion view. The view is flat.")
        max_hold = max(1, int(math.ceil(self.time_stop_half_lives * bars)))
        side = int(reversion_positions(z, self.entry_z, self.exit_z, self.stop_z, max_hold)[-1])
        word = {1: "long", -1: "short", 0: "flat"}[side]
        return view(ctx, "alpha.mean_reversion", float(side), criteria,
                    f"The z-score is measured over a window of {self.window_half_lives:g} half-lives. The view is {word}, and it is given up after {self.time_stop_half_lives:g} half-lives or at an extreme z-score.")

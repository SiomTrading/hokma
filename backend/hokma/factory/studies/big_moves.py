"""Daily study of big movers (SPEC-020: CTS-018). A research file: it gives no signal and places no order.

An event is a day on which a stock or contract moved 20 percent or more up or down against the close `window` days earlier. For each
event the study measures what a trader who acted on it would have lived, with the fill of the house:

  * the entry is the open of the day after the event, one tick against the continuation side (an up move is followed long, a down
    move short), never the close of the event day;
  * the result at 1, 3, 5, 10 and 20 days is measured from that entry to the close of the day counted from the event day, in the
    direction of the continuation;
  * the best and worst open result inside the window (MFE and MAE) are in USD with the contract multiplier.

Events are grouped by direction, catalyst and context. A group with fewer events than `min_sample` gives no statistics. A study over a
universe without delisted names carries CURRENT_UNIVERSE_BACKFILL_SURVIVORSHIP_BIAS. The t statistic and the bootstrap interval of a group
come from `hokma.quant.stats.events` (H3) or from a function the caller injects.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from hokma.factory.studies import _h3
from hokma.quant.errors import BadInput
from hokma.quant.limits import as_int

SURVIVORSHIP_FLAG = "CURRENT_UNIVERSE_BACKFILL_SURVIVORSHIP_BIAS"
HORIZONS = (1, 3, 5, 10, 20)
MAX_BARS = 200_000
MAX_EVENTS = 100_000
Direction = Literal["up", "down"]
StatsFn = Callable[[list[float], int], Any]
ZERO = Decimal(0)


@dataclass(frozen=True)
class DailyBar:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        values = (self.open, self.high, self.low, self.close)
        if not all(isinstance(v, Decimal) and v.is_finite() and v > 0 for v in values):
            raise BadInput(f"bar of {self.day}: prices are positive Decimal numbers")
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise BadInput(f"bar of {self.day}: the range does not hold the open and the close")


@dataclass(frozen=True)
class Instrument:
    symbol: str
    tick: Decimal
    multiplier: Decimal = Decimal(1)       # USD per point per unit: 1 for a share, the contract multiplier for a future

    def __post_init__(self) -> None:
        if not self.symbol or not all(isinstance(v, Decimal) and v.is_finite() and v > 0 for v in (self.tick, self.multiplier)):
            raise BadInput("an instrument needs a symbol, a positive tick and a positive multiplier")


@dataclass(frozen=True)
class Outcome:
    horizon: int
    return_pct: Decimal | None         # in the direction of the continuation; None when the window is not complete yet
    mfe_usd: Decimal | None            # at least 0
    mae_usd: Decimal | None            # at most 0
    continued: bool | None


@dataclass(frozen=True)
class Event:
    symbol: str
    day: date
    direction: Direction
    move_pct: Decimal
    entry: Decimal | None              # None when the next day does not exist yet
    outcomes: tuple[Outcome, ...]
    catalyst: str | None = None
    context: str | None = None


@dataclass(frozen=True)
class Cohort:
    key: tuple[str | None, ...]
    n_events: int
    insufficient_sample: bool
    horizons: Mapping[int, Mapping[str, Any]]     # per horizon: n, mean_return_pct, median_return_pct, continuation_rate, mean_mfe_usd, mean_mae_usd, significance


@dataclass(frozen=True)
class BigMoveStudy:
    events: tuple[Event, ...]
    cohorts: tuple[Cohort, ...]
    flags: tuple[str, ...]
    group_by: tuple[str, ...]
    min_sample: int


def _check_bars(symbol: str, bars: Sequence[DailyBar]) -> None:
    if not 0 < len(bars) <= MAX_BARS:
        raise BadInput(f"{symbol}: bars is a list of 1 to {MAX_BARS} daily bars")
    for earlier, later in zip(bars, bars[1:]):
        if later.day <= earlier.day:
            raise BadInput(f"{symbol}: bars must be in strictly increasing date order ({earlier.day} then {later.day})")


def find_events(bars: Sequence[DailyBar], *, threshold: Decimal = Decimal("0.20"), window: int = 1, cooldown: int = 20) -> list[tuple[int, Direction, Decimal]]:
    """(index, direction, move) of each day whose close is `threshold` or more away from the close `window` days before.

    After an event the same symbol is skipped for `cooldown` days, so the windows of two events never overlap.
    """
    if not isinstance(threshold, Decimal) or not threshold.is_finite() or threshold <= 0:
        raise BadInput("threshold is a positive Decimal, 0.20 for twenty percent")
    span, rest = as_int("window", window), as_int("cooldown", cooldown)
    if span < 1 or rest < 0:
        raise BadInput("window is at least 1 and cooldown at least 0")
    found: list[tuple[int, Direction, Decimal]] = []
    blocked_until = -1
    for i in range(span, len(bars)):
        if i <= blocked_until:
            continue
        move = bars[i].close / bars[i - span].close - 1
        if move >= threshold or move <= -threshold:
            found.append((i, "up" if move > 0 else "down", move))
            blocked_until = i + rest
    return found


def measure(bars: Sequence[DailyBar], index: int, direction: Direction, instrument: Instrument, horizons: Sequence[int] = HORIZONS) -> tuple[Decimal | None, tuple[Outcome, ...]]:
    """The entry price and the outcome at each horizon of the event on `bars[index]`.

    The entry day is `index + 1`; the exit of horizon h is the close of `index + h`. A window that is not complete has no result.
    """
    if direction not in ("up", "down"):
        raise BadInput("direction is up or down")
    if index + 1 >= len(bars):
        return None, tuple(Outcome(h, None, None, None, None) for h in horizons)
    long = direction == "up"
    entry = bars[index + 1].open + instrument.tick if long else bars[index + 1].open - instrument.tick
    if entry <= 0:
        raise BadInput(f"{instrument.symbol}: the entry price is not positive")
    outcomes = []
    for h in horizons:
        last = index + as_int("horizon", h)
        if h < 1 or last >= len(bars):
            outcomes.append(Outcome(h, None, None, None, None))
            continue
        window = bars[index + 1:last + 1]
        exit_close = bars[last].close
        result = (exit_close - entry) / entry if long else (entry - exit_close) / entry
        best = (max(b.high for b in window) - entry) if long else (entry - min(b.low for b in window))
        worst = (min(b.low for b in window) - entry) if long else (entry - max(b.high for b in window))
        outcomes.append(Outcome(h, result, max(best, ZERO) * instrument.multiplier, min(worst, ZERO) * instrument.multiplier, result > 0))
    return entry, tuple(outcomes)


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _stats_fn(fn: StatsFn | None) -> StatsFn:
    return fn or _h3.event_stats


def _significance(fn: StatsFn, values: list[Decimal], seed: int) -> Any:
    result = fn([float(v) for v in values], seed)
    value = getattr(result, "value", result)
    return dict(value) if isinstance(value, Mapping) else None


def summarize(events: Sequence[Event], *, group_by: Sequence[str] = ("direction", "catalyst", "context"), min_sample: int,
              horizons: Sequence[int] = HORIZONS, event_stats: StatsFn | None = None, significance: bool = False, seed: int = 0) -> tuple[Cohort, ...]:
    """Statistics per cohort and horizon. A cohort below `min_sample` events has none: its numbers would be noise."""
    fields = tuple(group_by)
    if not fields or any(f not in ("direction", "catalyst", "context", "symbol") for f in fields):
        raise BadInput("group_by uses direction, catalyst, context or symbol")
    minimum = as_int("min_sample", min_sample)
    if minimum < 2:
        raise BadInput("min_sample is at least 2")
    fn = _stats_fn(event_stats) if significance else None
    groups: dict[tuple[str | None, ...], list[Event]] = {}
    for event in events:
        groups.setdefault(tuple(getattr(event, f) for f in fields), []).append(event)
    cohorts = []
    for key in sorted(groups, key=lambda k: tuple("" if v is None else v for v in k)):
        members = groups[key]
        enough = len(members) >= minimum
        per_horizon: dict[int, Mapping[str, Any]] = {}
        for h in horizons:
            rows = [o for e in members for o in e.outcomes if o.horizon == h and o.return_pct is not None]
            if not enough or not rows:
                per_horizon[h] = {"n": len(rows), "mean_return_pct": None, "median_return_pct": None, "continuation_rate": None,
                                  "mean_mfe_usd": None, "mean_mae_usd": None, "significance": None}
                continue
            returns = [o.return_pct for o in rows]       # type: ignore[misc]
            per_horizon[h] = {"n": len(rows), "mean_return_pct": sum(returns, ZERO) / len(rows), "median_return_pct": _median(returns),
                              "continuation_rate": Decimal(sum(1 for o in rows if o.continued)) / len(rows),
                              "mean_mfe_usd": sum((o.mfe_usd for o in rows), ZERO) / len(rows),     # type: ignore[misc]
                              "mean_mae_usd": sum((o.mae_usd for o in rows), ZERO) / len(rows),     # type: ignore[misc]
                              "significance": _significance(fn, returns, seed) if fn is not None and len(rows) >= minimum else None}
        cohorts.append(Cohort(key, len(members), not enough, per_horizon))
    return tuple(cohorts)


def run_study(universe: Mapping[str, tuple[Instrument, Sequence[DailyBar]]], *, includes_delisted: bool, min_sample: int,
              annotations: Mapping[tuple[str, date], tuple[str | None, str | None]] | None = None, threshold: Decimal = Decimal("0.20"),
              window: int = 1, cooldown: int = 20, horizons: Sequence[int] = HORIZONS, group_by: Sequence[str] = ("direction", "catalyst", "context"),
              event_stats: StatsFn | None = None, significance: bool = False, seed: int = 0) -> BigMoveStudy:
    """Find the events of every symbol, measure them and group them. `includes_delisted` has no default: the caller states what the universe is.

    `annotations` maps (symbol, day) to a (catalyst, context) pair of labels; an event without one has no catalyst and no context.
    """
    if not isinstance(includes_delisted, bool):
        raise BadInput("includes_delisted is true or false")
    events: list[Event] = []
    for symbol in sorted(universe):
        instrument, bars = universe[symbol]
        _check_bars(symbol, bars)
        for index, direction, move in find_events(bars, threshold=threshold, window=window, cooldown=cooldown):
            entry, outcomes = measure(bars, index, direction, instrument, horizons)
            catalyst, context = (annotations or {}).get((symbol, bars[index].day), (None, None))
            events.append(Event(symbol, bars[index].day, direction, move, entry, outcomes, catalyst, context))
            if len(events) > MAX_EVENTS:
                raise BadInput(f"the study found more than {MAX_EVENTS} events: raise the threshold or narrow the universe")
    cohorts = summarize(events, group_by=group_by, min_sample=min_sample, horizons=horizons, event_stats=event_stats, significance=significance, seed=seed)
    return BigMoveStudy(tuple(events), cohorts, () if includes_delisted else (SURVIVORSHIP_FLAG,), tuple(group_by), as_int("min_sample", min_sample))

"""Stop and objective in R, rounded to the tick with Decimal (SPEC-019: DTS-04).

Every exit is a function of the entry, the tick and one rule of the line, never of a model:
- DT: the stop is a number of ticks from the entry.
- SWC: the stop is a multiple of the ATR, a number that comes from the indicators module.
- SWO: the stop is a share of the premium paid (or a multiple of the credit received), or a level of the underlying whose premium
  was computed by the options module (`premium_at_stop`). The order carries the premium level; the underlying level is kept for
  the monitor.

R is the distance from the entry to the stop. The objective is `r_multiple` times R away from the entry, in the other direction.
Rounding goes against the trader: a stop is rounded away from the entry (the risk is never understated) and an objective toward
it (the reward is never overstated). The risk of the plan is measured from the rounded stop.

Trailing stops (`atr_trailing`, `trailing_pct`) follow the best price of the position and never move against it. The desk and the
backtest import these two functions: there is no second implementation.

Defaults (the premium share, the credit multiple, the profit share of a credit) are marked uncalibrated in the plan: a backtest
replaces them.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Literal

from hokma.exec.money import MoneyError, dec

Side = Literal["long", "short"]
Variant = Literal["DT", "SWC", "SWO"]
DEFAULT_R_MULTIPLE = Decimal(2)
MAX_R_MULTIPLE = Decimal(20)
DEFAULT_DEBIT_STOP_FRACTION = Decimal("0.5")      # a debit structure exits when half of the premium paid is lost
DEFAULT_CREDIT_STOP_MULTIPLE = Decimal(2)         # a credit structure exits when the loss reaches twice the credit
DEFAULT_CREDIT_PROFIT_FRACTION = Decimal("0.5")   # a credit structure takes profit at half of the credit
MAX_STOP_TICKS = 100_000
ZERO = Decimal(0)


class ExitError(ValueError):
    """The exit cannot be built from these inputs. `code` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExitPlan:
    variant: Variant
    side: Side                           # the side of the position: long profits from a higher price
    entry: Decimal
    stop: Decimal
    target: Decimal
    risk_per_unit: Decimal               # entry to the rounded stop, in price units
    reward_per_unit: Decimal             # entry to the rounded target
    r_multiple: Decimal                  # the reward over the risk after rounding
    basis: str                           # "ticks", "atr", "premium" or "underlying"
    tick: Decimal | None
    underlying_stop: Decimal | None = None
    uncalibrated: tuple[str, ...] = ()   # defaults used that a backtest has not calibrated


def _d(value: Any, name: str) -> Decimal:
    try:
        return dec(value, name)
    except MoneyError as exc:
        raise ExitError("bad_number", str(exc)) from exc


def _tick(value: Any) -> Decimal | None:
    if value is None:
        return None
    tick = _d(value, "tick")
    if tick <= 0:
        raise ExitError("bad_tick", "the tick must be above zero")
    return tick


def _round(price: Decimal, tick: Decimal | None, direction: str) -> Decimal:
    if tick is None:
        return price
    mode = ROUND_FLOOR if direction == "down" else ROUND_CEILING
    return (price / tick).to_integral_value(rounding=mode) * tick


def _r(value: Any) -> Decimal:
    r = _d(value, "r_multiple")
    if not ZERO < r <= MAX_R_MULTIPLE:
        raise ExitError("bad_r", f"r_multiple must be above zero and at most {MAX_R_MULTIPLE}")
    return r


def _plan(variant: Variant, side: Side, entry: Decimal, tick: Decimal | None, distance: Decimal, target_distance: Decimal, basis: str,
          *, underlying_stop: Decimal | None = None, uncalibrated: tuple[str, ...] = ()) -> ExitPlan:
    if side not in ("long", "short"):
        raise ExitError("bad_side", "side is long or short")
    if entry <= 0:
        raise ExitError("bad_price", "the entry must be above zero")
    if distance <= 0:
        raise ExitError("bad_stop", "the distance to the stop must be above zero")
    # Long: stop below, target above, both rounded down. Short: stop above, target below, both rounded up.
    direction = "down" if side == "long" else "up"
    sign = -1 if side == "long" else 1
    stop = _round(entry + sign * distance, tick, direction)
    target = _round(entry - sign * target_distance, tick, direction)
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if tick is not None and risk < tick:
        raise ExitError("stop_inside_tick", f"the stop is {risk} from the entry, less than one tick of {tick}")
    if stop <= 0:
        raise ExitError("stop_not_positive", f"the stop would be at {stop}: there is no price to leave at above zero")
    if (side == "long" and target <= entry) or (side == "short" and target >= entry) or target <= 0:
        raise ExitError("bad_target", f"the objective {target} is not on the profit side of the entry {entry}")
    return ExitPlan(variant, side, entry, stop, target, risk, reward, reward / risk, basis, tick, underlying_stop, uncalibrated)


def dt_exit(side: Side, entry: Any, *, stop_ticks: int, tick: Any, r_multiple: Any = DEFAULT_R_MULTIPLE) -> ExitPlan:
    """Day trading: the stop is `stop_ticks` ticks from the entry."""
    tk, ent, r = _tick(tick), _d(entry, "entry"), _r(r_multiple)
    if tk is None:
        raise ExitError("bad_tick", "a day trading stop in ticks needs the tick")
    if isinstance(stop_ticks, bool) or not isinstance(stop_ticks, int) or not 1 <= stop_ticks <= MAX_STOP_TICKS:
        raise ExitError("bad_stop", f"stop_ticks must be a whole number from 1 to {MAX_STOP_TICKS}")
    distance = tk * stop_ticks
    return _plan("DT", side, ent, tk, distance, distance * r, "ticks")


def swc_exit(side: Side, entry: Any, *, atr: Any, atr_multiple: Any = Decimal(2), tick: Any = None, r_multiple: Any = DEFAULT_R_MULTIPLE) -> ExitPlan:
    """Swing on CFDs: the stop is `atr_multiple` times the ATR, a number from the indicators module."""
    tk, ent, r = _tick(tick), _d(entry, "entry"), _r(r_multiple)
    a, m = _d(atr, "atr"), _d(atr_multiple, "atr_multiple")
    if a <= 0 or m <= 0:
        raise ExitError("bad_stop", "the ATR and its multiple must be above zero")
    distance = a * m
    return _plan("SWC", side, ent, tk, distance, distance * r, "atr")


def swo_exit(position: Literal["debit", "credit"], entry_premium: Any, *, tick: Any = None, basis: Literal["premium", "underlying"] = "premium",
             r_multiple: Any = DEFAULT_R_MULTIPLE, stop_fraction: Any = None, stop_multiple: Any = None, profit_fraction: Any = None,
             underlying_stop: Any = None, premium_at_stop: Any = None) -> ExitPlan:
    """Options swing. `entry_premium` is the net premium per share, as a positive number (what was paid, or the credit received).

    A debit position is long the premium: the stop is below it. A credit position is short: the stop is above it.
    Basis `premium`: a debit stops at `stop_fraction` of the premium lost, a credit at `stop_multiple` times the credit as loss.
    Basis `underlying`: the stop is a level of the underlying and `premium_at_stop` is what the options module priced the
    structure at there; the order exits at that premium."""
    if position not in ("debit", "credit"):
        raise ExitError("bad_side", "position is debit or credit")
    tk, ent, r = _tick(tick), _d(entry_premium, "entry_premium"), _r(r_multiple)
    side: Side = "long" if position == "debit" else "short"
    uncalibrated: list[str] = []
    under: Decimal | None = None
    if basis == "premium":
        if position == "debit":
            if stop_fraction is None:
                uncalibrated.append("stop_fraction")
            frac = DEFAULT_DEBIT_STOP_FRACTION if stop_fraction is None else _d(stop_fraction, "stop_fraction")
            if not ZERO < frac < 1:
                raise ExitError("bad_stop", "stop_fraction is above zero and below one")
            distance = ent * frac
        else:
            if stop_multiple is None:
                uncalibrated.append("stop_multiple")
            mult = DEFAULT_CREDIT_STOP_MULTIPLE if stop_multiple is None else _d(stop_multiple, "stop_multiple")
            if mult <= 0:
                raise ExitError("bad_stop", "stop_multiple must be above zero")
            distance = ent * mult
    elif basis == "underlying":
        if underlying_stop is None or premium_at_stop is None:
            raise ExitError("bad_stop", "a stop on the underlying needs its level and the premium the options module priced there")
        under = _d(underlying_stop, "underlying_stop")
        if under <= 0:
            raise ExitError("bad_stop", "the underlying stop must be above zero")
        at_stop = _d(premium_at_stop, "premium_at_stop")
        distance = (ent - at_stop) if position == "debit" else (at_stop - ent)
        if distance <= 0:
            raise ExitError("bad_stop", "the premium at the stop is not on the losing side of the entry")
    else:
        raise ExitError("bad_stop", "basis is premium or underlying")
    if position == "credit":
        if profit_fraction is None:
            uncalibrated.append("profit_fraction")
        share = DEFAULT_CREDIT_PROFIT_FRACTION if profit_fraction is None else _d(profit_fraction, "profit_fraction")
        if not ZERO < share < 1:
            raise ExitError("bad_target", "profit_fraction is above zero and below one")
        target_distance = ent * share
    else:
        target_distance = distance * r
    return _plan("SWO", side, ent, tk, distance, target_distance, basis, underlying_stop=under, uncalibrated=tuple(uncalibrated))


def stop_and_target(variant: Variant, **kwargs: Any) -> ExitPlan:
    """The exit of a line by its variant name: DT, SWC or SWO."""
    if variant == "DT":
        return dt_exit(**kwargs)
    if variant == "SWC":
        return swc_exit(**kwargs)
    if variant == "SWO":
        return swo_exit(**kwargs)
    raise ExitError("bad_variant", "variant is DT, SWC or SWO")


# ---- trailing stops: one implementation for the desk and the backtest -------------------------------------------------------

@dataclass(frozen=True)
class TrailingStop:
    """The state of a trailing stop after one price. Feed `stop` and `extreme` back in as `previous_stop` and `extreme`."""
    side: Side
    stop: Decimal
    extreme: Decimal                     # the best price seen since the entry: the highest for a long, the lowest for a short
    stopped: bool                        # the price touched or crossed the stop
    basis: str                           # "atr" or "pct"
    distance: Decimal                    # from the extreme to the stop before rounding


def _trail(basis: str, side: Side, price: Any, distance: Decimal, extreme: Any, previous_stop: Any, tick: Any) -> TrailingStop:
    if side not in ("long", "short"):
        raise ExitError("bad_side", "side is long or short")
    tk, px = _tick(tick), _d(price, "price")
    if px <= 0:
        raise ExitError("bad_price", "the price must be above zero")
    best = px if extreme is None else _d(extreme, "extreme")
    if best <= 0:
        raise ExitError("bad_price", "the extreme must be above zero")
    previous = None if previous_stop is None else _d(previous_stop, "previous_stop")
    if previous is not None and previous <= 0:
        raise ExitError("bad_stop", "the previous stop must be above zero")
    best = max(best, px) if side == "long" else min(best, px)
    # A long stop sits below the best price and is rounded down; a short one sits above and is rounded up: the risk is never understated.
    raw = best - distance if side == "long" else best + distance
    level = _round(raw, tk, "down" if side == "long" else "up")
    if previous is not None:
        level = max(level, previous) if side == "long" else min(level, previous)
    if level <= 0:
        raise ExitError("stop_not_positive", f"the stop would be at {level}: there is no price to leave at above zero")
    stopped = px <= level if side == "long" else px >= level
    return TrailingStop(side, level, best, stopped, basis, distance)


def atr_trailing(side: Side, price: Any, *, atr: Any, atr_multiple: Any = Decimal(3), extreme: Any = None, previous_stop: Any = None,
                 tick: Any = None) -> TrailingStop:
    """A stop that follows the best price by `atr_multiple` times the ATR (DT: the ATR of short bars; SWC: the daily ATR).

    The stop only moves in the favourable direction: for a long it never goes down, for a short it never goes up, even if the ATR
    grows. Pass the first `extreme` as the entry price; afterwards pass back the `extreme` and the `stop` of the last result."""
    a, m = _d(atr, "atr"), _d(atr_multiple, "atr_multiple")
    if a <= 0 or m <= 0:
        raise ExitError("bad_stop", "the ATR and its multiple must be above zero")
    return _trail("atr", side, price, a * m, extreme, previous_stop, tick)


def trailing_pct(side: Side, price: Any, *, pct: Any, extreme: Any = None, previous_stop: Any = None, tick: Any = None) -> TrailingStop:
    """A stop `pct` percent away from the best price of the position (5 means 5 percent). It never moves against the position.

    The extreme is the highest price for a long and the lowest for a short. The distance is taken from that extreme."""
    p = _d(pct, "pct")
    if not ZERO < p < 100:
        raise ExitError("bad_stop", "pct is above zero and below 100")
    px = _d(price, "price")
    best = px if extreme is None else _d(extreme, "extreme")
    best = max(best, px) if side == "long" else min(best, px)
    return _trail("pct", side, price, best * p / 100, extreme, previous_stop, tick)

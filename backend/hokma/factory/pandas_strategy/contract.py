"""The format of a strategy in pandas (1.3.0, task 3433).

A strategy is a text that defines two things and nothing else of its own:

    PARAMS = {"fast": {"default": 9, "min": 3, "max": 30, "step": 1, "type": "int"}}
    def generate_signals(df, params): ...        # returns a DataFrame with the same index as df

`df` has the columns open, high, low, close, volume, session_id and is_rth; its index is the open time of each bar in UTC. The answer has the columns
entry_long, entry_short, exit_long and exit_short (true or false) and stop, a PRICE (not a distance) that the strategy fixes on the bar of its signal, and may have target
and limit_price. The signal of a bar is made with its close and the order goes at the open of the next bar. The timeframe is a parameter of the run, never of the strategy.

Every problem is a `Problem`: the rule that was broken, the line or the field, the message and the exact change.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

import numpy as np

from hokma.factory.params import Param, ParamSchema

NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
MAX_PARAMS = 12
PARAM_KEYS = ("default", "min", "max", "step", "type")
KINDS = ("int", "float")
DATA_COLUMNS = ("open", "high", "low", "close", "volume", "session_id", "is_rth")
FLAGS = ("entry_long", "entry_short", "exit_long", "exit_short")
LEVELS = ("stop",)
OPTIONAL = ("target", "limit_price")
SIGNAL_COLUMNS = (*FLAGS, *LEVELS, *OPTIONAL)
MAX_BARS = 2_000_000


@dataclass(frozen=True)
class Problem:
    """A refusal that tells how to fix it: no verdict without the rule, the place and the change."""
    rule: str
    message: str
    fix: str
    line: int | None = None
    field: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {k: v for k, v in (("rule", self.rule), ("line", self.line), ("field", self.field), ("message", self.message), ("fix", self.fix)) if v is not None}

    def text(self) -> str:
        where = f" line {self.line}" if self.line is not None else (f" {self.field}" if self.field else "")
        return f"[{self.rule}]{where}: {self.message} Fix: {self.fix}"


class ContractError(ValueError):
    """One or more problems of the format. `problems` is the list."""

    def __init__(self, problems: list[Problem]) -> None:
        self.problems = problems
        super().__init__("; ".join(p.text() for p in problems[:3]))


@dataclass(frozen=True)
class ParamDef:
    name: str
    default: float
    min: float
    max: float
    step: float
    kind: str

    def coerce(self, value: Any) -> int | float:
        return int(round(value)) if self.kind == "int" else float(value)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_params(raw: Any) -> tuple[ParamDef, ...]:
    """PARAMS as the definitions of the parameters, or ContractError with every problem found."""
    problems: list[Problem] = []
    if not isinstance(raw, dict) or not raw:
        raise ContractError([Problem("params_shape", "PARAMS must be a dict with at least one parameter.", 'Write PARAMS = {"fast": {"default": 9, "min": 3, "max": 30, "step": 1, "type": "int"}}.', field="PARAMS")])
    if len(raw) > MAX_PARAMS:
        problems.append(Problem("params_shape", f"PARAMS has {len(raw)} parameters; the most is {MAX_PARAMS}.", f"Keep the {MAX_PARAMS} that matter and make the rest constants in the code.", field="PARAMS"))
    out: list[ParamDef] = []
    for name, spec in raw.items():
        place = f"PARAMS.{name}"
        if not isinstance(name, str) or not NAME.match(name):
            problems.append(Problem("params_name", f"the parameter name {name!r} is not valid.", "Use lower case letters, digits and underscores, starting with a letter (fast_period).", field=place))
            continue
        if not isinstance(spec, dict):
            problems.append(Problem("params_shape", f"{place} must be a dict with default, min, max, step and type.", f'Write "{name}": {{"default": 9, "min": 3, "max": 30, "step": 1, "type": "int"}}.', field=place))
            continue
        extra, missing = sorted(set(spec) - set(PARAM_KEYS)), [k for k in PARAM_KEYS if k not in spec]
        if extra or missing:
            problems.append(Problem("params_keys", f"{place} has " + (f"the unknown keys {', '.join(map(str, extra))}" if extra else "") + (" and " if extra and missing else "") + (f"is missing {', '.join(missing)}" if missing else "") + ".",
                                    f"{place} has exactly the keys {', '.join(PARAM_KEYS)}.", field=place))
            continue
        kind = spec["type"]
        if kind not in KINDS:
            problems.append(Problem("params_type", f"{place}.type is {kind!r}.", 'type is "int" or "float", as text.', field=f"{place}.type"))
            continue
        bad = [k for k in ("default", "min", "max", "step") if not _number(spec[k])]
        if bad:
            problems.append(Problem("params_number", f"{place}.{bad[0]} is {spec[bad[0]]!r}, not a finite number.", f"Write a number in {place}.{bad[0]}.", field=f"{place}.{bad[0]}"))
            continue
        low, high, step, default = spec["min"], spec["max"], spec["step"], spec["default"]
        if kind == "int" and any(float(spec[k]) != int(spec[k]) for k in ("default", "min", "max", "step")):
            problems.append(Problem("params_int", f"{place} is an int but one of its numbers has decimals.", f'Use whole numbers in {place} or change its type to "float".', field=place))
            continue
        if step <= 0:
            problems.append(Problem("params_step", f"{place}.step is {step}; it must be above zero.", f"Set {place}.step to the smallest change worth trying (for example 1).", field=f"{place}.step"))
            continue
        if low > high:
            problems.append(Problem("params_range", f"{place}.min ({low}) is above max ({high}).", "Swap them.", field=place))
            continue
        if not low <= default <= high:
            problems.append(Problem("params_default", f"{place}.default ({default}) is outside min..max ({low}..{high}).", f"Set default between {low} and {high}.", field=f"{place}.default"))
            continue
        if int(math.floor((high - low) / step + 1e-9)) + 1 > 10_000:
            problems.append(Problem("params_step", f"{place} has more than 10,000 steps between min and max.", "Raise step or narrow min..max.", field=place))
            continue
        out.append(ParamDef(name, spec["default"], low, high, step, kind))
    if problems:
        raise ContractError(problems)
    return tuple(out)


def schema_of(defs: tuple[ParamDef, ...]) -> ParamSchema:
    """The ParamSchema of the factory: what a backtest validates its params against."""
    return ParamSchema(tuple(Param(d.name, d.kind, d.coerce(d.default), d.min, d.max) for d in defs))


def space_of(defs: tuple[ParamDef, ...], names: tuple[str, ...] | None = None) -> Any:
    """PARAMS as the search space of the optimizer: one axis for each parameter, from min to max by step. `names` keeps only some of them."""
    from hokma.backtest.optimize import Axis, ParamSpace

    chosen = [d for d in defs if names is None or d.name in names]
    return ParamSpace(tuple(Axis(d.name, d.kind, d.min, d.max, d.step) for d in chosen))


def defaults(defs: tuple[ParamDef, ...]) -> dict[str, int | float]:
    return {d.name: d.coerce(d.default) for d in defs}


def corner(defs: tuple[ParamDef, ...], which: str) -> dict[str, int | float]:
    """The point with every parameter at its min or at its max: the corners of the space must run too, or the optimizer meets an error in its first trials."""
    return {d.name: d.coerce(d.min if which == "min" else d.max) for d in defs}


def check_params(defs: tuple[ParamDef, ...], values: dict[str, Any]) -> dict[str, int | float]:
    """The values of a run checked against PARAMS (unknown, missing, out of range, wrong type)."""
    known = {d.name: d for d in defs}
    for key in values:
        if key not in known:
            raise ContractError([Problem("params_unknown", f"the parameter {key!r} is not in PARAMS.", f"Use one of {', '.join(known)}.", field=str(key))])
    out: dict[str, int | float] = {}
    for d in defs:
        value = values.get(d.name, d.default)
        if not _number(value) or not d.min <= value <= d.max:
            raise ContractError([Problem("params_range", f"{d.name} is {value!r}; it must be a number between {d.min} and {d.max}.", f"Pass {d.name} between {d.min} and {d.max}.", field=d.name)])
        out[d.name] = d.coerce(value)
    return out


# ------------------------------------------------------------------------------------------------------------------------------ the table of signals
@dataclass(frozen=True)
class Signals:
    """The signals as arrays, one value for each bar, in the order of the bars."""
    entry_long: np.ndarray
    entry_short: np.ndarray
    exit_long: np.ndarray
    exit_short: np.ndarray
    stop: np.ndarray
    target: np.ndarray | None = None
    limit_price: np.ndarray | None = None

    def __len__(self) -> int:
        return len(self.entry_long)

    def as_columns(self) -> dict[str, np.ndarray]:
        out = {c: getattr(self, c) for c in SIGNAL_COLUMNS if getattr(self, c) is not None}
        return out

    def column(self, name: str) -> np.ndarray:
        """A column as an array; a level the strategy does not give is NaN on every bar."""
        value = getattr(self, name)
        return np.full(len(self), np.nan) if value is None else value

    def same_as(self, other: "Signals", upto: int | None = None) -> bool:
        """The same decisions on the first `upto` bars (all by default); a level that is missing counts as NaN, and NaN equals NaN."""
        n = len(self) if upto is None else upto
        for name in SIGNAL_COLUMNS:
            a, b = self.column(name)[:n], other.column(name)[:n]
            if not np.array_equal(a.astype(float), b.astype(float), equal_nan=True):
                return False
        return True


def _flag(name: str, values: np.ndarray, n: int, problems: list[Problem]) -> np.ndarray:
    arr = np.asarray(values)
    if arr.shape != (n,):
        problems.append(Problem("signals_length", f"{name} has {arr.size} values for {n} bars.", f"Return {name} with the index of df.", field=name))
        return np.zeros(n, dtype=bool)
    if arr.dtype == bool:
        return arr
    if arr.dtype.kind in "fiu":
        f = arr.astype(float)
        f = np.where(np.isnan(f), 0.0, f)                      # a bar with no value yet (the start of a rolling window) is no signal
        if not np.all((f == 0.0) | (f == 1.0)):
            problems.append(Problem("signals_flag", f"{name} has values other than true or false.", f"Build {name} with a comparison (df['close'] > ema) or .astype(bool).", field=name))
        return f == 1.0
    problems.append(Problem("signals_flag", f"{name} has the type {arr.dtype}; it must be bool.", f"Build {name} with a comparison or .astype(bool).", field=name))
    return np.zeros(n, dtype=bool)


def _level(name: str, values: np.ndarray, n: int, problems: list[Problem]) -> np.ndarray:
    arr = np.asarray(values)
    if arr.shape != (n,) or arr.dtype.kind not in "fiu":
        problems.append(Problem("signals_level", f"{name} must be one number for each bar (NaN where there is none).", f"Return {name} as a float column with the index of df.", field=name))
        return np.full(n, np.nan)
    f = arr.astype(float)
    if np.any(np.isinf(f)):
        problems.append(Problem("signals_level", f"{name} has infinite values.", f"Leave {name} as NaN where there is no level.", field=name))
    return f


def build_signals(columns: dict[str, np.ndarray], close: np.ndarray, *, strict: bool = True) -> tuple[Signals, list[Problem]]:
    """The signals of the answer of a strategy, checked against the close of the bars: the shape of every column and the rules of an entry (it needs a stop, on the right side of the close).
    The problems are all the ones found; the signals are returned for what could be read."""
    n = len(close)
    problems: list[Problem] = []
    missing = [c for c in (*FLAGS, *LEVELS) if c not in columns]
    if missing:
        problems.append(Problem("signals_columns", f"the answer has no column {', '.join(missing)}.", f"Return a DataFrame with the columns {', '.join((*FLAGS, *LEVELS))} (and optionally {', '.join(OPTIONAL)}).", field=missing[0]))
    unknown = [c for c in columns if c not in SIGNAL_COLUMNS]
    if unknown:
        problems.append(Problem("signals_columns", f"the answer has the unknown column {', '.join(map(str, unknown))}.", f"Return only {', '.join(SIGNAL_COLUMNS)}.", field=str(unknown[0])))
    if missing:
        return Signals(*(np.zeros(n, dtype=bool) for _ in FLAGS), np.full(n, np.nan)), problems
    flags = {c: _flag(c, columns[c], n, problems) for c in FLAGS}
    stop = _level("stop", columns["stop"], n, problems)
    target = _level("target", columns["target"], n, problems) if "target" in columns else None
    limit = _level("limit_price", columns["limit_price"], n, problems) if "limit_price" in columns else None
    both = np.flatnonzero(flags["entry_long"] & flags["entry_short"])
    if both.size:
        problems.append(Problem("signals_both_sides", f"entry_long and entry_short are both true on {both.size} bars (the first is bar {int(both[0])}).", "A bar enters long or short, never both: make the two conditions exclusive.", field="entry_long"))
    entering = flags["entry_long"] | flags["entry_short"]
    no_stop = np.flatnonzero(entering & ~np.isfinite(stop))
    if no_stop.size:
        problems.append(Problem("signals_stop_missing", f"{no_stop.size} entries have no stop (the first is bar {int(no_stop[0])}).",
                                "Every entry carries its stop as a price on the same bar: stop = where('entry_long', close - 2 * atr, ...). Without a stop the entry is not valid.", field="stop"))
    wrong_long = np.flatnonzero(flags["entry_long"] & np.isfinite(stop) & ~(stop < close))
    wrong_short = np.flatnonzero(flags["entry_short"] & np.isfinite(stop) & ~(stop > close))
    if wrong_long.size:
        problems.append(Problem("signals_stop_side", f"a long entry has its stop at or above the close on {wrong_long.size} bars (the first is bar {int(wrong_long[0])}).", "The stop of a long is a price BELOW the close of the signal bar (close - 2 * atr).", field="stop"))
    if wrong_short.size:
        problems.append(Problem("signals_stop_side", f"a short entry has its stop at or below the close on {wrong_short.size} bars (the first is bar {int(wrong_short[0])}).", "The stop of a short is a price ABOVE the close of the signal bar (close + 2 * atr).", field="stop"))
    if target is not None:
        t_long = np.flatnonzero(flags["entry_long"] & np.isfinite(target) & ~(target > close))
        t_short = np.flatnonzero(flags["entry_short"] & np.isfinite(target) & ~(target < close))
        if t_long.size or t_short.size:
            problems.append(Problem("signals_target_side", "a target is on the wrong side of the close.", "The target of a long is above the close of the signal bar and the one of a short below it; use NaN for no target.", field="target"))
    if limit is not None:
        bad = np.flatnonzero(entering & np.isfinite(limit) & ~(limit > 0))
        if bad.size:
            problems.append(Problem("signals_limit", "limit_price has values that are not above zero.", "limit_price is a price above zero, or NaN for an entry at the next open.", field="limit_price"))
    return Signals(flags["entry_long"], flags["entry_short"], flags["exit_long"], flags["exit_short"], stop, target, limit), problems

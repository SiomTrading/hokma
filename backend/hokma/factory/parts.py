"""The parts of a composite strategy, built on the sizing, exit and slicing modules (SPEC-020: L-51).

A `Composite` is an alpha plus a sizer, an exit rule and an execution style. Each part here calls the one module that owns its calculation:
the number of contracts is `hokma.desks.trading.sizing.contracts` (exact arithmetic, never a moved stop), the stop and the target are
`hokma.desks.trading.exits` and the slices are `hokma.exec.algos.allocate`. So a strategy that passes a backtest reaches a desk with the same
pieces. The parts are listed in four registries (`SIZERS`, `EXITS`, `EXECUTORS`, `FILTERS`) with the schema of their parameters; a new part is one
`register` call and nothing here changes. A filter decides whether an entry may open on this bar (a session window, a regime, a maximum of
entries a day) and a control closes by the clock; neither blocks an exit. A parameter that sizes risk has no default: the caller states it.

A part that needs an indicator declares it with `requests()` and reads it by name; the library of the runner supplies it
(`hokma.factory.indicator_library`).
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import time as clock
from datetime import tzinfo
from decimal import Decimal
from typing import Any, Protocol

from hokma.contracts.identity import ContractSheet
from hokma.desks.core.signal import AlphaModel, Line, Signal
from hokma.desks.trading import exits, sizing
from hokma.factory.indicator_library import LIBRARY
from hokma.factory.orders import Price, Side, Size, SizeType, Unit, protective_price
from hokma.factory.params import Param, ParamError, ParamSchema, validate
from hokma.factory.strategy import (BadStrategy, BarContext, Composite, DayState, ExecutionStyle, IndicatorRequest, MarketExecutor,  # noqa: F401
                                    NoEntry)

ZERO = Decimal(0)
NO_ENTRY_EXIT_CODES = ("bad_stop", "stop_inside_tick", "stop_not_positive", "bad_target")     # a stop that cannot exist on this bar: no entry
NO_ENTRY_SIZE_CODES = ("stop_inside_tick",)


@dataclass(frozen=True)
class Env:
    """What a part needs to know about the contract it trades."""

    sheet: ContractSheet
    line: Line
    exit_rule: Any = None            # the exit rule already built: a sizer by risk reads the stop from it


@dataclass(frozen=True)
class PartKind:
    name: str
    schema: ParamSchema
    make: Callable[[Mapping[str, Any], Env], Any]
    summary: str
    normalize: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None     # a filter checks its own parameters (text, lists, optional bounds) here


SIZERS: dict[str, PartKind] = {}
EXITS: dict[str, PartKind] = {}
EXECUTORS: dict[str, PartKind] = {}
FILTERS: dict[str, PartKind] = {}


def register(table: dict[str, PartKind], kind: PartKind) -> PartKind:
    """Add a part to a registry. A name is used once."""
    if kind.name in table:
        raise ValueError(f"{kind.name!r} is already registered")
    table[kind.name] = kind
    return kind


def build_part(table: dict[str, PartKind], what: str, name: str, params: Mapping[str, Any] | None, env: Env) -> Any:
    """The part `name` with its parameters checked against its schema. An unknown name or a wrong parameter is BadStrategy."""
    kind = table.get(name)
    if kind is None:
        raise BadStrategy(f"unknown {what} {name!r}; the registered ones are {', '.join(sorted(table)) or 'none'}")
    if kind.normalize is not None:                       # a filter checks its own parameters
        return kind.make(kind.normalize(dict(params or {})), env)
    try:
        values = validate(kind.schema, params)
    except ParamError as exc:
        raise BadStrategy(f"{what} {name}: {exc}") from exc
    return kind.make(values, env)


def _close(ctx: BarContext) -> Decimal:
    return Decimal(repr(float(ctx.series("close").last)))


def _long(side: Side) -> exits.Side:
    return "long" if side == "buy" else "short"


# ---- sizers ----

class FixedContracts:
    """The same number of contracts on every entry."""

    def __init__(self, n: int) -> None:
        self.n = n

    def size(self, *, signal: Signal, ctx: BarContext) -> Size:
        return Size(SizeType.CONTRACTS, self.n)


class FixedRisk:
    """The contracts that risk `risk_pct` percent of the equity between the entry (the last close) and the stop of the exit rule.

    The count is `hokma.desks.trading.sizing.fixed_risk` (over `contracts`): the exact floor of the budget over the risk of one contract. When not
    even one contract fits, there is no entry: the stop is never moved to make one fit. An exit rule with no stop gives no entry either.
    """

    def __init__(self, risk_pct: float, exit_rule: Any, sheet: ContractSheet) -> None:
        self.risk_pct, self.exit_rule, self.sheet = Decimal(repr(float(risk_pct))), exit_rule, sheet

    def size(self, *, signal: Signal, ctx: BarContext) -> Size | None:
        side: Side = "buy" if float(signal.value or 0.0) > 0 else "sell"
        stop_unit, _ = self.exit_rule.levels(side=side, ctx=ctx)
        if stop_unit is None:
            raise NoEntry("the exit rule gives no stop: a size by risk needs one")
        entry = _close(ctx)
        stop = protective_price(stop_unit, side=side, kind="sl", entry=entry, sheet=self.sheet)
        try:
            result = sizing.fixed_risk(risk_usd=ctx.equity * self.risk_pct / 100, entry=entry, multiplier=self.sheet.multiplier,
                                       stop_distance=abs(entry - stop), tick_size=self.sheet.tick_size, side=_long(side), currency=self.sheet.currency)
        except sizing.SizingError as exc:
            if exc.code in NO_ENTRY_SIZE_CODES:
                raise NoEntry(str(exc)) from exc
            raise
        if result.status != "OK":
            raise NoEntry(result.reason)
        return Size(SizeType.CONTRACTS, result.contracts)


class VolTarget:
    """The contracts that carry the target volatility of the equity (`hokma.desks.trading.sizing.vol_target`, seven audited steps). The volatility of
    the instrument is the exponentially weighted one of H3, read from the library as the indicator `sizer_vol`. The sign of the view sets the side
    and the size is the full scalar: the conviction does not scale it. A size under one contract is no entry."""

    def __init__(self, target_vol: float, decay_pct: int, periods_per_year: int, sheet: ContractSheet) -> None:
        self.target_vol, self.decay_pct, self.periods, self.sheet = Decimal(repr(float(target_vol))), decay_pct, periods_per_year, sheet

    def requests(self) -> tuple[IndicatorRequest, ...]:
        return (IndicatorRequest("sizer_vol", "ewma_vol", "close", (("decay_pct", self.decay_pct),), 21),)

    def size(self, *, signal: Signal, ctx: BarContext) -> Size | None:
        sigma = float(ctx.series("sizer_vol").last)
        if not math.isfinite(sigma) or sigma <= 0:
            raise NoEntry("the volatility of the instrument is not available yet or is zero")
        result = sizing.vol_target(capital=ctx.equity, target_vol=self.target_vol, price=_close(ctx), instrument_vol=Decimal(repr(sigma)),
                                   multiplier=self.sheet.multiplier, periods_per_year=self.periods, currency=self.sheet.currency)
        if result.contracts == 0:
            raise NoEntry("the volatility target does not buy one contract")
        return Size(SizeType.CONTRACTS, abs(result.contracts))


# ---- exit rules ----

class NoExit:
    """No stop and no target: the alpha decides when to leave."""

    protective = False

    def levels(self, *, side: Side, ctx: BarContext) -> tuple[Unit | None, Unit | None]:
        return None, None


class AtrExit:
    """A stop at `stop_atr` times the ATR from the entry and a target `r_multiple` times the stop distance, from `hokma.desks.trading.exits`."""

    def __init__(self, length: int, stop_atr: float, r_multiple: float, sheet: ContractSheet) -> None:
        self.length, self.stop_atr, self.r_multiple, self.sheet = length, Decimal(repr(float(stop_atr))), Decimal(repr(float(r_multiple))), sheet

    def requests(self) -> tuple[IndicatorRequest, ...]:
        return (IndicatorRequest("exit_atr", "atr", "bars", (("length", self.length),), self.length + 1),)

    def levels(self, *, side: Side, ctx: BarContext) -> tuple[Unit | None, Unit | None]:
        atr = float(ctx.series("exit_atr").last)
        if not math.isfinite(atr) or atr <= 0:
            raise NoEntry("the ATR is not available yet or is zero")
        try:
            plan = exits.swc_exit(_long(side), _close(ctx), atr=Decimal(repr(atr)), atr_multiple=self.stop_atr, tick=self.sheet.tick_size,
                                  r_multiple=self.r_multiple)
        except exits.ExitError as exc:
            if exc.code in NO_ENTRY_EXIT_CODES:
                raise NoEntry(str(exc)) from exc
            raise
        return Price(plan.stop), Price(plan.target)


class _Trailing:
    """A stop that follows the best price of the position (the one implementation of `hokma.desks.trading.exits`). It places the first stop at the
    entry, as the stop of the order, and from then on `manage` moves it only in the favourable direction; when the close touches it the composite
    leaves at the next open. The state holds the best price and the last stop."""

    def __init__(self, sheet: ContractSheet) -> None:
        self.sheet = sheet

    def _trail(self, side: Side, price: Decimal, state: Any, ctx: BarContext) -> exits.TrailingStop:
        raise NotImplementedError

    def levels(self, *, side: Side, ctx: BarContext) -> tuple[Unit | None, Unit | None]:
        try:
            first = self._trail(side, _close(ctx), None, ctx)
        except exits.ExitError as exc:
            if exc.code in NO_ENTRY_EXIT_CODES:
                raise NoEntry(str(exc)) from exc
            raise
        return Price(first.stop), None

    def manage(self, *, side: Side, ctx: BarContext, state: Any) -> tuple[bool, Any]:
        try:
            step = self._trail(side, _close(ctx), state, ctx)
        except (exits.ExitError, NoEntry):
            return False, state                      # no ATR or no stop on this bar: the last stop stands and nothing is moved
        return step.stopped, step


class AtrTrailing(_Trailing):
    """A trailing stop `atr_multiple` ATR from the best price (`exits.atr_trailing`)."""

    def __init__(self, length: int, atr_multiple: float, sheet: ContractSheet) -> None:
        super().__init__(sheet)
        self.length, self.multiple = length, Decimal(repr(float(atr_multiple)))

    def requests(self) -> tuple[IndicatorRequest, ...]:
        return (IndicatorRequest("exit_atr", "atr", "bars", (("length", self.length),), self.length + 1),)

    def _trail(self, side: Side, price: Decimal, state: Any, ctx: BarContext) -> exits.TrailingStop:
        atr = float(ctx.series("exit_atr").last)
        if not math.isfinite(atr) or atr <= 0:
            raise NoEntry("the ATR is not available yet or is zero")
        return exits.atr_trailing(_long(side), price, atr=Decimal(repr(atr)), atr_multiple=self.multiple, tick=self.sheet.tick_size,
                                  extreme=None if state is None else state.extreme, previous_stop=None if state is None else state.stop)


class PctTrailing(_Trailing):
    """A trailing stop `pct` percent from the best price (`exits.trailing_pct`)."""

    def __init__(self, pct: float, sheet: ContractSheet) -> None:
        super().__init__(sheet)
        self.pct = Decimal(repr(float(pct)))

    def _trail(self, side: Side, price: Decimal, state: Any, ctx: BarContext) -> exits.TrailingStop:
        return exits.trailing_pct(_long(side), price, pct=self.pct, tick=self.sheet.tick_size,
                                  extreme=None if state is None else state.extreme, previous_stop=None if state is None else state.stop)


class TicksExit:
    """A stop `stop_ticks` ticks from the entry and a target `r_multiple` times that distance, from `hokma.desks.trading.exits`."""

    def __init__(self, stop_ticks: int, r_multiple: float, sheet: ContractSheet) -> None:
        self.stop_ticks, self.r_multiple, self.sheet = stop_ticks, Decimal(repr(float(r_multiple))), sheet

    def levels(self, *, side: Side, ctx: BarContext) -> tuple[Unit | None, Unit | None]:
        try:
            plan = exits.dt_exit(_long(side), _close(ctx), stop_ticks=self.stop_ticks, tick=self.sheet.tick_size, r_multiple=self.r_multiple)
        except exits.ExitError as exc:
            if exc.code in NO_ENTRY_EXIT_CODES:
                raise NoEntry(str(exc)) from exc
            raise
        return Price(plan.stop), Price(plan.target)


# ---- execution ----

class SlicedExecutor:
    """A twap: the entry leaves in `n_slices` equal slices, one on each bar. A vwap has one weight per slice (a volume profile)."""

    def __init__(self, algo: str, weights: tuple[Decimal, ...]) -> None:
        self.algo, self.weights = algo, weights

    def style(self, *, side: Side, ctx: BarContext) -> ExecutionStyle:
        return ExecutionStyle(algo=self.algo, weights=self.weights)      # type: ignore[arg-type]


def twap(n_slices: int) -> SlicedExecutor:
    return SlicedExecutor("twap", (Decimal(1),) * n_slices)


def vwap(profile: tuple[Any, ...]) -> SlicedExecutor:
    """A volume profile: one volume per slice, from the caller (nothing is assumed)."""
    return SlicedExecutor("vwap", tuple(Decimal(repr(float(v))) for v in profile))


# ---- filters and controls (SPEC-030) ----

class EntryFilter(Protocol):
    """Decides whether an entry may open on this bar. It is never asked about an exit. May also have `requests()` (an indicator it reads)."""

    def allows(self, *, side: Side, ctx: BarContext, state: DayState) -> tuple[bool, str | None]: ...


class Control(Protocol):
    """Closes the position by the clock (`flat_by_time`)."""

    def must_flatten(self, *, ctx: BarContext) -> bool: ...


DEFAULT_ZONE = "America/New_York"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_FILTERS = 10
_CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _zone(name: Any) -> tzinfo:
    """The zone of an IANA name, from the zone database that ships with the installation (the `tzdata` package, which pandas requires). An unknown
    name is a bad strategy when the spec is checked, never a silent fixed offset when it runs. The market modules are not imported here: the
    factory has no road to them."""
    if not isinstance(name, str) or not name or len(name) > 64:
        raise BadStrategy("tz is the name of a time zone, for example America/New_York")
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:                                    # unknown name, or no zone database on this machine
        raise BadStrategy(f"tz {name!r} is not a known time zone") from None


def _clock(name: str, value: Any) -> clock:
    match = _CLOCK.match(value) if isinstance(value, str) else None
    if match is None:
        raise BadStrategy(f"{name} is a time of day written HH:MM, got {value!r}")
    return clock(int(match.group(1)), int(match.group(2)))


def _only(kind: str, params: Mapping[str, Any], allowed: tuple[str, ...]) -> None:
    extra = sorted(set(params) - set(allowed))
    if extra:
        raise BadStrategy(f"filter {kind}: unknown parameter {extra[0]!r}; allowed: {', '.join(allowed)}")


def _number(kind: str, name: str, value: Any, low: float, high: float, whole: bool = False) -> Any:
    try:
        return Param(name, "int" if whole else "float", None, low, high).coerce(value)
    except ParamError as exc:
        raise BadStrategy(f"filter {kind}: {exc}") from exc


def _local(ctx: BarContext, zone: tzinfo):
    from datetime import timezone

    as_of = ctx.as_of if ctx.as_of.tzinfo is not None else ctx.as_of.replace(tzinfo=timezone.utc)
    return as_of.astimezone(zone)


class SessionFilter:
    """Opens only in the window [start, end) of the clock in `tz`, on the listed days (Monday to Friday by default). A window with start after end
    crosses midnight; the day tested is the one of the bar in the zone."""

    kind = "session"

    def __init__(self, tz: str, start: str, end: str, days: list[str]) -> None:
        self.tz, self.zone, self.start, self.end, self.days = tz, _zone(tz), _clock("start", start), _clock("end", end), frozenset(days)

    def allows(self, *, side: Side, ctx: BarContext, state: DayState) -> tuple[bool, str | None]:
        now = _local(ctx, self.zone)
        if WEEKDAYS[now.weekday()] not in self.days:
            return False, f"{WEEKDAYS[now.weekday()]} is not a trading day of the filter"
        at = now.time().replace(second=0, microsecond=0)
        inside = self.start <= at < self.end if self.start < self.end else at >= self.start or at < self.end
        return inside, None if inside else f"{at:%H:%M} is outside {self.start:%H:%M}-{self.end:%H:%M}"


class MaxTradesPerDay:
    """Opens only while the entries of the day in `tz` are fewer than `n`. A reversal is one entry."""

    kind = "max_trades_per_day"

    def __init__(self, tz: str, n: int) -> None:
        self.tz, self.zone, self.n = tz, _zone(tz), n

    def allows(self, *, side: Side, ctx: BarContext, state: DayState) -> tuple[bool, str | None]:
        ok = state.entries < self.n
        return ok, None if ok else f"{state.entries} entries today, the maximum is {self.n}"


class RegimeAdx:
    """Opens only when the ADX is inside [min, max] (a bound that was not given is open). With no value yet it does not open."""

    kind = "regime_adx"

    def __init__(self, length: int, low: float | None, high: float | None) -> None:
        self.length, self.low, self.high = length, low, high

    def requests(self) -> tuple[IndicatorRequest, ...]:
        return (IndicatorRequest("filter_adx", "adx", "bars", (("length", self.length),), 2 * self.length),)

    def allows(self, *, side: Side, ctx: BarContext, state: DayState) -> tuple[bool, str | None]:
        value = float(ctx.series("filter_adx").last)
        if not math.isfinite(value):
            return False, "the ADX is not available yet"
        ok = (self.low is None or value >= self.low) and (self.high is None or value <= self.high)
        return ok, None if ok else f"ADX {value:.1f} is outside the range"


class RegimeVol:
    """Opens only when the annualized EWMA volatility of the closes is inside [min, max]. With no value yet it does not open."""

    kind = "regime_vol"

    def __init__(self, decay_pct: int, periods_per_year: int, low: float | None, high: float | None) -> None:
        self.decay_pct, self.periods, self.low, self.high = decay_pct, periods_per_year, low, high

    def requests(self) -> tuple[IndicatorRequest, ...]:
        return (IndicatorRequest("filter_vol", "ewma_vol", "close", (("decay_pct", self.decay_pct),), 21),)

    def allows(self, *, side: Side, ctx: BarContext, state: DayState) -> tuple[bool, str | None]:
        value = float(ctx.series("filter_vol").last) * math.sqrt(self.periods)
        if not math.isfinite(value):
            return False, "the volatility is not available yet"
        ok = (self.low is None or value >= self.low) and (self.high is None or value <= self.high)
        return ok, None if ok else f"annualized volatility {value:.3f} is outside the range"


class FlatByTime:
    """From `time` in `tz` the position is closed and nothing new opens until the date of the zone changes."""

    kind = "flat_by_time"

    def __init__(self, tz: str, at: str) -> None:
        self.tz, self.zone, self.at = tz, _zone(tz), _clock("time", at)

    def _late(self, ctx: BarContext) -> bool:
        return _local(ctx, self.zone).time().replace(second=0, microsecond=0) >= self.at

    def must_flatten(self, *, ctx: BarContext) -> bool:
        return self._late(ctx)

    def allows(self, *, side: Side, ctx: BarContext, state: DayState) -> tuple[bool, str | None]:
        late = self._late(ctx)
        return not late, f"it is past {self.at:%H:%M}" if late else None


def _norm_session(params: Mapping[str, Any]) -> dict[str, Any]:
    _only("session", params, ("tz", "start", "end", "days"))
    for key in ("start", "end"):
        if key not in params:
            raise BadStrategy(f"filter session needs {key}")
    tz = params.get("tz", DEFAULT_ZONE)
    _zone(tz)
    start, end = _clock("start", params["start"]), _clock("end", params["end"])
    if start == end:
        raise BadStrategy("filter session: start and end are the same time")
    days = params.get("days", list(WEEKDAYS[:5]))
    if not isinstance(days, list) or not days or any(d not in WEEKDAYS for d in days) or len(set(days)) != len(days):
        raise BadStrategy(f"filter session: days is a list of {', '.join(WEEKDAYS)}, each once")
    return {"tz": tz, "start": params["start"], "end": params["end"], "days": [d for d in WEEKDAYS if d in days]}


def _norm_max_trades(params: Mapping[str, Any]) -> dict[str, Any]:
    _only("max_trades_per_day", params, ("n", "tz"))
    if "n" not in params:
        raise BadStrategy("filter max_trades_per_day needs n")
    tz = params.get("tz", DEFAULT_ZONE)
    _zone(tz)
    return {"n": _number("max_trades_per_day", "n", params["n"], 1, 100, True), "tz": tz}


def _bounds(kind: str, params: Mapping[str, Any], low: float, high: float) -> dict[str, Any]:
    if "min" not in params and "max" not in params:
        raise BadStrategy(f"filter {kind} needs min, max or both")
    out = {key: _number(kind, key, params[key], low, high) for key in ("min", "max") if key in params}
    if "min" in out and "max" in out and out["min"] > out["max"]:
        raise BadStrategy(f"filter {kind}: min is above max")
    return out


def _norm_adx(params: Mapping[str, Any]) -> dict[str, Any]:
    _only("regime_adx", params, ("length", "min", "max"))
    if "length" not in params:
        raise BadStrategy("filter regime_adx needs length")
    return {"length": _number("regime_adx", "length", params["length"], 2, 500, True), **_bounds("regime_adx", params, 0.0, 100.0)}


def _norm_vol(params: Mapping[str, Any]) -> dict[str, Any]:
    _only("regime_vol", params, ("decay_pct", "periods_per_year", "min", "max"))
    for key in ("decay_pct", "periods_per_year"):
        if key not in params:
            raise BadStrategy(f"filter regime_vol needs {key}")
    return {"decay_pct": _number("regime_vol", "decay_pct", params["decay_pct"], 50, 99, True),
            "periods_per_year": _number("regime_vol", "periods_per_year", params["periods_per_year"], 1, 366, True),
            **_bounds("regime_vol", params, 0.0, 10.0)}


def _norm_flat(params: Mapping[str, Any]) -> dict[str, Any]:
    _only("flat_by_time", params, ("time", "tz"))
    if "time" not in params:
        raise BadStrategy("filter flat_by_time needs time")
    tz = params.get("tz", DEFAULT_ZONE)
    _zone(tz)
    _clock("time", params["time"])
    return {"time": params["time"], "tz": tz}


def _filter_schema(*names: Param) -> ParamSchema:
    return ParamSchema(names)


def _p(name: str, kind: str, low: float | None = None, high: float | None = None) -> Param:
    return Param(name, kind, None, low, high)       # type: ignore[arg-type]


register(FILTERS, PartKind("session", _filter_schema(), lambda v, env: SessionFilter(v["tz"], v["start"], v["end"], v["days"]),
                           "Open only inside a window of the clock in a zone, on the listed days.", _norm_session))
register(FILTERS, PartKind("max_trades_per_day", _filter_schema(_p("n", "int", 1, 100)), lambda v, env: MaxTradesPerDay(v["tz"], v["n"]),
                           "Open only while the entries of the day are fewer than n.", _norm_max_trades))
register(FILTERS, PartKind("regime_adx", _filter_schema(_p("length", "int", 2, 500), _p("min", "float", 0.0, 100.0), _p("max", "float", 0.0, 100.0)),
                           lambda v, env: RegimeAdx(v["length"], v.get("min"), v.get("max")),
                           "Open only when the ADX is inside a range.", _norm_adx))
register(FILTERS, PartKind("regime_vol", _filter_schema(_p("decay_pct", "int", 50, 99), _p("periods_per_year", "int", 1, 366),
                                                        _p("min", "float", 0.0, 10.0), _p("max", "float", 0.0, 10.0)),
                           lambda v, env: RegimeVol(v["decay_pct"], v["periods_per_year"], v.get("min"), v.get("max")),
                           "Open only when the annualized volatility is inside a range.", _norm_vol))
register(FILTERS, PartKind("flat_by_time", _filter_schema(), lambda v, env: FlatByTime(v["tz"], v["time"]),
                           "Close the position at a time of day and open nothing more that day.", _norm_flat))


register(SIZERS, PartKind("fixed_contracts", ParamSchema((_p("n", "int", 1, sizing.HARD_MAX_CONTRACTS),)),
                          lambda v, env: FixedContracts(v["n"]), "The same number of contracts on every entry."))
register(SIZERS, PartKind("fixed_risk", ParamSchema((_p("risk_pct", "float", 0.001, 100.0),)),
                          lambda v, env: FixedRisk(v["risk_pct"], env.exit_rule, env.sheet),
                          "Contracts that risk a percent of the equity to the stop of the exit rule (exact floor)."))
register(SIZERS, PartKind("vol_target", ParamSchema((_p("target_vol", "float", 0.001, 5.0), _p("decay_pct", "int", 50, 99), _p("periods_per_year", "int", 1, 366))),
                          lambda v, env: VolTarget(v["target_vol"], v["decay_pct"], v["periods_per_year"], env.sheet),
                          "Contracts that carry a target volatility of the equity (exponentially weighted volatility of the closes)."))
register(EXITS, PartKind("none", ParamSchema(()), lambda v, env: NoExit(), "No stop and no target."))
register(EXITS, PartKind("atr", ParamSchema((_p("length", "int", 2, 500), _p("stop_atr", "float", 0.1, 50.0), _p("r_multiple", "float", 0.1, 20.0))),
                         lambda v, env: AtrExit(v["length"], v["stop_atr"], v["r_multiple"], env.sheet), "Stop at a multiple of the ATR, target at R times the stop."))
register(EXITS, PartKind("atr_trailing", ParamSchema((_p("length", "int", 2, 500), _p("atr_multiple", "float", 0.1, 50.0))),
                         lambda v, env: AtrTrailing(v["length"], v["atr_multiple"], env.sheet), "Stop that follows the best price at a multiple of the ATR."))
register(EXITS, PartKind("trailing_pct", ParamSchema((_p("pct", "float", 0.01, 99.0),)),
                         lambda v, env: PctTrailing(v["pct"], env.sheet), "Stop that follows the best price at a percent distance."))
register(EXITS, PartKind("ticks", ParamSchema((_p("stop_ticks", "int", 1, 100_000), _p("r_multiple", "float", 0.1, 20.0))),
                         lambda v, env: TicksExit(v["stop_ticks"], v["r_multiple"], env.sheet), "Stop in ticks, target at R times the stop."))
register(EXECUTORS, PartKind("market", ParamSchema(()), lambda v, env: MarketExecutor(), "Enter at market."))
register(EXECUTORS, PartKind("twap", ParamSchema((_p("n_slices", "int", 2, 100),)), lambda v, env: twap(v["n_slices"]),
                             "The entry leaves in equal slices, one on each bar."))

DEFAULT_PARTS: Mapping[str, Mapping[str, Any]] = {"sizer": {"kind": "fixed_contracts", "n": 1}, "exit": {"kind": "none"}, "execution": {"kind": "market"}}
SCALAR_KEYS = ("execution", "exit", "sizer")
PART_KEYS = frozenset({*SCALAR_KEYS, "filters"})


def _kind(spec: Mapping[str, Any], what: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(spec, Mapping) or not isinstance(spec.get("kind"), str):
        raise BadStrategy(f"{what} is a mapping with a kind")
    return spec["kind"], {k: v for k, v in spec.items() if k != "kind"}


def _filter_values(item: Any) -> tuple[str, dict[str, Any]]:
    """The kind of one filter and its parameters checked and with defaults filled in."""
    name, params = _kind(item, "a filter")
    kind = FILTERS.get(name)
    if kind is None:
        raise BadStrategy(f"unknown filter {name!r}; the registered ones are {', '.join(sorted(FILTERS))}")
    assert kind.normalize is not None
    return name, kind.normalize(params)


def validate_filters(filters: Any) -> list[dict[str, Any]]:
    """A list of 0 to 10 filters checked without building them. Two filters of one kind are an error, except the session windows, which are united.
    An empty list is the same as none, so a spec that names no filters keeps the hash it had before filters existed."""
    if not isinstance(filters, list):
        raise BadStrategy("filters is a list of mappings with a kind")
    if len(filters) > MAX_FILTERS:
        raise BadStrategy(f"filters has at most {MAX_FILTERS} entries")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in filters:
        name, values = _filter_values(item)
        if name in seen and name != "session":
            raise BadStrategy(f"the filter {name} appears twice: only session windows may repeat")
        seen.add(name)
        out.append({"kind": name, **values})
    return out


def validate_parts(parts: Any) -> dict[str, Any]:
    """The parts of a saved spec checked without building them: only the keys sizer, exit, execution and filters, each a kind of its registry with
    parameters that its schema accepts. Returns the plain values, with the defaults filled in, so two spellings of one choice hash alike. `filters`
    is in the result only when it names at least one filter."""
    if not isinstance(parts, Mapping):
        raise BadStrategy("parts is a mapping")
    out: dict[str, Any] = {}
    for key, table, what in (("sizer", SIZERS, "sizer"), ("exit", EXITS, "exit rule"), ("execution", EXECUTORS, "executor")):
        if key not in parts:
            continue
        name, params = _kind(parts[key], key)
        kind = table.get(name)
        if kind is None:
            raise BadStrategy(f"unknown {what} {name!r}; the registered ones are {', '.join(sorted(table))}")
        try:
            out[key] = {"kind": name, **validate(kind.schema, params)}
        except ParamError as exc:
            raise BadStrategy(f"{what} {name}: {exc}") from exc
    if "filters" in parts:
        chosen = validate_filters(parts["filters"])
        if chosen:
            out["filters"] = chosen
    extra = set(parts) - PART_KEYS
    if extra:
        raise BadStrategy(f"parts has the keys {', '.join(sorted(PART_KEYS))}")
    return out


def check_parts(parts: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    """The parts of a strategy with the defaults filled in. Only sizer, exit, execution and filters are keys; the first three are a mapping with its kind and
    filters is a list of them, present only when it names a filter."""
    raw = dict(parts or {})
    if set(raw) - PART_KEYS:
        raise BadStrategy(f"parts has the keys {', '.join(sorted(PART_KEYS))}")
    chosen: dict[str, Any] = {key: raw.get(key, DEFAULT_PARTS[key]) for key in SCALAR_KEYS}
    if raw.get("filters"):
        chosen["filters"] = validate_filters(list(raw["filters"]) if isinstance(raw["filters"], (list, tuple)) else raw["filters"])
    return chosen


def build_composite(alpha: AlphaModel, *, sheet: ContractSheet, desk_id: int, line: Line, parts: Mapping[str, Any] | None = None,
                    library: Mapping[str, Callable[..., Any]] | None = None, symbol: str | None = None, min_conviction: float = 0.0,
                    tag: str | None = None, horizon: str = "days", feeds: Mapping[str, Any] | None = None) -> Composite:
    """A composite of `alpha` with the named parts. The default is one contract, no stop and market entries: nothing is assumed about risk."""
    chosen = check_parts(parts)
    env = Env(sheet, line)
    name, params = _kind(chosen["exit"], "exit")
    exit_rule = build_part(EXITS, "exit rule", name, params, env)
    name, params = _kind(chosen["sizer"], "sizer")
    sizer = build_part(SIZERS, "sizer", name, params, Env(sheet, line, exit_rule))
    name, params = _kind(chosen["execution"], "execution")
    executor = build_part(EXECUTORS, "executor", name, params, env)
    filters = []
    for item in chosen.get("filters", ()):
        name, params = _kind(item, "a filter")
        filters.append(build_part(FILTERS, "filter", name, params, env))
    return Composite(alpha, sizer, exit_rule, executor, contract_id=sheet.contract_id, desk_id=desk_id, line=line, horizon=horizon, symbol=symbol,
                     min_conviction=min_conviction, library=LIBRARY if library is None else library, tag=tag, feeds=feeds, filters=filters)

"""The strategy contract of the factory (SPEC-020: B-01, B-02, B-17, L-51).

A strategy declares its parameters, its indicators and its warm-up in `init` and decides in `on_bar`, where it sees only closed
bars. It returns `OrderIntent` values and never receives a broker, an engine or a connector. `Composite` assembles a strategy
from an alpha model, a sizer, an exit rule and an execution style, each one replaceable.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np

from hokma.desks.core.hashing import content_hash
from hokma.desks.core.signal import AlphaModel, Line, Signal, SignalContext
from hokma.factory.orders import OrderIntent, Side, Size, SizeType, Unit, buy, reduce_intent, sell
from hokma.factory.params import ParamSchema, validate
from hokma.factory.strategy_errors import BadStrategy
from hokma.quant.errors import BadInput
from hokma.quant.limits import as_int, check_param

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
DATA_FIELDS = ("open", "high", "low", "close", "volume")
BARS = "bars"                                   # the source of an indicator that reads several fields: it receives the mapping of the revealed arrays


class LookAhead(IndexError):
    """A strategy tried to read a bar or an indicator value that is not closed yet."""

    code = "look_ahead"


class NoEntry(Exception):
    """A sizer or an exit rule says that no entry can be made on this bar (no ATR yet, a stop inside one tick, a budget that does not buy a
    contract). The composite places no order and the bar goes on; it is not an error of the run."""

    code = "no_entry"


# ---- B-02: gradual reveal ----

class Series:
    """The first `cursor` values of an array and nothing else. Index 0 is the oldest bar, -1 the last closed one."""

    __slots__ = ("_view",)

    def __init__(self, values: Any, cursor: int) -> None:
        array = np.asarray(values)
        if array.ndim != 1:
            raise ValueError("a series is one dimensional")
        count = as_int("cursor", cursor)
        if count < 0 or count > len(array):
            raise ValueError(f"cursor {count} is outside 0 to {len(array)}")
        view = array[:count]
        view.flags.writeable = False
        self._view = view

    def __len__(self) -> int:
        return len(self._view)

    def __getitem__(self, key: Any) -> Any:
        n = len(self._view)
        if isinstance(key, slice):
            for edge in (key.start, key.stop):
                if edge is not None and edge > n:
                    raise LookAhead(f"bars up to {edge} were requested: only {n} bars are closed")
            return self._view[key]
        index = as_int("index", key)
        if index >= n:
            raise LookAhead(f"bar {index} was requested: only {n} bars are closed (0 to {n - 1})")
        if index < -n:
            raise IndexError(f"index {index} is before the first bar")
        return self._view[index]

    def __iter__(self):
        return iter(self._view)

    def __array__(self, dtype: Any = None, copy: Any = None) -> np.ndarray:
        return np.array(self._view, dtype=dtype) if copy else self._view if dtype is None else self._view.astype(dtype)

    @property
    def last(self) -> Any:
        if not len(self._view):
            raise IndexError("no closed bar yet")
        return self._view[-1]

    def tail(self, n: int) -> np.ndarray:
        """A copy of the last `n` revealed values (all of them when fewer exist)."""
        count = as_int("n", n)
        return np.array(self._view[-count:] if count > 0 else self._view[:0])

    def to_numpy(self) -> np.ndarray:
        return np.array(self._view)


class Data:
    """The revealed fields of the bars: `data.close`, `data.high` and so on."""

    def __init__(self, fields: Mapping[str, Series]) -> None:
        self._fields = dict(fields)

    def __getattr__(self, name: str) -> Series:
        try:
            return self.__dict__["_fields"][name]
        except KeyError:
            raise AttributeError(f"the bars have no field {name!r}; available: {', '.join(self.__dict__.get('_fields', {}))}") from None

    def names(self) -> tuple[str, ...]:
        return tuple(self._fields)


@dataclass(frozen=True)
class Position:
    """A read only view of what the account holds. Positive quantity is long."""

    contract_id: str
    qty: Decimal
    avg_price: Decimal

    @property
    def side(self) -> Side | None:
        return "buy" if self.qty > 0 else "sell" if self.qty < 0 else None


@dataclass(frozen=True)
class BarContext:
    """What `on_bar` receives. There is no broker, engine or connector in it."""

    index: int                        # number of closed bars visible
    as_of: datetime                   # close time of the last closed bar
    contract_id: str
    data: Data
    indicators: Mapping[str, Series]
    params: Mapping[str, Any]
    position: Position | None
    equity: Decimal

    def series(self, name: str) -> Series:
        """A bar field or an indicator by name."""
        if name in self.indicators:
            return self.indicators[name]
        if name in self.data.names():
            return getattr(self.data, name)
        raise KeyError(f"no bar field or indicator named {name!r}")


# ---- B-17: indicators and warm-up ----

def warmup(lengths: Iterable[int]) -> int:
    """The bars a strategy waits before it operates: the longest indicator needs data (B-17)."""
    longest = 0
    for length in lengths:
        n = as_int("warmup", length)
        if n < 0:
            raise BadInput(f"a warm-up is zero or more bars, got {n}")
        check_param("warmup_size", n)
        longest = max(longest, n)
    return longest


@dataclass(frozen=True)
class IndicatorDecl:
    name: str
    fn: Callable[..., Any]            # plain function the runner supplies: fn(values, **kwargs) -> array of the same length; with source `bars`, values is the mapping of arrays
    source: str
    warmup: int
    kwargs: tuple[tuple[str, Any], ...] = ()


class InitContext:
    """What `init` receives: it declares indicators and the warm-up. It cannot see a bar."""

    def __init__(self, params: Mapping[str, Any]) -> None:
        self.params = dict(params)
        self._decls: dict[str, IndicatorDecl] = {}
        self._extra_warmup = 0

    def indicator(self, name: str, fn: Callable[..., Any], *, source: str = "close", warmup: int, **kwargs: Any) -> None:
        if not _NAME.match(name) or name in DATA_FIELDS:
            raise BadStrategy(f"indicator name {name!r} must be lower case letters, digits and underscores and not a bar field")
        if name in self._decls:
            raise BadStrategy(f"indicator {name!r} is declared twice")
        if source not in DATA_FIELDS and source != BARS and source not in self._decls:
            raise BadStrategy(f"indicator {name!r}: source {source!r} is not a bar field, `bars` or an earlier indicator")
        if not callable(fn):
            raise BadStrategy(f"indicator {name!r}: fn must be a plain function")
        for key, value in kwargs.items():
            check_param(key, value)
        self._decls[name] = IndicatorDecl(name, fn, source, self._positive(warmup), tuple(sorted(kwargs.items())))

    @staticmethod
    def _positive(value: int) -> int:
        n = warmup([value])
        if n < 1:
            raise BadStrategy("an indicator needs a warm-up of at least one bar")
        return n

    def set_warmup(self, bars: int) -> None:
        """A warm-up longer than the indicators need (a lookback the strategy reads directly)."""
        self._extra_warmup = warmup([bars])

    @property
    def indicators(self) -> tuple[IndicatorDecl, ...]:
        return tuple(self._decls.values())

    @property
    def warmup_bars(self) -> int:
        return warmup([self._extra_warmup, *(d.warmup for d in self._decls.values())])


# ---- B-01: the contract ----

@runtime_checkable
class Strategy(Protocol):
    params: ParamSchema

    def init(self, ctx: InitContext) -> None: ...

    def on_bar(self, ctx: BarContext) -> Sequence[OrderIntent]: ...


def check_strategy(obj: Any) -> ParamSchema:
    """The parameter schema of a strategy (an instance or a class), or BadStrategy naming what is missing."""
    name = obj.__name__ if isinstance(obj, type) else type(obj).__name__
    for method in ("init", "on_bar"):
        if not callable(getattr(obj, method, None)):
            raise BadStrategy(f"{name} does not implement {method}")
    schema = getattr(obj, "params", None)
    if not isinstance(schema, ParamSchema):
        schema = getattr(obj, "SCHEMA", None)
    if not isinstance(schema, ParamSchema):
        raise BadStrategy(f"{name} declares no ParamSchema (params or SCHEMA)")
    return schema


def _intents(result: Any, name: str) -> tuple[OrderIntent, ...]:
    if result is None:
        return ()
    if isinstance(result, OrderIntent) or not isinstance(result, (list, tuple)):
        raise BadStrategy(f"{name}.on_bar must return a list of OrderIntent")
    for item in result:
        if not isinstance(item, OrderIntent):
            raise BadStrategy(f"{name}.on_bar emitted {type(item).__name__}: a strategy emits only OrderIntent")
    return tuple(result)


def build_context(*, cursor: int, times: Sequence[datetime], contract_id: str, bars: Mapping[str, np.ndarray],
                  indicators: Mapping[str, np.ndarray], params: Mapping[str, Any], position: Position | None,
                  equity: Decimal) -> BarContext:
    """The context of the bar that has just closed: `cursor` bars are visible, no more."""
    if cursor < 1 or cursor > len(times):
        raise ValueError(f"cursor {cursor} is outside 1 to {len(times)}")
    return BarContext(cursor, times[cursor - 1], contract_id, Data({k: Series(v, cursor) for k, v in bars.items()}),
                      {k: Series(v, cursor) for k, v in indicators.items()}, dict(params), position, equity)


def _source_of(decl: IndicatorDecl, arrays: Mapping[str, np.ndarray], computed: Mapping[str, np.ndarray]) -> Any:
    if decl.source == BARS:
        return {k: arrays[k] for k in DATA_FIELDS if k in arrays}
    return arrays[decl.source] if decl.source in arrays else computed[decl.source]


def _length(source: Any) -> int:
    return len(next(iter(source.values()))) if isinstance(source, Mapping) else len(source)


def _head(source: Any, cut: int) -> Any:
    return {k: v[:cut] for k, v in source.items()} if isinstance(source, Mapping) else source[:cut]


def compute_indicator(decl: IndicatorDecl, arrays: Mapping[str, np.ndarray], computed: Mapping[str, np.ndarray]) -> np.ndarray:
    """The full array of one declared indicator over the bars: the runner and the dry run share this one function, so an indicator is computed
    the same way in both."""
    source = _source_of(decl, arrays, computed)
    n = _length(source)
    full = np.asarray(decl.fn(source, **dict(decl.kwargs)), dtype=float)
    if full.shape != (n,):
        raise BadStrategy(f"indicator {decl.name!r} returned {full.shape[0] if full.ndim else 0} values for {n} bars")
    return full


def _check_causal(decl: IndicatorDecl, source: Any, full: np.ndarray) -> None:
    """An indicator is causal when its value at a bar does not change if later bars are removed."""
    n = _length(source)
    for cut in sorted({max(decl.warmup, 1), max(n // 2, 1), max(n - 1, 1)}):
        if cut >= n:
            continue
        part = np.asarray(decl.fn(_head(source, cut), **dict(decl.kwargs)), dtype=float)
        if not np.allclose(part, full[:cut], equal_nan=True):
            raise LookAhead(f"indicator {decl.name!r} changes when later bars are removed: it uses future data")


@dataclass(frozen=True)
class ReplayResult:
    intents: tuple[tuple[int, tuple[OrderIntent, ...]], ...]    # (closed bars visible, intents emitted on that bar)
    warmup_bars: int


def replay_intents(strategy: Any, bars: Mapping[str, Any], *, times: Sequence[datetime], contract_id: str,
                   params: Mapping[str, Any] | None = None, equity: Decimal = Decimal(100000),
                   position_at: Callable[[int], Position | None] | None = None, causal_check: bool = True) -> ReplayResult:
    """A dry run of the contract: init, then `on_bar` on every bar after the warm-up. No fills, no costs.

    The backtest engine owns positions and fills; `position_at` lets a caller supply the position seen on each bar.
    """
    schema = check_strategy(strategy)
    name = type(strategy).__name__
    values = validate(schema, params)
    arrays = {k: np.asarray(v, dtype=float) for k, v in bars.items()}
    n = len(times)
    for key, array in arrays.items():
        if len(array) != n:
            raise ValueError(f"field {key!r} has {len(array)} values for {n} bars")
    init = InitContext(values)
    strategy.init(init)
    computed: dict[str, np.ndarray] = {}
    for decl in init.indicators:
        full = compute_indicator(decl, arrays, computed)
        if causal_check:
            _check_causal(decl, _source_of(decl, arrays, computed), full)
        computed[decl.name] = full
    wait = init.warmup_bars
    out: list[tuple[int, tuple[OrderIntent, ...]]] = []
    for cursor in range(max(wait, 1), n + 1):
        ctx = build_context(cursor=cursor, times=times, contract_id=contract_id, bars=arrays, indicators=computed, params=values,
                            position=None if position_at is None else position_at(cursor), equity=equity)
        emitted = _intents(strategy.on_bar(ctx), name)
        if emitted:
            out.append((cursor, emitted))
    return ReplayResult(tuple(out), wait)


# ---- L-51: composite ----

@dataclass(frozen=True)
class IndicatorRequest:
    """An indicator an alpha reads: `kind` names a function of the library the runner supplies."""

    name: str
    kind: str
    source: str = "close"
    params: tuple[tuple[str, int], ...] = ()
    warmup: int = 1


def apply_requests(ctx: InitContext, requests: Iterable[IndicatorRequest], library: Mapping[str, Callable[..., Any]]) -> None:
    """Declare the requested indicators in `init` with the functions of `library` (fn(values, **params))."""
    for req in requests:
        if req.kind not in library:
            raise BadStrategy(f"the indicator library has no {req.kind!r} (needed by {req.name!r})")
        ctx.indicator(req.name, library[req.kind], source=req.source, warmup=req.warmup, **dict(req.params))


@dataclass(frozen=True)
class ExecutionStyle:
    """How an entry leaves. `algo` (twap or vwap) cuts the entry in slices that leave one on each bar, one weight per slice (the same weight
    for a twap, the volume profile for a vwap): the sizes come from `hokma.exec.algos.allocate`, the same pure function the slicing of the
    desks uses, and the slices of a backtest have the same quantities as the ones a desk sends."""

    order_type: Literal["market", "limit", "stop", "stop_limit"] = "market"
    limit: Decimal | None = None
    stop: Decimal | None = None
    algo: Literal["twap", "vwap"] | None = None
    weights: tuple[Decimal, ...] = ()


class Sizer(Protocol):
    """How much. A sizer or an exit rule or an executor may also have `requests()`: the indicators it reads (an ATR, for example)."""

    def size(self, *, signal: Signal, ctx: BarContext) -> Size | None: ...


class ExitRule(Protocol):
    def levels(self, *, side: Side, ctx: BarContext) -> tuple[Unit | None, Unit | None]: ...


class Executor(Protocol):
    def style(self, *, side: Side, ctx: BarContext) -> ExecutionStyle: ...


def merged_requests(*owners: Any) -> tuple[IndicatorRequest, ...]:
    """The indicator requests of several parts, each name once. Two parts that ask for one name with different needs are refused."""
    seen: dict[str, IndicatorRequest] = {}
    for owner in owners:
        for req in tuple(getattr(owner, "requests", lambda: ())()):
            if seen.setdefault(req.name, req) != req:
                raise BadStrategy(f"two parts ask for the indicator {req.name!r} with different needs")
    return tuple(seen.values())


def _feed(values: np.ndarray) -> Callable[[Any], np.ndarray]:
    """A fact supplied whole, one value per bar of the run, that the alpha reads like an indicator. It must be point in time: the value at a bar
    uses only what was known at that bar (the strategy contract checks that the first bars do not change when later ones are cut)."""
    def feed(bars: Any) -> np.ndarray:
        return values[:len(bars)]

    return feed


@dataclass
class DayState:
    """What the composite counts for a filter of the day: the trading day (the calendar date in the zone of the filter) and the entries made in it.
    The composite keeps one per zone and starts it again when the date of the zone changes."""

    day: date | None = None
    entries: int = 0


class MarketExecutor:
    """Enter at market."""

    def style(self, *, side: Side, ctx: BarContext) -> ExecutionStyle:
        return ExecutionStyle()


class Composite:
    """A strategy made of an alpha (a H1 `AlphaModel`), a sizer, an exit rule and an execution style.

    The alpha gives a view in [-1, +1]; its sign is the direction. The sizer says how much, the exit rule where to leave and
    the executor how to enter. Replacing one part changes only what that part decides. The alpha is asked on every bar and
    the position held decides whether to enter, hold or reverse: an entry is made only when the account is flat.
    """

    def __init__(self, alpha: AlphaModel, sizer: Sizer, exit_rule: ExitRule, executor: Executor | None = None, *,
                 contract_id: str, desk_id: int, line: Line, horizon: str = "days", symbol: str | None = None,
                 min_conviction: float = 0.0, library: Mapping[str, Callable[..., Any]] | None = None, tag: str | None = None,
                 feeds: Mapping[str, Any] | None = None, filters: Sequence[Any] = ()) -> None:
        if not callable(getattr(alpha, "signal", None)):
            raise BadStrategy("the alpha must implement signal(ctx)")
        if not 0.0 <= min_conviction < 1.0:
            raise BadStrategy("min_conviction is in [0, 1)")
        self.alpha, self.sizer, self.exit_rule = alpha, sizer, exit_rule
        self.executor: Executor = executor or MarketExecutor()
        self.contract_id, self.desk_id, self.line, self.horizon = contract_id, desk_id, line, horizon
        self.symbol = symbol or contract_id
        self.min_conviction, self.library, self.tag = min_conviction, dict(library or {}), tag
        self.protective_orders = bool(getattr(exit_rule, "protective", True))      # an exit rule that gives no levels leaves no stop to place
        self.filters: tuple[Any, ...] = tuple(filters)                              # entry filters and controls (SPEC-030): they gate entries, never exits
        self.last_block: str | None = None                                         # why the last entry was not opened (diagnostics only)
        self._days: dict[str, DayState] = {}
        self._queue: list[OrderIntent] = []                                        # the slices of an entry that have not left yet
        self._trail: Any = None                                                    # the state of a trailing exit while a position lives
        self.feeds = {k: np.asarray(v, dtype=float) for k, v in (feeds or {}).items()}   # facts that are not made of the bars (fundamentals, ranks)
        self.params = alpha.SCHEMA if isinstance(getattr(alpha, "SCHEMA", None), ParamSchema) else ParamSchema(())     # the parameters of a run are the alpha's
        self._facts: tuple[str, ...] = tuple(getattr(alpha, "FACTS", ()))
        self._lookback = as_int("lookback", getattr(alpha, "lookback", 0))
        check_param("window", self._lookback)

    def init(self, ctx: InitContext) -> None:
        self._trail = None
        self._days, self.last_block = {}, None
        apply_requests(ctx, merged_requests(self.alpha, self.sizer, self.exit_rule, self.executor, *self.filters), self.library)
        for name, values in sorted(self.feeds.items()):
            ctx.indicator(name, _feed(values), warmup=1)
        ctx.set_warmup(self._lookback)
        self._queue = []

    def _signal(self, ctx: BarContext) -> Signal:
        facts: dict[str, Any] = {}
        for name in self._facts:
            series = ctx.series(name)
            facts[name] = series.tail(self._lookback) if self._lookback else series.to_numpy()
        digest = content_hash({"symbol": self.symbol, "as_of": ctx.as_of.isoformat(),
                               "facts": {k: [float(x) for x in v] for k, v in sorted(facts.items())}})
        return self.alpha.signal(SignalContext(source=f"alpha.{type(self.alpha).__name__.lower()}", desk_id=self.desk_id,
                                               line=self.line, symbol=self.symbol, as_of=ctx.as_of, horizon=self.horizon,  # type: ignore[arg-type]
                                               snapshot_hash=digest, facts=facts))

    def _managed_exit(self, ctx: BarContext) -> bool:
        """Whether an exit rule that follows the position (a trailing stop) says to leave now. `manage(side, ctx, state)` gives (stopped, state); the
        state is kept while the position lives and dropped when the account is flat."""
        manage = getattr(self.exit_rule, "manage", None)
        if manage is None:
            return False
        if ctx.position is None or ctx.position.qty == 0:
            self._trail = None
            return False
        stopped, self._trail = manage(side="buy" if ctx.position.qty > 0 else "sell", ctx=ctx, state=self._trail)
        return bool(stopped)

    def _day(self, flt: Any, ctx: BarContext) -> DayState:
        """The state of the day of a filter's zone, started again when the date of that zone is not the one it holds."""
        zone, key = getattr(flt, "zone", timezone.utc), str(getattr(flt, "tz", "UTC"))
        as_of = ctx.as_of if ctx.as_of.tzinfo is not None else ctx.as_of.replace(tzinfo=timezone.utc)
        today = as_of.astimezone(zone).date()
        state = self._days.get(key)
        if state is None or state.day != today:
            state = self._days[key] = DayState(today, 0)
        return state

    def _flatten(self, ctx: BarContext) -> bool:
        """Whether a control says to close the position now (a flat by time)."""
        return any(callable(getattr(f, "must_flatten", None)) and f.must_flatten(ctx=ctx) for f in self.filters)

    def _blocked(self, side: Side, ctx: BarContext) -> str | None:
        """The reason an entry cannot open on this bar, or None. Every filter is asked and all of them must let it through; the filters of one
        kind that may repeat (the session windows) are united: one of them is enough."""
        reasons: dict[str, list[str | None]] = {}
        for flt in self.filters:
            allows = getattr(flt, "allows", None)
            if not callable(allows):
                continue
            ok, why = allows(side=side, ctx=ctx, state=self._day(flt, ctx))
            reasons.setdefault(str(getattr(flt, "kind", type(flt).__name__)), []).append(None if ok else (why or "blocked"))
        for kind, found in reasons.items():
            if all(item is not None for item in found):
                return f"{kind}: {found[0]}"
        return None

    def _count_entry(self, ctx: BarContext) -> None:
        for state in {id(s): s for s in (self._day(f, ctx) for f in self.filters)}.values():
            state.entries += 1

    def on_bar(self, ctx: BarContext) -> tuple[OrderIntent, ...]:
        if self._managed_exit(ctx) and ctx.position is not None and ctx.position.side is not None:
            self._queue, self._trail = [], None
            return (reduce_intent(self.contract_id, ctx.position.side, abs(ctx.position.qty), tag="trail"),)
        if ctx.position is not None and ctx.position.side is not None and self._flatten(ctx):
            self._queue = []
            return (reduce_intent(self.contract_id, ctx.position.side, abs(ctx.position.qty), tag="flat_by_time"),)
        signal = self._signal(ctx)
        if signal.abstained or signal.value is None:
            self._queue = []                                       # no view, no more slices
            return ()
        value = float(signal.value)
        wanted = 0 if abs(value) <= self.min_conviction else 1 if value > 0 else -1
        held = 0 if ctx.position is None or ctx.position.qty == 0 else (1 if ctx.position.qty > 0 else -1)
        if self._queue:
            if wanted == (1 if self._queue[0].side == "buy" else -1):
                return (self._queue.pop(0),)                       # the next slice of the entry in course
            self._queue = []                                       # the view changed: the slices left are dropped
        if wanted == held:
            return ()
        out: list[OrderIntent] = []
        if held != 0 and ctx.position is not None and ctx.position.side is not None:
            out.append(reduce_intent(self.contract_id, ctx.position.side, abs(ctx.position.qty), tag="exit"))
        if wanted == 0:
            return tuple(out)
        side: Side = "buy" if wanted > 0 else "sell"
        self.last_block = self._blocked(side, ctx) if self.filters else None
        if self.last_block is not None:
            return tuple(out)                                      # an entry that a filter does not let through; the exit above is kept
        try:
            size = self.sizer.size(signal=signal, ctx=ctx)
            if size is None:
                return tuple(out)
            sl, tp = self.exit_rule.levels(side=side, ctx=ctx)
        except NoEntry:
            return tuple(out)
        style = self.executor.style(side=side, ctx=ctx)
        make = buy if side == "buy" else sell
        if style.algo is not None:
            first, *self._queue = self._slices(make, size, style, side, sl, tp, ctx)
            out.append(first)
            self._count_entry(ctx)
            return tuple(out)
        out.append(make(self.contract_id, size, line=self.line, limit=style.limit, stop=style.stop, sl=sl, tp=tp, tag=self.tag,
                        order_type=style.order_type))
        self._count_entry(ctx)
        return tuple(out)

    def _slices(self, make: Callable[..., OrderIntent], size: Size, style: ExecutionStyle, side: Side, sl: Unit | None, tp: Unit | None,
                ctx: BarContext) -> list[OrderIntent]:
        """The slices of an entry of `size` contracts, in the order they leave, each tagged with the fingerprint of the whole entry."""
        from hokma.exec.algos import ALGOS, AlgoError, allocate      # the pure part of the slicing: the module that sends orders is never imported

        if style.algo not in ALGOS or style.order_type != "market":
            raise BadStrategy("a sliced entry is a twap or a vwap of market orders")
        if size.kind is not SizeType.CONTRACTS:
            raise BadStrategy("a sliced entry needs a size in contracts: the sizer gives another unit")
        try:
            parts = allocate(size.value, style.weights)
        except AlgoError as exc:
            raise BadStrategy(str(exc)) from exc
        kept = [q for q in parts if q > 0]
        digest = content_hash({"contract": self.contract_id, "as_of": ctx.as_of.isoformat(), "side": side, "qty": str(size.value), "algo": style.algo})[:16]
        head = f"{self.tag}:" if self.tag else ""
        return [make(self.contract_id, Size(SizeType.CONTRACTS, q), line=self.line, sl=sl, tp=tp, tag=f"{head}{style.algo}_{i}_of_{len(kept)}_{digest}")
                for i, q in enumerate(kept, start=1)]

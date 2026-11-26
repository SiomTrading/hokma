"""The ONLY file that imports the backtest engine and the search library (SPEC-020, rule 4 and N-27).

Everything else in Hokma talks to the small typed wrapper below: build the venue, feed the synthetic ticks, run, read the fills.
Only the backtest modules of the engine are used here, never its adapters or its live node (test_no_live_path). The statistics of the
engine are not used: its portfolio analyzer is switched off at run time (section 4.8) because its cost grows with the square of the
number of trades. When the engine cannot be imported, or the hook of the analyzer is missing, every entry answers `engine_unavailable`."""
from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal

from hokma.backtest.fills import Decision, reject_prob, stop_first, stop_price, target_price
from hokma.backtest.spec import FillRules, SpecError
from hokma.backtest.ticks import BAR_NS, Bar, PositionSide, SyntheticTick, bar_to_ticks

ENGINE_UNAVAILABLE = "engine_unavailable"
SEARCH_UNAVAILABLE = "search_unavailable"
ANALYZER_HOOK = "_append_pnl_record"
Feed = Literal["fast", "ticks", "engine_default"]

try:                                    # a missing wheel (macOS Intel) is a state, not a crash
    import nautilus_trader
    from nautilus_trader.analysis import PortfolioAnalyzer
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.models import FillModel, OneTickSlippageFillModel, PerContractFeeModel
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.book import OrderBook
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.data import Bar as EngineBarData
    from nautilus_trader.model.data import BarType, BookOrder, TradeTick
    from nautilus_trader.model.enums import AccountType, AggressorSide, AssetClass, BookType, OmsType, OrderSide
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
    from nautilus_trader.model.instruments import FuturesContract
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy
    _IMPORT_ERROR: Exception | None = None
except Exception as exc:                # noqa: BLE001 - any failure to load the native engine means unavailable
    _IMPORT_ERROR = exc

try:                                    # the search library is a separate state: a grid or a refinement needs none of it
    import optuna
    optuna.logging.set_verbosity(optuna.logging.ERROR)
    _SEARCH_ERROR: Exception | None = None
except Exception as exc:                # noqa: BLE001 - any failure to load it means the TPE search is unavailable
    _SEARCH_ERROR = exc


class EngineUnavailable(RuntimeError):
    """The backtest engine is not usable here. `code` is the stable `engine_unavailable`."""

    code = ENGINE_UNAVAILABLE


def available() -> bool:
    return _IMPORT_ERROR is None


class SearchUnavailable(RuntimeError):
    """The TPE search library is not usable here. `code` is the stable `search_unavailable`."""

    code = SEARCH_UNAVAILABLE


def search_available() -> bool:
    return _SEARCH_ERROR is None


def require() -> None:
    if _IMPORT_ERROR is not None:
        raise EngineUnavailable(f"the backtest engine is not available on this machine: {type(_IMPORT_ERROR).__name__}")


def engine_version() -> str:
    require()
    return nautilus_trader.__version__


def hook_present() -> bool:
    """The analyzer has the method that the switch-off replaces (a library upgrade can move it)."""
    return available() and callable(PortfolioAnalyzer.__dict__.get(ANALYZER_HOOK))


_hook_lock = threading.Lock()
_hook_users = 0
_hook_original: Any = None


def hook_active() -> bool:
    return available() and PortfolioAnalyzer.__dict__.get(ANALYZER_HOOK) is _skip_pnl_record


def _skip_pnl_record(self: Any, *args: Any, **kwargs: Any) -> None:
    """Nothing is recorded: Hokma computes its own trades and statistics, never the ones of the engine (decision 1254)."""


@contextlib.contextmanager
def analyzer_off() -> Iterator[None]:
    """Switch the analyzer accounting off in memory, for the runs inside the block. No file of the library is edited. Runs inside
    the same process share the switch; the original method is back when the last one ends."""
    global _hook_users, _hook_original
    require()
    if not hook_present():
        raise EngineUnavailable(f"the analyzer hook {ANALYZER_HOOK} is missing in engine {engine_version()}: the backtest does not start")
    with _hook_lock:
        if _hook_users == 0:
            _hook_original = PortfolioAnalyzer.__dict__[ANALYZER_HOOK]
            setattr(PortfolioAnalyzer, ANALYZER_HOOK, _skip_pnl_record)
        _hook_users += 1
    try:
        yield
    finally:
        with _hook_lock:
            _hook_users -= 1
            if _hook_users == 0:
                setattr(PortfolioAnalyzer, ANALYZER_HOOK, _hook_original)


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str                      # for example NQZ5
    tick_size: Decimal
    multiplier: Decimal
    venue: str = "SIM"
    price_precision: int = 2


@dataclass(frozen=True)
class EngineFill:
    ts_ns: int
    side: Literal["buy", "sell"]
    qty: Decimal
    price: Decimal
    commission: Decimal              # what the engine charged (zero when Hokma charges the fill itself)
    kind: Literal["entry", "exit", "stop", "target"]
    reference: Decimal | None        # the price the order was waiting for, before the ticks against


@dataclass(frozen=True)
class EngineOutcome:
    fills: tuple[EngineFill, ...]
    balance_delta: Decimal
    ambiguous_bars: int
    rejected: int
    engine_version: str


Decide = Callable[[int, Bar, "PositionSide | None"], Sequence[Decision]]


def _price(value: Decimal, precision: int) -> "Price":
    return Price(float(value), precision)


class _Run:
    """The state of one run, shared by the data generator and the strategy."""

    def __init__(self, bars: Sequence[Bar], inst: InstrumentSpec, decide: Decide, rules: FillRules, seed: int, feed: Feed, span_ns: int) -> None:
        self.bars, self.inst, self.decide, self.rules, self.seed, self.feed, self.span_ns = bars, inst, decide, rules, seed, feed, span_ns
        self.net = Decimal(0)                 # signed quantity from the fills
        self.inflight = Decimal(0)            # signed quantity of market orders sent and not filled yet
        self.levels: tuple[Decimal | None, Decimal | None] = (None, None)
        self.bar_index = 0
        self.order_index = 0
        self.rejected = 0
        self.ambiguous = 0
        self.fills: list[EngineFill] = []

    def side(self) -> PositionSide | None:
        total = self.net + self.inflight
        return None if total == 0 else ("long" if total > 0 else "short")


@lru_cache(maxsize=1)
def _strategy_class() -> type:
    class BarStrategy(Strategy):
        """Decides on the close of a bar and sends on the next open tick; keeps the protective orders and the fill log."""

        def __init__(self, run: _Run, bar_type: "BarType", instrument_id: "InstrumentId") -> None:
            super().__init__()
            self.run, self.bar_type, self.iid = run, bar_type, instrument_id
            self.pending: list[Decision] = []
            self.meta: dict[Any, tuple[str, Decimal | None, Any]] = {}     # client order id -> (kind, reference, levels of an entry)
            self.protective: dict[str, Any] = {}

        def on_start(self) -> None:
            self.subscribe_bars(self.bar_type)
            if self.run.feed != "engine_default":
                self.subscribe_trade_ticks(self.iid)

        def on_bar(self, bar: Any) -> None:
            run = self.run
            i = run.bar_index
            run.bar_index += 1
            self.pending += list(run.decide(i, run.bars[i], run.side()))
            if run.feed == "engine_default" and self.pending and i + 1 < len(run.bars):
                self._send(run.bars[i + 1].open)

        def on_trade_tick(self, tick: Any) -> None:
            if self.pending and tick.trade_id.value.startswith("o"):
                self._send(tick.price.as_decimal())

        def _send(self, ref: Decimal) -> None:
            run, pending, self.pending = self.run, self.pending, []
            for decision in pending:
                index, run.order_index = run.order_index, run.order_index + 1
                if reject_prob(run.seed, index, run.rules.reject_prob):
                    run.rejected += 1
                    continue
                if decision.action == "exit":
                    self._exit()
                else:
                    self._enter(decision, ref)

        def _enter(self, decision: Decision, ref: Decimal) -> None:
            run = self.run
            side = decision.side
            stop = None if decision.stop_points is None else stop_price(side, ref, decision.stop_points)
            target = None if decision.target_points is None else target_price(side, ref, decision.target_points)
            order = self.order_factory.market(self.iid, OrderSide.BUY if side == "long" else OrderSide.SELL, Quantity.from_str(str(decision.qty)))
            self.meta[order.client_order_id] = ("entry", ref, (stop, target))
            run.inflight += decision.qty if side == "long" else -decision.qty
            run.levels = (stop, target)
            self.submit_order(order)

        def _exit(self) -> None:
            run = self.run
            net = run.net
            if net == 0:
                return
            self.cancel_all_orders(self.iid)
            order = self.order_factory.market(self.iid, OrderSide.SELL if net > 0 else OrderSide.BUY, Quantity.from_str(str(abs(net))))
            self.meta[order.client_order_id] = ("exit", None, None)
            run.inflight += -net
            run.levels = (None, None)
            self.submit_order(order)

        def _protect(self, side: str, qty: Decimal, stop: Decimal | None, target: Decimal | None) -> None:
            closing = OrderSide.SELL if side == "buy" else OrderSide.BUY
            amount = Quantity.from_str(str(qty))
            if stop is not None:
                order = self.order_factory.stop_market(self.iid, closing, amount, _price(stop, self.run.inst.price_precision))
                self.meta[order.client_order_id] = ("stop", stop, None)
                self.protective["stop"] = order
                self.submit_order(order)
            if target is not None:
                order = self.order_factory.limit(self.iid, closing, amount, _price(target, self.run.inst.price_precision))
                self.meta[order.client_order_id] = ("target", target, None)
                self.protective["target"] = order
                self.submit_order(order)

        def on_order_filled(self, event: Any) -> None:
            run = self.run
            kind, ref, levels = self.meta.get(event.client_order_id, ("exit", None, None))
            side = "buy" if event.order_side == OrderSide.BUY else "sell"
            qty = event.last_qty.as_decimal()
            signed = qty if side == "buy" else -qty
            run.net += signed
            if kind in ("entry", "exit"):
                run.inflight -= signed
            run.fills.append(EngineFill(int(event.ts_event), side, qty, event.last_px.as_decimal(), event.commission.as_decimal(), kind, ref))
            if kind == "entry" and levels != (None, None):
                self._protect(side, qty, *levels)
            elif kind in ("stop", "target"):
                other = self.protective.pop("target" if kind == "stop" else "stop", None)
                self.protective.pop(kind, None)
                if other is not None and other.is_open:
                    self.cancel_order(other)
                run.levels = (None, None)
            if run.net == 0:
                run.levels = (None, None)

    return BarStrategy


def _trade_tick(inst: "InstrumentId", price: Decimal, ts: int, trade_id: str, precision: int) -> "TradeTick":
    return TradeTick(inst, _price(price, precision), Quantity.from_int(1), AggressorSide.NO_AGGRESSOR, TradeId(trade_id), ts, ts)


def _ticks_feed(run: _Run, iid: "InstrumentId", bar_type: "BarType", starts: Sequence[int], fast: bool) -> Iterator[list[Any]]:
    """One chunk per item: the open tick first, then (after the engine has processed it and the strategy has sent its orders) the
    other ticks in the order that depends on the position at that moment, then the whole bar for the strategy."""
    precision, span = run.inst.price_precision, run.span_ns
    for i, bar in enumerate(run.bars):
        start = starts[i]
        yield [_trade_tick(iid, bar.open, start + 1_000, f"o{i}", precision)]
        side = run.side()
        stop, target = run.levels if side is not None else (None, None)
        if not fast and side is not None and stop is not None and stop_first(side, stop, target, bar).ambiguous:
            run.ambiguous += 1
        points: tuple[SyntheticTick, ...] = bar_to_ticks(bar, side=side, stop=stop, target=target, fast=fast, span_ns=span)
        for k, point in enumerate(points[1:]):
            yield [_trade_tick(iid, point.price, start + point.offset_ns, f"{i}x{k}", precision)]
        yield [EngineBarData(bar_type, _price(bar.open, precision), _price(bar.high, precision), _price(bar.low, precision),
                             _price(bar.close, precision), Quantity.from_int(max(int(bar.volume), 1)), start + span, start + span)]


@lru_cache(maxsize=1)
def _ticks_model_class() -> type:
    class TicksAgainstFillModel(FillModel):
        """Every order fills `ticks` ticks away from the best price, against the trader: the engine's one tick model for any number."""

        def __init__(self, ticks: int) -> None:
            super().__init__()
            self.ticks = ticks

        def get_orderbook_for_fill_simulation(self, instrument: Any, order: Any, best_bid: Any, best_ask: Any) -> Any:
            move = instrument.price_increment.as_double() * self.ticks
            bid = Price(best_bid.as_double() - move, instrument.price_precision)
            ask = Price(best_ask.as_double() + move, instrument.price_precision)
            book = OrderBook(instrument_id=instrument.id, book_type=BookType.L2_MBP)
            size = Quantity(1_000_000, instrument.size_precision)
            book.add(BookOrder(side=OrderSide.BUY, price=bid, size=size, order_id=1), 0, 0)
            book.add(BookOrder(side=OrderSide.SELL, price=ask, size=size, order_id=2), 0, 0)
            return book

    return TicksAgainstFillModel


def _fill_model(rules: FillRules, seed: int) -> Any:
    if rules.model == "probabilistic":
        if rules.next_open_ticks != 1:
            raise SpecError("unsupported_ticks: the probabilistic model moves the price exactly one tick, on half of the fills")
        return FillModel(prob_slippage=0.5, random_seed=int(seed))
    return OneTickSlippageFillModel() if rules.next_open_ticks == 1 else _ticks_model_class()(rules.next_open_ticks)


def run_bars(*, bars: Sequence[Bar], instrument: InstrumentSpec, decide: Decide, rules: FillRules, starting_balance: Decimal,
             per_contract_fee: Decimal | None, seed: int = 0, feed: Feed = "ticks", span_ns: int = BAR_NS,
             analyzer: bool = False) -> EngineOutcome:
    """Run the bars through the engine and return its fills. `feed`: "ticks" (every synthetic tick), "fast" (open tick and bar, for
    strategies with no protective orders) or "engine_default" (the bars alone, in the engine's own order: the control of the tests).
    `analyzer=True` leaves the engine statistics on (only the test that proves the switch-off changes nothing uses it)."""
    require()
    if not bars:
        raise SpecError("bad_spec: no bars to run")
    inst = instrument
    starts = [int(b.ts.timestamp()) * 10**9 for b in bars]
    run = _Run(bars, inst, decide, rules, seed, feed, span_ns)
    venue = Venue(inst.venue)
    iid = InstrumentId(Symbol(inst.symbol), venue)
    minutes = max(span_ns // (60 * 10**9), 1)
    bar_type = BarType.from_str(f"{iid}-{minutes}-MINUTE-LAST-EXTERNAL")
    contract = FuturesContract(
        instrument_id=iid, raw_symbol=Symbol(inst.symbol), asset_class=AssetClass.INDEX, currency=USD, price_precision=inst.price_precision,
        price_increment=Price(float(inst.tick_size), inst.price_precision), multiplier=Quantity.from_str(str(inst.multiplier)),
        lot_size=Quantity.from_int(1), underlying=inst.symbol, activation_ns=0, expiration_ns=starts[-1] + 365 * 86_400 * 10**9,
        ts_event=0, ts_init=0)
    start_balance = Money(float(starting_balance), USD)
    guard = contextlib.nullcontext() if analyzer else analyzer_off()
    with guard:
        engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR", bypass_logging=True)))
        try:
            engine.add_venue(venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN, base_currency=USD,
                             starting_balances=[start_balance], fill_model=_fill_model(rules, seed),
                             fee_model=None if per_contract_fee is None else PerContractFeeModel(Money(float(per_contract_fee), USD)),
                             bar_execution=feed == "engine_default", trade_execution=feed != "engine_default")
            engine.add_instrument(contract)
            if feed == "engine_default":
                engine.add_data([EngineBarData(bar_type, _price(b.open, inst.price_precision), _price(b.high, inst.price_precision),
                                               _price(b.low, inst.price_precision), _price(b.close, inst.price_precision),
                                               Quantity.from_int(max(int(b.volume), 1)), s + span_ns, s + span_ns) for b, s in zip(bars, starts)])
            else:
                engine.add_data_iterator("bars", _ticks_feed(run, iid, bar_type, starts, feed == "fast"))
            engine.add_strategy(_strategy_class()(run, bar_type, iid))
            engine.run()
            delta = engine.portfolio.account(venue).balance_total(USD).as_decimal() - Decimal(str(float(starting_balance)))
        finally:
            engine.dispose()
    return EngineOutcome(tuple(run.fills), delta, run.ambiguous, run.rejected, engine_version())


@dataclass(frozen=True)
class SearchAxis:
    """One axis of a discrete search space, as plain values: the wrapper below never sees a Hokma type."""
    name: str
    kind: Literal["int", "float", "choice"]
    low: float | None = None
    high: float | None = None
    step: float | None = None
    choices: tuple[Any, ...] = ()


class TpeSearch:
    """Ask and tell over a discrete space with the TPE sampler. The same seed and the same answers give the same sequence of points.

    `ask` returns the next point; `tell` gives the value of the last point back (higher is better)."""

    def __init__(self, axes: Sequence[SearchAxis], seed: int) -> None:
        if _SEARCH_ERROR is not None:
            raise SearchUnavailable(f"the search library is not available on this machine: {type(_SEARCH_ERROR).__name__}")
        self._axes = tuple(axes)
        self._study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=int(seed)))
        self._open: Any = None

    def ask(self) -> dict[str, Any]:
        if self._open is not None:
            raise RuntimeError("the last point was not answered with tell")
        trial = self._study.ask()
        point: dict[str, Any] = {}
        for axis in self._axes:
            if axis.kind == "choice":
                point[axis.name] = trial.suggest_categorical(axis.name, list(axis.choices))
            elif axis.kind == "int":
                point[axis.name] = int(trial.suggest_int(axis.name, int(axis.low), int(axis.high), step=int(axis.step)))
            else:
                point[axis.name] = round(float(trial.suggest_float(axis.name, float(axis.low), float(axis.high), step=float(axis.step))), 10)
        self._open = trial
        return point

    def tell(self, value: float) -> None:
        if self._open is None:
            raise RuntimeError("tell needs a point from ask")
        trial, self._open = self._open, None
        self._study.tell(trial, float(value))

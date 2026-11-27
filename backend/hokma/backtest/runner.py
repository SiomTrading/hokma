"""The run of a backtest, from a spec to a saved result (SPEC-020, section 4.2: B-12, B-35, AH-34).

One run goes through five stages and a failure says which one it was in: `data` (bars and the sheet of the instrument), `signal` (the strategy
decides), `size` (an intent becomes a quantity), `fill` (the engine matches the orders) and `result` (trades, curve and statistics). The
statistics come from `hokma.quant` through `hokma.backtest.results`, never from the engine. The run row, its trades and its status are written by
`results.record_result` in one transaction, and every run, failed ones included, enters the trial ledger (`trials.record`).

The service has no path to a broker: a desk's limits are applied by the pure guards of `hokma.exec.guards` through `guards_bridge`, and the
account those guards see is the simulated account of the run (`SimBroker`).
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from hokma.backtest import _lib, costs, data_feed, engine, guards_bridge, results, trials
from hokma.backtest.catalog import Catalog
from hokma.backtest.continuous import Adjustment, ChainError, ContractBars, RollRule
from hokma.backtest import continuous
from hokma.backtest.data_feed import DataFeedError
from hokma.backtest.engine import EngineUnavailable, Expiry, RunOutput
from hokma.backtest.fills import Decision, against, limit_fill, stop_first, stop_price, target_price
from hokma.backtest.guards_bridge import SimAccount, SimPosition
from hokma.backtest.results import BacktestResult, BarRange, EquityCurve, Fill
from hokma.backtest.spec import BacktestSpec, SpecError, sequence_same_bar, span_ns as bar_span_ns
from hokma.backtest.ticks import Bar
from hokma.contracts import history, identity
from hokma.contracts.identity import ContractSheet, NoContract
from hokma.db import Database, get_database
from hokma.db.timeutil import to_db_time, utcnow
from hokma.desks.core import paths
from hokma.exec.guards import Leg, OrderProposal
from hokma.factory.orders import BadOrder, BadSize, LoosenStop, OrderIntent, Protection, adjust_protection, intent_quantity, stop_units
from hokma.factory.parts import build_composite
from hokma.factory.registry import Registry
from hokma.factory.strategy import BadStrategy, InitContext, Position, build_context, check_strategy, compute_indicator
from hokma.factory.params import validate as validate_params
from hokma.quant import limits
from hokma.quant.errors import BadInput

ZERO = Decimal(0)
STAGES = ("data", "signal", "size", "fill", "result")
CONTINUOUS = "CONT:"
PERIODS_PER_YEAR = 252                      # the statistics read the daily equity returns of the curve
_SYMBOL = re.compile(r"[^A-Z0-9]")


class StageError(RuntimeError):
    """A run failed in one stage. `stage` is one of STAGES and `code` is the stable code of the cause; a failure in one stage is never
    reported as a failure of another."""

    def __init__(self, stage: str, code: str, message: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"stage is one of {', '.join(STAGES)}")
        self.stage, self.code, self.detail = stage, code, message
        super().__init__(f"{stage}:{code}: {message}")


_PREFIX = re.compile(r"^([a-z][a-z_]*):")


def _code(exc: BaseException) -> str:
    """The stable code of an error: its `code`, or for a spec error the code that its message starts with (`protective_in_fast_mode: ...`)."""
    code = str(getattr(exc, "code", None) or type(exc).__name__.lower())
    if isinstance(exc, SpecError):
        found = _PREFIX.match(str(exc))
        return found.group(1) if found else code
    return code


def stage_of(stage: str, exc: BaseException) -> StageError:
    """`exc` as a failure of `stage` (an error that already names its stage is kept)."""
    return exc if isinstance(exc, StageError) else StageError(stage, _code(exc), str(exc)[:500])


# ----------------------------------------------------------------------------------------------------------------------- data
@dataclass(frozen=True)
class InstrumentData:
    """What a run needs of one instrument: its bars (open time, UTC), the hash of those bars and the sheet that gives tick and multiplier."""
    instrument: str
    bars: tuple[Bar, ...]
    data_hash: str
    sheet: ContractSheet
    warnings: tuple[str, ...] = ()

    @property
    def symbol(self) -> str:
        return _SYMBOL.sub("", self.instrument.upper()) or "X"


Source = Callable[[str, BacktestSpec], InstrumentData]


def sheet_of(instrument: str, on: date) -> ContractSheet:
    """The sheet of an instrument: a continuous future (`CONT:NQ`) from the history table on `on`, anything else through `hokma.contracts`."""
    if instrument.startswith(CONTINUOUS):
        from hokma.contracts import expiry

        root = instrument[len(CONTINUOUS):].upper()
        sheet = history.sheet(root, on=on)
        months = [m for m in expiry.listed_months(root, on, on + timedelta(days=400)) if m >= on.replace(day=1)]
        front = months[0] if months else None                 # the contract in force on that day: the guards of H2 need a future with an expiration
        return ContractSheet(instrument, "future", root, None, sheet.multiplier, sheet.tick_size, sheet.tick_value, front, None, None, "USD",
                             sheet.source, datetime(on.year, on.month, on.day, tzinfo=timezone.utc))
    return identity.resolve(instrument)


def bars_of(frame: pd.DataFrame) -> tuple[Bar, ...]:
    """The rows of a frame (UTC index, open high low close volume) as bars; prices keep the exact text of the number."""
    out = []
    for ts, row in zip(frame.index, frame.itertuples(index=False)):
        moment = pd.Timestamp(ts)
        moment = moment.tz_localize("UTC") if moment.tzinfo is None else moment.tz_convert("UTC")
        out.append(Bar(moment.to_pydatetime(), *(Decimal(repr(float(x))) for x in (row.open, row.high, row.low, row.close, row.volume))))
    return tuple(out)


def _continuous(root: str, spec: BacktestSpec, fetch: Callable[..., Any] | None, catalog: Catalog | None) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """The continuous series of a root: the listed contracts of the window loaded one by one and rolled by volume with the difference
    adjustment (the last prices are the real ones)."""
    from hokma.contracts import expiry

    first, last = spec.start.date() - timedelta(days=120), spec.end.date() + timedelta(days=120)
    chain: list[ContractBars] = []
    short: DataFeedError | None = None                       # the notice of a contract whose bars did not reach: it is what the owner reads if no contract has enough
    for month in expiry.listed_months(root, first, last):
        contract = f"FUT:{root}:{month.year:04d}-{month.month:02d}"
        try:
            loaded = data_feed.load(contract, spec.bar, start=spec.start, end=spec.end, as_of=spec.as_of, fetch=fetch, catalog=catalog)
        except DataFeedError as exc:
            if exc.code in ("no_data", "insufficient_data"):
                short = exc if exc.code == "insufficient_data" else short
                continue
            raise
        chain.append(ContractBars(contract, loaded.frame))
    if not chain:
        if short is not None:
            raise short
        raise DataFeedError("no_data", f"no contract of {root} has bars between {spec.start.date()} and {spec.end.date()}")
    series = continuous.build(chain, RollRule.by_volume(), Adjustment.DIFFERENCE)
    return series.frame[["open", "high", "low", "close", "volume"]], ("continuous_adjusted:difference",)


def default_source(*, fetch: Callable[..., Any] | None = None, catalog: Catalog | None = None) -> Source:
    """The source of a run: bars through `data_feed` (and the catalog when there is one), continuous futures through `continuous`."""
    def source(instrument: str, spec: BacktestSpec) -> InstrumentData:
        sheet = sheet_of(instrument, spec.end.date())
        if instrument.startswith(CONTINUOUS):
            frame, warnings = _continuous(instrument[len(CONTINUOUS):].upper(), spec, fetch, catalog)
            return InstrumentData(instrument, bars_of(frame), data_feed.data_hash(frame), sheet, warnings)
        loaded = data_feed.load(instrument, spec.bar, start=spec.start, end=spec.end, as_of=spec.as_of, fetch=fetch, catalog=catalog)
        return InstrumentData(instrument, bars_of(loaded.frame), loaded.data_hash, sheet, loaded.warnings)
    return source


def load_data(spec: BacktestSpec, source: Source) -> InstrumentData:
    """The data of the one instrument of a spec, with the failures of the data stage named as such. The run, the optimization, the walk forward
    and the Monte Carlo load their bars here."""
    if len(spec.instruments) != 1:
        raise SpecError("bad_spec: a run takes one instrument; run_many takes several")
    try:
        data = source(spec.instruments[0], spec)
    except StageError:
        raise
    except (DataFeedError, ChainError, NoContract, history.MarginUnavailable, BadInput, ValueError, LookupError) as exc:
        raise StageError("data", _code(exc), str(exc)[:500]) from exc
    if not data.bars:
        raise StageError("data", "no_data", f"no bars for {spec.instruments[0]} between {spec.start.date()} and {spec.end.date()}")
    return data


@dataclass(frozen=True)
class Services:
    """What the public entry points (`hokma.backtest.run`, `optimize`, `walk_forward`, `monte_carlo`, `evidence`) read from outside the
    spec: the database, the registry of strategies, the source of the bars and the folder of the equity curves. Every field has the default of
    the service; a test or the server passes its own. `strategy` builds the strategy of a point from its parameters instead of the registry.
    The registry keeps one version of each id, so `spec.strategy_version` is recorded in the run and does not select a build."""
    db: Database | None = None
    registry: Registry | None = None
    source: Source | None = None
    fetch: Callable[..., Any] | None = None
    catalog: Catalog | None = None
    curve_dir: Path | None = None
    strategy: Callable[[Mapping[str, Any]], Any] | None = None

    def database(self) -> Database:
        return self.db or get_database()

    def curves(self) -> Path:
        return self.curve_dir or paths.user_path("backtest", "curves")

    def bars_source(self) -> Source:
        return self.source or default_source(fetch=self.fetch, catalog=self.catalog)

    def load(self, spec: BacktestSpec) -> InstrumentData:
        return load_data(spec, self.bars_source())

    def build(self, spec: BacktestSpec, params: Mapping[str, Any] | None = None, sheet: ContractSheet | None = None) -> Any:
        """The strategy of `spec` with `params` (the parameters of the spec when none are given). `sheet` is the contract of the data of the run: an
        alpha or a saved draft is put in a composite that trades that contract."""
        values = spec.params if params is None else params
        if self.strategy is not None:
            return self.strategy(values)
        return _resolve(None, self.registry, replace(spec, params=dict(values)), sheet=sheet, db=self.db)

    def run(self, spec: BacktestSpec) -> BacktestResult:
        """`run` of the spec with these services."""
        strategy = None if self.strategy is None else self.strategy(spec.params)
        return run(spec, strategy=strategy, registry=self.registry, source=self.source, fetch=self.fetch, catalog=self.catalog, db=self.db,
                   curve_dir=self.curve_dir)

    def driver(self, spec: BacktestSpec, data: InstrumentData, params: Mapping[str, Any] | None = None) -> "StrategyDriver":
        """The strategy of a point as the decision function of the engine, over `data`. The point's parameters go over those of the spec."""
        point = replace(spec, params={**spec.params, **dict(params or {})})
        return StrategyDriver(self.build(point, sheet=data.sheet), point, data)


# ----------------------------------------------------------------------------------------------------------------------- the account
@dataclass
class Holding:
    qty: Decimal                       # signed: long positive
    avg: Decimal


@dataclass(frozen=True)
class BrokerFill:
    at: datetime
    contract_id: str
    side: Literal["buy", "sell"]
    qty: Decimal
    price: Decimal
    commission: Decimal


class SimBroker:
    """The simulated broker of a backtest: it keeps the positions and the cash between steps and fills an order at a reference price with
    the ticks against the trader (`fills.against`, the one slippage function) and the commission of the fee table (`costs`). It reaches no
    connector: it is a ledger. The equity is the cash flow plus the open quantity at the last marks."""

    def __init__(self, capital: Decimal, sheets: Mapping[str, ContractSheet], model: Any, *, ticks_against: int = 1) -> None:
        self.capital, self.sheets, self.ticks = capital, dict(sheets), ticks_against
        self._fees = {cid: costs.fee_model(model, sh.asset_type) for cid, sh in self.sheets.items()}
        self.flow = capital
        self.holdings: dict[str, Holding] = {}
        self.fills: list[BrokerFill] = []
        self.peak = capital
        self.orders_today = 0
        self._day: date | None = None
        self._day_start = capital
        self._marks: dict[str, Decimal] = {}

    def position(self, contract_id: str) -> Holding | None:
        held = self.holdings.get(contract_id)
        return held if held is not None and held.qty != 0 else None

    def fill(self, contract_id: str, side: Literal["buy", "sell"], qty: Decimal, ref: Decimal, at: datetime) -> BrokerFill:
        sheet = self.sheets[contract_id]
        if qty <= 0:
            raise SpecError("bad_spec: a quantity is above zero")
        tick = sheet.tick_size or Decimal("0.01")
        price = against(side, ref, self.ticks, tick)
        fee = costs.commission_usd(self._fees[contract_id], qty=qty, price=price, multiplier=sheet.multiplier)
        signed = qty if side == "buy" else -qty
        self.flow += -signed * price * sheet.multiplier - fee
        held = self.holdings.setdefault(contract_id, Holding(ZERO, ZERO))
        if held.qty == 0 or (held.qty > 0) == (signed > 0):
            held.avg = (abs(held.qty) * held.avg + qty * price) / (abs(held.qty) + qty)
        elif abs(signed) > abs(held.qty):
            held.avg = price
        held.qty += signed
        self.orders_today += 1
        done = BrokerFill(at, contract_id, side, qty, price, fee)
        self.fills.append(done)
        return done

    def equity(self, marks: Mapping[str, Decimal] | None = None) -> Decimal:
        marks = {**self._marks, **dict(marks or {})}
        return self.flow + sum((h.qty * marks.get(cid, h.avg) * self.sheets[cid].multiplier for cid, h in self.holdings.items()), ZERO)

    def mark(self, marks: Mapping[str, Decimal], at: datetime) -> Decimal:
        """Take the marks of a bar close: the equity, the peak (it only goes up) and the day the daily result is measured from."""
        self._marks.update(marks)
        equity = self.equity()
        if self._day != at.date():
            self._day, self._day_start, self.orders_today = at.date(), equity, 0
        self.peak = max(self.peak, equity)
        return equity

    def account(self, at: datetime, marks: Mapping[str, Decimal] | None = None) -> SimAccount:
        """The account as the guards read it: equity, peak, the result of the day and the positions with their market value."""
        marks = {**self._marks, **dict(marks or {})}
        equity = self.equity(marks)
        positions, away = [], ZERO
        for cid, held in self.holdings.items():
            if held.qty == 0:
                continue
            sheet, mark = self.sheets[cid], marks.get(cid, held.avg)
            positions.append(SimPosition(cid, held.qty, sheet.multiplier, held.qty * mark * sheet.multiplier))
            away += held.qty * (mark - held.avg) * sheet.multiplier if sheet.asset_type == "future" else held.qty * mark * sheet.multiplier
        return SimAccount(equity - away, equity, max(self.peak, equity), equity - self._day_start, equity - self.capital, tuple(positions), self.orders_today)


# ----------------------------------------------------------------------------------------------------------------------- the strategy
class StrategyDriver:
    """A strategy of the factory as the decision function of the engine. It reveals the bars one by one, turns `OrderIntent` into decisions
    (quantity by `factory.orders`, V-10 same bar order by `spec.sequence_same_bar`), applies the limits of the desk when the spec has one,
    and keeps the tag and the indicator values of each entry for the trades (B-14, B-25). It has the attributes that `optimize.EngineStrategy`
    asks for: `decide`, `tick_size`, `multiplier`, `symbol`, `protective_orders`, `asset_type`."""

    def __init__(self, strategy: Any, spec: BacktestSpec, data: InstrumentData, *, desk_limits: Mapping[str, Any] | None = None,
                 level: int = 2, span_ns: int | None = None) -> None:
        self.spec, self.data, self.sheet, self.failure = spec, data, data.sheet, None
        if self.sheet.tick_size is None:
            raise StageError("data", "incomplete_sheet", f"{self.sheet.contract_id} has no tick size")
        self.tick_size, self.multiplier, self.symbol, self.asset_type = self.sheet.tick_size, self.sheet.multiplier, data.symbol, self.sheet.asset_type
        self.protective_orders = bool(getattr(strategy, "protective_orders", True))
        self.rejected: dict[str, int] = {}
        self.entry_meta: dict[int, list[tuple[str | None, dict[str, Any]]]] = {}
        self.warnings: list[str] = []
        self._strategy = strategy
        self._span = span_ns or bar_span_ns(spec.bar)
        self._pending: list[Decision] = []
        self._live_limits: list[tuple[Decision, int]] = []             # limit entries still waiting for a bar to cross them, with the bars they have left
        self._levels: tuple[Decimal | None, Decimal | None] = (None, None)
        self._level_side: str | None = None
        self._limits, self._level = desk_limits, level
        self.broker = SimBroker(spec.capital_usd, {data.instrument: self.sheet}, spec.costs, ticks_against=spec.fill.next_open_ticks)
        try:
            schema = check_strategy(strategy)
            self._values = validate_params(schema, spec.params)
            init = InitContext(self._values)
            strategy.init(init)
        except Exception as exc:
            raise stage_of("signal", exc) from exc
        bars = data.bars
        self._times = [b.ts + timedelta(microseconds=self._span // 1_000) for b in bars]
        self._arrays = {name: np.array([float(getattr(b, name)) for b in bars]) for name in ("open", "high", "low", "close", "volume")}
        self._computed: dict[str, np.ndarray] = {}
        try:
            for decl in init.indicators:
                self._computed[decl.name] = compute_indicator(decl, self._arrays, self._computed)
        except Exception as exc:
            raise stage_of("signal", exc) from exc
        self._wait = max(init.warmup_bars, 1)

    # -- the account the strategy and the guards see
    def _apply(self, decision: Decision, bar: Bar, price: Decimal | None = None) -> None:
        cid, broker = self.data.instrument, self.broker
        if decision.action == "reduce":
            held = broker.position(cid)
            if held is not None:
                broker.fill(cid, "sell" if held.qty > 0 else "buy", min(decision.qty, abs(held.qty)), bar.open, bar.ts)
            return
        if decision.action == "modify":
            if broker.position(cid) is not None:
                stop, target = self._levels
                self._levels = (decision.new_stop if decision.new_stop is not None else stop, decision.new_target if decision.new_target is not None else target)
            return
        if decision.action == "exit":
            self._live_limits = []
            held = broker.position(cid)
            if held is not None:
                broker.fill(cid, "sell" if held.qty > 0 else "buy", abs(held.qty), bar.open, bar.ts)
            self._levels, self._level_side = (None, None), None
            return
        side = "buy" if decision.side == "long" else "sell"
        ref = bar.open if price is None else price                      # a limit fills at its price, or at the open when the bar gaps through it
        broker.fill(cid, side, decision.qty, ref, bar.ts)
        stop = None if decision.stop_points is None else stop_price(decision.side, ref, decision.stop_points)    # type: ignore[arg-type]
        target = None if decision.target_points is None else target_price(decision.side, ref, decision.target_points)  # type: ignore[arg-type]
        self._levels, self._level_side = (stop, target), decision.side

    def _sync(self, bar: Bar, side: Any) -> None:
        """Bring the account of the run to the close of `bar`: the orders decided before are filled at its open, a position that the engine no
        longer holds was closed by its stop or target, and the equity is marked at the close."""
        cid, broker = self.data.instrument, self.broker
        for decision in self._pending:
            if decision.action == "enter" and decision.limit is not None:
                self._live_limits.append((decision, decision.valid_bars))
            else:
                self._apply(decision, bar)
        self._pending = []
        waiting, self._live_limits = self._live_limits, []
        for decision, left in waiting:                                   # the same rule as the engine: the bar crosses the limit or the order loses a bar
            reach = limit_fill(decision.side, decision.limit, bar)       # type: ignore[arg-type]
            if reach is None:
                if left > 1:
                    self._live_limits.append((decision, left - 1))
                continue
            self._apply(decision, bar, reach[0])
        held = broker.position(cid)
        if held is not None and side is None:
            stop, target = self._levels
            ref = bar.close
            if stop is not None and self._level_side in ("long", "short"):
                choice = stop_first(self._level_side, stop, target, bar)       # type: ignore[arg-type]
                if choice.kind is not None:
                    ref = bar.open if choice.at_open else (stop if choice.kind == "stop" else target)       # type: ignore[assignment]
            broker.fill(cid, "sell" if held.qty > 0 else "buy", abs(held.qty), ref, bar.ts)
            self._levels, self._level_side = (None, None), None
        broker.mark({cid: bar.close}, bar.ts)

    # -- the decision
    def decide(self, i: int, bar: Bar, side: Any) -> list[Decision]:
        if self.failure is not None:
            return []
        try:
            self._sync(bar, side)
            if i + 1 < self._wait:
                return []
            return self._decide(i, bar, side)
        except Exception as exc:                                  # noqa: BLE001 - kept, and raised by the runner when the engine returns
            self.failure = stage_of("signal", exc)
            return []

    def _decide(self, i: int, bar: Bar, side: Any) -> list[Decision]:
        cid, broker, spec = self.data.instrument, self.broker, self.spec
        held = broker.position(cid)
        position = None if held is None else Position(cid, held.qty, held.avg)
        context = build_context(cursor=i + 1, times=self._times, contract_id=cid, bars=self._arrays, indicators=self._computed, params=self._values,
                                position=position, equity=broker.equity())
        emitted = self._strategy.on_bar(context)
        if emitted is None:
            return []
        if isinstance(emitted, OrderIntent) or not isinstance(emitted, (list, tuple)) or not all(isinstance(x, OrderIntent) for x in emitted):
            raise BadStrategy(f"{type(self._strategy).__name__}.on_bar must return a list of OrderIntent")
        exits: list[Decision] = []
        entries: list[tuple[Decision, OrderIntent]] = []
        current = held                                                              # the position as the intents of this bar see it
        context_values = {name: float(series[i]) for name, series in self._computed.items() if np.isfinite(series[i])}
        for intent in emitted:
            if intent.contract_id != cid:
                raise StageError("signal", "wrong_contract", f"the strategy ordered {intent.contract_id} in a run of {cid}")
            moved = self._moved_stop(intent, current, bar)
            if moved is not None:
                exits.append(moved)
                continue
            if intent.order_type not in ("market", "limit") or intent.order_type == "limit" and (intent.reduce_only or current is not None and (
                    "buy" if current.qty > 0 else "sell") != intent.side):     # type: ignore[union-attr]
                raise StageError("fill", "order_type_unsupported", f"the run fills market orders and limit entries; {intent.order_type} is not supported yet")
            try:
                qty, notes = intent_quantity(intent, sheet=self.sheet, price=bar.close, equity=broker.equity(), mode=spec.qty_mode)
            except (BadSize, ValueError, ArithmeticError) as exc:
                raise StageError("size", _code(exc), str(exc)[:500]) from exc
            self.warnings.extend(n for n in notes if n not in self.warnings)
            held_side = None if current is None else ("buy" if current.qty > 0 else "sell")
            if held_side is not None and intent.side != held_side:                 # the other side of a position: it closes it
                if qty <= 0:
                    raise StageError("fill", "partial_close_unsupported", "an exit needs a quantity above zero")
                if intent.reduce_only and qty < abs(current.qty):                    # type: ignore[union-attr]
                    exits.append(Decision("reduce", qty=qty))                         # a partial exit: the rest stays with its protective orders
                    continue
                exits.append(Decision("exit"))
                if not intent.reduce_only and qty > abs(current.qty):               # type: ignore[union-attr]
                    entries.append((self._entry(intent, qty - abs(current.qty), bar), intent))   # type: ignore[union-attr]
                current = None                                                      # what is left of the bar's intents sees a flat account
                continue
            if qty <= 0:
                self.warnings.append("size_zero") if "size_zero" not in self.warnings else None
                continue
            entries.append((self._entry(intent, qty, bar), intent))
        if len({(d.side, d.stop_points, d.target_points, d.limit) for d, _i in entries}) > 1:
            raise StageError("fill", "multiple_targets_unsupported", "the run holds one stop and one target per position: entries of one bar with different levels are not supported yet")
        ordered, notes = sequence_same_bar(exits, entries, holding_side=None if held is None else ("buy" if held.qty > 0 else "sell"),
                                           accumulate=spec.accumulate, entry_side=lambda e: "buy" if e[0].side == "long" else "sell")
        self.warnings.extend(n for n in notes if n not in self.warnings)
        out: list[Decision] = []
        for item in ordered:
            decision, intent = (item, None) if isinstance(item, Decision) else item
            decision = self._filtered(decision, bar)
            if decision is None:
                continue
            out.append(decision)
            if intent is not None:
                self.entry_meta.setdefault(i + 1, []).append((intent.tag, context_values))
        self._pending = list(out)
        return out

    def _moved_stop(self, intent: OrderIntent, current: Any, bar: Bar) -> Decision | None:
        """A reduce-only stop order on the protective side of a position that has a live stop is the move of that stop (breakeven and trailing by
        hand): the live protective order goes to the price of the order from the next bar, whatever the quantity. A move against the position is refused."""
        if not (intent.reduce_only and intent.order_type == "stop" and current is not None and self._level_side in ("long", "short")):
            return None
        held_side = "buy" if current.qty > 0 else "sell"
        if intent.side == held_side:
            return None
        stop, target = self._levels
        try:
            adjust_protection(held_side, Protection(stop, target), new_sl=intent.stop)      # type: ignore[arg-type]
        except (BadOrder, LoosenStop) as exc:
            raise StageError("fill", _code(exc), str(exc)[:500]) from exc
        return Decision("modify", new_stop=intent.stop)

    def _entry(self, intent: OrderIntent, qty: Decimal, bar: Bar) -> Decision:
        side = "long" if intent.side == "buy" else "short"
        ref = bar.close if intent.limit is None else intent.limit              # a limit entry measures its stop and target from the price it waits for
        try:
            stop = None if intent.sl is None else stop_units(intent.sl, entry=ref, sheet=self.sheet)
            target = None if intent.tp is None else stop_units(intent.tp, entry=ref, sheet=self.sheet)
        except ValueError as exc:
            raise StageError("size", _code(exc), str(exc)[:500]) from exc
        return Decision("enter", side, qty, stop, target, limit=intent.limit)

    def _filtered(self, decision: Decision, bar: Bar) -> Decision | None:
        """The filter before an order, only for a run of a desk: the guards of H2 judge the order against the simulated account."""
        if self.spec.desk_id is None:
            return decision
        cid, held = self.data.instrument, self.broker.position(self.data.instrument)
        if decision.action == "exit":
            if held is None:
                return decision
            leg = Leg(cid, "sell" if held.qty > 0 else "buy", "close", abs(held.qty))
        else:
            leg = Leg(cid, "buy" if decision.side == "long" else "sell", "open", decision.qty)
        # the stop of the entry goes to the filter as the live order carries it (SPEC-026: futures and CFD need one); the distance is from the close the order is judged at
        stop_loss = None if decision.action != "enter" or decision.stop_points is None else stop_price(decision.side, bar.close, decision.stop_points)  # type: ignore[arg-type]
        proposal = OrderProposal(self.spec.desk_id, self.spec.line, "desk", (leg,), "market", stop_loss=stop_loss, created_by="backtest")
        now = bar.ts + timedelta(microseconds=self._span // 1_000)
        verdict = guards_bridge.check(proposal, self.broker.account(now, {cid: bar.close}), limits=self._limits or {}, sheets={cid: self.sheet},
                                      marks={cid: bar.close}, now=now, capital=self.spec.capital_usd, level=self._level,   # type: ignore[arg-type]
                                      state=guards_bridge.monitor_state(now))
        code = guards_bridge.blocked_code(verdict)
        if code is not None:
            self.rejected[code] = self.rejected.get(code, 0) + 1
            return None
        qty = verdict.proposal.legs[0].qty
        return decision if qty == leg.qty or decision.action == "exit" else replace(decision, qty=qty)


def wish_signals(driver: StrategyDriver) -> np.ndarray:
    """The position that a strategy without protective orders wants after the close of each bar: -1, 0 or +1 contracts. The strategy decides
    through the same driver as the engine run (same warm-up, same reveal of the bars, same size and filters), but nothing is filled by an engine:
    the position it sees is the one its own decisions leave. It is the signal vector that `sweep` fills in one pass to order the points."""
    if driver.protective_orders:
        raise SpecError("protective_in_fast_mode: a strategy with stops or targets has no signal vector: the engine runs it")
    held = 0
    out = np.zeros(len(driver.data.bars))
    for i, bar in enumerate(driver.data.bars):
        for decision in driver.decide(i, bar, None if held == 0 else ("long" if held > 0 else "short")):
            held = 0 if decision.action == "exit" else (1 if decision.side == "long" else -1)
        if driver.failure is not None:
            raise driver.failure
        out[i] = held
    return out


def spec_from_canonical(body: Mapping[str, Any]) -> BacktestSpec:
    """The spec of a saved run from the plain values it was saved as (`BacktestSpec.canonical`, kept in `backtest_runs.spec_json`)."""
    from hokma.backtest.spec import CostModel, FillRules

    try:
        return BacktestSpec(
            body["strategy_id"], int(body["strategy_version"]), dict(body["params"]), body["line"], tuple(body["instruments"]), body["bar"],
            datetime.fromisoformat(body["start"]), datetime.fromisoformat(body["end"]), datetime.fromisoformat(body["as_of"]), Decimal(body["capital_usd"]),
            CostModel(**body["costs"]), FillRules(**body["fill"]), body["oms"], body["qty_mode"], body["on_conflict"], body["finalize"], body["session"],
            body["desk_id"], body["accumulate"], int(body["seed"]))
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise SpecError(f"bad_spec: the saved spec cannot be read: {type(exc).__name__}: {exc}") from exc


# ----------------------------------------------------------------------------------------------------------------------- B-12: blow up
@dataclass(frozen=True)
class Blowup:
    at: datetime                       # the close of the bar where the equity reached the floor
    final_equity_usd: Decimal
    output: RunOutput                  # the run as it was at that bar, with the open position closed (tag account_blown)


def stop_on_blowup(out: RunOutput, spec: BacktestSpec, bars: Sequence[Bar], *, floor: Decimal | None = None, symbol: str, multiplier: Decimal,
                   span_ns: int, asset_type: str = "future") -> Blowup | None:
    """The run stops when the equity is at or under zero, or under the capital floor of the desk (`capital_floor_usd` of `desk_limits`), at the
    first bar close where that happens: the position still open is closed there and nothing is traded after that bar. None when it never does."""
    line = ZERO if floor is None else max(ZERO, floor)
    for index, (_at, equity) in enumerate(out.equity[: len(bars)]):
        if equity <= line:
            cut = engine.truncate(out, spec, bars, index, symbol=symbol, multiplier=multiplier, span_ns=span_ns, asset_type=asset_type, tag="account_blown")
            return Blowup(cut.equity[-1][0], cut.equity[-1][1], cut)
    return None


# ----------------------------------------------------------------------------------------------------------------------- the run
def run_with(spec: BacktestSpec, strategy: Any, bars: Sequence[Bar], *, expiry_rule: Expiry | None = None, latency_ms: int = 0,
             root: str | None = None) -> RunOutput:
    """One engine run of a strategy that has the attributes of `StrategyDriver` (`decide`, `tick_size`, `multiplier`, `symbol`,
    `protective_orders`, `asset_type`): the one place that the optimization and the runner share to start the engine."""
    return engine.run(spec, bars, strategy.decide, tick_size=strategy.tick_size, multiplier=strategy.multiplier, symbol=strategy.symbol,
                      protective_orders=strategy.protective_orders, asset_type=strategy.asset_type, span_ns=bar_span_ns(spec.bar),
                      expiry_rule=expiry_rule, latency_ms=latency_ms, root=root)


def _spec_json(spec: BacktestSpec) -> str:
    return json.dumps(spec.canonical(), sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def save_row(db: Database, kind: str, spec: BacktestSpec, body: Mapping[str, Any], *, engine_version: str | None = None, parent_id: int | None = None,
             data_hash: str | None = None) -> int:
    """A finished row of `backtest_runs` for a search or a validation (kind optimize, walkforward, montecarlo): the spec it ran under, its summary
    as JSON and the hash of the data it read. The one place that writes those rows."""
    version = engine_version or engine.status().get("engine_version") or "unknown"
    with db.write() as u:
        return u.session.insert("backtest_runs", {
            "kind": kind, "parent_id": parent_id, "strategy_id": spec.strategy_id, "strategy_version": spec.strategy_version, "desk_id": spec.desk_id,
            "spec_json": _spec_json(spec), "spec_hash": spec.fingerprint(), "data_hash": data_hash, "engine_version": version, "seed": spec.seed,
            "status": "done", "summary_json": json.dumps(dict(body), sort_keys=True, separators=(",", ":"), allow_nan=False), "finished_at": utcnow()})


def open_run(db: Database, spec: BacktestSpec, *, kind: str = "run", parent_id: int | None = None) -> int:
    """The row of a run, status running, before anything is computed: its id names the run in the trade rows, the ledger and the curve file."""
    with db.write() as u:
        return u.session.insert("backtest_runs", {
            "kind": kind, "parent_id": parent_id, "strategy_id": spec.strategy_id, "strategy_version": spec.strategy_version, "desk_id": spec.desk_id,
            "spec_json": _spec_json(spec), "spec_hash": spec.fingerprint(), "engine_version": engine.status().get("engine_version") or "unknown",
            "seed": spec.seed, "status": "running", "started_at": to_db_time(utcnow())})


def _resolve(strategy: Any, registry: Registry | None, spec: BacktestSpec, *, sheet: ContractSheet | None = None, db: Database | None = None) -> Any:
    """The strategy of a run: the one given, a registered strategy, or a composite. An alpha of the registry and a saved draft (an alpha with its
    parameters and its parts) are put in a composite of the factory with the parts they name: by default one contract, no stop and market entries."""
    if strategy is not None:
        return strategy
    try:
        found = registry or Registry.with_builtins()
        if spec.strategy_id in found.entries and found.get(spec.strategy_id).kind == "strategy":
            return found.build(spec.strategy_id, spec.params)
        if spec.strategy_id in found.entries:
            alpha_id, params, parts = spec.strategy_id, dict(spec.params), None
        else:
            alpha_id, params, parts = _draft(spec, found, db)
        contract = sheet or sheet_of(spec.instruments[0], spec.end.date())
        return build_composite(found.build(alpha_id, params), sheet=contract, desk_id=spec.desk_id or 0, line=spec.line, parts=parts,
                               tag=alpha_id[:48])
    except StageError:
        raise
    except Exception as exc:
        raise StageError("signal", "unknown_strategy" if isinstance(exc, KeyError) else _code(exc), str(exc).strip("'\"")[:500]) from exc


def _draft(spec: BacktestSpec, registry: Registry, db: Database | None) -> tuple[str, dict[str, Any], Mapping[str, Any] | None]:
    """The alpha, the parameters and the parts of the saved draft of `spec.strategy_id` at `spec.strategy_version`. The parameters of the spec go
    over the ones of the draft, so an optimization moves the parameters of the alpha."""
    from hokma.factory import spec as saved

    try:
        row = saved.load(spec.strategy_id, spec.strategy_version, db=db)
    except saved.BadSpec as exc:
        raise KeyError(f"no alpha, strategy or saved spec with id {spec.strategy_id!r} version {spec.strategy_version}") from exc
    draft = saved.parse_spec(row.spec_yaml, registry)
    return draft.alpha, {**draft.params, **dict(spec.params)}, draft.parts


def _trade_fills(out: RunOutput, driver: StrategyDriver) -> list[Fill]:
    meta = {k: list(v) for k, v in driver.entry_meta.items()}
    cid = driver.data.instrument
    bars = driver.data.bars
    fills: list[tuple[int, Fill]] = []
    for p in out.priced:
        bar = bars[p.bar_index]
        tag: str | None = p.fill.kind
        context: dict[str, Any] = {}
        if p.fill.kind == "entry":
            queued = meta.get(p.bar_index)
            tag, context = queued.pop(0) if queued else (None, {})
        fills.append((p.fill.ts_ns, Fill(cid, p.fill.side, p.fill.qty, p.price, bar.ts, p.commission, tag, context)))
    for s in out.settlements:
        fills.append((engine._ns(s.ts), Fill(cid, "buy" if s.qty > 0 else "sell", abs(s.qty), s.price, s.ts, s.commission, s.tag)))
    return [f for _ts, f in sorted(fills, key=lambda x: x[0])]


def _statistics(result: BacktestResult, multiplier: Decimal, contract_id: str, rf: float) -> tuple[dict[str, Any], tuple[str, ...]]:
    try:
        return dict(results.summary(result, rf=rf, periods_per_year=PERIODS_PER_YEAR, multipliers={contract_id: multiplier})), ()
    except Exception as exc:                    # noqa: BLE001 - a table of statistics that cannot be built never fails a run that has its trades
        return {}, (f"stats_unavailable:{_code(exc)}",)


def build_result(run_id: int, spec: BacktestSpec, driver: StrategyDriver, out: RunOutput, *, status: str, extra: Sequence[str] = (), rf: float = 0.0) -> BacktestResult:
    """The result of an engine output: trades grouped from the fills (with MAE, MFE, tags and indicator values), the curve, both profits and
    the statistics of `hokma.quant`."""
    data = driver.data
    series = [BarRange(b.ts, b.high, b.low) for b in data.bars]
    grouped = results.group_trades(_trade_fills(out, driver), {data.instrument: series}, {data.instrument: driver.multiplier})
    curve = EquityCurve(tuple(t for t, _ in out.equity), tuple(v for _, v in out.equity))
    rejected = {**out.rejected, **driver.rejected}
    notes = ["no_trades"] if not out.fills else []
    notes += ["position_open_at_end"] if out.open_qty != 0 else []
    warnings = tuple(dict.fromkeys([*data.warnings, *out.warnings, *driver.warnings, *extra, *notes]))
    result = BacktestResult(run_id, status, spec.fingerprint(), data.data_hash, out.engine_version, spec.seed, grouped.positions, curve,    # type: ignore[arg-type]
                            out.realized_pnl_usd, out.mtm_pnl_usd, dict(out.costs_usd), out.ambiguous_bars, rejected, {}, warnings)
    stats, notes = _statistics(result, driver.multiplier, data.instrument, rf)
    return replace(result, stats=stats, warnings=result.warnings + notes)


def _failed(run_id: int, spec: BacktestSpec, error: StageError, data_hash: str = "") -> BacktestResult:
    version = engine.status().get("engine_version") or "unknown"
    return BacktestResult(run_id, "failed", spec.fingerprint(), data_hash, version, spec.seed, (), EquityCurve((), ()), ZERO, ZERO, {}, 0, {}, {},
                          (f"failed_stage:{error.stage}", f"failed_code:{error.code}"))


def _objective(result: BacktestResult) -> float | None:
    block = result.stats.get("sharpe")
    value = getattr(block, "value", None)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def run(spec: BacktestSpec, *, strategy: Any = None, registry: Registry | None = None, source: Source | None = None,
        fetch: Callable[..., Any] | None = None, catalog: Catalog | None = None, db: Database | None = None, curve_dir: Path | None = None,
        hypothesis_id: str | None = None, desk_limits: Mapping[str, Any] | None = None, expiry_rule: Expiry | None = None, latency_ms: int = 0,
        rf: float = 0.0, parent_id: int | None = None, kind: str = "run") -> BacktestResult:
    """Run a backtest of one instrument under `spec` and save it. The strategy is the object given or the one of the registry under
    `spec.strategy_id`; the bars come from `source` (or `fetch` and `catalog` through the data feed). A failure in any stage is a saved run with
    status `failed`, its error and its stage, and a failed trial in the ledger; the engine being absent raises `EngineUnavailable` before any row.
    The same spec, the same data and the same seed give the same result."""
    _lib.require()
    if len(spec.instruments) != 1:
        raise SpecError("bad_spec: a run takes one instrument; run_many takes several")
    database = db or get_database()
    run_id = open_run(database, spec, kind=kind, parent_id=parent_id)
    hypothesis = hypothesis_id or spec.strategy_id
    data_hash = ""
    try:
        data = load_data(spec, source or default_source(fetch=fetch, catalog=catalog))
        data_hash = data.data_hash
        driver = StrategyDriver(_resolve(strategy, registry, spec, sheet=data.sheet, db=database), spec, data, desk_limits=desk_limits)
        root = data.sheet.root if spec.session is not None else None
        try:
            out = run_with(spec, driver, data.bars, expiry_rule=expiry_rule, latency_ms=latency_ms, root=root)
        except EngineUnavailable:
            raise
        except Exception as exc:
            raise (driver.failure if driver.failure is not None else stage_of("fill", exc)) from exc
        if driver.failure is not None:
            raise driver.failure
        floor = None if desk_limits is None or desk_limits.get("capital_floor_usd") is None else Decimal(str(desk_limits["capital_floor_usd"]))
        status, extra = "done", ()
        try:
            blown = stop_on_blowup(out, spec, data.bars, floor=floor, symbol=driver.symbol, multiplier=driver.multiplier, span_ns=bar_span_ns(spec.bar),
                                   asset_type=driver.asset_type)
            if blown is not None:
                out, status = blown.output, "account_blown"
                extra = (f"account_blown_at={blown.at.isoformat()}", f"final_capital_usd={format(blown.final_equity_usd, 'f')}")
            result = build_result(run_id, spec, driver, out, status=status, extra=extra, rf=rf)
            results.record_result(database, result, curve_dir=curve_dir or paths.user_path("backtest", "curves"))
        except Exception as exc:
            raise stage_of("result", exc) from exc
        trials.record(spec.strategy_id, hypothesis, run_id, spec.params, "done", _objective(result), db=database)
        return result
    except EngineUnavailable as exc:
        results.record_failure(database, run_id, f"fill:{_code(exc)}: {exc}")
        trials.record(spec.strategy_id, hypothesis, run_id, spec.params, "failed", None, db=database)
        raise
    except StageError as error:
        results.record_failure(database, run_id, str(error))
        trials.record(spec.strategy_id, hypothesis, run_id, spec.params, "failed", None, db=database)
        return _failed(run_id, spec, error, data_hash)


# ----------------------------------------------------------------------------------------------------------------------- B-35: many instruments
@dataclass(frozen=True)
class ManyResult:
    per_instrument: Mapping[str, BacktestResult]
    aggregate: BacktestResult


def _sum_curves(curves: Sequence[EquityCurve]) -> EquityCurve:
    """The curve of the sum of the accounts: at every time that any curve has a point, the sum of what each one was worth (the last known
    value of a curve that has no point at that time). A drawdown measured on it is the drawdown of the whole, not a sum of drawdowns."""
    times = sorted({t for c in curves for t in c.timestamps})
    last = [c.equity_usd[0] if c.equity_usd else ZERO for c in curves]
    position = [0] * len(curves)
    out: list[Decimal] = []
    for t in times:
        for k, c in enumerate(curves):
            while position[k] < len(c.timestamps) and c.timestamps[position[k]] <= t:
                last[k] = c.equity_usd[position[k]]
                position[k] += 1
        out.append(sum(last, ZERO))
    return EquityCurve(tuple(times), tuple(out))


def run_many(spec: BacktestSpec, make_strategy: Callable[[str], Any], *, source: Source | None = None, fetch: Callable[..., Any] | None = None,
             catalog: Catalog | None = None, db: Database | None = None, curve_dir: Path | None = None, rf: float = 0.0,
             desk_limits: Mapping[str, Any] | None = None) -> ManyResult:
    """The same strategy on several instruments: one result per instrument and one aggregate. The capital is split in equal parts; the
    aggregate adds the profit and loss and its curve is the sum of the curves, so its drawdown is measured on the sum. `make_strategy` builds the
    strategy of one instrument (a strategy is bound to its contract)."""
    limits.check_param("n_instruments", len(spec.instruments))
    if len(set(spec.instruments)) != len(spec.instruments):
        raise SpecError("bad_spec: an instrument appears once in a multi instrument run")
    database = db or get_database()
    parent = open_run(database, spec, kind="run")
    share = spec.capital_usd / len(spec.instruments)
    per: dict[str, BacktestResult] = {}
    for instrument in spec.instruments:
        child = replace(spec, instruments=(instrument,), capital_usd=share)
        per[instrument] = run(child, strategy=make_strategy(instrument), source=source, fetch=fetch, catalog=catalog, db=database, curve_dir=curve_dir,
                              hypothesis_id=spec.strategy_id, desk_limits=desk_limits, rf=rf, parent_id=parent)
    failed = [k for k, r in per.items() if r.status == "failed"]
    if failed:
        results.record_failure(database, parent, f"fill:instrument_failed: {', '.join(failed)}")
        raise StageError("result", "instrument_failed", f"the run of {', '.join(failed)} failed: no aggregate")
    done = list(per.values())
    curve = _sum_curves([r.equity for r in done])
    costs_total = {k: sum((r.costs_usd.get(k, ZERO) for r in done), ZERO) for k in ("commission", "slippage", "swap", "impact")}
    rejected: dict[str, int] = {}
    for r in done:
        for k, v in r.rejected.items():
            rejected[k] = rejected.get(k, 0) + v
    blown = any(r.status == "account_blown" for r in done)
    data_hash = data_feed_hash(r.data_hash for r in done)
    aggregate = BacktestResult(parent, "account_blown" if blown else "done", spec.fingerprint(), data_hash, done[0].engine_version, spec.seed,
                               tuple(t for r in done for t in r.trades), curve, sum((r.realized_pnl_usd for r in done), ZERO),
                               sum((r.mtm_pnl_usd for r in done), ZERO), costs_total, sum(r.ambiguous_bars for r in done), rejected, {},
                               tuple(dict.fromkeys(w for r in done for w in r.warnings)))
    multipliers = {t.contract_id: Decimal(1) for t in aggregate.trades}
    stats, notes = {}, ()
    try:
        stats = dict(results.summary(aggregate, rf=rf, periods_per_year=PERIODS_PER_YEAR, multipliers=multipliers))
    except Exception as exc:                    # noqa: BLE001 - same rule as the statistics of one run
        notes = (f"stats_unavailable:{_code(exc)}",)
    aggregate = replace(aggregate, stats=stats, warnings=aggregate.warnings + notes)
    results.record_result(database, aggregate, curve_dir=curve_dir or paths.user_path("backtest", "curves"))
    return ManyResult(per, aggregate)


def data_feed_hash(parts: Any) -> str:
    """One hash for the data of several instruments: the hash of their hashes in order."""
    import hashlib

    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------------------------------------------------- AH-34: the loop of a desk
DESK_LINES = ("DT", "SWC", "SWO", "LP")


@dataclass(frozen=True)
class StepContext:
    """What a desk strategy sees at one step: the date, the bars closed up to it, the account and the positions of the persistent broker."""
    index: int
    at: date
    bars: Mapping[str, tuple[Bar, ...]]
    account: SimAccount


@dataclass(frozen=True)
class StepRecord:
    at: date
    proposals: int
    filled: int
    blocked: Mapping[str, int]
    equity_usd: Decimal


@dataclass(frozen=True)
class DeskLoopResult:
    line: str
    steps: tuple[StepRecord, ...]
    fills: tuple[BrokerFill, ...]
    equity: EquityCurve
    final: SimAccount
    blocked: Mapping[str, int]


def desk_steps(line: str, dates: Sequence[date], *, events: Sequence[Any] = (), cadence: str | None = None) -> tuple[date, ...]:
    """The steps of a desk loop: DT every session, SWC every day, SWO the days of the events, LP the days of the rebalance (monthly by default)."""
    from hokma.backtest import grid

    days = tuple(sorted(set(dates)))
    if line in ("DT", "SWC"):
        return days
    if line == "SWO":
        return grid.rebalance_grid(days, "event", events=events)
    if line == "LP":
        return grid.rebalance_grid(days, cadence or "monthly")      # type: ignore[arg-type]
    raise SpecError(f"bad_spec: the desk loop runs the lines {', '.join(DESK_LINES)}, not {line!r}")


def run_desk_loop(line: str, *, desk_id: int, strategy: Callable[[StepContext], Sequence[OrderProposal]], bars: Mapping[str, Sequence[Bar]],
                  sheets: Mapping[str, ContractSheet], capital: Decimal, model: Any, desk_limits: Mapping[str, Any] | None = None, events: Sequence[Any] = (),
                  cadence: str | None = None, ticks_against: int = 1, level: int = 2, as_of: datetime | None = None) -> DeskLoopResult:
    """A desk strategy in a loop with a broker that keeps its positions between steps. At each step the strategy sees the bars closed up to
    that date and returns `OrderProposal` values; every one goes through the filter of H2 (`guards_bridge.check`) against the account of the
    broker, and what passes is filled at the next bar's open (never at the close that was seen), with the ticks against and the commission of
    the fee table. A proposal that is blocked is counted with the code of its guard. The steps follow the line: sessions, days, events or rebalance."""
    if line not in DESK_LINES:
        raise SpecError(f"bad_spec: the desk loop runs the lines {', '.join(DESK_LINES)}, not {line!r}")
    if not bars or any(not series for series in bars.values()):
        raise SpecError("bad_spec: a desk loop needs bars for every contract")
    series = {cid: tuple(s) for cid, s in bars.items()}
    for cid in series:
        if cid not in sheets:
            raise SpecError(f"bad_spec: no sheet for {cid}")
    dates = sorted({b.ts.date() for b in next(iter(series.values())) if as_of is None or b.ts <= as_of})
    steps = desk_steps(line, dates, events=events, cadence=cadence)
    limits.check_param("desk_steps", len(steps))
    broker = SimBroker(capital, sheets, model, ticks_against=ticks_against)
    records: list[StepRecord] = []
    curve_t: list[datetime] = []
    curve_v: list[Decimal] = []
    blocked_total: dict[str, int] = {}
    for index, day in enumerate(steps):
        seen = {cid: tuple(b for b in s if b.ts.date() <= day) for cid, s in series.items()}
        marks = {cid: s[-1].close for cid, s in seen.items() if s}
        close_at = max(s[-1].ts for s in seen.values() if s) + timedelta(minutes=1)
        broker.mark(marks, close_at)
        proposals = tuple(strategy(StepContext(index, day, seen, broker.account(close_at, marks))))
        filled, blocked = 0, {}
        for proposal in proposals:
            if proposal.desk_id != desk_id or proposal.line != line:
                raise SpecError("bad_spec: a proposal of the loop is of the desk and the line of the loop")
            verdict = guards_bridge.check(proposal, broker.account(close_at, marks), limits=desk_limits or {}, sheets=sheets, marks=marks, now=close_at,
                                          capital=capital, level=level, state=guards_bridge.monitor_state(close_at))      # type: ignore[arg-type]
            code = guards_bridge.blocked_code(verdict)
            if code is not None:
                blocked[code] = blocked.get(code, 0) + 1
                continue
            for leg in verdict.proposal.legs:
                later = [b for b in series[leg.contract_id] if b.ts.date() > day]
                if not later:
                    blocked["no_next_bar"] = blocked.get("no_next_bar", 0) + 1
                    continue
                broker.fill(leg.contract_id, leg.side, leg.qty, later[0].open, later[0].ts)
                filled += 1
        for code, n in blocked.items():
            blocked_total[code] = blocked_total.get(code, 0) + n
        equity = broker.equity(marks)
        curve_t.append(close_at)
        curve_v.append(equity)
        records.append(StepRecord(day, len(proposals), filled, blocked, equity))
    final_marks = {cid: s[-1].close for cid, s in series.items()}
    return DeskLoopResult(line, tuple(records), tuple(broker.fills), EquityCurve(tuple(curve_t), tuple(curve_v)),
                          broker.account(curve_t[-1] if curve_t else datetime.now(timezone.utc), final_marks), blocked_total)


__all__ = ["Blowup", "DeskLoopResult", "InstrumentData", "ManyResult", "Services", "SimBroker", "StageError", "StepContext", "StrategyDriver",
           "build_result", "default_source", "desk_steps", "load_data", "open_run", "run", "run_desk_loop", "run_many", "run_with", "save_row",
           "spec_from_canonical", "stop_on_blowup", "wish_signals"]

"""The backtest run on the engine (SPEC-020, N-27): bars and a decision function in, fills, trades and costs out.

The strategy decides on the close of each bar; the order is sent at the next open (fills.py), the bars become synthetic ticks
(ticks.py), the engine matches them (_lib.py, the only file that imports it) and everything that is a number of the result is read
back from the fills here, in Decimal: the commission from the fee tables, the slippage against the reference, the impact, the trades.
The statistics of the result come later from `hokma.quant.metrics`, never from the engine.

Around the run, in Hokma code: the margin or cash account (`account`), the latency of an order (`latency`), the settlement of a contract
that expires (`expiry`) and of the end of the data (`end_of_data`), the session gate (`spec.session`) and the proof that two runs of one
spec are the same run (`deterministic`)."""
from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from hokma.backtest import _lib, costs
from hokma.backtest._lib import Decide, EngineFill, EngineUnavailable, InstrumentSpec
from hokma.backtest.fills import Decision, Mode, against, choose_mode
from hokma.backtest.results import BarRange, Fill, Trade, group_trades
from hokma.backtest.spec import BacktestSpec, SpecError
from hokma.backtest.ticks import BAR_NS, OPEN_OFFSET_NS, Bar, SyntheticTick, bar_to_ticks
from hokma.contracts import sessions
from hokma.contracts.ticks import VARIABLE_TICK_ASSETS, tick_scheme
from hokma.exec.money import round_to_tick
from hokma.quant import limits

ZERO = Decimal(0)
AccountKind = Literal["margin", "cash"]
MARGIN_ASSETS = ("future", "future_option", "cfd", "option")


class NonDeterministic(RuntimeError):
    """Two runs of the same spec, data and seed gave different results. `code` is stable: non_deterministic."""

    code = "non_deterministic"


@dataclass(frozen=True)
class PricedFill:
    """A fill of the engine with the price Hokma charged (impact, latency and the grid of the tick applied) and its commission."""
    fill: EngineFill
    price: Decimal
    commission: Decimal
    bar_index: int
    base: Decimal                                 # the price before the impact and the grid: the fill price, or the one after the latency


@dataclass(frozen=True)
class Settlement:
    """A synthetic closing execution: the contract expired, the data ended or the account was blown. It is not an operation of the
    strategy, so it is not in `RunOutput.fills`; the trade it closes carries its tag as `exit_tag`."""
    ts: datetime
    qty: Decimal                                  # signed: the quantity that closes the position
    price: Decimal
    tag: Literal["expiry", "end_of_data", "account_blown"]
    bar_index: int
    commission: Decimal = ZERO                    # what the execution paid; zero for a settlement that the exchange makes (expiry)


@dataclass(frozen=True)
class Expiry:
    """The contract stops trading at `at`; `settlement` is the price the exchange settled it at (a real data point, never a default)."""
    at: datetime
    settlement: Decimal


@dataclass(frozen=True)
class Balance:
    """The account at the end of a run: what it started with, the price result, what it paid and what is left (N-08)."""
    kind: AccountKind
    capital_usd: Decimal
    gross_pnl_usd: Decimal                        # the price result of every position, open ones marked at the last close
    costs_usd: Decimal                            # commission
    final_usd: Decimal                            # capital plus gross result minus costs
    min_cash_usd: Decimal | None                  # the lowest cash a cash account reached (it never holds less than zero); None on margin


@dataclass(frozen=True)
class RunOutput:
    mode: Mode                                 # which feed ran: "fast" or "ticks" (the result always says it)
    fills: tuple[EngineFill, ...]              # the fills of the strategy's orders; a settlement is not one of them
    trades: tuple[Trade, ...]                  # closed positions (`results.group_trades`, flat to flat); pnl_usd is net of costs_usd, the commission of its sides
    realized_pnl_usd: Decimal                  # what the closing fills made, net of commission (a position closed in parts counts what is closed)
    mtm_pnl_usd: Decimal                       # the whole result at the last mark: realized plus the open position marked, net of commission
    costs_usd: Mapping[str, Decimal]           # commission, slippage, swap, impact
    ambiguous_bars: int
    rejected: Mapping[str, int]                # reject_prob, and when they happened outside_session, cash_account
    engine_version: str
    open_qty: Decimal                          # signed quantity still open at the end
    priced: tuple[PricedFill, ...] = ()
    settlements: tuple[Settlement, ...] = ()
    equity: tuple[tuple[datetime, Decimal], ...] = ()      # (close time of the bar, equity) at every bar close; a settlement adds a point
    account: Balance | None = None
    warnings: tuple[str, ...] = ()


def status() -> dict[str, Any]:
    """Whether the engine can run here, for the status tool and the doctor: never raises."""
    if not _lib.available():
        return {"available": False, "code": _lib.ENGINE_UNAVAILABLE, "engine_version": None, "analyzer_hook": False}
    return {"available": True, "code": None, "engine_version": _lib.engine_version(), "analyzer_hook": _lib.hook_present()}


def _dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 10**9, tz=timezone.utc)


def _ns(moment: datetime) -> int:
    return int(moment.timestamp()) * 10**9 + moment.microsecond * 1_000


def _reference(fill: EngineFill, bar: Bar) -> Decimal:
    """The price an order was waiting for. A stop that the open already passed was waiting at the open (a gap), not at its level."""
    if fill.kind == "stop" and fill.reference is not None:
        crossed = bar.open >= fill.reference if fill.side == "buy" else bar.open <= fill.reference
        return bar.open if crossed else fill.reference
    return fill.reference if fill.reference is not None else bar.open


def trades_of(symbol: str, bars: Sequence[Bar], priced: Sequence[PricedFill], settlements: Sequence[Settlement],
              multiplier: Decimal) -> tuple[tuple[Trade, ...], Decimal, Decimal]:
    """The closed positions of a run, the profit and loss of what its closing fills made and the signed open quantity. The netting is the
    one of `results.group_trades` (first in, first out; the runner uses it for the result too): there is no second implementation. A fill is
    placed at the open of its bar, so the bars it lived through are the ones from that bar on; a settlement keeps its own time."""
    fills: list[tuple[int, Fill]] = []
    for p in priced:
        tag = None if p.fill.kind == "entry" else p.fill.kind
        fills.append((p.fill.ts_ns, Fill(symbol, p.fill.side, p.fill.qty, p.price, bars[p.bar_index].ts, p.commission, tag)))
    for s in settlements:
        fills.append((_ns(s.ts), Fill(symbol, "buy" if s.qty > 0 else "sell", abs(s.qty), s.price, s.ts, s.commission, s.tag)))
    ordered = [f for _ts, f in sorted(fills, key=lambda x: x[0])]
    if not ordered:
        return (), ZERO, ZERO
    grouped = group_trades(ordered, {symbol: [BarRange(b.ts, b.high, b.low) for b in bars]}, {symbol: multiplier})
    closed = tuple(t for t in grouped.positions if t.exit_at is not None)
    realized = sum((t.pnl_usd for t in grouped.exit_trades if t.pnl_usd is not None), ZERO)
    open_qty = sum((f.qty if f.side == "buy" else -f.qty for f in ordered), ZERO)
    return closed, realized, open_qty


# ---- N-11: latency ----

def latency(bar: Bar, side: Literal["buy", "sell"], latency_ns: int, *, span_ns: int = BAR_NS) -> SyntheticTick:
    """The tick that an order sent at the open tick of `bar` meets after `latency_ns`: the first tick of the bar at or after the arrival
    (the last one when it arrives after all of them). The ticks are built in the order that hurts the order: a buy meets the high before
    the low, a sell the low before the high. Zero latency is the open tick."""
    if latency_ns < 0 or isinstance(latency_ns, bool):
        raise SpecError("bad_spec: a latency is zero or more nanoseconds")
    held = "short" if side == "buy" else "long"           # a short position meets its high first, a long its low first
    ticks = bar_to_ticks(bar, side=held, span_ns=span_ns)
    arrival = OPEN_OFFSET_NS + latency_ns
    return next((t for t in ticks if t.offset_ns >= arrival), ticks[-1])


# ---- N-08: the account ----

def account_kind(asset_type: str) -> AccountKind:
    """Futures, options and CFDs trade on margin; shares and ETF (the LP line) are paid in full from the cash of a cash account."""
    return "margin" if asset_type in MARGIN_ASSETS else "cash"


def account(kind: AccountKind, capital: Decimal, priced: Sequence[PricedFill], settlements: Sequence[Settlement], *, multiplier: Decimal,
            mark: Decimal) -> Balance:
    """The balance of an account from its executions: cash flows in time order, the open quantity marked at `mark`. The final balance is
    capital plus the price result minus the commission (the tests check it against the trades and the equity curve)."""
    cash, qty, fees = capital, ZERO, ZERO
    low = capital
    flows = sorted([(p.fill.ts_ns, p.fill.side, p.fill.qty, p.price, p.commission) for p in priced]
                   + [(_ns(s.ts), "buy" if s.qty > 0 else "sell", abs(s.qty), s.price, s.commission) for s in settlements], key=lambda f: f[0])
    for _ts, side, amount, price, fee in flows:
        signed = amount if side == "buy" else -amount
        cash += -signed * price * multiplier - fee
        qty += signed
        fees += fee
        if kind == "cash":
            low = min(low, cash)
    gross = cash + qty * mark * multiplier + fees - capital
    final = capital + gross - fees
    return Balance(kind, capital, gross, fees, final, low if kind == "cash" else None)


# ---- L-25: expiry and the end of the data ----

def expiry(open_qty: Decimal, rule: Expiry | None, bar_index: int) -> Settlement | None:
    """The execution that a contract that expires makes on a position left open: it is closed at the settlement price at the expiry time.
    It is tagged `expiry` and it is not an operation of the strategy. None when the position is flat or the contract does not expire."""
    if rule is None or open_qty == 0:
        return None
    return Settlement(rule.at, -open_qty, rule.settlement, "expiry", bar_index)


def end_of_data(open_qty: Decimal, bar: Bar, bar_index: int, span_ns: int, *, price: Decimal, commission: Decimal = ZERO) -> Settlement | None:
    """The synthetic close of `finalize = "close_at_end"`, tagged `end_of_data`. A bar after the last one does not exist, so the next open rule
    cannot apply: the position is closed at the close of the last bar with the same ticks against, impact, grid and commission as any other
    order (`run` prices it and passes `price` and `commission`)."""
    if open_qty == 0:
        return None
    return Settlement(bar.ts + timedelta(microseconds=span_ns // 1_000), -open_qty, price, "end_of_data", bar_index, commission)


# ---- the equity curve and the output ----

def _curve(bars: Sequence[Bar], span_ns: int, capital: Decimal, multiplier: Decimal, priced: Sequence[PricedFill],
           settlements: Sequence[Settlement]) -> tuple[tuple[datetime, Decimal], ...]:
    """Equity at every bar close: cash flows up to the bar plus the open quantity at the close. A settlement after the last bar adds a point."""
    steps: dict[int, list[tuple[int, Decimal, Decimal, Decimal]]] = {}
    for p in priced:
        signed = p.fill.qty if p.fill.side == "buy" else -p.fill.qty
        steps.setdefault(p.bar_index, []).append((p.fill.ts_ns, signed, p.price, p.commission))
    for s in settlements:
        steps.setdefault(s.bar_index, []).append((_ns(s.ts), s.qty, s.price, s.commission))
    cash, qty = capital, ZERO
    points: list[tuple[datetime, Decimal]] = []
    span = timedelta(microseconds=span_ns // 1_000)
    for i, bar in enumerate(bars):
        for _ts, signed, price, fee in sorted(steps.get(i, ()), key=lambda s: s[0]):
            cash += -signed * price * multiplier - fee
            qty += signed
        points.append((bar.ts + span, cash + qty * bar.close * multiplier))
    late = [s for s in settlements if s.bar_index >= len(bars)]
    for s in late:
        cash += -s.qty * s.price * multiplier - s.commission
        qty += s.qty
        points.append((s.ts, cash + qty * bars[-1].close * multiplier))
    return tuple(points)


def build(spec: BacktestSpec, bars: Sequence[Bar], priced: Sequence[PricedFill], settlements: Sequence[Settlement], *, symbol: str,
          multiplier: Decimal, span_ns: int, asset_type: str, mode: Mode, ambiguous: int, rejected: Mapping[str, int], engine_version: str,
          slippage: Decimal, impact: Decimal, fills: Sequence[EngineFill], warnings: Sequence[str] = ()) -> RunOutput:
    """The output of a run from its priced executions and its settlements. A run and a truncated run (`truncate`) are built the same way."""
    trades, realized, open_qty = trades_of(symbol, bars, priced, settlements, multiplier)
    curve = _curve(bars, span_ns, spec.capital_usd, multiplier, priced, settlements)
    commission = sum((p.commission for p in priced), ZERO) + sum((s.commission for s in settlements), ZERO)
    kind = account_kind(asset_type)
    balance = account(kind, spec.capital_usd, priced, settlements, multiplier=multiplier, mark=bars[-1].close)
    return RunOutput(mode, tuple(fills), trades, realized, curve[-1][1] - spec.capital_usd,
                     {"commission": commission, "slippage": slippage, "swap": ZERO, "impact": impact}, ambiguous, dict(rejected), engine_version,
                     open_qty, tuple(priced), tuple(settlements), curve, balance, tuple(warnings))


def truncate(out: RunOutput, spec: BacktestSpec, bars: Sequence[Bar], index: int, *, symbol: str, multiplier: Decimal, span_ns: int,
             asset_type: str, tag: Literal["account_blown"]) -> RunOutput:
    """The run as it was when bar `index` closed, with the open position closed at that close (tagged `tag`). An engine run depends only
    on the bars before it, so cutting the executions is the same as running the shorter series."""
    kept = [p for p in out.priced if p.bar_index <= index]
    open_qty = sum((p.fill.qty if p.fill.side == "buy" else -p.fill.qty for p in kept), ZERO)
    last = bars[index]
    settle = () if open_qty == 0 else (Settlement(last.ts + timedelta(microseconds=span_ns // 1_000), -open_qty, last.close, tag, index),)
    slip = sum((abs(p.base - _reference(p.fill, bars[p.bar_index])) * p.fill.qty * multiplier for p in kept), ZERO)
    imp = sum((abs(p.price - p.base) * p.fill.qty * multiplier for p in kept), ZERO)
    return build(spec, bars[: index + 1], kept, settle, symbol=symbol, multiplier=multiplier, span_ns=span_ns, asset_type=asset_type, mode=out.mode,
                 ambiguous=out.ambiguous_bars, rejected=out.rejected, engine_version=out.engine_version, slippage=slip, impact=imp,
                 fills=tuple(p.fill for p in kept), warnings=out.warnings)


def _on_grid(asset_type: str, side: str, price: Decimal, tick: Decimal) -> Decimal:
    """The price on the grid of its level when the contract has a scheme by level (options); any other contract keeps the price."""
    if asset_type not in VARIABLE_TICK_ASSETS:
        return price
    return round_to_tick(price, tick_scheme(asset_type, price, tick), direction="up" if side == "buy" else "down")


def run(spec: BacktestSpec, bars: Sequence[Bar], decide: Decide, *, tick_size: Decimal, multiplier: Decimal, symbol: str,
        protective_orders: bool, asset_type: str = "future", span_ns: int = BAR_NS, daily_volume: Decimal | None = None,
        daily_volatility_bps: Decimal | None = None, latency_ms: int = 0, expiry_rule: Expiry | None = None, root: str | None = None) -> RunOutput:
    """Run `decide` over `bars` under the fill rules and costs of `spec`. `protective_orders` says whether the strategy uses stops or
    targets: without them the run is fast (open tick and bar). `latency_ms` delays the market orders (see `latency`), `expiry_rule` closes
    a position left open when the contract expires, `root` is the futures root that `spec.session` is checked against. Answers
    `engine_unavailable` (EngineUnavailable) when the engine cannot run here; a bad spec or input is a SpecError with its stable code."""
    limits.check_param("n_observations", len(bars))
    _lib.require()
    if spec.costs.slippage_ticks != spec.fill.next_open_ticks:
        raise SpecError("bad_spec: the ticks against of the cost model and of the fill rule are the same number")
    if tick_size <= 0 or multiplier <= 0:
        raise SpecError("bad_spec: tick size and multiplier are above zero")
    if spec.costs.cfd_swap:
        raise SpecError("bad_spec: the overnight swap is charged on CFD runs, which this feed does not run")
    if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0:
        raise SpecError("bad_spec: a latency is zero or more milliseconds")
    if spec.session is not None and root is None:
        raise SpecError("bad_spec: a session needs the root of the contract it is checked against")
    if expiry_rule is not None:
        bars = [b for b in bars if b.ts < expiry_rule.at]
    if not bars:
        raise SpecError("bad_spec: no bars to run")
    mode = choose_mode(protective_orders)
    kind = account_kind(asset_type)
    fee = costs.fee_model(spec.costs, asset_type)
    rate = fee.engine_rate()
    refused = {"outside_session": 0, "cash_account": 0}
    committed = [ZERO]                                  # notional of the entries of a cash account that are still open

    def checked(i: int, bar: Bar, side: Any) -> Sequence[Decision]:
        decisions = decide(i, bar, side)
        if mode == "fast" and any(d.protective for d in decisions):
            raise SpecError("protective_in_fast_mode: the run was declared with no protective orders")
        if not decisions or i + 1 >= len(bars):
            return decisions
        if spec.session is not None and not sessions.is_open(str(root), bars[i + 1].ts, session=spec.session):
            refused["outside_session"] += len(decisions)        # an order is not sent while its session is closed
            return ()
        if kind != "cash":
            return decisions
        if side is None:
            committed[0] = ZERO
        allowed: list[Decision] = []
        for d in decisions:
            if d.action == "exit":
                committed[0] = ZERO
                allowed.append(d)
                continue
            need = bars[i + 1].open * d.qty * multiplier
            if d.side == "short" or committed[0] + need > spec.capital_usd:
                refused["cash_account"] += 1                     # a cash account does not sell short and pays in full
                continue
            committed[0] += need
            allowed.append(d)
        return allowed

    outcome = _lib.run_bars(bars=bars, instrument=InstrumentSpec(symbol, tick_size, multiplier, price_precision=max(-tick_size.as_tuple().exponent, 2)),
                            decide=checked, rules=spec.fill, starting_balance=spec.capital_usd, per_contract_fee=rate, seed=spec.seed,
                            feed=mode, span_ns=BAR_NS)      # the engine takes the bars as one minute bars: the order of the ticks inside a bar is all it needs
    starts = [int(b.ts.timestamp()) * 10**9 for b in bars]
    # The engine closes what is still open when its instrument expires, a year after the last bar. That close is not a decision of the
    # strategy and it is priced off the last tick: it is dropped, and what the run does with a position left open is the finalize rule.
    taken = tuple(f for f in outcome.fills if f.ts_ns < starts[-1] + span_ns)
    priced: list[PricedFill] = []
    slippage = impact = ZERO
    for fill in taken:
        i = bisect_right(starts, fill.ts_ns) - 1
        bar = bars[i]
        base = fill.price
        if latency_ms and fill.kind in ("entry", "exit"):
            arrival = latency(bar, fill.side, latency_ms * 1_000_000, span_ns=span_ns)
            base = against(fill.side, arrival.price, spec.fill.next_open_ticks, tick_size)
        price = costs.apply_impact(spec.costs, fill.side, base, fill.qty, bar_volume=bar.volume or None, daily_volume=daily_volume,
                                   daily_volatility_bps=daily_volatility_bps)
        price = _on_grid(asset_type, fill.side, price, tick_size)
        fee_usd = fill.commission if rate is not None else costs.commission_usd(fee, qty=fill.qty, price=price, multiplier=multiplier)
        slippage += abs(base - _reference(fill, bar)) * fill.qty * multiplier
        impact += abs(price - base) * fill.qty * multiplier
        priced.append(PricedFill(fill, price, fee_usd, i, base))
    held = sum((p.fill.qty if p.fill.side == "buy" else -p.fill.qty for p in priced), ZERO)
    settlements: list[Settlement] = []
    warnings: list[str] = []
    cut = expiry(held, expiry_rule, len(bars))
    if cut is None and spec.finalize == "close_at_end" and held != 0:
        last, side = bars[-1], "sell" if held > 0 else "buy"
        base = against(side, last.close, spec.fill.next_open_ticks, tick_size)
        price = costs.apply_impact(spec.costs, side, base, abs(held), bar_volume=last.volume or None, daily_volume=daily_volume,
                                   daily_volatility_bps=daily_volatility_bps)
        price = _on_grid(asset_type, side, price, tick_size)
        cut = end_of_data(held, last, len(bars) - 1, span_ns, price=price, commission=costs.commission_usd(fee, qty=abs(held), price=price, multiplier=multiplier))
        slippage += abs(base - last.close) * abs(held) * multiplier
        impact += abs(price - base) * abs(held) * multiplier
    if cut is not None:
        settlements.append(cut)
        warnings.append(f"closed_by_{cut.tag}")
    rejected = {"reject_prob": outcome.rejected, **{k: v for k, v in refused.items() if v}}
    return build(spec, bars, priced, settlements, symbol=symbol, multiplier=multiplier, span_ns=span_ns, asset_type=asset_type, mode=mode,
                 ambiguous=outcome.ambiguous_bars, rejected=rejected, engine_version=outcome.engine_version, slippage=slippage, impact=impact,
                 fills=taken, warnings=warnings)


# ---- N-35: determinism ----

def fingerprint(out: RunOutput) -> str:
    """A hash of everything that a run produced: fills, trades, equity, costs and counts. Two equal hashes are two identical runs."""
    body = {"mode": out.mode, "fills": [[f.ts_ns, f.side, str(f.qty), str(f.price), str(f.commission), f.kind] for f in out.fills],
            "priced": [[str(p.price), str(p.commission), p.bar_index] for p in out.priced],
            "settlements": [[s.ts.isoformat(), str(s.qty), str(s.price), s.tag, str(s.commission)] for s in out.settlements],
            "trades": [[t.side, str(t.qty), t.entry_at.isoformat(), str(t.entry_price), None if t.exit_at is None else t.exit_at.isoformat(),
                        str(t.exit_price), str(t.pnl_usd), str(t.costs_usd), t.exit_tag] for t in out.trades],
            "equity": [[t.isoformat(), str(v)] for t, v in out.equity], "realized": str(out.realized_pnl_usd), "mtm": str(out.mtm_pnl_usd),
            "costs": {k: str(v) for k, v in sorted(out.costs_usd.items())}, "ambiguous": out.ambiguous_bars, "rejected": dict(sorted(out.rejected.items())),
            "engine": out.engine_version, "open": str(out.open_qty)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def deterministic(spec: BacktestSpec, bars: Sequence[Bar], make_decide: Callable[[], Decide], *, repeats: int = 2, **kwargs: Any) -> RunOutput:
    """Run the same spec `repeats` times, each with a fresh decision function, and return the run only if every copy is identical
    (same `fingerprint`). The seed is in the spec: the same seed and the same bars always give the same run. Raises `NonDeterministic`."""
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 2 <= repeats <= 10:
        raise SpecError("bad_spec: repeats is a whole number from 2 to 10")
    first = run(spec, bars, make_decide(), **kwargs)
    digest = fingerprint(first)
    for _ in range(repeats - 1):
        if fingerprint(run(spec, bars, make_decide(), **kwargs)) != digest:
            raise NonDeterministic("two runs of the same spec, data and seed gave different results")
    return first


__all__ = ["Balance", "EngineUnavailable", "Expiry", "NonDeterministic", "PricedFill", "RunOutput", "Settlement", "account", "account_kind",
           "build", "deterministic", "end_of_data", "expiry", "fingerprint", "latency", "run", "status", "tick_scheme", "trades_of", "truncate"]

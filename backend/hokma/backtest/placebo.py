"""Controls that a strategy has to beat, and Monte Carlo over a saved run (SPEC-020: B-31, V-04, V-27).

`random_ohlc` makes a series with the same returns in another order, `buy_and_hold` is the reference of owning the instrument, and
`random_signals` makes entries at random moments with the same number of trades and the same holding time as the strategy under test. `simulate`
resamples the equity of a saved run (bootstrap, permutation) or runs the placebo against its result, on the database and the bars it is given;
`monte_carlo` is the public entry point (spec 4.2): it finds the run, its spec and its bars itself and saves the result. Everything is seeded with a numpy generator, so
the same inputs and seed give the same output; the figures of the first two methods come from `hokma.quant.metrics.montecarlo`, not from here."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

import numpy as np

from hokma.backtest import costs as backtest_costs
from hokma.backtest import results, sweep
from hokma.backtest.results import Trade
from hokma.backtest.spec import CostModel, SpecError
from hokma.backtest.sweep import SweepCosts
from hokma.backtest.ticks import Bar
from hokma.db import Database
from hokma.quant import limits
from hokma.quant.metrics import montecarlo

METHODS = ("bootstrap", "permutation", "placebo")


class PlaceboError(ValueError):
    """A control or a Monte Carlo run cannot be built. `code` is stable: bad_placebo, bad_run."""

    code = "bad_placebo"


def _snap(value: float, tick: Decimal) -> Decimal:
    """`value` on the grid of the tick, half up (a price that is not on the grid does not exist)."""
    return (Decimal(repr(float(value))) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


# --------------------------------------------------------------------------------------------------------------
# B-31: a random series with the returns of the original
# --------------------------------------------------------------------------------------------------------------
def random_ohlc(bars: Sequence[Bar], *, seed: int, tick_size: Decimal, n: int | None = None) -> tuple[Bar, ...]:
    """A series with the returns of `bars` in another order, as a control and for Monte Carlo.

    Every bar after the first gives a record: the gap from the previous close, the body, the high and the low (all relative to its own open) and
    its volume. The series starts at the first bar and chains the records in a seeded random order. With the same length, the records are
    permuted, so the close to close returns are the same numbers (the same mean and deviation, up to the rounding of prices to the tick);
    with another length `n` they are drawn with replacement. Each bar keeps its own shape, so the high is never below the open or the close and
    the low is never above them."""
    if len(bars) < 3:
        raise PlaceboError("bad_placebo: a random series needs at least three bars")
    if tick_size <= 0:
        raise PlaceboError("bad_placebo: the tick size is above zero")
    length = len(bars) if n is None else limits.as_int("n", n)
    limits.check_param("n_observations", length)
    if length < 2:
        raise PlaceboError("bad_placebo: a random series has at least two bars")
    rng = np.random.default_rng(seed)
    gap, body, up, down = (np.array([float(f(b, p)) for p, b in zip(bars, bars[1:])]) for f in (
        lambda b, p: math.log(b.open / p.close), lambda b, p: math.log(b.close / b.open), lambda b, p: math.log(b.high / b.open),
        lambda b, p: math.log(b.low / b.open)))
    volume = [b.volume for b in bars[1:]]
    if length == len(bars):
        order = rng.permutation(len(bars) - 1)
    else:
        order = rng.integers(0, len(bars) - 1, size=length - 1)
    span = bars[1].ts - bars[0].ts
    out = [bars[0]]
    close = bars[0].close
    for k, j in enumerate(order, start=1):
        open_ = _snap(float(close) * math.exp(gap[j]), tick_size)
        shut = _snap(float(open_) * math.exp(body[j]), tick_size)
        high = max(_snap(float(open_) * math.exp(up[j]), tick_size), open_, shut)
        low = min(_snap(float(open_) * math.exp(down[j]), tick_size), open_, shut)
        out.append(Bar(bars[k].ts if length == len(bars) else bars[0].ts + span * k, open_, high, low, shut, volume[j]))
        close = shut
    return tuple(out)


# --------------------------------------------------------------------------------------------------------------
# V-04: buy and hold and random signals
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class BuyAndHold:
    round_trip: backtest_costs.RoundTrip      # one entry and one exit with the full cost model
    price_return: Decimal                     # last close over first open, minus one: the return of the price
    bars: int


def buy_and_hold(bars: Sequence[Bar], *, tick_size: Decimal, multiplier: Decimal, model: CostModel, qty: Decimal = Decimal(1),
                 asset_type: str = "future") -> BuyAndHold:
    """The reference of owning the instrument over the whole window: buy at the open of the first bar and sell at the close of the last, each
    with the ticks against and the commission (`hokma.backtest.costs.round_trip`). A strategy that does not beat it, after costs, adds nothing."""
    if not bars:
        raise PlaceboError("bad_placebo: no bars")
    trip = backtest_costs.round_trip("long", entry_ref=bars[0].open, exit_ref=bars[-1].close, qty=qty, tick_size=tick_size, multiplier=multiplier,
                                     model=model, asset_type=asset_type)
    return BuyAndHold(trip, bars[-1].close / bars[0].open - 1, len(bars))


def random_signals(n_bars: int, *, n_trades: int, mean_hold: int, seed: int, count: int = 1, side: Literal["long", "short", "both"] = "long") -> np.ndarray:
    """`count` random signal vectors (rows) for the sweep: each has exactly `n_trades` entries, each held `mean_hold` bars, at random moments with
    at least one flat bar between two trades. The number of trades and the holding time are those of the strategy under test, so the
    frequency of operations is the same and only the timing is random. `both` draws the side of every trade. The signal of the last bar is
    never used (it has no next bar), so every trade is closed by a signal before it."""
    n_bars, n_trades, mean_hold, count = (limits.as_int(name, v) for name, v in (("n_observations", n_bars), ("n_trades", n_trades),
                                                                                   ("mean_hold", mean_hold), ("n_combinations", count)))
    limits.check_param("n_observations", n_bars)
    limits.check_param("n_combinations", count)
    if min(n_trades, mean_hold, count) < 1 or side not in ("long", "short", "both"):
        raise PlaceboError("bad_placebo: trades, hold and count are at least 1 and side is long, short or both")
    free = (n_bars - 2) - n_trades * mean_hold - (n_trades - 1)            # trades end before the second to last bar: their exit signal is used
    if free < 0:
        raise PlaceboError(f"too_many_trades: {n_trades} trades of {mean_hold} bars do not fit in {n_bars} bars")
    rng = np.random.default_rng(seed)
    out = np.zeros((count, n_bars), dtype=np.int8)
    for row in range(count):
        cuts = np.sort(rng.integers(0, free + 1, size=n_trades))                  # how much of the free time is spent before each trade
        before = np.diff(cuts, prepend=0)
        at = 0
        for t in range(n_trades):
            at += int(before[t]) + (1 if t else 0)
            sign = 1 if side == "long" else -1 if side == "short" else int(rng.choice((-1, 1)))
            out[row, at:at + mean_hold] = sign
            at += mean_hold
    return out


@dataclass(frozen=True)
class Profile:
    n_trades: int
    mean_hold: int                            # bars, at least 1
    side: Literal["long", "short", "both"]


def profile(trades: Sequence[Trade], bars: Sequence[Bar]) -> Profile:
    """The number of closed trades, the average holding time in bars and the side(s) of a run: what the placebo has to copy."""
    closed = [t for t in trades if t.exit_at is not None]
    if not closed:
        raise PlaceboError("bad_run: the run has no closed trade to copy")
    starts = np.array([b.ts.timestamp() for b in bars])
    if closed[0].entry_at.timestamp() < starts[0] or max(t.exit_at.timestamp() for t in closed) > starts[-1] + 1e-6:
        raise PlaceboError("bad_run: the bars are not the ones of the run (a trade lies outside them)")
    hold = [max(1, int(np.searchsorted(starts, t.exit_at.timestamp(), side="right")) - int(np.searchsorted(starts, t.entry_at.timestamp(), side="right")))
            for t in closed]
    sides = {t.side for t in closed}
    return Profile(len(closed), max(1, round(sum(hold) / len(hold))), "both" if len(sides) > 1 else sides.pop())


# --------------------------------------------------------------------------------------------------------------
# Monte Carlo over a saved run
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PlaceboInputs:
    """What the placebo needs besides the saved run: the bars the run was made on and the cost of one contract."""
    bars: Sequence[Bar]
    costs: SweepCosts


@dataclass(frozen=True)
class MonteCarloResult:
    run_id: int
    method: str
    n: int
    seed: int
    figures: Mapping[str, Any]
    warnings: tuple[str, ...]
    p_value: float | None = None              # placebo only: the share of random runs (counting the real one) at least as good as the real one

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "method": self.method, "n": self.n, "seed": self.seed, "figures": dict(self.figures),
                "warnings": list(self.warnings), "p_value": self.p_value}


def _spread(values: np.ndarray) -> dict[str, float]:
    return {"min": float(values.min()), "percentile_5": float(np.percentile(values, 5)), "median": float(np.percentile(values, 50)),
            "percentile_95": float(np.percentile(values, 95)), "max": float(values.max()), "mean": float(values.mean()), "std": float(values.std(ddof=1))}


def _saved_mtm(db: Database, run_id: int) -> float:
    with db.read() as u:
        row = u.session.one("SELECT status, summary_json FROM backtest_runs WHERE id = :i", {"i": int(run_id)})
    if row is None or row["status"] not in ("done", "account_blown") or not row["summary_json"]:
        raise PlaceboError(f"bad_run: run {run_id} is not a finished run with a result")
    return float(Decimal(json.loads(row["summary_json"])["mtm_pnl_usd"]))


def simulate(run_id: int, *, n: int, method: Literal["bootstrap", "permutation", "placebo"], seed: int, db: Database, curve_dir: Path,
             placebo: PlaceboInputs | None = None) -> MonteCarloResult:
    """Monte Carlo over the saved run `run_id`, `n` paths, seeded.

    bootstrap: stationary bootstrap of the daily returns of the equity (Sharpe and CAGR distributions); permutation: the same returns in another
    order (maximum drawdown); both are the functions of `hokma.quant.metrics.montecarlo`. placebo: `n` random runs with the trades and holding time
    of the real one (`random_signals`, priced by the sweep) and the share that did at least as well as the real result. `curve_dir` is set by the
    service, never by an agent."""
    if method not in METHODS:
        raise PlaceboError(f"bad_placebo: method must be one of {', '.join(METHODS)}")
    runs = limits.as_int("runs", n)
    limits.check_param("runs", runs)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise PlaceboError("bad_placebo: seed must be a whole number")
    if method == "placebo":
        return _placebo(run_id, runs, seed, db, placebo)
    returns = results.equity_returns(results.load_curve(db, run_id, curve_dir=Path(curve_dir)))
    if method == "bootstrap":
        parts = {"sharpe": montecarlo.sharpe(returns, runs=runs, seed=seed), "cagr": montecarlo.cagr(returns, runs=runs, seed=seed)}
    else:
        parts = {"max_drawdown": montecarlo.drawdown(returns, runs=runs, seed=seed)}
    warnings = tuple(dict.fromkeys(w for r in parts.values() for w in r.warnings))
    return MonteCarloResult(int(run_id), method, runs, seed, {name: r.value for name, r in parts.items()}, warnings)


def _placebo(run_id: int, runs: int, seed: int, db: Database, placebo: PlaceboInputs | None) -> MonteCarloResult:
    if placebo is None:
        raise PlaceboError("bad_placebo: the placebo needs the bars and the costs of the run (placebo=PlaceboInputs(...))")
    observed = _saved_mtm(db, run_id)
    copy = profile(results.load_trades(db, run_id), placebo.bars)
    signals = random_signals(len(placebo.bars), n_trades=copy.n_trades, mean_hold=copy.mean_hold, seed=seed, count=runs, side=copy.side)
    scores = sweep.sweep_signals(placebo.bars, signals, placebo.costs).mtm
    better = int((scores >= observed).sum())
    return MonteCarloResult(int(run_id), "placebo", runs, seed,
                            {"observed_mtm_pnl": observed, "placebo": _spread(scores), "n_trades": copy.n_trades, "mean_hold_bars": copy.mean_hold, "side": copy.side},
                            (), (1 + better) / (runs + 1))


def monte_carlo(run_id: int, *, n: int, method: Literal["bootstrap", "permutation", "placebo"], seed: int, services: Any = None) -> MonteCarloResult:
    """Monte Carlo over the saved run `run_id` (spec 4.2): `simulate` on the database and the curve folder of the service. For the placebo the bars
    are loaded again from the spec that the run was saved with, and they must be the ones the run read (the hash of its data): other bars would
    make a control of another run. The result is saved as a row of `backtest_runs` (kind montecarlo, child of the run) that `evidence` reads."""
    from hokma.backtest import runner

    services = services or runner.Services()
    db = services.database()
    with db.read() as u:
        row = u.session.one("SELECT kind, status, spec_json, data_hash FROM backtest_runs WHERE id = :i", {"i": int(run_id)})
    if row is None or row["kind"] != "run" or row["status"] not in ("done", "account_blown"):
        raise PlaceboError(f"bad_run: {run_id} is not a finished run")
    spec = runner.spec_from_canonical(json.loads(row["spec_json"]))
    inputs = None
    if method == "placebo":
        data = services.load(spec)
        if data.data_hash != row["data_hash"]:
            raise PlaceboError(f"bad_run: the bars of run {run_id} are not the ones it read (the hash of the data changed): its placebo cannot be built")
        sheet = data.sheet
        inputs = PlaceboInputs(data.bars, sweep.sweep_costs(spec.costs, tick_size=sheet.tick_size, multiplier=sheet.multiplier, price=data.bars[0].open,
                                                           asset_type=sheet.asset_type))
    result = simulate(run_id, n=n, method=method, seed=seed, db=db, curve_dir=services.curves(), placebo=inputs)
    runner.save_row(db, "montecarlo", replace(spec, seed=seed), result.to_dict(), parent_id=int(run_id), data_hash=row["data_hash"])
    return result

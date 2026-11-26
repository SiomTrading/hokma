"""Helpers of the search tests (SPEC-020, B5b): a long deterministic series built from the REAL NQ bars of the fixture, and the same
moving average cross written once as a signal vector (for the sweep) and once as a decision function (for the engine)."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import numpy as np
import pandas as pd

from backtest import support
from hokma.backtest import engine
from hokma.backtest.fills import Decision
from hokma.backtest.sweep import Arrays, SweepCosts, arrays, sweep_costs
from hokma.backtest.ticks import Bar

def hide_trials(monkeypatch) -> None:
    """Make `from hokma.backtest import trials` fail, as if the ledger module were not built."""
    import sys

    import hokma.backtest
    monkeypatch.delattr(hokma.backtest, "trials", raising=False)
    monkeypatch.setitem(sys.modules, "hokma.backtest.trials", None)


class FakeLedger:
    """The shape of hokma.backtest.trials (spec 4.6): record and count, kept in memory."""

    def __init__(self):
        self.rows = []
        self.errors = []

    def record(self, strategy_id, hypothesis_id, run_id, params, status, objective, *, error=None):
        self.rows.append((strategy_id, hypothesis_id, run_id, dict(params), status, objective))
        self.errors.append(error)
        return len(self.rows)

    def count(self, strategy_id, *, hypothesis_id=None):
        return sum(1 for r in self.rows if r[0] == strategy_id and (hypothesis_id is None or r[1] == hypothesis_id))


START = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)
FIRST_OPEN = D("25640.00")


def _real_bars() -> list[Bar]:
    return [bar for name in support.SCENARIOS for bar in support.bars_of(name)]


def chained_bars(n: int, seed: int = 1) -> list[Bar]:
    """`n` one minute bars on shifted dates. Each bar takes the shape of a real fixture bar (body, high and low from its open, volume) and
    the gap from the previous close is a real gap between two consecutive minutes of the fixture; both are drawn with a seeded generator
    and chained from one price, so the series is long, deterministic and made only of real moves. It is not a real series."""
    real = _real_bars()
    gaps = [real[i].open - real[i - 1].close for i in range(1, len(real)) if real[i].ts - real[i - 1].ts == timedelta(minutes=1)]
    if not gaps:
        raise AssertionError("the fixture has no pair of consecutive minutes to take a gap from")
    rng = np.random.default_rng(seed)
    shape, gap = rng.integers(0, len(real), size=n), rng.integers(0, len(gaps), size=n)
    out: list[Bar] = []
    close = FIRST_OPEN
    for k in range(n):
        src = real[int(shape[k])]
        open_ = FIRST_OPEN if k == 0 else close + gaps[int(gap[k])]
        out.append(Bar(START + timedelta(minutes=k), open_, open_ + (src.high - src.open), open_ + (src.low - src.open), open_ + (src.close - src.open), src.volume))
        close = out[-1].close
    return out


def cross_signal(px: Arrays, fast: int, slow: int) -> np.ndarray:
    """Long (1) while the fast average of the closes is above the slow one, flat (0) otherwise and before the slow average exists."""
    close = pd.Series(px.close)
    f, s = close.rolling(fast).mean().to_numpy(), close.rolling(slow).mean().to_numpy()
    return np.where(np.nan_to_num(f - s, nan=-1.0) > 0, 1, 0)


def builder(px: Arrays, params: Mapping[str, object]) -> np.ndarray:
    return cross_signal(px, int(params["fast"]), int(params["slow"]))


def cross_decide(signal: np.ndarray):
    """The same rule as a decision function of the engine: enter long when the signal is on and flat, exit when it is off and long."""
    def decide(i, bar, side):
        if signal[i] == 1 and side is None:
            return [Decision("enter", "long")]
        if signal[i] == 0 and side == "long":
            return [Decision("exit")]
        return []
    return decide


def engine_run(bars: Sequence[Bar], fast: int, slow: int, **over):
    signal = cross_signal(arrays_of(bars), fast, slow)
    return engine.run(support.spec(**over), list(bars), cross_decide(signal), tick_size=support.TICK, multiplier=support.MULT, symbol="NQZ5",
                      protective_orders=False)


def arrays_of(bars: Sequence[Bar]) -> Arrays:
    return arrays(bars)


def returns_fixture() -> pd.Series:
    """The daily returns of the real fixture of the quant tests, with their dates."""
    from pathlib import Path
    frame = pd.read_csv(Path(__file__).resolve().parents[1] / "quant" / "fixtures" / "returns_daily.csv", index_col=0, parse_dates=True)
    return frame["return"]


def saved_run(db, curve_dir, bars: Sequence[Bar], *, fast: int = 10, slow: int = 60, days: int = 150, mtm: D | None = None) -> int:
    """A finished run in the database: the trades are the ones of the engine run of the cross on `bars`, the equity curve is the real daily
    returns of the fixture compounded from 1,000,000, so a Monte Carlo over it has the history of a year of days."""
    from hokma.backtest import results
    from hokma.backtest.results import BacktestResult, EquityCurve
    out = engine_run(bars, fast, slow)
    rets = returns_fixture().iloc[:days]
    equity = 1_000_000 * (1 + rets).cumprod()
    curve = EquityCurve(tuple(ts.to_pydatetime().replace(tzinfo=timezone.utc) for ts in equity.index), tuple(D(repr(round(float(v), 6))) for v in equity))
    with db.write() as u:
        run_id = u.session.insert("backtest_runs", {"kind": "run", "spec_json": "{}", "spec_hash": "spec-hash-1", "engine_version": "engine-1.0", "seed": 7,
                                                    "status": "running"})
    result = BacktestResult(run_id, "done", "spec-hash-1", "data-hash-1", "engine-1.0", 7, out.trades, curve, out.realized_pnl_usd,
                            out.mtm_pnl_usd if mtm is None else mtm, dict(out.costs_usd), out.ambiguous_bars, {}, {}, ())
    results.record_result(db, result, curve_dir=curve_dir)
    return run_id


def costs_of(bars: Sequence[Bar]) -> SweepCosts:
    return sweep_costs(support.spec().costs, tick_size=support.TICK, multiplier=support.MULT, price=bars[0].open)

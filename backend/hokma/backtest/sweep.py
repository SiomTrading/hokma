"""Fast sweep of a signal strategy (SPEC-020, section 4.5: V-01 and B-32). Own numpy, no backtest library.

A signal strategy gives, for each bar, the position it wants after the close: -1, 0 or +1 contracts. The sweep fills it the way the
engine does under the Hokma rules (the order decided at the close of bar N fills at the OPEN of bar N+1 with the ticks against, the
commission on each side) and ranks thousands of combinations in one pass. It orders them and nothing else: only the best K go to the
engine (optimize.py) and the official result is always the engine's. Money here is float because it is a ranking, not a statement."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pandas as pd

from hokma.backtest import costs as backtest_costs
from hokma.backtest.spec import CostModel, SpecError
from hokma.backtest.ticks import Bar
from hokma.quant import limits

DEFAULT_TOP_K = 20
CHUNK = 256                                  # combinations built and simulated at a time: bounds the memory of a large grid
METRICS = ("mtm_pnl", "net_pnl")             # mtm_pnl marks the open position at the last close; net_pnl counts closed trades only


@dataclass(frozen=True)
class Arrays:
    """The prices of the bars as float arrays: what a signal builder reads."""
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray

    def __len__(self) -> int:
        return len(self.open)


@dataclass(frozen=True)
class SweepCosts:
    """What one contract pays on one side: the ticks against and the commission."""
    tick_size: Decimal
    multiplier: Decimal
    slippage_ticks: int
    commission_per_side: Decimal

    @property
    def slippage_usd(self) -> float:
        return float(self.slippage_ticks * self.tick_size * self.multiplier)


def sweep_costs(model: CostModel, *, tick_size: Decimal, multiplier: Decimal, price: Decimal, asset_type: str = "future") -> SweepCosts:
    """The costs of one contract from the same fee table and slippage ticks the engine run uses (one source: hokma.backtest.costs)."""
    fee = backtest_costs.fee_model(model, asset_type)
    commission = backtest_costs.commission_usd(fee, qty=Decimal(1), price=price, multiplier=multiplier)
    return SweepCosts(tick_size, multiplier, model.slippage_ticks, commission)


def arrays(bars: Sequence[Bar]) -> Arrays:
    if not bars:
        raise SpecError("bad_spec: no bars to sweep")
    limits.check_param("n_observations", len(bars))
    columns = ([float(getattr(b, name)) for b in bars] for name in ("open", "high", "low", "close"))
    return Arrays(*(np.asarray(c, dtype=np.float64) for c in columns))


@dataclass(frozen=True, eq=False)
class SweepResult:
    """The sweep of N combinations, all net of costs. `mtm` marks the open position at the last close, like `mtm_pnl_usd` of the engine, and is
    the figure that ranks like the engine; `realized` counts closed trades only and leaves out a position that is still open."""
    realized: np.ndarray                     # (N,) USD
    mtm: np.ndarray                          # (N,) USD, the open position marked at the last close (as the engine does)
    trades: np.ndarray                       # (N,) closed trades
    pnl: np.ndarray | None = None            # (bars, N) USD per bar, marked to market and net of costs (only with per_bar)

    def scores(self, metric: str = "mtm_pnl") -> np.ndarray:
        if metric not in METRICS:
            raise SpecError(f"bad_spec: the sweep ranks by one of {', '.join(METRICS)}, not {metric!r}")
        return self.mtm if metric == "mtm_pnl" else self.realized

    def order(self, metric: str = "mtm_pnl") -> np.ndarray:
        """Indices of the combinations, best first; a tie keeps the order of the grid, so the ranking is reproducible."""
        return np.argsort(-self.scores(metric), kind="stable")

    def top(self, k: int = DEFAULT_TOP_K, metric: str = "mtm_pnl") -> np.ndarray:
        limits.check_param("top_k", k)
        if k < 1:
            raise SpecError("bad_spec: top_k is at least 1")
        return self.order(metric)[:k]


def sweep_signals(prices: Arrays | Sequence[Bar], signals: np.ndarray, costs: SweepCosts, *, per_bar: bool = False) -> SweepResult:
    """Simulate every row of `signals` (combinations x bars, values -1, 0 or +1: the position wanted after the close of the bar).

    The position of a combination is held from the open of the next bar, filled one tick (or more) against; the last signal has no next
    bar and is ignored, as in the engine. A flip from short to long is two fills of one contract, each with its costs."""
    px = prices if isinstance(prices, Arrays) else arrays(prices)
    sig = np.asarray(signals)
    if sig.ndim != 2 or sig.shape[1] != len(px):
        raise SpecError(f"bad_spec: the signals are a matrix of combinations by bars ({len(px)} bars), got shape {sig.shape}")
    n, bars = sig.shape
    limits.check_param("n_combinations", n)
    limits.check_param("n_observations", bars)
    limits.check_product("sweep cells", n, bars)
    if n == 0:
        raise SpecError("bad_spec: no combination to sweep")
    if not np.isin(sig, (-1, 0, 1)).all():
        raise SpecError("bad_signal: a signal is -1, 0 or 1")
    mult, slip, fee = float(costs.multiplier), costs.slippage_usd, float(costs.commission_per_side)
    side_cost = slip + fee
    pos = np.zeros((n, bars))
    pos[:, 1:] = sig[:, :-1]
    prev = np.zeros_like(pos)
    prev[:, 1:] = pos[:, :-1]
    move = pos - prev                                          # contracts bought (+) or sold (-) at the open of each bar
    cash = -move * (px.open * mult) - np.abs(move) * side_cost
    equity = np.cumsum(cash, axis=1) + pos * (px.close * mult)
    mtm_all = equity[:, -1]
    held = pos[:, -1]
    changed = move != 0
    last_move = bars - 1 - np.argmax(changed[:, ::-1], axis=1)             # bar of the last fill (only read where a position is open)
    entry = px.open[last_move] + np.sign(held) * costs.slippage_ticks * float(costs.tick_size)
    unrealized = held * (px.close[-1] - entry) * mult
    mtm = mtm_all + np.abs(held) * fee                         # the engine does not charge the commission of a lot that is still open
    realized = mtm - np.where(held != 0, unrealized, 0.0)
    closes = (prev != 0) & (pos != prev)
    pnl = None
    if per_bar:
        pnl = np.diff(equity, axis=1, prepend=0.0).T
    return SweepResult(realized, mtm, closes.sum(axis=1), pnl)


Builder = Callable[[Arrays, Mapping[str, object]], np.ndarray]


def signal_sweep(bars: Sequence[Bar], builder: Builder, grid: Sequence[Mapping[str, object]], costs: SweepCosts, *, per_bar: bool = False) -> SweepResult:
    """The sweep of a signal strategy over a list of parameter combinations. `builder(arrays, params)` returns the signal vector of
    one combination; the strategy owns its indicators, the sweep only fills and counts."""
    px = arrays(bars)
    points = list(grid)
    limits.check_param("n_combinations", len(points))
    if not points:
        raise SpecError("bad_spec: no combination to sweep")
    parts = [sweep_signals(px, np.vstack([np.asarray(builder(px, p)) for p in points[i:i + CHUNK]]), costs, per_bar=per_bar)
             for i in range(0, len(points), CHUNK)]
    return SweepResult(np.concatenate([p.realized for p in parts]), np.concatenate([p.mtm for p in parts]), np.concatenate([p.trades for p in parts]),
                       np.concatenate([p.pnl for p in parts], axis=1) if per_bar else None)


def rank_correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """Spearman correlation of two scorings of the same combinations (ties get the average rank). It is the check that the sweep orders
    like the engine. A scoring with no spread has no ranking: that is a SpecError, never a number."""
    x, y = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if x.shape != y.shape or x.ndim != 1 or len(x) < 2:
        raise SpecError("bad_spec: two scorings of the same combinations, at least two")
    rx, ry = pd.Series(x).rank().to_numpy(), pd.Series(y).rank().to_numpy()
    if rx.std() == 0 or ry.std() == 0:
        raise SpecError("bad_spec: a scoring with all values equal has no ranking")
    return float(np.corrcoef(rx, ry)[0, 1])


def top_overlap(a: Sequence[float], b: Sequence[float], k: int) -> int:
    """How many of the best `k` combinations of scoring `a` are also in the best `k` of scoring `b` (ties keep the grid order)."""
    first = np.argsort(-np.asarray(a, dtype=float), kind="stable")[:k]
    second = np.argsort(-np.asarray(b, dtype=float), kind="stable")[:k]
    return len(set(first.tolist()) & set(second.tolist()))

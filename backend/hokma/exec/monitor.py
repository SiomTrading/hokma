"""Live monitoring of a desk against its backtest (SPEC-018: ECT-026): the gap between live and backtest, drift detection (KS, CUSUM, rolling
Sharpe against the expected one minus two standard errors) and the levels of the emergency switch. Pure functions over a list of returns.

CUSUM here accumulates DETERIORATION: a return under the expected one adds to the sum and a return over it does not. The sign matters: a
version that accumulates the other way rings when the desk does better than expected and stays quiet when it does worse."""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

LEVELS = ("ok", "warn", "reduce", "halt")


def cusum_deterioration(returns: Sequence[float], *, expected: float, slack: float, threshold: float) -> tuple[bool, list[float]]:
    """(alarm, the running sum). S[t] = max(0, S[t-1] + (expected - r[t]) - slack); alarm when S passes the threshold."""
    total, path = 0.0, []
    for r in returns:
        total = max(0.0, total + (expected - r) - slack)
        path.append(total)
    return any(x > threshold for x in path), path


def rolling_sharpe(returns: Sequence[float], window: int) -> list[float]:
    out = []
    for i in range(window, len(returns) + 1):
        part = returns[i - window:i]
        mean = sum(part) / window
        sd = math.sqrt(sum((x - mean) ** 2 for x in part) / (window - 1)) if window > 1 else 0.0
        out.append(mean / sd if sd else 0.0)
    return out


def sharpe_warning(returns: Sequence[float], *, expected_sharpe: float, window: int) -> bool:
    """True when the latest rolling Sharpe is under the expected one minus two standard errors (se = sqrt((1 + sr^2 / 2) / window))."""
    series = rolling_sharpe(returns, window)
    if not series:
        return False
    se = math.sqrt((1 + expected_sharpe ** 2 / 2) / window)
    return series[-1] < expected_sharpe - 2 * se


def ks_statistic(a: Sequence[float], b: Sequence[float]) -> float:
    """Two sample Kolmogorov-Smirnov distance: the largest gap between the two empirical distributions."""
    xs, ys = sorted(a), sorted(b)
    values = sorted(set(xs) | set(ys))
    best = 0.0
    for v in values:
        fa = sum(1 for x in xs if x <= v) / len(xs)
        fb = sum(1 for y in ys if y <= v) / len(ys)
        best = max(best, abs(fa - fb))
    return best


def live_vs_backtest_gap(live_mean: float, backtest_mean: float) -> float:
    """The live average return minus the backtest one; negative means the desk does worse than its backtest."""
    return live_mean - backtest_mean


@dataclass(frozen=True)
class Level:
    name: str
    reason: str
    measured: bool = False          # at least one armed threshold was compared against a real (not None) value


def kill_level(*, drawdown_pct: float | None, day_loss_usd: float | None, thresholds: dict[str, float], drawdown_usd: float | None = None) -> Level:
    """The level of the emergency switch from the drawdown and the day loss: warn, reduce (no new openings), halt (the desk is paused). A fact
    that is None never raises a level and never lowers one. Thresholds: warn_drawdown_pct, reduce_drawdown_pct, halt_drawdown_pct, halt_day_loss_usd."""
    if day_loss_usd is not None and "halt_day_loss_usd" in thresholds and day_loss_usd >= thresholds["halt_day_loss_usd"]:
        return Level("halt", f"the loss of the day is {day_loss_usd:,.2f}")
    if drawdown_usd is not None and "halt_drawdown_usd" in thresholds and drawdown_usd >= thresholds["halt_drawdown_usd"]:
        return Level("halt", f"the drawdown is {drawdown_usd:,.2f} USD")
    if drawdown_pct is not None:
        for name in ("halt", "reduce", "warn"):
            limit = thresholds.get(f"{name}_drawdown_pct")
            if limit is not None and drawdown_pct >= limit:
                return Level(name, f"the drawdown is {drawdown_pct:.2f}%")
    return Level("ok", "")

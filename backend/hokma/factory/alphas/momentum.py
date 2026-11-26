"""Momentum alpha and its pieces (SPEC-020: ECT-054).

  * time series momentum: the sign of the return of an asset over `lookback` bars that ends `skip` bars before the last close (the most recent
    month is skipped because short horizons reverse), weighted by target volatility over realized volatility (the weight of the TSMOM alpha);
  * cross-sectional momentum: assets ranked on the same measure, the top share long and the bottom share short;
  * dual momentum: the best asset by return is held only when that return beats the risk-free return, otherwise the refuge is held;
  * crash control: the view is switched off when the realized volatility is above a percentile of its own history (90 by default), or when the
    momentum factor lost money over the last 12 months.

Costs of turnover are the functions of the IC study (one definition): `annual_cost` and `breakeven_cost_bps`.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

import numpy as np

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.factory.alphas._base import fact, view
from hokma.factory.alphas.tsmom import tsmom_weight
from hokma.factory.params import Param, ParamSchema
from hokma.factory.studies.ic_decay import annual_cost, breakeven_cost_bps    # noqa: F401  (re-exported: one definition of the cost of turnover)
from hokma.quant.errors import BadInput


def momentum_return(close: np.ndarray, lookback: int, skip: int) -> float:
    """close[-1 - skip] / close[-1 - skip - lookback] - 1: the return over `lookback` bars that ends `skip` bars before the last close."""
    if lookback < 1 or skip < 0 or len(close) < lookback + skip + 1:
        raise BadInput(f"momentum needs {lookback + skip + 1} closes, got {len(close)}")
    start, end = float(close[-1 - skip - lookback]), float(close[-1 - skip])
    if start <= 0 or end <= 0:
        raise BadInput("momentum needs positive closes")
    return end / start - 1.0


def cross_sectional_weights(scores: Mapping[str, float], top_fraction: float, *, long_short: bool = True) -> dict[str, float]:
    """The top `top_fraction` of the assets share +1 equally and, with `long_short`, the bottom share share -1. Ties break by name."""
    if not isinstance(top_fraction, (int, float)) or isinstance(top_fraction, bool) or not 0.0 < top_fraction <= 0.5:
        raise BadInput("top_fraction is a number above 0 and at most 0.5")
    ranked = sorted(((n, float(s)) for n, s in scores.items() if math.isfinite(float(s))), key=lambda kv: (-kv[1], kv[0]))
    if len(ranked) < 2:
        raise BadInput("cross-sectional momentum needs at least two assets with a score")
    k = max(1, int(len(ranked) * top_fraction))
    weights = {name: 0.0 for name, _ in ranked}
    for name, _ in ranked[:k]:
        weights[name] = 1.0 / k
    if long_short:
        for name, _ in ranked[-k:]:
            weights[name] = -1.0 / k
    return weights


def dual_momentum_pick(returns: Mapping[str, float], risk_free_return: float, refuge: str) -> str:
    """The asset with the best return when that return is above the risk-free return; otherwise the refuge (bonds or cash)."""
    usable = {n: float(r) for n, r in returns.items() if math.isfinite(float(r))}
    if not usable:
        raise BadInput("dual momentum needs at least one return")
    best, value = min(usable.items(), key=lambda kv: (-kv[1], kv[0]))
    return best if value > float(risk_free_return) else refuge


def vol_scale(target_vol: float, realized_vol: float, max_scale: float) -> float:
    """target / realized, never above `max_scale` (1 for a book with no leverage). Higher volatility gives a smaller size."""
    if not (target_vol > 0 and realized_vol > 0 and max_scale > 0):
        raise BadInput("target_vol, realized_vol and max_scale must be positive")
    return min(float(target_vol) / float(realized_vol), float(max_scale))


def momentum_off(realized_vol: float, vol_history: np.ndarray, percentile: float, factor_return_12m: float | None = None) -> bool:
    """True when momentum should be switched off: the volatility is above the given percentile (linear) of its history, or the factor lost money."""
    if not 0.0 < percentile < 100.0:
        raise BadInput("percentile is between 0 and 100")
    history = np.asarray(vol_history, dtype=float)
    history = history[np.isfinite(history)]
    if history.size == 0:
        raise BadInput("the volatility history is empty")
    return bool(realized_vol > np.percentile(history, percentile)) or (factor_return_12m is not None and factor_return_12m < 0.0)


def _annual_vol(returns: np.ndarray, periods_per_year: int) -> float | None:
    from hokma.quant.metrics.volatility import volatility   # imported here: the metrics module loads its library on import

    value = volatility(returns, periods_per_year=periods_per_year).value
    return float(value) if value is not None and math.isfinite(float(value)) else None


@dataclass(frozen=True)
class Momentum:
    """Reads the fact close, plus risk_free when `absolute_filter` and factor_return_12m when `factor_gate` are on."""

    periods: int = 252
    skip: int = 21
    vol_periods: int = 60
    target_vol: float = 0.10
    periods_per_year: int = 252
    vol_off_percentile: float = 90.0
    vol_history: int = 756
    absolute_filter: bool = False
    factor_gate: bool = False
    long_only: bool = False

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((
        Param("periods", "int", 252, 2, 5000), Param("skip", "int", 21, 0, 1000), Param("vol_periods", "int", 60, 2, 5000),
        Param("target_vol", "float", 0.10, 0.001, 2.0), Param("periods_per_year", "int", 252, 1, 100000),
        Param("vol_off_percentile", "float", 90.0, 0.0, 99.0), Param("vol_history", "int", 756, 4, 5000),
        Param("absolute_filter", "bool", False), Param("factor_gate", "bool", False), Param("long_only", "bool", False)))

    def __post_init__(self) -> None:
        if self.skip >= self.periods:
            raise ValueError("skip must be shorter than periods")
        if self.vol_off_percentile > 0 and self.vol_history < 2 * self.vol_periods:
            raise ValueError("vol_history must hold at least two blocks of vol_periods")

    @property
    def FACTS(self) -> tuple[str, ...]:
        return ("close", *(("risk_free",) if self.absolute_filter else ()), *(("factor_return_12m",) if self.factor_gate else ()))

    @property
    def lookback(self) -> int:
        """Bars the alpha needs: the formation window with its skipped bars, the volatility window and the volatility history."""
        need = self.periods + self.skip + 1
        return max(need, self.vol_periods + 1, (self.vol_history + 1) if self.vol_off_percentile > 0 else 0)

    def signal(self, ctx: SignalContext) -> Signal:
        close = fact(ctx, "close", self.periods + self.skip + 1)
        if close is None or len(close) < self.lookback or np.any(close[-self.lookback:] <= 0):
            return ctx.abstain("There are not enough positive closes for the momentum window and the volatility history. No view is given until there are.", ("close",))
        mom = momentum_return(close, self.periods, self.skip)
        recent = close[-self.vol_periods - 1:]
        sigma = _annual_vol(recent[1:] / recent[:-1] - 1.0, self.periods_per_year)
        if sigma is None or sigma <= 0:
            return ctx.abstain("The volatility of the window is not defined. No view is given.", ("volatility",))
        value = tsmom_weight(mom, sigma, self.target_vol)
        criteria = [Criterion("momentum_return", mom, 0.0, mom > 0, "data.close"), Criterion("sigma_annual", sigma, self.target_vol, None, "metrics.volatility")]
        notes = []
        if self.absolute_filter:
            rf = fact(ctx, "risk_free", 1)
            if rf is None:
                return ctx.abstain("The risk-free return is not available. No view is given until it is.", ("risk_free",))
            criteria.append(Criterion("excess_over_risk_free", mom - float(rf[-1]), 0.0, mom > float(rf[-1]), "data.risk_free"))
            if mom <= float(rf[-1]):
                value, notes = 0.0, ["The return does not beat the risk-free return, so the view is the refuge."]
        if self.long_only and value < 0:
            value = 0.0
        if self.factor_gate:
            factor = fact(ctx, "factor_return_12m", 1)
            if factor is None:
                return ctx.abstain("The factor return of the last 12 months is not available. No view is given until it is.", ("factor_return_12m",))
            criteria.append(Criterion("factor_return_12m", float(factor[-1]), 0.0, float(factor[-1]) >= 0, "data.factor"))
            if float(factor[-1]) < 0:
                value, notes = 0.0, ["The momentum factor lost money over 12 months, so the view is off."]
        if self.vol_off_percentile > 0 and value != 0.0:
            returns = close[-self.vol_history - 1:]
            returns = returns[1:] / returns[:-1] - 1.0
            blocks = [v for v in (_annual_vol(returns[i:i + self.vol_periods], self.periods_per_year)
                                  for i in range(len(returns) - self.vol_periods, -1, -self.vol_periods)) if v is not None]
            if len(blocks) < 2:
                return ctx.abstain("The volatility history is too short for its percentile. No view is given.", ("volatility",))
            off = momentum_off(sigma, np.array(blocks), self.vol_off_percentile)
            criteria.append(Criterion("vol_vs_history", sigma, float(np.percentile(blocks, self.vol_off_percentile)), not off, "metrics.volatility"))
            if off:
                value, notes = 0.0, [f"The volatility is above the {self.vol_off_percentile:g}th percentile of its history, so the view is off."]
        word = "positive" if mom > 0 else "negative" if mom < 0 else "flat"
        text = f"The return from {self.periods + self.skip} to {self.skip} bars ago is {word}. " + " ".join(notes or [f"The weight carries {self.target_vol:.0%} annual volatility."])
        return view(ctx, "alpha.momentum", value, criteria, text)

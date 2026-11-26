"""Selection pipeline and spread alpha for pairs (SPEC-020: ECT-055).

Pipeline (`select_pairs`): a correlation prefilter, the cointegration test with its own critical values, a correction for the number of
pairs tested, a half-life range, a Hurst bound, an economic reason and a ranking; a selected pair is tested again every month.

The correction counts every pair that was looked at, not only the ones that reached the test: with 4,850 random pairs about 5 percent
pass the test by luck, and the correction removes them. The cointegration test, the half-life, the Hurst exponent and the dynamic hedge
ratio come from the statistics module of H3 through the adapters below; the least squares hedge ratio is the one of the validation module.

The legs of a pair are sized by the hedge ratio: a hedge ratio of 0.9 means 0.9 dollars of B for each dollar of A, which is not equal dollars.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from typing import Any, ClassVar, Literal

import numpy as np

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.factory.alphas._base import fact, view
from hokma.factory.alphas.mean_reversion import reversion_positions
from hokma.factory.features import rolling_zscore
from hokma.factory.params import Param, ParamSchema
from hokma.factory.studies import _h3
from hokma.factory.studies.ic_decay import effective_breadth
from hokma.quant.errors import BadInput
from hokma.quant.limits import as_int

MAX_PAIRS = 50_000
Method = Literal["ols", "kalman"]


# ---- hedge ratio and legs ------------------------------------------------------------------------------------------

def ols_hedge_ratio(log_a: Any, log_b: Any) -> float:
    """The slope of log A on log B by least squares, from the regression of the validation module."""
    from hokma.quant.validation.stat_tests import ols      # imported here: the validation package loads its libraries on import

    y, x = np.asarray(log_a, dtype=float), np.asarray(log_b, dtype=float)
    if y.shape != x.shape or y.ndim != 1 or len(y) < 3 or not (np.all(np.isfinite(y)) and np.all(np.isfinite(x))):
        raise BadInput("the two log price series must be finite and of the same length, at least 3")
    if float(np.var(x)) == 0.0:
        raise BadInput("the partner did not move: the hedge ratio is not defined")
    return float(ols(y, x.reshape(-1, 1))["beta"][1])


def hedge_ratio(log_a: Any, log_b: Any, method: Method = "ols", kalman_fn: Callable[[Any, Any], Any] | None = None) -> float | np.ndarray:
    """A number for `ols`; one value per row (the causal filter) for `kalman`, from H3 or from the injected function."""
    if method == "ols":
        return ols_hedge_ratio(log_a, log_b)
    if method == "kalman":
        betas = np.asarray((kalman_fn or _h3.dynamic_hedge_ratio)(log_a, log_b), dtype=float)
        if betas.shape != np.asarray(log_a).shape:
            raise BadInput("the dynamic hedge ratio needs one value per row")
        return betas
    raise BadInput("method is ols or kalman")


def pair_legs(notional_a: float, hedge: float, mode: Literal["hedge_ratio", "dollar"] = "hedge_ratio") -> tuple[float, float]:
    """The dollars of each leg: A gets `notional_a`; B gets hedge times that (hedge_ratio) or the same amount (dollar)."""
    if not (notional_a > 0 and hedge > 0):
        raise BadInput("notional_a and the hedge ratio must be positive")
    if mode not in ("hedge_ratio", "dollar"):
        raise BadInput("mode is hedge_ratio or dollar")
    return float(notional_a), float(notional_a) * float(hedge) if mode == "hedge_ratio" else float(notional_a)


def diversified_sharpe(single_sharpe: float, n_pairs: int, avg_correlation: float) -> float:
    """Sharpe of a book of equal pairs: the single Sharpe times the square root of the effective number of independent pairs."""
    return float(single_sharpe) * math.sqrt(effective_breadth(n_pairs, avg_correlation))


# ---- the selection pipeline ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PairCandidate:
    a: str
    b: str
    correlation: float | None          # correlation of the two return series
    eg_p: float | None                 # p-value of the cointegration test; None when it was not run
    half_life_days: float | None
    hurst: float | None
    economic: bool | None              # a documented reason for the two to move together


@dataclass(frozen=True)
class PairRules:
    """Every limit is stated by the caller."""

    min_correlation: float
    alpha: float
    min_half_life_days: float
    max_half_life_days: float
    max_hurst: float
    top_n: int
    method: str                        # bonferroni, holm or bh
    require_economic: bool


@dataclass(frozen=True)
class SelectedPair:
    a: str
    b: str
    adjusted_p: float
    half_life_days: float


@dataclass(frozen=True)
class PairSelection:
    selected: tuple[SelectedPair, ...]
    rejected: Mapping[tuple[str, str], str]
    n_tested: int                      # every candidate counts, the ones dropped by the prefilter included
    n_pass_raw: int                    # how many passed the cointegration test at alpha with no correction


def select_pairs(candidates: Sequence[PairCandidate], rules: PairRules) -> PairSelection:
    """Run the pipeline. A pair the prefilter drops enters the correction with p = 1: it was looked at and did not pass."""
    from hokma.quant.validation.multiple_testing import correct      # imported here: the validation package loads its libraries on import

    if not candidates or len(candidates) > MAX_PAIRS:
        raise BadInput(f"candidates is a list of 1 to {MAX_PAIRS} pairs")
    if not (0 < rules.alpha < 1 and 0 < rules.min_half_life_days < rules.max_half_life_days and as_int("top_n", rules.top_n) >= 1
            and -1 <= rules.min_correlation <= 1 and 0 < rules.max_hurst <= 1):
        raise BadInput("the pair rules have a value outside its range")
    rejected: dict[tuple[str, str], str] = {}
    p = []
    for c in candidates:
        if c.correlation is None or c.correlation < rules.min_correlation:
            rejected[(c.a, c.b)] = "correlation"
            p.append(1.0)
        elif c.eg_p is None or not 0.0 <= c.eg_p <= 1.0:
            rejected[(c.a, c.b)] = "no_cointegration_test"
            p.append(1.0)
        else:
            p.append(float(c.eg_p))
    fixed = correct(p, rules.method, rules.alpha)
    survivors: list[tuple[float, float, str, str, PairCandidate]] = []
    for c, adjusted, passed in zip(candidates, fixed.adjusted, fixed.reject):
        key = (c.a, c.b)
        if key in rejected:
            continue
        if not passed:
            rejected[key] = "cointegration"
        elif c.half_life_days is None or not rules.min_half_life_days <= c.half_life_days <= rules.max_half_life_days:
            rejected[key] = "half_life"
        elif c.hurst is None or c.hurst >= rules.max_hurst:
            rejected[key] = "hurst"
        elif rules.require_economic and c.economic is not True:
            rejected[key] = "economic_reason"
        else:
            survivors.append((adjusted, c.half_life_days, c.a, c.b, c))
    survivors.sort(key=lambda s: s[:4])
    for _, _, a, b, _c in survivors[rules.top_n:]:
        rejected[(a, b)] = "rank"
    chosen = tuple(SelectedPair(a, b, adjusted, hl) for adjusted, hl, a, b, _c in survivors[:rules.top_n])
    raw = sum(1 for c in candidates if c.correlation is not None and c.correlation >= rules.min_correlation and c.eg_p is not None and c.eg_p <= rules.alpha)
    return PairSelection(chosen, dict(sorted(rejected.items())), len(candidates), raw)


def retest_due(selected_on: date, today: date, every_days: int) -> bool:
    """True when a selected pair is due for its new test: `every_days` (30 for a monthly test) or more have passed."""
    if as_int("every_days", every_days) < 1:
        raise BadInput("every_days is at least 1")
    return (today - selected_on).days >= every_days


def build_candidates(logs: Mapping[str, Any], *, economic: Callable[[str, str], bool | None] | None = None, bar_days: float = 1.0,
                     half_life_fn: Callable[[Any], Any] | None = None, eg_fn: Callable[[Any, Any], Any] | None = None,
                     hurst_fn: Callable[[Any], Any] | None = None) -> list[PairCandidate]:
    """The table of every pair of the given log price series: correlation of returns, cointegration p-value, half-life (days) and Hurst.

    The three statistics come from H3 unless a function is injected. `economic` says whether a documented reason links the two names.
    """
    from hokma.factory.alphas.mean_reversion import estimate_half_life

    names = sorted(logs)
    if len(list(combinations(names, 2))) > MAX_PAIRS:
        raise BadInput(f"more than {MAX_PAIRS} pairs")
    arrays = {n: np.asarray(logs[n], dtype=float) for n in names}
    eg = eg_fn or _h3.engle_granger_p
    hurst = hurst_fn or _h3.hurst_exponent
    out = []
    for a, b in combinations(names, 2):
        la, lb = arrays[a], arrays[b]
        if la.shape != lb.shape or len(la) < 10:
            raise BadInput(f"{a} and {b}: the series must have the same length, at least 10")
        corr = float(np.corrcoef(np.diff(la), np.diff(lb))[0, 1])
        spread = la - ols_hedge_ratio(la, lb) * lb
        hl = estimate_half_life(spread, half_life_fn)
        out.append(PairCandidate(a, b, corr if math.isfinite(corr) else None, _h3.scalar(eg(la, lb)), None if hl is None else hl * bar_days,
                                 _h3.scalar(hurst(spread)), None if economic is None else economic(a, b)))
    return out


# ---- the spread alpha ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PairsSpread:
    """View on the first symbol against its partner from the z-score of log(a) - beta * log(b).

    Reads the facts close and pair_close; half_life (bars) for the time exit; kalman_beta when `hedge` is kalman; retest_p (the p-value of
    the latest monthly cointegration test) and event (1 on a bar with a corporate event) when their switches are on. A long view is long A and
    short B in the hedge ratio. The view turns flat when the z-score goes past stop_z, when the relationship breaks (a failed re-test or a
    corporate event) or when the view has lasted `time_stop_half_lives` half-lives.
    """

    window: int = 60
    entry_z: float = 2.0
    exit_z: float = 0.5
    stop_z: float = 4.0
    time_stop_half_lives: float = 3.0
    hedge: str = "ols"
    use_retest: bool = False
    retest_alpha: float = 0.05
    use_events: bool = False
    history: int = 250

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((
        Param("window", "int", 60, 5, 5000), Param("entry_z", "float", 2.0, 0.1, 10.0), Param("exit_z", "float", 0.5, 0.0, 10.0),
        Param("stop_z", "float", 4.0, 0.2, 20.0), Param("time_stop_half_lives", "float", 3.0, 1.0, 10.0),
        Param("hedge", "choice", "ols", choices=("ols", "kalman")), Param("use_retest", "bool", False), Param("retest_alpha", "float", 0.05, 0.001, 0.5),
        Param("use_events", "bool", False), Param("history", "int", 250, 10, 5000)))

    def __post_init__(self) -> None:
        if not (0.0 <= self.exit_z < self.entry_z < self.stop_z):
            raise ValueError("the z levels need exit_z < entry_z < stop_z")
        if self.history < self.window + 2:
            raise ValueError("history must hold the window and two more bars")

    @property
    def FACTS(self) -> tuple[str, ...]:
        return ("close", "pair_close", "half_life", *(("kalman_beta",) if self.hedge == "kalman" else ()),
                *(("retest_p",) if self.use_retest else ()), *(("event",) if self.use_events else ()))

    @property
    def lookback(self) -> int:
        return self.history

    def signal(self, ctx: SignalContext) -> Signal:
        a, b, hl = fact(ctx, "close", self.window), fact(ctx, "pair_close", self.window), fact(ctx, "half_life", 1)
        if a is None or b is None or hl is None or np.any(a[-self.window:] <= 0) or np.any(b[-self.window:] <= 0):
            return ctx.abstain("Both price series need a full window of positive closes and a half-life. No view is given until they have them.", ("close", "pair_close", "half_life"))
        size = min(len(a), len(b), self.history)
        la, lb = np.log(a[-size:]), np.log(b[-size:])
        if self.hedge == "kalman":
            beta = fact(ctx, "kalman_beta", size)
            if beta is None:
                return ctx.abstain("The dynamic hedge ratio is not available. No view is given until it is.", ("kalman_beta",))
            spread = la - beta[-size:] * lb
        else:
            try:
                spread = la - ols_hedge_ratio(la[-self.window:], lb[-self.window:]) * lb
            except BadInput:
                return ctx.abstain("The partner did not move in the window. The hedge ratio cannot be estimated.", ("pair_close",))
        z = rolling_zscore(spread, self.window)
        max_hold = max(1, int(math.ceil(self.time_stop_half_lives * float(hl[-1])))) if math.isfinite(float(hl[-1])) and hl[-1] > 0 else size
        side = int(reversion_positions(z, self.entry_z, self.exit_z, self.stop_z, max_hold)[-1])
        last = float(z[-1]) if np.isfinite(z[-1]) else None
        criteria = [Criterion("spread_z", last, self.entry_z, None if last is None else abs(last) >= self.entry_z, "data.close")]
        broken = ""
        if self.use_retest:
            p = fact(ctx, "retest_p", 1)
            if p is None:
                return ctx.abstain("The latest cointegration re-test is not available. No view is given until it is.", ("retest_p",))
            criteria.append(Criterion("retest_p", float(p[-1]), self.retest_alpha, float(p[-1]) <= self.retest_alpha, "stats.cointegration"))
            if float(p[-1]) > self.retest_alpha:
                broken = "The monthly re-test no longer finds the relationship."
        if self.use_events:
            event = fact(ctx, "event", 1)
            if event is not None and float(event[-1]) != 0.0:
                broken = "A corporate event hit one of the two names."
        if broken:
            return view(ctx, "alpha.pairs_spread", 0.0, criteria, f"{broken} The relationship is treated as broken. The view is flat.")
        word = {1: "long the first name against its partner", -1: "short the first name against its partner", 0: "flat"}[side]
        return view(ctx, "alpha.pairs_spread", float(side), criteria,
                    f"The spread is measured with a hedge ratio from {self.hedge}. The view is {word}, and it is given up after {self.time_stop_half_lives:g} half-lives.")

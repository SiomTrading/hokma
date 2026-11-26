"""Facts of the value and fundamental alphas, from the fundamentals module of H3 (SPEC-020: AH-30, AH-31, AS-020i, AS-020k).

The alphas `value_screen` and `fundamental_rank` read yields and percentile ranks as facts. Here they are made from the statements of a name
with the functions of `hokma.quant.fundamentals` (the ratios and the F-score) and `hokma.quant.portfolio.cs_rank` (the ranks of a universe): no
ratio is computed a second time. Every figure is point in time: the statements of a date are the ones filed on or before it, which H3 enforces
with its `as_of`, and a statement filed later never moves a fact of an earlier bar.

A fact that H3 cannot compute (a missing line, a loss where the ratio needs a profit) is None and the alpha abstains: nothing is filled.
H3 has no free cash flow yield: it is the inverse of its `price_to_free_cash_flow_ratio` (a loss keeps its sign); the book to price is the
inverse of its `price_to_book_ratio`.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from hokma.factory.alphas.momentum import momentum_return
from hokma.quant import limits, registry
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult

VALUE_FACTS = ("earnings_yield", "fcf_yield", "book_to_price")
RANK_FACTS = ("value_rank", "quality_rank", "momentum_rank")
MAX_NAMES = 5_000


def _call(name: str, **kwargs: Any) -> QuantResult:
    import hokma.quant.fundamentals  # noqa: F401  (importing the packages registers their functions)
    import hokma.quant.portfolio  # noqa: F401

    limits.check_inputs(kwargs)
    return registry.describe(name).fn(**kwargs)           # type: ignore[misc]


def _latest(result: QuantResult, key: str) -> float | None:
    value = result.value["latest"].get(key) if result.ok and isinstance(result.value, dict) else None
    return None if value is None or not np.isfinite(float(value)) else float(value)


def _inverse(value: float | None) -> float | None:
    return None if value is None or value == 0.0 else 1.0 / value


def value_facts(statements: pd.DataFrame, as_of: Any, price: float | None = None) -> dict[str, float | None]:
    """The earnings yield, the free cash flow yield and the book to price of a name as of a date, from the valuation ratios of H3.

    `price` replaces the price of the latest statement that was filed by `as_of` (the price of the bar); without it the price column of the
    statements is used, which is the price at that statement."""
    frame = statements
    if price is not None:
        eligible = statements[pd.to_datetime(statements["filed"]) <= pd.Timestamp(as_of)] if "filed" in statements else statements
        if len(eligible):
            frame = statements.copy()
            frame.loc[eligible.index[-1], "price"] = float(price)
    got = _call("fundamentals.valuation", statements=frame, as_of=as_of,
                ratios=["earnings_yield", "price_to_free_cash_flow_ratio", "price_to_book_ratio"])
    return {"earnings_yield": _latest(got, "earnings_yield"), "fcf_yield": _inverse(_latest(got, "price_to_free_cash_flow_ratio")),
            "book_to_price": _inverse(_latest(got, "price_to_book_ratio"))}


def quality_score(statements: pd.DataFrame, as_of: Any) -> float | None:
    """The F-score of H3 (0 to 9) as of a date; None when the statements lack a line it needs."""
    return _latest(_call("fundamentals.piotroski_f", statements=statements, as_of=as_of), "piotroski_f")


def percentile_ranks(scores: Mapping[str, float | None]) -> dict[str, float | None]:
    """The percentile rank of each name among the names that have a score: 0 for the lowest, 1 for the highest (a higher score is better).

    The ranks are the cross sectional ranks of H3 (ties share the average rank). A name without a score has no rank, and a universe with fewer
    than two scored names has none: a rank among one is not a rank."""
    names = sorted(scores)
    if not names or len(names) > MAX_NAMES:
        raise BadInput(f"scores is a mapping of 1 to {MAX_NAMES} names")
    row = pd.DataFrame([[np.nan if scores[n] is None else float(scores[n]) for n in names]], columns=names)    # type: ignore[arg-type]
    got = _call("portfolio.cs_rank", a=row)
    ranked = got.value["ranks"][0] if got.ok else [None] * len(names)           # no name with a score: H3 has no cross section to rank
    valid = sum(1 for r in ranked if r is not None)
    return {n: None if r is None or valid < 2 else (r - 1.0) / (valid - 1.0) for n, r in zip(names, ranked)}


@dataclass(frozen=True)
class Name:
    """What a name brings to the ranking: its statements (one row per period with `filed`) and its closes by date."""

    statements: pd.DataFrame
    closes: pd.Series            # index: dates, oldest first


def rank_facts(universe: Mapping[str, Name], as_of: Any, *, periods: int = 252, skip: int = 21) -> dict[str, dict[str, float | None]]:
    """The three percentile ranks of every name of a universe as of a date: value (the mean of the ranks of its three yields, ranked again),
    quality (the F-score) and momentum (the return that skips the last `skip` closes). Only the closes on or before the date are read."""
    stamp = pd.Timestamp(as_of)
    yields = {n: value_facts(u.statements, as_of) for n, u in universe.items()}
    by_yield = {k: percentile_ranks({n: y[k] for n, y in yields.items()}) for k in VALUE_FACTS}
    mean_rank: dict[str, float | None] = {}
    for n in universe:
        parts = [r for r in (by_yield[k][n] for k in VALUE_FACTS) if r is not None]
        mean_rank[n] = float(np.mean(parts)) if parts else None
    quality = percentile_ranks({n: quality_score(u.statements, as_of) for n, u in universe.items()})
    momentum: dict[str, float | None] = {}
    for n, u in universe.items():
        closes = u.closes[pd.to_datetime(u.closes.index) <= stamp].to_numpy(dtype=float)
        try:
            momentum[n] = momentum_return(closes, periods, skip)
        except BadInput:
            momentum[n] = None
    value, mom = percentile_ranks(mean_rank), percentile_ranks(momentum)
    return {n: {"value_rank": value[n], "quality_rank": quality[n], "momentum_rank": mom[n]} for n in universe}


def step_feeds(dates: Sequence[Any], rebalance: Sequence[Any], compute: Callable[[date], Mapping[str, float | None]]) -> dict[str, np.ndarray]:
    """Facts as arrays aligned with `dates` (the bars of a run). `compute(day)` runs on each rebalance date only, and its figures hold from that
    date until the next one (they are what was known on the rebalance). Before the first rebalance, and where a fact is None, the value is NaN."""
    days = [pd.Timestamp(d).date() for d in dates]
    marks = sorted({pd.Timestamp(d).date() for d in rebalance})
    out: dict[str, np.ndarray] = {}
    current: Mapping[str, float | None] = {}
    position = 0
    for i, day in enumerate(days):
        while position < len(marks) and marks[position] <= day:
            current, position = compute(marks[position]), position + 1
        for name, value in current.items():
            out.setdefault(name, np.full(len(days), np.nan))[i] = np.nan if value is None else value
    return out

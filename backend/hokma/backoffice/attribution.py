"""Performance attribution (SPEC-018: ECT-078). Returns are fractions (0.08 is 8 percent), every number is Decimal.

* `brinson_fachler`: allocation, selection and interaction by sector. They add up exactly to the active return, which is the portfolio return
  minus the benchmark return. The weights of each side must add to one.
* `carino_link`: links the attribution of several periods. Effects of different periods are never added: each one is scaled by the Carino factor
  of its period so that the linked effects add up to the active return of the whole span. When the accumulated returns are equal the factor
  of the whole span is its limit (one over one plus the return), never a division by zero. The same limit applies to a period with equal returns.
* `factor_attribution`: active exposure (portfolio minus benchmark) times the factor return, plus a specific part. This explains
  R_p minus R_b, not R_p minus the risk free rate; the specific part is what is left of the active return of the period. Periods are linked.
* `fixed_income_attribution`: the income effect is the yield difference scaled by the time of the period (days over the basis).
This module explains the result of one portfolio against its own benchmark. It does not compare the lines of Hokma with each other, and it has no
options attribution.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, localcontext

from hokma.exec.money import ZERO, dec

ONE = Decimal(1)
WEIGHT_TOLERANCE = Decimal("1e-9")
EFFECT_TOLERANCE = Decimal("1e-12")
EQUAL_RETURNS = Decimal("1e-20")
MAX_SECTORS = 500
MAX_PERIODS = 1200
MAX_FACTORS = 200


class AttributionError(ValueError):
    """The inputs of an attribution do not hold together (weights that do not add to one, a return of minus one or less)."""


@dataclass(frozen=True)
class BrinsonRow:
    sector: str
    portfolio_weight: Decimal
    benchmark_weight: Decimal
    portfolio_return: Decimal
    benchmark_return: Decimal
    allocation: Decimal
    selection: Decimal
    interaction: Decimal

    @property
    def total(self) -> Decimal:
        return self.allocation + self.selection + self.interaction


@dataclass(frozen=True)
class BrinsonResult:
    rows: tuple[BrinsonRow, ...]
    portfolio_return: Decimal
    benchmark_return: Decimal
    allocation: Decimal
    selection: Decimal
    interaction: Decimal

    @property
    def active_return(self) -> Decimal:
        return self.portfolio_return - self.benchmark_return

    @property
    def effects(self) -> dict[str, Decimal]:
        return {"allocation": self.allocation, "selection": self.selection, "interaction": self.interaction}


def brinson_fachler(sectors: Mapping[str, Sequence[Decimal | int | str]]) -> BrinsonResult:
    """`sectors` maps a name to (portfolio weight, benchmark weight, portfolio return, benchmark return).
    allocation = (wp - wb) * (Rb_s - Rb); selection = wb * (Rp_s - Rb_s); interaction = (wp - wb) * (Rp_s - Rb_s)."""
    if not sectors or len(sectors) > MAX_SECTORS:
        raise AttributionError(f"give between 1 and {MAX_SECTORS} sectors")
    parsed: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    for name, values in sectors.items():
        if not isinstance(name, str) or not name.strip() or len(values) != 4:
            raise AttributionError("a sector is a name with four numbers: two weights and two returns")
        wp, wb, rp, rb = (dec(v, f"{name}") for v in values)
        if wp < 0 or wb < 0:
            raise AttributionError(f"{name}: a weight cannot be negative")
        parsed[name] = (wp, wb, rp, rb)
    for label, index in (("portfolio", 0), ("benchmark", 1)):
        if abs(sum((v[index] for v in parsed.values()), ZERO) - ONE) > WEIGHT_TOLERANCE:
            raise AttributionError(f"the {label} weights must add up to one")
    total_b = sum((v[1] * v[3] for v in parsed.values()), ZERO)
    total_p = sum((v[0] * v[2] for v in parsed.values()), ZERO)
    rows = []
    for name, (wp, wb, rp, rb) in parsed.items():
        rows.append(BrinsonRow(name, wp, wb, rp, rb, (wp - wb) * (rb - total_b), wb * (rp - rb), (wp - wb) * (rp - rb)))
    return BrinsonResult(tuple(rows), total_p, total_b, sum((r.allocation for r in rows), ZERO), sum((r.selection for r in rows), ZERO),
                         sum((r.interaction for r in rows), ZERO))


@dataclass(frozen=True)
class Period:
    """One period to link: the returns of the portfolio and the benchmark and the effects that explain their difference."""
    portfolio_return: Decimal
    benchmark_return: Decimal
    effects: Mapping[str, Decimal]


@dataclass(frozen=True)
class Linked:
    effects: dict[str, Decimal]
    portfolio_return: Decimal                 # accumulated (compounded)
    benchmark_return: Decimal
    factors: tuple[Decimal, ...] = field(default_factory=tuple)

    @property
    def active_return(self) -> Decimal:
        return self.portfolio_return - self.benchmark_return

    @property
    def residual(self) -> Decimal:
        """Linked effects minus the active return of the whole span: zero up to the precision of the logarithm."""
        return sum(self.effects.values(), ZERO) - self.active_return


def _log_ratio(rp: Decimal, rb: Decimal) -> Decimal:
    """(ln(1 + rp) - ln(1 + rb)) / (rp - rb); the limit 1 / (1 + rp) when the two returns are equal."""
    if rp <= -ONE or rb <= -ONE:
        raise AttributionError("a return of minus one or less cannot be linked")
    if abs(rp - rb) < EQUAL_RETURNS:
        return ONE / (ONE + rp)
    return ((ONE + rp).ln() - (ONE + rb).ln()) / (rp - rb)


def carino_link(periods: Sequence[Period]) -> Linked:
    """Link the attribution of consecutive periods. Each effect of period t is multiplied by k_t = c_t / c, where c_t is the log ratio of the
    period and c the log ratio of the accumulated returns. The sum of the linked effects is the accumulated active return."""
    if not periods or len(periods) > MAX_PERIODS:
        raise AttributionError(f"give between 1 and {MAX_PERIODS} periods")
    with localcontext() as ctx:
        ctx.prec = 50
        cum_p = cum_b = ONE
        for p in periods:
            rp, rb = dec(p.portfolio_return, "portfolio return"), dec(p.benchmark_return, "benchmark return")
            if rp <= -ONE or rb <= -ONE:
                raise AttributionError("a return of minus one or less cannot be linked")
            gap = sum((dec(v, "effect") for v in p.effects.values()), ZERO) - (rp - rb)
            if abs(gap) > EFFECT_TOLERANCE:
                raise AttributionError("the effects of a period must add up to its active return")
            cum_p, cum_b = cum_p * (ONE + rp), cum_b * (ONE + rb)
        total = _log_ratio(cum_p - ONE, cum_b - ONE)
        linked: dict[str, Decimal] = {}
        factors: list[Decimal] = []
        for p in periods:
            k = _log_ratio(dec(p.portfolio_return), dec(p.benchmark_return)) / total
            factors.append(k)
            for name, value in p.effects.items():
                linked[name] = linked.get(name, ZERO) + k * dec(value, "effect")
        return Linked(linked, cum_p - ONE, cum_b - ONE, tuple(factors))


@dataclass(frozen=True)
class FactorPeriod:
    portfolio_return: Decimal
    benchmark_return: Decimal
    portfolio_exposure: Mapping[str, Decimal]
    benchmark_exposure: Mapping[str, Decimal]
    factor_return: Mapping[str, Decimal]


def factor_attribution(periods: Sequence[FactorPeriod]) -> Linked:
    """Per period: contribution of a factor = (portfolio exposure - benchmark exposure) * factor return; specific = active return of the period
    minus the sum of the contributions. The periods are linked with Carino, never added."""
    if not periods or len(periods) > MAX_PERIODS:
        raise AttributionError(f"give between 1 and {MAX_PERIODS} periods")
    linked_input: list[Period] = []
    for p in periods:
        names = set(p.portfolio_exposure) | set(p.benchmark_exposure)
        if len(names) > MAX_FACTORS:
            raise AttributionError(f"more than {MAX_FACTORS} factors")
        missing = names - set(p.factor_return)
        if missing:
            raise AttributionError(f"no factor return for {sorted(missing)[0]}")
        effects = {n: (dec(p.portfolio_exposure.get(n, ZERO)) - dec(p.benchmark_exposure.get(n, ZERO))) * dec(p.factor_return[n]) for n in sorted(names)}
        effects["specific"] = (dec(p.portfolio_return) - dec(p.benchmark_return)) - sum(effects.values(), ZERO)
        linked_input.append(Period(dec(p.portfolio_return), dec(p.benchmark_return), effects))
    return carino_link(linked_input)


@dataclass(frozen=True)
class FixedIncomeSide:
    yield_: Decimal            # annual, as a fraction
    duration: Decimal
    spread_duration: Decimal
    total_return: Decimal      # of the period


def fixed_income_attribution(portfolio: FixedIncomeSide, benchmark: FixedIncomeSide, *, days: int, rate_change: Decimal, spread_change: Decimal,
                             basis_days: int = 365) -> dict[str, Decimal]:
    """income = (yield_p - yield_b) * days / basis (scaled by the time of the period); duration = -(D_p - D_b) * rate change; spread =
    -(SD_p - SD_b) * spread change; selection = what is left of the active return. The parts add up to the active return."""
    if days <= 0 or basis_days <= 0:
        raise AttributionError("days and basis must be positive")
    income = (dec(portfolio.yield_) - dec(benchmark.yield_)) * Decimal(days) / Decimal(basis_days)
    duration = -(dec(portfolio.duration) - dec(benchmark.duration)) * dec(rate_change)
    spread = -(dec(portfolio.spread_duration) - dec(benchmark.spread_duration)) * dec(spread_change)
    active = dec(portfolio.total_return) - dec(benchmark.total_return)
    return {"income": income, "duration": duration, "spread": spread, "selection": active - income - duration - spread, "active": active}


def brinson_table(result: BrinsonResult) -> list[list[str]]:
    """The attribution as rows of text for a report: every number comes from the result, none is typed."""
    from hokma.exec.money import text

    head = ["sector", "allocation", "selection", "interaction", "total"]
    body = [[r.sector, text(r.allocation), text(r.selection), text(r.interaction), text(r.total)] for r in result.rows]
    return [head, *body, ["total", text(result.allocation), text(result.selection), text(result.interaction), text(result.active_return)]]


__all__ = ["AttributionError", "BrinsonResult", "FactorPeriod", "FixedIncomeSide", "Linked", "Period", "brinson_fachler", "brinson_table", "carino_link",
           "factor_attribution", "fixed_income_attribution"]

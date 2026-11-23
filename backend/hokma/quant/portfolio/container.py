"""Portfolio containers (SK-106, SK-108, SK-109, SK-111, SK-112).

A portfolio is a weight vector over a returns panel. `Portfolio` gives every measure, the compounded NAV and the risk
contribution; `charts` returns the traces of the figures as plain data; `failed` is the controlled result of a solver
that did not converge (the reason and a labelled baseline, never invented weights); `Population` compares candidates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _common as c
from hokma.quant.portfolio import _lib
from hokma.quant.types import QuantResult

_RET = {"type": "dataframe", "required": True}
_W = {"type": "dict", "required": True}
MIN_OBS = 3
CHART_KINDS = ("cumulative_returns", "drawdowns", "composition", "returns", "returns_distribution")
FALLBACKS = ("inverse_volatility", "equal_weighted")
DEFAULT_MEASURES = ("mean", "annualized_mean", "standard_deviation", "annualized_standard_deviation", "variance", "cvar", "max_drawdown",
                    "sharpe_ratio", "annualized_sharpe_ratio", "sortino_ratio", "calmar_ratio", "effective_number_assets")
MAX_CANDIDATES = 200


def _measure_name(name: Any) -> str:
    return c.choice("measure", name, _lib.ALL_MEASURES + ("effective_number_assets",))


def _pf_value(pf: Any, name: str) -> float | None:
    if name == "effective_number_assets":
        return float(pf.effective_number_assets)
    return c.jsonable(getattr(pf, name))


@dataclass(frozen=True)
class Portfolio:
    """Weights over a returns panel, with P&L, NAV and every measure of the library (SK-106)."""

    returns: pd.DataFrame
    weights: dict[str, float]
    risk_free_rate: float = 0.0
    name: str | None = None

    @staticmethod
    def build(returns: Any, weights: Any, *, risk_free_rate: float = 0.0, name: str | None = None,
              min_obs: int = MIN_OBS) -> tuple["Portfolio | None", tuple[str, ...]]:
        """A Portfolio, or None with the reasons (too few rows, NaN). Unknown assets in the weights raise BadInput."""
        panel, warns = c.usable_panel(returns, min_obs=min_obs)
        if panel is None:
            return None, warns
        vec = c.weights_vector(weights, panel.columns)
        if not np.all(np.isfinite(vec)):
            return None, warns + ("weights must be finite",)
        return Portfolio(panel, c.weights_to_dict(vec, panel.columns), float(risk_free_rate), name), warns

    def sk(self) -> Any:
        vec = np.array([self.weights[str(col)] for col in self.returns.columns])
        return _lib.SkPortfolio(X=self.returns, weights=vec, risk_free_rate=self.risk_free_rate, name=self.name, compounded=True)

    def measure(self, name: str) -> float | None:
        return _pf_value(self.sk(), _measure_name(name))

    def measures(self, names: tuple[str, ...] = DEFAULT_MEASURES) -> dict[str, float | None]:
        pf = self.sk()
        return {n: _pf_value(pf, _measure_name(n)) for n in names}

    def pnl(self) -> pd.Series:
        """Return of each period: the weights times the asset returns."""
        vec = np.array([self.weights[str(col)] for col in self.returns.columns])
        return pd.Series(self.returns.to_numpy() @ vec, index=self.returns.index, name="pnl")

    def nav(self, start: float = 1.0) -> pd.Series:
        """Compounded value of `start` invested at the first period."""
        return (1.0 + self.pnl()).cumprod() * start

    def to_dict(self, names: tuple[str, ...] = DEFAULT_MEASURES) -> dict[str, Any]:
        nav = self.nav()
        return {"weights": dict(self.weights), "measures": self.measures(names), "nav_last": float(nav.iloc[-1]),
                "total_return": float(nav.iloc[-1] - 1.0), "n_observations": int(len(self.returns))}


def _opt_names(measures: Any) -> tuple[str, ...]:
    if measures is None:
        return DEFAULT_MEASURES
    if not isinstance(measures, (list, tuple)) or not measures or len(measures) > len(_lib.ALL_MEASURES) + 1:
        raise BadInput("measures must be a non-empty list of measure names")
    return tuple(_measure_name(m) for m in measures)


@registry.register("portfolio.portfolio_summary", module="portfolio",
                   summary="Portfolio container: weights over a returns panel with P&L, compounded NAV and the named measures.",
                   inputs={"returns": _RET, "weights": _W, "measures": {"type": "list", "required": False},
                           "risk_free_rate": {"type": "float", "required": False, "default": 0.0}},
                   piece_ids=("SK-106",))
def portfolio_summary(returns: Any, weights: Any, measures: Any = None, risk_free_rate: float = 0.0) -> QuantResult:
    name = "portfolio.portfolio_summary"
    pieces = ("SK-106",)
    params = {"measures": measures, "risk_free_rate": risk_free_rate}

    def compute() -> Any:
        names = _opt_names(measures)
        c.number("risk_free_rate", risk_free_rate)
        pf, warns = Portfolio.build(returns, weights, risk_free_rate=risk_free_rate)
        if pf is None:
            raise c.Domain("; ".join(warns))
        return pf.to_dict(names), warns

    return c.run(name, unit="mixed", method="Portfolio", params=params, piece_ids=pieces, compute=compute, inputs=(returns, weights))


def _plain(value: Any) -> Any:
    if isinstance(value, np.datetime64):
        return pd.Timestamp(value).isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return c.jsonable(value)


def _trace_data(trace: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"name": trace.name, "type": trace.type}
    for axis in ("x", "y"):
        data = getattr(trace, axis, None)
        out[axis] = None if data is None else [_plain(v) for v in list(data)]
    return out


@registry.register("portfolio.charts", module="portfolio",
                   summary="Traces of a portfolio chart as plain data: cumulative_returns, drawdowns, composition, returns or returns_distribution.",
                   inputs={"returns": _RET, "weights": _W, "kind": {"type": "str", "required": False, "default": "cumulative_returns"}},
                   piece_ids=("SK-108",))
def charts(returns: Any, weights: Any, kind: str = "cumulative_returns") -> QuantResult:
    name = "portfolio.charts"
    pieces = ("SK-108",)
    params = {"kind": kind}

    def compute() -> Any:
        c.choice("kind", kind, CHART_KINDS)
        pf, warns = Portfolio.build(returns, weights)
        if pf is None:
            raise c.Domain("; ".join(warns))
        fig = getattr(pf.sk(), f"plot_{kind}")()
        title = fig.layout.title.text if fig.layout.title is not None else None
        return {"kind": kind, "title": title, "traces": [_trace_data(t) for t in fig.data]}, warns

    return c.run(name, unit="chart", method="BasePortfolio.plot", params=params, piece_ids=pieces, compute=compute, inputs=(returns, weights))


@registry.register("portfolio.contribution", module="portfolio",
                   summary="Contribution of each asset to a risk measure (weight times the gradient of the measure) and its share of the total.",
                   inputs={"returns": _RET, "weights": _W, "measure": {"type": "str", "required": False, "default": "variance"}},
                   piece_ids=("SK-109",))
def contribution(returns: Any, weights: Any, measure: str = "variance") -> QuantResult:
    name = "portfolio.contribution"
    pieces = ("SK-109",)
    params = {"measure": measure}

    def compute() -> Any:
        c.choice("measure", measure, _lib.RISK_MEASURES)
        pf, warns = Portfolio.build(returns, weights)
        if pf is None:
            raise c.Domain("; ".join(warns))
        values = np.asarray(pf.sk().contribution(measure=_lib.RiskMeasure(measure)), dtype=float)
        total = float(values.sum())
        if total == 0.0:
            raise c.Domain("the total contribution is zero")
        cols = list(pf.returns.columns)
        return {"contribution": c.weights_to_dict(values, pd.Index(cols)), "share": c.weights_to_dict(values / total, pd.Index(cols)),
                "total": total}, warns

    return c.run(name, unit="risk", method="BasePortfolio.contribution", params=params, piece_ids=pieces, compute=compute, inputs=(returns, weights))


def failed_payload(returns: pd.DataFrame, reason: str, fallback: str = "inverse_volatility") -> dict[str, Any]:
    """The result of an optimization that did not converge: the reason, no weights, and a labelled baseline as respaldo."""
    c.choice("fallback", fallback, FALLBACKS + ("none",))
    marker = _lib.SkFailedPortfolio(X=returns, optimization_error=reason[:400])
    payload: dict[str, Any] = {"status": "failed", "reason": marker.optimization_error, "weights": None, "fallback": None}
    if fallback == "none":
        return payload
    if fallback == "equal_weighted":
        vec = np.full(returns.shape[1], 1.0 / returns.shape[1])
    else:
        vec = _lib.sk_stats.inverse_volatility_weights(np.cov(returns.to_numpy().T, bias=True).reshape(returns.shape[1], returns.shape[1]))
    payload["fallback"] = {"method": fallback, "weights": c.weights_to_dict(vec, returns.columns)}
    return payload


@registry.register("portfolio.failed", module="portfolio",
                   summary="Controlled result when a solver fails: the reason, weights None and a labelled baseline (inverse_volatility or equal_weighted).",
                   inputs={"returns": _RET, "reason": {"type": "str", "required": True},
                           "fallback": {"type": "str", "required": False, "default": "inverse_volatility"}},
                   piece_ids=("SK-111",))
def failed(returns: Any, reason: str, fallback: str = "inverse_volatility") -> QuantResult:
    name = "portfolio.failed"
    pieces = ("SK-111",)
    params = {"fallback": fallback}

    def compute() -> Any:
        if not isinstance(reason, str) or not reason.strip():
            raise c.Domain("reason must be a non-empty text")
        panel, warns = c.usable_panel(returns, min_obs=2)
        if panel is None:
            raise c.Domain("; ".join(warns))
        return failed_payload(panel, reason.strip(), fallback), warns + (f"optimization failed: {reason.strip()[:200]}",)

    return c.run(name, unit="weights", method="FailedPortfolio", params=params, piece_ids=pieces, compute=compute, inputs=(returns, reason))


@dataclass(frozen=True)
class Population:
    """Candidate portfolios over the same returns panel, compared by measure (SK-112)."""

    returns: pd.DataFrame
    candidates: Mapping[str, Portfolio] = field(default_factory=dict)

    @staticmethod
    def build(returns: Any, candidates: Any, *, min_obs: int = MIN_OBS) -> tuple["Population | None", tuple[str, ...]]:
        if not isinstance(candidates, Mapping) or not candidates:
            raise BadInput("candidates must be a non-empty dict name -> weights")
        if len(candidates) > MAX_CANDIDATES:
            raise BadInput(f"more than {MAX_CANDIDATES} candidates")
        panel, warns = c.usable_panel(returns, min_obs=min_obs)
        if panel is None:
            return None, warns
        built: dict[str, Portfolio] = {}
        for key, weights in candidates.items():
            pf, _ = Portfolio.build(panel, weights, name=str(key), min_obs=min_obs)
            if pf is None:
                return None, warns + (f"candidate {key!r} has non finite weights",)
            built[str(key)] = pf
        return Population(panel, built), warns

    def sk(self) -> Any:
        return _lib.SkPopulation([p.sk() for p in self.candidates.values()])

    def table(self, measures: tuple[str, ...]) -> dict[str, dict[str, float | None]]:
        return {name: pf.measures(measures) for name, pf in self.candidates.items()}

    def ranking(self, measure: str) -> list[str]:
        """Names from best to worst. Risk measures rank low to high; performance and ratios rank high to low."""
        name = _measure_name(measure)
        values = {n: pf.measure(name) for n, pf in self.candidates.items()}
        if any(v is None for v in values.values()):
            raise c.Domain(f"{name} is not finite for every candidate")
        higher_is_better = name in {m.value for m in _lib.sk_measures.PerfMeasure} | {m.value for m in _lib.sk_measures.RatioMeasure} | {"effective_number_assets"}
        return sorted(values, key=lambda n: values[n], reverse=higher_is_better)

    def dominance(self) -> list[dict[str, str]]:
        """Pairs (winner, loser) where the first portfolio dominates the second in every measure of the library."""
        sks = {n: p.sk() for n, p in self.candidates.items()}
        return [{"dominates": a, "dominated": b} for a in sks for b in sks if a != b and sks[a].dominates(sks[b])]


@registry.register("portfolio.population", module="portfolio",
                   summary="Compare candidate portfolios: measure table, ranking by one measure and the pairs where one dominates the other.",
                   inputs={"returns": _RET, "candidates": {"type": "dict", "required": True}, "measures": {"type": "list", "required": False},
                           "rank_by": {"type": "str", "required": False, "default": "sharpe_ratio"}},
                   piece_ids=("SK-112",))
def population(returns: Any, candidates: Any, measures: Any = None, rank_by: str = "sharpe_ratio") -> QuantResult:
    name = "portfolio.population"
    pieces = ("SK-112",)
    params = {"rank_by": rank_by}

    def compute() -> Any:
        names = _opt_names(measures)
        _measure_name(rank_by)
        pop, warns = Population.build(returns, candidates)
        if pop is None:
            raise c.Domain("; ".join(warns))
        return {"measures": pop.table(names), "ranking": pop.ranking(rank_by), "dominance": pop.dominance(), "rank_by": rank_by}, warns

    return c.run(name, unit="mixed", method="Population", params=params, piece_ids=pieces, compute=compute, inputs=(returns, candidates))

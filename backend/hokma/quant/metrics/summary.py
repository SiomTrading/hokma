"""Summary table of the metrics module: one function per block of the standard performance table.

Every block takes a return series (and a benchmark when the block needs one) and returns a QuantResult whose value is a
dict of named figures. Bad data never raises: the result has value None and the reason in warnings.

This file also holds the small helpers the other metrics files of this part share (series preparation and result
building), so that each calculation exists once.
"""
from __future__ import annotations

import math
import warnings as _warnings
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

import numpy as np
import pandas as pd

from hokma.quant import helpers, registry
from hokma.quant.errors import BadInput
from hokma.quant.metrics import _lib
from hokma.quant.types import QuantResult

_FALLBACK_START = "2000-01-03"


# --------------------------------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------------------------------
@contextmanager
def quiet() -> Iterator[None]:
    """Silence the library warnings: the module reports problems in QuantResult.warnings instead."""
    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore")
        with np.errstate(all="ignore"):
            yield


def with_date_index(series: pd.Series) -> tuple[pd.Series, tuple[str, ...]]:
    """The series on a DatetimeIndex. A series without dates gets business days from a fixed start, with a warning."""
    if isinstance(series.index, pd.DatetimeIndex):
        out = series.sort_index()
        if out.index.tz is not None:
            out = out.tz_convert("UTC").tz_localize(None)
        return out, ()
    out = series.copy()
    out.index = pd.bdate_range(_FALLBACK_START, periods=len(out))
    return out, ("input has no date index: business days from 2000-01-03 were assigned",)


def prepare_series(values: Any, *, name: str = "returns", min_obs: int = 2,
                   allow_constant: bool = False) -> tuple[pd.Series | None, tuple[str, ...]]:
    """Clean series on a date index, or None with the reasons. Wrong type or shape raises BadInput."""
    series, warns = helpers.validate_returns(values, min_obs=min_obs, name=name)
    blocking = [w for w in warns if w.startswith("need at least") or (w.startswith("constant") and not allow_constant)]
    if not blocking and not allow_constant and len(series) > 1 and float(np.ptp(series.to_numpy())) <= 1e-12:
        warns = tuple(warns) + ("constant series: dispersion is zero",)
        blocking = [warns[-1]]
    if blocking:
        return None, warns
    series, extra = with_date_index(series)
    return series, tuple(warns) + extra


def align_pair(returns: Any, benchmark: Any, *, min_obs: int = 3) -> tuple[pd.Series | None, pd.Series | None, tuple[str, ...]]:
    """Both series cleaned and restricted to the dates they share."""
    r, rw = prepare_series(returns, name="returns", min_obs=min_obs)
    if r is None:
        return None, None, rw
    b, bw = prepare_series(benchmark, name="benchmark", min_obs=min_obs)
    if b is None:
        return None, None, tuple(rw) + tuple("benchmark: " + w for w in bw)
    if isinstance(returns, (list, tuple, np.ndarray)) and isinstance(benchmark, (list, tuple, np.ndarray)):
        n = min(len(r), len(b))
        r, b = r.iloc[:n], b.iloc[:n]
        b.index = r.index
    joined = pd.concat([r.rename("r"), b.rename("b")], axis=1, join="inner").dropna()
    warns = tuple(rw) + tuple("benchmark: " + w for w in bw)
    if len(joined) < min_obs:
        return None, None, warns + (f"returns and benchmark share {len(joined)} date(s); need at least {min_obs}",)
    if len(joined) < min(len(r), len(b)):
        warns += (f"only {len(joined)} dates are shared by returns and benchmark",)
    if float(joined["b"].std(ddof=1)) == 0.0:
        return None, None, warns + ("constant benchmark: dispersion is zero",)
    return joined["r"], joined["b"], warns


def clean_value(value: Any) -> Any:
    """Make a value safe for a QuantResult: finite floats, ISO dates, None for NaN and infinity, recursive on containers."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): clean_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_value(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()[:10] if isinstance(value, date) and not isinstance(value, datetime) else pd.Timestamp(value).isoformat()[:10]
    if isinstance(value, (int, np.integer)):
        return int(value)
    return helpers.finite_or_none(value)


def has_data(value: Any) -> bool:
    if isinstance(value, dict):
        return any(has_data(v) for v in value.values())
    if isinstance(value, list):
        return any(has_data(v) for v in value)
    return value is not None


def finish(name: str, value: Any, *, unit: str, method: str, series: pd.Series | None, pieces: tuple[str, ...],
           params: dict | None = None, warnings: tuple[str, ...] = (), hash_extra: tuple = ()) -> QuantResult:
    """Build the QuantResult of a computed value. A value that is entirely undefined becomes an empty result."""
    cleaned = clean_value(value)
    warns = tuple(warnings)
    parts: list[Any] = [] if series is None else [series]
    inputs_hash = helpers.hash_inputs(*parts, dict(params or {}), *hash_extra)
    if not has_data(cleaned):
        return QuantResult(name=name, value=None, unit=unit, method=method, params=dict(params or {}), inputs_hash=inputs_hash,
                           as_of=helpers.last_as_of(series) if series is not None else None,
                           warnings=warns + ("value is not defined for this input",), piece_ids=pieces)
    return QuantResult(name=name, value=cleaned, unit=unit, method=method, params=dict(params or {}), inputs_hash=inputs_hash,
                       as_of=helpers.last_as_of(series) if series is not None else None, warnings=warns, piece_ids=pieces)


def empty(name: str, unit: str, method: str, warns: tuple[str, ...], pieces: tuple[str, ...], params: dict | None = None) -> QuantResult:
    reasons = tuple(warns) or ("input cannot be used",)
    return QuantResult(name=name, value=None, unit=unit, method=method, params=dict(params or {}), warnings=reasons, piece_ids=pieces)


def scalar(fn: Any, *args: Any, **kwargs: Any) -> float | None:
    """Call a library statistic and return a finite float or None; library errors become None."""
    try:
        with quiet():
            return helpers.finite_or_none(fn(*args, **kwargs))
    except (ValueError, TypeError, ZeroDivisionError, KeyError, IndexError, FloatingPointError):
        return None


_SERIES_IN = {"type": "series", "required": True}
_BENCH_IN = {"type": "series", "required": False, "default": None}
_RF_IN = {"type": "float", "required": False, "default": 0.0}
_PPY_IN = {"type": "int", "required": False, "default": 252}


def _std_inputs(*, benchmark: bool = False) -> dict:
    out: dict = {"returns": _SERIES_IN}
    if benchmark:
        out["benchmark"] = {"type": "series", "required": True}
    out["rf"] = _RF_IN
    out["periods_per_year"] = _PPY_IN
    return out


# --------------------------------------------------------------------------------------------------------------
# QS-038: header
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.summary_header", module="metrics", summary="Report header: period, risk-free rate, observations and time in market.",
                   inputs=_std_inputs(), piece_ids=("QS-038",))
def summary_header(returns: Any, rf: float = 0.0, periods_per_year: int = 252) -> QuantResult:
    name, pieces, method = "metrics.summary_header", ("QS-038",), "period bounds and share of non-zero periods"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "mixed", method, warns, pieces)
    s = _lib.returns_stats()
    value = {"start": r.index[0], "end": r.index[-1], "observations": len(r), "risk_free_rate": rf,
             "time_in_market": scalar(s.exposure, r, prepare_returns=False)}
    return finish(name, value, unit="mixed", method=method, series=r, pieces=pieces, params={"rf": rf, "periods_per_year": periods_per_year}, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# QS-039: returns
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.summary_returns", module="metrics", summary="Cumulative return, total (summed) return and CAGR.",
                   inputs=_std_inputs(), piece_ids=("QS-039",))
def summary_returns(returns: Any, rf: float = 0.0, periods_per_year: int = 252) -> QuantResult:
    name, pieces, method = "metrics.summary_returns", ("QS-039",), "compounded product, simple sum and annualised geometric rate"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "pct", method, warns, pieces)
    s = _lib.returns_stats()
    value = {"cumulative_return": scalar(s.comp, r), "total_return": float(r.sum()),
             "cagr": scalar(s.cagr, r, rf, True, periods_per_year)}
    return finish(name, value, unit="pct", method=method, series=r, pieces=pieces, params={"rf": rf, "periods_per_year": periods_per_year}, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# QS-040a, QS-040b: ratios and the probabilistic Sharpe row
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.summary_ratios", module="metrics", summary="Sharpe, smart Sharpe, Sortino, smart Sortino, Sortino over root 2 and Omega.",
                   inputs=_std_inputs(), piece_ids=("QS-040a",))
def summary_ratios(returns: Any, rf: float = 0.0, periods_per_year: int = 252) -> QuantResult:
    name, pieces, method = "metrics.summary_ratios", ("QS-040a",), "annualised ratios on period returns"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "ratio", method, warns, pieces)
    s = _lib.returns_stats()
    sortino = scalar(s.sortino, r, rf, periods_per_year, True)
    smart_sortino = scalar(s.smart_sortino, r, rf, periods_per_year, True)
    value = {"sharpe": scalar(s.sharpe, r, rf, periods_per_year, True),
             "smart_sharpe": scalar(s.smart_sharpe, r, rf, periods_per_year, True),
             "sortino": sortino, "smart_sortino": smart_sortino,
             "sortino_over_sqrt2": None if sortino is None else sortino / math.sqrt(2),
             "smart_sortino_over_sqrt2": None if smart_sortino is None else smart_sortino / math.sqrt(2),
             "omega": scalar(s.omega, r, rf, 0.0, periods_per_year)}
    return finish(name, value, unit="ratio", method=method, series=r, pieces=pieces, params={"rf": rf, "periods_per_year": periods_per_year}, warnings=warns)


@registry.register("metrics.summary_psr_row", module="metrics", summary="Probabilistic Sharpe row of the summary: the value of validation.psr, never the library's own.",
                   inputs={"returns": _SERIES_IN, "sr_benchmark": {"type": "float", "required": False, "default": 0.0}}, piece_ids=("QS-040b",))
def summary_psr_row(returns: Any, sr_benchmark: float = 0.0) -> QuantResult:
    name, pieces = "metrics.summary_psr_row", ("QS-040b",)
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "prob", "validation.psr", warns, pieces)
    try:
        from hokma.quant.validation import overfit
    except ImportError:
        return empty(name, "prob", "validation.psr", ("validation.psr is not available",), pieces)
    inner = overfit.psr(r, sr_benchmark=sr_benchmark)
    if inner.value is None:
        return empty(name, "prob", "validation.psr", tuple(warns) + tuple(inner.warnings), pieces, {"sr_benchmark": sr_benchmark})
    return QuantResult(name=name, value=inner.value, unit="prob", method="validation.psr: " + inner.method, params=dict(inner.params),
                       inputs_hash=inner.inputs_hash, as_of=inner.as_of, warnings=tuple(warns) + tuple(inner.warnings), piece_ids=pieces)


# --------------------------------------------------------------------------------------------------------------
# QS-041: drawdown
# --------------------------------------------------------------------------------------------------------------
def underwater(r: pd.Series) -> pd.Series:
    """Drawdown of the compounded path at every date; differences below 1e-12 are rounding noise of the product and count as zero."""
    wealth = (1.0 + r).cumprod()
    dd = wealth / wealth.cummax() - 1.0
    return dd.where(dd.abs() > 1e-12, 0.0)


def drawdown_episodes(r: pd.Series) -> pd.DataFrame:
    """Every underwater episode: first underwater date, valley, recovery date (or last date), calendar days and depth."""
    dd = underwater(r)
    rows = []
    start: int | None = None
    n = len(dd)
    for i in range(n):
        if dd.iloc[i] < 0 and start is None:
            start = i
        recovered = dd.iloc[i] >= 0
        if start is not None and (recovered or i == n - 1):
            segment = dd.iloc[start:i] if recovered else dd.iloc[start:i + 1]
            end_ts = dd.index[i - 1] if recovered else dd.index[i]
            rows.append({"start": dd.index[start], "valley": segment.idxmin(), "end": end_ts,
                         "days": int((end_ts - dd.index[start]).days) + 1, "depth": float(segment.min())})
            start = None
    return pd.DataFrame(rows, columns=["start", "valley", "end", "days", "depth"])


@registry.register("metrics.summary_drawdown", module="metrics", summary="Maximum drawdown with its dates, longest drawdown in days and average drawdown.",
                   inputs=_std_inputs(), piece_ids=("QS-041",))
def summary_drawdown(returns: Any, rf: float = 0.0, periods_per_year: int = 252) -> QuantResult:
    name, pieces, method = "metrics.summary_drawdown", ("QS-041",), "running peak of compounded wealth; episodes from peak to recovery"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "pct", method, warns, pieces)
    eps = drawdown_episodes(r)
    if eps.empty:
        value = {"max_drawdown": 0.0, "max_dd_date": None, "max_dd_start": None, "max_dd_end": None, "longest_dd_days": 0,
                 "avg_drawdown": 0.0, "avg_drawdown_days": 0.0, "episodes": 0}
    else:
        worst = eps.loc[eps["depth"].idxmin()]
        value = {"max_drawdown": float(worst["depth"]), "max_dd_date": worst["valley"], "max_dd_start": worst["start"], "max_dd_end": worst["end"],
                 "longest_dd_days": int(eps["days"].max()), "avg_drawdown": float(eps["depth"].mean()),
                 "avg_drawdown_days": float(eps["days"].mean()), "episodes": int(len(eps))}
    return finish(name, value, unit="pct", method=method, series=r, pieces=pieces, params={"rf": rf}, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# QS-042: volatility and relations
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.summary_volatility", module="metrics",
                   summary="Volatility, R squared and information ratio (with a benchmark), Calmar, skew, kurtosis, ulcer performance, risk-adjusted return, risk-return ratio.",
                   inputs={**_std_inputs(), "benchmark": _BENCH_IN}, piece_ids=("QS-042",))
def summary_volatility(returns: Any, benchmark: Any = None, rf: float = 0.0, periods_per_year: int = 252) -> QuantResult:
    name, pieces, method = "metrics.summary_volatility", ("QS-042",), "annualised dispersion and return-to-risk relations"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "mixed", method, warns, pieces)
    s = _lib.returns_stats()
    value: dict[str, Any] = {"volatility_annual": scalar(s.volatility, r, periods_per_year, True),
                             "calmar": scalar(s.calmar, r, prepare_returns=False, compounded=True, periods=periods_per_year),
                             "skew": scalar(s.skew, r, prepare_returns=False), "kurtosis": scalar(s.kurtosis, r, prepare_returns=False),
                             "ulcer_performance_index": scalar(s.ulcer_performance_index, r, rf),
                             "risk_adjusted_return": scalar(s.rar, r, rf, periods=periods_per_year, compounded=True),
                             "risk_return_ratio": scalar(s.risk_return_ratio, r, prepare_returns=False),
                             "r_squared": None, "information_ratio": None}
    if benchmark is not None:
        ra, ba, bw = align_pair(r, benchmark)
        warns = tuple(warns) + tuple(bw)
        if ra is not None:
            value["r_squared"] = scalar(s.r_squared, ra, ba, prepare_returns=False)
            value["information_ratio"] = scalar(s.information_ratio, ra, ba, prepare_returns=False)
    return finish(name, value, unit="mixed", method=method, series=r, pieces=pieces, params={"rf": rf, "periods_per_year": periods_per_year}, warnings=warns,
                  hash_extra=(() if benchmark is None else (helpers.as_series(benchmark, name="benchmark"),)))


# --------------------------------------------------------------------------------------------------------------
# QS-043 operations, QS-044a expectations and risk, QS-044b ruin row, QS-045 streaks and ratios
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.summary_operations", module="metrics", summary="Average return, average win, average loss, win/loss ratio and profit ratio.",
                   inputs={"returns": _SERIES_IN}, piece_ids=("QS-043",))
def summary_operations(returns: Any) -> QuantResult:
    from hokma.quant.metrics import trades
    name, pieces, method = "metrics.summary_operations", ("QS-043",), "statistics of non-zero period returns"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "mixed", method, warns, pieces)
    value = {"avg_return": trades.avg_return_value(r), "avg_win": trades.avg_win_value(r), "avg_loss": trades.avg_loss_value(r),
             "win_loss_ratio": trades.payoff_value(r), "profit_ratio": trades.profit_ratio_value(r)}
    return finish(name, value, unit="mixed", method=method, series=r, pieces=pieces, warnings=warns)


@registry.register("metrics.summary_expectations", module="metrics", summary="Expected daily, monthly and yearly return, Kelly fraction, daily VaR and expected shortfall.",
                   inputs={"returns": _SERIES_IN}, piece_ids=("QS-044a",))
def summary_expectations(returns: Any) -> QuantResult:
    from hokma.quant.metrics import trades
    name, pieces, method = "metrics.summary_expectations", ("QS-044a",), "geometric expectation per period; parametric tail figures"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "mixed", method, warns, pieces)
    s = _lib.returns_stats()
    var = scalar(s.var, r, prepare_returns=False)
    cvar = scalar(s.cvar, r, prepare_returns=False)
    value = {"expected_daily": scalar(s.expected_return, r, prepare_returns=False),
             "expected_monthly": scalar(s.expected_return, r, aggregate="ME", prepare_returns=False),
             "expected_yearly": scalar(s.expected_return, r, aggregate="YE", prepare_returns=False),
             "kelly_criterion": trades.kelly_value(r),
             "daily_var": None if var is None else -abs(var), "expected_shortfall": None if cvar is None else -abs(cvar)}
    return finish(name, value, unit="mixed", method=method, series=r, pieces=pieces, warnings=warns)


@registry.register("metrics.summary_ruin_row", module="metrics", summary="Risk of ruin row of the summary: the value of metrics.risk_of_ruin.",
                   inputs={"returns": _SERIES_IN, "ruin_level": {"type": "float", "required": False, "default": 0.5}}, piece_ids=("QS-044b",))
def summary_ruin_row(returns: Any, ruin_level: float = 0.5) -> QuantResult:
    from hokma.quant.metrics import trades
    inner = trades.risk_of_ruin(returns, ruin_level=ruin_level)
    return QuantResult(name="metrics.summary_ruin_row", value=inner.value, unit=inner.unit, method=inner.method, params=dict(inner.params),
                       inputs_hash=inner.inputs_hash, as_of=inner.as_of, warnings=inner.warnings, piece_ids=("QS-044b",))


@registry.register("metrics.summary_streaks", module="metrics",
                   summary="Longest win and loss streaks, gain/pain, payoff, profit factor, common sense ratio, CPC index, tail ratio and outlier ratios.",
                   inputs={"returns": _SERIES_IN, "rf": _RF_IN}, piece_ids=("QS-045",))
def summary_streaks(returns: Any, rf: float = 0.0) -> QuantResult:
    from hokma.quant.metrics import trades
    name, pieces, method = "metrics.summary_streaks", ("QS-045",), "streak lengths and gain/loss ratios of period returns"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "mixed", method, warns, pieces)
    s = _lib.returns_stats()
    value = {"max_consecutive_wins": trades.streak_value(r, wins=True), "max_consecutive_losses": trades.streak_value(r, wins=False),
             "gain_pain_ratio": scalar(s.gain_to_pain_ratio, r, rf), "gain_pain_ratio_1m": scalar(s.gain_to_pain_ratio, r, rf, "ME"),
             "payoff_ratio": trades.payoff_value(r), "profit_factor": trades.profit_factor_value(r),
             "common_sense_ratio": scalar(s.common_sense_ratio, r, prepare_returns=False), "cpc_index": scalar(s.cpc_index, r, prepare_returns=False),
             "tail_ratio": scalar(s.tail_ratio, r, prepare_returns=False),
             "outlier_win_ratio": trades.outlier_win_value(r), "outlier_loss_ratio": trades.outlier_loss_value(r)}
    return finish(name, value, unit="mixed", method=method, series=r, pieces=pieces, params={"rf": rf}, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# QS-046: returns by period (anchored on the last observation, never on today's date)
# --------------------------------------------------------------------------------------------------------------
def _period_return(r: pd.Series, since: pd.Timestamp) -> float | None:
    part = r[r.index >= since]
    return None if part.empty else float((1.0 + part).prod() - 1.0)


def _annualised_window(r: pd.Series, years: int) -> float | None:
    end = r.index[-1]
    since = end - pd.DateOffset(years=years)
    if r.index[0] > since:
        return None
    part = r[r.index >= since]
    if part.empty:
        return None
    total = float((1.0 + part).prod())
    span = (part.index[-1] - part.index[0]).days / 365.25
    return None if span <= 0 or total <= 0 else total ** (1.0 / span) - 1.0


@registry.register("metrics.summary_periods", module="metrics", summary="Returns by trailing period: MTD, 3M, 6M, YTD, 1Y, annualised 3Y, 5Y, 10Y and all-time.",
                   inputs={"returns": _SERIES_IN}, piece_ids=("QS-046",))
def summary_periods(returns: Any) -> QuantResult:
    name, pieces, method = "metrics.summary_periods", ("QS-046",), "compounded return since an anchor date counted back from the last observation"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "pct", method, warns, pieces)
    end = r.index[-1]
    value = {"mtd": _period_return(r, pd.Timestamp(end.year, end.month, 1)), "3m": _period_return(r, end - pd.DateOffset(months=3)),
             "6m": _period_return(r, end - pd.DateOffset(months=6)), "ytd": _period_return(r, pd.Timestamp(end.year, 1, 1)),
             "1y": _period_return(r, end - pd.DateOffset(years=1)), "3y_annualised": _annualised_window(r, 3),
             "5y_annualised": _annualised_window(r, 5), "10y_annualised": _annualised_window(r, 10)}
    span = (end - r.index[0]).days / 365.25
    total = float((1.0 + r).prod())
    value["all_time_annualised"] = total ** (1.0 / span) - 1.0 if span > 0 and total > 0 else None
    return finish(name, value, unit="pct", method=method, series=r, pieces=pieces, params={"anchor": end.isoformat()[:10]}, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# QS-048: quality
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.summary_quality", module="metrics", summary="Recovery factor, ulcer index and serenity index.",
                   inputs={"returns": _SERIES_IN, "rf": _RF_IN}, piece_ids=("QS-048",))
def summary_quality(returns: Any, rf: float = 0.0) -> QuantResult:
    name, pieces, method = "metrics.summary_quality", ("QS-048",), "drawdown-based quality figures"
    r, warns = prepare_series(returns)
    if r is None:
        return empty(name, "ratio", method, warns, pieces)
    s = _lib.returns_stats()
    value = {"recovery_factor": scalar(s.recovery_factor, r), "ulcer_index": scalar(s.ulcer_index, r), "serenity_index": scalar(s.serenity_index, r, rf)}
    return finish(name, value, unit="ratio", method=method, series=r, pieces=pieces, params={"rf": rf}, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# QS-034: strategy against benchmark by period
# --------------------------------------------------------------------------------------------------------------
_AGG = {"D": "D", "W": "W", "M": "ME", "Q": "QE", "Y": "YE"}


@registry.register("metrics.compare_periods", module="metrics", summary="Strategy against benchmark return per period (D, W, M, Q, Y) with multiplier and winner.",
                   inputs={"returns": _SERIES_IN, "benchmark": {"type": "series", "required": True}, "aggregate": {"type": "str", "required": False, "default": "M"}},
                   piece_ids=("QS-034",))
def compare_periods(returns: Any, benchmark: Any, aggregate: str = "M") -> QuantResult:
    name, pieces, method = "metrics.compare_periods", ("QS-034",), "compounded return per period, strategy against benchmark"
    if aggregate not in _AGG:
        raise BadInput(f"aggregate must be one of {sorted(_AGG)}, got {aggregate!r}")
    r, b, warns = align_pair(returns, benchmark)
    if r is None:
        return empty(name, "pct", method, warns, pieces, {"aggregate": aggregate})
    with quiet():
        table = _lib.returns_stats().compare(r, b, aggregate=_AGG[aggregate], prepare_returns=False)
    rows = []
    for idx, row in table.iterrows():
        label = "-".join(str(p) for p in idx) if isinstance(idx, tuple) else str(idx)[:10]
        rows.append({"period": label, "benchmark": row["Benchmark"], "returns": row["Returns"], "multiplier": row["Multiplier"], "won": str(row["Won"])})
    return finish(name, {"aggregate": aggregate, "rows": rows}, unit="pct", method=method, series=r, pieces=pieces,
                  params={"aggregate": aggregate}, warnings=warns, hash_extra=(b,))


# --------------------------------------------------------------------------------------------------------------
# AH-36: metrics of a fund from its value curve
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.fund_metrics", module="metrics", summary="Fund metrics from a value curve: total and annualised return, Sharpe, maximum drawdown and excess over a benchmark curve.",
                   inputs={"nav": _SERIES_IN, "capital": {"type": "float", "required": True}, "benchmark_nav": {"type": "series", "required": False, "default": None},
                           "periods_per_year": _PPY_IN}, piece_ids=("AH-36",))
def fund_metrics(nav: Any, capital: float, benchmark_nav: Any = None, periods_per_year: int = 252) -> QuantResult:
    name, pieces, method = "metrics.fund_metrics", ("AH-36",), "returns of the curve including the starting capital; years from calendar days"
    curve, warns = prepare_series(nav, name="nav", min_obs=2, allow_constant=True)
    if curve is None or not (isinstance(capital, (int, float)) and capital > 0):
        return empty(name, "mixed", method, tuple(warns) + (() if isinstance(capital, (int, float)) and capital > 0 else ("capital must be positive",)), pieces)
    if (curve <= 0).any():
        return empty(name, "mixed", method, tuple(warns) + ("value curve has non-positive values",), pieces)
    values = np.concatenate([[float(capital)], curve.to_numpy(dtype=float)])
    period_returns = values[1:] / values[:-1] - 1.0
    total = values[-1] / capital - 1.0
    first_end = curve.index[0] if isinstance(curve.index, pd.DatetimeIndex) else None
    days = (curve.index[-1] - first_end).days if first_end is not None else 0
    years = max(days / 365.25, 0.01)
    annualised = (1.0 + total) ** (1.0 / years) - 1.0
    sd = float(period_returns.std(ddof=1)) if len(period_returns) > 1 else 0.0
    sharpe = float(period_returns.mean() / sd * math.sqrt(periods_per_year)) if sd > 0 else None
    peak = np.maximum.accumulate(values)
    max_dd = float(((peak - values) / peak).max())
    value: dict[str, Any] = {"total_return": total, "annualized_return": annualised, "sharpe_ratio": sharpe, "max_drawdown": max_dd,
                             "observations": int(len(curve))}
    if sharpe is None:
        warns = tuple(warns) + ("constant period returns: Sharpe is not defined",)
    if benchmark_nav is not None:
        bench, bw = prepare_series(benchmark_nav, name="benchmark_nav", min_obs=1, allow_constant=True)
        if bench is None:
            warns = tuple(warns) + tuple("benchmark: " + w for w in bw)
        else:
            bench_total = float(bench.iloc[-1]) / capital - 1.0
            value["benchmark_return"] = bench_total
            value["excess_return"] = total - bench_total
    return finish(name, value, unit="mixed", method=method, series=curve, pieces=pieces,
                  params={"capital": capital, "periods_per_year": periods_per_year}, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# FF-001, FF-002, FF-003: price-series statistics
# --------------------------------------------------------------------------------------------------------------
def prices_from_returns(r: pd.Series, start: float = 100.0) -> pd.Series:
    """A price curve that starts at start on the day before the first return."""
    curve = start * (1.0 + r).cumprod()
    first = r.index[0] - pd.tseries.offsets.BDay(1)
    return pd.concat([pd.Series([start], index=[first]), curve])


def _stats_dict(stats: pd.Series) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in stats.items():
        out[str(key)] = val
    return out


@registry.register("metrics.performance_stats", module="metrics",
                   summary="The full statistics sheet of one series (returns, risk, ratios by frequency, drawdowns). Daily against monthly Sharpe exposes autocorrelation.",
                   inputs={"returns": _SERIES_IN, "rf": _RF_IN}, piece_ids=("FF-001",))
def performance_stats(returns: Any, rf: float = 0.0) -> QuantResult:
    name, pieces, method = "metrics.performance_stats", ("FF-001",), "statistics sheet computed on a price curve built from the returns"
    r, warns = prepare_series(returns, min_obs=30)
    if r is None:
        return empty(name, "mixed", method, warns, pieces)
    with quiet():
        stats = _lib.price_stats().PerformanceStats(prices_from_returns(r), rf=rf)
        stats.set_riskfree_rate(rf)
        sheet = _stats_dict(stats.stats)
    daily, monthly = helpers.finite_or_none(sheet.get("daily_sharpe")), helpers.finite_or_none(sheet.get("monthly_sharpe"))
    sheet["sharpe_monthly_over_daily"] = None if daily in (None, 0.0) or monthly is None else monthly / daily
    return finish(name, sheet, unit="mixed", method=method, series=r, pieces=pieces, params={"rf": rf}, warnings=warns)


@registry.register("metrics.group_stats", module="metrics", summary="The statistics sheet of several series side by side (robots, pairs or assets).",
                   inputs={"returns_frame": {"type": "dataframe", "required": True}, "rf": _RF_IN}, piece_ids=("FF-002",))
def group_stats(returns_frame: Any, rf: float = 0.0) -> QuantResult:
    name, pieces, method = "metrics.group_stats", ("FF-002",), "statistics sheet per column, computed on price curves built from the returns"
    if not isinstance(returns_frame, pd.DataFrame):
        raise BadInput(f"returns_frame: expected a DataFrame, got {type(returns_frame).__name__}")
    curves: dict[str, pd.Series] = {}
    warns: list[str] = []
    kept: list[pd.Series] = []
    for col in returns_frame.columns:
        r, w = prepare_series(returns_frame[col], name=str(col), min_obs=30)
        if r is None:
            warns.extend(f"{col}: {x}" for x in w)
            continue
        curves[str(col)] = prices_from_returns(r).rename(str(col))
        kept.append(r)
    if not curves:
        return empty(name, "mixed", method, tuple(warns) or ("no usable column",), pieces)
    with quiet():
        group = _lib.price_stats().GroupStats(*curves.values())
        group.set_riskfree_rate(rf)
        table = group.stats
    value = {str(metric): {str(col): table.loc[metric, col] for col in table.columns} for metric in table.index}
    return finish(name, value, unit="mixed", method=method, series=kept[0], pieces=pieces, params={"rf": rf, "columns": list(curves)},
                  warnings=tuple(warns), hash_extra=(returns_frame.astype(float),))


@registry.register("metrics.calc_stats", module="metrics", summary="Entry point of the statistics sheets: from returns or from prices, standard or performance-only sheet.",
                   inputs={"series": {"type": "series", "required": True}, "kind": {"type": "str", "required": False, "default": "returns"},
                           "perf_only": {"type": "bool", "required": False, "default": False}, "rf": _RF_IN}, piece_ids=("FF-003",))
def calc_stats(series: Any, kind: str = "returns", perf_only: bool = False, rf: float = 0.0) -> QuantResult:
    name, pieces, method = "metrics.calc_stats", ("FF-003",), "statistics sheet of a price curve"
    if kind not in ("returns", "prices"):
        raise BadInput(f"kind must be 'returns' or 'prices', got {kind!r}")
    s, warns = prepare_series(series, min_obs=30)
    if s is None:
        return empty(name, "mixed", method, warns, pieces, {"kind": kind})
    if kind == "prices":
        if (s <= 0).any():
            return empty(name, "mixed", method, tuple(warns) + ("prices must be positive",), pieces, {"kind": kind})
        prices = s
        base = s.pct_change().dropna()
    else:
        prices = prices_from_returns(s)
        base = s
    with quiet():
        fn = _lib.price_stats().calc_perf_stats if perf_only else _lib.price_stats().calc_stats
        stats = fn(prices)
        if hasattr(stats, "set_riskfree_rate"):
            stats.set_riskfree_rate(rf)
        sheet = _stats_dict(stats.stats)
    return finish(name, sheet, unit="mixed", method=method, series=base, pieces=pieces, params={"kind": kind, "perf_only": perf_only, "rf": rf}, warnings=warns)


# --------------------------------------------------------------------------------------------------------------
# Full table
# --------------------------------------------------------------------------------------------------------------
def build_summary(returns: Any, *, benchmark: Any = None, rf: float = 0.0, periods_per_year: int = 252) -> dict[str, QuantResult]:
    """Every block of the standard table, keyed by block name. The probabilistic Sharpe row comes from validation.psr."""
    out = {
        "header": summary_header(returns, rf, periods_per_year),
        "returns": summary_returns(returns, rf, periods_per_year),
        "ratios": summary_ratios(returns, rf, periods_per_year),
        "psr": summary_psr_row(returns),
        "drawdown": summary_drawdown(returns, rf, periods_per_year),
        "volatility": summary_volatility(returns, benchmark, rf, periods_per_year),
        "operations": summary_operations(returns),
        "expectations": summary_expectations(returns),
        "ruin": summary_ruin_row(returns),
        "streaks": summary_streaks(returns, rf),
        "periods": summary_periods(returns),
        "quality": summary_quality(returns, rf),
    }
    if benchmark is not None:
        from hokma.quant.metrics import benchmark as bench_mod
        out["versus_benchmark"] = bench_mod.versus_benchmark(returns, benchmark, rf=rf, periods_per_year=periods_per_year)
    return out

"""Return series: conversions, period returns, resampling and alignment utilities (SPEC-017, metrics module).

This file also holds the small framework every metric of the module uses: input checks, result building and the
`metric` factory that registers a function in the registry. Nothing here imports a quantitative library: the
calculations go through `_lib`.
"""
from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np
import pandas as pd

from hokma.quant import helpers, registry
from hokma.quant.errors import BadInput
from hokma.quant.metrics import _lib
from hokma.quant.types import QuantResult

Compute = Callable[[pd.Series, "pd.Series | None", dict], Any]


# ---------------------------------------------------------------------------------------------------------------
# Framework
# ---------------------------------------------------------------------------------------------------------------

def to_jsonable(value: Any) -> Any:
    """Series, frames and numpy values to plain Python: dates become ISO strings, NaN becomes None."""
    if isinstance(value, pd.Series):
        return {_key(k): to_jsonable(v) for k, v in value.items() if not _is_nan(v)}
    if isinstance(value, pd.DataFrame):
        return {_key(k): {_key(c): to_jsonable(v) for c, v in row.items()} for k, row in value.iterrows()}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (pd.Timestamp, pd.Period)):
        return str(value.date()) if isinstance(value, pd.Timestamp) else str(value)
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(float(value)) else None
    return value


def _is_nan(value: Any) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _key(key: Any) -> str:
    if isinstance(key, pd.Timestamp):
        return key.date().isoformat()
    return str(key)


def check_series(values: Any, label: str, *, min_obs: int = 2, need_dispersion: bool = True) -> tuple[pd.Series | None, str | None]:
    """The series as floats, or None plus the reason it cannot be used. Wrong type or shape raises BadInput."""
    if values is None:
        return None, f"{label} is missing"
    series = helpers.as_series(values, name=label)
    if len(series) == 0:
        return None, f"{label} is empty"
    if not np.isfinite(series.to_numpy()).all():
        return None, f"{label} contains NaN or infinite values"
    if len(series) < min_obs:
        return None, f"{label} needs at least {min_obs} observation(s), got {len(series)}"
    if need_dispersion and float(series.max() - series.min()) == 0.0:
        return None, f"{label} is constant: dispersion is zero"
    return series, None


def align(series: pd.Series, other: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Both series on their common index (positional when neither has dates)."""
    if isinstance(series.index, pd.DatetimeIndex) or isinstance(other.index, pd.DatetimeIndex):
        frame = pd.concat([series, other], axis=1, join="inner").dropna()
        return frame.iloc[:, 0], frame.iloc[:, 1]
    n = min(len(series), len(other))
    return series.iloc[:n], other.iloc[:n].set_axis(series.index[:n])


def finish(name: str, *, value: Any, unit: str, method: str, piece_ids: tuple[str, ...], params: dict | None = None,
           inputs: tuple = (), as_of: Any = None, warnings: tuple[str, ...] = ()) -> QuantResult:
    """A result with its hash and a value that is plain Python. A missing value is explained in warnings."""
    params = dict(params or {})
    plain = to_jsonable(value)
    warns = tuple(warnings)
    if isinstance(plain, (dict, list)) and not plain:
        plain = None
        warns += (f"{name} has no complete window or observation for this input",)
    if plain is None and not warns:
        warns = (f"{name} is undefined for this input",)
    return QuantResult(name=name, value=plain, unit=unit, method=method, params=params,
                       inputs_hash=helpers.hash_inputs(*inputs, params), as_of=as_of, warnings=warns, piece_ids=piece_ids)


def build(name: str, *, returns: Any, benchmark: Any, compute: Compute, unit: str, method: str, piece_ids: tuple[str, ...],
          params: dict, min_obs: int = 2, need_dispersion: bool = True, label: str = "returns") -> QuantResult:
    """Check the inputs, run `compute(series, benchmark, params)` and wrap the outcome in a QuantResult."""
    def fail(reason: str) -> QuantResult:
        return QuantResult.empty(name, unit, method, reason, params=dict(params), piece_ids=piece_ids)

    series, why = check_series(returns, label, min_obs=min_obs, need_dispersion=need_dispersion)
    if series is None:
        return fail(why or "invalid input")
    bench = None
    if benchmark is not None:
        bench, why = check_series(benchmark, "benchmark", min_obs=min_obs, need_dispersion=need_dispersion)
        if bench is None:
            return fail(why or "invalid benchmark")
        series, bench = align(series, bench)
        if len(series) < min_obs:
            return fail("returns and benchmark share fewer than %d observation(s)" % min_obs)
    try:
        with _lib.quiet():
            value = compute(series, bench, params)
    except BadInput:
        raise
    except (ArithmeticError, ValueError, TypeError, KeyError, IndexError, AttributeError, NotImplementedError, np.linalg.LinAlgError) as exc:
        return fail(f"{name} could not be computed: {exc}")
    return finish(name, value=value, unit=unit, method=method, piece_ids=piece_ids, params=params,
                  inputs=(series,) if bench is None else (series, bench), as_of=helpers.last_as_of(series))


def _type_name(default: Any) -> str:
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    return "str" if isinstance(default, str) else "any"


def metric(name: str, *, piece_ids: tuple[str, ...], summary: str, unit: str, method: str, compute: Compute,
           benchmark: bool = False, defaults: dict | None = None, rf: bool = False, ppy: bool = False,
           min_obs: int = 2, need_dispersion: bool = True, input_label: str = "returns",
           doc: dict | None = None) -> Callable[..., QuantResult]:
    """Create and register the function `name` ("metrics.<function>") around a compute callable.

    `rf` adds the annual risk-free rate input and `ppy` the periods per year. The generated function takes
    the series (and the benchmark when `benchmark` is true) followed by keyword parameters.
    """
    spec_defaults: dict = {}
    if rf:
        spec_defaults["rf"] = 0.0
    if ppy:
        spec_defaults["periods_per_year"] = 252
    spec_defaults.update(defaults or {})
    inputs: dict = {"returns": {"type": "series", "required": True}}
    if input_label != "returns":
        inputs = {input_label: {"type": "series", "required": True}}
    if benchmark:
        inputs["benchmark"] = {"type": "series", "required": True}
    for key, default in spec_defaults.items():
        inputs[key] = {"type": _type_name(default), "required": False, "default": default}
    short = name.split(".", 1)[1]

    def check_params(params: dict) -> dict:
        unknown = set(params) - set(spec_defaults)
        if unknown:
            raise BadInput(f"{name}: unknown parameter(s) {sorted(unknown)}")
        return {**spec_defaults, **params}

    def fn(*args: Any, **kwargs: Any) -> QuantResult:
        """Positional: (series[, benchmark]); by keyword the series is named `input_label`."""
        args = list(args)
        series = args.pop(0) if args else kwargs.pop(input_label, None)
        bench = None
        if benchmark:
            bench = args.pop(0) if args else kwargs.pop("benchmark", None)
        if args:
            raise BadInput(f"{name}: too many positional arguments")
        return build(name, returns=series, benchmark=bench, compute=compute, unit=unit, method=method,
                     piece_ids=piece_ids, params=check_params(kwargs), min_obs=min_obs,
                     need_dispersion=need_dispersion, label=input_label)
    fn.__name__ = short
    fn.__qualname__ = short
    fn.__doc__ = summary
    registry.register(name, module="metrics", summary=summary, inputs=inputs, piece_ids=piece_ids)(fn)
    return fn


def scalar_function(name: str, *, piece_ids: tuple[str, ...], summary: str, unit: str, method: str, inputs: dict,
                    compute: Callable[..., Any]) -> Callable[..., QuantResult]:
    """Register a function whose inputs are plain numbers, dates or labels instead of a return series."""
    def fn(**kwargs: Any) -> QuantResult:
        params = {k: (v.get("default") if isinstance(v, dict) else None) for k, v in inputs.items()}
        unknown = set(kwargs) - set(inputs)
        if unknown:
            raise BadInput(f"{name}: unknown parameter(s) {sorted(unknown)}")
        params.update(kwargs)
        missing = [k for k, v in inputs.items() if v.get("required") and params.get(k) is None]
        if missing:
            return QuantResult.empty(name, unit, method, f"missing input(s) {missing}", params={}, piece_ids=piece_ids)
        try:
            with _lib.quiet():
                value = compute(**params)
        except BadInput:
            raise
        except (ArithmeticError, ValueError, TypeError, KeyError, IndexError, AttributeError, NotImplementedError) as exc:
            return QuantResult.empty(name, unit, method, f"{name} could not be computed: {exc}", params=to_jsonable(params), piece_ids=piece_ids)
        return finish(name, value=value, unit=unit, method=method, piece_ids=piece_ids, params=to_jsonable(params), inputs=())
    short = name.split(".", 1)[1]
    fn.__name__ = short
    fn.__qualname__ = short
    fn.__doc__ = summary
    registry.register(name, module="metrics", summary=summary, inputs=inputs, piece_ids=piece_ids)(fn)
    return fn


# ---------------------------------------------------------------------------------------------------------------
# Conversions (FF-004, QS-058, SK-105)
# ---------------------------------------------------------------------------------------------------------------

prices_to_returns = metric(
    "metrics.prices_to_returns", input_label="prices", piece_ids=("SK-105", "QS-058"), unit="ratio", method="price[t] / price[t-1] - 1 (log option: ln of the ratio)",
    summary="Convert a price series to simple or log returns; the first observation is dropped.",
    defaults={"log": False},
    compute=lambda s, b, p: _lib.prices_to_returns(s, log=bool(p["log"])))

to_returns = metric(
    "metrics.to_returns", input_label="prices", piece_ids=("FF-004", "QS-058"), unit="ratio", method="price[t] / price[t-1] - 1",
    summary="Simple returns of a price series (NAV to returns).",
    compute=lambda s, b, p: _lib.ffn.to_returns(s).dropna())

to_log_returns = metric(
    "metrics.to_log_returns", input_label="prices", piece_ids=("FF-004", "QS-058"), unit="ratio", method="ln(price[t] / price[t-1])",
    summary="Log returns of a price series.",
    compute=lambda s, b, p: _lib.ffn.to_log_returns(s).dropna())

to_price_index = metric(
    "metrics.to_price_index", piece_ids=("FF-004",), unit="index", method="start * cumprod(1 + r), with one extra starting row",
    summary="Price index (NAV) built from returns; the first row is the start value one day before the first return.",
    defaults={"start": 100.0},
    compute=lambda s, b, p: _lib.ffn.to_price_index(s, start=float(p["start"])))

to_prices = metric(
    "metrics.to_prices", piece_ids=("QS-058",), unit="usd", method="base * cumprod(1 + r)",
    summary="Wealth path from returns and a starting balance, one row per return.",
    defaults={"base": 100000.0},
    compute=lambda s, b, p: _lib.qutils.to_prices(s, base=float(p["base"])))

log_returns = metric(
    "metrics.log_returns", piece_ids=("QS-058",), unit="ratio", method="ln(1 + r)",
    summary="Convert simple returns to log returns.",
    compute=lambda s, b, p: _lib.qutils.log_returns(s))

rebase = metric(
    "metrics.rebase", input_label="prices", piece_ids=("FF-004", "QS-058"), unit="index", method="price / first price * base",
    summary="Rebase a price series so that its first value equals base.", defaults={"base": 100.0},
    compute=lambda s, b, p: _lib.ffn.rebase(s, value=float(p["base"])))


def _group(s: pd.Series, b: pd.Series | None, p: dict) -> pd.Series:
    freq = _lib.resample_alias(str(p["period"]))
    periods = pd.DatetimeIndex(s.index).to_period("D" if freq == "D" else freq.replace("ME", "M").replace("QE", "Q").replace("YE", "Y"))
    return _lib.qutils.group_returns(s, periods, compounded=bool(p["compounded"]))


group_returns = metric(
    "metrics.group_returns", piece_ids=("QS-058",), unit="ratio", method="returns compounded (or summed) inside each calendar period",
    summary="Aggregate returns by day, week, month, quarter or year.", defaults={"period": "month", "compounded": True},
    compute=_group)


def _portfolio(s: pd.Series, b: pd.Series | None, p: dict) -> pd.Series:
    return _lib.qutils.make_portfolio(s, start_balance=float(p["start_balance"]), mode=str(p["mode"]))


make_portfolio = metric(
    "metrics.make_portfolio", piece_ids=("QS-058",), unit="usd", method="balance path: compounded ('comp') or summed ('sum') returns",
    summary="Balance path of a return series from a starting balance.", defaults={"start_balance": 100000.0, "mode": "comp"},
    compute=_portfolio)


# ---------------------------------------------------------------------------------------------------------------
# Compounding, rank and expected return (QS-001, QS-003, QS-017)
# ---------------------------------------------------------------------------------------------------------------

total_return = metric(
    "metrics.total_return", piece_ids=("FF-006", "QS-001"), unit="ratio", method="prod(1 + r) - 1",
    summary="Compounded return over the whole series.",
    compute=lambda s, b, p: _lib.ffn.calc_total_return(_lib.ffn.to_price_index(s)))

compsum = metric(
    "metrics.compsum", piece_ids=("QS-001",), unit="ratio", method="cumprod(1 + r) - 1",
    summary="Cumulative compounded return, one value per period.", compute=lambda s, b, p: _lib.qstats.compsum(s))

pct_rank = metric(
    "metrics.pct_rank", piece_ids=("QS-001",), unit="pct", method="percentile rank of the last price inside a rolling window",
    summary="Rolling percentile rank of a price series (0 to 100).", defaults={"window": 60}, input_label="prices",
    min_obs=2, compute=lambda s, b, p: _lib.qstats.pct_rank(s, window=int(p["window"])).dropna())

expected_return = metric(
    "metrics.expected_return", piece_ids=("QS-003",), unit="ratio", method="geometric mean per period: prod(1 + r)^(1/n) - 1",
    summary="Expected (geometric mean) return per period.", defaults={"compounded": True},
    compute=lambda s, b, p: _lib.qstats.expected_return(s, compounded=bool(p["compounded"])))

geometric_mean = metric(
    "metrics.geometric_mean", piece_ids=("QS-003",), unit="ratio", method="geometric mean per period: prod(1 + r)^(1/n) - 1",
    summary="Geometric mean return per period.", defaults={"compounded": True},
    compute=lambda s, b, p: _lib.qstats.geometric_mean(s, compounded=bool(p["compounded"])))

ghpr = metric(
    "metrics.ghpr", piece_ids=("QS-003",), unit="ratio", method="geometric holding period return per period",
    summary="Geometric holding period return per period.", defaults={"compounded": True},
    compute=lambda s, b, p: _lib.qstats.ghpr(s, compounded=bool(p["compounded"])))

cagr = metric(
    "metrics.cagr", piece_ids=("QS-017",), unit="ratio", method="(1 + total return)^(1/years) - 1, years = calendar days / 365",
    summary="Compound annual growth rate of a return series with a date index.", rf=True, ppy=True, defaults={"compounded": True},
    compute=lambda s, b, p: _lib.qstats.cagr(s, rf=float(p["rf"]), compounded=bool(p["compounded"]), periods=int(p["periods_per_year"])))

rar = metric(
    "metrics.rar", piece_ids=("QS-017",), unit="ratio", method="CAGR divided by the share of periods with exposure",
    summary="Return adjusted for the time the strategy was exposed to the market.", rf=True,
    compute=lambda s, b, p: _lib.qstats.rar(s, rf=float(p["rf"])))

cagr_from_prices = metric(
    "metrics.cagr_from_prices", input_label="prices", piece_ids=("FF-006",), unit="ratio", method="(last / first)^(1 / year fraction) - 1",
    summary="Compound annual growth rate of a price series with a date index.",
    compute=lambda s, b, p: _lib.ffn.calc_cagr(s))


def _period_return(kind: str) -> Compute:
    def run(s: pd.Series, b: pd.Series | None, p: dict) -> float:
        prices = _lib.ffn.to_price_index(s)
        if kind == "mtd":
            return float(_lib.ffn.calc_mtd(prices, prices.resample("ME").last()))
        return float(_lib.ffn.calc_ytd(prices, prices.resample("YE").last()))
    return run


mtd = metric("metrics.mtd", piece_ids=("FF-006",), unit="ratio", method="return from the last close of the previous month",
             summary="Month-to-date return at the last observation.", compute=_period_return("mtd"))
ytd = metric("metrics.ytd", piece_ids=("FF-006",), unit="ratio", method="return from the last close of the previous year",
             summary="Year-to-date return at the last observation.", compute=_period_return("ytd"))

# ---------------------------------------------------------------------------------------------------------------
# Resampling and series utilities (FF-013, FF-021, FF-022, FF-023)
# ---------------------------------------------------------------------------------------------------------------

_STATISTICS: dict[str, Callable[[pd.Series], float]] = {
    "mean": lambda x: float(x.mean()), "std": lambda x: float(x.std(ddof=1)), "median": lambda x: float(x.median()),
    "sum": lambda x: float(x.sum()), "total_return": lambda x: float((1.0 + x).prod() - 1.0),
    "sharpe": lambda x: float(x.mean() / x.std(ddof=1) * math.sqrt(252)),
}


def _statistic(label: str) -> Callable[[pd.Series], float]:
    try:
        return _STATISTICS[label]
    except KeyError as exc:
        raise BadInput(f"unknown statistic {label!r}; use one of {sorted(_STATISTICS)}") from exc


def _resample(s: pd.Series, b: pd.Series | None, p: dict) -> dict:
    from hokma.quant import limits

    limits.check_param("num_trials", int(p["num_trials"]))
    limits.check_product("num_trials x length", int(p["num_trials"]), len(s))
    stat = _statistic(str(p["statistic"]))
    draws = _lib.ffn.resample_returns(s, stat, seed=int(p["seed"]), num_trials=int(p["num_trials"])).astype(float)
    return {"statistic": p["statistic"], "num_trials": int(p["num_trials"]), "mean": float(draws.mean()), "std": float(draws.std(ddof=1)),
            "p05": float(draws.quantile(0.05)), "p50": float(draws.quantile(0.5)), "p95": float(draws.quantile(0.95)),
            "values": draws.tolist()}


resample_returns = metric(
    "metrics.resample_returns", piece_ids=("FF-013",), unit="ratio", method="bootstrap: draw n returns with replacement, num_trials times",
    summary="Bootstrap distribution of a statistic (mean, std, median, sum, total_return, sharpe) with a fixed seed.",
    defaults={"statistic": "mean", "seed": 0, "num_trials": 100}, compute=_resample)

rolling_apply = metric(
    "metrics.rolling_apply", piece_ids=("FF-021",), unit="ratio", method="statistic over each full window",
    summary="Apply a named statistic (mean, std, median, sum, total_return, sharpe) over a rolling window.",
    defaults={"window": 20, "statistic": "mean"},
    compute=lambda s, b, p: _lib.ffn.rollapply(s, int(p["window"]), _statistic(str(p["statistic"]))).dropna())

winsorize = metric(
    "metrics.winsorize", piece_ids=("FF-021",), unit="ratio", method="clip the lowest and highest `limits` share of values",
    summary="Series with its extreme values clipped at the given tail share.", defaults={"limits": 0.01},
    compute=lambda s, b, p: _lib.ffn.winsorize(s, limits=float(p["limits"])))

rescale = metric(
    "metrics.rescale", piece_ids=("FF-021",), unit="ratio", method="linear map of [min, max] to [low, high]",
    summary="Rescale a series linearly into the range [low, high].", defaults={"low": 0.0, "high": 1.0},
    compute=lambda s, b, p: _lib.ffn.rescale(s, min=float(p["low"]), max=float(p["high"])))

annualize = scalar_function(
    "metrics.annualize", piece_ids=("FF-021",), unit="ratio", method="(1 + r)^(one_year / duration) - 1",
    summary="Annualize a total return earned over a number of days.",
    inputs={"total_return": {"type": "float", "required": True}, "duration_days": {"type": "float", "required": True},
            "one_year": {"type": "float", "required": False, "default": 365.0}},
    compute=lambda total_return, duration_days, one_year: float(_lib.ffn.annualize(total_return, duration_days, one_year=one_year)))

deannualize = scalar_function(
    "metrics.deannualize", piece_ids=("FF-021",), unit="ratio", method="(1 + r)^(1 / periods) - 1",
    summary="Convert an annual rate to its per-period equivalent.",
    inputs={"annual_rate": {"type": "float", "required": True}, "periods_per_year": {"type": "int", "required": False, "default": 252}},
    compute=lambda annual_rate, periods_per_year: float(_lib.ffn.deannualize(annual_rate, periods_per_year)))


def _infer_freq(s: pd.Series, b: pd.Series | None, p: dict) -> dict:
    index = pd.DatetimeIndex(s.index)
    return {"freq": _lib.ffn.infer_freq(s), "periods_per_year": _periods_for(s), "first": str(index[0].date()), "last": str(index[-1].date())}


def _periods_for(s: pd.Series) -> int | None:
    found = _lib.ffn.infer_nperiods(s)
    if found is not None:
        return int(found)
    gaps = pd.Series(pd.DatetimeIndex(s.index)).diff().dropna().dt.days
    median = float(gaps.median()) if len(gaps) else 0.0
    if median <= 0:
        return None
    return int(round(252 if median <= 1.5 else 52 if median <= 8 else 12 if median <= 35 else 4 if median <= 100 else 1))


infer_freq = metric(
    "metrics.infer_freq", piece_ids=("FF-022",), unit="label", method="pandas frequency inference, with the median gap as fallback",
    summary="Frequency of a dated series and the number of periods per year to annualize it.", compute=_infer_freq)

infer_nperiods = metric(
    "metrics.infer_nperiods", piece_ids=("FF-022",), unit="count", method="periods per year from the inferred frequency",
    summary="Number of observations per year of a dated series (252 for business days).",
    compute=lambda s, b, p: _periods_for(s))

num_days_required = scalar_function(
    "metrics.num_days_required", piece_ids=("FF-022",), unit="count", method="trading days in the lookback times the required share",
    summary="Minimum number of good observations needed to trust a lookback of the given months.",
    inputs={"lookback_months": {"type": "int", "required": True}, "period": {"type": "str", "required": False, "default": "d"},
            "perc_required": {"type": "float", "required": False, "default": 0.9},
            "periods_per_year": {"type": "int", "required": False, "default": 252}},
    compute=lambda lookback_months, period, perc_required, periods_per_year: float(_lib.ffn.get_num_days_required(
        pd.DateOffset(months=int(lookback_months)), period=period, perc_required=perc_required, annualization_factor=periods_per_year)))

asfreq_actual = metric(
    "metrics.asfreq_actual", input_label="prices", piece_ids=("FF-022",), unit="index", method="last (or first) actual observation of each period",
    summary="Resample a price series keeping actual observation dates.", defaults={"freq": "month", "how": "end"},
    compute=lambda s, b, p: _lib.ffn.asfreq_actual(s, _lib.resample_alias(str(p["freq"])), how=str(p["how"])))

to_monthly = metric(
    "metrics.to_monthly", input_label="prices", piece_ids=("FF-022",), unit="index", method="last observation of each month",
    summary="Month-end values of a price series.", compute=lambda s, b, p: _lib.ffn.to_monthly(s))


def _merge(**kwargs: Any) -> dict:
    series = []
    for label, values in kwargs.items():
        got, why = check_series(values, label, min_obs=1, need_dispersion=False)
        if got is None:
            raise ValueError(why)
        series.append(got.rename(label))
    return to_jsonable(_lib.ffn.merge(*series))


merge_series = scalar_function(
    "metrics.merge_series", piece_ids=("FF-023",), unit="table", method="outer join on the index, one column per series",
    summary="Align two series on a common index; columns are named returns and other.",
    inputs={"returns": {"type": "series", "required": True}, "other": {"type": "series", "required": True}},
    compute=lambda returns, other: _merge(returns=returns, other=other))


def _drop_duplicate_cols(**kwargs: Any) -> dict:
    columns = []
    for label, values in kwargs.items():
        series = helpers.as_series(values, name=label)
        columns.append(series.rename(series.name if series.name is not None else label))
    frame = pd.concat(columns, axis=1)
    unique = _lib.ffn.drop_duplicate_cols(frame)
    return {"columns": [str(c) for c in unique.columns], "observations": int(unique.iloc[:, 0].count()) if unique.shape[1] else 0}


drop_duplicate_cols = scalar_function(
    "metrics.drop_duplicate_cols", piece_ids=("FF-023",), unit="table",
    method="when two series carry the same name, keep the one with more observations",
    summary="Names of the series that remain after merging two series; a repeated name keeps its most complete copy.",
    inputs={"returns": {"type": "series", "required": True}, "other": {"type": "series", "required": True}},
    compute=lambda returns, other: _drop_duplicate_cols(returns=returns, other=other))

year_frac = scalar_function(
    "metrics.year_frac", piece_ids=("FF-023",), unit="years", method="elapsed seconds / 31,557,600",
    summary="Years elapsed between two ISO dates.",
    inputs={"start": {"type": "str", "required": True}, "end": {"type": "str", "required": True}},
    compute=lambda start, end: float(_lib.ffn.year_frac(pd.Timestamp(start), pd.Timestamp(end))))

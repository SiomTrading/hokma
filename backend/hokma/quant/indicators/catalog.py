"""Catalog of 13 technical indicators with their use and cautions (SPEC-019: TA-74).

indicators.catalog_value computes one catalog indicator at the last bar of daily bars and returns its use and cautions next to
the number. The catalog is also in the description of the function (quant_describe shows the choices). Windows are the SWC and SWO
daily windows by default; the DT line uses short windows and the LP line long ones (indicators._common.LINE_SCALE).
"""
from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators import momentum, overlap, volatility  # noqa: F401  (importing them registers the indicators this catalog calls)
from hokma.quant.indicators._common import (
    DEFAULT_LAST_N, _type_name, build, check_last_n, domain_problem, empty, resolve_params, scale_window, series_value,
)
from hokma.quant.indicators.screens import _base
from hokma.quant.types import QuantResult

_PIECE = ("TA-74",)
_NAME = "indicators.catalog_value"


@dataclass(frozen=True)
class Entry:
    name: str
    function: str
    column: str
    base_window: int | None
    use: str
    caution: str


CATALOG: dict[str, Entry] = {e.name: e for e in (
    Entry("close_50_sma", "indicators.sma", "sma", 50, "Medium term trend and a dynamic support or resistance level.",
          "Reacts slowly and whipsaws in sideways markets: pair it with a faster signal."),
    Entry("close_200_sma", "indicators.sma", "sma", 200, "Long term trend benchmark; price above it is a bullish context, below it bearish.",
          "Very slow: it confirms a trend long after it started and is a poor timing tool."),
    Entry("close_10_ema", "indicators.ema", "ema", 10, "Short term momentum and quick entry or exit reference.",
          "Noisy in choppy markets: confirm with a longer average before acting."),
    Entry("macd", "indicators.macd", "macd", None, "Momentum from the gap between a short and a long EMA; crossovers and divergences.",
          "Gives false signals in ranges and lags price: confirm with another indicator or the trend."),
    Entry("macds", "indicators.macd", "signal", None, "Signal line (EMA of MACD); a crossover of MACD with it is the usual trigger.",
          "A smoothing of a lagging line: it adds delay. Use it with a trend filter."),
    Entry("macdh", "indicators.macd", "histogram", None, "Histogram of MACD minus signal: shows the strength of momentum and an early fade.",
          "Sensitive to short moves and can flip often in fast markets."),
    Entry("rsi", "indicators.rsi", "rsi", None, "Overbought above 70 and oversold below 30; divergences with price warn of a fade.",
          "In a strong trend it stays extreme for a long time: read it inside the trend, not alone."),
    Entry("boll", "indicators.bollinger", "middle", None, "Middle Bollinger band (simple average): the baseline the bands are built on.",
          "It is only an average: it says where the center is, not where price goes."),
    Entry("boll_ub", "indicators.bollinger", "upper", None, "Upper band, two standard deviations above the middle: a stretched or breakout zone.",
          "Touching it is not a sell signal: in a trend price can walk the band."),
    Entry("boll_lb", "indicators.bollinger", "lower", None, "Lower band, two standard deviations below the middle: a stretched or washout zone.",
          "Touching it is not a buy signal: in a downtrend price can walk the band."),
    Entry("atr", "indicators.atr", "atr", None, "Average true range: volatility in price units for stop distance and position size.",
          "It has no direction and lags a sudden change of volatility."),
    Entry("vwma", "indicators.vwma", "vwma", None, "Volume weighted moving average: a trend line that gives more weight to the heavy volume bars.",
          "A single volume spike pulls it: check the volume before trusting a turn."),
    Entry("mfi", "indicators.mfi", "mfi", None, "Money flow index: RSI that includes volume; above 80 overbought, below 20 oversold.",
          "Like RSI it can stay extreme in a trend; confirm with the price trend."),
)}
assert len(CATALOG) == 13


def _describe_choices() -> dict[str, str]:
    return {e.name: f"{e.use} Caution: {e.caution}" for e in CATALOG.values()}


@registry.register(_NAME, module="indicators",
                   summary="Value at the last bar of one of 13 catalog indicators (close_50_sma, close_200_sma, close_10_ema, macd, macds, macdh, rsi, boll, boll_ub, boll_lb, atr, vwma, mfi) with its use and cautions. Windows follow the line: SWC and SWO daily, DT short, LP long.",
                   inputs={"bars": {"type": "dataframe", "required": True, "columns": ["open", "high", "low", "close", "volume"]},
                           "indicator": {"type": "str", "required": True, "choices": _describe_choices()},
                           "line": {"type": "str", "required": False, "default": "SWO"}}, piece_ids=_PIECE)
def catalog_value(bars: Any, indicator: str, line: str = "SWO") -> QuantResult:
    params = {"indicator": indicator, "line": str(line).upper()}
    if not isinstance(indicator, str) or indicator not in CATALOG:
        return empty(_NAME, "value", "catalog", f"indicator must be one of {sorted(CATALOG)}", params=params, piece_ids=_PIECE)
    entry = CATALOG[indicator]
    check_last_n(1)
    fn: Callable[..., QuantResult] = registry.describe(entry.function).fn  # type: ignore[assignment]
    kwargs: dict[str, Any] = {}
    if entry.base_window is not None:
        kwargs["window"] = scale_window(entry.base_window, line)
    inner = fn(bars, line=line, last_n=1, **kwargs)
    if not inner.ok:
        return empty(_NAME, "value", "catalog", inner.warnings, params={**params, **inner.params}, piece_ids=_PIECE)
    latest = inner.value["latest"][entry.column]
    if latest is None:
        return empty(_NAME, "value", "catalog", "latest value is undefined: not enough bars for the window", params={**params, **inner.params}, piece_ids=_PIECE)
    value = {"name": indicator, "value": latest, "use": entry.use, "caution": entry.caution, "indicator": entry.function}
    return build(_NAME, value, unit="value", method="catalog", params={**params, **inner.params}, piece_ids=_PIECE, inputs=(pd.DataFrame(),),
                 warnings=inner.warnings)


# ---------------------------------------------------------------------------------------------- V-30: registering a new indicator
LAGS: dict[str, float] = {}      # registry name -> declared lag in bars of the indicators registered through register_indicator
OUTPUTS = ("series", "value")


def lag_of(name: str) -> float | None:
    """The lag in bars declared when the indicator was registered, or None for a function registered another way."""
    return LAGS.get(name)


def _declared_lag(full: str, lag_bars: Any) -> float:
    if lag_bars is None:
        raise BadInput(f"lag_missing: {full} must declare its measured lag in bars (lag_bars); an indicator without it is not registered")
    if isinstance(lag_bars, bool) or not isinstance(lag_bars, (int, float, np.integer, np.floating)):
        raise BadInput(f"lag_invalid: lag_bars of {full} must be a number of bars, got {type(lag_bars).__name__}")
    lag = float(lag_bars)
    if not math.isfinite(lag) or lag < 0:
        raise BadInput(f"lag_invalid: lag_bars of {full} must be a finite number that is not negative, got {lag_bars}")
    return lag


def _lag_text(lag: float) -> str:
    whole = int(lag) if lag == int(lag) else None
    return f"{whole} bar{'s' if whole != 1 else ''}" if whole is not None else f"{lag:g} bars"


def measure_ramp_lag(fn: Callable[..., pd.Series | pd.DataFrame], column: str | None = None, n: int = 600, **params: Any) -> float:
    """Lag in bars of an indicator on a unit ramp (a close that rises one unit per bar): the median over the last 50 bars of the close
    minus the output. The same measure as docs/quant/indicators.md; fn takes the bars frame and the parameters."""
    if n < 100:
        raise BadInput("n must be at least 100 bars")
    close = 100.0 + np.arange(n, dtype=float)
    frame = pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": 1000.0}, index=pd.bdate_range("2020-01-01", periods=n))
    out = fn(frame, **params)
    if isinstance(out, pd.DataFrame):
        if column is None or column not in out.columns:
            raise BadInput(f"column must be one of {list(out.columns)}")
        out = out[column]
    return float(np.median((frame["close"] - out).iloc[-50:].to_numpy(dtype=float)))


def register_indicator(name: str, fn: Callable[..., Any], *, pieces: tuple[str, ...], needed: tuple[str, ...], lag_bars: float | None, summary: str,
                       unit: str = "value", defaults: dict[str, Any] | None = None, min_bars: int = 2, method: str | None = None,
                       closed_only: bool = False, output: str = "series") -> Callable[..., QuantResult]:
    """Register "indicators.<name>" with its lag. fn takes the clean bars frame and the resolved parameters and returns a Series or a
    DataFrame (output "series": the latest row plus last_n rows) or a dict (output "value": one result for the window).

    lag_bars is the measured lag in bars and is required: without it the indicator is not registered (BadInput lag_missing). The lag is
    shown at the end of the summary agents read and is kept in LAGS. closed_only adds as_of and reads only closed daily bars (a bar is
    closed once 16:00 New York of its date has passed); a boolean closed column always applies. Windows follow the line like any indicator.
    """
    if not isinstance(name, str) or not name or not name.replace("_", "").isalnum():
        raise BadInput("name must be a short word with letters, digits and underscores")
    full = f"indicators.{name}"
    lag = _declared_lag(full, lag_bars)
    if not callable(fn) or not pieces or not needed or output not in OUTPUTS or not summary:
        raise BadInput(f"{full}: give a function, its piece ids, the bar columns it needs, a summary and an output of {list(OUTPUTS)}")
    base = dict(defaults or {})
    inputs: dict[str, Any] = {"bars": {"type": "dataframe", "required": True, "columns": list(needed)},
                              "line": {"type": "str", "required": False, "default": "SWO"}}
    if output == "series":
        inputs["last_n"] = {"type": "int", "required": False, "default": DEFAULT_LAST_N}
    if closed_only:
        inputs["as_of"] = {"type": "str", "required": False}
    for key, value in base.items():
        inputs[key] = {"type": _type_name(value), "required": False, "default": value}
    how = method or name

    @functools.wraps(fn)
    def wrapper(bars: Any, line: str = "SWO", last_n: int | None = DEFAULT_LAST_N, as_of: Any = None, **params: Any) -> QuantResult:
        if as_of is not None and not closed_only:
            raise BadInput(f"{full}: as_of is not an argument of this indicator")
        resolved = resolve_params(line, base, params)
        keep = check_last_n(last_n) if output == "series" else None
        report = {**resolved, "line": str(line).upper()}
        problem = domain_problem(resolved)
        if problem:
            return empty(full, unit, how, problem, params=report, piece_ids=pieces)
        frame, dropped, warns = _base.daily_closed(bars, needed, as_of=as_of if closed_only else None, min_bars=min_bars)
        if closed_only or dropped:
            report["dropped_open_bars"] = dropped
        warns = warns if closed_only else tuple(w for w in warns if not w.startswith("as_of not given"))
        if frame is None:
            return empty(full, unit, how, warns, params=report, piece_ids=pieces)
        try:
            out = fn(frame, **resolved)
        except BadInput:
            raise
        except Exception as exc:  # a failure on odd data is a data problem, not a crash
            return empty(full, unit, how, warns + (f"{type(exc).__name__}: {exc}",), params=report, piece_ids=pieces)
        value = series_value(out, name, keep) if output == "series" else out
        if value is None:
            return empty(full, unit, how, warns + ("latest value is undefined: not enough bars for the window or a zero denominator",), params=report, piece_ids=pieces)
        return build(full, value, unit=unit, method=how, params=report, piece_ids=pieces, inputs=(frame,), as_of_source=frame, warnings=warns)

    registry.register(full, module="indicators", summary=f"{summary} Lag: {_lag_text(lag)}.", inputs=inputs, piece_ids=tuple(pieces))(wrapper)
    LAGS[full] = lag
    return wrapper

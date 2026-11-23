"""Catalog of 13 technical indicators with their use and cautions (SPEC-019: TA-74).

indicators.catalog_value computes one catalog indicator at the last bar of daily bars and returns its use and cautions next to
the number. The catalog is also in the description of the function (quant_describe shows the choices). Windows are the SWC and SWO
daily windows by default; the DT line uses short windows and the LP line long ones (indicators._common.LINE_SCALE).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from hokma.quant import registry
from hokma.quant.indicators import momentum, overlap, volatility  # noqa: F401  (importing them registers the indicators this catalog calls)
from hokma.quant.indicators._common import build, check_last_n, empty, scale_window
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

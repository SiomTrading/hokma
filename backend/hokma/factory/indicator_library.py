"""The indicator library of the factory: the functions the runner supplies to an alpha that asks for an indicator (SPEC-020: L-52, FM-20).

Every function here is the `indicators.*` function of H3 (hokma.quant.indicators) over the whole array of bars, so the number an alpha reads in a
backtest is the number the indicator module gives and nothing is calculated a second time. The H3 function is causal: its value at a bar does not
change when later bars are removed, and the strategy contract checks it. A bar with no value yet (the warm-up) is NaN. An answer that is not
about the warm-up (a bad parameter, bad bars) is an error, never a column of NaN.

A kind that reads the high, the low and the close takes the mapping of bar arrays (the source `bars` of an indicator request); the other
kinds take the array of one field.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from hokma.factory.strategy_errors import BadStrategy
from hokma.factory.studies import _h3
from hokma.quant import limits, registry

WARMUP_NOTES = ("latest value is undefined", "need at least")      # what H3 says when the bars are too few for the window: that is a warm-up


def _call(name: str, frame: pd.DataFrame, column: str, **params: Any) -> np.ndarray:
    import hokma.quant.indicators  # noqa: F401  (importing the package registers the indicators)

    limits.check_inputs({"bars": frame, **params})
    result = registry.describe(f"indicators.{name}").fn(frame, last_n=None, **params)        # type: ignore[misc]
    if not result.ok:
        note = "; ".join(result.warnings)
        if any(w in note for w in WARMUP_NOTES):
            return np.full(len(frame), np.nan)
        raise BadStrategy(f"indicators.{name}: {note}")
    values = np.asarray(result.value["values"][column], dtype=float)
    if values.shape != (len(frame),):
        raise BadStrategy(f"indicators.{name} gave {values.shape} values for {len(frame)} bars")
    return values


def ema(values: Any, length: int) -> np.ndarray:
    return _call("ema", pd.DataFrame({"close": np.asarray(values, dtype=float)}), "ema", window=int(length))


def sma(values: Any, length: int) -> np.ndarray:
    return _call("sma", pd.DataFrame({"close": np.asarray(values, dtype=float)}), "sma", window=int(length))


def rsi(values: Any, length: int) -> np.ndarray:
    return _call("rsi", pd.DataFrame({"close": np.asarray(values, dtype=float)}), "rsi", window=int(length))


def macd_line(values: Any, fast: int, slow: int) -> np.ndarray:
    return _call("macd", pd.DataFrame({"close": np.asarray(values, dtype=float)}), "macd", short_window=int(fast), long_window=int(slow))


def macd_signal(values: Any, fast: int, slow: int, signal: int) -> np.ndarray:
    return _call("macd", pd.DataFrame({"close": np.asarray(values, dtype=float)}), "signal", short_window=int(fast), long_window=int(slow),
                 signal_window=int(signal))


def _bars(bars: Mapping[str, Any]) -> pd.DataFrame:
    return pd.DataFrame({k: np.asarray(bars[k], dtype=float) for k in ("open", "high", "low", "close", "volume") if k in bars})


def adx(bars: Mapping[str, Any], length: int) -> np.ndarray:
    """The trend strength (0 to 100) with Wilder smoothing."""
    return _call("adx", _bars(bars), "adx", window=int(length))


def atr(bars: Mapping[str, Any], length: int) -> np.ndarray:
    """The average true range in price units."""
    return _call("atr", _bars(bars), "atr", window=int(length))


def ewma_vol(values: Any, decay_pct: int, min_bars: int = 20) -> np.ndarray:
    """The exponentially weighted volatility of the simple returns of the closes at each bar (`risk.ewma_volatility` of H3, with a decay of
    `decay_pct` percent: 94 is 0.94). Causal: the value at a bar uses the returns up to it. NaN until `min_bars` returns exist."""
    import hokma.quant.risk  # noqa: F401  (importing the package registers its functions)

    x = np.asarray(values, dtype=float)
    returns = x[1:] / x[:-1] - 1.0
    fn = registry.describe("risk.ewma_volatility").fn
    out = np.full(len(x), np.nan)
    for t in range(int(min_bars), len(returns) + 1):
        limits.check_inputs({"returns": returns[:t]})
        got = fn(returns=returns[:t], lambda_=int(decay_pct) / 100.0)             # type: ignore[misc]
        if got.ok and isinstance(got.value, (int, float)) and np.isfinite(got.value):
            out[t] = float(got.value)                                           # return t - 1 ends at bar t
    return out


def half_life(values: Any, length: int) -> np.ndarray:
    """The mean reversion half life, in bars, of the trailing `length` closes at each bar (the `stats.half_life` function of H3). NaN until `length`
    bars exist and where the window does not revert: the alpha that reads it gives no view there."""
    x = np.asarray(values, dtype=float)
    size = int(length)
    out = np.full(len(x), np.nan)
    for t in range(size - 1, len(x)):
        value = _h3.half_life(x[t - size + 1:t + 1])
        out[t] = np.nan if value is None else value
    return out


@dataclass(frozen=True)
class Wiring:
    """One more function of the library, made from a column of an `indicators.*` function of H3 (SPEC-032: C7). Nothing is calculated here: the
    function made from a row calls the same `_call` as the first nine and gives the column H3 gives."""

    name: str                  # the library name an alpha or a part asks for
    h3: str                    # "indicators.bollinger"
    column: str                # the column of result.value["values"]
    fields: tuple[str, ...]    # ("close",) or ("high", "low", "close"): the bar fields H3 reads
    params: Mapping[str, str]  # library parameter -> H3 parameter, e.g. {"length": "window"}; an H3 parameter that is not here keeps its H3 default


def _wired_frame(wiring: Wiring, source: Any) -> pd.DataFrame:
    if len(wiring.fields) == 1:
        return pd.DataFrame({wiring.fields[0]: np.asarray(source, dtype=float)})
    if not isinstance(source, Mapping):
        raise BadStrategy(f"{wiring.name} reads {', '.join(wiring.fields)}: its source is the mapping of bar arrays")
    missing = [f for f in wiring.fields if f not in source]
    if missing:
        raise BadStrategy(f"{wiring.name} needs the bar field(s) {', '.join(missing)}")
    return pd.DataFrame({f: np.asarray(source[f], dtype=float) for f in wiring.fields})


def _make(wiring: Wiring) -> Callable[..., np.ndarray]:
    short = wiring.h3.removeprefix("indicators.")

    def wired(source: Any, **params: Any) -> np.ndarray:
        unknown = sorted(set(params) - set(wiring.params))
        if unknown:
            raise BadStrategy(f"{wiring.name}: unknown parameter(s) {', '.join(unknown)}; it takes {', '.join(sorted(wiring.params)) or 'none'}")
        return _call(short, _wired_frame(wiring, source), wiring.column, **{wiring.params[k]: int(v) for k, v in params.items()})

    wired.__name__ = wiring.name
    wired.__doc__ = f"The column {wiring.column!r} of {wiring.h3} of H3 at each bar; NaN in the warm-up."
    return wired


# Every column of an `indicators.*` function of H3 that gives one value per bar from the bars of one instrument and passes the causality test of
# the strategy contract on the real bars of NQ (backend/tests/factory/test_indicator_wiring.py). The ones that do not pass stay out, with their reason.
WIRING: tuple[Wiring, ...] = (
    Wiring("accumulation_distribution", "indicators.accumulation_distribution", "accumulation_distribution", ("high", "low", "close", "volume"), {}),
    Wiring("adx_plus_di", "indicators.adx", "plus_di", ("high", "low", "close"), {"length": "window"}),
    Wiring("adx_minus_di", "indicators.adx", "minus_di", ("high", "low", "close"), {"length": "window"}),
    Wiring("aroon_up", "indicators.aroon", "up", ("high", "low"), {"length": "window"}),
    Wiring("aroon_down", "indicators.aroon", "down", ("high", "low"), {"length": "window"}),
    Wiring("aroon_oscillator", "indicators.aroon", "oscillator", ("high", "low"), {"length": "window"}),
    Wiring("awesome_oscillator", "indicators.awesome_oscillator", "awesome_oscillator", ("high", "low"), {"fast": "short_window", "slow": "long_window"}),
    Wiring("balance_of_power", "indicators.balance_of_power", "balance_of_power", ("open", "high", "low", "close"), {}),
    Wiring("bollinger_upper", "indicators.bollinger", "upper", ("close",), {"length": "window"}),
    Wiring("bollinger_middle", "indicators.bollinger", "middle", ("close",), {"length": "window"}),
    Wiring("bollinger_lower", "indicators.bollinger", "lower", ("close",), {"length": "window"}),
    Wiring("bollinger_percent_b", "indicators.bollinger", "percent_b", ("close",), {"length": "window"}),
    Wiring("bollinger_bandwidth", "indicators.bollinger", "bandwidth", ("close",), {"length": "window"}),
    Wiring("cci", "indicators.cci", "cci", ("high", "low", "close"), {"length": "window"}),
    Wiring("chaikin_money_flow", "indicators.chaikin_money_flow", "chaikin_money_flow", ("high", "low", "close", "volume"), {"length": "window"}),
    Wiring("chaikin_oscillator", "indicators.chaikin_oscillator", "chaikin_oscillator", ("high", "low", "close", "volume"), {"fast": "short_window", "slow": "long_window"}),
    Wiring("chande_momentum", "indicators.chande_momentum", "chande_momentum", ("close",), {"length": "window"}),
    Wiring("chandelier_exit_long_exit", "indicators.chandelier_exit", "long_exit", ("high", "low", "close"), {"length": "window"}),
    Wiring("chandelier_exit_short_exit", "indicators.chandelier_exit", "short_exit", ("high", "low", "close"), {"length": "window"}),
    Wiring("choppiness", "indicators.choppiness", "choppiness", ("high", "low", "close"), {"length": "window"}),
    Wiring("donchian_upper", "indicators.donchian", "upper", ("high", "low"), {"length": "window"}),
    Wiring("donchian_middle", "indicators.donchian", "middle", ("high", "low"), {"length": "window"}),
    Wiring("donchian_lower", "indicators.donchian", "lower", ("high", "low"), {"length": "window"}),
    Wiring("ease_of_movement", "indicators.ease_of_movement", "ease_of_movement", ("high", "low", "volume"), {"length": "window"}),
    Wiring("fibonacci_level_0_0", "indicators.fibonacci", "level_0_0", ("high", "low"), {"length": "window"}),
    Wiring("fibonacci_level_23_6", "indicators.fibonacci", "level_23_6", ("high", "low"), {"length": "window"}),
    Wiring("fibonacci_level_38_2", "indicators.fibonacci", "level_38_2", ("high", "low"), {"length": "window"}),
    Wiring("fibonacci_level_50_0", "indicators.fibonacci", "level_50_0", ("high", "low"), {"length": "window"}),
    Wiring("fibonacci_level_61_8", "indicators.fibonacci", "level_61_8", ("high", "low"), {"length": "window"}),
    Wiring("fibonacci_level_78_6", "indicators.fibonacci", "level_78_6", ("high", "low"), {"length": "window"}),
    Wiring("fibonacci_level_100_0", "indicators.fibonacci", "level_100_0", ("high", "low"), {"length": "window"}),
    Wiring("force_index", "indicators.force_index", "force_index", ("close", "volume"), {"length": "window"}),
    Wiring("hull", "indicators.hull", "hull", ("close",), {"length": "window"}),
    Wiring("ichimoku_conversion", "indicators.ichimoku", "conversion", ("high", "low", "close"), {"conversion_window": "conversion_window", "base_window": "base_window", "lead_span_b_window": "lead_span_b_window"}),
    Wiring("ichimoku_base", "indicators.ichimoku", "base", ("high", "low", "close"), {"conversion_window": "conversion_window", "base_window": "base_window", "lead_span_b_window": "lead_span_b_window"}),
    Wiring("ichimoku_span_a", "indicators.ichimoku", "span_a", ("high", "low", "close"), {"conversion_window": "conversion_window", "base_window": "base_window", "lead_span_b_window": "lead_span_b_window"}),
    Wiring("ichimoku_span_b", "indicators.ichimoku", "span_b", ("high", "low", "close"), {"conversion_window": "conversion_window", "base_window": "base_window", "lead_span_b_window": "lead_span_b_window"}),
    Wiring("ichimoku_lagging_close", "indicators.ichimoku", "lagging_close", ("high", "low", "close"), {"conversion_window": "conversion_window", "base_window": "base_window", "lead_span_b_window": "lead_span_b_window"}),
    Wiring("kama", "indicators.kama", "kama", ("close",), {"length": "window", "fast_window": "fast_window", "slow_window": "slow_window"}),
    Wiring("keltner_upper", "indicators.keltner", "upper", ("high", "low", "close"), {"length": "window", "atr_window": "atr_window"}),
    Wiring("keltner_middle", "indicators.keltner", "middle", ("high", "low", "close"), {"length": "window", "atr_window": "atr_window"}),
    Wiring("keltner_lower", "indicators.keltner", "lower", ("high", "low", "close"), {"length": "window", "atr_window": "atr_window"}),
    Wiring("macd_histogram", "indicators.macd", "histogram", ("close",), {"fast": "short_window", "slow": "long_window", "signal": "signal_window"}),
    Wiring("mfi", "indicators.mfi", "mfi", ("high", "low", "close", "volume"), {"length": "window"}),
    Wiring("negative_volume_index", "indicators.negative_volume_index", "negative_volume_index", ("close", "volume"), {}),
    Wiring("obv", "indicators.obv", "obv", ("close", "volume"), {}),
    Wiring("parabolic_sar_sar", "indicators.parabolic_sar", "sar", ("high", "low", "close"), {}),
    Wiring("parabolic_sar_trend", "indicators.parabolic_sar", "trend", ("high", "low", "close"), {}),
    Wiring("pivot_points_pivot", "indicators.pivot_points", "pivot", ("high", "low", "close"), {}),
    Wiring("pivot_points_r1", "indicators.pivot_points", "r1", ("high", "low", "close"), {}),
    Wiring("pivot_points_r2", "indicators.pivot_points", "r2", ("high", "low", "close"), {}),
    Wiring("pivot_points_r3", "indicators.pivot_points", "r3", ("high", "low", "close"), {}),
    Wiring("pivot_points_s1", "indicators.pivot_points", "s1", ("high", "low", "close"), {}),
    Wiring("pivot_points_s2", "indicators.pivot_points", "s2", ("high", "low", "close"), {}),
    Wiring("pivot_points_s3", "indicators.pivot_points", "s3", ("high", "low", "close"), {}),
    Wiring("positive_volume_index", "indicators.positive_volume_index", "positive_volume_index", ("close", "volume"), {}),
    Wiring("ppo", "indicators.ppo", "ppo", ("close",), {"fast": "short_window", "slow": "long_window"}),
    Wiring("price_volume_trend", "indicators.price_volume_trend", "price_volume_trend", ("close", "volume"), {}),
    Wiring("roc", "indicators.roc", "roc", ("close",), {"length": "window"}),
    Wiring("stochastic_k", "indicators.stochastic", "k", ("high", "low", "close"), {"length": "window", "smooth_window": "smooth_window"}),
    Wiring("stochastic_d", "indicators.stochastic", "d", ("high", "low", "close"), {"length": "window", "smooth_window": "smooth_window"}),
    Wiring("stochastic_rsi_k", "indicators.stochastic_rsi", "k", ("close",), {"length": "window", "stochastic_window": "stochastic_window", "smooth_window": "smooth_window"}),
    Wiring("stochastic_rsi_d", "indicators.stochastic_rsi", "d", ("close",), {"length": "window", "stochastic_window": "stochastic_window", "smooth_window": "smooth_window"}),
    Wiring("supertrend", "indicators.supertrend", "supertrend", ("high", "low", "close"), {"length": "window"}),
    Wiring("supertrend_direction", "indicators.supertrend", "direction", ("high", "low", "close"), {"length": "window"}),
    Wiring("support_resistance_resistance", "indicators.support_resistance", "resistance", ("close",), {"length": "window"}),
    Wiring("support_resistance_support", "indicators.support_resistance", "support", ("close",), {"length": "window"}),
    Wiring("true_range", "indicators.true_range", "true_range", ("high", "low", "close"), {}),
    Wiring("true_strength_index_tsi", "indicators.true_strength_index", "tsi", ("close",), {"slow": "long_window", "fast": "short_window", "signal": "signal_window"}),
    Wiring("true_strength_index_signal", "indicators.true_strength_index", "signal", ("close",), {"slow": "long_window", "fast": "short_window", "signal": "signal_window"}),
    Wiring("ultimate_oscillator", "indicators.ultimate_oscillator", "ultimate_oscillator", ("high", "low", "close"), {"window_1": "window_1", "window_2": "window_2", "window_3": "window_3"}),
    Wiring("volatility_cone_realised", "indicators.volatility_cone", "realised", ("close",), {"length": "window"}),
    Wiring("vortex_plus", "indicators.vortex", "plus", ("high", "low", "close"), {"length": "window"}),
    Wiring("vortex_minus", "indicators.vortex", "minus", ("high", "low", "close"), {"length": "window"}),
    Wiring("vwma", "indicators.vwma", "vwma", ("close", "volume"), {"length": "window"}),
    Wiring("williams_r", "indicators.williams_r", "williams_r", ("high", "low", "close"), {"length": "window"}),
    Wiring("wma", "indicators.wma", "wma", ("close",), {"length": "window"}),
)

_FIRST: Mapping[str, Callable[..., np.ndarray]] = {"ema": ema, "sma": sma, "rsi": rsi, "macd_line": macd_line, "macd_signal": macd_signal,
                                                  "adx": adx, "atr": atr, "half_life": half_life, "ewma_vol": ewma_vol}
if len({w.name for w in WIRING}) != len(WIRING) or {w.name for w in WIRING} & set(_FIRST):
    raise ValueError("a wiring repeats a name of the library")
LIBRARY: Mapping[str, Callable[..., np.ndarray]] = {**_FIRST, **{w.name: _make(w) for w in WIRING}}
BAR_KINDS = frozenset({"adx", "atr"} | {w.name for w in WIRING if len(w.fields) > 1})     # the kinds that read several fields: their request has the source `bars`

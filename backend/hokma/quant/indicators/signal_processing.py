"""Signal processing filters with a measured lag (SPEC-019: ECT-016).

Causal filters (they use only the bars up to the current one): Butterworth low pass, one sided Savitzky-Golay, wavelet
denoising over a trailing window with the universal threshold, and the dominant cycle of a trailing window. Moving averages
(EMA, KAMA, Hull) are built in the overlap file and measured here with measure_lag.

Two sided filters (Hodrick-Prescott and the centered Savitzky-Golay) use bars after the point they estimate, so they repaint
when new bars arrive. They are registered as research only: with mode="live" (the default) they return no value and a warning;
mode="research" runs them for offline study.

Units: the filters work on the series they receive, in its own units and bar spacing. A signal to noise ratio quoted per
period is not an annual ratio: this module never converts between them.
"""
from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np
import pandas as pd

from hokma.quant import helpers, limits, registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators import _lib
from hokma.quant.indicators._common import build, check_last_n, empty, series_value
from hokma.quant.types import QuantResult

_PIECE = ("ECT-016",)
RESEARCH_ONLY = frozenset({"indicators.hodrick_prescott", "indicators.savgol_centered"})
_UNIVERSAL_MAD = 0.6745
_VALUES = {"type": "series", "required": True}
_LAST_N = {"type": "int", "required": False, "default": 60}


# ---------------------------------------------------------------------------------------------- input
def _series(values: Any, *, min_len: int, label: str = "values") -> tuple[pd.Series | None, tuple[str, ...]]:
    series = helpers.as_series(values, name=label)
    if series.empty:
        return None, (f"{label}: no values",)
    if not np.isfinite(series.to_numpy()).all():
        return None, (f"{label}: NaN or infinite values",)
    if len(series) < min_len:
        return None, (f"{label}: need at least {min_len} value(s), got {len(series)}",)
    return series, ()


def _int(name: str, value: Any, low: int, high: int | None = None) -> int:
    n = limits.as_int(name, value)
    limits.check_param(name, n)
    if n < low or (high is not None and n > high):
        raise BadInput(f"{name} must be between {low} and {high if high is not None else 'the cap'}, got {n}")
    return n


# ---------------------------------------------------------------------------------------------- pure filters
def butterworth_filter(x: np.ndarray, cutoff_period: float, order: int) -> np.ndarray:
    """Causal low pass: forward filtering only, started at the first value (no zero start transient)."""
    b, a = _lib.sp_signal.butter(order, 2.0 / cutoff_period, btype="low")
    zi = _lib.sp_signal.lfilter_zi(b, a) * x[0]
    out, _ = _lib.sp_signal.lfilter(b, a, x, zi=zi)
    return out


def savgol_causal_filter(x: np.ndarray, window: int, polyorder: int) -> np.ndarray:
    """Polynomial fit of the trailing window evaluated at its last point (one sided). NaN until the first full window."""
    coeffs = _lib.sp_signal.savgol_coeffs(window, polyorder, pos=window - 1, use="dot")
    out = np.full(len(x), np.nan)
    if len(x) >= window:
        out[window - 1:] = np.lib.stride_tricks.sliding_window_view(x, window) @ coeffs
    return out


def _haar_forward(block: np.ndarray, level: int) -> tuple[np.ndarray, list[np.ndarray]]:
    approx, details = block, []
    for _ in range(level):
        even, odd = approx[:, 0::2], approx[:, 1::2]
        details.append((even - odd) / math.sqrt(2.0))
        approx = (even + odd) / math.sqrt(2.0)
    return approx, details


def _haar_inverse(approx: np.ndarray, details: list[np.ndarray]) -> np.ndarray:
    for detail in reversed(details):
        out = np.empty((approx.shape[0], approx.shape[1] * 2))
        out[:, 0::2] = (approx + detail) / math.sqrt(2.0)
        out[:, 1::2] = (approx - detail) / math.sqrt(2.0)
        approx = out
    return approx


def wavelet_denoise_filter(x: np.ndarray, window: int, level: int) -> np.ndarray:
    """Haar multiresolution over each trailing window; detail coefficients are soft thresholded with the universal threshold
    sigma * sqrt(2 ln window), sigma from the median absolute deviation of the finest details. The estimate at the last
    point of the window is kept, so the output at bar t never depends on a later bar."""
    out = np.full(len(x), np.nan)
    if len(x) < window:
        return out
    blocks = np.lib.stride_tricks.sliding_window_view(x, window).astype(float)
    approx, details = _haar_forward(blocks, level)
    sigma = np.median(np.abs(details[0]), axis=1) / _UNIVERSAL_MAD
    lam = (sigma * math.sqrt(2.0 * math.log(window)))[:, None]
    shrunk = [np.sign(d) * np.maximum(np.abs(d) - lam, 0.0) for d in details]
    out[window - 1:] = _haar_inverse(approx, shrunk)[:, -1]
    return out


def hodrick_prescott_trend(y: np.ndarray, smoothing: float) -> np.ndarray:
    """Trend of the Hodrick-Prescott filter: the solution of (I + smoothing * D'D) trend = y, D the second difference."""
    n = len(y)
    main = np.ones(n)
    off1 = np.zeros(n - 1)
    off2 = np.zeros(n - 2)
    k = np.arange(n - 2)
    main_extra = np.zeros(n)
    np.add.at(main_extra, k, 1.0)
    np.add.at(main_extra, k + 1, 4.0)
    np.add.at(main_extra, k + 2, 1.0)
    np.add.at(off1, k, -2.0)
    np.add.at(off1, k + 1, -2.0)
    off2[:] = 1.0
    ab = np.zeros((3, n))
    ab[2, :] = main + smoothing * main_extra
    ab[1, 1:] = smoothing * off1
    ab[0, 2:] = smoothing * off2
    return _lib.sp_linalg.solveh_banded(ab, y)


def savgol_centered_filter(x: np.ndarray, window: int, polyorder: int) -> np.ndarray:
    return _lib.sp_signal.savgol_filter(x, window, polyorder, mode="interp")


def dominant_period(x: np.ndarray) -> tuple[float, float]:
    """(period in bars, share of the spectral power) of the strongest cycle after removing a linear trend and tapering."""
    n = len(x)
    t = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(t, x, 1)
    resid = (x - (slope * t + intercept)) * np.hanning(n)
    power = np.abs(np.fft.rfft(resid)) ** 2
    freqs = np.fft.rfftfreq(n)
    use = (freqs > 0) & (freqs <= 0.5)
    power, freqs = power[use], freqs[use]
    total = float(power.sum())
    if total <= 0:
        return math.nan, math.nan
    k = int(np.argmax(power))
    return float(1.0 / freqs[k]), float(power[k] / total)


# ---------------------------------------------------------------------------------------------- filter registry for lag and cycle tests
def apply_filter(method: str, x: pd.Series, **p: Any) -> pd.Series:
    """Run a named causal filter on a Series. Used by measure_lag and cycle_check."""
    arr = x.to_numpy(dtype=float)
    o = _lib.ta_overlap
    if method == "sma":
        return o.get_moving_average(x, int(p.get("window", 20)))
    if method == "ema":
        return o.get_exponential_moving_average(x, int(p.get("window", 20)))
    if method == "wma":
        return o.get_weighted_moving_average(x, int(p.get("window", 20)))
    if method == "hull":
        return o.get_hull_moving_average(x, int(p.get("window", 20)))
    if method == "kama":
        return o.get_kaufman_adaptive_moving_average(x, int(p.get("window", 10)), int(p.get("fast_window", 2)), int(p.get("slow_window", 30)))
    if method == "butterworth":
        return pd.Series(butterworth_filter(arr, float(p.get("cutoff_period", 10.0)), int(p.get("order", 2))), index=x.index)
    if method == "savgol":
        return pd.Series(savgol_causal_filter(arr, int(p.get("window", 11)), int(p.get("polyorder", 2))), index=x.index)
    if method == "wavelet":
        return pd.Series(wavelet_denoise_filter(arr, int(p.get("window", 64)), int(p.get("level", 3))), index=x.index)
    raise BadInput(f"unknown method {method!r}; use one of {sorted(METHODS)}")


METHODS = frozenset({"sma", "ema", "wma", "hull", "kama", "butterworth", "savgol", "wavelet"})


def ramp_lag(method: str, n: int = 600, **p: Any) -> float:
    """Lag in bars of a filter on a unit ramp: the median over the last 50 bars of (input minus output)."""
    ramp = pd.Series(np.arange(n, dtype=float))
    out = apply_filter(method, ramp, **p)
    gap = (ramp - out).iloc[-50:]
    return float(gap.median())


# ---------------------------------------------------------------------------------------------- registered functions
def _run_filter(name: str, values: Any, last_n: Any, params: dict, min_len: int, unit: str, method: str,
                compute: Callable[[np.ndarray], np.ndarray]) -> QuantResult:
    keep = check_last_n(last_n)
    series, warns = _series(values, min_len=min_len)
    if series is None:
        return empty(name, unit, method, warns, params=params, piece_ids=_PIECE)
    try:
        out = pd.Series(compute(series.to_numpy(dtype=float)), index=series.index)
    except BadInput:
        raise
    except Exception as exc:
        return empty(name, unit, method, (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=_PIECE)
    value = series_value(out, "filtered", keep)
    if value is None:
        return empty(name, unit, method, "latest value is undefined: not enough values for the window", params=params, piece_ids=_PIECE)
    return build(name, value, unit=unit, method=method, params=params, piece_ids=_PIECE, inputs=(series,), as_of_source=series, warnings=warns)


@registry.register("indicators.butterworth", module="indicators",
                   summary="Causal Butterworth low pass filter (forward only, started at the first value); cutoff given as a period in bars above 2. It lags the input: measure it with measure_lag.",
                   inputs={"values": _VALUES, "cutoff_period": {"type": "float", "required": False, "default": 10.0},
                           "order": {"type": "int", "required": False, "default": 2}, "last_n": _LAST_N}, piece_ids=_PIECE)
def butterworth(values: Any, cutoff_period: float = 10.0, order: int = 2, last_n: int | None = 60) -> QuantResult:
    name, method = "indicators.butterworth", "butterworth_lfilter"
    order = _int("order", order, 1, 8)
    if isinstance(cutoff_period, bool) or not isinstance(cutoff_period, (int, float)):
        raise BadInput("cutoff_period must be a number")
    params = {"cutoff_period": float(cutoff_period), "order": order}
    if not math.isfinite(cutoff_period) or cutoff_period <= 2.0:
        return empty(name, "value", method, "cutoff_period must be above 2 bars (the Nyquist period)", params=params, piece_ids=_PIECE)
    shortest = max(2 * order + 2, math.ceil(cutoff_period))      # fewer values than one cutoff period say nothing about the filter
    return _run_filter(name, values, last_n, params, shortest, "value", method, lambda x: butterworth_filter(x, float(cutoff_period), order))


@registry.register("indicators.savgol_causal", module="indicators",
                   summary="One sided Savitzky-Golay filter: a polynomial fitted to the trailing window and evaluated at the current bar. Causal; its noise at the edge is larger than the centered filter.",
                   inputs={"values": _VALUES, "window": {"type": "int", "required": False, "default": 11},
                           "polyorder": {"type": "int", "required": False, "default": 2}, "last_n": _LAST_N}, piece_ids=_PIECE)
def savgol_causal(values: Any, window: int = 11, polyorder: int = 2, last_n: int | None = 60) -> QuantResult:
    name, method = "indicators.savgol_causal", "savitzky_golay_one_sided"
    window, polyorder = _int("window", window, 2), _int("polyorder", polyorder, 0)
    params = {"window": window, "polyorder": polyorder}
    if polyorder >= window:
        return empty(name, "value", method, "polyorder must be below window", params=params, piece_ids=_PIECE)
    return _run_filter(name, values, last_n, params, window, "value", method, lambda x: savgol_causal_filter(x, window, polyorder))


@registry.register("indicators.wavelet_denoise", module="indicators",
                   summary="Causal wavelet denoising: Haar multiresolution of each trailing window with the universal soft threshold; keeps the estimate of the current bar. Window must be a multiple of 2^level.",
                   inputs={"values": _VALUES, "window": {"type": "int", "required": False, "default": 64},
                           "level": {"type": "int", "required": False, "default": 3}, "last_n": _LAST_N}, piece_ids=_PIECE)
def wavelet_denoise(values: Any, window: int = 64, level: int = 3, last_n: int | None = 60) -> QuantResult:
    name, method = "indicators.wavelet_denoise", "haar_mra_universal_soft_threshold"
    window, level = _int("window", window, 2), _int("level", level, 1, 10)
    params = {"window": window, "level": level}
    if window % (2 ** level) != 0:
        return empty(name, "value", method, "window must be a multiple of 2^level", params=params, piece_ids=_PIECE)
    count = len(values) if hasattr(values, "__len__") else 0
    limits.check_product("wavelet cells", count, window)
    return _run_filter(name, values, last_n, params, window, "value", method, lambda x: wavelet_denoise_filter(x, window, level))


@registry.register("indicators.dominant_cycle", module="indicators",
                   summary="Strongest cycle of the last window bars (linear trend removed, Hann taper): its period in bars and its share of the spectral power. A peak in noise is also reported: check it with cycle_check.",
                   inputs={"values": _VALUES, "window": {"type": "int", "required": False, "default": 128}}, piece_ids=_PIECE)
def dominant_cycle(values: Any, window: int = 128) -> QuantResult:
    name, method = "indicators.dominant_cycle", "periodogram_hann"
    window = _int("window", window, 8)
    params = {"window": window}
    series, warns = _series(values, min_len=window)
    if series is None:
        return empty(name, "bars", method, warns, params=params, piece_ids=_PIECE)
    tail = series.iloc[-window:].to_numpy(dtype=float)
    if float(tail.max() - tail.min()) == 0.0:
        return empty(name, "bars", method, "constant series: no cycle", params=params, piece_ids=_PIECE)
    period, share = dominant_period(tail)
    return build(name, {"period": period, "power_share": share}, unit="bars", method=method, params=params, piece_ids=_PIECE,
                 inputs=(series,), as_of_source=series, warnings=warns)


@registry.register("indicators.measure_lag", module="indicators",
                   summary="Lag in bars that a causal filter adds on a trend (unit ramp), for sma, ema, wma, hull, kama, butterworth, savgol or wavelet. Report it next to any filtered value.",
                   inputs={"method": {"type": "str", "required": True}, "window": {"type": "int", "required": False, "default": 20},
                           "cutoff_period": {"type": "float", "required": False, "default": 10.0},
                           "order": {"type": "int", "required": False, "default": 2},
                           "polyorder": {"type": "int", "required": False, "default": 2},
                           "level": {"type": "int", "required": False, "default": 3},
                           "fast_window": {"type": "int", "required": False, "default": 2},
                           "slow_window": {"type": "int", "required": False, "default": 30}}, piece_ids=_PIECE)
def measure_lag(method: str, window: int = 20, cutoff_period: float = 10.0, order: int = 2, polyorder: int = 2, level: int = 3,
                fast_window: int = 2, slow_window: int = 30) -> QuantResult:
    name = "indicators.measure_lag"
    params = {"method": method, "window": window, "cutoff_period": cutoff_period, "order": order, "polyorder": polyorder,
              "level": level, "fast_window": fast_window, "slow_window": slow_window}
    if not isinstance(method, str) or method not in METHODS:
        return empty(name, "bars", "ramp_response", f"method must be one of {sorted(METHODS)}", params=params, piece_ids=_PIECE)
    window, order, polyorder, level = _int("window", window, 2), _int("order", order, 1, 8), _int("polyorder", polyorder, 0), _int("level", level, 1, 10)
    fast_window, slow_window = _int("fast_window", fast_window, 1), _int("slow_window", slow_window, 2)
    if isinstance(cutoff_period, bool) or not isinstance(cutoff_period, (int, float)) or not math.isfinite(cutoff_period) or cutoff_period <= 2.0:
        return empty(name, "bars", "ramp_response", "cutoff_period must be a number above 2", params=params, piece_ids=_PIECE)
    if method == "savgol" and polyorder >= window:
        return empty(name, "bars", "ramp_response", "polyorder must be below window", params=params, piece_ids=_PIECE)
    if method == "wavelet" and window % (2 ** level) != 0:
        return empty(name, "bars", "ramp_response", "window must be a multiple of 2^level", params=params, piece_ids=_PIECE)
    if method == "kama" and fast_window >= slow_window:
        return empty(name, "bars", "ramp_response", "fast_window must be below slow_window", params=params, piece_ids=_PIECE)
    n = max(600, 8 * window)
    limits.check_param("window", window)
    try:
        lag = ramp_lag(method, n, window=window, cutoff_period=float(cutoff_period), order=order, polyorder=polyorder, level=level,
                       fast_window=fast_window, slow_window=slow_window)
    except Exception as exc:
        return empty(name, "bars", "ramp_response", (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=_PIECE)
    return build(name, {"lag_bars": lag}, unit="bars", method="ramp_response", params=params, piece_ids=_PIECE, inputs=(), warnings=())


def _peak_ratio(filtered_diff: np.ndarray) -> float:
    power = np.abs(np.fft.rfft(filtered_diff - filtered_diff.mean())) ** 2
    power = power[1:]
    if power.size == 0 or float(power.mean()) <= 0:
        return math.nan
    return float(power.max() / power.mean())


@registry.register("indicators.cycle_check", module="indicators",
                   summary="Monte Carlo test that a filtered series shows a cycle beyond what the same filter makes out of random walks: p-value of the peak to mean spectral ratio of the filtered differences.",
                   inputs={"values": _VALUES, "method": {"type": "str", "required": False, "default": "ema"},
                           "window": {"type": "int", "required": False, "default": 20},
                           "cutoff_period": {"type": "float", "required": False, "default": 10.0},
                           "n_paths": {"type": "int", "required": False, "default": 200},
                           "seed": {"type": "int", "required": False, "default": 0},
                           "alpha": {"type": "float", "required": False, "default": 0.05}}, piece_ids=_PIECE)
def cycle_check(values: Any, method: str = "ema", window: int = 20, cutoff_period: float = 10.0, n_paths: int = 200,
                seed: int = 0, alpha: float = 0.05) -> QuantResult:
    name, how = "indicators.cycle_check", "monte_carlo_random_walk_null"
    n_paths = _int("n_paths", n_paths, 20)
    window, seed = _int("window", window, 2), limits.as_int("seed", seed)
    params = {"method": method, "window": window, "cutoff_period": cutoff_period, "n_paths": n_paths, "seed": seed, "alpha": alpha}
    if not isinstance(method, str) or method not in METHODS - {"wavelet"}:
        return empty(name, "prob", how, f"method must be one of {sorted(METHODS - {'wavelet'})}", params=params, piece_ids=_PIECE)
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0.0 < alpha < 1.0:
        return empty(name, "prob", how, "alpha must be strictly between 0 and 1", params=params, piece_ids=_PIECE)
    series, warns = _series(values, min_len=max(64, 4 * window))
    if series is None:
        return empty(name, "prob", how, warns, params=params, piece_ids=_PIECE)
    steps = series.diff().dropna().to_numpy()
    sigma = float(steps.std(ddof=1))
    if sigma == 0.0:
        return empty(name, "prob", how, "constant series: no cycle", params=params, piece_ids=_PIECE)
    limits.check_product("cycle_check cells", n_paths, len(series))
    kw = {"window": window, "cutoff_period": float(cutoff_period)}

    def stat(x: pd.Series) -> float:
        out = apply_filter(method, x, **kw).dropna().to_numpy()
        return _peak_ratio(np.diff(out)) if len(out) > 8 else math.nan

    observed = stat(series.reset_index(drop=True))
    rng = np.random.default_rng(seed)
    sims = []
    for _ in range(n_paths):
        path = pd.Series(np.cumsum(rng.normal(0.0, sigma, len(series))))
        sims.append(stat(path))
    sims_arr = np.array([s for s in sims if math.isfinite(s)])
    if not math.isfinite(observed) or sims_arr.size == 0:
        return empty(name, "prob", how, "the spectrum is undefined for this input", params=params, piece_ids=_PIECE)
    p_value = float((1 + (sims_arr >= observed).sum()) / (1 + sims_arr.size))
    value = {"statistic": observed, "null_q95": float(np.quantile(sims_arr, 0.95)), "p_value": p_value, "cycle_detected": bool(p_value < alpha)}
    return build(name, value, unit="prob", method=how, params=params, piece_ids=_PIECE, inputs=(series,), as_of_source=series, warnings=warns)


def _research_gate(name: str, mode: str, params: dict) -> QuantResult | None:
    if mode != "research":
        return empty(name, "value", "research_only", "research_only: this filter uses bars after the point it estimates and repaints; it is blocked for live runs (use mode='research' offline)",
                     params={**params, "mode": mode, "research_only": True}, piece_ids=_PIECE)
    return None


@registry.register("indicators.hodrick_prescott", module="indicators",
                   summary="research_only, two sided: Hodrick-Prescott trend. Repaints with new bars, so it is blocked unless mode='research'. The smoothing value has no universal source: 1600 is the quarterly convention; test the value you use.",
                   inputs={"values": _VALUES, "smoothing": {"type": "float", "required": False, "default": 1600.0},
                           "mode": {"type": "str", "required": False, "default": "live"}, "last_n": _LAST_N}, piece_ids=_PIECE)
def hodrick_prescott(values: Any, smoothing: float = 1600.0, mode: str = "live", last_n: int | None = 60) -> QuantResult:
    name, method = "indicators.hodrick_prescott", "hodrick_prescott_banded"
    params = {"smoothing": smoothing}
    blocked = _research_gate(name, mode, params)
    if blocked is not None:
        return blocked
    if isinstance(smoothing, bool) or not isinstance(smoothing, (int, float)) or not math.isfinite(smoothing) or smoothing <= 0:
        return empty(name, "value", method, "smoothing must be a positive number", params={**params, "mode": mode, "research_only": True}, piece_ids=_PIECE)
    result = _run_filter(name, values, last_n, {**params, "mode": mode, "research_only": True}, 3, "value", method,
                         lambda x: hodrick_prescott_trend(x, float(smoothing)))
    return result


@registry.register("indicators.savgol_centered", module="indicators",
                   summary="research_only, two sided: centered Savitzky-Golay smoothing. Repaints with new bars, so it is blocked unless mode='research'.",
                   inputs={"values": _VALUES, "window": {"type": "int", "required": False, "default": 11},
                           "polyorder": {"type": "int", "required": False, "default": 2},
                           "mode": {"type": "str", "required": False, "default": "live"}, "last_n": _LAST_N}, piece_ids=_PIECE)
def savgol_centered(values: Any, window: int = 11, polyorder: int = 2, mode: str = "live", last_n: int | None = 60) -> QuantResult:
    name, method = "indicators.savgol_centered", "savitzky_golay_centered"
    params = {"window": window, "polyorder": polyorder}
    blocked = _research_gate(name, mode, params)
    if blocked is not None:
        return blocked
    window, polyorder = _int("window", window, 2), _int("polyorder", polyorder, 0)
    report = {"window": window, "polyorder": polyorder, "mode": mode, "research_only": True}
    if polyorder >= window or window % 2 == 0:
        return empty(name, "value", method, "window must be odd and above polyorder", params=report, piece_ids=_PIECE)
    return _run_filter(name, values, last_n, report, window, "value", method, lambda x: savgol_centered_filter(x, window, polyorder))

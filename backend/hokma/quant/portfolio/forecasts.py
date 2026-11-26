"""Scale, cap and combination of forecasts (P-09).

A forecast is a signed number whose size says how strong the view is. Rules that live on different scales are put on one scale so that
the average absolute forecast is `target_abs` (10 by default), and no forecast is above `cap` in absolute value (20 by default).
Scaled forecasts of several rules are combined with weights that sum to one; because rules that disagree partly cancel, the combination
is multiplied by a diversification multiplier that restores the average size. The multiplier is 1 over the square root of w'Hw, where H
is the correlation of the forecasts with negative correlations set to zero; it is never below 1 and never above `max_multiplier`.
"""
from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _common as c
from hokma.quant.types import QuantResult

DEFAULT_TARGET_ABS = 10.0
DEFAULT_CAP = 20.0
DEFAULT_MAX_MULTIPLIER = 2.5
MAX_RULES = 200
_SERIES = {"type": "series", "required": True}


def _forecast_series(value: Any) -> pd.Series:
    if isinstance(value, pd.Series):
        series = value
    elif isinstance(value, (list, tuple, np.ndarray)):
        series = pd.Series(np.asarray(value, dtype=float).ravel())
    else:
        raise BadInput(f"forecast: expected a Series or a list, got {type(value).__name__}")
    try:
        return series.astype(float).replace([np.inf, -np.inf], np.nan)
    except (TypeError, ValueError) as exc:
        raise BadInput("forecast: values are not numeric") from exc


@registry.register("portfolio.forecast_scale_cap", module="portfolio",
                   summary="Scale a forecast so its average absolute value is target_abs (10), then cap it at plus or minus cap (20). Returns the scalar and the capped series.",
                   inputs={"forecast": _SERIES, "target_abs": {"type": "float", "required": False, "default": DEFAULT_TARGET_ABS},
                           "cap": {"type": "float", "required": False, "default": DEFAULT_CAP}},
                   piece_ids=("P-09",))
def forecast_scale_cap(forecast: Any, target_abs: float = DEFAULT_TARGET_ABS, cap: float = DEFAULT_CAP) -> QuantResult:
    name, pieces = "portfolio.forecast_scale_cap", ("P-09",)
    params = {"target_abs": target_abs, "cap": cap}

    def compute() -> Any:
        target = c.number("target_abs", target_abs, low=0.0, low_open=True)
        limit = c.number("cap", cap, low=0.0, low_open=True)
        series = _forecast_series(forecast)
        values = series.to_numpy()
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            raise c.Domain("the forecast has no finite value")
        mean_abs = float(np.abs(finite).mean())
        if mean_abs == 0.0:
            raise c.Domain("the average absolute forecast is zero: there is nothing to scale")
        scalar = target / mean_abs
        scaled = values * scalar
        capped = np.clip(scaled, -limit, limit)
        return {"scalar": scalar, "mean_abs": mean_abs, "forecast": c.jsonable(capped), "n_capped": int(np.sum(np.abs(scaled[np.isfinite(scaled)]) > limit))}

    return c.run(name, unit="forecast", method="scalar = target_abs / mean(|forecast|); capped at plus or minus cap", params=params,
                 piece_ids=pieces, compute=compute, inputs=(forecast, params))


def _weights(weights: Any, columns: pd.Index) -> np.ndarray:
    """Weights per column, normalized to sum to one. None is equal weights; a missing column has weight zero."""
    if weights is None:
        return np.full(len(columns), 1.0 / len(columns))
    if not isinstance(weights, Mapping):
        raise BadInput("weights must be a dict column -> weight")
    names = {str(col): col for col in columns}
    unknown = [k for k in weights if str(k) not in names]
    if unknown:
        raise BadInput(f"weights: unknown column(s) {unknown}")
    raw = np.array([c.number(f"weights[{col}]", weights.get(col, weights.get(str(col), 0.0)), low=0.0) for col in columns])
    total = float(raw.sum())
    if total <= 0.0:
        raise c.Domain("the weights sum to zero")
    return raw / total


def _diversification_multiplier(frame: pd.DataFrame, w: np.ndarray, max_multiplier: float) -> float:
    """1 / sqrt(w'Hw) with H the correlation of the columns, negative and missing correlations set to zero; at least 1, at most the cap."""
    if frame.shape[1] == 1:
        return 1.0
    corr = frame.corr().to_numpy(dtype=float)
    corr = np.where(np.isfinite(corr), corr, 0.0)
    corr = np.clip(corr, 0.0, 1.0)
    np.fill_diagonal(corr, 1.0)
    variance = float(w @ corr @ w)
    return min(max(1.0 / np.sqrt(variance), 1.0), max_multiplier)


@registry.register("portfolio.forecast_combine", module="portfolio",
                   summary="Combine scaled forecasts (columns are rules) with weights that sum to one, times a diversification multiplier (given, or from the correlations), capped at plus or minus cap.",
                   inputs={"forecasts": {"type": "dataframe", "required": True}, "weights": {"type": "dict", "required": False},
                           "diversification_multiplier": {"type": "float", "required": False},
                           "cap": {"type": "float", "required": False, "default": DEFAULT_CAP},
                           "max_multiplier": {"type": "float", "required": False, "default": DEFAULT_MAX_MULTIPLIER}},
                   piece_ids=("P-09",))
def forecast_combine(forecasts: Any, weights: Any = None, diversification_multiplier: Any = None, cap: float = DEFAULT_CAP,
                     max_multiplier: float = DEFAULT_MAX_MULTIPLIER) -> QuantResult:
    name, pieces = "portfolio.forecast_combine", ("P-09",)
    params = {"weights": weights, "diversification_multiplier": diversification_multiplier, "cap": cap, "max_multiplier": max_multiplier}

    def compute() -> Any:
        limit = c.number("cap", cap, low=0.0, low_open=True)
        top = c.number("max_multiplier", max_multiplier, low=1.0)
        frame = c.as_panel(forecasts, name="forecasts").replace([np.inf, -np.inf], np.nan)
        if frame.shape[1] > MAX_RULES:
            raise BadInput(f"more than {MAX_RULES} forecast columns")
        if frame.shape[0] == 0:
            raise c.Domain("forecasts is empty")
        w = _weights(weights, frame.columns)
        if diversification_multiplier is None:
            dm = _diversification_multiplier(frame, w, top)
        else:
            dm = c.number("diversification_multiplier", diversification_multiplier, low=1.0)
        values = frame.to_numpy()
        present = np.isfinite(values)
        # a rule with no value at a date drops out of that date and the rest share its weight
        usable = np.where(present, w, 0.0)
        totals = usable.sum(axis=1)
        mixed = np.where(totals > 0.0, np.where(present, values, 0.0) @ w / np.where(totals > 0.0, totals, 1.0), np.nan)
        combined = np.clip(mixed * dm, -limit, limit)
        return {"combined": c.jsonable(combined), "weights": c.weights_to_dict(w, frame.columns), "diversification_multiplier": dm,
                "n_capped": int(np.sum(np.abs(mixed[np.isfinite(mixed)] * dm) > limit))}

    return c.run(name, unit="forecast", method="sum of weight times forecast, times the diversification multiplier, capped", params=params,
                 piece_ids=pieces, compute=compute, inputs=(forecasts, params))

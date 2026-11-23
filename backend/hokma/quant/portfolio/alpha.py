"""Alpha forecasts (SK-085 to SK-089): units, three ways to turn descriptors into a forecast, and the evaluation of a forecast.

The inputs are wide frames named as panel fields (dates by assets) and descriptors named as in the fundamentals module.
A forecast is expressed in expected return units; `idio_sharpe` forecasts are multiplied by the idiosyncratic volatility.
"""
from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import descriptors as fdesc
from hokma.quant.portfolio import _common as c
from hokma.quant.portfolio import _lib
from hokma.quant.portfolio import _panel as pn
from hokma.quant.types import QuantResult

MIN_OBS = 20
MAX_ESTIMATORS = 10
KINDS = ("fixed_weighted", "ew_sharpe_optimal", "predictor")
ALPHA_CLASSES = {"fixed_weighted": "FixedWeightedAlpha", "ew_sharpe_optimal": "EWSharpeOptimalAlpha", "predictor": "PredictorAlpha"}
_FIELDS = {"type": "dict", "required": True}
_COMMON = {
    "fields": _FIELDS, "descriptors": {"type": "list", "required": True}, "active": {"type": "dataframe", "required": False},
    "outlier": {"type": "str", "required": False, "default": "winsorizer"}, "scoring": {"type": "str", "required": False, "default": "standard"},
}
ALLOWED = {
    "fixed_weighted": {"descriptors", "forecast_scale", "weights", "forecast_unit", "min_coverage", "outlier", "scoring"},
    "ew_sharpe_optimal": {"descriptors", "half_life", "ridge_scale", "horizon", "signal_lag", "forecast_unit", "forecast_scale",
                          "normalize_weights", "outlier", "scoring"},
    "predictor": {"descriptors", "predictor", "predictor_alpha", "horizon", "signal_lag", "half_life", "forecast_unit",
                  "calibrate_to_return_units", "forecast_scale", "outlier", "scoring"},
}
REQUIRED_FIELDS = {"fixed_weighted": (), "ew_sharpe_optimal": ("idio_returns", "idio_variances"), "predictor": ("idio_returns",)}


def _unit(value: Any) -> Any:
    return _lib.FORECAST_UNITS[c.choice("forecast_unit", value, tuple(_lib.FORECAST_UNITS))]


def build_estimator(spec: Mapping[str, Any], fields: Mapping[str, Any]) -> Any:
    """Library alpha estimator from {"kind": ..., ...}. Unknown keys raise BadInput; a missing panel field raises Domain."""
    if not isinstance(spec, Mapping):
        raise BadInput("an estimator spec must be a dict")
    kind = c.choice("kind", spec.get("kind"), KINDS)
    extra = set(spec) - ALLOWED[kind] - {"kind"}
    if extra:
        raise BadInput(f"{kind}: unknown key(s) {sorted(extra)}")
    missing = [f for f in REQUIRED_FIELDS[kind] if f not in fields]
    if missing:
        raise c.Domain(f"data_missing:{','.join(missing)} ({kind})")
    descs = pn.descriptor_list(spec.get("descriptors"), fields)
    common = {"descriptors": descs, "outlier_transformer": pn.transformer(spec.get("outlier", "winsorizer"), pn.OUTLIERS, "outlier"),
              "scoring_transformer": pn.transformer(spec.get("scoring", "standard"), pn.SCORERS, "scoring")}
    unit = _unit(spec.get("forecast_unit", "idio_return"))
    if unit == _lib.ForecastUnit.IDIO_SHARPE and "idio_variances" not in fields:
        raise c.Domain("data_missing:idio_variances (forecast_unit idio_sharpe)")
    scale = c.number("forecast_scale", spec.get("forecast_scale", 1.0))
    if kind == "fixed_weighted":
        if "forecast_scale" not in spec:
            raise c.Domain("fixed_weighted needs forecast_scale")
        weights = spec.get("weights")
        if weights is not None:
            if not isinstance(weights, (list, tuple)) or len(weights) != len(descs):
                raise BadInput("weights needs one value per descriptor")
            weights = np.array([c.number("weight", w) for w in weights])
        cov = c.number("min_coverage", spec.get("min_coverage", 0.0), low=0.0, high=1.0)
        return _lib.sk_alpha.FixedWeightedAlpha(forecast_scale=scale, weights=weights, forecast_unit=unit, min_coverage=cov, **common)
    horizon = int(c.number("horizon", spec.get("horizon", 1), low=1, integer=True))
    lag = int(c.number("signal_lag", spec.get("signal_lag", 1), low=0, integer=True))
    half_life = c.number("half_life", spec.get("half_life", 20.0), low=0.0, high=pn.MAX_HALF_LIFE, low_open=True)
    if kind == "ew_sharpe_optimal":
        return _lib.sk_alpha.EWSharpeOptimalAlpha(
            half_life=half_life, ridge_scale=c.number("ridge_scale", spec.get("ridge_scale", 1e-6), low=0.0), horizon=horizon, signal_lag=lag,
            forecast_unit=unit, forecast_scale=scale, normalize_weights=bool(spec.get("normalize_weights", True)), **common)
    name = c.choice("predictor", spec.get("predictor", "ridge"), tuple(_lib.PREDICTORS))
    model = _lib.PREDICTORS[name]() if name == "linear" else _lib.PREDICTORS[name](alpha=c.number("predictor_alpha", spec.get("predictor_alpha", 1.0), low=0.0))
    return _lib.sk_alpha.PredictorAlpha(predictor=model, horizon=horizon, signal_lag=lag, half_life=half_life, forecast_unit=unit, forecast_scale=scale,
                                        calibrate_to_return_units=bool(spec.get("calibrate_to_return_units", True)), **common)


def _panel(fields: Any, active: Any, spec: Any) -> tuple[Any, pd.DataFrame, pd.DataFrame, tuple[str, ...]]:
    numeric, mask, ref, notes = pn.sk_fields(fields, active)
    if len(ref) < MIN_OBS:
        raise c.Domain(f"need at least {MIN_OBS} rows, got {len(ref)}")
    return numeric, mask, ref, notes


def _fit_alpha(kind: str, name: str, pieces: tuple[str, ...], fields: Any, active: Any, spec: dict[str, Any], params: dict) -> QuantResult:
    def compute() -> Any:
        numeric, mask, ref, notes = _panel(fields, active, spec)
        est = build_estimator({"kind": kind, **spec}, numeric)
        sk = _lib.sk_panel(numeric, mask)
        _lib.quiet(est.fit, sk)
        alpha = np.asarray(est.alpha_, dtype=float)
        if not np.isfinite(alpha).any():
            raise c.Domain("the alpha is not finite for any asset (not enough history or coverage)")
        out: dict[str, Any] = {"alpha": pn.finite_vector(alpha, ref.columns), "n_observations": int(len(ref)),
                               "as_of": c.jsonable(ref.index[-1]), "descriptors": list(est.named_descriptors_)}
        if hasattr(est, "coef_"):
            out["coef"] = {n: c.jsonable(float(v)) for n, v in zip(out["descriptors"], np.asarray(est.coef_, dtype=float).ravel())}
        if hasattr(est, "descriptor_weights_"):
            out["descriptor_weights"] = {n: c.jsonable(float(v)) for n, v in zip(out["descriptors"], np.asarray(est.descriptor_weights_, dtype=float).ravel())}
        if hasattr(est, "composite_score_"):
            out["composite_score"] = pn.finite_vector(np.asarray(est.composite_score_, dtype=float)[-1], ref.columns)
        return out, notes

    return c.run(name, unit="return", method=ALPHA_CLASSES[kind], params=params, piece_ids=pieces, compute=compute, inputs=(fields, params))


@registry.register("portfolio.alpha_base", module="portfolio",
                   summary="Forecast units and the alpha estimators with the panel fields each one needs; converts a forecast to expected return units.",
                   inputs={"forecast": {"type": "dict", "required": False}, "unit": {"type": "str", "required": False, "default": "idio_return"},
                           "idio_variances": {"type": "dict", "required": False}},
                   piece_ids=("SK-085",))
def base(forecast: Any = None, unit: str = "idio_return", idio_variances: Any = None) -> QuantResult:
    name, pieces = "portfolio.alpha_base", ("SK-085",)
    params = {"unit": unit}

    def compute() -> Any:
        which = c.choice("unit", unit, tuple(_lib.FORECAST_UNITS))
        out: dict[str, Any] = {
            "units": {"idio_return": "Forecast is the idiosyncratic return: used as it is.",
                      "idio_sharpe": "Forecast is the idiosyncratic Sharpe: multiplied by the idiosyncratic volatility to get a return."},
            "estimators": {k: {"class": cls, "extends_base": bool(issubclass(getattr(_lib.sk_alpha, cls), _lib.sk_alpha.BaseAlpha)),
                               "needs_fields": list(REQUIRED_FIELDS[k])} for k, cls in ALPHA_CLASSES.items()},
            "descriptors": sorted(fdesc.BY_NAME),
        }
        if forecast is not None:
            if not isinstance(forecast, Mapping) or not forecast:
                raise c.Domain("forecast must be a non-empty dict asset -> value")
            values = {str(k): c.number(f"forecast[{k}]", v) for k, v in forecast.items()}
            if _lib.FORECAST_UNITS[which] == _lib.ForecastUnit.IDIO_SHARPE:
                if not isinstance(idio_variances, Mapping) or set(map(str, idio_variances)) != set(values):
                    raise c.Domain("idio_sharpe needs idio_variances for exactly the assets of the forecast")
                out["alpha"] = {k: values[k] * float(np.sqrt(c.number(f"idio_variances[{k}]", idio_variances[k], low=0.0))) for k in values}
            else:
                out["alpha"] = values
        return out

    return c.run(name, unit="catalog", method="BaseAlpha", params=params, piece_ids=pieces, compute=compute, inputs=(forecast, idio_variances, params))


@registry.register("portfolio.alpha_ew_sharpe_optimal", module="portfolio",
                   summary="Alpha that combines descriptors with exponentially weighted Sharpe-optimal coefficients estimated from past idiosyncratic returns.",
                   inputs={**_COMMON, "half_life": {"type": "float", "required": False, "default": 20.0}, "ridge_scale": {"type": "float", "required": False, "default": 1e-6},
                           "horizon": {"type": "int", "required": False, "default": 1}, "signal_lag": {"type": "int", "required": False, "default": 1},
                           "forecast_unit": {"type": "str", "required": False, "default": "idio_return"},
                           "forecast_scale": {"type": "float", "required": False, "default": 1.0}},
                   piece_ids=("SK-086",))
def ew_sharpe_optimal(fields: Any, descriptors: Any, active: Any = None, outlier: str = "winsorizer", scoring: str = "standard",
                      half_life: float = 20.0, ridge_scale: float = 1e-6, horizon: int = 1, signal_lag: int = 1,
                      forecast_unit: str = "idio_return", forecast_scale: float = 1.0) -> QuantResult:
    spec = {"descriptors": descriptors, "outlier": outlier, "scoring": scoring, "half_life": half_life, "ridge_scale": ridge_scale,
            "horizon": horizon, "signal_lag": signal_lag, "forecast_unit": forecast_unit, "forecast_scale": forecast_scale}
    return _fit_alpha("ew_sharpe_optimal", "portfolio.alpha_ew_sharpe_optimal", ("SK-086",), fields, active, spec,
                      {k: v for k, v in spec.items() if k != "descriptors"})


@registry.register("portfolio.alpha_fixed_weighted", module="portfolio",
                   summary="Alpha from descriptors with fixed signed weights and a forecast scale; no coefficient is estimated.",
                   inputs={**_COMMON, "forecast_scale": {"type": "float", "required": True}, "weights": {"type": "list", "required": False},
                           "forecast_unit": {"type": "str", "required": False, "default": "idio_return"},
                           "min_coverage": {"type": "float", "required": False, "default": 0.0}},
                   piece_ids=("SK-087",))
def fixed_weighted(fields: Any, descriptors: Any, forecast_scale: float, active: Any = None, weights: Any = None, outlier: str = "winsorizer",
                   scoring: str = "standard", forecast_unit: str = "idio_return", min_coverage: float = 0.0) -> QuantResult:
    spec: dict[str, Any] = {"descriptors": descriptors, "outlier": outlier, "scoring": scoring, "forecast_scale": forecast_scale,
                            "forecast_unit": forecast_unit, "min_coverage": min_coverage}
    if weights is not None:
        spec["weights"] = weights
    return _fit_alpha("fixed_weighted", "portfolio.alpha_fixed_weighted", ("SK-087",), fields, active, spec,
                      {k: v for k, v in spec.items() if k != "descriptors"})


@registry.register("portfolio.alpha_predictor", module="portfolio",
                   summary="Alpha from a predictor (ridge, linear or lasso regression) trained on descriptors to forecast idiosyncratic returns.",
                   inputs={**_COMMON, "predictor": {"type": "str", "required": False, "default": "ridge"},
                           "predictor_alpha": {"type": "float", "required": False, "default": 1.0},
                           "horizon": {"type": "int", "required": False, "default": 1}, "signal_lag": {"type": "int", "required": False, "default": 1},
                           "half_life": {"type": "float", "required": False, "default": 20.0},
                           "forecast_unit": {"type": "str", "required": False, "default": "idio_return"},
                           "calibrate_to_return_units": {"type": "bool", "required": False, "default": True},
                           "forecast_scale": {"type": "float", "required": False, "default": 1.0}},
                   piece_ids=("SK-088",))
def predictor(fields: Any, descriptors: Any, active: Any = None, outlier: str = "winsorizer", scoring: str = "standard",
              predictor: str = "ridge", predictor_alpha: float = 1.0, horizon: int = 1, signal_lag: int = 1, half_life: float = 20.0,
              forecast_unit: str = "idio_return", calibrate_to_return_units: bool = True, forecast_scale: float = 1.0) -> QuantResult:
    spec = {"descriptors": descriptors, "outlier": outlier, "scoring": scoring, "predictor": predictor, "predictor_alpha": predictor_alpha,
            "horizon": horizon, "signal_lag": signal_lag, "half_life": half_life, "forecast_unit": forecast_unit,
            "calibrate_to_return_units": calibrate_to_return_units, "forecast_scale": forecast_scale}
    return _fit_alpha("predictor", "portfolio.alpha_predictor", ("SK-088",), fields, active, spec,
                      {k: v for k, v in spec.items() if k != "descriptors"})


def _table(frame: Any) -> dict[str, dict[str, float | None]]:
    """A summary DataFrame as {row: {column: value}}."""
    return {str(r): {str(col): c.jsonable(frame.loc[r, col]) for col in frame.columns} for r in frame.index}


@registry.register("portfolio.alpha_evaluation", module="portfolio",
                   summary="Evaluate alpha estimators against what happened next: IC (Spearman and Pearson), portfolio returns, calibration; several estimators are compared.",
                   inputs={"fields": _FIELDS, "estimators": {"type": "dict", "required": True}, "active": {"type": "dataframe", "required": False},
                           "target": {"type": "str", "required": False, "default": "idio_returns"},
                           "holding_period": {"type": "int", "required": False, "default": 1}, "signal_lag": {"type": "int", "required": False, "default": 1},
                           "evaluation_step": {"type": "int", "required": False}, "n_forward_periods": {"type": "int", "required": False, "default": 10},
                           "min_count": {"type": "int", "required": False, "default": 3}},
                   piece_ids=("SK-089",))
def evaluation(fields: Any, estimators: Any, active: Any = None, target: str = "idio_returns", holding_period: int = 1, signal_lag: int = 1,
               evaluation_step: int | None = None, n_forward_periods: int = 10, min_count: int = 3) -> QuantResult:
    name, pieces = "portfolio.alpha_evaluation", ("SK-089",)
    params = {"target": target, "holding_period": holding_period, "signal_lag": signal_lag, "evaluation_step": evaluation_step,
              "n_forward_periods": n_forward_periods, "min_count": min_count}

    def compute() -> Any:
        if not isinstance(estimators, Mapping) or not estimators or len(estimators) > MAX_ESTIMATORS:
            raise BadInput(f"estimators must be a dict name -> spec with 1 to {MAX_ESTIMATORS} entries")
        if not isinstance(target, str) or not target:
            raise BadInput("target must be the name of a field")
        hp = int(c.number("holding_period", holding_period, low=1, integer=True))
        lag = int(c.number("signal_lag", signal_lag, low=0, integer=True))
        step = None if evaluation_step is None else int(c.number("evaluation_step", evaluation_step, low=1, integer=True))
        nf = int(c.number("n_forward_periods", n_forward_periods, low=1, integer=True))
        mc = int(c.number("min_count", min_count, low=1, integer=True))
        numeric, mask, ref, notes = _panel(fields, active, None)
        if target not in numeric:
            raise c.Domain(f"data_missing:{target} (target)")
        sk = _lib.sk_panel(numeric, mask)
        evals: dict[str, Any] = {}
        for label, spec in estimators.items():
            est = build_estimator(spec, numeric)
            evals[str(label)] = _lib.quiet(_lib.sk_alpha.alpha_forecast_evaluation, est, sk, target=target, holding_period=hp, signal_lag=lag,
                                           evaluation_step=step, n_forward_periods=nf, min_count=mc, name=str(label))
        out: dict[str, Any] = {"ic": {n: _table(e.ic_summary()) for n, e in evals.items()},
                               "portfolio": {n: _table(e.portfolio_summary()) for n, e in evals.items()},
                               "calibration": {n: c.jsonable(e.calibration_summary()) if not hasattr(e.calibration_summary(), "columns")
                                               else _table(e.calibration_summary()) for n, e in evals.items()}}
        if len(evals) > 1:
            comp = _lib.sk_alpha.AlphaForecastComparison(list(evals.values()), names=list(evals))
            out["comparison"] = {"ic": _table(comp.ic_summary()), "portfolio": _table(comp.portfolio_summary())}
        return out, notes

    return c.run(name, unit="correlation", method="AlphaForecastEvaluation", params=params, piece_ids=pieces, compute=compute, inputs=(fields, params))

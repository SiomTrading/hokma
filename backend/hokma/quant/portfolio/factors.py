"""Factor models (SK-058, SK-059, SK-060).

`FactorModel` is the container of a fitted model: loadings, factor premia and covariance, idiosyncratic variances. `time_series`
estimates loadings by regressing asset returns on factor returns; `characteristics` builds the exposures of each asset from
descriptors (for example to choose stocks after an earnings surprise) and estimates the factor returns cross-section by cross-section.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _common as c
from hokma.quant.portfolio import _lib
from hokma.quant.portfolio import _panel as pn
from hokma.quant.types import QuantResult

_RET = {"type": "dataframe", "required": True}
MIN_OBS = 10
MAX_MATRIX_ASSETS = 100
REGRESSORS = ("ols", "ridge", "lasso")


@dataclass(frozen=True)
class FactorModel:
    """Result of a factor model: asset = loadings x factors + idiosyncratic part (SK-058)."""

    asset_names: tuple[str, ...]
    factor_names: tuple[str, ...]
    loading_matrix: np.ndarray                 # assets x factors
    factor_mu: np.ndarray                      # factors
    factor_covariance: np.ndarray              # factors x factors
    idio_variances: np.ndarray | None = None   # assets
    intercepts: np.ndarray | None = None       # assets

    def __post_init__(self) -> None:
        n, k = len(self.asset_names), len(self.factor_names)
        if self.loading_matrix.shape != (n, k) or self.factor_mu.shape != (k,) or self.factor_covariance.shape != (k, k):
            raise BadInput("the factor model parts do not have matching shapes")
        if self.idio_variances is not None and self.idio_variances.shape != (n,):
            raise BadInput("idio_variances needs one value per asset")
        if self.intercepts is not None and self.intercepts.shape != (n,):
            raise BadInput("intercepts needs one value per asset")
        if self.idio_variances is not None and np.any(self.idio_variances < 0):
            raise c.Domain("idiosyncratic variances must not be negative")
        eig = np.linalg.eigvalsh((self.factor_covariance + self.factor_covariance.T) / 2.0)
        if eig.min() < -1e-12:
            raise c.Domain("the factor covariance is not positive semidefinite")
        # The container of the library validates the same parts and is what the rest of the module consumes.
        object.__setattr__(self, "_library", _lib.SkFactorModel(
            observations=np.arange(1), asset_names=np.asarray(self.asset_names), factor_names=np.asarray(self.factor_names), factor_families=None,
            loading_matrix=self.loading_matrix, exposures=None, factor_covariance=self.factor_covariance, factor_mu=self.factor_mu,
            factor_returns=None, idio_covariance=np.diag(self.idio_variances if self.idio_variances is not None else np.zeros(n)),
            idio_mu=None, idio_returns=None, idio_variances=self.idio_variances))

    @staticmethod
    def from_library(fm: Any, intercepts: Any = None) -> "FactorModel":
        raw = np.asarray(fm.idio_covariance, dtype=float)
        idio = np.diag(raw).copy() if raw.ndim == 2 else raw.copy()
        return FactorModel(tuple(map(str, fm.asset_names)), tuple(map(str, fm.factor_names)), np.asarray(fm.loading_matrix, dtype=float),
                           np.asarray(fm.factor_mu, dtype=float), np.asarray(fm.factor_covariance, dtype=float), idio,
                           None if intercepts is None else np.asarray(intercepts, dtype=float).ravel())

    def implied_mu(self) -> np.ndarray:
        base = self.loading_matrix @ self.factor_mu
        return base if self.intercepts is None else base + self.intercepts

    def implied_covariance(self) -> np.ndarray:
        cov = self.loading_matrix @ self.factor_covariance @ self.loading_matrix.T
        return cov if self.idio_variances is None else cov + np.diag(self.idio_variances)

    def to_dict(self) -> dict[str, Any]:
        assets, factors = pd.Index(self.asset_names), pd.Index(self.factor_names)
        out: dict[str, Any] = {
            "loadings": {a: {f: c.jsonable(float(self.loading_matrix[i, j])) for j, f in enumerate(self.factor_names)} for i, a in enumerate(self.asset_names)},
            "factor_mu": c.weights_to_dict(self.factor_mu, factors), "factor_covariance": c.matrix_to_dict(self.factor_covariance, factors),
            "idio_variances": None if self.idio_variances is None else c.weights_to_dict(self.idio_variances, assets),
            "implied_mu": c.weights_to_dict(self.implied_mu(), assets),
        }
        if len(self.asset_names) <= MAX_MATRIX_ASSETS:
            out["implied_covariance"] = c.matrix_to_dict(self.implied_covariance(), assets)
        return out


def _nested(label: str, value: Any, rows: list[str], cols: list[str]) -> np.ndarray:
    if not isinstance(value, Mapping) or set(map(str, value)) != set(rows):
        raise BadInput(f"{label}: expected a dict with exactly the keys {rows}")
    out = np.zeros((len(rows), len(cols)))
    for i, r in enumerate(rows):
        row = value.get(r, value.get(r))
        if not isinstance(row, Mapping) or set(map(str, row)) != set(cols):
            raise BadInput(f"{label}[{r}]: expected a dict with exactly the keys {cols}")
        for j, col in enumerate(cols):
            out[i, j] = c.number(f"{label}[{r}][{col}]", row[col])
    return out


def _vector(label: str, value: Any, keys: list[str]) -> np.ndarray:
    if not isinstance(value, Mapping) or set(map(str, value)) != set(keys):
        raise BadInput(f"{label}: expected a dict with exactly the keys {keys}")
    return np.array([c.number(f"{label}[{k}]", value[k]) for k in keys])


@registry.register("portfolio.factor_model", module="portfolio",
                   summary="Factor model container from given parts (loadings, factor premia, factor covariance, idiosyncratic variances): implied mean and covariance.",
                   inputs={"loadings": {"type": "dict", "required": True}, "factor_mu": {"type": "dict", "required": True},
                           "factor_covariance": {"type": "dict", "required": True}, "idio_variances": {"type": "dict", "required": False},
                           "intercepts": {"type": "dict", "required": False}},
                   piece_ids=("SK-058",))
def factor_model(loadings: Any, factor_mu: Any, factor_covariance: Any, idio_variances: Any = None, intercepts: Any = None) -> QuantResult:
    name, pieces = "portfolio.factor_model", ("SK-058",)
    params = {"n_assets": len(loadings) if isinstance(loadings, Mapping) else None}

    def compute() -> Any:
        if not isinstance(loadings, Mapping) or not loadings or not isinstance(factor_mu, Mapping) or not factor_mu:
            raise c.Domain("loadings and factor_mu must be non-empty dicts")
        assets, factors = [str(a) for a in loadings], [str(f) for f in factor_mu]
        if len(assets) > 5_000 or len(factors) > pn.MAX_FACTORS:
            raise BadInput("too many assets or factors")
        fm = FactorModel(tuple(assets), tuple(factors), _nested("loadings", loadings, assets, factors), _vector("factor_mu", factor_mu, factors),
                         _nested("factor_covariance", factor_covariance, factors, factors),
                         None if idio_variances is None else _vector("idio_variances", idio_variances, assets),
                         None if intercepts is None else _vector("intercepts", intercepts, assets))
        return fm.to_dict()

    return c.run(name, unit="return", method="FactorModel", params=params, piece_ids=pieces, compute=compute,
                 inputs=(loadings, factor_mu, factor_covariance, idio_variances, intercepts))


def _regressor(kind: str, alpha: float) -> Any:
    which = c.choice("regressor", kind, REGRESSORS)
    if which == "ols":
        return _lib.sk_linear_model.LinearRegression()
    strength = c.number("alpha", alpha, low=0.0)
    return (_lib.sk_linear_model.Ridge if which == "ridge" else _lib.sk_linear_model.Lasso)(alpha=strength)


def _aligned(returns: Any, factors: Any) -> tuple[pd.DataFrame, pd.DataFrame, tuple[str, ...]]:
    x = c.as_panel(returns, name="returns", min_cols=1)
    f = c.as_panel(factors, name="factors", min_cols=1)
    if isinstance(x.index, pd.DatetimeIndex) and isinstance(f.index, pd.DatetimeIndex):
        joined = pd.concat([x, f.add_prefix("__factor__")], axis=1, join="inner")
    elif len(x) == len(f):
        joined = pd.concat([x.reset_index(drop=True), f.reset_index(drop=True).add_prefix("__factor__")], axis=1)
    else:
        raise BadInput(f"returns and factors must have the same length, got {len(x)} and {len(f)}")
    joined = joined.replace([np.inf, -np.inf], np.nan)
    kept = joined.dropna()
    warns = (f"dropped {len(joined) - len(kept)} row(s) with NaN or infinite values",) if len(kept) != len(joined) else ()
    xs = kept[[col for col in kept.columns if not str(col).startswith("__factor__")]]
    fs = kept[[col for col in kept.columns if str(col).startswith("__factor__")]]
    fs = fs.rename(columns=lambda col: str(col)[len("__factor__"):])
    return xs, fs, warns


@registry.register("portfolio.time_series_factor_model", module="portfolio",
                   summary="Factors by regression of series: loadings of each asset on the factor returns (ols, ridge or lasso), factor premia, residual variances.",
                   inputs={"returns": _RET, "factors": {"type": "dataframe", "required": True},
                           "regressor": {"type": "str", "required": False, "default": "ols"},
                           "alpha": {"type": "float", "required": False, "default": 1.0}},
                   piece_ids=("SK-059",))
def time_series(returns: Any, factors: Any, regressor: str = "ols", alpha: float = 1.0) -> QuantResult:
    name, pieces = "portfolio.time_series_factor_model", ("SK-059",)
    params = {"regressor": regressor, "alpha": alpha}

    def compute() -> Any:
        reg = _regressor(regressor, alpha)
        xs, fs, warns = _aligned(returns, factors)
        if len(xs) < max(MIN_OBS, fs.shape[1] + 2):
            raise c.Domain(f"need at least {max(MIN_OBS, fs.shape[1] + 2)} complete rows, got {len(xs)}")
        est = _lib.sk_prior.TimeSeriesFactorModel(loading_matrix_estimator=_lib.sk_prior.LoadingMatrixRegression(linear_regressor=reg))
        fitted = _lib.quiet(est.fit, xs, factors=fs)
        rd = fitted.return_distribution_
        intercepts = np.asarray(fitted.loading_matrix_estimator_.intercepts_, dtype=float)
        model = FactorModel.from_library(rd.factor_model, intercepts)
        out = model.to_dict()
        out["mu"] = c.weights_to_dict(rd.mu, xs.columns)
        out["n_observations"] = int(len(xs))
        return out, warns

    return c.run(name, unit="return", method="TimeSeriesFactorModel", params=params, piece_ids=pieces, compute=compute, inputs=(returns, factors, params))


def factor_estimators(specs: Any, fields: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """Factors as [{"name": "value", "family": "style", "descriptors": [...], "weights": [...]}] to library estimators."""
    if not isinstance(specs, (list, tuple)) or not specs or len(specs) > pn.MAX_FACTORS:
        raise BadInput(f"factors must be a list of 1 to {pn.MAX_FACTORS} factor dicts")
    out: list[tuple[str, Any]] = []
    for i, spec in enumerate(specs):
        if not isinstance(spec, Mapping) or set(spec) - {"name", "family", "descriptors", "weights", "outlier", "scoring"}:
            raise BadInput(f"factors[{i}]: expected a dict with name, family, descriptors, weights, outlier and scoring")
        name = spec.get("name")
        if not isinstance(name, str) or not name or len(name) > 40:
            raise BadInput(f"factors[{i}]: name must be a short text")
        family = spec.get("family", "style")
        if not isinstance(family, str) or not family or len(family) > 40:
            raise BadInput(f"factors[{i}]: family must be a short text")
        descs = pn.descriptor_list(spec.get("descriptors"), fields, label=f"factors[{i}].descriptors")
        weights = spec.get("weights")
        if weights is not None:
            if not isinstance(weights, (list, tuple)) or len(weights) != len(descs):
                raise BadInput(f"factors[{i}]: weights needs one value per descriptor")
            weights = np.array([c.number("weight", w, low=0.0) for w in weights])
            if abs(float(weights.sum()) - 1.0) > 1e-9:
                raise c.Domain(f"factors[{i}]: weights must sum to 1, got {float(weights.sum())}")
        est = _lib.sk_factor_exposure.FixedWeightedFactor(
            descriptors=descs, family=family, weights=weights,
            outlier_transformer=pn.transformer(spec.get("outlier", "winsorizer"), pn.OUTLIERS, "outlier"),
            scoring_transformer=pn.transformer(spec.get("scoring", "standard"), pn.SCORERS, "scoring"))
        out.append((name, est))
    return out


@registry.register("portfolio.characteristics_factor_model", module="portfolio",
                   summary="Factors from asset characteristics: exposures from descriptors, factor returns by cross-section; for example to choose stocks after earnings.",
                   inputs={"fields": {"type": "dict", "required": True}, "factors": {"type": "list", "required": True},
                           "active": {"type": "dataframe", "required": False}},
                   piece_ids=("SK-060",))
def characteristics(fields: Any, factors: Any, active: Any = None) -> QuantResult:
    name, pieces = "portfolio.characteristics_factor_model", ("SK-060",)
    params = {"n_factors": len(factors) if isinstance(factors, (list, tuple)) else None}

    def compute() -> Any:
        numeric, mask, ref, notes = pn.sk_fields(fields, active)
        for needed in ("returns", "market_cap"):
            if needed not in numeric:
                raise c.Domain(f"data_missing:{needed}")
        if len(ref) < MIN_OBS:
            raise c.Domain(f"need at least {MIN_OBS} rows, got {len(ref)}")
        est = _lib.sk_prior.CharacteristicsFactorModel(factors=factor_estimators(factors, numeric))
        fitted = _lib.quiet(est.fit, None, characteristics=_lib.sk_panel(numeric, mask))
        rd = fitted.return_distribution_
        model = FactorModel.from_library(fitted.factor_model_)
        out = model.to_dict()
        out["mu"] = c.weights_to_dict(rd.mu, ref.columns)
        out["n_observations"] = int(len(ref))
        return out, notes

    return c.run(name, unit="return", method="CharacteristicsFactorModel", params=params, piece_ids=pieces, compute=compute, inputs=(fields, factors))

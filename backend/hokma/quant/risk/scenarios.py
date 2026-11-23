"""Scenarios and stress: synthetic returns from a fitted dependence model, factor family constraints and explicit shocks."""
from __future__ import annotations

from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.risk import _common as c
from hokma.quant.risk import _lib
from hokma.quant.types import QuantResult


@registry.register("risk.synthetic_scenarios", module="risk",
                   summary="Draw synthetic return scenarios from a vine copula fitted to a returns panel, optionally conditioned on bounds or fixed values of some assets, with portfolio VaR and CVaR when weights are given.",
                   inputs={"panel": {"type": "dataframe", "required": True}, "n_samples": {"type": "int", "required": False, "default": 1000},
                           "weights": {"type": "dict", "required": False, "default": None},
                           "level": {"type": "float", "required": False, "default": 0.95},
                           "conditioning": {"type": "dict", "required": False, "default": None},
                           "seed": {"type": "int", "required": False, "default": 0}},
                   piece_ids=("SK-061",))
def synthetic_scenarios(panel: Any, n_samples: int = 1000, weights: Any = None, level: float = 0.95,
                        conditioning: dict | None = None, seed: int = 0) -> QuantResult:
    name, unit, method, pieces = "risk.synthetic_scenarios", "return", "vine_copula_sampling", ("SK-061",)
    from hokma.quant import limits

    limits.check_param("n_samples", n_samples)
    params = {"n_samples": n_samples, "level": level, "seed": seed, "conditioning": conditioning}
    if isinstance(n_samples, bool) or not isinstance(n_samples, (int, np.integer)):
        raise BadInput(f"n_samples must be an integer, got {type(n_samples).__name__}")
    if conditioning is not None and not isinstance(conditioning, dict):
        raise BadInput(f"conditioning: expected a dict, got {type(conditioning).__name__}")
    problem = c.level_warning(level)
    if problem or n_samples < 10:
        return c.empty(name, unit, method, problem or "n_samples must be at least 10", params=params, piece_ids=pieces)
    frame = c.as_panel(panel)
    w = c.weights_series(weights, frame.columns) if weights is not None else None
    unknown = [k for k in (conditioning or {}) if k not in set(frame.columns)]
    if unknown:
        raise BadInput(f"conditioning: unknown asset(s) {unknown}")
    cleaned, warns = c.clean_panel(frame, min_obs=30)
    if not c.panel_ok(cleaned, warns, 30):
        return c.empty(name, unit, method, warns or ("degenerate input",), params=params, piece_ids=pieces)
    try:
        vine = _lib.sk_dist.VineCopula(random_state=seed, central_assets=list(conditioning) if conditioning else None)
        prior = _lib.sk_prior.SyntheticData(distribution_estimator=vine, n_samples=int(n_samples),
                                            sample_args={"conditioning": conditioning} if conditioning else None).fit(cleaned)
        sims = prior.return_distribution_.returns
    except Exception as exc:
        return c.empty(name, unit, method, warns + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=pieces)
    value: dict[str, Any] = {
        "assets": [str(a) for a in cleaned.columns], "n_samples": int(sims.shape[0]), "mean": sims.mean(axis=0),
        "std": sims.std(axis=0, ddof=1), "min": sims.min(axis=0), "max": sims.max(axis=0), "correlation": np.corrcoef(sims, rowvar=False),
    }
    if w is not None:
        port = sims @ w.to_numpy()
        value["portfolio"] = {"var": _lib.sk_measures.value_at_risk(port, beta=level), "cvar": _lib.sk_measures.cvar(port, beta=level),
                              "worst": _lib.sk_measures.worst_realization(port)}
    return c.build(name, value, unit=unit, method=method, params=params, piece_ids=pieces, inputs=(cleaned,),
                   as_of_source=cleaned.iloc[:, 0], warnings=warns)


@registry.register("risk.family_constraint_basis", module="risk",
                   summary="Reduce factor exposures to a full rank basis by benchmark weighted zero sum constraints inside factor families; reports the dropped factors and the constraint ratios.",
                   inputs={"constrained_families": {"type": "list", "required": True}, "factor_exposures": {"type": "array", "required": True},
                           "benchmark_weights": {"type": "array", "required": True}, "factor_names": {"type": "list", "required": True},
                           "factor_families": {"type": "list", "required": True}},
                   piece_ids=("SK-063",))
def family_constraint_basis(constrained_families: Any, factor_exposures: Any, benchmark_weights: Any, factor_names: Any,
                            factor_families: Any) -> QuantResult:
    name, unit, method, pieces = "risk.family_constraint_basis", "basis", "benchmark_zero_sum", ("SK-063",)
    try:
        exposures = np.asarray(factor_exposures, dtype=float)
        bench = np.asarray(benchmark_weights, dtype=float)
        names = np.asarray(factor_names, dtype=str)
        families = np.asarray(factor_families, dtype=str)
        pairs = [(str(f), None if d is None else str(d)) for f, d in constrained_families]
    except (TypeError, ValueError) as exc:
        raise BadInput(f"family constraint inputs are malformed: {exc}") from exc
    if exposures.ndim != 3 or bench.ndim != 2 or names.ndim != 1 or families.ndim != 1:
        raise BadInput("factor_exposures must be 3D, benchmark_weights 2D, factor_names and factor_families 1D")
    params = {"constrained_families": pairs}
    if not (np.isfinite(bench).all() and bench.sum() > 0):
        return c.empty(name, unit, method, "benchmark_weights must be finite with a positive sum", params=params, piece_ids=pieces)
    try:
        basis, resolved = _lib.compute_family_constraint_basis(
            constrained_families=pairs, factor_exposures=exposures, benchmark_weights=bench, factor_names=names, factor_families=families)
    except Exception as exc:
        return c.empty(name, unit, method, (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=pieces)
    value = {
        "n_observations": basis.n_observations, "n_constraints": basis.n_constraints, "n_reduced_factors": basis.n_reduced_factors,
        "resolved_constraints": [list(p) for p in resolved], "dropped_factors": [str(names[i]) for i in basis.dropped_full_indices],
        "reduced_factor_names": basis.reduced_factor_names(names), "constraint_ratios": basis.constraint_ratios,
        "families": [{"family_name": f.family_name, "full_factor_indices": f.full_factor_indices,
                      "dropped_index_in_family": f.dropped_index_in_family} for f in basis.family_constraints],
    }
    return c.build(name, value, unit=unit, method=method, params=params, piece_ids=pieces, inputs=(exposures.tolist(), bench.tolist()))


def _nearest_correlation(matrix: np.ndarray) -> np.ndarray:
    """Clip negative eigenvalues and rescale to a unit diagonal."""
    vals, vecs = np.linalg.eigh((matrix + matrix.T) / 2.0)
    fixed = vecs @ np.diag(np.clip(vals, 1e-10, None)) @ vecs.T
    d = np.sqrt(np.diag(fixed))
    return fixed / np.outer(d, d)


@registry.register("risk.stress", module="risk",
                   summary="Stress a weighted portfolio. scenario keys: shocks (asset to instant return), vol_multiplier and corr_shift (stressed Gaussian VaR), level. Reports the shock P&L and base against stressed volatility and VaR.",
                   inputs={"panel": {"type": "dataframe", "required": True}, "weights": {"type": "dict", "required": True},
                           "scenario": {"type": "dict", "required": True}},
                   piece_ids=("SK-061",))
def stress(panel: pd.DataFrame, weights: dict[str, float], *, scenario: dict) -> QuantResult:
    name, unit, method, pieces = "risk.stress", "return", "scenario_stress", ("SK-061",)
    if not isinstance(scenario, dict):
        raise BadInput(f"scenario: expected a dict, got {type(scenario).__name__}")
    known = {"shocks", "vol_multiplier", "corr_shift", "level"}
    extra = sorted(set(scenario) - known)
    params = {"scenario": scenario}
    if extra:
        return c.empty(name, unit, method, f"unknown scenario key(s) {extra}; use {sorted(known)}", params=params, piece_ids=pieces)
    if not ({"shocks", "vol_multiplier", "corr_shift"} & set(scenario)):
        return c.empty(name, unit, method, "scenario needs shocks, vol_multiplier or corr_shift", params=params, piece_ids=pieces)
    frame = c.as_panel(panel)
    w = c.weights_series(weights, frame.columns)
    level = scenario.get("level", 0.95)
    problem = c.level_warning(level)
    if problem:
        return c.empty(name, unit, method, problem, params=params, piece_ids=pieces)
    shocks = scenario.get("shocks")
    if shocks is not None:
        if not isinstance(shocks, dict):
            raise BadInput(f"shocks: expected a dict, got {type(shocks).__name__}")
        unknown = [k for k in shocks if k not in set(frame.columns)]
        if unknown:
            raise BadInput(f"shocks: unknown asset(s) {unknown}")
    cleaned, warns = c.clean_panel(frame, min_obs=30)
    if not c.panel_ok(cleaned, warns, 30):
        return c.empty(name, unit, method, warns or ("degenerate input",), params=params, piece_ids=pieces)
    mult = float(scenario.get("vol_multiplier", 1.0))
    shift = float(scenario.get("corr_shift", 0.0))
    if mult < 0:
        return c.empty(name, unit, method, "vol_multiplier must not be negative", params=params, piece_ids=pieces)
    mu = cleaned.mean().to_numpy()
    sd = cleaned.std(ddof=1).to_numpy()
    corr = np.corrcoef(cleaned.to_numpy(), rowvar=False)
    stressed_corr = corr
    if shift:
        stressed_corr = corr + shift * (1.0 - np.eye(len(corr)))
        stressed_corr = _nearest_correlation(np.clip(stressed_corr, -1.0, 1.0))
    wv = w.to_numpy()

    def port(sigmas: np.ndarray, rho: np.ndarray) -> tuple[float, float]:
        sigma_p = float(np.sqrt(wv @ (np.outer(sigmas, sigmas) * rho) @ wv))
        return sigma_p, float(-(wv @ mu) + NormalDist().inv_cdf(level) * sigma_p)

    base_vol, base_var = port(sd, corr)
    stressed_vol, stressed_var = port(sd * mult, stressed_corr)
    value: dict[str, Any] = {"base_volatility": base_vol, "base_var": base_var, "stressed_volatility": stressed_vol,
                             "stressed_var": stressed_var, "level": level}
    if shocks is not None:
        value["shock_return"] = float(sum(wv[list(frame.columns).index(k)] * float(v) for k, v in shocks.items()))
    return c.build(name, value, unit=unit, method=method, params=params, piece_ids=pieces, inputs=(cleaned, w.to_dict()),
                   as_of_source=cleaned.iloc[:, 0], warnings=warns)

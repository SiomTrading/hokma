"""Residual edge: does a strategy keep a return after the exposure to declared baselines is removed?

SPEC-017 row CTS-038. Least squares of the strategy return on declared baseline returns with an intercept, Newey-West
(Bartlett) standard errors, annualized alpha, residual edge ratio (annualized alpha over annualized residual volatility),
rolling stability, sensitivity to alternate baselines and a breakdown by regimes declared beforehand.

States: RESIDUAL_EDGE, BASELINE_EXPLAINED, RESIDUAL_FRAGILE, INSUFFICIENT_EVIDENCE. Any missing declaration lowers the
decision eligibility to REVIEW_REQUIRED. The function never changes sizing and never confirms alpha from in-sample data.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import helpers, registry
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult
from hokma.quant.validation import _lib

ANNUALIZATION = {"daily": 252, "weekly": 52, "monthly": 12}
ROLLING_WINDOW = {"daily": 63, "weekly": 52, "monthly": 24}
DEFAULT_THRESHOLDS = {
    "baseline_explained_r2_min": 0.75, "weak_edge_ratio_max": 0.50, "residual_edge_ratio_min": 0.75,
    "alpha_t_stat_min": 2.0, "rolling_positive_fraction_min": 0.60,
}
DECLARATION_VALUES = {
    "baseline_selection": {"predeclared"},
    "strategy_return_basis": {"gross", "net"},
    "baseline_return_basis": {"gross", "net"},
    "analysis_scope": {"out_of_sample", "live", "in_sample"},
    "universe_data": {"point_in_time", "current_constituents", "not_applicable"},
}
MIN_ROLLING_WINDOWS = 12
VIF_LIMIT = 10.0


def default_hac_lags(n: int) -> int:
    return max(0, int(math.floor(4 * (n / 100.0) ** (2.0 / 9.0))))


def fit(strategy: np.ndarray, factors: np.ndarray, *, hac_lags: int | None = None) -> dict:
    """OLS with intercept and Newey-West covariance. Index 0 of beta is the per period alpha."""
    n, m = factors.shape
    design = np.column_stack([np.ones(n), factors])
    k = m + 1
    if n <= k:
        raise ValueError("not enough observations for the number of baselines")
    xtx = design.T @ design
    if np.linalg.matrix_rank(xtx) < k:
        raise ValueError("baseline returns are perfectly collinear or constant")
    inv = np.linalg.inv(xtx)
    beta = inv @ (design.T @ strategy)
    resid = strategy - design @ beta
    lags = default_hac_lags(n) if hac_lags is None else int(hac_lags)
    lags = min(max(0, lags), n - 1)
    scores = design * resid[:, None]
    meat = scores.T @ scores
    for lag in range(1, lags + 1):
        cross = scores[lag:].T @ scores[:-lag]
        meat += (1.0 - lag / (lags + 1.0)) * (cross + cross.T)
    meat *= n / max(1, n - k)
    cov = inv @ meat @ inv
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    ssr = float(resid @ resid)
    centered = strategy - strategy.mean()
    tss = float(centered @ centered)
    return {"beta": beta, "se": se, "resid": resid, "n": n, "k": k, "hac_lags": lags,
            "r2": (1.0 - ssr / tss) if tss > 0 else float("nan")}


def _vif(factors: np.ndarray, names: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    if factors.shape[1] < 2:
        return out
    for i, col in enumerate(names):
        others = np.column_stack([np.ones(len(factors)), np.delete(factors, i, axis=1)])
        coef, *_ = np.linalg.lstsq(others, factors[:, i], rcond=None)
        resid = factors[:, i] - others @ coef
        centered = factors[:, i] - factors[:, i].mean()
        tss = float(centered @ centered)
        r2 = 1.0 - float(resid @ resid) / tss if tss > 0 else 1.0
        out[col] = float("inf") if r2 >= 1.0 else 1.0 / (1.0 - r2)
    return out


def _model(strategy: pd.Series, baselines: pd.DataFrame, names: list[str], ann: int, window: int, thresholds: dict,
           hac_lags: int | None) -> dict:
    factors = baselines[names].to_numpy(dtype=float)
    y = strategy.to_numpy(dtype=float)
    res = fit(y, factors, hac_lags=hac_lags)
    alpha = float(res["beta"][0])
    t_stat = float(alpha / res["se"][0]) if res["se"][0] > 0 else None
    resid_vol = float(res["resid"].std(ddof=1) * math.sqrt(ann))
    ann_alpha = alpha * ann
    edge_ratio = ann_alpha / resid_vol if resid_vol > 0 else None
    resid = res["resid"]
    rho1 = float(np.corrcoef(resid[:-1], resid[1:])[0, 1]) if len(resid) > 2 and resid.std() > 0 else None
    alphas, failed = [], 0
    if len(y) >= window + 1:
        for start in range(0, len(y) - window + 1):
            try:
                alphas.append(float(fit(y[start:start + window], factors[start:start + window], hac_lags=hac_lags)["beta"][0] * ann))
            except (ValueError, np.linalg.LinAlgError):
                failed += 1
    n_windows = len(alphas) + failed
    rolling = {"enabled": n_windows >= MIN_ROLLING_WINDOWS, "window": window, "n_windows": n_windows, "failed_window_count": failed,
               "positive_alpha_fraction": (sum(a > 0 for a in alphas) / len(alphas)) if alphas else None}
    if not rolling["enabled"]:
        rolling["reason"] = f"fewer than {MIN_ROLLING_WINDOWS} rolling windows"
    model = {
        "baselines": list(names), "n": res["n"], "hac_lags": res["hac_lags"], "r_squared": res["r2"],
        "betas": {n: float(b) for n, b in zip(names, res["beta"][1:])},
        "alpha": {"per_period": alpha, "annualized": ann_alpha, "t_stat": t_stat,
                  "p_value": _lib.t_sf(t_stat, res["n"] - res["k"]) if t_stat is not None else None},
        "residual": {"annualized_volatility": resid_vol, "residual_edge_ratio": edge_ratio, "lag1_autocorrelation": rho1},
        "vif": _vif(factors, list(names)), "rolling": rolling,
    }
    model["status"], model["reasons"] = _classify(model, thresholds)
    return model


def _classify(model: dict, th: dict) -> tuple[str, list[str]]:
    t_stat, ratio, rolling = model["alpha"]["t_stat"], model["residual"]["residual_edge_ratio"], model["rolling"]
    if not rolling["enabled"]:
        return "RESIDUAL_FRAGILE", [f"rolling stability is unavailable: {rolling['reason']}"]
    if rolling["failed_window_count"] > 0:
        return "RESIDUAL_FRAGILE", [f"{rolling['failed_window_count']} rolling window(s) could not be estimated"]
    rolling_pass = (rolling["positive_alpha_fraction"] or 0.0) >= th["rolling_positive_fraction_min"]
    if ratio is not None and t_stat is not None and ratio >= th["residual_edge_ratio_min"] and t_stat >= th["alpha_t_stat_min"] and rolling_pass:
        return "RESIDUAL_EDGE", ["alpha, residual edge ratio and rolling stability clear the thresholds"]
    if (model["r_squared"] >= th["baseline_explained_r2_min"] and (ratio is None or abs(ratio) <= th["weak_edge_ratio_max"])
            and (t_stat is None or abs(t_stat) < th["alpha_t_stat_min"])):
        return "BASELINE_EXPLAINED", ["declared baselines explain most variation and the residual edge is weak"]
    reasons = ["residual evidence does not clear every robustness threshold"]
    if not rolling_pass:
        reasons.append("rolling alpha is positive in too few windows")
    return "RESIDUAL_FRAGILE", reasons


def _declaration_problems(declarations: dict | None) -> list[str]:
    declarations = declarations or {}
    problems = []
    for key, allowed in DECLARATION_VALUES.items():
        value = declarations.get(key)
        if value is None:
            problems.append(f"{key} is not declared")
        elif value not in allowed:
            problems.append(f"{key}={value!r} is not decision grade (allowed: {sorted(allowed)})")
    if declarations.get("strategy_return_basis") and declarations.get("baseline_return_basis") \
            and declarations["strategy_return_basis"] != declarations["baseline_return_basis"]:
        problems.append("strategy and baseline returns are not on the same basis")
    return problems


def _regimes(strategy: pd.Series, baselines: pd.DataFrame, model: dict, regimes: dict | None, ann: int, min_obs: int) -> dict:
    out: dict[str, Any] = {}
    if not regimes:
        return out
    fitted_part = sum(baselines[n] * b for n, b in model["betas"].items())
    active = strategy - fitted_part
    for label_name, labels in regimes.items():
        series = pd.Series(labels).reindex(strategy.index)
        rows = {}
        for regime, idx in active.groupby(series).groups.items():
            part = active.loc[idx]
            rows[str(regime)] = {"n": int(len(part)), "annualized_active_return": float(part.mean() * ann) if len(part) >= min_obs else None,
                                 "sufficient": bool(len(part) >= min_obs)}
        out[str(label_name)] = rows
    return out


def analyze(strategy: pd.Series, baselines: pd.DataFrame, *, primary: list[str], sensitivity: list[list[str]] | None = None,
            regimes: dict | None = None, declarations: dict | None = None, frequency: str = "daily", rolling_window: int | None = None,
            min_observations: int = 60, min_regime_observations: int = 20, hac_lags: int | None = None,
            thresholds: dict | None = None) -> dict:
    """Full analysis on aligned series. Raises ValueError when the data cannot be fitted."""
    if frequency not in ANNUALIZATION:
        raise BadInput(f"frequency must be one of {sorted(ANNUALIZATION)}")
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    unknown = set(th) - set(DEFAULT_THRESHOLDS)
    if unknown:
        raise BadInput(f"unknown threshold(s) {sorted(unknown)}")
    ann = ANNUALIZATION[frequency]
    window = rolling_window or ROLLING_WINDOW[frequency]
    needed = sorted({c for model in [primary] + list(sensitivity or []) for c in model})
    missing = [c for c in needed if c not in baselines.columns]
    if missing:
        raise BadInput(f"baseline column(s) not found: {missing}")
    frame = pd.concat([strategy.rename("__strategy__"), baselines[needed]], axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    y, bl = frame["__strategy__"], frame[needed]
    report: dict[str, Any] = {"n": int(len(y)), "frequency": frequency, "declaration_problems": _declaration_problems(declarations)}
    if len(y) < max(min_observations, len(needed) + 3):
        report.update(status="INSUFFICIENT_EVIDENCE", decision_eligibility="REVIEW_REQUIRED", primary=None, sensitivity=[], regimes={})
        return report
    primary_model = _model(y, bl, list(primary), ann, window, th, hac_lags)
    sens_models = [_model(y, bl, list(m), ann, window, th, hac_lags) for m in (sensitivity or [])]
    status = primary_model["status"]
    notes: list[str] = []
    if status == "RESIDUAL_EDGE":
        if not sens_models:
            status, notes = "RESIDUAL_FRAGILE", ["no alternate baseline model was tested"]
        elif any(m["status"] != "RESIDUAL_EDGE" for m in sens_models):
            status, notes = "RESIDUAL_FRAGILE", ["the result changes across declared baseline models"]
    review = list(report["declaration_problems"])
    if any(v > VIF_LIMIT for v in primary_model["vif"].values()):
        review.append("multicollinearity among baselines (VIF above 10)")
    if not primary_model["rolling"]["enabled"]:
        review.append("rolling evidence unavailable")
    if not sens_models:
        review.append("no alternate baseline tested")
    report.update(status=status, status_notes=notes, decision_eligibility="REVIEW_REQUIRED" if review else "ELIGIBLE", review_reasons=review,
                  primary=primary_model, sensitivity=sens_models,
                  regimes=_regimes(y, bl, primary_model, regimes, ann, min_regime_observations))
    return report


@registry.register(
    "validation.residual_edge", module="validation",
    summary="Separate a strategy return into declared baseline exposure and residual edge (HAC alpha, edge ratio, rolling stability, sensitivity, regimes).",
    inputs={"strategy": {"type": "series", "required": True}, "baselines": {"type": "dataframe", "required": True},
            "primary": {"type": "list", "required": True}, "sensitivity": {"type": "list", "required": False, "default": None},
            "regimes": {"type": "dict", "required": False, "default": None}, "declarations": {"type": "dict", "required": False, "default": None},
            "frequency": {"type": "str", "required": False, "default": "daily"}, "rolling_window": {"type": "int", "required": False, "default": None},
            "min_observations": {"type": "int", "required": False, "default": 60}},
    piece_ids=("CTS-038",),
)
def residual_edge(strategy: Any, baselines: pd.DataFrame, primary: list[str], sensitivity: list[list[str]] | None = None,
                  regimes: dict | None = None, declarations: dict | None = None, frequency: str = "daily",
                  rolling_window: int | None = None, min_observations: int = 60) -> QuantResult:
    name = "validation.residual_edge"
    series = helpers.as_series(strategy, name="strategy")
    if not isinstance(baselines, pd.DataFrame):
        raise BadInput("baselines must be a DataFrame with one column per baseline")
    if not isinstance(primary, (list, tuple)) or not primary:
        raise BadInput("primary must be a non empty list of baseline column names")
    if (series.dropna() <= -1.0).any():
        return QuantResult.empty(name, "ratio", "newey_west_ols", "strategy returns must be greater than -100%")
    observed = series.replace([np.inf, -np.inf], np.nan).dropna()
    if observed.empty or float(observed.max() - observed.min()) == 0.0:
        return QuantResult.empty(name, "ratio", "newey_west_ols", "strategy returns are empty or constant")
    try:
        report = analyze(series, baselines, primary=list(primary), sensitivity=[list(m) for m in (sensitivity or [])], regimes=regimes,
                         declarations=declarations, frequency=frequency, rolling_window=rolling_window, min_observations=min_observations)
    except BadInput:
        raise
    except (ValueError, np.linalg.LinAlgError) as exc:
        return QuantResult.empty(name, "ratio", "newey_west_ols", str(exc))
    warnings = tuple(report["declaration_problems"])
    return QuantResult(name=name, value=report, unit="ratio", method="newey_west_ols",
                       params={"frequency": frequency, "primary": list(primary), "rolling_window": rolling_window or ROLLING_WINDOW[frequency]},
                       inputs_hash=helpers.hash_inputs(series, baselines[sorted({c for m in [list(primary)] + [list(m) for m in (sensitivity or [])] for c in m if c in baselines})]),
                       as_of=helpers.last_as_of(series), warnings=warnings, piece_ids=("CTS-038",))

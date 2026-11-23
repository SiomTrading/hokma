"""Kalman filter tools (ECT-035): local level and time varying regression (dynamic beta and pairs hedge ratio).

Rules built into the functions:
* Operation uses the filter (causal): the value at time t uses data up to t. The smoother uses future data and is tagged
  research_only; it must never feed a signal or an order.
* Q and R are never taken from a rule of thumb: when they are not given they are calibrated by maximum likelihood.
* Every result carries the innovation diagnostics: standardized innovations should have mean near 0, standard deviation near 1
  and no autocorrelation.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.stats import _common as c
from hokma.quant.stats import _lib
from hokma.quant.types import QuantResult

PIECES = ("ECT-035",)
MODELS = ("local_level", "regression")
MAX_CALIBRATION_OBS = 20_000
_SERIES = {"type": "series", "required": True}


def _design(y: np.ndarray, x: np.ndarray | None, model: str) -> np.ndarray:
    if model == "local_level":
        return np.ones((len(y), 1))
    if x is None:
        raise c.Domain("the regression model needs x")
    return np.column_stack([np.ones(len(y)), x])


def _joseph(cov_pred: np.ndarray, gain: np.ndarray, h_row: np.ndarray, r: float) -> np.ndarray:
    """Covariance update in the Joseph form: stays symmetric and positive under the huge diffuse start covariance."""
    m = len(gain)
    factor = np.eye(m) - np.outer(gain, h_row)
    return factor @ cov_pred @ factor.T + r * np.outer(gain, gain)


def kalman_run(y: np.ndarray, h: np.ndarray, q: float, r: float, p0_scale: float = 1e4) -> dict:
    """Linear Gaussian filter for y_t = H_t theta_t + e (variance r), theta_t = theta_{t-1} + w (covariance q I).

    Initial state: zero (the first observation for the local level) with a diffuse covariance p0_scale * max(y0^2, 1) * I, which uses
    the first observation only, so no later observation reaches an earlier estimate. Returns filtered and predicted quantities.
    """
    n, m = h.shape
    state = np.zeros(m)
    if m == 1:
        state[0] = y[0]
    cov = np.eye(m) * p0_scale * max(float(y[0]) ** 2, 1.0)
    states = np.empty((n, m))
    innov = np.empty(n)
    var_s = np.empty(n)
    loglik = 0.0
    q_mat = np.eye(m) * q
    for t in range(n):
        cov_pred = cov + q_mat
        ht = h[t]
        s = float(ht @ cov_pred @ ht) + r
        v = float(y[t] - ht @ state)
        gain = cov_pred @ ht / s
        state = state + gain * v
        cov = _joseph(cov_pred, gain, ht, r)
        states[t] = state
        innov[t] = v
        var_s[t] = s
        if t >= m:
            loglik += -0.5 * (math.log(2.0 * math.pi * s) + v * v / s)
    return {"states": states, "innovations": innov, "innovation_variance": var_s, "loglik": loglik, "last_cov": cov}


def smooth(y: np.ndarray, h: np.ndarray, q: float, r: float, p0_scale: float = 1e4) -> np.ndarray:
    """Rauch-Tung-Striebel smoother of the same model. Uses the whole sample: research only."""
    n, m = h.shape
    state = np.zeros(m)
    if m == 1:
        state[0] = y[0]
    cov = np.eye(m) * p0_scale * max(float(y[0]) ** 2, 1.0)
    q_mat = np.eye(m) * q
    filt = np.empty((n, m))
    filt_cov = np.empty((n, m, m))
    pred_cov = np.empty((n, m, m))
    for t in range(n):
        cov_pred = cov + q_mat
        pred_cov[t] = cov_pred
        ht = h[t]
        s = float(ht @ cov_pred @ ht) + r
        gain = cov_pred @ ht / s
        state = state + gain * float(y[t] - ht @ state)
        cov = _joseph(cov_pred, gain, ht, r)
        filt[t], filt_cov[t] = state, cov
    out = filt.copy()
    for t in range(n - 2, -1, -1):
        j = filt_cov[t] @ np.linalg.inv(pred_cov[t + 1])
        out[t] = filt[t] + j @ (out[t + 1] - filt[t])
    return out


def innovation_diagnostics(run: dict, burn: int) -> dict:
    z = run["innovations"][burn:] / np.sqrt(run["innovation_variance"][burn:])
    lags = min(10, max(len(z) // 5, 2))
    q1, p1 = c.ljung_box(z, lags)
    q2, p2 = c.ljung_box(z ** 2, lags)
    return {"mean": float(z.mean()), "std": float(z.std(ddof=1)), "ljung_box_p": p1, "ljung_box_squared_p": p2,
            "well_specified": bool(abs(z.mean()) < 2.0 / math.sqrt(len(z)) * 1.5 and 0.8 < float(z.std(ddof=1)) < 1.25 and p1 >= 0.05)}


def calibrate_core(y: np.ndarray, h: np.ndarray) -> dict:
    """Maximum likelihood of (q, r) on a log scale from several starts."""
    if len(y) > MAX_CALIBRATION_OBS:
        raise c.Domain(f"calibration needs at most {MAX_CALIBRATION_OBS} observations")
    scale = max(float(np.var(np.diff(y))), 1e-12)

    def negative(theta: np.ndarray) -> float:
        q, r = math.exp(theta[0]), math.exp(theta[1])
        return -kalman_run(y, h, q, r)["loglik"]

    best = None
    for q0, r0 in ((0.01, 0.5), (0.001, 0.9), (0.1, 0.1)):
        start = np.log(np.array([q0 * scale, r0 * scale]))
        sol = _lib.sp_optimize.minimize(negative, start, method="Nelder-Mead", options={"xatol": 1e-6, "fatol": 1e-9, "maxiter": 400})
        if best is None or sol.fun < best.fun:
            best = sol
    q, r = float(math.exp(best.x[0])), float(math.exp(best.x[1]))
    return {"q": q, "r": r, "loglik": float(-best.fun), "converged": bool(best.success)}


def _prepare(y: Any, x: Any, model: str) -> tuple[np.ndarray, np.ndarray | None, np.ndarray, tuple[str, ...], pd.Index]:
    if model not in MODELS:
        raise c.Domain(f"model must be one of {list(MODELS)}")
    sy = c.helpers.as_series(y, name="y")
    if model == "regression":
        if x is None:
            raise c.Domain("the regression model needs x")
        sx = c.helpers.as_series(x, name="x")
        if len(sx) != len(sy):
            raise BadInput(f"y and x must have the same length, got {len(sy)} and {len(sx)}")
        joined = pd.concat([sy.rename("y").reset_index(drop=True), sx.rename("x").reset_index(drop=True)], axis=1).replace([np.inf, -np.inf], np.nan)
        keep = joined.notna().all(axis=1).to_numpy()
        warns = (f"dropped {int((~keep).sum())} row(s) with NaN or infinite values",) if (~keep).any() else ()
        index = sy.index[keep]
        yy, xx = joined["y"].to_numpy()[keep], joined["x"].to_numpy()[keep]
    else:
        clean = c.helpers.clean(sy)
        warns = (f"dropped {len(sy) - len(clean)} NaN or infinite value(s)",) if len(clean) != len(sy) else ()
        index = clean.index
        yy, xx = clean.to_numpy(), None
    if len(yy) < 30:
        raise c.Domain(f"need at least 30 observations, got {len(yy)}")
    if c.is_constant(yy):
        raise c.Domain("constant series: dispersion is zero")
    return yy, xx, _design(yy, xx, model), warns, index


def _noise_params(q: Any, r: Any, yy: np.ndarray, h: np.ndarray) -> tuple[float, float, dict | None]:
    if (q is None) != (r is None):
        raise c.Domain("give both q and r, or neither (then they are calibrated by maximum likelihood)")
    if q is None:
        cal = calibrate_core(yy, h)
        return cal["q"], cal["r"], cal
    qv, rv = c.number("q", q, low=0.0), c.number("r", r, low=0.0)
    if rv <= 0:
        raise c.Domain("r must be positive")
    return qv, rv, None


@registry.register("stats.kalman_filter", module="stats",
                   summary="Causal Kalman filter: local_level (smoothed level of y) or regression (dynamic intercept and beta of y on x, the adaptive hedge ratio of a pair). Q and R are calibrated by maximum likelihood when not given. Returns the filtered state, the real time spread and the innovation diagnostics.",
                   inputs={"y": _SERIES, "x": {"type": "series", "required": False, "default": None}, "model": {"type": "str", "required": False, "default": "regression"},
                           "q": {"type": "float", "required": False, "default": None}, "r": {"type": "float", "required": False, "default": None}}, piece_ids=PIECES)
def kalman_filter(y: Any, x: Any = None, model: str = "regression", q: float | None = None, r: float | None = None) -> QuantResult:
    name = "stats.kalman_filter"
    params = {"model": model, "q": q, "r": r}

    def compute() -> Any:
        yy, xx, h, warns, index = _prepare(y, x, model)
        qv, rv, cal = _noise_params(q, r, yy, h)
        run = kalman_run(yy, h, qv, rv)
        burn = h.shape[1]
        diag = innovation_diagnostics(run, burn)
        z = run["innovations"] / np.sqrt(run["innovation_variance"])
        extra = ["filtered values use data up to each time only (causal); use them in operation, never the smoother"]
        if cal is not None:
            extra.append("q and r were calibrated by maximum likelihood on this sample: re-calibrate on a schedule and test out of sample")
        if not diag["well_specified"]:
            extra.append("innovations are not white with unit variance: the model is mis-specified for this series")
        states = run["states"]
        out = {"model": model, "q": qv, "r": rv, "calibrated": cal is not None, "loglik": run["loglik"], "n_obs": len(yy), "index": [str(i) for i in index],
               "innovations": run["innovations"], "standardized_innovations": z, "innovation_diagnostics": diag, "research_only": False}
        if model == "local_level":
            out["level"] = states[:, 0]
            out["latest_level"] = float(states[-1, 0])
        else:
            out["intercept"] = states[:, 0]
            out["beta"] = states[:, 1]
            out["latest_beta"] = float(states[-1, 1])
            out["latest_intercept"] = float(states[-1, 0])
            out["latest_z"] = float(z[-1])
        return out, warns + tuple(extra)

    return c.run(name, unit="state", method=f"kalman_{model}", params=params, piece_ids=PIECES, compute=compute)


@registry.register("stats.kalman_calibrate", module="stats",
                   summary="Maximum likelihood estimate of the Kalman noise variances: Q (state) and R (observation) for the local_level or regression model, with the log likelihood. Calibrate instead of using fixed starting values.",
                   inputs={"y": _SERIES, "x": {"type": "series", "required": False, "default": None}, "model": {"type": "str", "required": False, "default": "regression"}},
                   piece_ids=PIECES)
def kalman_calibrate(y: Any, x: Any = None, model: str = "regression") -> QuantResult:
    name = "stats.kalman_calibrate"

    def compute() -> Any:
        yy, xx, h, warns, _ = _prepare(y, x, model)
        cal = calibrate_core(yy, h)
        extra = [] if cal["converged"] else ["the optimizer did not report convergence: treat the values as approximate"]
        return {**cal, "n_obs": len(yy), "model": model, "signal_to_noise": cal["q"] / cal["r"]}, warns + tuple(extra)

    return c.run(name, unit="variance", method="kalman_mle", params={"model": model}, piece_ids=PIECES, compute=compute)


@registry.register("stats.kalman_smoother", module="stats",
                   summary="RESEARCH ONLY. Rauch-Tung-Striebel smoother of the Kalman model: it uses the whole sample, including the future of every date, so it must never feed a signal, a backtest entry or an order. Use stats.kalman_filter in operation.",
                   inputs={"y": _SERIES, "x": {"type": "series", "required": False, "default": None}, "model": {"type": "str", "required": False, "default": "regression"},
                           "q": {"type": "float", "required": False, "default": None}, "r": {"type": "float", "required": False, "default": None}}, piece_ids=PIECES)
def kalman_smoother(y: Any, x: Any = None, model: str = "regression", q: float | None = None, r: float | None = None) -> QuantResult:
    name = "stats.kalman_smoother"
    params = {"model": model, "q": q, "r": r, "research_only": True}

    def compute() -> Any:
        yy, xx, h, warns, index = _prepare(y, x, model)
        qv, rv, cal = _noise_params(q, r, yy, h)
        states = smooth(yy, h, qv, rv)
        out: dict[str, Any] = {"model": model, "q": qv, "r": rv, "research_only": True, "index": [str(i) for i in index]}
        if model == "local_level":
            out["level"] = states[:, 0]
        else:
            out["intercept"] = states[:, 0]
            out["beta"] = states[:, 1]
        return out, warns + ("research_only: the smoothed path uses future data at every date; never use it as a signal or in a backtest",)

    return c.run(name, unit="state", method=f"rts_smoother_{model}", params=params, piece_ids=PIECES, compute=compute)

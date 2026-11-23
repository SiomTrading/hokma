"""Numerical cores shared by the time series, cointegration, regression and causal files of the stats module.

Pure numpy: no statistics package is needed. Critical values are the published asymptotic or response surface values for the
Dickey-Fuller family, and simulated (seeded) for the Johansen statistics.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from hokma.quant.stats import _common as c
from hokma.quant.stats import _lib


@dataclass(frozen=True)
class Ols:
    beta: np.ndarray
    resid: np.ndarray
    cov: np.ndarray            # covariance matrix of beta, s2 (X'X)^-1
    s2: float
    df: int
    r2: float
    n: int
    xtx_inv: np.ndarray

    @property
    def se(self) -> np.ndarray:
        return np.sqrt(np.clip(np.diag(self.cov), 0.0, None))

    @property
    def t(self) -> np.ndarray:
        se = self.se
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(se > 0, self.beta / se, np.nan)


def ols(y: np.ndarray, x: np.ndarray) -> Ols:
    """Ordinary least squares with an explicit design matrix. Singular designs are a Domain error."""
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    n, k = x.shape
    if n <= k:
        raise c.Domain(f"need more observations ({n}) than regressors ({k})")
    if np.linalg.matrix_rank(x) < k:
        raise c.Domain("the regressors are collinear")
    xtx_inv = np.linalg.inv(x.T @ x)
    beta = xtx_inv @ (x.T @ y)
    resid = y - x @ beta
    df = n - k
    s2 = float(resid @ resid) / df
    ybar = y.mean()
    tss = float(np.sum((y - ybar) ** 2))
    r2 = 1.0 - float(resid @ resid) / tss if tss > 0 else float("nan")
    return Ols(beta=beta, resid=resid, cov=s2 * xtx_inv, s2=s2, df=df, r2=r2, n=n, xtx_inv=xtx_inv)


# Dickey-Fuller response surface: critical value = b0 + b1 / T + b2 / T^2 + b3 / T^3, by regression type and level.
_DF_SURFACE = {
    "n": {"1%": (-2.56574, -2.2358, -3.627, 0.0), "5%": (-1.94100, -0.2686, -3.365, 31.223), "10%": (-1.61682, 0.2656, -2.714, 25.364)},
    "c": {"1%": (-3.43035, -6.5393, -16.786, -79.433), "5%": (-2.86154, -2.8903, -4.234, -40.040), "10%": (-2.56677, -1.5384, -2.809, 0.0)},
    "ct": {"1%": (-3.95877, -9.0531, -28.428, -134.155), "5%": (-3.41049, -4.3904, -9.036, -45.374), "10%": (-3.12705, -2.5856, -3.925, -22.380)},
}
REGRESSIONS = ("n", "c", "ct")


def df_critical_values(regression: str, nobs: int) -> dict[str, float]:
    surface = _DF_SURFACE[regression]
    return {lvl: b[0] + b[1] / nobs + b[2] / nobs ** 2 + b[3] / nobs ** 3 for lvl, b in surface.items()}


# Residual based cointegration test (Engle-Granger, constant in the cointegrating regression): asymptotic critical values by number of variables.
_EG_ASYMPTOTIC = {
    2: {"1%": -3.89644, "5%": -3.33613, "10%": -3.04445},
    3: {"1%": -4.29374, "5%": -3.74066, "10%": -3.45218},
    4: {"1%": -4.64332, "5%": -4.09600, "10%": -3.81020},
    5: {"1%": -4.95756, "5%": -4.41519, "10%": -4.13157},
    6: {"1%": -5.24568, "5%": -4.70644, "10%": -4.42501},
}


def band(stat: float, crit: dict[str, float]) -> str:
    """Where the statistic falls against the 1%, 5% and 10% critical values (lower is more negative)."""
    if stat <= crit["1%"]:
        return "<0.01"
    if stat <= crit["5%"]:
        return "0.01-0.05"
    if stat <= crit["10%"]:
        return "0.05-0.10"
    return ">0.10"


def adf_design(y: np.ndarray, regression: str, lags: int, start: int) -> tuple[np.ndarray, np.ndarray]:
    """Dependent and design matrix of the augmented Dickey-Fuller regression, using observations from `start` on."""
    dy = np.diff(y)
    n = len(dy)
    rows = np.arange(start, n)
    cols = [y[rows]]                       # y_{t-1}, aligned with dy[t]
    for i in range(1, lags + 1):
        cols.append(dy[rows - i])
    deterministic = []
    if regression in ("c", "ct"):
        deterministic.append(np.ones(len(rows)))
    if regression == "ct":
        deterministic.append(rows.astype(float) + 1.0)
    x = np.column_stack(cols + deterministic)
    return dy[rows], x


def adf_core(y: np.ndarray, regression: str = "c", lags: int | None = None, max_lag: int | None = None) -> dict:
    """Augmented Dickey-Fuller statistic. `lags=None` picks the lag by AIC over a common sample up to `max_lag`."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    if max_lag is None:
        max_lag = int(math.ceil(12.0 * (n / 100.0) ** 0.25))
    max_lag = max(0, min(max_lag, (n - 1) // 2 - 2))
    chosen = lags
    if chosen is None:
        best = None
        for k in range(0, max_lag + 1):
            dep, x = adf_design(y, regression, k, max_lag)
            fit = ols(dep, x)
            m = len(dep)
            aic = m * math.log(float(fit.resid @ fit.resid) / m) + 2 * x.shape[1]
            if best is None or aic < best[0]:
                best = (aic, k)
        chosen = best[1]
    if chosen > (n - 1) // 2 - 2:
        raise c.Domain("too many lags for this sample")
    dep, x = adf_design(y, regression, chosen, chosen)
    fit = ols(dep, x)
    stat = float(fit.beta[0] / fit.se[0])
    nobs = len(dep)
    return {"statistic": stat, "lags": int(chosen), "n_obs": int(nobs), "rho_minus_one": float(fit.beta[0]),
            "critical_values": df_critical_values(regression, nobs), "fit": fit}


def johansen_core(data: np.ndarray, k_ar_diff: int) -> dict:
    """Johansen reduced rank regression with an unrestricted constant. Returns eigenvalues, eigenvectors and the moment matrices."""
    data = np.asarray(data, dtype=float)
    t_total, n_vars = data.shape
    dy = np.diff(data, axis=0)
    t_eff = len(dy) - k_ar_diff
    if t_eff < 5 * n_vars + 5:
        raise c.Domain("not enough observations for the Johansen procedure")
    y0 = dy[k_ar_diff:]
    y1 = data[k_ar_diff:-1]
    z = np.column_stack([np.ones(t_eff)] + [dy[k_ar_diff - i: len(dy) - i] for i in range(1, k_ar_diff + 1)])
    proj = z @ np.linalg.pinv(z)
    r0 = y0 - proj @ y0
    r1 = y1 - proj @ y1
    s00 = r0.T @ r0 / t_eff
    s11 = r1.T @ r1 / t_eff
    s01 = r0.T @ r1 / t_eff
    s10 = s01.T
    # generalized eigenproblem S10 S00^-1 S01 v = lambda S11 v, solved on the symmetrized form
    l11 = np.linalg.cholesky(s11)
    inv_l11 = np.linalg.inv(l11)
    middle = inv_l11 @ s10 @ np.linalg.inv(s00) @ s01 @ inv_l11.T
    middle = (middle + middle.T) / 2.0
    values, vectors = np.linalg.eigh(middle)
    order = np.argsort(values)[::-1]
    values = np.clip(values[order], 0.0, 1.0 - 1e-12)
    vectors = inv_l11.T @ vectors[:, order]         # beta' S11 beta = I
    return {"eigenvalues": values, "beta": vectors, "s00": s00, "s11": s11, "s01": s01, "t_eff": t_eff, "z": z, "y0": y0, "y1": y1}


def johansen_statistics(eigenvalues: np.ndarray, t_eff: int) -> tuple[np.ndarray, np.ndarray]:
    """Trace and maximum eigenvalue statistics for the null ranks 0..n_vars-1."""
    lam = np.asarray(eigenvalues, dtype=float)
    n = len(lam)
    log_terms = -np.log(1.0 - lam)
    trace = np.array([t_eff * float(log_terms[r:].sum()) for r in range(n)])
    max_eig = np.array([t_eff * float(log_terms[r]) for r in range(n)])
    return trace, max_eig


@lru_cache(maxsize=64)
def johansen_critical_values(n_vars: int, nobs: int, k_ar_diff: int, n_sim: int, seed: int, drift_ratio: float = 0.0) -> dict:
    """Critical values of the trace and maximum eigenvalue statistics by simulation of random walks with drift.

    Row r holds the values for the null "rank at most r", which is the test of no cointegration among n_vars - r common
    trends: a system of n_vars - r random walks of the size of the data is simulated and the statistic of its rank zero test is
    recorded. `drift_ratio` is the drift of each walk in units of its step standard deviation. With an unrestricted
    constant a large drift gives the chi-square like values of the drifting case (about 3.8 at 95% for one trend) and no drift gives
    the larger values of the demeaned case (about 7 to 9), so the ratio is taken from the data. Columns are the 90%, 95% and
    99% levels.
    """
    quantiles = (0.90, 0.95, 0.99)
    trace_rows, max_rows = [], []
    for r in range(n_vars):
        dims = n_vars - r
        rng = np.random.default_rng(seed + 1000 * dims)
        traces = np.empty(n_sim)
        maxes = np.empty(n_sim)
        for i in range(n_sim):
            walk = np.cumsum(drift_ratio + rng.standard_normal((nobs, dims)), axis=0)
            core = johansen_core(walk, k_ar_diff)
            tr, mx = johansen_statistics(core["eigenvalues"], core["t_eff"])
            traces[i], maxes[i] = tr[0], mx[0]
        trace_rows.append(np.quantile(traces, quantiles))
        max_rows.append(np.quantile(maxes, quantiles))
    return {"trace": np.array(trace_rows), "max_eigenvalue": np.array(max_rows), "levels": ["90%", "95%", "99%"]}


def two_sided_p(t_stat: float, df: int) -> float:
    return float(2.0 * _lib.sp_stats.t.sf(abs(t_stat), df))

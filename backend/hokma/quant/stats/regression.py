"""Regressions of the stats module: OLS, WLS, GLS, logit, probit, quantile (FT-091), Fama-MacBeth (FT-090) and panel
models with fixed effects, random effects and the Hausman test (FT-092).

Written on numpy and scipy (the statistics package of the source library is not a dependency). Standard errors: classical,
heteroskedasticity robust (HC1) or Newey-West (HAC) for the linear models.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import limits, registry
from hokma.quant.errors import BadInput
from hokma.quant.stats import _common as c
from hokma.quant.stats import _econ as econ
from hokma.quant.stats import _lib
from hokma.quant.types import QuantResult

METHODS = ("ols", "wls", "gls", "logit", "probit", "quantile")
COV_TYPES = ("nonrobust", "hc1", "hac")
MAX_GLS_OBS = 3000
_SERIES = {"type": "series", "required": True}
_X = {"type": "series or table", "required": True}


def _design(x: Any, add_constant: bool) -> tuple[np.ndarray, list[str]]:
    if isinstance(x, pd.Series):
        x = x.to_frame(x.name if x.name is not None else "x")
    if isinstance(x, pd.DataFrame):
        names = [str(col) for col in x.columns]
        arr = x
    elif isinstance(x, (list, tuple, np.ndarray)):
        arr = pd.DataFrame(np.asarray(x, dtype=float))
        if arr.ndim == 1 or arr.shape[1] == 0:
            raise BadInput("x must be a series or a table")
        names = [f"x{i + 1}" for i in range(arr.shape[1])]
    else:
        raise BadInput(f"x: expected a Series, DataFrame or array, got {type(x).__name__}")
    try:
        mat = arr.astype(float).to_numpy()
    except (TypeError, ValueError) as exc:
        raise BadInput("x: values are not numeric") from exc
    if add_constant:
        return np.column_stack([np.ones(len(mat)), mat]), ["const"] + names
    return mat, names


def _sandwich(xtx_inv: np.ndarray, x: np.ndarray, u: np.ndarray, cov_type: str, hac_lags: int | None) -> np.ndarray:
    n, k = x.shape
    if cov_type == "hc1":
        meat = (x * u[:, None]).T @ (x * u[:, None])
        return xtx_inv @ meat @ xtx_inv * n / (n - k)
    lags = hac_lags if hac_lags is not None else c.default_nw_lags(n)
    xu = x * u[:, None]
    meat = xu.T @ xu
    for lag in range(1, lags + 1):
        w = 1.0 - lag / (lags + 1.0)
        gamma = xu[lag:].T @ xu[:-lag]
        meat += w * (gamma + gamma.T)
    return xtx_inv @ meat @ xtx_inv * n / (n - k)


def _linear_result(beta: np.ndarray, cov: np.ndarray, df: int, names: list[str], extra: dict) -> dict:
    se = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(se > 0, beta / se, np.nan)
    p = 2.0 * _lib.sp_stats.t.sf(np.abs(t), df)
    return {"names": names, "coefficients": dict(zip(names, beta)), "std_errors": dict(zip(names, se)), "t_stats": dict(zip(names, t)),
            "p_values": dict(zip(names, p)), "df_resid": int(df), **extra}


def _fit_linear(y: np.ndarray, x: np.ndarray, names: list[str], method: str, weights: Any, sigma: Any, cov_type: str, hac_lags: int | None) -> tuple[dict, tuple[str, ...]]:
    n, k = x.shape
    warns: list[str] = []
    if method == "ols":
        fit = econ.ols(y, x)
        beta, resid, xtx_inv, df = fit.beta, fit.resid, fit.xtx_inv, fit.df
        cov = fit.cov if cov_type == "nonrobust" else _sandwich(xtx_inv, x, resid, cov_type, hac_lags)
        tss = float(np.sum((y - y.mean()) ** 2))
        extra = {"r2": fit.r2, "adj_r2": 1.0 - (1.0 - fit.r2) * (n - 1) / df if math.isfinite(fit.r2) else None, "sigma": math.sqrt(fit.s2), "n_obs": n}
        if tss > 0 and k > 1:
            f_stat = ((tss - float(resid @ resid)) / (k - 1)) / (float(resid @ resid) / df) if "const" in names else None
            if f_stat is not None:
                extra["f_stat"] = f_stat
                extra["f_p_value"] = float(_lib.sp_stats.f.sf(f_stat, k - 1, df))
    elif method == "wls":
        if weights is None:
            raise c.Domain("wls needs weights")
        w = np.asarray(weights, dtype=float)
        if w.shape != (n,) or not np.all(np.isfinite(w)) or (w <= 0).any():
            raise c.Domain("weights must be positive, finite and one per observation")
        sw = np.sqrt(w)
        fit = econ.ols(y * sw, x * sw[:, None])
        beta, xtx_inv, df = fit.beta, fit.xtx_inv, fit.df
        resid_w = fit.resid
        cov = fit.cov if cov_type == "nonrobust" else _sandwich(xtx_inv, x * sw[:, None], resid_w, cov_type, hac_lags)
        ybar_w = float(np.sum(w * y) / np.sum(w))
        tss = float(np.sum(w * (y - ybar_w) ** 2))
        extra = {"r2": 1.0 - float(resid_w @ resid_w) / tss if tss > 0 else None, "sigma": math.sqrt(fit.s2), "n_obs": n}
    else:
        if n > MAX_GLS_OBS:
            raise c.Domain(f"gls needs at most {MAX_GLS_OBS} observations")
        if sigma is not None:
            s = np.asarray(sigma, dtype=float)
            if s.ndim == 1:
                if s.shape != (n,) or (s <= 0).any():
                    raise c.Domain("a vector sigma needs one positive variance per observation")
                s = np.diag(s)
            if s.shape != (n, n) or not np.allclose(s, s.T) or not np.all(np.isfinite(s)):
                raise c.Domain("sigma must be a symmetric n x n covariance matrix")
            try:
                chol = np.linalg.cholesky(s)
            except np.linalg.LinAlgError as exc:
                raise c.Domain("sigma is not positive definite") from exc
            ty = np.linalg.solve(chol, y)
            tx = np.linalg.solve(chol, x)
            extra_gls = {"gls": "given covariance"}
        else:
            base = econ.ols(y, x)
            e = base.resid
            rho = float(e[1:] @ e[:-1] / (e @ e))
            if abs(rho) >= 0.999:
                raise c.Domain("the estimated autocorrelation of the errors is too close to one for feasible GLS")
            scale = math.sqrt(1.0 - rho ** 2)
            ty = np.concatenate([[y[0] * scale], y[1:] - rho * y[:-1]])
            tx = np.vstack([x[0] * scale, x[1:] - rho * x[:-1]])
            extra_gls = {"gls": "feasible, AR(1) errors (Prais-Winsten)", "rho": rho}
        fit = econ.ols(ty, tx)
        beta, xtx_inv, df = fit.beta, fit.xtx_inv, fit.df
        cov = fit.cov if cov_type == "nonrobust" else _sandwich(xtx_inv, tx, fit.resid, cov_type, hac_lags)
        tss = float(np.sum((ty - ty.mean()) ** 2))
        extra = {"r2": 1.0 - float(fit.resid @ fit.resid) / tss if tss > 0 else None, "sigma": math.sqrt(fit.s2), "n_obs": n, **extra_gls}
        warns.append("r2 of a transformed (GLS) regression is not comparable with the OLS r2")
    return _linear_result(beta, cov, df, names, extra), tuple(warns)


def logit_probit_fit(y: np.ndarray, x: np.ndarray, link: str, max_iter: int = 100, tol: float = 1e-12) -> dict:
    """Maximum likelihood by Newton-Raphson. Perfect separation or a singular information matrix is a Domain error."""
    n, k = x.shape
    norm = _lib.sp_stats.norm
    beta = np.zeros(k)

    def parts(b: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
        eta = np.clip(x @ b, -35.0, 35.0)
        if link == "logit":
            p = 1.0 / (1.0 + np.exp(-eta))
            weight = p * (1.0 - p)
            score_w = y - p
        else:
            p = np.clip(norm.cdf(eta), 1e-16, 1.0 - 1e-16)
            dens = norm.pdf(eta)
            weight = dens ** 2 / (p * (1.0 - p))
            score_w = dens * (y - p) / (p * (1.0 - p))
        llf = float(np.sum(y * np.log(np.clip(p, 1e-300, None)) + (1 - y) * np.log(np.clip(1 - p, 1e-300, None))))
        return score_w, weight, llf

    llf_old = -float("inf")
    converged = False
    for _ in range(max_iter):
        score_w, weight, llf = parts(beta)
        grad = x.T @ score_w
        info = (x * weight[:, None]).T @ x
        try:
            step = np.linalg.solve(info, grad)
        except np.linalg.LinAlgError as exc:
            raise c.Domain("the information matrix is singular: collinear regressors or perfect separation") from exc
        beta = beta + step
        if np.max(np.abs(step)) < tol:
            converged = True
            break
        if abs(llf - llf_old) < 1e-14 and np.max(np.abs(step)) < 1e-9:
            converged = True
            break
        llf_old = llf
    if not converged or np.max(np.abs(beta)) > 30.0:
        raise c.Domain("no convergence: perfect or quasi perfect separation of the outcome")
    score_w, weight, llf = parts(beta)
    info = (x * weight[:, None]).T @ x
    cov = np.linalg.inv(info)
    ybar = float(y.mean())
    llf_null = n * (ybar * math.log(ybar) + (1 - ybar) * math.log(1 - ybar)) if 0 < ybar < 1 else float("nan")
    return {"beta": beta, "cov": cov, "llf": llf, "llf_null": llf_null, "score_contrib": x * score_w[:, None], "info_inv": cov}


def quantile_fit(y: np.ndarray, x: np.ndarray, tau: float) -> np.ndarray:
    """Exact quantile regression as a linear program (the simplex based solver of scipy)."""
    n, k = x.shape
    cost = np.concatenate([np.zeros(k), np.full(n, tau), np.full(n, 1.0 - tau)])
    a_eq = _lib.sp_sparse.hstack([_lib.sp_sparse.csr_matrix(x), _lib.sp_sparse.identity(n, format="csr"), -_lib.sp_sparse.identity(n, format="csr")], format="csr")
    bounds = [(None, None)] * k + [(0, None)] * (2 * n)
    sol = _lib.sp_optimize.linprog(cost, A_eq=a_eq, b_eq=y, bounds=bounds, method="highs")
    if not sol.success:
        raise c.Domain(f"the quantile regression did not solve: {sol.message}")
    return np.asarray(sol.x[:k])


@registry.register("stats.regress", module="stats",
                   summary="Regressions of y on x: ols, wls (weights), gls (covariance sigma, or feasible AR(1) errors), logit, probit (y is 0 or 1) and quantile (tau). Standard errors classical, hc1 or hac (Newey-West) for the linear methods.",
                   inputs={"y": _SERIES, "x": _X, "method": {"type": "str", "required": False, "default": "ols"},
                           "add_constant": {"type": "bool", "required": False, "default": True},
                           "weights": {"type": "series", "required": False, "default": None}, "sigma": {"type": "series or table", "required": False, "default": None},
                           "tau": {"type": "float", "required": False, "default": 0.5}, "cov_type": {"type": "str", "required": False, "default": "nonrobust"},
                           "hac_lags": {"type": "int", "required": False, "default": None}, "n_boot": {"type": "int", "required": False, "default": 200},
                           "seed": {"type": "int", "required": False, "default": 0}}, piece_ids=("FT-091",))
def regress(y: Any, x: Any, method: str = "ols", add_constant: bool = True, weights: Any = None, sigma: Any = None, tau: float = 0.5,
            cov_type: str = "nonrobust", hac_lags: int | None = None, n_boot: int = 200, seed: int = 0) -> QuantResult:
    name, pieces = "stats.regress", ("FT-091",)
    limits.check_param("n_boot", n_boot)
    limits.as_int("n_boot", n_boot)
    limits.as_int("seed", seed)
    if hac_lags is not None:
        limits.as_int("hac_lags", hac_lags)
    params = {"method": method, "add_constant": bool(add_constant), "tau": tau, "cov_type": cov_type, "hac_lags": hac_lags, "n_boot": int(n_boot), "seed": int(seed)}

    def compute() -> Any:
        if method not in METHODS:
            raise c.Domain(f"method must be one of {list(METHODS)}")
        if cov_type not in COV_TYPES:
            raise c.Domain(f"cov_type must be one of {list(COV_TYPES)}")
        design, names = _design(x, bool(add_constant))
        sy = c.helpers.as_series(y, name="y")
        if len(sy) != len(design):
            raise BadInput(f"y and x must have the same length, got {len(sy)} and {len(design)}")
        keep = np.isfinite(sy.to_numpy()) & np.all(np.isfinite(design), axis=1)
        warns: list[str] = []
        if not keep.all():
            warns.append(f"dropped {int((~keep).sum())} row(s) with NaN or infinite values")
        yy, xx = sy.to_numpy()[keep], design[keep]
        w = None if weights is None else np.asarray(weights, dtype=float)[keep]
        sg = sigma
        if sigma is not None and np.asarray(sigma).ndim == 1 and not keep.all():
            sg = np.asarray(sigma, dtype=float)[keep]
        if len(yy) < xx.shape[1] + 2:
            raise c.Domain(f"need at least {xx.shape[1] + 2} complete rows, got {len(yy)}")
        if method in ("logit", "probit"):
            if not np.isin(yy, (0.0, 1.0)).all():
                raise c.Domain("y must be 0 or 1 for logit and probit")
            if yy.min() == yy.max():
                raise c.Domain("y has a single class")
            if cov_type == "hac":
                raise c.Domain("hac is available for the linear methods only")
            fit = logit_probit_fit(yy, xx, method)
            cov = fit["cov"]
            if cov_type == "hc1":
                meat = fit["score_contrib"].T @ fit["score_contrib"]
                cov = cov @ meat @ cov * len(yy) / (len(yy) - xx.shape[1])
            se = np.sqrt(np.diag(cov))
            z = fit["beta"] / se
            p = 2.0 * _lib.sp_stats.norm.sf(np.abs(z))
            res = {"names": names, "coefficients": dict(zip(names, fit["beta"])), "std_errors": dict(zip(names, se)), "z_stats": dict(zip(names, z)),
                   "p_values": dict(zip(names, p)), "log_likelihood": fit["llf"], "pseudo_r2": 1.0 - fit["llf"] / fit["llf_null"] if math.isfinite(fit["llf_null"]) else None,
                   "n_obs": len(yy), "link": method}
            return res, tuple(warns)
        if method == "quantile":
            if not 0.0 < tau < 1.0:
                raise c.Domain("tau must be strictly between 0 and 1")
            if len(yy) > 20000:
                raise c.Domain("the quantile regression needs at most 20000 observations")
            if n_boot < 20:
                raise c.Domain("n_boot must be at least 20")
            if np.linalg.matrix_rank(xx) < xx.shape[1]:
                raise c.Domain("the regressors are collinear: the quantile regression has no unique solution")
            beta = quantile_fit(yy, xx, tau)
            rng = np.random.default_rng(int(seed))
            draws = []
            for _ in range(int(n_boot)):
                idx = rng.integers(0, len(yy), len(yy))
                try:
                    draws.append(quantile_fit(yy[idx], xx[idx], tau))
                except c.Domain:
                    continue
            if len(draws) < 10:
                raise c.Domain("the bootstrap did not produce enough solutions")
            se = np.std(np.array(draws), axis=0, ddof=1)
            resid = yy - xx @ beta
            check = float(np.sum(np.where(resid >= 0, tau * resid, (tau - 1.0) * resid)))
            return ({"names": names, "coefficients": dict(zip(names, beta)), "std_errors": dict(zip(names, se)), "tau": tau, "check_loss": check,
                     "n_obs": len(yy), "n_boot": len(draws)}, tuple(warns) + ("standard errors are from a seeded pairs bootstrap",))
        res, extra = _fit_linear(yy, xx, names, method, w, sg, cov_type, hac_lags)
        return res, tuple(warns) + extra

    return c.run(name, unit="coefficient", method=method, params=params, piece_ids=pieces, compute=compute)


def _long_frame(data: Any, columns: list[str], entity: str, time: str) -> tuple[pd.DataFrame, tuple[str, ...]]:
    if not isinstance(data, pd.DataFrame):
        raise BadInput(f"data: expected a DataFrame, got {type(data).__name__}")
    missing = [col for col in columns + [entity, time] if col not in data.columns]
    if missing:
        raise BadInput(f"data: missing column(s) {missing}")
    frame = data[columns + [entity, time]].copy()
    try:
        frame[columns] = frame[columns].astype(float)
    except (TypeError, ValueError) as exc:
        raise BadInput("data: the numeric columns are not numeric") from exc
    frame[columns] = frame[columns].replace([np.inf, -np.inf], np.nan)
    cleaned = frame.dropna()
    warns = (f"dropped {len(frame) - len(cleaned)} row(s) with missing values",) if len(cleaned) != len(frame) else ()
    return cleaned, warns


@registry.register("stats.fama_macbeth", module="stats",
                   summary="Fama-MacBeth cross-sectional regression: one regression of y on the characteristics per date, then the mean of each coefficient with its time series standard error (Newey-West when nw_lags > 0). data is a long table.",
                   inputs={"data": {"type": "table", "required": True}, "y": {"type": "str", "required": True}, "x": {"type": "list of column names", "required": True},
                           "date": {"type": "str", "required": False, "default": "date"}, "entity": {"type": "str", "required": False, "default": "entity"},
                           "nw_lags": {"type": "int", "required": False, "default": 0}}, piece_ids=("FT-090",))
def fama_macbeth(data: Any, y: str, x: list, date: str = "date", entity: str = "entity", nw_lags: int = 0) -> QuantResult:
    name, pieces = "stats.fama_macbeth", ("FT-090",)
    limits.as_int("nw_lags", nw_lags)
    params = {"y": y, "x": list(x) if isinstance(x, (list, tuple)) else x, "date": date, "entity": entity, "nw_lags": int(nw_lags)}

    def compute() -> Any:
        if not isinstance(x, (list, tuple)) or not x or not all(isinstance(v, str) for v in x) or not isinstance(y, str):
            raise BadInput("y is a column name and x a list of column names")
        cols = [y] + list(x)
        frame, warns = _long_frame(data, cols, entity, date)
        if nw_lags < 0:
            raise c.Domain("nw_lags cannot be negative")
        k = len(x) + 1
        rows, dates, counts, r2s = [], [], [], []
        for d, grp in frame.groupby(date, sort=True):
            if len(grp) < k + 2:
                continue
            design = np.column_stack([np.ones(len(grp)), grp[list(x)].to_numpy()])
            try:
                fit = econ.ols(grp[y].to_numpy(), design)
            except c.Domain:
                continue
            rows.append(fit.beta)
            dates.append(d)
            counts.append(len(grp))
            r2s.append(fit.r2)
        if len(rows) < 3:
            raise c.Domain(f"need at least 3 usable dates (with {k + 2}+ assets each), got {len(rows)}")
        betas = np.array(rows)
        t_n = len(betas)
        mean = betas.mean(axis=0)
        names = ["const"] + list(x)
        if nw_lags == 0:
            se = betas.std(axis=0, ddof=1) / math.sqrt(t_n)
        else:
            se = np.array([math.sqrt(c.newey_west_variance(betas[:, j] - mean[j], int(nw_lags)) / t_n) for j in range(k)])
        with np.errstate(divide="ignore", invalid="ignore"):
            t_stat = np.where(se > 0, mean / se, np.nan)
        p = 2.0 * _lib.sp_stats.t.sf(np.abs(t_stat), t_n - 1)
        out = {"names": names, "coefficients": dict(zip(names, mean)), "std_errors": dict(zip(names, se)), "t_stats": dict(zip(names, t_stat)),
               "p_values": dict(zip(names, p)), "n_dates": t_n, "mean_assets_per_date": float(np.mean(counts)), "mean_r2": float(np.mean(r2s)),
               "date_index": [str(d) for d in dates], "period_coefficients": {n_: betas[:, j] for j, n_ in enumerate(names)}}
        return out, warns

    return c.run(name, unit="coefficient", method="fama_macbeth", params=params, piece_ids=pieces, compute=compute)


PANEL_MODELS = ("fixed", "random", "pooled", "between", "hausman")


def _demean(values: np.ndarray, groups: np.ndarray) -> np.ndarray:
    frame = pd.DataFrame(values)
    return values - frame.groupby(groups).transform("mean").to_numpy()


def _cluster_cov(xtx_inv: np.ndarray, x: np.ndarray, u: np.ndarray, groups: np.ndarray, n_params: int) -> np.ndarray:
    labels, inverse = np.unique(groups, return_inverse=True)
    g = len(labels)
    scores = np.zeros((g, x.shape[1]))
    np.add.at(scores, inverse, x * u[:, None])
    meat = scores.T @ scores
    n = len(u)
    factor = g / (g - 1.0) * (n - 1.0) / (n - n_params) if g > 1 else 1.0
    return xtx_inv @ meat @ xtx_inv * factor


def panel_estimates(frame: pd.DataFrame, y: str, xcols: list[str], entity: str, cluster: bool) -> dict:
    ids = frame[entity].to_numpy()
    yy = frame[y].to_numpy()
    xx = frame[xcols].to_numpy()
    n, k = xx.shape
    labels, inverse, t_i = np.unique(ids, return_inverse=True, return_counts=True)
    n_ent = len(labels)
    if n_ent < 2:
        raise c.Domain("need at least 2 entities")
    if n <= n_ent + k:
        raise c.Domain("need more observations than entities plus regressors")
    # fixed effects: within transformation
    xw, yw = _demean(xx, inverse), _demean(yy[:, None], inverse)[:, 0]
    if np.linalg.matrix_rank(xw) < k:
        raise c.Domain("a regressor does not vary within entities: fixed effects absorb it")
    xtx_inv = np.linalg.inv(xw.T @ xw)
    b_fe = xtx_inv @ xw.T @ yw
    e_fe = yw - xw @ b_fe
    df_fe = n - n_ent - k
    s2_fe = float(e_fe @ e_fe) / df_fe
    cov_fe = _cluster_cov(xtx_inv, xw, e_fe, ids, n_ent + k) if cluster else s2_fe * xtx_inv
    alpha_i = pd.DataFrame(yy - xx @ b_fe).groupby(inverse).mean().to_numpy()[:, 0] - (pd.DataFrame(xx).groupby(inverse).mean().to_numpy() @ b_fe)
    # between and random effects (Swamy-Arora)
    ybar = pd.DataFrame(yy).groupby(inverse).mean().to_numpy()[:, 0]
    xbar = pd.DataFrame(xx).groupby(inverse).mean().to_numpy()
    xb = np.column_stack([np.ones(n_ent), xbar])
    if n_ent <= k + 1:
        raise c.Domain("need more entities than regressors for the between and random effects estimators")
    fit_b = econ.ols(ybar, xb)
    sigma_u2 = s2_fe
    t_harm = n_ent / float(np.sum(1.0 / t_i))
    sigma_b2 = float(fit_b.resid @ fit_b.resid) / (n_ent - k - 1)
    sigma_a2 = max(sigma_b2 - sigma_u2 / t_harm, 0.0)
    theta_i = 1.0 - np.sqrt(sigma_u2 / (sigma_u2 + t_i * sigma_a2))
    theta = theta_i[inverse]
    ystar = yy - theta * ybar[inverse]
    xstar = np.column_stack([1.0 - theta, xx - theta[:, None] * xbar[inverse]])
    xs_inv = np.linalg.inv(xstar.T @ xstar)
    b_re_full = xs_inv @ xstar.T @ ystar
    e_re = ystar - xstar @ b_re_full
    # after the quasi-demeaning the error variance is the idiosyncratic variance (taken from the within fit)
    cov_re_full = _cluster_cov(xs_inv, xstar, e_re, ids, k + 1) if cluster else sigma_u2 * xs_inv
    pooled = econ.ols(yy, np.column_stack([np.ones(n), xx]))
    cov_pool = _cluster_cov(pooled.xtx_inv, np.column_stack([np.ones(n), xx]), pooled.resid, ids, k + 1) if cluster else pooled.cov
    return {"n": n, "k": k, "n_entities": n_ent, "b_fe": b_fe, "cov_fe": cov_fe, "df_fe": df_fe, "sigma_u2": sigma_u2, "sigma_alpha2": sigma_a2, "theta_mean": float(theta_i.mean()),
            "b_re": b_re_full, "cov_re": cov_re_full, "b_between": fit_b.beta, "cov_between": fit_b.cov, "b_pooled": pooled.beta, "cov_pooled": cov_pool,
            "effects": alpha_i, "labels": labels, "r2_within": 1.0 - float(e_fe @ e_fe) / float(yw @ yw) if float(yw @ yw) > 0 else None}


@registry.register("stats.panel", module="stats",
                   summary="Panel data regression of y on x with an entity and a time column: fixed (within), random (Swamy-Arora), pooled, between, and hausman (fixed against random). Standard errors clustered by entity by default.",
                   inputs={"data": {"type": "table", "required": True}, "y": {"type": "str", "required": True}, "x": {"type": "list of column names", "required": True},
                           "entity": {"type": "str", "required": False, "default": "entity"}, "time": {"type": "str", "required": False, "default": "date"},
                           "model": {"type": "str", "required": False, "default": "fixed"}, "cluster": {"type": "bool", "required": False, "default": True}},
                   piece_ids=("FT-092",))
def panel(data: Any, y: str, x: list, entity: str = "entity", time: str = "date", model: str = "fixed", cluster: bool = True) -> QuantResult:
    name, pieces = "stats.panel", ("FT-092",)
    params = {"y": y, "x": list(x) if isinstance(x, (list, tuple)) else x, "entity": entity, "time": time, "model": model, "cluster": bool(cluster)}

    def compute() -> Any:
        if not isinstance(x, (list, tuple)) or not x or not all(isinstance(v, str) for v in x) or not isinstance(y, str):
            raise BadInput("y is a column name and x a list of column names")
        if model not in PANEL_MODELS:
            raise c.Domain(f"model must be one of {list(PANEL_MODELS)}")
        xcols = list(x)
        frame, warns = _long_frame(data, [y] + xcols, entity, time)
        dup = frame.duplicated(subset=[entity, time]).sum()
        if dup:
            raise c.Domain(f"{int(dup)} duplicated (entity, time) pair(s): each entity has one row per time")
        est = panel_estimates(frame, y, xcols, entity, bool(cluster) and model != "hausman")
        k = est["k"]
        extra = ["balanced and unbalanced panels are handled; standard errors are clustered by entity" if cluster and model != "hausman" else "classical standard errors"]

        def block(beta: np.ndarray, cov: np.ndarray, names: list[str], df: int) -> dict:
            return _linear_result(beta, cov, df, names, {})

        if model == "fixed":
            res = block(est["b_fe"], est["cov_fe"], xcols, est["df_fe"])
            res.update({"r2_within": est["r2_within"], "n_obs": est["n"], "n_entities": est["n_entities"], "sigma_u2": est["sigma_u2"],
                        "entity_effects": dict(zip([str(v) for v in est["labels"]], est["effects"]))})
        elif model == "random":
            res = block(est["b_re"], est["cov_re"], ["const"] + xcols, est["n"] - k - 1)
            res.update({"n_obs": est["n"], "n_entities": est["n_entities"], "sigma_u2": est["sigma_u2"], "sigma_alpha2": est["sigma_alpha2"], "theta_mean": est["theta_mean"]})
        elif model == "pooled":
            res = block(est["b_pooled"], est["cov_pooled"], ["const"] + xcols, est["n"] - k - 1)
            res["n_obs"] = est["n"]
        elif model == "between":
            res = block(est["b_between"], est["cov_between"], ["const"] + xcols, est["n_entities"] - k - 1)
            res["n_entities"] = est["n_entities"]
        else:
            diff = est["b_fe"] - est["b_re"][1:]
            v_fe = est["cov_fe"]
            v_re = est["cov_re"][1:, 1:]
            v_diff = v_fe - v_re
            eigs = np.linalg.eigvalsh((v_diff + v_diff.T) / 2.0)
            if eigs.min() <= 0:
                extra.append("the covariance difference is not positive definite (typical with clustered errors or little within variation): the generalized inverse is used, read the test with care")
            chi2 = float(diff @ np.linalg.pinv(v_diff) @ diff)
            if chi2 < 0:
                raise c.Domain("the Hausman statistic is negative: the covariance difference is not usable")
            res = {"statistic": chi2, "df": k, "p_value": float(_lib.sp_stats.chi2.sf(chi2, k)), "prefer": "fixed" if _lib.sp_stats.chi2.sf(chi2, k) < 0.05 else "random",
                   "fixed": block(est["b_fe"], est["cov_fe"], xcols, est["df_fe"]), "random": block(est["b_re"][1:], est["cov_re"][1:, 1:], xcols, est["n"] - k - 1),
                   "n_obs": est["n"], "n_entities": est["n_entities"]}
            extra.append("the Hausman test is a screening step: a rejection favors fixed effects, no rejection does not prove random effects")
        return res, warns + tuple(extra)

    return c.run(name, unit="coefficient", method=f"panel_{model}", params=params, piece_ids=pieces, compute=compute)

"""Time series tools of the stats module: unit roots, stationarity check, autocorrelation, ARIMA, VAR, IRF, FEVD, VECM,
Granger causality, half life, structural breaks and the intraday seasonality.

Rows FT-082, FT-083, FT-087 and ECT-015. Everything is written on numpy and scipy; the critical values are the published
Dickey-Fuller, KPSS and Zivot-Andrews values.
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
from hokma.quant.stats import cointegration
from hokma.quant.types import QuantResult

_SERIES = {"type": "series", "required": True}
UNIT_ROOT_TESTS = ("adf", "kpss", "pp", "za")

KPSS_CRITICAL = {"c": {"10%": 0.347, "5%": 0.463, "2.5%": 0.574, "1%": 0.739},
                 "ct": {"10%": 0.119, "5%": 0.146, "2.5%": 0.176, "1%": 0.216}}
# Zivot-Andrews (1992) 1%, 5% and 10% critical values by break type: intercept, trend, both.
ZA_CRITICAL = {"c": {"1%": -5.34, "5%": -4.80, "10%": -4.58}, "t": {"1%": -4.93, "5%": -4.42, "10%": -4.11},
               "ct": {"1%": -5.57, "5%": -5.08, "10%": -4.82}}
MAX_ZA_OBS = 5000
MAX_DP_OBS = 5000


def kpss_core(y: np.ndarray, regression: str, lags: int | None) -> dict:
    n = len(y)
    t = np.arange(1, n + 1, dtype=float)
    x = np.ones((n, 1)) if regression == "c" else np.column_stack([np.ones(n), t])
    resid = y - x @ np.linalg.lstsq(x, y, rcond=None)[0]
    if lags is None:
        lags = int(4.0 * (n / 100.0) ** 0.25)
    lags = min(lags, n - 1)
    s2 = c.newey_west_variance(resid, lags)
    if s2 <= 0:
        raise c.Domain("the long run variance is zero")
    partial = np.cumsum(resid)
    return {"statistic": float(np.sum(partial ** 2) / (n ** 2 * s2)), "lags": int(lags), "n_obs": n, "critical_values": KPSS_CRITICAL[regression]}


def pp_core(y: np.ndarray, regression: str, lags: int | None) -> dict:
    n = len(y) - 1
    cols = [y[:-1]]
    if regression in ("c", "ct"):
        cols.append(np.ones(n))
    if regression == "ct":
        cols.append(np.arange(1, n + 1, dtype=float))
    fit = econ.ols(y[1:], np.column_stack(cols))
    t_rho = float((fit.beta[0] - 1.0) / fit.se[0])
    if lags is None:
        lags = int(4.0 * (n / 100.0) ** 0.25)
    lags = min(lags, n - 2)
    gamma0 = float(fit.resid @ fit.resid) / n
    lam2 = c.newey_west_variance(fit.resid, lags)
    if lam2 <= 0 or gamma0 <= 0:
        raise c.Domain("the long run variance is zero")
    s = math.sqrt(fit.s2)
    stat = math.sqrt(gamma0 / lam2) * t_rho - 0.5 * (lam2 - gamma0) / math.sqrt(lam2) * (n * float(fit.se[0]) / s)
    return {"statistic": float(stat), "lags": int(lags), "n_obs": n, "critical_values": econ.df_critical_values(regression, n)}


def za_core(y: np.ndarray, model: str, lags: int, trim: float = 0.15) -> dict:
    n = len(y)
    dy = np.diff(y)
    start = lags
    rows = np.arange(start, len(dy))
    base = [y[rows]] + [dy[rows - i] for i in range(1, lags + 1)] + [np.ones(len(rows)), (rows + 1.0)]
    best = None
    for tb in range(int(math.ceil(trim * n)), int(math.floor((1.0 - trim) * n))):
        extra = []
        if model in ("c", "ct"):
            extra.append((rows + 1 > tb).astype(float))
        if model in ("t", "ct"):
            extra.append(np.where(rows + 1 > tb, rows + 1.0 - tb, 0.0))
        cols = list(base) + extra
        if model == "c":
            pass
        x = np.column_stack(cols)
        try:
            fit = econ.ols(dy[rows], x)
        except c.Domain:
            continue
        stat = float(fit.beta[0] / fit.se[0])
        if best is None or stat < best[0]:
            best = (stat, tb)
    if best is None:
        raise c.Domain("no admissible break date")
    return {"statistic": best[0], "break_index": int(best[1]), "lags": int(lags), "n_obs": int(len(rows)), "critical_values": ZA_CRITICAL[model]}


def _run_unit_root(y: np.ndarray, test: str, regression: str, lags: int | None, max_lag: int | None) -> dict:
    if test == "adf":
        if regression not in econ.REGRESSIONS:
            raise c.Domain("regression must be n, c or ct for the ADF test")
        core = econ.adf_core(y, regression, lags, max_lag)
        crit = core["critical_values"]
        out = {"null_hypothesis": "unit root", "statistic": core["statistic"], "critical_values": crit,
               "p_value_band": econ.band(core["statistic"], crit), "reject_null_5pct": core["statistic"] <= crit["5%"],
               "lags": core["lags"], "n_obs": core["n_obs"]}
    elif test == "pp":
        if regression not in econ.REGRESSIONS:
            raise c.Domain("regression must be n, c or ct for the Phillips-Perron test")
        core = pp_core(y, regression, lags)
        crit = core["critical_values"]
        out = {"null_hypothesis": "unit root", "statistic": core["statistic"], "critical_values": crit,
               "p_value_band": econ.band(core["statistic"], crit), "reject_null_5pct": core["statistic"] <= crit["5%"],
               "lags": core["lags"], "n_obs": core["n_obs"]}
    elif test == "kpss":
        if regression not in KPSS_CRITICAL:
            raise c.Domain("regression must be c or ct for the KPSS test")
        core = kpss_core(y, regression, lags)
        crit = core["critical_values"]
        out = {"null_hypothesis": "stationary", "statistic": core["statistic"], "critical_values": crit,
               "p_value_band": "<0.01" if core["statistic"] >= crit["1%"] else "0.01-0.05" if core["statistic"] >= crit["5%"] else "0.05-0.10" if core["statistic"] >= crit["10%"] else ">0.10",
               "reject_null_5pct": core["statistic"] >= crit["5%"], "lags": core["lags"], "n_obs": core["n_obs"]}
    else:
        if regression not in ZA_CRITICAL:
            raise c.Domain("regression must be c (intercept break), t (trend break) or ct (both) for the Zivot-Andrews test")
        if len(y) > MAX_ZA_OBS:
            raise c.Domain(f"the Zivot-Andrews search needs at most {MAX_ZA_OBS} observations")
        if lags is None:
            lags = econ.adf_core(y, "ct", None, max_lag)["lags"]
        core = za_core(y, regression, lags)
        crit = core["critical_values"]
        out = {"null_hypothesis": "unit root without structural break", "statistic": core["statistic"], "critical_values": crit,
               "p_value_band": econ.band(core["statistic"], crit), "reject_null_5pct": core["statistic"] <= crit["5%"],
               "break_index": core["break_index"], "lags": core["lags"], "n_obs": core["n_obs"]}
    out["test"] = test
    out["regression"] = regression
    return out


@registry.register("stats.unit_root", module="stats",
                   summary="Unit root and stationarity tests: adf, pp (null: unit root), kpss (null: stationary) and za (break in intercept, trend or both). Returns the statistic, critical values and the 5% decision.",
                   inputs={"series": _SERIES, "test": {"type": "str", "required": False, "default": "adf"},
                           "regression": {"type": "str", "required": False, "default": "c"},
                           "lags": {"type": "int", "required": False, "default": None},
                           "max_lag": {"type": "int", "required": False, "default": None}}, piece_ids=("FT-082",))
def unit_root(series: Any, test: str = "adf", regression: str = "c", lags: int | None = None, max_lag: int | None = None) -> QuantResult:
    name, pieces = "stats.unit_root", ("FT-082",)
    for label, v in (("lags", lags), ("max_lag", max_lag)):
        limits.check_param(label, v)
        if v is not None:
            limits.as_int(label, v)
    params = {"test": test, "regression": regression, "lags": lags, "max_lag": max_lag}

    def compute() -> Any:
        if test not in UNIT_ROOT_TESTS:
            raise c.Domain(f"test must be one of {list(UNIT_ROOT_TESTS)}")
        y, warns = c.clean_series(series, name="series", min_obs=20)
        if c.is_constant(y):
            raise c.Domain("constant series: the tests are undefined")
        if lags is not None and lags < 0:
            raise c.Domain("lags cannot be negative")
        return _run_unit_root(y.to_numpy(), test, regression, lags, max_lag), warns

    return c.run(name, unit="statistic", method=test, params=params, piece_ids=pieces, compute=compute,
                 inputs=(series,) if isinstance(series, pd.Series) else ())


@registry.register("stats.stationarity_check", module="stats",
                   summary="ADF and KPSS together on one series and the joint reading: stationary, unit_root (difference it), trend_or_break or inconclusive.",
                   inputs={"series": _SERIES, "regression": {"type": "str", "required": False, "default": "c"}}, piece_ids=("FT-082", "ECT-015"))
def stationarity_check(series: Any, regression: str = "c") -> QuantResult:
    name, pieces = "stats.stationarity_check", ("FT-082", "ECT-015")
    params = {"regression": regression}

    def compute() -> Any:
        if regression not in ("c", "ct"):
            raise c.Domain("regression must be c or ct")
        y, warns = c.clean_series(series, name="series", min_obs=20)
        if c.is_constant(y):
            raise c.Domain("constant series: the tests are undefined")
        adf = _run_unit_root(y.to_numpy(), "adf", regression, None, None)
        kpss = _run_unit_root(y.to_numpy(), "kpss", regression, None, None)
        adf_rejects, kpss_rejects = adf["reject_null_5pct"], kpss["reject_null_5pct"]
        if adf_rejects and not kpss_rejects:
            conclusion, advice = "stationary", "model the series in levels"
        elif not adf_rejects and kpss_rejects:
            conclusion, advice = "unit_root", "difference the series (or use returns) before modelling"
        elif adf_rejects and kpss_rejects:
            conclusion, advice = "trend_or_break", "the tests disagree: look for a trend or a structural break (stats.structural_breaks)"
        else:
            conclusion, advice = "inconclusive", "neither test is decisive: the sample may be too short for either"
        return {"conclusion": conclusion, "advice": advice, "adf": adf, "kpss": kpss}, warns

    return c.run(name, unit="statistic", method="adf_and_kpss", params=params, piece_ids=pieces, compute=compute,
                 inputs=(series,) if isinstance(series, pd.Series) else ())


@registry.register("stats.autocorrelation", module="stats",
                   summary="Autocorrelation and partial autocorrelation up to a lag, the 95% band and the Ljung-Box test of the series.",
                   inputs={"series": _SERIES, "lags": {"type": "int", "required": False, "default": 20}}, piece_ids=("ECT-015",))
def autocorrelation(series: Any, lags: int = 20) -> QuantResult:
    name, pieces = "stats.autocorrelation", ("ECT-015",)
    limits.check_param("lags", lags)
    limits.as_int("lags", lags)
    params = {"lags": int(lags)}

    def compute() -> Any:
        if lags < 1:
            raise c.Domain("lags must be at least 1")
        y, warns = c.clean_series(series, name="series", min_obs=max(lags + 5, 10))
        if lags >= len(y) // 2:
            raise c.Domain("lags must be below half the number of observations")
        x = y.to_numpy()
        a, p = c.acf(x, lags), c.pacf(x, lags)
        band = 1.96 / math.sqrt(len(x))
        q, pv = c.ljung_box(x, lags)
        return ({"acf": a, "pacf": p, "band_95": band,
                 "significant_acf_lags": [k for k in range(1, lags + 1) if abs(a[k]) > band],
                 "significant_pacf_lags": [k for k in range(1, lags + 1) if abs(p[k]) > band],
                 "ljung_box": {"q": q, "p_value": pv, "lags": int(lags)}}, warns)

    return c.run(name, unit="ratio", method="acf_pacf_ljung_box", params=params, piece_ids=pieces, compute=compute,
                 inputs=(series,) if isinstance(series, pd.Series) else ())


# ---------------------------------------------------------------- ARIMA, VAR, IRF, FEVD, VECM

MAX_ORDER = 10


def _difference(x: np.ndarray, d: int) -> np.ndarray:
    for _ in range(d):
        x = np.diff(x)
    return x


def arima_fit(y: np.ndarray, p: int, d: int, q: int) -> dict:
    """ARIMA(p, d, q) with a constant by conditional least squares (exact least squares when q is zero)."""
    w = _difference(y, d)
    n = len(w)
    if n <= p + q + 5:
        raise c.Domain("not enough observations for this order")
    if q == 0:
        if p == 0:
            const, phi = float(w.mean()), np.zeros(0)
            resid = w - const
        else:
            rows = np.arange(p, n)
            x = np.column_stack([np.ones(len(rows))] + [w[rows - i] for i in range(1, p + 1)])
            fit = econ.ols(w[rows], x)
            phi = fit.beta[1:]
            const = float(fit.beta[0] / (1.0 - phi.sum())) if abs(1.0 - phi.sum()) > 1e-12 else float(fit.beta[0])
            resid = fit.resid
        theta = np.zeros(0)
    else:
        start = np.concatenate([[w.mean()], np.zeros(p + q)])
        if p > 0:
            rows = np.arange(p, n)
            x = np.column_stack([np.ones(len(rows))] + [w[rows - i] for i in range(1, p + 1)])
            beta0 = np.linalg.lstsq(x, w[rows], rcond=None)[0]
            start[1:1 + p] = beta0[1:]
            start[0] = beta0[0] / max(1.0 - beta0[1:].sum(), 1e-3)

        def residuals(theta_vec: np.ndarray) -> np.ndarray:
            const_, phi_, th_ = theta_vec[0], theta_vec[1:1 + p], theta_vec[1 + p:]
            e = _lib.sp_signal.lfilter(np.concatenate([[1.0], -phi_]), np.concatenate([[1.0], th_]), w - const_)
            return e[p:]

        sol = _lib.sp_optimize.least_squares(residuals, start, method="lm", xtol=1e-12, ftol=1e-12, gtol=1e-12)
        const, phi, theta = float(sol.x[0]), sol.x[1:1 + p], sol.x[1 + p:]
        resid = residuals(sol.x)
    m = len(resid)
    sigma2 = float(resid @ resid) / m
    k = p + q + 1
    aic = m * math.log(sigma2) + 2 * k
    bic = m * math.log(sigma2) + k * math.log(m)
    ar_roots = np.roots(np.concatenate([[1.0], -phi])[::1]) if p else np.zeros(0)
    ma_roots = np.roots(np.concatenate([[1.0], theta])) if q else np.zeros(0)
    stationary = bool(np.all(np.abs(ar_roots) < 1.0)) if p else True
    invertible = bool(np.all(np.abs(ma_roots) < 1.0)) if q else True
    return {"const": const, "phi": np.asarray(phi), "theta": np.asarray(theta), "resid": resid, "sigma2": sigma2, "aic": aic, "bic": bic,
            "w": w, "stationary": stationary, "invertible": invertible, "n_resid": m}


def arima_forecast(y: np.ndarray, fit: dict, d: int, horizon: int) -> np.ndarray:
    const, phi, theta, w, resid = fit["const"], fit["phi"], fit["theta"], fit["w"], fit["resid"]
    p, q = len(phi), len(theta)
    hist = list(w - const)
    errs = list(np.concatenate([np.zeros(len(w) - len(resid)), resid]))
    out = []
    for _ in range(horizon):
        value = sum(phi[i] * hist[-1 - i] for i in range(p)) + sum(theta[j] * errs[-1 - j] for j in range(q))
        hist.append(value)
        errs.append(0.0)
        out.append(value + const)
    forecast = np.array(out)
    for level in range(d):
        base = _difference(y, d - level - 1)[-1]
        forecast = base + np.cumsum(forecast)
    return forecast


def var_fit(data: np.ndarray, lags: int) -> dict:
    t_total, k = data.shape
    rows = np.arange(lags, t_total)
    x = np.column_stack([np.ones(len(rows))] + [data[rows - i] for i in range(1, lags + 1)])
    if len(rows) <= x.shape[1] + 2:
        raise c.Domain("not enough observations for this number of lags")
    if np.linalg.matrix_rank(x) < x.shape[1]:
        raise c.Domain("the lagged regressors are collinear")
    y = data[rows]
    beta = np.linalg.lstsq(x, y, rcond=None)[0]
    resid = y - x @ beta
    dof = len(rows) - x.shape[1]
    sigma_u = resid.T @ resid / dof
    a = [beta[1 + i * k: 1 + (i + 1) * k].T for i in range(lags)]
    companion = np.zeros((k * lags, k * lags))
    companion[:k, :] = np.hstack(a)
    if lags > 1:
        companion[k:, :-k] = np.eye(k * (lags - 1))
    max_root = float(np.max(np.abs(np.linalg.eigvals(companion))))
    m = len(rows)
    sigma_ml = resid.T @ resid / m
    logdet = float(np.linalg.slogdet(sigma_ml)[1])
    n_par = k * (k * lags + 1)
    return {"const": beta[0], "A": a, "sigma_u": sigma_u, "resid": resid, "max_root": max_root, "stable": max_root < 1.0,
            "aic": logdet + 2.0 * n_par / m, "bic": logdet + math.log(m) * n_par / m, "n_obs": m}


def ma_matrices(a: list[np.ndarray], horizon: int) -> list[np.ndarray]:
    k = a[0].shape[0]
    psi = [np.eye(k)]
    for s in range(1, horizon + 1):
        term = np.zeros((k, k))
        for i in range(1, min(s, len(a)) + 1):
            term += a[i - 1] @ psi[s - i]
        psi.append(term)
    return psi


def var_forecast(data: np.ndarray, fit: dict, horizon: int) -> np.ndarray:
    lags = len(fit["A"])
    hist = [row for row in data[-lags:]]
    out = []
    for _ in range(horizon):
        nxt = fit["const"] + sum(fit["A"][i] @ hist[-1 - i] for i in range(lags))
        hist.append(nxt)
        out.append(nxt)
    return np.array(out)


MODEL_KINDS = ("arima", "var", "irf", "fevd", "vecm")


def _frame(data: Any) -> tuple[pd.DataFrame, tuple[str, ...]]:
    return c.clean_frame(data, name="data", min_cols=2, min_obs=20)


@registry.register("stats.models", module="stats",
                   summary="Time series models: arima (p, d, q, conditional least squares, Ljung-Box and forecast), var (lags), irf (orthogonalized, Cholesky in column order), fevd and vecm (Johansen, rank and lags).",
                   inputs={"data": {"type": "series or table", "required": True}, "kind": {"type": "str", "required": False, "default": "arima"},
                           "p": {"type": "int", "required": False, "default": 1}, "d": {"type": "int", "required": False, "default": 0},
                           "q": {"type": "int", "required": False, "default": 0}, "lags": {"type": "int", "required": False, "default": 1},
                           "horizon": {"type": "int", "required": False, "default": 10}, "rank": {"type": "int", "required": False, "default": 1}},
                   piece_ids=("FT-083", "ECT-015"))
def models(data: Any, kind: str = "arima", p: int = 1, d: int = 0, q: int = 0, lags: int = 1, horizon: int = 10, rank: int = 1) -> QuantResult:
    name, pieces = "stats.models", ("FT-083", "ECT-015")
    for label, v in (("lags", lags), ("horizon", horizon)):
        limits.check_param(label, v)
        limits.as_int(label, v)
    for label, v in (("p", p), ("d", d), ("q", q), ("rank", rank)):
        limits.as_int(label, v)
    params = {"kind": kind, "p": int(p), "d": int(d), "q": int(q), "lags": int(lags), "horizon": int(horizon), "rank": int(rank)}

    def compute() -> Any:
        if kind not in MODEL_KINDS:
            raise c.Domain(f"kind must be one of {list(MODEL_KINDS)}")
        if horizon < 1:
            raise c.Domain("horizon must be at least 1")
        if kind == "arima":
            if not (0 <= p <= MAX_ORDER and 0 <= q <= MAX_ORDER and 0 <= d <= 2):
                raise c.Domain(f"need 0 <= p, q <= {MAX_ORDER} and 0 <= d <= 2")
            y, warns = c.clean_series(data, name="data", min_obs=30)
            if c.is_constant(y):
                raise c.Domain("constant series: dispersion is zero")
            arr = y.to_numpy()
            fit = arima_fit(arr, p, d, q)
            w_var = float(np.var(fit["w"][p:]))
            explained = 1.0 - fit["sigma2"] / w_var if w_var > 0 else 0.0
            n_lb = len(fit["resid"])
            lb_lags = min(max(10, p + q + 2), n_lb // 3)
            qstat, pval = c.ljung_box(fit["resid"], lb_lags, model_df=p + q)
            extra = []
            if explained < 0.02:
                extra.append("the model explains less than 2% of the variance: ARIMA on raw returns carries no tradable signal")
            if not fit["stationary"]:
                extra.append("the autoregressive part is not stationary: lower p or raise d")
            if not fit["invertible"]:
                extra.append("the moving average part is not invertible")
            if pval < 0.05:
                extra.append("residuals are autocorrelated (Ljung-Box p < 0.05): the order is not enough")
            return ({"order": [p, d, q], "constant": fit["const"], "ar": fit["phi"], "ma": fit["theta"], "sigma2": fit["sigma2"],
                     "aic": fit["aic"], "bic": fit["bic"], "n_obs": fit["n_resid"], "explained_variance": explained,
                     "stationary": fit["stationary"], "invertible": fit["invertible"],
                     "ljung_box": {"q": qstat, "p_value": pval, "lags": lb_lags}, "forecast": arima_forecast(arr, fit, d, int(horizon))},
                    warns + tuple(extra))
        frame, warns = _frame(data)
        arr = frame.to_numpy()
        if lags < 1:
            raise c.Domain("lags must be at least 1")
        if kind == "vecm":
            if len(arr) < 40:
                raise c.Domain(f"the VECM needs at least 40 complete rows, got {len(arr)}")
            core = cointegration.vecm_core(arr, int(rank), int(lags) - 1)
            return ({"rank": int(rank), "k_ar_diff": int(lags) - 1, "alpha": core["alpha"], "beta": core["beta"], "gamma": core["gamma"],
                     "const": core["const"], "eigenvalues": core["eigenvalues"], "columns": [str(col) for col in frame.columns],
                     "forecast": core["forecast"](int(horizon))}, warns)
        fit = var_fit(arr, int(lags))
        extra = [] if fit["stable"] else ["the VAR is not stable (a root of the companion matrix has modulus >= 1): difference the data or lower the lags"]
        cols = [str(col) for col in frame.columns]
        base = {"columns": cols, "lags": int(lags), "n_obs": fit["n_obs"], "stable": fit["stable"], "max_root_modulus": fit["max_root"]}
        if kind == "var":
            return ({**base, "constant": fit["const"], "coefficients": fit["A"], "sigma_u": fit["sigma_u"], "aic": fit["aic"], "bic": fit["bic"],
                     "forecast": var_forecast(arr, fit, int(horizon))}, warns + tuple(extra))
        chol = np.linalg.cholesky(fit["sigma_u"])
        psi = ma_matrices(fit["A"], int(horizon))
        theta = [ps @ chol for ps in psi]
        extra.append("orthogonalized with the Cholesky factor in the column order of the data: the order is an assumption, test the reverse order")
        if kind == "irf":
            return ({**base, "orthogonalized": True, "irf": theta, "response_by_row_shock_by_column": True}, warns + tuple(extra))
        fevd = []
        for h in range(1, int(horizon) + 1):
            contrib = np.sum([th ** 2 for th in theta[:h]], axis=0)
            fevd.append(contrib / contrib.sum(axis=1, keepdims=True))
        return ({**base, "fevd": fevd, "share_of_row_variable_by_column_shock": True}, warns + tuple(extra))

    return c.run(name, unit="model", method=kind, params=params, piece_ids=pieces, compute=compute,
                 inputs=(data,) if isinstance(data, (pd.Series, pd.DataFrame)) else ())


# ---------------------------------------------------------------- Granger causality

def _aligned_pair(x: Any, y: Any) -> tuple[pd.Series, pd.Series, tuple[str, ...]]:
    sx, sy = c.helpers.as_series(x, name="x"), c.helpers.as_series(y, name="y")
    both_dates = isinstance(sx.index, pd.DatetimeIndex) and isinstance(sy.index, pd.DatetimeIndex)
    if not both_dates and len(sx) != len(sy):
        raise BadInput(f"x and y must have the same length, got {len(sx)} and {len(sy)}")
    joined = pd.concat([sx.rename("x"), sy.rename("y")], axis=1, join="inner" if both_dates else "outer")
    joined = joined.replace([np.inf, -np.inf], np.nan)
    cleaned = joined.dropna()
    warns = (f"dropped {len(joined) - len(cleaned)} row(s) with NaN or infinite values",) if len(cleaned) != len(joined) else ()
    return cleaned["x"], cleaned["y"], warns


def granger_f(x: np.ndarray, y: np.ndarray, lag: int) -> dict:
    """F test that `lag` lags of x add nothing to the regression of y on its own lags and a constant."""
    n = len(y)
    rows = np.arange(lag, n)
    own = [y[rows - i] for i in range(1, lag + 1)]
    cross = [x[rows - i] for i in range(1, lag + 1)]
    restricted = np.column_stack([np.ones(len(rows))] + own)
    unrestricted = np.column_stack([np.ones(len(rows))] + own + cross)
    dep = y[rows]
    fr, fu = econ.ols(dep, restricted), econ.ols(dep, unrestricted)
    rss_r, rss_u = float(fr.resid @ fr.resid), float(fu.resid @ fu.resid)
    if rss_u <= 0:
        raise c.Domain("the unrestricted model fits exactly: the test is undefined")
    df2 = fu.df
    f_stat = ((rss_r - rss_u) / lag) / (rss_u / df2)
    return {"lag": int(lag), "f_stat": float(f_stat), "p_value": float(_lib.sp_stats.f.sf(f_stat, lag, df2)), "df_num": int(lag), "df_den": int(df2)}


@registry.register("stats.granger", module="stats",
                   summary="Granger causality: does x help predict y beyond y's own lags. F test for each lag from 1 to max_lag, with a Bonferroni adjusted minimum p value. Both series should be stationary.",
                   inputs={"x": _SERIES, "y": _SERIES, "max_lag": {"type": "int", "required": False, "default": 4}}, piece_ids=("FT-087",))
def granger(x: Any, y: Any, max_lag: int = 4) -> QuantResult:
    name, pieces = "stats.granger", ("FT-087",)
    limits.check_param("max_lag", max_lag)
    limits.as_int("max_lag", max_lag)
    params = {"max_lag": int(max_lag)}

    def compute() -> Any:
        if max_lag < 1:
            raise c.Domain("max_lag must be at least 1")
        sx, sy, warns = _aligned_pair(x, y)
        if len(sx) < 3 * max_lag + 10:
            raise c.Domain(f"need at least {3 * max_lag + 10} complete pairs, got {len(sx)}")
        if c.is_constant(sx) or c.is_constant(sy):
            raise c.Domain("constant series: the test is undefined")
        ax, ay = sx.to_numpy(), sy.to_numpy()
        extra = []
        for label, arr in (("x", ax), ("y", ay)):
            adf = econ.adf_core(arr, "c")
            if not adf["statistic"] <= adf["critical_values"]["5%"]:
                extra.append(f"{label} does not reject a unit root: difference it first, Granger tests on unit root series mislead")
        rows = [granger_f(ax, ay, k) for k in range(1, int(max_lag) + 1)]
        best = min(rows, key=lambda r: r["p_value"])
        return ({"by_lag": rows, "min_p_value": best["p_value"], "best_lag": best["lag"],
                 "min_p_value_bonferroni": min(1.0, best["p_value"] * len(rows)), "direction": "x causes y"}, warns + tuple(extra))

    return c.run(name, unit="statistic", method="granger_f", params=params, piece_ids=pieces, compute=compute)


# ---------------------------------------------------------------- half life, breaks, intraday seasonality

@registry.register("stats.half_life", module="stats",
                   summary="Mean reversion half life of a series from the discrete AR(1): ln(2) / -ln(phi). None when phi is not in (0, 1): the series does not revert.",
                   inputs={"series": _SERIES}, piece_ids=("ECT-015",))
def half_life(series: Any) -> QuantResult:
    name, pieces = "stats.half_life", ("ECT-015",)

    def compute() -> Any:
        y, warns = c.clean_series(series, name="series", min_obs=20)
        if c.is_constant(y):
            raise c.Domain("constant series: dispersion is zero")
        arr = y.to_numpy()
        fit = econ.ols(arr[1:], np.column_stack([np.ones(len(arr) - 1), arr[:-1]]))
        phi = float(fit.beta[1])
        if not 0.0 < phi < 1.0:
            raise c.Domain(f"phi = {phi:.4f} is outside (0, 1): the series does not mean revert, there is no half life")
        theta = -math.log(phi)
        return ({"half_life": math.log(2.0) / theta, "phi": phi, "theta": theta, "phi_standard_error": float(fit.se[1]),
                 "mean_level": float(fit.beta[0] / (1.0 - phi))}, warns)

    return c.run(name, unit="periods", method="ar1_half_life", params={}, piece_ids=pieces, compute=compute,
                 inputs=(series,) if isinstance(series, pd.Series) else ())


def mean_shift_segmentation(y: np.ndarray, max_breaks: int, min_size: int) -> dict:
    """Least squares segmentation of the mean by dynamic programming; the number of breaks is chosen by BIC."""
    n = len(y)
    csum = np.concatenate([[0.0], np.cumsum(y)])
    csq = np.concatenate([[0.0], np.cumsum(y ** 2)])

    def cost(i: int, j: int) -> float:
        m = j - i
        s = csum[j] - csum[i]
        return float(csq[j] - csq[i] - s * s / m)

    inf = float("inf")
    best = [[inf] * (n + 1) for _ in range(max_breaks + 1)]
    arg = [[0] * (n + 1) for _ in range(max_breaks + 1)]
    for j in range(min_size, n + 1):
        best[0][j] = cost(0, j)
    for b in range(1, max_breaks + 1):
        for j in range((b + 1) * min_size, n + 1):
            for i in range(b * min_size, j - min_size + 1):
                v = best[b - 1][i] + cost(i, j)
                if v < best[b][j]:
                    best[b][j], arg[b][j] = v, i
    results = []
    for b in range(0, max_breaks + 1):
        if best[b][n] == inf:
            continue
        rss = max(best[b][n], 1e-300)
        bic = n * math.log(rss / n) + (2 * b + 1) * math.log(n)
        points, j = [], n
        for bb in range(b, 0, -1):
            j = arg[bb][j]
            points.append(j)
        results.append({"n_breaks": b, "break_indices": sorted(points), "rss": best[b][n], "bic": bic})
    chosen = min(results, key=lambda r: r["bic"])
    return {"chosen": chosen, "candidates": results}


BREAK_METHODS = ("chow", "cusum", "mean_shift")


@registry.register("stats.structural_breaks", module="stats",
                   summary="Structural break tests: chow (AR(1) fit with a break at break_index, or the sup F scan when none is given), cusum (OLS-CUSUM stability of the mean) and mean_shift (least squares segmentation of the mean, breaks chosen by BIC).",
                   inputs={"series": _SERIES, "method": {"type": "str", "required": False, "default": "cusum"},
                           "break_index": {"type": "int", "required": False, "default": None},
                           "max_breaks": {"type": "int", "required": False, "default": 3}}, piece_ids=("ECT-015",))
def structural_breaks(series: Any, method: str = "cusum", break_index: int | None = None, max_breaks: int = 3) -> QuantResult:
    name, pieces = "stats.structural_breaks", ("ECT-015",)
    if break_index is not None:
        limits.as_int("break_index", break_index)
    limits.as_int("max_breaks", max_breaks)
    params = {"method": method, "break_index": break_index, "max_breaks": int(max_breaks)}

    def compute() -> Any:
        if method not in BREAK_METHODS:
            raise c.Domain(f"method must be one of {list(BREAK_METHODS)}")
        y, warns = c.clean_series(series, name="series", min_obs=30)
        if c.is_constant(y):
            raise c.Domain("constant series: dispersion is zero")
        arr = y.to_numpy()
        n = len(arr)
        if method == "cusum":
            resid = arr - arr.mean()
            sigma = float(np.sqrt(np.sum(resid ** 2) / (n - 1)))
            path = np.cumsum(resid) / (sigma * math.sqrt(n))
            stat = float(np.max(np.abs(path)))
            crit = {"10%": 1.224, "5%": 1.358, "1%": 1.628}
            return ({"statistic": stat, "critical_values": crit, "reject_stability_5pct": stat > crit["5%"],
                     "peak_index": int(np.argmax(np.abs(path))), "null_hypothesis": "the mean is stable"}, warns)
        if method == "mean_shift":
            if not 1 <= max_breaks <= 10:
                raise c.Domain("max_breaks must be between 1 and 10")
            if n > MAX_DP_OBS:
                raise c.Domain(f"the segmentation needs at most {MAX_DP_OBS} observations")
            min_size = max(int(0.1 * n), 5)
            return mean_shift_segmentation(arr, int(max_breaks), min_size), warns
        # chow on y_t = a + b y_{t-1}
        rows = np.arange(1, n)
        x = np.column_stack([np.ones(len(rows)), arr[rows - 1]])
        dep = arr[rows]
        k = 2
        m = len(rows)
        full = econ.ols(dep, x)
        rss_full = float(full.resid @ full.resid)

        def f_at(split: int) -> float:
            left, right = slice(0, split), slice(split, m)
            fl, fr = econ.ols(dep[left], x[left]), econ.ols(dep[right], x[right])
            rss_split = float(fl.resid @ fl.resid) + float(fr.resid @ fr.resid)
            return ((rss_full - rss_split) / k) / (rss_split / (m - 2 * k))

        if break_index is not None:
            split = int(break_index) - 1
            if not k + 1 < split < m - k - 1:
                raise c.Domain("break_index leaves too few observations on one side")
            f_val = f_at(split)
            return {"f_stat": f_val, "p_value": float(_lib.sp_stats.f.sf(f_val, k, m - 2 * k)), "break_index": int(break_index), "df_num": k, "df_den": m - 2 * k}, warns
        lo, hi = int(0.15 * m), int(0.85 * m)
        scan = [(f_at(s), s) for s in range(lo, hi)]
        best = max(scan)
        return ({"sup_f": best[0], "break_index": int(best[1]) + 1, "n_candidates": len(scan),
                 "note": "supremum over many split points: the F distribution does not apply, use the sup F critical values of the literature"}, warns)

    return c.run(name, unit="statistic", method=method, params=params, piece_ids=pieces, compute=compute,
                 inputs=(series,) if isinstance(series, pd.Series) else ())


@registry.register("stats.intraday_seasonality", module="stats",
                   summary="Time of day pattern of intraday volatility (mean absolute return by bar time) and the returns with it removed. Run it before any GARCH fit on minute bars. Needs a datetime index.",
                   inputs={"returns": _SERIES}, piece_ids=("ECT-015",))
def intraday_seasonality(returns: Any) -> QuantResult:
    name, pieces = "stats.intraday_seasonality", ("ECT-015",)

    def compute() -> Any:
        series = c.helpers.as_series(returns, name="returns")
        if not isinstance(series.index, pd.DatetimeIndex):
            raise BadInput("returns need a datetime index")
        cleaned, warns = c.clean_series(series, name="returns", min_obs=50)
        absolute = cleaned.abs()
        slots = cleaned.index.strftime("%H:%M")
        by_slot = absolute.groupby(slots).mean()
        overall = float(absolute.mean())
        if overall == 0.0:
            raise c.Domain("returns are all zero")
        factors = by_slot / overall
        if (factors <= 0).any():
            raise c.Domain("a time of day has no movement: the factor would be zero")
        adjusted = cleaned / factors.reindex(slots).to_numpy()
        ordered = factors.sort_index()
        edge = max(1, len(ordered) // 10)
        u_shape = bool(ordered.iloc[:edge].mean() > ordered.iloc[edge:-edge].mean() and ordered.iloc[-edge:].mean() > ordered.iloc[edge:-edge].mean()) if len(ordered) > 4 * edge else None
        return ({"factors": {str(k): float(v) for k, v in ordered.items()}, "u_shape": u_shape,
                 "adjusted_index": [str(i) for i in adjusted.index], "adjusted_returns": adjusted.to_numpy()}, warns)

    return c.run(name, unit="ratio", method="mean_absolute_return_by_slot", params={}, piece_ids=pieces, compute=compute,
                 inputs=(returns,) if isinstance(returns, pd.Series) else ())

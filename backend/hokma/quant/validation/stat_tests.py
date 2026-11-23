"""Statistical tests for the court of a desk: residual diagnostics, specification, hypothesis and forecast comparison.

SPEC-017 rows FT-084, FT-085, FT-086, FT-088. Every test is a plain function returning a dict with the statistic and
its p-value; four registered functions expose them by name to agents. Bad data gives value None with a warning.
"""
from __future__ import annotations

import itertools
import math
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import helpers, registry
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult
from hokma.quant.validation import _lib
from hokma.quant.validation._checks import clean_returns


# --------------------------------------------------------------------------- linear algebra helpers

def _vector(values: Any, name: str) -> np.ndarray:
    return helpers.as_series(values, name=name).to_numpy(dtype=float)


def _design(x: Any, name: str = "x") -> tuple[np.ndarray, list[str]]:
    if isinstance(x, pd.DataFrame):
        return x.to_numpy(dtype=float), [str(c) for c in x.columns]
    if isinstance(x, pd.Series):
        return x.to_numpy(dtype=float).reshape(-1, 1), [str(x.name or name)]
    arr = np.asarray(x, dtype=float) if isinstance(x, (list, tuple, np.ndarray)) else None
    if arr is None:
        raise BadInput(f"{name}: expected a DataFrame, Series or array, got {type(x).__name__}")
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    if arr.ndim != 2:
        raise BadInput(f"{name}: expected one or two dimensions")
    return arr, [f"{name}{i}" for i in range(arr.shape[1])]


def _with_const(x: np.ndarray) -> np.ndarray:
    return np.column_stack([np.ones(len(x)), x])


def ols(y: np.ndarray, x: np.ndarray, *, add_const: bool = True) -> dict:
    """Least squares fit: coefficients, residuals, sums of squares, R squared and (X'X)^-1."""
    design = _with_const(x) if add_const else x
    n, k = design.shape
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    fitted = design @ beta
    resid = y - fitted
    ssr = float(resid @ resid)
    centered = y - y.mean()
    tss = float(centered @ centered)
    return {"beta": beta, "resid": resid, "fitted": fitted, "n": n, "k": k, "ssr": ssr, "tss": tss,
            "r2": (1.0 - ssr / tss) if tss > 0 else float("nan"), "xtx_inv": np.linalg.pinv(design.T @ design), "design": design}


def _lagged(values: np.ndarray, lags: int) -> tuple[np.ndarray, np.ndarray]:
    y = values[lags:]
    x = np.column_stack([values[lags - j: len(values) - j] for j in range(1, lags + 1)])
    return y, x


def _test(statistic: float, p_value: float, **extra: Any) -> dict:
    return {"statistic": float(statistic), "p_value": float(p_value), **extra}


# --------------------------------------------------------------------------- residual diagnostics (FT-084)

def arch_lm(residuals: np.ndarray, lags: int = 5) -> dict:
    """Engle LM test for conditional heteroskedasticity: (n - lags) * R^2 of e^2 on its own lags, chi2(lags)."""
    sq = np.asarray(residuals, dtype=float) ** 2
    y, x = _lagged(sq, lags)
    fit = ols(y, x)
    lm = len(y) * fit["r2"]
    return _test(lm, _lib.chi2_sf(lm, lags), df=lags)


def ljung_box(values: np.ndarray, lags: int = 10) -> dict:
    """Ljung-Box Q statistic for autocorrelation up to `lags`, chi2(lags)."""
    x = np.asarray(values, dtype=float)
    n = len(x)
    centered = x - x.mean()
    denom = float(centered @ centered)
    rho = np.array([float(centered[:-k] @ centered[k:]) / denom for k in range(1, lags + 1)])
    q = n * (n + 2) * float(np.sum(rho ** 2 / (n - np.arange(1, lags + 1))))
    return _test(q, _lib.chi2_sf(q, lags), df=lags)


def jarque_bera(values: np.ndarray) -> dict:
    stat, p = _lib.jarque_bera(np.asarray(values, dtype=float))
    return _test(stat, p)


def variance_ratio(returns: np.ndarray, q: int = 5) -> dict:
    """Lo-MacKinlay variance ratio with the homoskedastic z statistic (null: random walk, ratio 1)."""
    r = np.asarray(returns, dtype=float)
    n = len(r)
    mu = r.mean()
    var1 = float(np.sum((r - mu) ** 2)) / (n - 1)
    sums = np.convolve(r, np.ones(q), mode="valid")
    m = q * (n - q + 1) * (1.0 - q / n)
    varq = float(np.sum((sums - q * mu) ** 2)) / m
    ratio = varq / var1
    z = (ratio - 1.0) / math.sqrt(2.0 * (2 * q - 1) * (q - 1) / (3.0 * q * n))
    return _test(z, 2.0 * (1.0 - _lib.norm_cdf(abs(z))), variance_ratio=float(ratio), q=q)


def cusum(residuals: np.ndarray) -> dict:
    """OLS-CUSUM stability test: sup of the scaled partial sums of residuals; p-value from the Kolmogorov law."""
    e = np.asarray(residuals, dtype=float)
    n = len(e)
    sigma = float(e.std(ddof=1))
    path = np.cumsum(e - e.mean()) / (sigma * math.sqrt(n))
    stat = float(np.max(np.abs(path)))
    p = 2.0 * sum((-1) ** (j + 1) * math.exp(-2.0 * j * j * stat * stat) for j in range(1, 101))
    return _test(stat, min(1.0, max(0.0, p)))


_RESIDUAL_TESTS = ("arch_lm", "jarque_bera", "ljung_box", "variance_ratio", "cusum")


@registry.register(
    "validation.residual_diagnostics", module="validation",
    summary="Residual diagnostics: ARCH-LM, Jarque-Bera, Ljung-Box, variance ratio and CUSUM (statistic and p-value each).",
    inputs={"residuals": {"type": "series", "required": True}, "tests": {"type": "list", "required": False, "default": None},
            "lags": {"type": "int", "required": False, "default": 5}, "vr_q": {"type": "int", "required": False, "default": 5}},
    piece_ids=("FT-084",),
)
def residual_diagnostics(residuals: Any, tests: list[str] | None = None, lags: int = 5, vr_q: int = 5) -> QuantResult:
    name = "validation.residual_diagnostics"
    series, warnings, degenerate = clean_returns(residuals, min_obs=max(30, 3 * lags, 3 * vr_q), name="residuals")
    if degenerate:
        return QuantResult.empty(name, "statistic", "residual_diagnostics", warnings[-1])
    chosen = list(tests) if tests else list(_RESIDUAL_TESTS)
    unknown = [t for t in chosen if t not in _RESIDUAL_TESTS]
    if unknown:
        raise BadInput(f"unknown test(s) {unknown}; use {list(_RESIDUAL_TESTS)}")
    values = series.to_numpy()
    runners = {"arch_lm": lambda: arch_lm(values, lags), "jarque_bera": lambda: jarque_bera(values),
               "ljung_box": lambda: ljung_box(values, max(lags, 1)), "variance_ratio": lambda: variance_ratio(values, vr_q),
               "cusum": lambda: cusum(values)}
    return QuantResult(name=name, value={t: runners[t]() for t in chosen}, unit="statistic", method="residual_diagnostics",
                       params={"lags": lags, "vr_q": vr_q, "n": int(len(series))}, inputs_hash=helpers.hash_inputs(series, chosen, lags, vr_q),
                       as_of=helpers.last_as_of(series), warnings=warnings, piece_ids=("FT-084",))


# --------------------------------------------------------------------------- specification tests (FT-085)

def durbin_watson(residuals: np.ndarray) -> dict:
    e = np.asarray(residuals, dtype=float)
    stat = float(np.sum(np.diff(e) ** 2) / np.sum(e ** 2))
    return {"statistic": stat, "p_value": None, "note": "near 2 means no first order autocorrelation"}


def breusch_pagan(y: np.ndarray, x: np.ndarray) -> dict:
    """Koenker form: n * R^2 of e^2 on the regressors, chi2(k)."""
    fit = ols(y, x)
    aux = ols(fit["resid"] ** 2, x)
    lm = fit["n"] * aux["r2"]
    return _test(lm, _lib.chi2_sf(lm, x.shape[1]), df=x.shape[1])


def white_test(y: np.ndarray, x: np.ndarray) -> dict:
    """White test: n * R^2 of e^2 on the regressors, their squares and cross products."""
    fit = ols(y, x)
    cols = [x[:, i] for i in range(x.shape[1])]
    terms = list(cols) + [c * c for c in cols] + [a * b for a, b in itertools.combinations(cols, 2)]
    aux_x = np.column_stack(terms)
    keep = [i for i in range(aux_x.shape[1]) if aux_x[:, i].std() > 0]
    aux = ols(fit["resid"] ** 2, aux_x[:, keep])
    lm = fit["n"] * aux["r2"]
    return _test(lm, _lib.chi2_sf(lm, len(keep)), df=len(keep))


def vif(x: np.ndarray, names: list[str]) -> dict:
    """Variance inflation factor of each column: 1 / (1 - R^2) of the column on the others."""
    out = {}
    for i, col in enumerate(names):
        others = np.delete(x, i, axis=1)
        out[col] = float(1.0 / (1.0 - ols(x[:, i], others)["r2"])) if others.shape[1] else 1.0
    return {"statistic": out, "p_value": None}


def ramsey_reset(y: np.ndarray, x: np.ndarray, power: int = 3) -> dict:
    """RESET: F test that powers 2..power of the fitted values add nothing to the regression."""
    base = ols(y, x)
    extra = np.column_stack([base["fitted"] ** p for p in range(2, power + 1)])
    full = ols(y, np.column_stack([x, extra]))
    q = power - 1
    df2 = full["n"] - full["k"]
    f = ((base["ssr"] - full["ssr"]) / q) / (full["ssr"] / df2)
    return _test(f, _lib.f_sf(f, q, df2), df1=q, df2=df2)


def chow_test(y: np.ndarray, x: np.ndarray, break_index: int) -> dict:
    """Chow F test for a structural break at the positional `break_index`."""
    pooled = ols(y, x)
    a, b = ols(y[:break_index], x[:break_index]), ols(y[break_index:], x[break_index:])
    k = pooled["k"]
    df2 = pooled["n"] - 2 * k
    f = ((pooled["ssr"] - a["ssr"] - b["ssr"]) / k) / ((a["ssr"] + b["ssr"]) / df2)
    return _test(f, _lib.f_sf(f, k, df2), df1=k, df2=df2)


_SPEC_TESTS = ("breusch_pagan", "white", "durbin_watson", "vif", "reset", "chow")


@registry.register(
    "validation.specification_tests", module="validation",
    summary="Model specification: Breusch-Pagan, White, Durbin-Watson, VIF, Ramsey RESET and Chow for y on x with an intercept.",
    inputs={"y": {"type": "series", "required": True}, "x": {"type": "dataframe", "required": True},
            "tests": {"type": "list", "required": False, "default": None}, "break_index": {"type": "int", "required": False, "default": None}},
    piece_ids=("FT-085",),
)
def specification_tests(y: Any, x: Any, tests: list[str] | None = None, break_index: int | None = None) -> QuantResult:
    name = "validation.specification_tests"
    yv = _vector(y, "y")
    xm, names = _design(x)
    if len(yv) != len(xm):
        raise BadInput("y and x must have the same number of rows")
    chosen = list(tests) if tests else [t for t in _SPEC_TESTS if t != "chow" or break_index is not None]
    unknown = [t for t in chosen if t not in _SPEC_TESTS]
    if unknown:
        raise BadInput(f"unknown test(s) {unknown}; use {list(_SPEC_TESTS)}")
    mask = np.isfinite(yv) & np.isfinite(xm).all(axis=1)
    warnings: list[str] = []
    if not mask.all():
        warnings.append(f"dropped {int((~mask).sum())} row(s) with NaN or infinite values")
    yv, xm = yv[mask], xm[mask]
    if len(yv) < xm.shape[1] + 12 or np.any(np.ptp(xm, axis=0) == 0) or np.ptp(yv) == 0:
        return QuantResult.empty(name, "statistic", "specification_tests", "insufficient or constant data for a regression")
    if "chow" in chosen and (break_index is None or not xm.shape[1] + 1 < break_index < len(yv) - xm.shape[1] - 1):
        raise BadInput("chow needs a break_index with enough observations on both sides")
    resid = ols(yv, xm)["resid"]
    runners = {"breusch_pagan": lambda: breusch_pagan(yv, xm), "white": lambda: white_test(yv, xm),
               "durbin_watson": lambda: durbin_watson(resid), "vif": lambda: vif(xm, names),
               "reset": lambda: ramsey_reset(yv, xm), "chow": lambda: chow_test(yv, xm, int(break_index or 0))}
    return QuantResult(name=name, value={t: runners[t]() for t in chosen}, unit="statistic", method="specification_tests",
                       params={"n": int(len(yv)), "break_index": break_index}, inputs_hash=helpers.hash_inputs(pd.Series(yv), pd.DataFrame(xm), chosen),
                       warnings=tuple(warnings), piece_ids=("FT-085",))


# --------------------------------------------------------------------------- hypothesis tests (FT-086)

def two_sample_t(a: np.ndarray, b: np.ndarray, equal_var: bool = False) -> dict:
    stat, p = _lib.two_sample_t(a, b, equal_var)
    return _test(stat, p, equal_var=equal_var)


def f_test(y: np.ndarray, x_restricted: np.ndarray, x_unrestricted: np.ndarray) -> dict:
    """Nested model F test; x_restricted must be a subset of the columns of x_unrestricted."""
    r, u = ols(y, x_restricted), ols(y, x_unrestricted)
    q = u["k"] - r["k"]
    if q < 1:
        raise BadInput("the unrestricted model must have more regressors than the restricted one")
    df2 = u["n"] - u["k"]
    f = ((r["ssr"] - u["ssr"]) / q) / (u["ssr"] / df2)
    return _test(f, _lib.f_sf(f, q, df2), df1=q, df2=df2)


def wald_test(y: np.ndarray, x: np.ndarray, restriction: np.ndarray, rhs: np.ndarray) -> dict:
    """Wald chi2 test of R beta = q on the OLS coefficients (intercept first in beta)."""
    fit = ols(y, x)
    r = np.atleast_2d(np.asarray(restriction, dtype=float))
    q = np.atleast_1d(np.asarray(rhs, dtype=float))
    if r.shape[1] != fit["k"] or r.shape[0] != len(q):
        raise BadInput("restriction must have one column per coefficient (intercept first) and one row per right hand side value")
    cov = fit["ssr"] / (fit["n"] - fit["k"]) * fit["xtx_inv"]
    diff = r @ fit["beta"] - q
    w = float(diff @ np.linalg.solve(r @ cov @ r.T, diff))
    return _test(w, _lib.chi2_sf(w, r.shape[0]), df=r.shape[0])


def likelihood_ratio_test(y: np.ndarray, x_restricted: np.ndarray, x_unrestricted: np.ndarray) -> dict:
    """Gaussian likelihood ratio test of nested models: n * (ln SSR_r - ln SSR_u), chi2(q)."""
    r, u = ols(y, x_restricted), ols(y, x_unrestricted)
    q = u["k"] - r["k"]
    if q < 1:
        raise BadInput("the unrestricted model must have more regressors than the restricted one")
    lr = r["n"] * (math.log(r["ssr"]) - math.log(u["ssr"]))
    return _test(lr, _lib.chi2_sf(lr, q), df=q)


def hausman_wu_test(y: np.ndarray, x_suspect: np.ndarray, x_exog: np.ndarray | None, instruments: np.ndarray) -> dict:
    """Regression based Durbin-Wu-Hausman: t test on the first stage residual when added to the structural equation."""
    first_x = instruments if x_exog is None else np.column_stack([x_exog, instruments])
    first = ols(x_suspect, first_x)
    structural_x = first["resid"].reshape(-1, 1)
    structural_x = np.column_stack([x_suspect] + ([x_exog] if x_exog is not None else []) + [structural_x])
    fit = ols(y, structural_x)
    df = fit["n"] - fit["k"]
    se = math.sqrt(fit["ssr"] / df * fit["xtx_inv"][-1, -1])
    t = float(fit["beta"][-1] / se)
    return _test(t, _lib.t_sf(t, df), df=df)


_HYPOTHESIS_TESTS = ("two_sample_t", "f", "wald", "likelihood_ratio", "hausman_wu")


@registry.register(
    "validation.hypothesis_tests", module="validation",
    summary="Hypothesis tests: two sample t, nested F, Wald, likelihood ratio and Hausman-Wu (name in test; inputs by test).",
    inputs={"test": {"type": "str", "required": True}, "y": {"type": "series", "required": False, "default": None},
            "x_restricted": {"type": "dataframe", "required": False, "default": None}, "x": {"type": "dataframe", "required": False, "default": None},
            "sample_a": {"type": "series", "required": False, "default": None}, "sample_b": {"type": "series", "required": False, "default": None},
            "restriction": {"type": "matrix", "required": False, "default": None}, "rhs": {"type": "list", "required": False, "default": None},
            "x_suspect": {"type": "series", "required": False, "default": None}, "x_exog": {"type": "dataframe", "required": False, "default": None},
            "instruments": {"type": "dataframe", "required": False, "default": None}, "equal_var": {"type": "bool", "required": False, "default": False}},
    piece_ids=("FT-086",),
)
def hypothesis_tests(test: str, y: Any = None, x_restricted: Any = None, x: Any = None, sample_a: Any = None, sample_b: Any = None,
                     restriction: Any = None, rhs: Any = None, x_suspect: Any = None, x_exog: Any = None, instruments: Any = None,
                     equal_var: bool = False) -> QuantResult:
    name = "validation.hypothesis_tests"
    if test not in _HYPOTHESIS_TESTS:
        raise BadInput(f"unknown test {test!r}; use {list(_HYPOTHESIS_TESTS)}")

    def need(**given: Any) -> None:
        missing = [k for k, v in given.items() if v is None]
        if missing:
            raise BadInput(f"{test} needs {missing}")

    try:
        if test == "two_sample_t":
            need(sample_a=sample_a, sample_b=sample_b)
            a, b = helpers.clean(helpers.as_series(sample_a, name="sample_a")).to_numpy(), helpers.clean(helpers.as_series(sample_b, name="sample_b")).to_numpy()
            if len(a) < 3 or len(b) < 3 or a.std() == 0 and b.std() == 0:
                return QuantResult.empty(name, "statistic", test, "need at least 3 non constant observations per sample")
            value = two_sample_t(a, b, equal_var)
        else:
            need(y=y)
            yv = _vector(y, "y")
            if test in ("f", "likelihood_ratio"):
                need(x_restricted=x_restricted, x=x)
                xr, xu = _design(x_restricted, "x_restricted")[0], _design(x)[0]
                value = (f_test if test == "f" else likelihood_ratio_test)(yv, xr, xu)
            elif test == "wald":
                need(x=x, restriction=restriction, rhs=rhs)
                value = wald_test(yv, _design(x)[0], np.asarray(restriction, dtype=float), np.asarray(rhs, dtype=float))
            else:
                need(x_suspect=x_suspect, instruments=instruments)
                value = hausman_wu_test(yv, _vector(x_suspect, "x_suspect"), None if x_exog is None else _design(x_exog, "x_exog")[0],
                                        _design(instruments, "instruments")[0])
    except np.linalg.LinAlgError:
        return QuantResult.empty(name, "statistic", test, "singular design matrix")
    if any(isinstance(v, float) and not math.isfinite(v) for v in value.values()):
        return QuantResult.empty(name, "statistic", test, "the statistic is not finite (degenerate or perfectly fitting data)")
    return QuantResult(name=name, value=value, unit="statistic", method=test, params={"test": test},
                       inputs_hash=helpers.hash_inputs([test, str(equal_var)]), piece_ids=("FT-086",))


# --------------------------------------------------------------------------- forecast evaluation (FT-088)

def diebold_mariano(errors_a: np.ndarray, errors_b: np.ndarray, h: int = 1, loss: str = "squared") -> dict:
    """Diebold-Mariano test of equal predictive accuracy with the small sample correction; positive favours model B."""
    a, b = np.asarray(errors_a, dtype=float), np.asarray(errors_b, dtype=float)
    d = (a ** 2 - b ** 2) if loss == "squared" else (np.abs(a) - np.abs(b))
    n = len(d)
    centered = d - d.mean()
    lrv = float(centered @ centered) / n
    for lag in range(1, h):
        lrv += 2.0 * (1.0 - lag / h) * float(centered[lag:] @ centered[:-lag]) / n
    if not lrv > 0:
        return {"statistic": None, "p_value": None, "note": "no dispersion in the loss differential"}
    dm = d.mean() / math.sqrt(lrv / n)
    correction = math.sqrt((n + 1 - 2 * h + h * (h - 1) / n) / n)
    stat = dm * correction
    return _test(stat, _lib.t_sf(stat, n - 1), mean_loss_differential=float(d.mean()), h=h, loss=loss)


def forecast_accuracy(actual: np.ndarray, forecast: np.ndarray, oos_start: int | None = None) -> dict:
    err = np.asarray(actual, dtype=float) - np.asarray(forecast, dtype=float)
    out = {"rmse": float(np.sqrt(np.mean(err ** 2))), "mae": float(np.mean(np.abs(err))), "n": int(len(err))}
    if oos_start is not None:
        oos, ins = err[oos_start:], err[:oos_start]
        out.update({"rmse_in_sample": float(np.sqrt(np.mean(ins ** 2))), "mae_in_sample": float(np.mean(np.abs(ins))),
                    "rmse_out_of_sample": float(np.sqrt(np.mean(oos ** 2))), "mae_out_of_sample": float(np.mean(np.abs(oos))), "n_out_of_sample": int(len(oos))})
    return out


@registry.register(
    "validation.forecast_tests", module="validation",
    summary="Forecast comparison: Diebold-Mariano on two error series, or RMSE and MAE (with an out of sample split) of one forecast.",
    inputs={"test": {"type": "str", "required": True}, "errors_a": {"type": "series", "required": False, "default": None},
            "errors_b": {"type": "series", "required": False, "default": None}, "actual": {"type": "series", "required": False, "default": None},
            "forecast": {"type": "series", "required": False, "default": None}, "h": {"type": "int", "required": False, "default": 1},
            "loss": {"type": "str", "required": False, "default": "squared"}, "oos_start": {"type": "int", "required": False, "default": None}},
    piece_ids=("FT-088",),
)
def forecast_tests(test: str, errors_a: Any = None, errors_b: Any = None, actual: Any = None, forecast: Any = None, h: int = 1,
                   loss: str = "squared", oos_start: int | None = None) -> QuantResult:
    name = "validation.forecast_tests"
    if test not in ("diebold_mariano", "accuracy"):
        raise BadInput("test must be 'diebold_mariano' or 'accuracy'")
    if loss not in ("squared", "absolute"):
        raise BadInput("loss must be 'squared' or 'absolute'")
    if test == "diebold_mariano":
        if errors_a is None or errors_b is None:
            raise BadInput("diebold_mariano needs errors_a and errors_b")
        a, b = _vector(errors_a, "errors_a"), _vector(errors_b, "errors_b")
        if len(a) != len(b):
            raise BadInput("errors_a and errors_b must have the same length")
        mask = np.isfinite(a) & np.isfinite(b)
        if mask.sum() < max(10, 3 * h):
            return QuantResult.empty(name, "statistic", "diebold_mariano", "need at least 10 paired finite errors")
        value = diebold_mariano(a[mask], b[mask], h, loss)
        if value["statistic"] is None:
            return QuantResult.empty(name, "statistic", "diebold_mariano", value["note"])
        return QuantResult(name=name, value=value, unit="statistic", method="diebold_mariano", params={"h": h, "loss": loss, "n": int(mask.sum())},
                           inputs_hash=helpers.hash_inputs(pd.Series(a[mask]), pd.Series(b[mask]), h, loss), piece_ids=("FT-088",))
    if actual is None or forecast is None:
        raise BadInput("accuracy needs actual and forecast")
    act, fc = _vector(actual, "actual"), _vector(forecast, "forecast")
    if len(act) != len(fc):
        raise BadInput("actual and forecast must have the same length")
    mask = np.isfinite(act) & np.isfinite(fc)
    if mask.sum() < 2:
        return QuantResult.empty(name, "statistic", "accuracy", "need at least 2 paired finite values")
    if oos_start is not None and not 1 <= oos_start < int(mask.sum()):
        raise BadInput("oos_start must leave observations on both sides")
    return QuantResult(name=name, value=forecast_accuracy(act[mask], fc[mask], oos_start), unit="statistic", method="accuracy",
                       params={"oos_start": oos_start, "n": int(mask.sum())}, inputs_hash=helpers.hash_inputs(pd.Series(act[mask]), pd.Series(fc[mask]), oos_start),
                       piece_ids=("FT-088",))

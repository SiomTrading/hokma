"""Cointegration (FT-081): Engle-Granger and Johansen tests, the VECM behind them and the hedge amounts of a pair.

Written on numpy and scipy. Engle-Granger uses the published asymptotic critical values of the residual based test (not
the plain Dickey-Fuller ones, which would reject too often because the hedge ratio is estimated). The Johansen critical values
are simulated with a fixed seed at the size of the data.
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
from hokma.quant.types import QuantResult

_SERIES = {"type": "series", "required": True}
MAX_SIMULATIONS = 5000
MAX_PAIR_TESTS = 1_000_000


def _half_life_of(spread: np.ndarray) -> float | None:
    fit = econ.ols(spread[1:], np.column_stack([np.ones(len(spread) - 1), spread[:-1]]))
    phi = float(fit.beta[1])
    return math.log(2.0) / -math.log(phi) if 0.0 < phi < 1.0 else None


@registry.register("stats.engle_granger", module="stats",
                   summary="Engle-Granger cointegration test of y against x: hedge ratio, spread, ADF statistic on the spread with the residual based critical values, and the half life of the spread. Declare ratio_basis (log or level) and how many pairs were tested.",
                   inputs={"y": _SERIES, "x": {"type": "series or table", "required": True}, "lags": {"type": "int", "required": False, "default": None},
                           "ratio_basis": {"type": "str", "required": False, "default": "level"},
                           "n_tested": {"type": "int", "required": False, "default": 1}}, piece_ids=("FT-081", "ECT-015"))
def engle_granger(y: Any, x: Any, lags: int | None = None, ratio_basis: str = "level", n_tested: int = 1) -> QuantResult:
    name, pieces = "stats.engle_granger", ("FT-081", "ECT-015")
    limits.check_param("lags", lags)
    if lags is not None:
        limits.as_int("lags", lags)
    limits.as_int("n_tested", n_tested)
    params = {"lags": lags, "ratio_basis": ratio_basis, "n_tested": int(n_tested)}

    def compute() -> Any:
        if ratio_basis not in ("level", "log"):
            raise c.Domain("ratio_basis must be level or log: say whether the series are prices or log prices")
        if not 1 <= n_tested <= MAX_PAIR_TESTS:
            raise c.Domain(f"n_tested must be between 1 and {MAX_PAIR_TESTS}")
        sy = c.helpers.as_series(y, name="y")
        if isinstance(x, pd.DataFrame):
            xf = x.astype(float)
        else:
            xf = c.helpers.as_series(x, name="x").to_frame("x")
        if len(xf) != len(sy):
            raise BadInput(f"y and x must have the same length, got {len(sy)} and {len(xf)}")
        joined = pd.concat([sy.rename("y").reset_index(drop=True), xf.reset_index(drop=True)], axis=1).replace([np.inf, -np.inf], np.nan)
        cleaned = joined.dropna()
        warns: list[str] = []
        if len(cleaned) != len(joined):
            warns.append(f"dropped {len(joined) - len(cleaned)} row(s) with NaN or infinite values")
        n_vars = cleaned.shape[1]
        if n_vars not in econ._EG_ASYMPTOTIC:
            raise c.Domain("Engle-Granger critical values exist for 2 to 6 variables")
        if len(cleaned) < 30:
            raise c.Domain(f"need at least 30 complete rows, got {len(cleaned)}")
        if any(c.is_constant(cleaned[col]) for col in cleaned.columns):
            raise c.Domain("constant series: the test is undefined")
        yy = cleaned["y"].to_numpy()
        xx = cleaned.drop(columns="y").to_numpy()
        reg = econ.ols(yy, np.column_stack([np.ones(len(yy)), xx]))
        spread = reg.resid
        core = econ.adf_core(spread, "n", lags)
        crit = econ._EG_ASYMPTOTIC[n_vars]
        stat = core["statistic"]
        tested = int(n_tested)
        if tested > 1:
            warns.append(f"{tested} pairs tested: at the 5% level about {0.05 * tested:.1f} would pass by chance; correct for the number of pairs or validate out of sample")
        warns.append("asymptotic critical values of the residual based test; the sample is finite")
        if ratio_basis == "log":
            warns.append("the hedge ratio is on log prices: it is an elasticity, not a share ratio; use stats.hedge_amounts for the dollar legs")
        return ({"statistic": stat, "critical_values": crit, "p_value_band": econ.band(stat, crit), "cointegrated_5pct": stat <= crit["5%"],
                 "intercept": float(reg.beta[0]), "hedge_ratio": reg.beta[1:] if xx.shape[1] > 1 else float(reg.beta[1]),
                 "ratio_basis": ratio_basis, "adf_lags": core["lags"], "n_obs": len(yy), "r2": reg.r2,
                 "spread_mean": float(spread.mean()), "spread_std": float(spread.std(ddof=1)), "spread_half_life": _half_life_of(spread),
                 "n_tested": tested, "expected_false_positives_5pct": 0.05 * tested}, tuple(warns))

    return c.run(name, unit="statistic", method="engle_granger", params=params, piece_ids=pieces, compute=compute)


def vecm_core(data: np.ndarray, rank: int, k_ar_diff: int) -> dict:
    """Vector error correction model by the Johansen procedure with an unrestricted constant."""
    n_vars = data.shape[1]
    if not 1 <= rank < n_vars:
        raise c.Domain(f"rank must be between 1 and {n_vars - 1}")
    core = econ.johansen_core(data, k_ar_diff)
    v = core["beta"][:, :rank]
    beta = v @ np.linalg.inv(v[:rank, :rank])
    alpha = core["s01"] @ beta @ np.linalg.inv(beta.T @ core["s11"] @ beta)
    dy = np.diff(data, axis=0)
    y0 = core["y0"]
    y1 = core["y1"]
    z = core["z"]
    target = y0 - y1 @ beta @ alpha.T
    coefs = np.linalg.lstsq(z, target, rcond=None)[0]
    const = coefs[0]
    gamma = [coefs[1 + i * n_vars: 1 + (i + 1) * n_vars].T for i in range(k_ar_diff)]

    def forecast(horizon: int) -> np.ndarray:
        level = data[-1].copy()
        diffs = [dy[-1 - i] for i in range(k_ar_diff)]
        out = []
        for _ in range(horizon):
            step = const + alpha @ (beta.T @ level) + sum(gamma[i] @ diffs[i] for i in range(k_ar_diff))
            level = level + step
            diffs = [step] + diffs[:-1] if k_ar_diff else []
            out.append(level.copy())
        return np.array(out)

    return {"alpha": alpha, "beta": beta, "gamma": gamma, "const": const, "eigenvalues": core["eigenvalues"], "forecast": forecast}


@registry.register("stats.johansen", module="stats",
                   summary="Johansen cointegration test on 2 or more series: trace and maximum eigenvalue statistics with simulated (seeded) critical values at 90, 95 and 99 percent, the rank chosen at 5 percent and the cointegrating vectors.",
                   inputs={"data": {"type": "table", "required": True}, "k_ar_diff": {"type": "int", "required": False, "default": 1},
                           "n_simulations": {"type": "int", "required": False, "default": 500}, "seed": {"type": "int", "required": False, "default": 0},
                           "drift": {"type": "str", "required": False, "default": "data"}},
                   piece_ids=("FT-081", "ECT-015"))
def johansen(data: Any, k_ar_diff: int = 1, n_simulations: int = 500, seed: int = 0, drift: str = "data") -> QuantResult:
    name, pieces = "stats.johansen", ("FT-081", "ECT-015")
    limits.check_param("n_simulations", n_simulations)
    for label, v in (("k_ar_diff", k_ar_diff), ("n_simulations", n_simulations), ("seed", seed)):
        limits.as_int(label, v)
    params = {"k_ar_diff": int(k_ar_diff), "n_simulations": int(n_simulations), "seed": int(seed), "drift": drift}

    def compute() -> Any:
        if drift not in ("data", "none"):
            raise c.Domain("drift must be data (drift of the simulated walks taken from the data) or none")
        if not (0 <= k_ar_diff <= 10):
            raise c.Domain("k_ar_diff must be between 0 and 10")
        if not (50 <= n_simulations <= MAX_SIMULATIONS):
            raise c.Domain(f"n_simulations must be between 50 and {MAX_SIMULATIONS}")
        frame, warns = c.clean_frame(data, name="data", min_cols=2, min_obs=40)
        if frame.shape[1] > 6:
            raise c.Domain("the Johansen test supports 2 to 6 series")
        if any(c.is_constant(frame[col]) for col in frame.columns):
            raise c.Domain("constant series: the test is undefined")
        arr = frame.to_numpy()
        core = econ.johansen_core(arr, int(k_ar_diff))
        trace, max_eig = econ.johansen_statistics(core["eigenvalues"], core["t_eff"])
        steps = np.diff(arr, axis=0)
        ratio = 0.0 if drift == "none" else round(float(np.mean(np.abs(steps.mean(axis=0)) / steps.std(axis=0, ddof=1))), 3)
        crit = econ.johansen_critical_values(arr.shape[1], len(arr), int(k_ar_diff), int(n_simulations), int(seed), ratio)
        rank = 0
        for r in range(arr.shape[1]):
            if trace[r] > crit["trace"][r, 1]:
                rank = r + 1
            else:
                break
        vectors = core["beta"].copy()
        for j in range(vectors.shape[1]):
            if vectors[0, j] != 0:
                vectors[:, j] = vectors[:, j] / vectors[0, j]
        return ({"columns": [str(col) for col in frame.columns], "eigenvalues": core["eigenvalues"], "trace": trace, "max_eigenvalue": max_eig,
                 "trace_critical": {lvl: crit["trace"][:, i] for i, lvl in enumerate(crit["levels"])},
                 "max_eigenvalue_critical": {lvl: crit["max_eigenvalue"][:, i] for i, lvl in enumerate(crit["levels"])},
                 "rank_5pct": rank, "cointegrating_vectors": vectors.T, "n_obs": int(core["t_eff"]), "drift_ratio": ratio,
                 "null_rank_index": list(range(arr.shape[1]))},
                warns + ("critical values come from a seeded simulation of random walks at the sample size and the drift of the data; rank is the sequential trace test at 5%",))

    return c.run(name, unit="statistic", method="johansen", params=params, piece_ids=pieces, compute=compute)


@registry.register("stats.hedge_amounts", module="stats",
                   summary="Turn a hedge ratio into the two legs of a pair. A ratio on log prices is an elasticity: the dollar value of leg B is ratio times the dollar value of leg A. A ratio on prices is a share ratio.",
                   inputs={"price_a": {"type": "float", "required": True}, "price_b": {"type": "float", "required": True},
                           "shares_a": {"type": "float", "required": True}, "ratio": {"type": "float", "required": True},
                           "ratio_basis": {"type": "str", "required": False, "default": "level"}}, piece_ids=("FT-081", "ECT-015"))
def hedge_amounts(price_a: float, price_b: float, shares_a: float, ratio: float, ratio_basis: str = "level") -> QuantResult:
    name, pieces = "stats.hedge_amounts", ("FT-081", "ECT-015")
    params = {"price_a": price_a, "price_b": price_b, "shares_a": shares_a, "ratio": ratio, "ratio_basis": ratio_basis}

    def compute() -> Any:
        pa, pb = c.number("price_a", price_a, low=0.0), c.number("price_b", price_b, low=0.0)
        sa, r = c.number("shares_a", shares_a), c.number("ratio", ratio)
        if pa <= 0 or pb <= 0:
            raise c.Domain("prices must be positive")
        if ratio_basis not in ("level", "log"):
            raise c.Domain("ratio_basis must be level or log")
        dollars_a = sa * pa
        if ratio_basis == "log":
            dollars_b = r * dollars_a
            shares_b = dollars_b / pb
        else:
            shares_b = r * sa
            dollars_b = shares_b * pb
        return {"shares_a": sa, "dollars_a": dollars_a, "shares_b": shares_b, "dollars_b": dollars_b,
                "dollar_neutral": bool(math.isclose(abs(dollars_a), abs(dollars_b), rel_tol=1e-9)),
                "dollar_imbalance": dollars_a - dollars_b}

    return c.run(name, unit="usd", method="hedge_legs", params=params, piece_ids=pieces, compute=compute)

"""Causal inference tools (FT-093): IV-2SLS, difference in differences, regression discontinuity, propensity score and
synthetic control. Written from the method, on numpy and scipy.

They are registered as a methodological reference: no desk agent calls them today. Each one states its identifying assumption
in the warnings, because the number alone does not make an effect causal.
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
from hokma.quant.stats import regression as rg
from hokma.quant.types import QuantResult

PIECES = ("FT-093",)
_SERIES = {"type": "series", "required": True}
MAX_PLACEBO_DONORS = 60


def _matrix(values: Any, name: str, prefix: str) -> tuple[np.ndarray, list[str]]:
    if isinstance(values, pd.Series):
        values = values.to_frame(values.name if values.name is not None else prefix)
    if isinstance(values, pd.DataFrame):
        names = [str(col) for col in values.columns]
        try:
            return values.astype(float).to_numpy(), names
        except (TypeError, ValueError) as exc:
            raise BadInput(f"{name}: values are not numeric") from exc
    arr = np.asarray(values, dtype=float)
    if arr.ndim == 1:
        arr = arr[:, None]
    return arr, [f"{prefix}{i + 1}" for i in range(arr.shape[1])]


def _finite_rows(*mats: np.ndarray) -> np.ndarray:
    keep = np.ones(len(mats[0]), dtype=bool)
    for m in mats:
        if len(m) != len(mats[0]):
            raise BadInput("all inputs must have the same length")
        keep &= np.all(np.isfinite(m.reshape(len(m), -1)), axis=1)
    return keep


@registry.register("stats.iv_2sls", module="stats",
                   summary="Instrumental variables by two stage least squares: effect of the endogenous regressor(s) on y using the instruments z, optional exogenous controls. Returns the first stage F of the instruments (weak below 10) and the Sargan test when overidentified.",
                   inputs={"y": _SERIES, "endog": {"type": "series or table", "required": True}, "instruments": {"type": "series or table", "required": True},
                           "controls": {"type": "series or table", "required": False, "default": None}}, piece_ids=PIECES)
def iv_2sls(y: Any, endog: Any, instruments: Any, controls: Any = None) -> QuantResult:
    name = "stats.iv_2sls"

    def compute() -> Any:
        yy = c.helpers.as_series(y, name="y").to_numpy()
        xe, xe_names = _matrix(endog, "endog", "endog")
        zz, z_names = _matrix(instruments, "instruments", "z")
        ctrl, ctrl_names = (np.zeros((len(yy), 0)), []) if controls is None else _matrix(controls, "controls", "control")
        keep = _finite_rows(yy[:, None], xe, zz, ctrl)
        warns: list[str] = []
        if not keep.all():
            warns.append(f"dropped {int((~keep).sum())} row(s) with NaN or infinite values")
        yy, xe, zz, ctrl = yy[keep], xe[keep], zz[keep], ctrl[keep]
        n, k_end = len(yy), xe.shape[1]
        if zz.shape[1] < k_end:
            raise c.Domain(f"underidentified: {k_end} endogenous regressor(s) need at least {k_end} instruments, got {zz.shape[1]}")
        ones = np.ones((n, 1))
        x_full = np.hstack([ones, xe, ctrl])
        z_full = np.hstack([ones, zz, ctrl])
        if n <= z_full.shape[1] + 2:
            raise c.Domain("not enough observations")
        if np.linalg.matrix_rank(z_full) < z_full.shape[1]:
            raise c.Domain("the instruments and controls are collinear")
        pz = z_full @ np.linalg.pinv(z_full.T @ z_full) @ z_full.T
        xhat = pz @ x_full
        xtx_inv = np.linalg.inv(xhat.T @ xhat)
        beta = xtx_inv @ xhat.T @ yy
        resid = yy - x_full @ beta
        df = n - x_full.shape[1]
        s2 = float(resid @ resid) / df
        se = np.sqrt(np.diag(s2 * xtx_inv))
        names = ["const"] + xe_names + ctrl_names
        t = beta / se
        p = 2.0 * _lib.sp_stats.t.sf(np.abs(t), df)
        first = {}
        for j, nm in enumerate(xe_names):
            restricted = np.hstack([ones, ctrl])
            fr = econ.ols(xe[:, j], restricted) if restricted.shape[1] else None
            fu = econ.ols(xe[:, j], z_full)
            rss_r, rss_u = float(fr.resid @ fr.resid), float(fu.resid @ fu.resid)
            q = zz.shape[1]
            f_stat = ((rss_r - rss_u) / q) / (rss_u / fu.df)
            first[nm] = {"f_stat": f_stat, "p_value": float(_lib.sp_stats.f.sf(f_stat, q, fu.df)), "r2": fu.r2}
            if f_stat < 10.0:
                warns.append(f"weak instruments for {nm}: first stage F = {f_stat:.2f} < 10, the 2SLS estimate is biased toward OLS and its inference unreliable")
        out: dict[str, Any] = {"names": names, "coefficients": dict(zip(names, beta)), "std_errors": dict(zip(names, se)), "t_stats": dict(zip(names, t)),
                               "p_values": dict(zip(names, p)), "first_stage": first, "n_obs": n, "df_resid": df}
        over = zz.shape[1] - k_end
        if over > 0:
            aux = econ.ols(resid, z_full)
            r2 = max(aux.r2, 0.0)
            sargan = n * r2
            out["sargan"] = {"statistic": sargan, "df": over, "p_value": float(_lib.sp_stats.chi2.sf(sargan, over))}
        warns.append("identification rests on two assumptions the data cannot prove: the instruments move the regressor, and they affect y only through it")
        return out, tuple(warns)

    return c.run(name, unit="coefficient", method="two_stage_least_squares", params={}, piece_ids=PIECES, compute=compute)


@registry.register("stats.diff_in_diff", module="stats",
                   summary="Difference in differences with one treated group and one control group, before and after: the interaction coefficient is the effect on the treated. treated and post are 0 or 1. Optional cluster ids.",
                   inputs={"y": _SERIES, "treated": _SERIES, "post": _SERIES, "cluster": {"type": "series", "required": False, "default": None}}, piece_ids=PIECES)
def diff_in_diff(y: Any, treated: Any, post: Any, cluster: Any = None) -> QuantResult:
    name = "stats.diff_in_diff"

    def compute() -> Any:
        yy = c.helpers.as_series(y, name="y").to_numpy()
        tr = c.helpers.as_series(treated, name="treated").to_numpy()
        po = c.helpers.as_series(post, name="post").to_numpy()
        keep = _finite_rows(yy[:, None], tr[:, None], po[:, None])
        warns: list[str] = []
        cl = None if cluster is None else np.asarray(list(cluster))
        if cl is not None and len(cl) != len(yy):
            raise BadInput("cluster must have the same length as y")
        if not keep.all():
            warns.append(f"dropped {int((~keep).sum())} row(s) with NaN or infinite values")
        yy, tr, po = yy[keep], tr[keep], po[keep]
        cl = cl[keep] if cl is not None else None
        if not (np.isin(tr, (0.0, 1.0)).all() and np.isin(po, (0.0, 1.0)).all()):
            raise c.Domain("treated and post must be 0 or 1")
        cells = {(a, b): yy[(tr == a) & (po == b)] for a in (0.0, 1.0) for b in (0.0, 1.0)}
        empty_cells = [k for k, v in cells.items() if len(v) == 0]
        if empty_cells:
            raise c.Domain("every group by period cell needs observations: treated/control before/after")
        design = np.column_stack([np.ones(len(yy)), tr, po, tr * po])
        fit = econ.ols(yy, design)
        cov = fit.cov if cl is None else rg._cluster_cov(fit.xtx_inv, design, fit.resid, cl, 4)
        se = np.sqrt(np.diag(cov))
        df = fit.df if cl is None else max(len(np.unique(cl)) - 1, 1)
        t = fit.beta / se
        names = ["const", "treated", "post", "treated_x_post"]
        means = {f"{'treated' if a else 'control'}_{'post' if b else 'pre'}": float(np.mean(v)) for (a, b), v in cells.items()}
        warns.append("the effect is causal only if the treated group would have moved like the control group without treatment (parallel trends): check the pre period")
        return ({"effect": float(fit.beta[3]), "std_error": float(se[3]), "t_stat": float(t[3]), "p_value": float(2.0 * _lib.sp_stats.t.sf(abs(t[3]), df)),
                 "cell_means": means, "coefficients": dict(zip(names, fit.beta)), "n_obs": len(yy), "clustered": cl is not None}, tuple(warns))

    return c.run(name, unit="coefficient", method="did_2x2", params={}, piece_ids=PIECES, compute=compute)


@registry.register("stats.regression_discontinuity", module="stats",
                   summary="Sharp regression discontinuity: local linear fit on each side of the cutoff with triangular weights; the jump at the cutoff is the effect. The default bandwidth is a rule of thumb, report results for several.",
                   inputs={"y": _SERIES, "running": _SERIES, "cutoff": {"type": "float", "required": True}, "bandwidth": {"type": "float", "required": False, "default": None}},
                   piece_ids=PIECES)
def regression_discontinuity(y: Any, running: Any, cutoff: float, bandwidth: float | None = None) -> QuantResult:
    name = "stats.regression_discontinuity"
    params = {"cutoff": cutoff, "bandwidth": bandwidth}

    def compute() -> Any:
        cut = c.number("cutoff", cutoff)
        yy = c.helpers.as_series(y, name="y").to_numpy()
        xx = c.helpers.as_series(running, name="running").to_numpy()
        keep = _finite_rows(yy[:, None], xx[:, None])
        warns: list[str] = []
        if not keep.all():
            warns.append(f"dropped {int((~keep).sum())} row(s) with NaN or infinite values")
        yy, xx = yy[keep], xx[keep]
        if len(yy) < 20:
            raise c.Domain(f"need at least 20 observations, got {len(yy)}")
        h = c.number("bandwidth", bandwidth, low=0.0) if bandwidth is not None else 1.84 * float(np.std(xx, ddof=1)) * len(xx) ** (-0.2)
        if h <= 0:
            raise c.Domain("bandwidth must be positive")
        centered = xx - cut
        inside = np.abs(centered) <= h
        left, right = inside & (centered < 0), inside & (centered >= 0)
        if left.sum() < 5 or right.sum() < 5:
            raise c.Domain(f"need at least 5 observations on each side inside the bandwidth, got {int(left.sum())} and {int(right.sum())}")
        w = np.where(inside, 1.0 - np.abs(centered) / h, 0.0)
        d = (centered >= 0).astype(float)
        design = np.column_stack([np.ones(len(yy)), d, centered, d * centered])[inside]
        sw = np.sqrt(w[inside])
        fit = econ.ols(yy[inside] * sw, design * sw[:, None])
        cov = rg._sandwich(fit.xtx_inv, design * sw[:, None], fit.resid, "hc1", None)
        se = float(math.sqrt(cov[1, 1]))
        t = float(fit.beta[1] / se) if se > 0 else float("nan")
        warns.append("the jump is causal only if units cannot sort themselves around the cutoff: look at the density of the running variable there")
        return ({"effect": float(fit.beta[1]), "std_error": se, "t_stat": t, "p_value": float(2.0 * _lib.sp_stats.t.sf(abs(t), fit.df)) if math.isfinite(t) else None,
                 "bandwidth": h, "n_left": int(left.sum()), "n_right": int(right.sum()), "left_intercept": float(fit.beta[0]),
                 "right_intercept": float(fit.beta[0] + fit.beta[1])}, tuple(warns))

    return c.run(name, unit="coefficient", method="local_linear_rd", params=params, piece_ids=PIECES, compute=compute)


@registry.register("stats.propensity_score", module="stats",
                   summary="Propensity score from a logit of the treatment on covariates, then the average treatment effect by inverse probability weighting and the effect on the treated by nearest neighbour matching, with the common support check and a seeded bootstrap error.",
                   inputs={"treatment": _SERIES, "x": {"type": "series or table", "required": True}, "y": _SERIES,
                           "n_boot": {"type": "int", "required": False, "default": 100}, "seed": {"type": "int", "required": False, "default": 0}}, piece_ids=PIECES)
def propensity_score(treatment: Any, x: Any, y: Any, n_boot: int = 100, seed: int = 0) -> QuantResult:
    name = "stats.propensity_score"
    limits.check_param("n_boot", n_boot)
    limits.as_int("n_boot", n_boot)
    limits.as_int("seed", seed)
    params = {"n_boot": int(n_boot), "seed": int(seed)}

    def estimate(tt: np.ndarray, cov: np.ndarray, yy: np.ndarray) -> tuple[float, float, np.ndarray]:
        design = np.column_stack([np.ones(len(tt)), cov])
        fit = rg.logit_probit_fit(tt, design, "logit")
        eta = design @ fit["beta"]
        e = 1.0 / (1.0 + np.exp(-eta))
        e = np.clip(e, 1e-6, 1.0 - 1e-6)
        w1, w0 = tt / e, (1.0 - tt) / (1.0 - e)
        ate = float(np.sum(w1 * yy) / np.sum(w1) - np.sum(w0 * yy) / np.sum(w0))
        treated_idx, control_idx = np.where(tt == 1.0)[0], np.where(tt == 0.0)[0]
        nearest = control_idx[np.argmin(np.abs(e[treated_idx][:, None] - e[control_idx][None, :]), axis=1)]
        att = float(np.mean(yy[treated_idx] - yy[nearest]))
        return ate, att, e

    def compute() -> Any:
        tt = c.helpers.as_series(treatment, name="treatment").to_numpy()
        yy = c.helpers.as_series(y, name="y").to_numpy()
        cov, names = _matrix(x, "x", "x")
        keep = _finite_rows(tt[:, None], yy[:, None], cov)
        warns: list[str] = []
        if not keep.all():
            warns.append(f"dropped {int((~keep).sum())} row(s) with NaN or infinite values")
        tt, yy, cov = tt[keep], yy[keep], cov[keep]
        if not np.isin(tt, (0.0, 1.0)).all():
            raise c.Domain("treatment must be 0 or 1")
        n1, n0 = int(tt.sum()), int(len(tt) - tt.sum())
        if n1 < 10 or n0 < 10:
            raise c.Domain(f"need at least 10 treated and 10 control units, got {n1} and {n0}")
        ate, att, e = estimate(tt, cov, yy)
        lo, hi = max(e[tt == 1].min(), e[tt == 0].min()), min(e[tt == 1].max(), e[tt == 0].max())
        outside = int(np.sum((e < lo) | (e > hi)))
        if outside:
            warns.append(f"{outside} unit(s) lie outside the common support of the scores: their weights are not backed by comparable units")
        se_ate = se_att = None
        if n_boot >= 20:
            rng = np.random.default_rng(int(seed))
            ates, atts = [], []
            for _ in range(int(n_boot)):
                idx = rng.integers(0, len(tt), len(tt))
                if tt[idx].sum() < 5 or (1 - tt[idx]).sum() < 5:
                    continue
                try:
                    a, b, _ = estimate(tt[idx], cov[idx], yy[idx])
                except c.Domain:
                    continue
                ates.append(a)
                atts.append(b)
            if len(ates) >= 10:
                se_ate, se_att = float(np.std(ates, ddof=1)), float(np.std(atts, ddof=1))
        warns.append("the effect is causal only if no unobserved confounder drives both treatment and outcome (unconfoundedness)")
        return ({"ate_ipw": ate, "att_matching": att, "se_ate": se_ate, "se_att": se_att, "n_treated": n1, "n_control": n0,
                 "score_summary": {"treated_mean": float(e[tt == 1].mean()), "control_mean": float(e[tt == 0].mean()), "common_support": [float(lo), float(hi)],
                                   "outside_support": outside}, "covariates": names}, tuple(warns))

    return c.run(name, unit="coefficient", method="propensity_ipw_matching", params=params, piece_ids=PIECES, compute=compute)


def _sc_ratio(post: float, pre: float, scale: float) -> float:
    # An exact pre period fit (pre RMSPE at rounding level, or exactly 0 on some BLAS builds) would make the ratio infinite,
    # which is not representable in the result. The pre RMSPE is floored at rounding noise relative to the series scale.
    return post / max(pre, 1e-12 * max(scale, 1.0))


def _sc_weights(y1: np.ndarray, y0: np.ndarray) -> np.ndarray:
    j = y0.shape[1]
    start = np.full(j, 1.0 / j)

    def loss(w: np.ndarray) -> float:
        diff = y1 - y0 @ w
        return float(diff @ diff)

    def grad(w: np.ndarray) -> np.ndarray:
        return -2.0 * y0.T @ (y1 - y0 @ w)

    sol = _lib.sp_optimize.minimize(loss, start, jac=grad, method="SLSQP", bounds=[(0.0, 1.0)] * j,
                                    constraints=[{"type": "eq", "fun": lambda w: float(np.sum(w) - 1.0), "jac": lambda w: np.ones(j)}],
                                    options={"maxiter": 500, "ftol": 1e-14})
    w = np.clip(sol.x, 0.0, None)
    return w / w.sum()


@registry.register("stats.synthetic_control", module="stats",
                   summary="Synthetic control: weights on donor units (non negative, sum to one) that reproduce the treated unit before the event; the gap afterwards is the effect. Placebo runs on the donors give a permutation p value.",
                   inputs={"treated_pre": _SERIES, "donors_pre": {"type": "table", "required": True}, "treated_post": _SERIES, "donors_post": {"type": "table", "required": True}},
                   piece_ids=PIECES)
def synthetic_control(treated_pre: Any, donors_pre: Any, treated_post: Any, donors_post: Any) -> QuantResult:
    name = "stats.synthetic_control"

    def compute() -> Any:
        y1_pre = c.helpers.as_series(treated_pre, name="treated_pre").to_numpy()
        y1_post = c.helpers.as_series(treated_post, name="treated_post").to_numpy()
        d_pre, donor_names = _matrix(donors_pre, "donors_pre", "donor")
        d_post, post_names = _matrix(donors_post, "donors_post", "donor")
        if donor_names != post_names:
            raise BadInput("donors_pre and donors_post must have the same columns")
        if len(y1_pre) != len(d_pre) or len(y1_post) != len(d_post):
            raise BadInput("treated and donor series must have the same length in each period")
        if not (np.all(np.isfinite(y1_pre)) and np.all(np.isfinite(y1_post)) and np.all(np.isfinite(d_pre)) and np.all(np.isfinite(d_post))):
            raise c.Domain("NaN or infinite values: a synthetic control needs complete series")
        t0, j = d_pre.shape
        if t0 < 5 or len(y1_post) < 1:
            raise c.Domain("need at least 5 pre period and 1 post period observations")
        if j < 2:
            raise c.Domain("need at least 2 donor units")
        w = _sc_weights(y1_pre, d_pre)
        pre_gap = y1_pre - d_pre @ w
        post_gap = y1_post - d_post @ w
        rmspe_pre = float(np.sqrt(np.mean(pre_gap ** 2)))
        rmspe_post = float(np.sqrt(np.mean(post_gap ** 2)))
        ratio = _sc_ratio(rmspe_post, rmspe_pre, float(np.max(np.abs(y1_pre))))
        warns = ["a poor pre period fit (large pre RMSPE) means the post gap is not an effect estimate"]
        p_value = None
        if 5 <= j <= MAX_PLACEBO_DONORS:
            ratios = []
            for k in range(j):
                others = [i for i in range(j) if i != k]
                wk = _sc_weights(d_pre[:, k], d_pre[:, others])
                pre_k = d_pre[:, k] - d_pre[:, others] @ wk
                post_k = d_post[:, k] - d_post[:, others] @ wk
                rp = float(np.sqrt(np.mean(pre_k ** 2)))
                ratios.append(_sc_ratio(float(np.sqrt(np.mean(post_k ** 2))), rp, float(np.max(np.abs(d_pre[:, k])))))
            p_value = float((1 + sum(1 for r_ in ratios if r_ >= ratio)) / (j + 1))
        else:
            warns.append("placebo p value needs between 5 and 60 donors")
        return ({"weights": dict(zip(donor_names, w)), "effect_path": post_gap, "average_effect": float(post_gap.mean()), "pre_gap": pre_gap,
                 "rmspe_pre": rmspe_pre, "rmspe_post": rmspe_post, "rmspe_ratio": ratio, "placebo_p_value": p_value}, tuple(warns))

    return c.run(name, unit="gap", method="synthetic_control", params={}, piece_ids=PIECES, compute=compute)

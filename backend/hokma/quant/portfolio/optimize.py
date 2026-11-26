"""Portfolio optimizers (SK-001, SK-002, SK-003, SK-006, SK-014, SK-015, L-53).

Every optimizer gets the returns panel and its own limits. A solver that does not converge never raises and never invents
weights: the result carries status "failed", the reason and a labelled baseline (see `container.failed_payload`).
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _common as c
from hokma.quant.portfolio import _lib, priors
from hokma.quant.portfolio.container import FALLBACKS, failed_payload
from hokma.quant.types import QuantResult

_RET = {"type": "dataframe", "required": True}
MIN_OBS = 5
CAPPED_MEASURES = ("variance", "standard_deviation", "semi_variance", "semi_deviation", "mean_absolute_deviation",
                   "first_lower_partial_moment", "worst_realization", "cvar", "evar", "max_drawdown", "average_drawdown", "cdar",
                   "edar", "ulcer_index", "gini_mean_difference")
BASELINES = ("inverse_volatility", "equal_weighted", "random")
MAX_ASSETS = 5_000

_BASIC = {
    "objective": {"type": "str", "required": False, "default": "min_risk"},
    "risk_measure": {"type": "str", "required": False, "default": "variance"},
    "risk_aversion": {"type": "float", "required": False, "default": 1.0},
    "min_weights": {"type": "float_or_dict", "required": False, "default": 0.0},
    "max_weights": {"type": "float_or_dict", "required": False, "default": 1.0},
    "budget": {"type": "float", "required": False, "default": 1.0},
    "risk_free_rate": {"type": "float", "required": False, "default": 0.0},
    "l1_coef": {"type": "float", "required": False, "default": 0.0},
    "l2_coef": {"type": "float", "required": False, "default": 0.0},
    "cvar_beta": {"type": "float", "required": False, "default": 0.95},
    "solver": {"type": "str", "required": False, "default": "CLARABEL"},
}
_EXTRA = {
    "transaction_costs": {"type": "float_or_dict", "required": False, "default": 0.0},
    "management_fees": {"type": "float_or_dict", "required": False, "default": 0.0},
    "previous_weights": {"type": "dict", "required": False},
    "max_turnover": {"type": "float", "required": False},
    "threshold_long": {"type": "float_or_dict", "required": False},
    "cardinality": {"type": "int", "required": False},
    "max_risk": {"type": "dict", "required": False},
    "min_return": {"type": "float", "required": False},
    "max_short": {"type": "float", "required": False},
    "max_long": {"type": "float", "required": False},
    "min_budget": {"type": "float", "required": False},
    "max_budget": {"type": "float", "required": False},
}


def _panel(returns: Any, name: str, pieces: tuple[str, ...]) -> tuple[pd.DataFrame, tuple[str, ...]]:
    panel, warns = c.usable_panel(returns, min_obs=MIN_OBS)
    if panel is None:
        raise c.Domain("; ".join(warns))
    if panel.shape[1] > MAX_ASSETS:
        raise BadInput(f"more than {MAX_ASSETS} assets")
    return panel, warns


def _positive(label: str, value: Any, *, low: float = 0.0) -> float:
    return c.number(label, value, low=low)


def _beta(label: str, value: Any) -> float:
    f = c.number(label, value)
    if not 0.0 < f < 1.0:
        raise c.Domain(f"{label} must be strictly between 0 and 1, got {value}")
    return f


def _solver(value: Any) -> str:
    return c.choice("solver", value, _lib.SOLVERS)


def _result(panel: pd.DataFrame, model: Any, risk_measure: str, label: str) -> dict[str, Any]:
    pf = model.predict(panel)
    weights = np.asarray(model.weights_, dtype=float)
    return {"status": "optimal", "weights": c.weights_to_dict(weights, panel.columns), "risk_measure": risk_measure,
            "risk": c.jsonable(pf.get_measure(_lib.RiskMeasure(risk_measure))), "mean": c.jsonable(pf.mean), "method": label}


def _fit(panel: pd.DataFrame, model: Any, risk_measure: str, label: str, fallback: str = "inverse_volatility") -> tuple[dict[str, Any], tuple[str, ...]]:
    """Fit; on a solver failure return the controlled failed result and say so in the warnings."""
    try:
        model.fit(panel)
    except _lib.SOLVER_ERRORS as exc:
        reason = f"{type(exc).__name__}: {str(exc)[:300]}"
        return failed_payload(panel, reason, fallback), (f"optimization failed: {reason}",)
    return _result(panel, model, risk_measure, label), ()


def _weight_bounds(panel: pd.DataFrame, min_weights: Any, max_weights: Any) -> dict[str, Any]:
    lo = c.per_asset(min_weights, panel.columns, "min_weights", 0.0)
    hi = c.per_asset(max_weights, panel.columns, "max_weights", 1.0)
    return {"min_weights": lo, "max_weights": hi}


def _mean_risk(name: str, pieces: tuple[str, ...], returns: Any, *, objective: str, risk_measure: str, risk_aversion: float,
               min_weights: Any, max_weights: Any, budget: Any, risk_free_rate: float, l1_coef: float, l2_coef: float,
               cvar_beta: float, solver: str, extra: dict[str, Any] | None, params: dict) -> QuantResult:
    extra = extra or {}

    def compute() -> Any:
        obj = c.choice("objective", objective, tuple(_lib.OBJECTIVES))
        measure = c.choice("risk_measure", risk_measure, _lib.RISK_MEASURES)
        kwargs: dict[str, Any] = {
            "objective_function": _lib.OBJECTIVES[obj], "risk_measure": _lib.RiskMeasure(measure),
            "risk_aversion": _positive("risk_aversion", risk_aversion), "budget": None if budget is None else c.number("budget", budget),
            "risk_free_rate": c.number("risk_free_rate", risk_free_rate), "l1_coef": _positive("l1_coef", l1_coef),
            "l2_coef": _positive("l2_coef", l2_coef), "cvar_beta": _beta("cvar_beta", cvar_beta), "solver": _solver(solver),
        }
        panel, warns = _panel(returns, name, pieces)
        kwargs.update(_weight_bounds(panel, min_weights, max_weights))
        kwargs.update(_extra_kwargs(panel, extra))
        value, more = _fit(panel, _lib.sk_optimization.MeanRisk(**kwargs), measure, "MeanRisk")
        return value, warns + more

    return c.run(name, unit="weights", method="MeanRisk", params=params, piece_ids=pieces, compute=compute, inputs=(returns, params))


def _extra_kwargs(panel: pd.DataFrame, extra: dict[str, Any]) -> dict[str, Any]:
    """Practical constraints of the optimizer, validated. Unknown risk measure names raise Domain."""
    out: dict[str, Any] = {}
    cols = panel.columns
    for key in ("transaction_costs", "management_fees"):
        if extra.get(key) is not None:
            out[key] = c.per_asset(extra[key], cols, key, 0.0)
    if extra.get("previous_weights") is not None:
        out["previous_weights"] = c.weights_vector(extra["previous_weights"], cols, label="previous_weights")
    if extra.get("threshold_long") is not None:
        out["threshold_long"] = c.per_asset(extra["threshold_long"], cols, "threshold_long", 0.0)
    for key in ("max_turnover", "min_return", "max_short", "max_long", "min_budget", "max_budget"):
        if extra.get(key) is not None:
            out[key] = c.number(key, extra[key])
    if extra.get("cardinality") is not None:
        card = c.number("cardinality", extra["cardinality"], low=1, integer=True)
        if card > panel.shape[1]:
            raise c.Domain(f"cardinality {int(card)} is above the number of assets {panel.shape[1]}")
        out["cardinality"] = int(card)
    caps = extra.get("max_risk")
    if caps is not None:
        if not isinstance(caps, dict) or not caps:
            raise BadInput("max_risk must be a non-empty dict measure -> limit")
        for measure, limit in caps.items():
            c.choice("max_risk measure", measure, CAPPED_MEASURES)
            out[f"max_{measure}"] = _positive(f"max_risk[{measure}]", limit)
    if "max_turnover" in out and "previous_weights" not in out:
        raise c.Domain("max_turnover needs previous_weights")
    return out


@registry.register("portfolio.mean_risk", module="portfolio",
                   summary="Optimal weights: minimize risk, maximize return, utility or ratio under one of 19 risk measures, with weight bounds and a budget.",
                   inputs={"returns": _RET, **_BASIC}, piece_ids=("SK-001",))
def mean_risk(returns: Any, objective: str = "min_risk", risk_measure: str = "variance", risk_aversion: float = 1.0,
              min_weights: Any = 0.0, max_weights: Any = 1.0, budget: Any = 1.0, risk_free_rate: float = 0.0,
              l1_coef: float = 0.0, l2_coef: float = 0.0, cvar_beta: float = 0.95, solver: str = "CLARABEL") -> QuantResult:
    params = {"objective": objective, "risk_measure": risk_measure, "risk_aversion": risk_aversion, "min_weights": min_weights,
              "max_weights": max_weights, "budget": budget, "risk_free_rate": risk_free_rate, "l1_coef": l1_coef, "l2_coef": l2_coef,
              "cvar_beta": cvar_beta, "solver": solver}
    return _mean_risk("portfolio.mean_risk", ("SK-001",), returns, objective=objective, risk_measure=risk_measure, risk_aversion=risk_aversion,
                      min_weights=min_weights, max_weights=max_weights, budget=budget, risk_free_rate=risk_free_rate, l1_coef=l1_coef,
                      l2_coef=l2_coef, cvar_beta=cvar_beta, solver=solver, extra=None, params=params)


@registry.register("portfolio.mean_risk_constrained", module="portfolio",
                   summary="Mean-risk optimizer with practical constraints: costs, fees, turnover against previous weights, minimum position, cardinality, caps by risk measure.",
                   inputs={"returns": _RET, **_BASIC, **_EXTRA}, piece_ids=("SK-002",))
def mean_risk_constrained(returns: Any, objective: str = "min_risk", risk_measure: str = "variance", risk_aversion: float = 1.0,
                          min_weights: Any = 0.0, max_weights: Any = 1.0, budget: Any = 1.0, risk_free_rate: float = 0.0,
                          l1_coef: float = 0.0, l2_coef: float = 0.0, cvar_beta: float = 0.95, solver: str = "CLARABEL",
                          transaction_costs: Any = None, management_fees: Any = None, previous_weights: Any = None,
                          max_turnover: Any = None, threshold_long: Any = None, cardinality: Any = None, max_risk: Any = None,
                          min_return: Any = None, max_short: Any = None, max_long: Any = None, min_budget: Any = None,
                          max_budget: Any = None) -> QuantResult:
    extra = {"transaction_costs": transaction_costs, "management_fees": management_fees, "previous_weights": previous_weights,
             "max_turnover": max_turnover, "threshold_long": threshold_long, "cardinality": cardinality, "max_risk": max_risk,
             "min_return": min_return, "max_short": max_short, "max_long": max_long, "min_budget": min_budget, "max_budget": max_budget}
    params = {"objective": objective, "risk_measure": risk_measure, "risk_aversion": risk_aversion, "min_weights": min_weights,
              "max_weights": max_weights, "budget": budget, "risk_free_rate": risk_free_rate, "l1_coef": l1_coef, "l2_coef": l2_coef,
              "cvar_beta": cvar_beta, "solver": solver, **{k: v for k, v in extra.items() if v is not None}}
    return _mean_risk("portfolio.mean_risk_constrained", ("SK-002",), returns, objective=objective, risk_measure=risk_measure,
                      risk_aversion=risk_aversion, min_weights=min_weights, max_weights=max_weights, budget=budget,
                      risk_free_rate=risk_free_rate, l1_coef=l1_coef, l2_coef=l2_coef, cvar_beta=cvar_beta, solver=solver,
                      extra=extra, params=params)


_OBJECTIVE_TEXT = {
    "min_risk": "Lowest value of the chosen risk measure.",
    "max_return": "Highest expected return, usually under a cap on the risk measure.",
    "max_utility": "Highest expected return minus risk_aversion times the risk measure.",
    "max_ratio": "Highest ratio of excess expected return to the risk measure.",
}


@registry.register("portfolio.objectives", module="portfolio",
                   summary="The objectives and the risk measures an optimizer accepts, and the solvers installed. No calculation.",
                   inputs={}, piece_ids=("SK-003",))
def objectives() -> QuantResult:
    value = {"objectives": {k: _OBJECTIVE_TEXT[k] for k in _lib.OBJECTIVES}, "risk_measures": list(_lib.RISK_MEASURES),
             "capped_measures": list(CAPPED_MEASURES), "solvers": list(_lib.SOLVERS)}
    return c.build("portfolio.objectives", value, unit="catalog", method="ObjectiveFunction", params={}, piece_ids=("SK-003",), inputs=())


@registry.register("portfolio.robust_cvar", module="portfolio",
                   summary="Distributionally robust CVaR optimizer: weights that keep the tail loss bounded when the model is wrong (Wasserstein ball).",
                   inputs={"returns": _RET, "risk_aversion": {"type": "float", "required": False, "default": 1.0},
                           "cvar_beta": {"type": "float", "required": False, "default": 0.95},
                           "wasserstein_ball_radius": {"type": "float", "required": False, "default": 0.02},
                           "min_weights": {"type": "float_or_dict", "required": False, "default": 0.0},
                           "max_weights": {"type": "float_or_dict", "required": False, "default": 1.0},
                           "budget": {"type": "float", "required": False, "default": 1.0},
                           "solver": {"type": "str", "required": False, "default": "CLARABEL"}},
                   piece_ids=("SK-006",))
def robust_cvar(returns: Any, risk_aversion: float = 1.0, cvar_beta: float = 0.95, wasserstein_ball_radius: float = 0.02,
                min_weights: Any = 0.0, max_weights: Any = 1.0, budget: Any = 1.0, solver: str = "CLARABEL") -> QuantResult:
    name, pieces = "portfolio.robust_cvar", ("SK-006",)
    params = {"risk_aversion": risk_aversion, "cvar_beta": cvar_beta, "wasserstein_ball_radius": wasserstein_ball_radius,
              "min_weights": min_weights, "max_weights": max_weights, "budget": budget, "solver": solver}

    def compute() -> Any:
        kwargs = {"risk_aversion": _positive("risk_aversion", risk_aversion), "cvar_beta": _beta("cvar_beta", cvar_beta),
                  "wasserstein_ball_radius": _positive("wasserstein_ball_radius", wasserstein_ball_radius),
                  "budget": None if budget is None else c.number("budget", budget), "solver": _solver(solver)}
        panel, warns = _panel(returns, name, pieces)
        kwargs.update(_weight_bounds(panel, min_weights, max_weights))
        value, more = _fit(panel, _lib.sk_optimization.DistributionallyRobustCVaR(**kwargs), "cvar", "DistributionallyRobustCVaR")
        return value, warns + more

    return c.run(name, unit="weights", method="DistributionallyRobustCVaR", params=params, piece_ids=pieces, compute=compute, inputs=(returns, params))


@registry.register("portfolio.baselines", module="portfolio",
                   summary="Baseline weights every candidate must beat: inverse_volatility, equal_weighted or random (seeded).",
                   inputs={"returns": _RET, "kind": {"type": "str", "required": False, "default": "inverse_volatility"},
                           "seed": {"type": "int", "required": False}},
                   piece_ids=("SK-014",))
def baselines(returns: Any, kind: str = "inverse_volatility", seed: int | None = None) -> QuantResult:
    name, pieces = "portfolio.baselines", ("SK-014",)
    params = {"kind": kind, "seed": seed}

    def compute() -> Any:
        which = c.choice("kind", kind, BASELINES)
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or not 0 <= int(seed) < 2**32):
            raise BadInput("seed must be a whole number in [0, 2**32)")
        panel, warns = _panel(returns, name, pieces)
        state = np.random.get_state()
        try:
            if which == "random" and seed is not None:
                np.random.seed(int(seed))
            model = {"inverse_volatility": _lib.sk_optimization.InverseVolatility, "equal_weighted": _lib.sk_optimization.EqualWeighted,
                     "random": _lib.sk_optimization.Random}[which]().fit(panel)
        finally:
            np.random.set_state(state)
        weights = np.asarray(model.weights_, dtype=float)
        pf = model.predict(panel)
        return {"status": "optimal", "weights": c.weights_to_dict(weights, panel.columns), "kind": which,
                "risk": c.jsonable(pf.variance), "risk_measure": "variance", "mean": c.jsonable(pf.mean)}, warns

    return c.run(name, unit="weights", method=kind if isinstance(kind, str) else "baseline", params=params, piece_ids=pieces,
                 compute=compute, inputs=(returns, params))


@registry.register("portfolio.risk_parity", module="portfolio",
                   summary="Risk parity: weights whose risk contributions match a budget (equal by default), under one risk measure, with weight bounds.",
                   inputs={"returns": _RET, "risk_measure": {"type": "str", "required": False, "default": "variance"},
                           "risk_budget": {"type": "dict", "required": False},
                           "min_weights": {"type": "float_or_dict", "required": False, "default": 0.0},
                           "max_weights": {"type": "float_or_dict", "required": False, "default": 1.0},
                           "solver": {"type": "str", "required": False, "default": "CLARABEL"}},
                   piece_ids=("L-53",))
def risk_parity(returns: Any, risk_measure: str = "variance", risk_budget: Any = None, min_weights: Any = 0.0, max_weights: Any = 1.0,
                solver: str = "CLARABEL") -> QuantResult:
    name, pieces = "portfolio.risk_parity", ("L-53",)
    params = {"risk_measure": risk_measure, "risk_budget": risk_budget, "min_weights": min_weights, "max_weights": max_weights, "solver": solver}

    def compute() -> Any:
        measure = c.choice("risk_measure", risk_measure, _lib.RISK_MEASURES)
        kwargs: dict[str, Any] = {"risk_measure": _lib.RiskMeasure(measure), "solver": _solver(solver)}
        panel, warns = _panel(returns, name, pieces)
        if risk_budget is not None:
            if not isinstance(risk_budget, dict) or not risk_budget:
                raise BadInput("risk_budget must be a non-empty dict asset -> share")
            shares = c.per_asset(risk_budget, panel.columns, "risk_budget", None)
            if min(shares) <= 0.0:
                raise c.Domain("every risk budget share must be above zero")
            kwargs["risk_budget"] = shares
        kwargs.update(_weight_bounds(panel, min_weights, max_weights))
        value, more = _fit(panel, _lib.sk_optimization.RiskBudgeting(**kwargs), measure, "RiskBudgeting")
        return value, warns + more

    return c.run(name, unit="weights", method="RiskBudgeting", params=params, piece_ids=pieces, compute=compute, inputs=(returns, params))


@registry.register("portfolio.black_litterman_weights", module="portfolio",
                   summary="Weights from the Black-Litterman posterior (market prior mixed with views): the mean-risk optimizer run on the posterior returns and covariance.",
                   inputs={"returns": _RET, "views": {"type": "list", "required": True}, "view_confidences": {"type": "list", "required": False},
                           "tau": {"type": "float", "required": False, "default": 0.05},
                           "risk_free_rate": {"type": "float", "required": False, "default": 0.0},
                           "objective": {"type": "str", "required": False, "default": "max_utility"},
                           "risk_aversion": {"type": "float", "required": False, "default": 1.0},
                           "min_weights": {"type": "float_or_dict", "required": False, "default": 0.0},
                           "max_weights": {"type": "float_or_dict", "required": False, "default": 1.0},
                           "budget": {"type": "float", "required": False, "default": 1.0},
                           "solver": {"type": "str", "required": False, "default": "CLARABEL"}},
                   piece_ids=("L-53",))
def black_litterman_weights(returns: Any, views: Any, view_confidences: Any = None, tau: float = 0.05, risk_free_rate: float = 0.0,
                            objective: str = "max_utility", risk_aversion: float = 1.0, min_weights: Any = 0.0, max_weights: Any = 1.0,
                            budget: Any = 1.0, solver: str = "CLARABEL") -> QuantResult:
    name, pieces = "portfolio.black_litterman_weights", ("L-53",)
    params = {"views": views, "view_confidences": view_confidences, "tau": tau, "risk_free_rate": risk_free_rate, "objective": objective,
              "risk_aversion": risk_aversion, "min_weights": min_weights, "max_weights": max_weights, "budget": budget, "solver": solver}

    def compute() -> Any:
        obj = c.choice("objective", objective, tuple(_lib.OBJECTIVES))
        rate = c.number("risk_free_rate", risk_free_rate)
        kwargs: dict[str, Any] = {"objective_function": _lib.OBJECTIVES[obj], "risk_aversion": _positive("risk_aversion", risk_aversion),
                                  "budget": None if budget is None else c.number("budget", budget), "risk_free_rate": rate,
                                  "solver": _solver(solver)}
        panel, warns = _panel(returns, name, pieces)
        if not isinstance(views, (list, tuple)) or len(views) > priors.MAX_VIEWS:
            raise BadInput(f"views must be a list of at most {priors.MAX_VIEWS} views")
        texts = [priors.view_text(v, panel.columns, label=f"views[{i}]") for i, v in enumerate(views)]
        if not texts:
            raise c.Domain("at least one view is needed")
        conf = None
        if view_confidences is not None:
            if not isinstance(view_confidences, (list, tuple)) or len(view_confidences) != len(texts):
                raise BadInput("view_confidences must be a list with one value per view")
            conf = [c.number("view confidence", v, low=0.0, high=1.0, low_open=True) for v in view_confidences]
        kwargs["prior_estimator"] = _lib.sk_prior.BlackLitterman(views=texts, view_confidences=conf, risk_free_rate=rate,
                                                                 tau=c.number("tau", tau, low=0.0, low_open=True))
        kwargs.update(_weight_bounds(panel, min_weights, max_weights))
        value, more = _fit(panel, _lib.sk_optimization.MeanRisk(**kwargs), "variance", "BlackLitterman + MeanRisk")
        return value, warns + more

    return c.run(name, unit="weights", method="BlackLitterman + MeanRisk", params=params, piece_ids=pieces, compute=compute,
                 inputs=(returns, params))


_OPTIMIZERS = {
    "mean_risk": ("MeanRisk", "portfolio.mean_risk"),
    "robust_cvar": ("DistributionallyRobustCVaR", "portfolio.robust_cvar"),
    "inverse_volatility": ("InverseVolatility", "portfolio.baselines"),
    "equal_weighted": ("EqualWeighted", "portfolio.baselines"),
    "random": ("Random", "portfolio.baselines"),
    "risk_parity": ("RiskBudgeting", "portfolio.risk_parity"),
    "black_litterman": ("MeanRisk", "portfolio.black_litterman_weights"),
}


@registry.register("portfolio.optimizers", module="portfolio",
                   summary="The optimizers of the module with the function that runs each one. All share one base: fit on returns, weights per asset.",
                   inputs={}, piece_ids=("SK-015",))
def optimizers() -> QuantResult:
    base = _lib.sk_optimization.BaseOptimization
    rows = []
    for key, (cls_name, function) in _OPTIMIZERS.items():
        cls = getattr(_lib.sk_optimization, cls_name)
        rows.append({"name": key, "class": cls_name, "function": function, "shares_base": bool(issubclass(cls, base))})
    return c.build("portfolio.optimizers", {"base": base.__name__, "optimizers": rows}, unit="catalog", method="BaseOptimization",
                   params={}, piece_ids=("SK-015",), inputs=())

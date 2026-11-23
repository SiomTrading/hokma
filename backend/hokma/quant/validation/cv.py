"""Cross validation for time series: walk forward, combinatorial purged CV, randomized CV, and portfolio evaluation.

SPEC-017 rows SK-034 to SK-041 and SK-045. The classes and functions are exposed unchanged; the registered functions
return `QuantResult` summaries that agents can call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import helpers, registry
from hokma.quant import limits
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult
from hokma.quant.validation import _lib

WalkForward = _lib.WalkForward
CombinatorialPurgedCV = _lib.CombinatorialPurgedCV
BaseCombinatorialCV = _lib.BaseCombinatorialCV
MultipleRandomizedCV = _lib.MultipleRandomizedCV
OnlineGridSearch = _lib.OnlineGridSearch
OnlineRandomizedSearch = _lib.OnlineRandomizedSearch
cross_val_predict = _lib.cross_val_predict
online_predict = _lib.online_predict
online_score = _lib.online_score
optimal_folds_number = _lib.optimal_folds_number
make_scorer = _lib.make_scorer

SCHEMES = ("cpcv", "walk_forward", "expanding")
ESTIMATORS = ("equal_weight", "inverse_volatility")


@dataclass(frozen=True)
class Fold:
    """One partition: positional train indices, positional test indices and the contiguous test blocks [start, stop)."""
    train: np.ndarray
    test: np.ndarray
    test_blocks: tuple[tuple[int, int], ...]


def _length(index: Any) -> int:
    if isinstance(index, bool):
        raise BadInput("index must be a length, an Index, a Series or a DataFrame")
    if isinstance(index, (int, np.integer)):
        if index < 0:
            raise BadInput("index length must not be negative")
        return int(index)
    if isinstance(index, (pd.Index, pd.Series, pd.DataFrame, list, tuple, np.ndarray)):
        return len(index)
    raise BadInput(f"index must be a length, an Index, a Series or a DataFrame, got {type(index).__name__}")


def _blocks(positions: np.ndarray) -> tuple[tuple[int, int], ...]:
    if len(positions) == 0:
        return ()
    cuts = np.where(np.diff(positions) != 1)[0] + 1
    return tuple((int(chunk[0]), int(chunk[-1]) + 1) for chunk in np.split(positions, cuts))


def split(index: Any, *, scheme: str = "cpcv", n_folds: int, purge: int, embargo: int, n_test_folds: int | None = None) -> list[Fold]:
    """Train and test partitions with purge and embargo.

    cpcv: `n_folds` contiguous groups, every combination of `n_test_folds` (default 2) groups is a test set; train
    observations within `purge` before a test block, and `purge + embargo` after it, are removed.
    walk_forward and expanding: a train window followed by a test window of the same size (`n // (n_folds + 1)`);
    the gap between them is `purge + embargo` observations, so forward-only schemes never train on the embargo zone.
    """
    n = _length(index)
    if scheme not in SCHEMES:
        raise BadInput(f"unknown scheme {scheme!r}; use one of {SCHEMES}")
    if n_folds < 2 or purge < 0 or embargo < 0:
        raise BadInput("n_folds must be >= 2; purge and embargo must be >= 0")
    positions = np.arange(n).reshape(-1, 1)
    if scheme == "cpcv":
        k = 2 if n_test_folds is None else int(n_test_folds)
        if not 1 <= k < n_folds:
            raise BadInput("n_test_folds must be between 1 and n_folds - 1")
        n_folds, k = limits.as_int("n_folds", n_folds), limits.as_int("n_test_folds", k)
        limits.check_param("n_folds", n_folds)
        limits.check_param("n_test_folds", k)
        limits.check_combinations(n_folds, k)
        if n < n_folds * 2:
            raise BadInput("too few observations for the requested number of folds")
        cv_obj = _lib.CombinatorialPurgedCV(n_folds=int(n_folds), n_test_folds=k, purged_size=int(purge), embargo_size=int(embargo))
        if purge + embargo >= n // n_folds - 1:
            raise BadInput("too few observations: purge plus embargo must be smaller than a fold")
        out = []
        for train, tests in cv_obj.split(positions):
            test = np.concatenate(tests)
            out.append(Fold(train=np.asarray(train), test=test, test_blocks=_blocks(np.sort(test))))
        return out
    block = n // (n_folds + 1)
    if block < 1:
        raise BadInput("too few observations for the requested number of folds")
    if purge + embargo >= block:
        raise BadInput("too few observations: purge plus embargo must be smaller than a window")
    cv_obj = _lib.WalkForward(test_size=block, train_size=block, purged_size=int(purge + embargo), expand_train=(scheme == "expanding"))
    return [Fold(train=np.asarray(tr), test=np.asarray(te), test_blocks=_blocks(np.asarray(te))) for tr, te in cv_obj.split(positions)]


def leakage_violations(folds: list[Fold], *, purge: int, embargo: int) -> list[str]:
    """Descriptions of every train observation that sits inside a test block, its purge zone or its embargo zone."""
    problems: list[str] = []
    for i, fold in enumerate(folds):
        train = set(int(t) for t in fold.train)
        overlap = train & set(int(t) for t in fold.test)
        if overlap:
            problems.append(f"fold {i}: {len(overlap)} observation(s) are both in train and in test")
        for start, stop in fold.test_blocks:
            zone = range(max(0, start - purge), stop + embargo)
            hit = [z for z in zone if z in train]
            if hit:
                problems.append(f"fold {i}: train observation {hit[0]} falls in the zone of test block [{start}, {stop})")
    return problems


@registry.register(
    "validation.split", module="validation",
    summary="Train and test partitions with purge and embargo (cpcv, walk_forward, expanding); reports whether any leakage exists.",
    inputs={"index": {"type": "int|index", "required": True}, "scheme": {"type": "str", "required": False, "default": "cpcv"},
            "n_folds": {"type": "int", "required": True}, "purge": {"type": "int", "required": False, "default": 0},
            "embargo": {"type": "int", "required": False, "default": 0}, "n_test_folds": {"type": "int", "required": False, "default": None}},
    piece_ids=("SK-034", "SK-035", "SK-036"),
)
def split_result(index: Any, n_folds: int, scheme: str = "cpcv", purge: int = 0, embargo: int = 0, n_test_folds: int | None = None) -> QuantResult:
    name = "validation.split"
    n = _length(index)
    try:
        folds = split(index, scheme=scheme, n_folds=n_folds, purge=purge, embargo=embargo, n_test_folds=n_test_folds)
    except BadInput as exc:
        if scheme in SCHEMES and "too few" in str(exc):
            return QuantResult.empty(name, "folds", scheme, str(exc))
        raise
    problems = leakage_violations(folds, purge=purge, embargo=embargo)
    value = {
        "scheme": scheme, "n_observations": n, "n_splits": len(folds), "leakage_free": not problems, "violations": problems[:10],
        "folds": [{"n_train": int(len(f.train)), "n_test": int(len(f.test)), "test_blocks": [list(b) for b in f.test_blocks]} for f in folds],
    }
    return QuantResult(name=name, value=value, unit="folds", method=scheme,
                       params={"n_folds": int(n_folds), "purge": int(purge), "embargo": int(embargo), "n_test_folds": n_test_folds},
                       inputs_hash=helpers.hash_inputs([n, scheme, n_folds, purge, embargo, n_test_folds]),
                       piece_ids=("SK-034", "SK-035", "SK-036"))


@registry.register(
    "validation.optimal_folds", module="validation",
    summary="Number of folds and test folds for combinatorial purged CV that best meets a target train size and number of test paths.",
    inputs={"n_observations": {"type": "int", "required": True}, "target_train_size": {"type": "int", "required": True},
            "target_n_test_paths": {"type": "int", "required": True}},
    piece_ids=("SK-037",),
)
def optimal_folds(n_observations: int, target_train_size: int, target_n_test_paths: int) -> QuantResult:
    name = "validation.optimal_folds"
    for _k, _v in (("n_observations", n_observations), ("target_train_size", target_train_size), ("target_n_test_paths", target_n_test_paths)):
        limits.check_param(_k, _v)
    if min(n_observations, target_train_size, target_n_test_paths) < 1 or target_train_size >= n_observations:
        return QuantResult.empty(name, "folds", "grid_search", "all inputs must be positive and target_train_size < n_observations")
    n_folds, n_test_folds = _lib.optimal_folds_number(n_observations=int(n_observations), target_train_size=int(target_train_size),
                                                      target_n_test_paths=int(target_n_test_paths))
    return QuantResult(name=name, value={"n_folds": int(n_folds), "n_test_folds": int(n_test_folds)}, unit="folds", method="grid_search",
                       params={"n_observations": int(n_observations), "target_train_size": int(target_train_size),
                               "target_n_test_paths": int(target_n_test_paths)},
                       inputs_hash=helpers.hash_inputs([n_observations, target_train_size, target_n_test_paths]), piece_ids=("SK-037",))


def _matrix(matrix: Any, name: str, min_rows: int) -> tuple[pd.DataFrame | None, tuple[str, ...]]:
    if not isinstance(matrix, pd.DataFrame):
        raise BadInput("matrix must be a DataFrame of returns, one column per asset")
    frame = matrix.astype(float).replace([np.inf, -np.inf], np.nan).dropna(how="any")
    warnings = ()
    if len(frame) < len(matrix):
        warnings = (f"dropped {len(matrix) - len(frame)} row(s) with NaN or infinite values",)
    if len(frame) < min_rows or frame.shape[1] < 1:
        return None, warnings + (f"need at least {min_rows} observations and 1 asset, got {len(frame)} and {frame.shape[1]}",)
    if ((frame.max() - frame.min()) == 0).any():
        return None, warnings + ("constant column: dispersion is zero",)
    return frame, warnings


def _estimator(kind: str):
    if kind == "equal_weight":
        return _lib.EqualWeighted()
    if kind == "inverse_volatility":
        return _lib.InverseVolatility()
    raise BadInput(f"unknown estimator {kind!r}; use one of {ESTIMATORS}")


def online_estimator(half_life: float):
    """Minimum variance portfolio with exponentially weighted moments: supports incremental updates."""
    return _lib.MeanRisk(prior_estimator=_lib.EmpiricalPrior(mu_estimator=_lib.EWMu(half_life=half_life),
                                                              covariance_estimator=_lib.EWCovariance(half_life=half_life)))


@registry.register(
    "validation.randomized_cv", module="validation",
    summary="Randomized robustness splits: walk forward windows over random blocks and random asset subsets (fixed seed).",
    inputs={"matrix": {"type": "dataframe", "required": True}, "n_subsamples": {"type": "int", "required": True},
            "asset_subset_size": {"type": "int", "required": True}, "train_size": {"type": "int", "required": True},
            "test_size": {"type": "int", "required": True}, "window_size": {"type": "int", "required": False, "default": None},
            "seed": {"type": "int", "required": False, "default": 0}},
    piece_ids=("SK-038",),
)
def randomized_cv(matrix: pd.DataFrame, n_subsamples: int, asset_subset_size: int, train_size: int, test_size: int,
                  window_size: int | None = None, seed: int = 0) -> QuantResult:
    name = "validation.randomized_cv"
    for _k, _v in (("n_subsamples", n_subsamples), ("asset_subset_size", asset_subset_size), ("train_size", train_size), ("test_size", test_size), ("window_size", window_size)):
        limits.check_param(_k, _v)
    frame, warnings = _matrix(matrix, name, train_size + test_size)
    if frame is None:
        return QuantResult.empty(name, "splits", "randomized_walk_forward", warnings[-1])
    if not 1 <= asset_subset_size <= frame.shape[1] or n_subsamples < 1:
        return QuantResult.empty(name, "splits", "randomized_walk_forward", "asset_subset_size must be within the number of assets and n_subsamples >= 1")
    window = window_size if window_size is not None else len(frame)
    cv_obj = _lib.MultipleRandomizedCV(walk_forward=_lib.WalkForward(test_size=int(test_size), train_size=int(train_size)),
                                       n_subsamples=int(n_subsamples), asset_subset_size=int(asset_subset_size),
                                       window_size=int(window), random_state=int(seed))
    splits = list(cv_obj.split(frame))
    value = {"n_splits": len(splits), "n_subsamples": int(n_subsamples), "asset_subset_size": int(asset_subset_size),
             "mean_train_size": float(np.mean([len(s[0]) for s in splits])), "mean_test_size": float(np.mean([len(s[1]) for s in splits]))}
    return QuantResult(name=name, value=value, unit="splits", method="randomized_walk_forward",
                       params={"seed": int(seed), "window_size": int(window), "train_size": int(train_size), "test_size": int(test_size)},
                       inputs_hash=helpers.hash_inputs(frame, n_subsamples, asset_subset_size, train_size, test_size, window, seed),
                       as_of=helpers.last_as_of(frame.iloc[:, 0]), warnings=warnings, piece_ids=("SK-038",))


@registry.register(
    "validation.oos_population", module="validation",
    summary="Out of sample portfolios from a cross validation scheme (population of paths for cpcv): Sharpe distribution per path.",
    inputs={"matrix": {"type": "dataframe", "required": True}, "estimator": {"type": "str", "required": False, "default": "equal_weight"},
            "scheme": {"type": "str", "required": False, "default": "cpcv"}, "n_folds": {"type": "int", "required": False, "default": 10},
            "n_test_folds": {"type": "int", "required": False, "default": 2}, "purge": {"type": "int", "required": False, "default": 0},
            "embargo": {"type": "int", "required": False, "default": 0}, "train_size": {"type": "int", "required": False, "default": 252},
            "test_size": {"type": "int", "required": False, "default": 63}},
    piece_ids=("SK-039",),
)
def oos_population(matrix: pd.DataFrame, estimator: str = "equal_weight", scheme: str = "cpcv", n_folds: int = 10, n_test_folds: int = 2,
                   purge: int = 0, embargo: int = 0, train_size: int = 252, test_size: int = 63) -> QuantResult:
    name = "validation.oos_population"
    est = _estimator(estimator)
    if scheme not in ("cpcv", "walk_forward", "expanding"):
        raise BadInput(f"unknown scheme {scheme!r}")
    n_folds, n_test_folds, train_size, test_size = (limits.as_int(k, v) for k, v in (("n_folds", n_folds), ("n_test_folds", n_test_folds), ("train_size", train_size), ("test_size", test_size)))
    for _k, _v in (("n_folds", n_folds), ("n_test_folds", n_test_folds), ("train_size", train_size), ("test_size", test_size)):
        limits.check_param(_k, _v)
    if scheme == "cpcv":
        limits.check_combinations(n_folds, n_test_folds)
    frame, warnings = _matrix(matrix, name, max(n_folds * 2, train_size + test_size) if scheme != "cpcv" else n_folds * 2)
    if frame is None:
        return QuantResult.empty(name, "ratio", "cross_val_predict", warnings[-1])
    if scheme == "cpcv":
        cv_obj: Any = _lib.CombinatorialPurgedCV(n_folds=int(n_folds), n_test_folds=int(n_test_folds), purged_size=int(purge), embargo_size=int(embargo))
    else:
        cv_obj = _lib.WalkForward(test_size=int(test_size), train_size=int(train_size), purged_size=int(purge + embargo), expand_train=(scheme == "expanding"))
    predicted = _lib.cross_val_predict(est, frame, cv=cv_obj)
    if hasattr(predicted, "measures"):
        sharpes = np.asarray(predicted.measures(_lib.RatioMeasure.SHARPE_RATIO), dtype=float)
    else:
        sharpes = np.array([float(predicted.sharpe_ratio)])
    value = {"n_portfolios": int(len(sharpes)), "sharpe_mean": float(sharpes.mean()),
             "sharpe_std": float(sharpes.std(ddof=1)) if len(sharpes) > 1 else 0.0,
             "sharpe_p05": float(np.quantile(sharpes, 0.05)), "sharpe_p95": float(np.quantile(sharpes, 0.95)),
             "sharpe_per_portfolio": [float(s) for s in sharpes], "unit": "per period"}
    return QuantResult(name=name, value=value, unit="ratio", method="cross_val_predict",
                       params={"estimator": estimator, "scheme": scheme, "n_folds": int(n_folds), "n_test_folds": int(n_test_folds),
                               "purge": int(purge), "embargo": int(embargo), "train_size": int(train_size), "test_size": int(test_size)},
                       inputs_hash=helpers.hash_inputs(frame, estimator, scheme, n_folds, n_test_folds, purge, embargo, train_size, test_size),
                       as_of=helpers.last_as_of(frame.iloc[:, 0]), warnings=warnings, piece_ids=("SK-039",))


@registry.register(
    "validation.online_oos", module="validation",
    summary="Incremental out of sample evaluation of a minimum variance portfolio: predicted path and its Sharpe ratio (per period).",
    inputs={"matrix": {"type": "dataframe", "required": True}, "half_life": {"type": "float", "required": False, "default": 30.0},
            "warmup_size": {"type": "int", "required": False, "default": 252}, "test_size": {"type": "int", "required": False, "default": 21},
            "purge": {"type": "int", "required": False, "default": 0}},
    piece_ids=("SK-041",),
)
def online_oos(matrix: pd.DataFrame, half_life: float = 30.0, warmup_size: int = 252, test_size: int = 21, purge: int = 0) -> QuantResult:
    name = "validation.online_oos"
    limits.check_param("warmup_size", warmup_size)
    limits.check_param("test_size", test_size)
    frame, warnings = _matrix(matrix, name, warmup_size + test_size + purge + 1)
    if frame is None:
        return QuantResult.empty(name, "ratio", "online_walk_forward", warnings[-1])
    est = online_estimator(half_life)
    kwargs = dict(warmup_size=int(warmup_size), test_size=int(test_size), purged_size=int(purge))
    path = _lib.online_predict(est, frame, **kwargs)
    score = _lib.online_score(est, frame, scoring=_lib.RatioMeasure.SHARPE_RATIO, **kwargs)
    value = {"sharpe_online_score": float(score), "sharpe_online_predict": float(path.sharpe_ratio), "n_observations_scored": int(len(path.returns))}
    return QuantResult(name=name, value=value, unit="ratio", method="online_walk_forward",
                       params={"half_life": float(half_life), **kwargs}, inputs_hash=helpers.hash_inputs(frame, half_life, warmup_size, test_size, purge),
                       as_of=helpers.last_as_of(frame.iloc[:, 0]), warnings=warnings, piece_ids=("SK-041",))


@registry.register(
    "validation.online_search", module="validation",
    summary="Hyper-parameter search (grid or random) with incremental updates over the half-life of the moment estimators.",
    inputs={"matrix": {"type": "dataframe", "required": True}, "half_lives": {"type": "list", "required": True},
            "search": {"type": "str", "required": False, "default": "grid"}, "n_iter": {"type": "int", "required": False, "default": 3},
            "warmup_size": {"type": "int", "required": False, "default": 252}, "test_size": {"type": "int", "required": False, "default": 21},
            "seed": {"type": "int", "required": False, "default": 0}},
    piece_ids=("SK-040",),
)
def online_search(matrix: pd.DataFrame, half_lives: list[float], search: str = "grid", n_iter: int = 3, warmup_size: int = 252,
                  test_size: int = 21, seed: int = 0) -> QuantResult:
    name = "validation.online_search"
    if search not in ("grid", "random"):
        raise BadInput("search must be 'grid' or 'random'")
    if not isinstance(half_lives, (list, tuple)) or len(half_lives) < 1:
        raise BadInput("half_lives must be a non empty list")
    for _k, _v in (("warmup_size", warmup_size), ("test_size", test_size), ("n_iter", n_iter)):
        limits.check_param(_k, _v)
    frame, warnings = _matrix(matrix, name, warmup_size + test_size + 1)
    if frame is None:
        return QuantResult.empty(name, "ratio", f"online_{search}_search", warnings[-1])
    key = "prior_estimator__covariance_estimator__half_life"
    est = online_estimator(float(half_lives[0]))
    common = dict(scoring=_lib.RatioMeasure.SHARPE_RATIO, warmup_size=int(warmup_size), test_size=int(test_size))
    if search == "grid":
        searcher = _lib.OnlineGridSearch(est, param_grid={key: list(half_lives)}, **common)
    else:
        searcher = _lib.OnlineRandomizedSearch(est, param_distributions={key: list(half_lives)}, n_iter=min(int(n_iter), len(half_lives)),
                                               random_state=int(seed), **common)
    searcher.fit(frame)
    scores = [float(s) for s in searcher.cv_results_["mean_score"]]
    tried = [float(p[key]) for p in searcher.cv_results_["params"]]
    value = {"best_half_life": float(searcher.best_params_[key]), "best_score": float(searcher.best_score_),
             "tried": tried, "scores": scores}
    return QuantResult(name=name, value=value, unit="ratio", method=f"online_{search}_search",
                       params={"search": search, "seed": int(seed), "warmup_size": int(warmup_size), "test_size": int(test_size)},
                       inputs_hash=helpers.hash_inputs(frame, list(half_lives), search, n_iter, warmup_size, test_size, seed),
                       as_of=helpers.last_as_of(frame.iloc[:, 0]), warnings=warnings, piece_ids=("SK-040",))


_MEASURES = {"sharpe_ratio": "SHARPE_RATIO", "sortino_ratio": "SORTINO_RATIO", "calmar_ratio": "CALMAR_RATIO",
             "annualized_sharpe_ratio": "ANNUALIZED_SHARPE_RATIO", "annualized_sortino_ratio": "ANNUALIZED_SORTINO_RATIO"}


@registry.register(
    "validation.score_measure", module="validation",
    summary="Score a fitted portfolio with a portfolio measure turned into a scorer (sharpe_ratio, sortino_ratio, calmar_ratio, ...).",
    inputs={"matrix": {"type": "dataframe", "required": True}, "measure": {"type": "str", "required": False, "default": "sharpe_ratio"},
            "estimator": {"type": "str", "required": False, "default": "equal_weight"}},
    piece_ids=("SK-045",),
)
def score_measure(matrix: pd.DataFrame, measure: str = "sharpe_ratio", estimator: str = "equal_weight") -> QuantResult:
    name = "validation.score_measure"
    if measure not in _MEASURES:
        raise BadInput(f"unknown measure {measure!r}; use one of {sorted(_MEASURES)}")
    frame, warnings = _matrix(matrix, name, 10)
    if frame is None:
        return QuantResult.empty(name, "ratio", "scorer", warnings[-1])
    scorer = _lib.make_scorer(getattr(_lib.RatioMeasure, _MEASURES[measure]))
    fitted = _estimator(estimator).fit(frame)
    return QuantResult(name=name, value=float(scorer(fitted, frame)), unit="ratio", method="scorer",
                       params={"measure": measure, "estimator": estimator}, inputs_hash=helpers.hash_inputs(frame, measure, estimator),
                       as_of=helpers.last_as_of(frame.iloc[:, 0]), warnings=warnings, piece_ids=("SK-045",))

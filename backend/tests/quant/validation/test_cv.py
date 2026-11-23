"""Cross validation: SK-034..SK-041 and SK-045 (index of 1,000 synthetic observations)."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.validation import _lib, cv
from hokma.quant import validation

from .conftest import assert_registered

N = 1000
INDEX = pd.bdate_range("2019-01-01", periods=N)


def _assert_no_leakage(folds, purge, embargo):
    for fold in folds:
        assert not set(fold.train) & set(fold.test), "train and test overlap"
        for start, stop in fold.test_blocks:
            zone = set(range(max(0, start - purge), stop + embargo))
            assert not zone & set(int(t) for t in fold.train), f"train inside the zone of [{start}, {stop})"


def test_cpcv_partitions_purge_and_embargo_SK_035():
    folds = validation.split(INDEX, scheme="cpcv", n_folds=10, purge=5, embargo=3, n_test_folds=2)
    assert len(folds) == 45
    _assert_no_leakage(folds, 5, 3)
    assert all(len(f.test) == 200 for f in folds)
    reference = list(_lib.CombinatorialPurgedCV(n_folds=10, n_test_folds=2, purged_size=5, embargo_size=3).split(np.arange(N).reshape(-1, 1)))
    for fold, (train, tests) in zip(folds, reference):
        assert np.array_equal(fold.train, train)
        assert np.array_equal(fold.test, np.concatenate(tests))


def test_walk_forward_gap_and_equality_SK_034():
    folds = validation.split(INDEX, scheme="walk_forward", n_folds=4, purge=5, embargo=3)
    assert folds
    _assert_no_leakage(folds, 5, 3)
    for fold in folds:
        assert fold.train.max() + 1 + 8 <= fold.test.min()
        assert len(fold.train) == len(fold.test) == N // 5
    reference = list(_lib.WalkForward(test_size=N // 5, train_size=N // 5, purged_size=8).split(np.arange(N).reshape(-1, 1)))
    assert len(reference) == len(folds)
    for fold, (train, test) in zip(folds, reference):
        assert np.array_equal(fold.train, train) and np.array_equal(fold.test, test)


def test_expanding_window_grows_SK_034():
    folds = validation.split(INDEX, scheme="expanding", n_folds=4, purge=2, embargo=0)
    sizes = [len(f.train) for f in folds]
    assert sizes == sorted(sizes) and sizes[-1] > sizes[0]
    _assert_no_leakage(folds, 2, 0)


def test_base_class_is_the_parent_of_cpcv_SK_036():
    assert issubclass(cv.CombinatorialPurgedCV, cv.BaseCombinatorialCV)
    assert cv.CombinatorialPurgedCV is _lib.CombinatorialPurgedCV and cv.WalkForward is _lib.WalkForward


def test_split_is_registered_with_leakage_flag_SK_034_SK_035_SK_036():
    result = registry.call("validation.split", index=N, n_folds=10, purge=5, embargo=3)
    assert result.value["leakage_free"] is True and result.value["n_splits"] == 45
    assert result.value["folds"][0]["test_blocks"] == [[0, 100], [100, 200]] or result.value["folds"][0]["test_blocks"] == [[0, 200]]
    for pid in ("SK-034", "SK-035", "SK-036"):
        assert_registered("validation.split", pid)


def test_split_bad_and_short_inputs_SK_035():
    with pytest.raises(BadInput):
        validation.split(INDEX, scheme="nope", n_folds=4, purge=0, embargo=0)
    with pytest.raises(BadInput):
        validation.split("text", n_folds=4, purge=0, embargo=0)
    short = registry.call("validation.split", index=0, n_folds=10)
    assert short.value is None and short.warnings
    assert registry.call("validation.split", index=3, n_folds=4, scheme="walk_forward").value is None


def test_optimal_folds_equals_origin_SK_037():
    result = registry.call("validation.optimal_folds", n_observations=N, target_train_size=300, target_n_test_paths=10)
    assert result.value == {"n_folds": 6, "n_test_folds": 4}
    assert (result.value["n_folds"], result.value["n_test_folds"]) == _lib.optimal_folds_number(n_observations=N, target_train_size=300, target_n_test_paths=10)
    assert_registered("validation.optimal_folds", "SK-037")
    assert registry.call("validation.optimal_folds", n_observations=100, target_train_size=500, target_n_test_paths=3).value is None


def test_randomized_cv_equals_origin_SK_038(asset_matrix):
    result = registry.call("validation.randomized_cv", matrix=asset_matrix, n_subsamples=3, asset_subset_size=2, train_size=100, test_size=50,
                           window_size=400, seed=0)
    reference = _lib.MultipleRandomizedCV(walk_forward=_lib.WalkForward(test_size=50, train_size=100), n_subsamples=3, asset_subset_size=2,
                                          window_size=400, random_state=0)
    splits = list(reference.split(asset_matrix))
    assert result.value["n_splits"] == len(splits) == reference.get_n_splits(asset_matrix)
    assert result.value["mean_train_size"] == pytest.approx(np.mean([len(s[0]) for s in splits]))
    assert registry.call("validation.randomized_cv", matrix=asset_matrix, n_subsamples=3, asset_subset_size=2, train_size=100, test_size=50,
                         window_size=400, seed=0).inputs_hash == result.inputs_hash
    assert_registered("validation.randomized_cv", "SK-038")


def test_oos_population_equals_origin_SK_039(asset_matrix):
    result = registry.call("validation.oos_population", matrix=asset_matrix, estimator="equal_weight", scheme="cpcv", n_folds=10,
                           n_test_folds=2, purge=5, embargo=2)
    cv_obj = _lib.CombinatorialPurgedCV(n_folds=10, n_test_folds=2, purged_size=5, embargo_size=2)
    population = _lib.cross_val_predict(_lib.EqualWeighted(), asset_matrix, cv=cv_obj)
    expected = np.asarray(population.measures(_lib.RatioMeasure.SHARPE_RATIO), dtype=float)
    assert result.value["n_portfolios"] == len(expected) == 9
    assert np.allclose(result.value["sharpe_per_portfolio"], expected, atol=1e-12)
    assert registry.call("validation.oos_population", matrix=asset_matrix, scheme="walk_forward", train_size=252, test_size=63).value["n_portfolios"] == 1
    assert_registered("validation.oos_population", "SK-039")


def test_online_oos_equals_origin_SK_041(asset_matrix):
    result = registry.call("validation.online_oos", matrix=asset_matrix, half_life=30.0, warmup_size=252, test_size=21)
    est = cv.online_estimator(30.0)
    path = _lib.online_predict(est, asset_matrix, warmup_size=252, test_size=21)
    score = _lib.online_score(est, asset_matrix, warmup_size=252, test_size=21, scoring=_lib.RatioMeasure.SHARPE_RATIO)
    assert result.value["sharpe_online_predict"] == pytest.approx(float(path.sharpe_ratio), abs=1e-12)
    assert result.value["sharpe_online_score"] == pytest.approx(float(score), abs=1e-12)
    assert_registered("validation.online_oos", "SK-041")


@pytest.mark.parametrize("search", ["grid", "random"])
def test_online_search_equals_origin_SK_040(asset_matrix, search):
    result = registry.call("validation.online_search", matrix=asset_matrix, half_lives=[10, 30, 60], search=search, n_iter=3, warmup_size=252,
                           test_size=21, seed=0)
    key = "prior_estimator__covariance_estimator__half_life"
    common = dict(scoring=_lib.RatioMeasure.SHARPE_RATIO, warmup_size=252, test_size=21)
    est = cv.online_estimator(10.0)
    ref = (_lib.OnlineGridSearch(est, param_grid={key: [10, 30, 60]}, **common) if search == "grid"
           else _lib.OnlineRandomizedSearch(est, param_distributions={key: [10, 30, 60]}, n_iter=3, random_state=0, **common))
    ref.fit(asset_matrix)
    assert result.value["best_half_life"] == float(ref.best_params_[key])
    assert result.value["best_score"] == pytest.approx(float(ref.best_score_), abs=1e-12)
    assert_registered("validation.online_search", "SK-040")


def test_score_measure_equals_origin_SK_045(asset_matrix):
    result = registry.call("validation.score_measure", matrix=asset_matrix, measure="sharpe_ratio", estimator="inverse_volatility")
    scorer = _lib.make_scorer(_lib.RatioMeasure.SHARPE_RATIO)
    assert result.value == pytest.approx(float(scorer(_lib.InverseVolatility().fit(asset_matrix), asset_matrix)), abs=1e-12)
    assert_registered("validation.score_measure", "SK-045")
    with pytest.raises(BadInput):
        registry.call("validation.score_measure", matrix=asset_matrix, measure="made_up")


@pytest.mark.parametrize("name,kwargs", [
    ("validation.randomized_cv", dict(n_subsamples=2, asset_subset_size=1, train_size=50, test_size=20)),
    ("validation.oos_population", dict()),
    ("validation.online_oos", dict()),
    ("validation.online_search", dict(half_lives=[10, 30])),
    ("validation.score_measure", dict()),
])
def test_empty_constant_and_nan_matrices_give_none_SK_038_to_SK_045(name, kwargs):
    empty = pd.DataFrame({"a": []}, dtype=float)
    constant = pd.DataFrame(0.01, index=range(600), columns=["a", "b"])
    nan = pd.DataFrame(np.nan, index=range(600), columns=["a", "b"])
    for bad in (empty, constant, nan):
        result = registry.call(name, matrix=bad, **kwargs)
        assert result.value is None and result.warnings


def test_wrong_matrix_type_is_bad_input_SK_039():
    with pytest.raises(BadInput):
        registry.call("validation.oos_population", matrix=[1, 2, 3])

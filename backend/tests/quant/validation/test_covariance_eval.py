"""Covariance forecast calibration: SK-042, SK-043, SK-044."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.validation import _lib

from .conftest import assert_registered


@pytest.fixture(scope="module")
def panel():
    rng = np.random.default_rng(99)
    return pd.DataFrame(rng.normal(0, 0.01, (600, 4)), index=pd.bdate_range("2020-01-01", periods=600), columns=list("abcd"))


def test_online_distribution_equals_origin_SK_042(panel):
    result = registry.call("validation.cov_forecast_online", matrix=panel, half_life=30.0, warmup_size=252, test_size=1)
    ev = _lib.online_covariance_forecast_evaluation(_lib.EWCovariance(half_life=30.0), panel, warmup_size=252, test_size=1)
    assert result.value["n_steps"] == len(ev.observations) == 600 - 252
    assert result.value["mahalanobis_ratio"]["mean"] == pytest.approx(float(np.nanmean(ev.mahalanobis_calibration_ratio)), abs=1e-12)
    assert result.value["diagonal_ratio"]["p95"] == pytest.approx(float(np.nanquantile(ev.diagonal_calibration_ratio, 0.95)), abs=1e-12)
    assert 0.5 < result.value["mahalanobis_ratio"]["mean"] < 2.0
    assert_registered("validation.cov_forecast_online", "SK-042")


def test_compare_batch_equals_origin_SK_043(panel):
    result = registry.call("validation.cov_forecast_compare", matrix=panel, half_lives=[10, 60], train_size=252, test_size=1)
    refs = [_lib.covariance_forecast_evaluation(_lib.EWCovariance(half_life=h), panel, train_size=252, test_size=1) for h in (10, 60)]
    for h, ev in zip((10, 60), refs):
        assert result.value["models"][str(h)]["qlike"] == pytest.approx(float(np.nanmean(ev.portfolio_variance_qlike_loss)), abs=1e-12)
    comparison = _lib.CovarianceForecastComparison(refs, names=["a", "b"])
    assert result.value["comparison_rows"] == len(comparison.summary())
    assert result.value["n_models"] == 2 and result.value["best_by_qlike"] in (10.0, 60.0)
    assert_registered("validation.cov_forecast_compare", "SK-043")


def test_calibration_metrics_equal_origin_SK_044(panel):
    result = registry.call("validation.cov_calibration", matrix=panel, half_life=30.0, warmup_size=252, test_size=1)
    ev = _lib.online_covariance_forecast_evaluation(_lib.EWCovariance(half_life=30.0), panel, warmup_size=252, test_size=1)
    summary = ev.summary()
    assert result.value["mahalanobis_ratio"] == pytest.approx(float(summary.loc["Mahalanobis ratio", "mean"]), abs=1e-12)
    assert result.value["diagonal_ratio"] == pytest.approx(float(summary.loc["Diagonal ratio", "mean"]), abs=1e-12)
    assert result.value["qlike"] == pytest.approx(float(summary.loc["Portfolio QLIKE", "mean"]), abs=1e-12)
    exceed = ev.exceedance_summary()
    assert result.value["exceedance_rate"]["0.95"] == pytest.approx(float(exceed.loc[0.95, "observed_rate"]), abs=1e-12)
    assert 0.0 < result.value["exceedance_rate"]["0.99"] < 0.1
    assert_registered("validation.cov_calibration", "SK-044")


@pytest.mark.parametrize("name,kwargs", [("validation.cov_forecast_online", {}), ("validation.cov_calibration", {}),
                                         ("validation.cov_forecast_compare", {"half_lives": [10, 30]})])
def test_empty_constant_and_nan_give_none_SK_042_to_SK_044(name, kwargs):
    for bad in (pd.DataFrame({"a": []}, dtype=float), pd.DataFrame(0.01, index=range(600), columns=["a", "b"]),
                pd.DataFrame(np.nan, index=range(600), columns=["a", "b"])):
        result = registry.call(name, matrix=bad, **kwargs)
        assert result.value is None and result.warnings

"""Moments of a returns series. Rows SK-016 to SK-022 and SK-033."""
import math

import numpy as np
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.risk import _lib, moments

from .conftest import call, piece_ids

TOL = 1e-9
M = _lib.sk_measures


def _dev(x):
    x = np.asarray(x, dtype=float)
    return x - x.mean()


def test_mean_SK_016(ret, tiny):
    assert call("risk.mean", returns=ret).value == pytest.approx(M.mean(ret.to_numpy()), abs=TOL)
    assert call("risk.mean", returns=tiny).value == pytest.approx(0.006, abs=TOL)
    assert "SK-016" in piece_ids("risk.mean")


def test_mean_absolute_deviation_SK_017(ret, tiny):
    assert call("risk.mean_absolute_deviation", returns=ret).value == pytest.approx(M.mean_absolute_deviation(ret.to_numpy()), abs=TOL)
    assert call("risk.mean_absolute_deviation", returns=tiny).value == pytest.approx(0.0168, abs=TOL)
    assert "SK-017" in piece_ids("risk.mean_absolute_deviation")


def test_first_lower_partial_moment_SK_018(ret, tiny):
    got = call("risk.first_lower_partial_moment", returns=ret, min_acceptable_return=0.0).value
    assert got == pytest.approx(M.first_lower_partial_moment(ret.to_numpy(), min_acceptable_return=0.0), abs=TOL)
    # shortfalls below zero are 0.02 and 0.01, averaged over 5 observations
    assert call("risk.first_lower_partial_moment", returns=tiny, min_acceptable_return=0.0).value == pytest.approx(0.006, abs=TOL)
    assert "SK-018" in piece_ids("risk.first_lower_partial_moment")


def test_variance_and_semi_variance_SK_019(ret, tiny):
    assert call("risk.variance", returns=ret).value == pytest.approx(M.variance(ret.to_numpy()), abs=TOL)
    assert call("risk.variance", returns=tiny).value == pytest.approx(0.00043, abs=TOL)
    assert call("risk.variance", returns=tiny, biased=True).value == pytest.approx(0.000344, abs=TOL)
    assert call("risk.semi_variance", returns=ret).value == pytest.approx(M.semi_variance(ret.to_numpy()), abs=TOL)
    # below zero: 0.02 and 0.01, squared sum 0.0005, over n-1 = 4
    assert call("risk.semi_variance", returns=tiny, min_acceptable_return=0.0).value == pytest.approx(0.000125, abs=TOL)
    assert {"SK-019"} <= set(piece_ids("risk.variance")) & set(piece_ids("risk.semi_variance"))


def test_standard_deviation_and_semi_deviation_SK_020(ret, tiny):
    assert call("risk.standard_deviation", returns=ret).value == pytest.approx(M.standard_deviation(ret.to_numpy()), abs=TOL)
    assert call("risk.standard_deviation", returns=tiny).value == pytest.approx(math.sqrt(0.00043), abs=TOL)
    assert call("risk.semi_deviation", returns=ret).value == pytest.approx(M.semi_deviation(ret.to_numpy()), abs=TOL)
    assert call("risk.semi_deviation", returns=tiny, min_acceptable_return=0.0).value == pytest.approx(math.sqrt(0.000125), abs=TOL)
    assert "SK-020" in piece_ids("risk.standard_deviation") and "SK-020" in piece_ids("risk.semi_deviation")


def test_third_central_moment_and_skew_SK_021(ret, tiny):
    assert call("risk.third_central_moment", returns=ret).value == pytest.approx(M.third_central_moment(ret.to_numpy()), abs=TOL)
    assert call("risk.third_central_moment", returns=tiny).value == pytest.approx(float((_dev(tiny) ** 3).mean()), abs=TOL)
    assert call("risk.skew", returns=ret).value == pytest.approx(M.skew(ret.to_numpy()), abs=TOL)
    d = _dev(tiny)
    assert call("risk.skew", returns=tiny).value == pytest.approx(float((d ** 3).mean() / (d ** 2).mean() ** 1.5), abs=TOL)
    assert "SK-021" in piece_ids("risk.skew") and "SK-021" in piece_ids("risk.third_central_moment")


def test_fourth_moments_and_kurtosis_SK_022(ret, tiny):
    d = _dev(tiny)
    assert call("risk.fourth_central_moment", returns=ret).value == pytest.approx(M.fourth_central_moment(ret.to_numpy()), abs=TOL)
    assert call("risk.fourth_central_moment", returns=tiny).value == pytest.approx(float((d ** 4).mean()), abs=TOL)
    assert call("risk.kurtosis", returns=ret).value == pytest.approx(M.kurtosis(ret.to_numpy()), abs=TOL)
    assert call("risk.kurtosis", returns=tiny).value == pytest.approx(float((d ** 4).mean() / (d ** 2).mean() ** 2), abs=TOL)
    got = call("risk.fourth_lower_partial_moment", returns=ret, min_acceptable_return=0.0).value
    assert got == pytest.approx(M.fourth_lower_partial_moment(ret.to_numpy(), min_acceptable_return=0.0), abs=TOL)
    assert call("risk.fourth_lower_partial_moment", returns=tiny, min_acceptable_return=0.0).value == pytest.approx((0.02 ** 4 + 0.01 ** 4) / 5, abs=TOL)
    for name in ("risk.fourth_central_moment", "risk.kurtosis", "risk.fourth_lower_partial_moment"):
        assert "SK-022" in piece_ids(name)


def test_correlation_with_sample_weights_SK_033(panel):
    res = call("risk.correlation", panel=panel)
    assert res.value["assets"] == list(panel.columns)
    assert np.allclose(res.value["matrix"], M.correlation(panel.to_numpy()), atol=TOL)
    assert np.allclose(res.value["matrix"], np.corrcoef(panel.to_numpy(), rowvar=False), atol=TOL)
    weights = np.linspace(1.0, 2.0, len(panel))
    weighted = call("risk.correlation", panel=panel, sample_weight=list(weights))
    expected = M.correlation(panel.to_numpy(), sample_weight=weights / weights.sum())
    assert np.allclose(weighted.value["matrix"], expected, atol=TOL)
    assert weighted.method == "weighted_correlation"
    # hand value: two perfectly anti correlated columns
    import pandas as pd
    x = np.linspace(-1, 1, 20)
    anti = pd.DataFrame({"a": x, "b": -x})
    assert call("risk.correlation", panel=anti).value["matrix"][0][1] == pytest.approx(-1.0, abs=TOL)
    assert "SK-033" in piece_ids("risk.correlation")


def test_correlation_bad_input_SK_033(panel):
    with pytest.raises(BadInput):
        call("risk.correlation", panel=[1, 2, 3])
    with pytest.raises(BadInput):
        call("risk.correlation", panel=panel[["AAA"]])
    with pytest.raises(BadInput):
        call("risk.correlation", panel=panel, sample_weight=[1.0, 2.0])
    constant = panel.copy()
    constant["AAA"] = 0.01
    res = call("risk.correlation", panel=constant)
    assert res.value is None and res.warnings


def test_module_functions_listed_SK_016_to_033():
    names = {s.name for s in registry.list("risk")}
    expected = {"risk.mean", "risk.mean_absolute_deviation", "risk.first_lower_partial_moment", "risk.variance", "risk.semi_variance",
                "risk.standard_deviation", "risk.semi_deviation", "risk.third_central_moment", "risk.skew",
                "risk.fourth_central_moment", "risk.kurtosis", "risk.fourth_lower_partial_moment", "risk.correlation"}
    assert expected <= names
    assert moments.mean.__name__ == "mean"

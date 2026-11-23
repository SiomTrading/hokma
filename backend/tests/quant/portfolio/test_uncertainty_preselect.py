"""SK-077 (uncertainty sets), SK-079 (drop zero variance) and SK-080 (rule based selection)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _lib
from hokma.quant.portfolio.uncertainty import CompactCovarianceUncertaintySet, UncertaintySet

from .conftest import call

SMALL = pd.DataFrame({"a": [0.01, -0.01, 0.02, -0.02], "b": [0.02, 0.0, -0.02, 0.0]})
# chi-square quantiles at 95 percent: 2 degrees of freedom is -2 ln(0.05); 4 degrees of freedom from the table
CHI2_95_2 = -2.0 * math.log(0.05)
CHI2_95_4 = 9.487729036781154


def test_sk077_mean_set_radius_and_geometry_by_hand():
    result = call("portfolio.uncertainty_set", returns=SMALL)
    assert result.value["radius"] == pytest.approx(math.sqrt(CHI2_95_2), abs=1e-12)
    # standard error of each mean: sample std / sqrt(n): a has sum of squares 1e-3 over 3, b has 8e-4 over 3
    expected = [math.sqrt(1e-3 / 3.0) / 2.0, math.sqrt(8e-4 / 3.0) / 2.0]
    assert result.value["geometry_diagonal"] == pytest.approx(expected, abs=1e-12)
    assert result.value["norm"] == 2.0 and result.value["dual_norm"] == 2.0 and result.piece_ids == ("SK-077",)


def test_sk077_worst_case_loss_of_weights_is_radius_times_the_dual_norm_of_the_exposure():
    result = call("portfolio.uncertainty_set", returns=SMALL, weights={"a": 0.5, "b": 0.5})
    se = [math.sqrt(1e-3 / 3.0) / 2.0, math.sqrt(8e-4 / 3.0) / 2.0]
    expected = math.sqrt(CHI2_95_2) * math.sqrt((0.5 * se[0]) ** 2 + (0.5 * se[1]) ** 2)
    assert result.value["worst_case_return_loss"] == pytest.approx(expected, abs=1e-12)


def test_sk077_covariance_set_and_the_library(panel):
    result = call("portfolio.uncertainty_set", returns=SMALL, target="covariance")
    assert result.value["radius"] == pytest.approx(math.sqrt(CHI2_95_4), abs=1e-12) and result.value["geometry_shape"] == [4, 4]
    direct = _lib.sk_uncertainty.EmpiricalMuUncertaintySet(confidence_level=0.9, diagonal=False).fit(panel).uncertainty_set_
    mine = call("portfolio.uncertainty_set", returns=panel, confidence_level=0.9, diagonal=False)
    assert mine.value["radius"] == pytest.approx(direct.radius, abs=1e-12) and mine.value["geometry_shape"] == list(direct.geometry.shape)
    container = UncertaintySet(float(direct.radius), np.asarray(direct.geometry), float(direct.norm))
    w = np.full(5, 0.2)
    assert container.worst_case(w) == pytest.approx(float(direct.radius * np.linalg.norm(direct.geometry.T @ w)), abs=1e-14)


def test_sk077_compact_covariance_penalty_by_hand():
    # C = I, Q = e1, w = (0.3, 0.7): C w - Q Q' C w = (0, 0.7), penalty = radius * 0.49
    result = call("portfolio.compact_covariance_set", radius=2.0, metric_sqrt=[1, 1], basis=[[1], [0]], weights=[0.3, 0.7])
    assert result.value["variance_penalty"] == pytest.approx(0.98, abs=1e-12) and result.value["rank"] == 1
    assert CompactCovarianceUncertaintySet(2.0, np.array([1.0, 1.0]), np.array([[1.0], [0.0]])).penalty(np.array([0.3, 0.7])) == pytest.approx(0.98)
    scaled = call("portfolio.compact_covariance_set", radius=1.0, metric_sqrt=[2, 1], basis=[[1], [0]], weights=[0.3, 0.7])
    assert scaled.value["variance_penalty"] == pytest.approx(0.49, abs=1e-12)          # only the second coordinate is left over


def test_sk077_empty_nan_and_out_of_domain_give_none():
    assert call("portfolio.uncertainty_set", returns=SMALL.iloc[:0]).value is None
    assert call("portfolio.uncertainty_set", returns=SMALL, confidence_level=1.0).value is None
    assert call("portfolio.uncertainty_set", returns=SMALL, target="vol").value is None
    assert call("portfolio.uncertainty_set", returns=SMALL, target="covariance", weights={"a": 1.0}).value is None
    nan = SMALL.copy()
    nan["a"] = np.nan
    assert call("portfolio.uncertainty_set", returns=nan).value is None
    assert call("portfolio.compact_covariance_set", radius=-1.0, metric_sqrt=[1, 1], basis=[[1], [0]]).value is None
    assert call("portfolio.compact_covariance_set", radius=1.0, metric_sqrt=[1, 1], basis=[[1], [1]]).value is None      # not orthonormal
    assert call("portfolio.compact_covariance_set", radius=1.0, metric_sqrt=[1, float("nan")], basis=[[1], [0]]).value is None
    assert call("portfolio.compact_covariance_set", radius=1.0, metric_sqrt=[1, 1], basis=[[1], [0]], weights=[1.0]).value is None
    with pytest.raises(BadInput):
        call("portfolio.compact_covariance_set", radius=1.0, metric_sqrt="x", basis=[[1], [0]])
    with pytest.raises(BadInput):
        call("portfolio.uncertainty_set", returns=SMALL, weights={"zzz": 1.0})


def test_sk079_a_flat_series_is_dropped_and_the_threshold_decides(panel):
    flat = panel.copy()
    flat["ZZZ"] = 0.0
    result = call("portfolio.drop_zero_variance", returns=flat)
    assert result.value["dropped"] == ["ZZZ"] and result.value["kept"] == list(panel.columns) and result.piece_ids == ("SK-079",)
    assert result.value["variance"]["ZZZ"] == 0.0
    direct = _lib.sk_pre_selection.DropZeroVariance().fit(flat).to_keep_
    assert [c for c, k in zip(flat.columns, direct) if k] == result.value["kept"]
    # a threshold above the smallest variance drops that asset too (population variance of the fixture, by hand with numpy)
    smallest = panel.var(ddof=0).sort_values().index[0]
    level = float(panel.var(ddof=0).sort_values().iloc[0]) * 1.01
    assert call("portfolio.drop_zero_variance", returns=panel, threshold=level).value["dropped"] == [smallest]


def test_sk079_empty_every_flat_and_out_of_domain(panel):
    assert call("portfolio.drop_zero_variance", returns=panel.iloc[:0]).value is None
    assert call("portfolio.drop_zero_variance", returns=panel * 0.0).value is None
    assert call("portfolio.drop_zero_variance", returns=panel, threshold=-1.0).value is None
    nan = panel.copy()
    nan.iloc[:, :] = np.nan
    assert call("portfolio.drop_zero_variance", returns=nan).value is None


def test_sk080_complete_keeps_assets_without_gaps(panel):
    gap = panel.copy()
    gap.iloc[:10, 4] = np.nan
    result = call("portfolio.select", returns=gap, method="complete")
    assert result.value["dropped"] == ["EEE"] and result.value["kept"] == ["AAA", "BBB", "CCC", "DDD"] and result.piece_ids == ("SK-080",)
    direct = _lib.sk_pre_selection.SelectComplete().fit(gap).to_keep_
    assert [c for c, k in zip(gap.columns, direct) if k] == result.value["kept"]


def test_sk080_k_extremes_by_hand_and_non_dominated_match_the_library(panel):
    means = pd.DataFrame({"low": [0.00, 0.01, 0.00, 0.01], "mid": [0.01, 0.02, 0.01, 0.02], "high": [0.02, 0.03, 0.02, 0.03]})
    top = call("portfolio.select", returns=means, method="k_extremes", k=2, measure="mean")
    assert top.value["kept"] == ["mid", "high"] and top.value["dropped"] == ["low"]
    bottom = call("portfolio.select", returns=means, method="k_extremes", k=1, measure="mean", highest=False)
    assert bottom.value["kept"] == ["low"]
    nd = call("portfolio.select", returns=panel, method="non_dominated")
    keep = _lib.sk_pre_selection.SelectNonDominated().fit(panel).to_keep_
    assert nd.value["kept"] == [c for c, k in zip(panel.columns, keep) if k]


def test_sk080_non_expiring_removes_the_expired_and_the_ones_about_to_expire(panel):
    last = panel.index[-1]
    assert str(last.date()) == "2023-10-31"
    expirations = {"AAA": "2020-02-01", "BBB": "2030-01-01", "CCC": "2023-11-05"}
    now = call("portfolio.select", returns=panel, method="non_expiring", expirations=expirations)
    assert now.value["dropped"] == ["AAA"]                                  # expired long ago; the others are alive
    soon = call("portfolio.select", returns=panel, method="non_expiring", expirations=expirations, lookahead_days=10)
    assert soon.value["dropped"] == ["AAA", "CCC"]                          # CCC expires 5 days after the last date
    short = call("portfolio.select", returns=panel, method="non_expiring", expirations=expirations, lookahead_days=3)
    assert short.value["dropped"] == ["AAA"]
    earlier = call("portfolio.select", returns=panel, method="non_expiring", expirations=expirations, as_of="2023-01-02")
    assert earlier.value["dropped"] == ["AAA"]
    assert call("portfolio.select", returns=panel, method="non_expiring", expirations={"BBB": "2023-01-10"}, lookahead_days=10,
                as_of="2023-01-02").value["dropped"] == ["BBB"]             # expires within 10 days of the as_of date


def test_sk080_empty_missing_and_out_of_domain(panel):
    assert call("portfolio.select", returns=panel.iloc[:0], method="complete").value is None
    assert call("portfolio.select", returns=panel, method="magic").value is None
    assert call("portfolio.select", returns=panel, method="non_expiring").value is None                    # needs expirations
    assert call("portfolio.select", returns=panel, method="non_expiring", expirations={"AAA": "not a date"}).value is None
    assert call("portfolio.select", returns=panel, method="non_expiring", expirations={"AAA": "2020-01-01"}, lookahead_days=-1).value is None
    assert call("portfolio.select", returns=panel, method="k_extremes", k=0).value is None
    assert call("portfolio.select", returns=panel, method="k_extremes", measure="charm").value is None
    with pytest.raises(BadInput):
        call("portfolio.select", returns=panel, method="non_expiring", expirations={"ZZZ": "2030-01-01"})
    with pytest.raises(BadInput):
        call("portfolio.select", returns=panel.reset_index(drop=True), method="non_expiring", expirations={"AAA": "2030-01-01"})
    assert registry.describe("portfolio.select").piece_ids == ("SK-080",)

"""P-10: the risk overlay scales every position down when the risk of the portfolio is above the target, and never up."""
from __future__ import annotations

import pandas as pd
import pytest

from hokma.quant.errors import BadInput

from .conftest import call


def _cov(a: float = 4e-4, b: float = 1e-4, ab: float = 0.0) -> pd.DataFrame:
    return pd.DataFrame([[a, ab], [ab, b]], index=["x", "y"], columns=["x", "y"])


def test_p10_a_risk_of_twice_the_target_scales_every_position_by_one_half():
    out = call("portfolio.risk_overlay", positions={"x": 1.0}, covariance=_cov(), target_risk=0.01)   # risk sqrt(4e-4) = 0.02
    assert out.piece_ids == ("P-10",)
    assert out.value["scale"] == pytest.approx(0.5) and out.value["risk_before"] == pytest.approx(0.02)
    assert out.value["risk_after"] == pytest.approx(0.01) and out.value["reduced"] is True
    assert out.value["positions"] == pytest.approx({"x": 0.5, "y": 0.0})


def test_p10_all_positions_get_the_same_factor_and_the_proportions_stay():
    out = call("portfolio.risk_overlay", positions={"x": 2.0, "y": -4.0}, covariance=_cov(), target_risk=0.02)
    # variance 4 * 4e-4 + 16 * 1e-4 = 3.2e-3, risk 0.0565685; the factor is 0.02 / 0.0565685 = 0.353553
    assert out.value["risk_before"] == pytest.approx(0.0565685425, rel=1e-9)
    assert out.value["scale"] == pytest.approx(0.3535533906, rel=1e-9)
    assert out.value["positions"]["x"] / out.value["positions"]["y"] == pytest.approx(-0.5)


def test_p10_the_correlation_enters_the_portfolio_risk():
    # long x and long y with a covariance of 1.5e-4: variance 4e-4 + 1e-4 + 2 * 1.5e-4 = 8e-4
    out = call("portfolio.risk_overlay", positions={"x": 1.0, "y": 1.0}, covariance=_cov(ab=1.5e-4), target_risk=0.01)
    assert out.value["risk_before"] == pytest.approx(8e-4 ** 0.5, rel=1e-12)
    # opposite positions hedge: 4e-4 + 1e-4 - 3e-4 = 2e-4
    hedged = call("portfolio.risk_overlay", positions={"x": 1.0, "y": -1.0}, covariance=_cov(ab=1.5e-4), target_risk=0.01)
    assert hedged.value["risk_before"] == pytest.approx(2e-4 ** 0.5, rel=1e-12)


@pytest.mark.parametrize("target", [0.02, 0.03, 1.0, 100.0])
def test_p10_it_never_scales_above_one_and_leaves_the_positions_alone_when_the_risk_is_within_the_target(target):
    out = call("portfolio.risk_overlay", positions={"x": 1.0, "y": 3.0}, covariance=_cov(), target_risk=target)
    risk = out.value["risk_before"]                                         # sqrt(4e-4 + 9e-4) = 0.036055
    if target >= risk:
        assert out.value["scale"] == 1.0 and out.value["reduced"] is False and out.value["positions"] == {"x": 1.0, "y": 3.0}
    else:
        assert 0.0 < out.value["scale"] < 1.0
    assert out.value["scale"] <= 1.0


def test_p10_a_position_that_is_not_in_the_dict_is_zero_and_an_empty_risk_needs_no_scaling():
    out = call("portfolio.risk_overlay", positions={"y": 0.0}, covariance=_cov(), target_risk=0.01)
    assert (out.value["scale"], out.value["risk_before"]) == (1.0, 0.0)


def test_p10_bad_inputs_are_refused_or_say_why():
    base = dict(positions={"x": 1.0}, covariance=_cov(), target_risk=0.01)
    with pytest.raises(BadInput):
        call("portfolio.risk_overlay", **{**base, "positions": {}})
    with pytest.raises(BadInput):
        call("portfolio.risk_overlay", **{**base, "positions": {"zzz": 1.0}})
    with pytest.raises(BadInput):
        call("portfolio.risk_overlay", **{**base, "covariance": pd.DataFrame([[1.0, 0.0]], columns=["x", "y"])})
    with pytest.raises(BadInput):
        call("portfolio.risk_overlay", **{**base, "covariance": _cov().set_axis(["y", "x"], axis=0)})
    with pytest.raises(BadInput):
        call("portfolio.risk_overlay", **{**base, "target_risk": "0.01"})
    assert call("portfolio.risk_overlay", **{**base, "target_risk": 0}).value is None
    assert call("portfolio.risk_overlay", **{**base, "positions": {"x": float("nan")}}).value is None
    assert call("portfolio.risk_overlay", **{**base, "covariance": _cov(a=float("nan"))}).value is None
    assert call("portfolio.risk_overlay", **{**base, "covariance": _cov(a=-1e-4)}).value is None       # a negative variance is not a covariance

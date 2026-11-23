"""Metrics part B: trade-style statistics, risk of ruin, Kelly cap and tail view (QS-006, QS-007, QS-008, QS-022a, QS-023, QS-025, QS-027, QS-031)."""
import itertools
import math
from statistics import NormalDist

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.metrics import _lib, trades

from .metrics_b_qa import assert_bad_data_gives_none, assert_close, assert_registered, call, returns

R = returns()
N = len(R)


def lib():
    return _lib.returns_stats()


def _longest(flags):
    return max((len(list(g)) for k, g in itertools.groupby(flags) if k), default=0)


def test_consecutive_streaks_QS_006():
    assert_registered("metrics.consecutive_streaks", "QS-006")
    res = call("metrics.consecutive_streaks", returns=R)
    assert res.value["max_consecutive_wins"] == _longest((R > 0).tolist()) == lib().consecutive_wins(R, prepare_returns=False)
    assert res.value["max_consecutive_losses"] == _longest((R < 0).tolist()) == lib().consecutive_losses(R, prepare_returns=False)
    known = pd.Series([0.01, 0.02, 0.03, -0.01, -0.02, 0.01, -0.01, -0.03, -0.02, -0.04], index=pd.bdate_range("2022-01-03", periods=10))
    hand = call("metrics.consecutive_streaks", returns=known)
    assert hand.value == {"max_consecutive_wins": 3, "max_consecutive_losses": 4}
    assert_bad_data_gives_none(lambda s: call("metrics.consecutive_streaks", returns=s))


def test_exposure_QS_007():
    assert_registered("metrics.exposure", "QS-007")
    assert_close(call("metrics.exposure", returns=R).value, lib().exposure(R, prepare_returns=False))
    half = pd.Series([0.01, 0.0, -0.02, 0.0, 0.03, 0.0, 0.01, 0.0, 0.02, 0.0], index=pd.bdate_range("2022-01-03", periods=10))
    assert_close(call("metrics.exposure", returns=half).value, 0.5)
    assert_close(call("metrics.exposure", returns=half.iloc[:3] + 0.001).value, 1.0)
    assert_bad_data_gives_none(lambda s: call("metrics.exposure", returns=s))


def test_win_rate_stats_QS_008():
    assert_registered("metrics.win_rate_stats", "QS-008")
    res = call("metrics.win_rate_stats", returns=R)
    wins, losses = R[R > 0], R[R < 0]
    assert_close(res.value["win_rate"], len(wins) / (len(wins) + len(losses)))
    assert_close(res.value["avg_win"], wins.mean())
    assert_close(res.value["avg_loss"], losses.mean())
    assert_close(res.value["avg_return"], R[R != 0].mean())
    assert_close(res.value["win_rate"], lib().win_rate(R, prepare_returns=False))
    known = pd.Series([0.02, -0.01, 0.04, -0.03], index=pd.bdate_range("2022-01-03", periods=4))
    hand = call("metrics.win_rate_stats", returns=known).value
    assert_close(hand, {"win_rate": 0.5, "avg_win": 0.03, "avg_loss": -0.02, "avg_return": 0.005})
    assert_bad_data_gives_none(lambda s: call("metrics.win_rate_stats", returns=s))


def test_payoff_ratios_QS_025():
    assert_registered("metrics.payoff_ratios", "QS-025")
    res = call("metrics.payoff_ratios", returns=R)
    wins, losses = R[R > 0], R[R < 0]
    assert_close(res.value["payoff_ratio"], wins.mean() / abs(losses.mean()))
    assert_close(res.value["win_loss_ratio"], res.value["payoff_ratio"])
    assert_close(res.value["profit_factor"], R[R >= 0].sum() / abs(losses.sum()))
    assert_close(res.value["profit_ratio"], lib().profit_ratio(R, prepare_returns=False))
    known = pd.Series([0.02, -0.01, 0.04, -0.03], index=pd.bdate_range("2022-01-03", periods=4))
    hand = call("metrics.payoff_ratios", returns=known).value
    assert_close(hand["payoff_ratio"], 1.5)
    assert_close(hand["profit_factor"], 0.06 / 0.04)
    assert_bad_data_gives_none(lambda s: call("metrics.payoff_ratios", returns=s))


def test_outlier_ratios_QS_027():
    assert_registered("metrics.outlier_ratios", "QS-027")
    res = call("metrics.outlier_ratios", returns=R)
    assert_close(res.value["outlier_win_ratio"], R.quantile(0.99) / R[R >= 0].mean())
    assert_close(res.value["outlier_loss_ratio"], R.quantile(0.01) / R[R < 0].mean())
    assert_close(res.value["outlier_win_ratio"], lib().outlier_win_ratio(R, prepare_returns=False))
    assert_close(res.value["outlier_loss_ratio"], lib().outlier_loss_ratio(R, prepare_returns=False))
    assert_bad_data_gives_none(lambda s: call("metrics.outlier_ratios", returns=s))


def test_kelly_cap_QS_031():
    assert_registered("metrics.kelly_cap", "QS-031")
    res = call("metrics.kelly_cap", returns=R)
    wins, losses = R[R > 0], R[R < 0]
    payoff = wins.mean() / abs(losses.mean())
    p = len(wins) / (len(wins) + len(losses))
    assert_close(res.value["kelly"], (payoff * p - (1 - p)) / payoff)
    assert_close(res.value["kelly"], lib().kelly_criterion(R, prepare_returns=False))
    half = call("metrics.kelly_cap", returns=R, fraction=0.5)
    assert_close(half.value["position_cap"], 0.5 * res.value["kelly"])
    losing = pd.Series([0.01, -0.03, 0.01, -0.03, 0.02, -0.04, 0.01, -0.02], index=pd.bdate_range("2022-01-03", periods=8))
    neg = call("metrics.kelly_cap", returns=losing)
    assert neg.value["kelly"] < 0 and neg.value["position_cap"] == 0.0 and neg.warnings
    for bad in (0.0, 1.5, -1.0):
        with pytest.raises(BadInput):
            call("metrics.kelly_cap", returns=R, fraction=bad)
    assert_bad_data_gives_none(lambda s: call("metrics.kelly_cap", returns=s))


def test_risk_of_ruin_closed_form_QS_022a():
    assert_registered("metrics.risk_of_ruin", "QS-022a")
    assert_registered("metrics.risk_of_ruin_params", "QS-022a")
    # Symmetric steps in log wealth: (1 + W)(1 - L) = 1, so ruin probability is (q / p) ** (barrier / step).
    win = 0.10
    loss = 1 - 1 / 1.10
    p, level = 0.6, 0.5
    expected = ((1 - p) / p) ** (math.log(2) / math.log(1.10))
    res = call("metrics.risk_of_ruin_params", win_rate=p, avg_win=win, avg_loss=loss, ruin_level=level)
    assert_close(res.value, expected, tol=1e-9)
    assert res.unit == "prob" and res.params["ruin_level"] == level
    # no positive drift: certain ruin; no losses: no ruin; no wins: certain ruin
    assert call("metrics.risk_of_ruin_params", win_rate=0.4, avg_win=0.01, avg_loss=0.01).value == 1.0
    assert call("metrics.risk_of_ruin_params", win_rate=1.0, avg_win=0.01, avg_loss=0.0).value == 0.0
    assert call("metrics.risk_of_ruin_params", win_rate=0.0, avg_win=0.01, avg_loss=0.01).value == 1.0
    one_loss = call("metrics.risk_of_ruin_params", win_rate=0.9, avg_win=0.5, avg_loss=1.0)
    assert one_loss.value == 1.0 and one_loss.warnings
    assert call("metrics.risk_of_ruin_params", win_rate=1.5, avg_win=0.01, avg_loss=0.01).value is None
    assert call("metrics.risk_of_ruin_params", win_rate=float("nan"), avg_win=0.01, avg_loss=0.01).value is None
    with pytest.raises(BadInput):
        call("metrics.risk_of_ruin_params", win_rate=0.5, avg_win=0.01, avg_loss=0.01, ruin_level=1.0)
    # more favourable edge means lower ruin probability; a deeper barrier means a lower probability
    base = call("metrics.risk_of_ruin_params", win_rate=0.55, avg_win=0.02, avg_loss=0.015, ruin_level=0.3).value
    better = call("metrics.risk_of_ruin_params", win_rate=0.65, avg_win=0.02, avg_loss=0.015, ruin_level=0.3).value
    deeper = call("metrics.risk_of_ruin_params", win_rate=0.55, avg_win=0.02, avg_loss=0.015, ruin_level=0.6).value
    assert better < base and deeper < base
    # the root of the adjustment equation holds
    a, c = math.log1p(0.02), -math.log1p(-0.015)
    theta = -math.log(base) / -math.log1p(-0.3)
    assert abs(0.55 * math.exp(-theta * a) + 0.45 * math.exp(theta * c) - 1.0) < 1e-9
    assert_bad_data_gives_none(lambda s: call("metrics.risk_of_ruin", returns=s))


def test_risk_of_ruin_from_series_QS_022a():
    res = call("metrics.risk_of_ruin", returns=R)
    wins, losses = R[R > 0], R[R < 0]
    hand = trades.ruin_probability(len(wins) / (len(wins) + len(losses)), float(wins.mean()), float(-losses.mean()), 0.5)
    assert_close(res.value, hand)
    assert_close(res.params["avg_win"], float(wins.mean()))
    simple_formula = ((1 - 0.504) / (1 + 0.504)) ** N     # the sample-size formula would give practically zero
    assert res.value > 100 * simple_formula
    with pytest.raises(BadInput):
        call("metrics.risk_of_ruin", returns=R, ruin_level=0.0)
    allwins = pd.Series([0.01, 0.02, 0.03, 0.01], index=pd.bdate_range("2022-01-03", periods=4))
    assert call("metrics.risk_of_ruin", returns=allwins).value == 0.0
    zeros_then = pd.Series([0.0, 0.0, 0.0, 0.0, 0.0001], index=pd.bdate_range("2022-01-03", periods=5))
    assert call("metrics.risk_of_ruin", returns=zeros_then).value == 0.0


@pytest.mark.parametrize("method", ["parametric", "historical"])
def test_tail_view_QS_023(method):
    assert_registered("metrics.tail_view", "QS-023")
    res = call("metrics.tail_view", returns=R, level=0.95, method=method)
    mu, sigma = R.mean(), R.std(ddof=1)
    if method == "parametric":
        z = NormalDist().inv_cdf(0.05)
        assert_close(res.value["value_at_risk"], NormalDist(mu, sigma).inv_cdf(0.05))
        assert_close(res.value["conditional_value_at_risk"], mu - sigma * NormalDist().pdf(z) / 0.05)
        assert_close(res.value["value_at_risk"], lib().value_at_risk(R, confidence=0.95, prepare_returns=False))
        assert_close(res.value["expected_shortfall"], lib().expected_shortfall(R, confidence=0.95))
    else:
        q = float(np.quantile(R.to_numpy(), 0.05))
        assert_close(res.value["value_at_risk"], q)
        assert_close(res.value["conditional_value_at_risk"], float(R[R <= q].mean()))
        assert_close(res.value["conditional_value_at_risk"], lib().conditional_value_at_risk(R, confidence=0.95, prepare_returns=False, method="historical"))
    assert res.value["expected_shortfall"] == res.value["conditional_value_at_risk"]
    assert res.value["conditional_value_at_risk"] <= res.value["value_at_risk"] < 0
    assert_bad_data_gives_none(lambda s: call("metrics.tail_view", returns=s, method=method))


def test_tail_view_bad_arguments_QS_023():
    with pytest.raises(BadInput):
        call("metrics.tail_view", returns=R, method="cornish")
    with pytest.raises(BadInput):
        call("metrics.tail_view", returns=R, level=1.2)
    with pytest.raises(BadInput):
        call("metrics.tail_view", returns="not a series")

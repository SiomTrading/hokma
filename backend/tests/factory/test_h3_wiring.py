"""The adapters of the studies call the real functions of H3 (SPEC-019) and agree with them to the digit (SPEC-020, rule: one calculation)."""
import math

import numpy as np
import pytest

from hokma.factory.studies import _h3
from hokma.quant.portfolio import ic
from hokma.quant.stats import cointegration, events, hurst, kalman, timeseries


def test_the_information_coefficient_is_the_rank_correlation_of_one_cross_section_by_hand():
    """Ranks equal the values; the rank gaps are -1, 1, -1, 1, 0: rho = 1 - 6 * 4 / (5 * 24) = 0.8."""
    assert _h3.information_coefficient([1, 2, 3, 4, 5], [2, 1, 4, 3, 5]) == pytest.approx(0.8)
    assert _h3.information_coefficient([1, 2, 3, 4, 5], [2, 1, 4, 3, 5]) == pytest.approx(ic.cs_spearman_correlation([1, 2, 3, 4, 5], [2, 1, 4, 3, 5]).value["mean"])
    assert _h3.information_coefficient([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)


def test_the_event_statistics_are_those_of_h3_and_the_t_statistic_is_by_hand():
    """Mean 0.025, deviations -0.015, 0.005, -0.005, 0.015: variance 5e-4 / 3, std 0.0129099, t = 0.025 / (0.0129099 / 2) = 3.87298."""
    out = _h3.event_stats([0.01, 0.03, 0.02, 0.04], seed=7)
    assert out.ok and out.value["mean"] == pytest.approx(0.025)
    assert out.value["t_stat"] == pytest.approx(0.025 / (math.sqrt(5e-4 / 3) / 2.0))
    assert out.value == events.event_stats([0.01, 0.03, 0.02, 0.04], seed=7).value


def test_the_half_life_is_the_exact_one_of_an_exact_autoregression():
    """x(t) = 10 + 0.8 (x(t-1) - 10) has phi = 0.8 exactly: the half life is ln 2 / -ln 0.8 = 3.1063 bars."""
    x = [20.0]
    for _ in range(40):
        x.append(10.0 + 0.8 * (x[-1] - 10.0))
    assert _h3.half_life(x) == pytest.approx(math.log(2.0) / -math.log(0.8))
    assert _h3.half_life(x) == pytest.approx(timeseries.half_life(x).value["half_life"])
    assert _h3.half_life([1.5 ** t for t in range(30)]) is None           # phi = 1.5 is outside (0, 1): it does not revert, no number is made up


def pair(n=300, seed=3):
    rng = np.random.default_rng(seed)
    b = np.cumsum(rng.normal(0.0, 0.01, n)) + 4.6
    own = np.zeros(n)
    for i in range(1, n):                                                 # a mean reverting gap between the two logs
        own[i] = 0.7 * own[i - 1] + rng.normal(0.0, 0.004)
    return 1.5 * b + own, b, np.cumsum(rng.normal(0.0, 0.01, n)) + 4.6


def test_the_cointegration_p_value_is_the_upper_bound_of_the_band_of_h3():
    a, b, other = pair()
    band = cointegration.engle_granger(a, b, ratio_basis="log").value["p_value_band"]
    assert band == "<0.01" and _h3.engle_granger_p(a, b) == 0.01
    unrelated = cointegration.engle_granger(a, other, ratio_basis="log").value["p_value_band"]
    assert _h3.engle_granger_p(a, other) == _h3.P_UPPER_BOUND[unrelated]
    assert _h3.P_UPPER_BOUND == {"<0.01": 0.01, "0.01-0.05": 0.05, "0.05-0.10": 0.10, ">0.10": 1.0}


def test_the_hurst_exponent_and_the_dynamic_hedge_ratio_are_those_of_h3():
    a, b, _ = pair()
    spread = a - 1.5 * b
    assert _h3.hurst_exponent(spread) == pytest.approx(hurst.hurst(spread, input_kind="levels").value["hurst"])
    assert _h3.hurst_exponent(spread) < 0.5                                # the gap reverts: below one half
    beta = _h3.dynamic_hedge_ratio(a, b)
    assert len(beta) == len(a) == len(kalman.kalman_filter(a, b, model="regression").value["beta"])
    assert beta[-1] == pytest.approx(1.5, abs=0.15)                        # the slope that built the pair


def test_a_function_that_cannot_compute_gives_none_or_the_stable_error():
    assert _h3.engle_granger_p(list(np.ones(40)), list(np.arange(40.0))) is None       # a constant series: H3 explains, no number is made up
    with pytest.raises(_h3.H3Unavailable, match="could not compute the hedge ratio"):
        _h3.dynamic_hedge_ratio(list(np.ones(40)), list(np.arange(40.0)))
    with pytest.raises(_h3.H3Unavailable, match="has no function"):
        _h3.resolve("hokma.quant.stats.hurst", "no_such_function")

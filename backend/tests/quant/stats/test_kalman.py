"""Kalman filter, calibration and smoother. Row ECT-035."""
import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.stats import kalman

from .conftest import call, piece_ids, rng


@pytest.fixture(scope="module")
def dynamic_pair():
    g = rng(81)
    n = 500
    beta = 1.0 + np.cumsum(g.normal(0, 0.01, n))
    x = 50 + np.cumsum(g.standard_normal(n))
    y = 3.0 + beta * x + g.normal(0, 0.5, n)
    return pd.Series(y), pd.Series(x), beta


def test_local_level_filter_ECT_035_hand_values():
    g = rng(82)
    y = 10.0 + g.standard_normal(60) * 2.0
    # with no state noise the level is the precision weighted mean of the first guess y0 (variance p0) and every observation
    p0, r = 1e4 * max(float(y[0]) ** 2, 1.0), 4.0
    run = kalman.kalman_run(y, np.ones((60, 1)), 0.0, r, p0_scale=1e4)
    for t in (0, 5, 59):
        expected = (y[0] / p0 + y[: t + 1].sum() / r) / (1 / p0 + (t + 1) / r)
        assert run["states"][t, 0] == pytest.approx(expected, abs=1e-9)
    # steady state of the local level filter: predicted variance (q + sqrt(q^2 + 4 q r)) / 2, innovation variance that plus r
    q, r = 0.5, 2.0
    steady = kalman.kalman_run(g.standard_normal(400), np.ones((400, 1)), q, r)
    p_pred = (q + math.sqrt(q * q + 4 * q * r)) / 2
    assert steady["innovation_variance"][-1] == pytest.approx(p_pred + r, abs=1e-9)
    res = call("stats.kalman_filter", y=pd.Series(y), model="local_level", q=0.1, r=4.0)
    assert res.value["latest_level"] == pytest.approx(kalman.kalman_run(y, np.ones((60, 1)), 0.1, 4.0)["states"][-1, 0], abs=1e-12)
    assert res.value["research_only"] is False and res.value["calibrated"] is False
    assert "ECT-035" in piece_ids("stats.kalman_filter")


def test_regression_filter_ECT_035_with_no_state_noise_equals_least_squares(dynamic_pair):
    y, x, _ = dynamic_pair
    design = np.column_stack([np.ones(len(y)), x.to_numpy()])
    run = kalman.kalman_run(y.to_numpy(), design, 0.0, 0.25, p0_scale=1e6)
    ols = np.linalg.lstsq(design, y.to_numpy(), rcond=None)[0]
    assert run["states"][-1] == pytest.approx(ols, abs=1e-4)            # recursive least squares with a very wide start is least squares (the remaining gap is the weight of the start)


def test_calibration_ECT_035_recovers_the_noise_variances_and_is_the_default(dynamic_pair):
    y, x, beta = dynamic_pair
    cal = call("stats.kalman_calibrate", y=y, x=x).value
    assert cal["r"] == pytest.approx(0.25, rel=0.4) and 1e-6 < cal["q"] < 1e-3
    assert cal["signal_to_noise"] == pytest.approx(cal["q"] / cal["r"], abs=1e-15)
    res = call("stats.kalman_filter", y=y, x=x)                         # q and r are not given: they are calibrated
    assert res.value["calibrated"] is True and res.value["q"] == pytest.approx(cal["q"], rel=1e-6)
    assert abs(res.value["latest_beta"] - beta[-1]) < 0.25
    assert any("calibrated by maximum likelihood" in w for w in res.warnings)
    diag = res.value["innovation_diagnostics"]
    assert abs(diag["mean"]) < 0.15 and diag["std"] == pytest.approx(1.0, abs=0.1) and diag["well_specified"] is True
    one_given = call("stats.kalman_filter", y=y, x=x, q=1e-4)
    assert one_given.value is None and any("both q and r" in w for w in one_given.warnings)


def test_filter_is_causal_and_the_smoother_is_research_only(dynamic_pair):
    y, x, _ = dynamic_pair
    base = call("stats.kalman_filter", y=y, x=x, q=1e-4, r=0.25).value
    changed = y.copy()
    changed.iloc[-50:] += 25.0                                          # the future changes
    after = call("stats.kalman_filter", y=changed, x=x, q=1e-4, r=0.25).value
    assert after["beta"][:-50] == base["beta"][:-50]                    # nothing before the change moves: the filter never looks ahead
    smooth_base = call("stats.kalman_smoother", y=y, x=x, q=1e-4, r=0.25)
    smooth_after = call("stats.kalman_smoother", y=changed, x=x, q=1e-4, r=0.25)
    assert smooth_after.value["beta"][:-50] != smooth_base.value["beta"][:-50]    # the smoother uses the future at every date
    assert smooth_base.value["research_only"] is True and smooth_base.params["research_only"] is True
    assert any("research_only" in w for w in smooth_base.warnings)
    assert registry.describe("stats.kalman_smoother").summary.startswith("RESEARCH ONLY")
    assert smooth_base.value["beta"][-1] == pytest.approx(base["beta"][-1], abs=1e-12)    # the last point has no future: smoother equals filter
    assert smooth_base.value["beta"][0] != base["beta"][0]
    level = call("stats.kalman_smoother", y=y, model="local_level", q=1e-3, r=0.5).value
    assert level["research_only"] is True and len(level["level"]) == len(y)


def test_kalman_ECT_035_empty_nan_and_out_of_domain(dynamic_pair):
    y, x, _ = dynamic_pair
    assert call("stats.kalman_filter", y=pd.Series([], dtype=float), model="local_level", q=0.1, r=1.0).value is None
    assert call("stats.kalman_filter", y=pd.Series(np.ones(100)), model="local_level", q=0.1, r=1.0).value is None
    assert call("stats.kalman_filter", y=y.iloc[:10], x=x.iloc[:10]).value is None
    assert call("stats.kalman_filter", y=y).value is None                                # the regression model needs x
    assert call("stats.kalman_filter", y=y, x=x, model="arima").value is None
    assert call("stats.kalman_filter", y=y, x=x, q=-1.0, r=1.0).value is None
    assert call("stats.kalman_filter", y=y, x=x, q=0.1, r=0.0).value is None
    assert call("stats.kalman_calibrate", y=y.iloc[:10], x=x.iloc[:10]).value is None
    assert call("stats.kalman_calibrate", y=pd.Series(rng(1).standard_normal(30000)), model="local_level").value is None
    nan = y.copy()
    nan.iloc[4] = np.nan
    assert any("dropped" in w for w in call("stats.kalman_filter", y=nan, x=x, q=1e-4, r=0.25).warnings)
    with pytest.raises(BadInput):
        call("stats.kalman_filter", y=y, x=x.iloc[:100], q=1e-4, r=0.25)
    with pytest.raises(BadInput):
        call("stats.kalman_filter", y=y, x=x, q="a", r=1.0)

"""P-02: robust volatility. Exponentially weighted (span 35), at least 10 returns, floored at the 5th percentile of the last 500 values."""
import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput

from .conftest import call, piece_ids

LAMBDA = 1.0 - 2.0 / (35 + 1.0)                      # span 35: the weight of an observation falls by 17/18 per bar


def ewm_std(x, span=35, min_obs=10):
    """Exponentially weighted standard deviation written out: weights lambda^age, the weighted variance divided by 1 - sum(w^2) / sum(w)^2."""
    lam = 1.0 - 2.0 / (span + 1.0)
    out = []
    for n in range(1, len(x) + 1):
        if n < min_obs:
            continue
        w = np.array([lam ** (n - 1 - i) for i in range(n)])
        mean = float(np.dot(w, x[:n]) / w.sum())
        variance = float(np.dot(w, (np.asarray(x[:n]) - mean) ** 2) / w.sum()) / (1.0 - float(np.dot(w, w)) / float(w.sum()) ** 2)
        out.append(math.sqrt(variance))
    return out


def series(values):
    return pd.Series(values, index=pd.bdate_range("2020-01-01", periods=len(values)))


def test_ten_returns_by_hand():
    r = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.0, 0.005, -0.015, 0.01]
    res = call("stats.robust_vol", returns=series(r))
    # one value only: the tenth return is the first with 10 observations. Weights (17/18)^age; sum of weights, weighted mean and weighted variance by hand
    w = [LAMBDA ** (9 - i) for i in range(10)]
    mean = sum(wi * x for wi, x in zip(w, r)) / sum(w)
    biased = sum(wi * (x - mean) ** 2 for wi, x in zip(w, r)) / sum(w)
    expected = math.sqrt(biased / (1.0 - sum(wi * wi for wi in w) / sum(w) ** 2))
    assert res.value["latest"] == pytest.approx(expected, abs=1e-12) and res.value["values"] == [pytest.approx(expected, abs=1e-12)]
    assert res.value["raw_latest"] == res.value["latest"] and res.value["floor"] is None and res.value["floor_binding"] is False
    assert res.value["n_returns"] == 10 and any("floor not applied" in w_ for w_ in res.warnings)
    assert piece_ids("stats.robust_vol") == ("P-02",)


def test_fewer_than_ten_returns_give_no_value_and_min_obs_moves_the_start():
    r = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.0, 0.005, -0.015]
    res = call("stats.robust_vol", returns=series(r))
    assert res.value is None and "at least 10" in " ".join(res.warnings)
    short = call("stats.robust_vol", returns=series(r), min_obs=3, last_n=100)
    assert short.value["values"] == pytest.approx(ewm_std(r, min_obs=3), abs=1e-12) and len(short.value["values"]) == 7
    first = ewm_std(r[:3], min_obs=3)[0]                          # three returns: weights lambda^2, lambda, 1
    assert first == pytest.approx(short.value["values"][0], abs=1e-12)


def test_after_30_calm_days_the_volatility_does_not_fall_below_the_floor():
    rng = np.random.default_rng(3)
    history = list(rng.choice([-0.01, 0.01], size=520))           # 520 days of one percent moves
    returns = series(history + [0.0] * 30)                         # then 30 days of no move at all
    res = call("stats.robust_vol", returns=returns, last_n=30)
    raw = ewm_std(list(returns))
    floor = float(np.quantile(raw[-500:], 0.05))                  # the 5th percentile of the last 500 estimates, the current one included
    assert res.value["raw_latest"] == pytest.approx(raw[-1], abs=1e-12)
    assert raw[-1] < floor                                        # 30 calm days leave the plain estimate under the floor
    assert res.value["floor"] == pytest.approx(floor, abs=1e-12) and res.value["floor_binding"] is True
    assert res.value["latest"] == pytest.approx(floor, abs=1e-12) and res.value["latest"] > res.value["raw_latest"]
    assert all(v >= r_ for v, r_ in zip(res.value["values"], res.value["raw_values"]))      # the floor only ever lifts the estimate
    assert len(res.value["values"]) == 30 and res.warnings == ()


def test_the_floor_leaves_a_busy_market_alone_and_needs_enough_history():
    rng = np.random.default_rng(4)
    r = list(rng.normal(0.0, 0.01, 300))
    busy = call("stats.robust_vol", returns=series(r), last_n=300).value
    raw = ewm_std(r)
    # with 300 returns there are 291 estimates: the floor applies from the 100th and equals the quantile of the estimates seen so far
    expected = [max(v, float(np.quantile(raw[max(0, k - 499):k + 1], 0.05))) if k + 1 >= 100 else v for k, v in enumerate(raw)]
    assert busy["values"] == pytest.approx(expected, abs=1e-12)
    no_floor = call("stats.robust_vol", returns=series(r), floor_min_periods=1000, floor_window=1000, last_n=300).value
    assert no_floor["values"] == pytest.approx(raw, abs=1e-12) and no_floor["floor"] is None


def test_other_spans_and_missing_values():
    r = list(np.random.default_rng(5).normal(0.0, 0.02, 80))
    assert call("stats.robust_vol", returns=series(r), span=10, last_n=80).value["raw_values"] == pytest.approx(ewm_std(r, span=10), abs=1e-12)
    with_nan = series(r)
    with_nan.iloc[5] = np.nan
    res = call("stats.robust_vol", returns=with_nan)
    assert res.value is not None and any("dropped 1" in w for w in res.warnings)


def test_bad_parameters_give_no_value_and_wrong_types_raise():
    r = series([0.01, -0.01] * 20)
    assert call("stats.robust_vol", returns=r, span=1).value is None
    assert call("stats.robust_vol", returns=r, floor_quantile=1.0).value is None
    assert call("stats.robust_vol", returns=r, floor_window=5, floor_min_periods=10).value is None
    with pytest.raises(BadInput):
        call("stats.robust_vol", returns=r, span=100_001)
    with pytest.raises(BadInput):
        call("stats.robust_vol", returns=r, span=2.5)
    with pytest.raises(BadInput):
        call("stats.robust_vol", returns=r, floor_quantile="5")
    with pytest.raises(BadInput):
        call("stats.robust_vol", returns=r, nonsense=1)


def test_the_function_runs_in_a_worker_process_and_comes_back_whole(monkeypatch):
    from hokma.quant import registry

    monkeypatch.setattr(registry, "IN_PROCESS", False)
    r = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.0, 0.005, -0.015, 0.01]
    res = registry.call("stats.robust_vol", returns=series(r))
    assert res.value["latest"] == pytest.approx(ewm_std(r)[0], abs=1e-12) and res.piece_ids == ("P-02",)
    carry = registry.call("stats.futures_carry", price=70.0, expiry="2026-11-20", next_price=69.4, next_expiry="2026-12-21")
    assert carry.value["carry_annualized"] == pytest.approx(0.6 * 365 / 31) and carry.piece_ids == ("P-05",)

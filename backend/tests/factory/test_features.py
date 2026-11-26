"""Features without leakage (SPEC-020: ECT-069): causal transformations by hand, LeakError, selection inside the training rows, Amihud in dollars."""
import numpy as np
import pytest

from hokma.factory import features as ft
from hokma.factory.studies import _h3, ic_decay
from hokma.quant.errors import BadInput


def ranks(values):
    order = np.argsort(values, kind="stable")
    out = np.empty(len(values))
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[order[j + 1]] == values[order[i]]:
            j += 1
        out[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return out


def spearman(x, y):
    return float(np.corrcoef(ranks(x), ranks(y))[0, 1])


def test_the_target_is_the_one_of_the_ic_study_from_the_open_of_t_plus_1():
    assert ft.target_next_open is ic_decay.forward_returns
    assert ft.target_next_open([100, 101, 102, 100], 1)[:2] == pytest.approx([102 / 101 - 1, 100 / 102 - 1])


def test_returns_by_hand():
    assert ft.returns([100, 110, 99])[1:] == pytest.approx([0.10, -0.10]) and np.isnan(ft.returns([100, 110])[0])
    assert ft.returns([100, 110], log=True)[1] == pytest.approx(np.log(1.1))
    assert np.isnan(ft.returns([0, 5])[1])


def test_forward_fill_never_interpolates():
    nan = float("nan")
    assert ft.forward_fill([1, nan, nan, 4]).tolist() == [1, 1, 1, 4]                 # interpolation would give 2 and 3: it reads the 4
    assert ft.forward_fill([nan, 2, nan]).tolist()[1:] == [2, 2] and np.isnan(ft.forward_fill([nan, 2, nan])[0])
    stale = ft.forward_fill([1, nan, nan, 4], max_gap=1)
    assert stale[:2].tolist() == [1, 1] and np.isnan(stale[2]) and stale[3] == 4


def test_rolling_zscore_by_hand_uses_the_window_and_the_current_row_only():
    z = ft.rolling_zscore([1, 2, 4, 7], 3)
    assert np.isnan(z[:2]).all()
    # window 1, 2, 4: mean 7/3, sample variance ((4/3)^2 + (1/3)^2 + (5/3)^2) / 2 = 7/3
    assert z[2] == pytest.approx((4 - 7 / 3) / np.sqrt(7 / 3))
    # window 2, 4, 7: mean 13/3, sample variance ((7/3)^2 + (1/3)^2 + (8/3)^2) / 2 = 19/3
    assert z[3] == pytest.approx((7 - 13 / 3) / np.sqrt(19 / 3))
    assert np.isnan(ft.rolling_zscore([5, 5, 5], 3)[2])                                 # no spread: no score


def test_expanding_zscore_by_hand():
    z = ft.expanding_zscore([1, 2, 3, 10], 3)
    assert np.isnan(z[:2]).all() and z[2] == pytest.approx(1.0)                         # past 1, 2, 3: mean 2, std 1
    assert z[3] == pytest.approx((10 - 4) / np.sqrt(50 / 3))                            # past 1, 2, 3, 10: mean 4, variance 50 / 3


def test_fractional_difference_by_hand():
    assert ft.fractional_weights(1.0, 1e-5).tolist() == [1.0, -1.0]                    # d = 1 is the plain difference
    w = ft.fractional_weights(0.5, 0.1)
    assert w.tolist() == pytest.approx([1.0, -0.5, -0.125])                              # next weight -0.0625 is below the threshold
    out = ft.fractional_diff([1, 2, 4, 8, 16], 0.5, 0.1)
    assert np.isnan(out[:2]).all()
    assert out[2:] == pytest.approx([4 - 1 - 0.125, 8 - 2 - 0.25, 16 - 4 - 0.5])
    assert ft.fractional_diff([1, 3, 6], 1.0)[1:].tolist() == [2.0, 3.0]
    for bad in (0, -1, 2.5, True):
        with pytest.raises(BadInput):
            ft.fractional_weights(bad, 0.1)


def test_cross_section_standardization_looks_across_assets_of_one_date_only():
    panel = [[1, 2, 3], [10, 10, 10], [5, np.nan, 7]]
    z = ft.cross_section_zscore(panel)
    assert z[0].tolist() == pytest.approx([-1, 0, 1]) and np.isnan(z[1]).all()
    assert z[2][[0, 2]] == pytest.approx([-np.sqrt(0.5), np.sqrt(0.5)]) and np.isnan(z[2][1])
    r = ft.cross_section_rank([[1, 2, 3], [1, 1, 3], [4, np.nan, 9]])
    assert r[0].tolist() == [0.0, 0.5, 1.0] and r[1].tolist() == [0.25, 0.25, 1.0]        # ties share the average rank 1.5
    assert r[2][0] == 0.0 and r[2][2] == 1.0 and np.isnan(r[2][1])
    with pytest.raises(BadInput):
        ft.cross_section_rank([1, 2, 3])


def test_amihud_uses_dollar_volume_by_hand():
    """close 100 and 50, volume 1000 and 4000 shares: dollars 100000 and 200000, so |r| / dollars * 1e6 is 0.1 and 0.1."""
    out = ft.amihud([0.01, -0.02], [100, 50], [1000, 4000])
    assert out.tolist() == pytest.approx([0.1, 0.1])
    per_unit = np.abs([0.01, -0.02]) / np.array([1000, 4000]) * 1e6
    assert per_unit.tolist() == pytest.approx([10.0, 5.0]) and per_unit.tolist() != out.tolist()   # volume in units would say otherwise
    future = ft.amihud([0.001], [25000], [10], multiplier=20)                              # 25000 * 10 * 20 = 5,000,000 dollars
    assert future[0] == pytest.approx(0.001 / 5_000_000 * 1e6)
    assert np.isnan(ft.amihud([0.01], [100], [0])[0])


def test_amihud_mean_is_the_value_of_the_registered_risk_function():
    from hokma.quant import registry
    registry.load_all()
    r = np.array([0.01, -0.02, 0.015, -0.005])
    close = np.array([100.0, 50.0, 80.0, 90.0])
    volume = np.array([1000.0, 4000.0, 2500.0, 3000.0])
    reference = registry.call("risk.amihud_illiquidity", returns=r, dollar_volume=close * volume, scale=1_000_000.0)
    assert reference.value is not None
    assert float(ft.amihud(r, close, volume).mean()) == pytest.approx(reference.value, rel=1e-9)


def close_rows():
    return {"close": np.array([100.0, 101.0, 99.0, 103.0, 104.0, 102.0, 107.0, 105.0, 108.0, 110.0])}


def test_a_causal_feature_passes_and_the_feature_is_built():
    feature = ft.Feature("z3", "price", lambda d: ft.rolling_zscore(d["close"], 3))
    values = ft.build([feature, ft.Feature("ret", "price", lambda d: ft.returns(d["close"]))], close_rows())
    assert set(values) == {"z3", "ret"} and values["z3"].shape == (10,)


def test_a_feature_that_reads_a_future_row_fails_with_leak_error():
    next_close = ft.Feature("next_close", "price", lambda d: np.r_[d["close"][1:], np.nan])
    with pytest.raises(ft.LeakError, match="next_close") as caught:
        ft.check_causal(next_close, close_rows())
    assert caught.value.code == "leak" and "future row" in str(caught.value)
    with pytest.raises(ft.LeakError):
        ft.build([next_close], close_rows())
    assert ft.build([next_close], close_rows(), check=False)["next_close"][0] == 101.0       # the check is the only guard: switching it off is explicit


def test_full_sample_statistics_leak():
    full_sample = ft.Feature("zfull", "price", lambda d: (d["close"] - d["close"].mean()) / d["close"].std(ddof=1))
    with pytest.raises(ft.LeakError, match="zfull"):
        ft.check_causal(full_sample, close_rows())
    centered = ft.Feature("centered", "price", lambda d: np.convolve(d["close"], np.ones(3) / 3, mode="same"))
    with pytest.raises(ft.LeakError):
        ft.check_causal(centered, close_rows())


def test_bad_feature_declarations_raise_bad_input():
    with pytest.raises(BadInput):
        ft.Feature("x", "weather", lambda d: d["close"])
    with pytest.raises(BadInput):
        ft.build([ft.Feature("a", "price", lambda d: d["close"]), ft.Feature("a", "price", lambda d: d["close"])], close_rows())
    with pytest.raises(BadInput):
        ft.build([ft.Feature("a", "price", lambda d: d["close"][:3])], close_rows())


RULE = ft.SelectionRule(min_abs_ic=0.3, min_icir=0.5, max_abs_corr=0.9, vif_limit=10.0, min_periods=3)


def series(seed, n=80):
    return np.random.default_rng(seed).normal(size=n)


def test_ic_and_t_statistic_by_hand():
    assert ft.ic_t_stat(0.5, 100) == pytest.approx(5.0)               # the t of the mean IC is ICIR times the square root of the periods
    x = np.arange(12.0)
    y = np.r_[np.arange(4.0), -np.arange(4.0), np.arange(4.0)]
    out = ft.ic_series(x, y, range(12), spearman, block=4)
    assert out.tolist() == pytest.approx([1.0, -1.0, 1.0])
    panel_x = [[1.0, 2.0, 3.0], [3.0, 2.0, 1.0]]
    assert ft.ic_series(panel_x, [[1.0, 2.0, 3.0], [1.0, 2.0, 3.0]], [0, 1], spearman).tolist() == pytest.approx([1.0, -1.0])
    with pytest.raises(BadInput):
        ft.ic_series(x, y, range(12), spearman)                            # a single series needs a block size


def test_selection_uses_the_training_rows_only():
    y = series(1)
    good = y + 0.5 * series(2)
    train = np.arange(0, 48)
    only_in_test = series(3)
    only_in_test[48:] = y[48:]                                             # predictive after row 47 only
    features = {"good": good.copy(), "late": only_in_test.copy(), "noise": series(4)}
    chosen = ft.select_in_train(features, y, train, RULE, spearman, block=8)
    assert chosen.kept == ("good",) and chosen.dropped["late"] in ("weak_ic", "weak_icir") and chosen.train_rows == 48
    poisoned = {k: v.copy() for k, v in features.items()}
    for v in poisoned.values():
        v[48:] = series(9)[:32] * 100
    y2 = y.copy()
    y2[48:] = series(10)[:32]
    again = ft.select_in_train(poisoned, y2, train, RULE, spearman, block=8)
    assert again == chosen                                                  # the test rows cannot change the answer


def test_a_correlated_feature_is_dropped_in_favour_of_the_stronger_one():
    y = series(5)
    a = y + 0.4 * series(6)
    chosen = ft.select_in_train({"a": a, "c": 2.0 * a}, y, np.arange(80), RULE, spearman, block=8)
    assert chosen.kept == ("a",) and chosen.dropped == {"c": "correlated_with:a"}


def test_a_collinear_set_is_cut_by_vif():
    y = series(7)
    x1, x2 = y + series(8), y + series(11)
    x3 = x1 + x2 + 0.01 * series(12)
    rule = ft.SelectionRule(min_abs_ic=0.1, min_icir=0.1, max_abs_corr=0.99, vif_limit=10.0, min_periods=3)
    chosen = ft.select_in_train({"x1": x1, "x2": x2, "x3": x3}, y, np.arange(80), rule, spearman, block=8)
    assert len(chosen.kept) == 2 and list(chosen.dropped.values()) == ["vif"]


def test_selection_without_h3_and_without_a_function_fails_with_a_stable_error(monkeypatch):
    monkeypatch.setattr(_h3, "ALLOWED_PREFIX", "nowhere.")
    with pytest.raises(_h3.H3Unavailable):
        ft.select_in_train({"a": series(1)}, series(2), range(40), RULE, block=8)


def test_bad_selection_inputs_raise_bad_input():
    with pytest.raises(BadInput):
        ft.select_in_train({}, series(1), [0, 1], RULE, spearman, block=8)
    with pytest.raises(BadInput):
        ft.select_in_train({"a": series(1)}, series(2), [], RULE, spearman, block=8)
    with pytest.raises(BadInput):
        ft.select_in_train({"a": series(1)}, series(2), [0, 500], RULE, spearman, block=8)
    with pytest.raises(BadInput):
        ft.select_in_train({"a": series(1)}, series(2), range(10), ft.SelectionRule(0.1, 0.1, 1.5, 10.0, 3), spearman, block=8)

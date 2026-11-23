"""ECT-016: causal filters with a measured lag, research only two sided filters, dominant cycle and the cycle check."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators import _lib, signal_processing as sp

from .helpers import column, latest

N = 100_000


def _walk(seed: int, n: int = 300) -> pd.Series:
    return pd.Series(np.cumsum(np.random.default_rng(seed).normal(size=n)))


def test_lag_of_the_averages_matches_theory():
    assert sp.measure_lag("sma", window=5).value["lag_bars"] == pytest.approx(2.0)                # (N - 1) / 2
    assert sp.measure_lag("ema", window=5).value["lag_bars"] == pytest.approx(2.0, abs=1e-6)      # (N - 1) / 2
    assert sp.measure_lag("wma", window=5).value["lag_bars"] == pytest.approx(4 / 3, abs=1e-6)    # (N - 1) / 3
    assert sp.measure_lag("sma", window=21).value["lag_bars"] == pytest.approx(10.0)
    assert sp.measure_lag("hull", window=20).value["lag_bars"] == pytest.approx(2 / 3, abs=1e-6)


def test_lag_of_butterworth_matches_the_group_delay():
    lag = sp.measure_lag("butterworth", cutoff_period=10.0, order=2).value["lag_bars"]
    assert lag == pytest.approx(math.sqrt(2) / (2 * math.pi / 10.0), rel=0.05)      # analog group delay at DC
    slower = sp.measure_lag("butterworth", cutoff_period=20.0, order=2).value["lag_bars"]
    assert slower == pytest.approx(2 * lag, rel=0.05)


def test_lag_of_savgol_wavelet_and_kama_is_measured_and_reported():
    assert sp.measure_lag("savgol", window=11, polyorder=2).value["lag_bars"] == pytest.approx(0.0, abs=1e-8)
    wave = sp.measure_lag("wavelet", window=64, level=3).value["lag_bars"]
    assert wave == pytest.approx(sp.ramp_lag("wavelet", window=64, level=3)) and 1.0 < wave < 4.0
    kama = sp.measure_lag("kama", window=10).value["lag_bars"]
    assert 0.0 <= kama < 5.0


def test_measure_lag_rejects_what_it_cannot_measure():
    for kwargs in ({"method": "fourier"}, {"method": "butterworth", "cutoff_period": 1.0}, {"method": "wavelet", "window": 10, "level": 3},
                   {"method": "savgol", "window": 3, "polyorder": 3}, {"method": "kama", "fast_window": 30, "slow_window": 2}):
        assert sp.measure_lag(**kwargs).value is None
    with pytest.raises(BadInput):
        sp.measure_lag("ema", window=2.5)


def test_butterworth_keeps_a_constant_and_matches_the_library():
    assert np.allclose(column(sp.butterworth([5.0] * 30, cutoff_period=8.0, last_n=N), "filtered"), 5.0)
    x = _walk(1)
    b, a = _lib.sp_signal.butter(2, 2.0 / 12.0, btype="low")
    want, _ = _lib.sp_signal.lfilter(b, a, x.to_numpy(), zi=_lib.sp_signal.lfilter_zi(b, a) * x.iloc[0])
    got = column(sp.butterworth(x, cutoff_period=12.0, last_n=N), "filtered")
    np.testing.assert_allclose(got.to_numpy(), want, atol=1e-8)


def test_causal_filters_never_look_ahead():
    x = _walk(2, 200)
    for fn, kw in ((sp.butterworth, {"cutoff_period": 10.0}), (sp.savgol_causal, {"window": 11, "polyorder": 2}),
                   (sp.wavelet_denoise, {"window": 32, "level": 3})):
        full = column(fn(x, last_n=N, **kw), "filtered")
        part = column(fn(x.iloc[:150], last_n=N, **kw), "filtered")
        np.testing.assert_allclose(full.iloc[:len(part)].to_numpy(), part.to_numpy(), atol=1e-10, equal_nan=True)


def test_savgol_causal_by_hand_and_against_a_polynomial_fit():
    quad = [float(t * t) for t in range(30)]
    got = column(sp.savgol_causal(quad, window=7, polyorder=2, last_n=N), "filtered").dropna()
    np.testing.assert_allclose(got.to_numpy(), quad[6:], atol=1e-8)           # a quadratic is reproduced exactly
    x = _walk(3, 60)
    res = sp.savgol_causal(x, window=9, polyorder=3)
    tail = x.iloc[-9:].to_numpy()
    fit = np.polyval(np.polyfit(np.arange(9), tail, 3), 8)
    assert latest(res, "filtered") == pytest.approx(fit, abs=1e-8)


def test_wavelet_denoise_by_hand():
    assert latest(sp.wavelet_denoise([4.0, 4.0, 4.0, 6.0], window=4, level=1), "filtered") == pytest.approx(5.0)
    constant = column(sp.wavelet_denoise([3.0] * 40, window=16, level=2, last_n=N), "filtered").dropna()
    assert np.allclose(constant, 3.0)


def test_wavelet_denoise_reduces_noise_on_a_noisy_level():
    rng = np.random.default_rng(5)
    x = pd.Series(10.0 + rng.normal(0, 1.0, 400))
    out = column(sp.wavelet_denoise(x, window=64, level=3, last_n=N), "filtered").dropna()
    assert out.std() < 0.7 * x.iloc[63:].std()


def test_two_sided_filters_are_research_only_and_blocked_live():
    assert sp.RESEARCH_ONLY == {"indicators.hodrick_prescott", "indicators.savgol_centered"}
    for name in sp.RESEARCH_ONLY:
        assert registry.describe(name).summary.startswith("research_only")
    x = _walk(4, 80)
    for fn in (sp.hodrick_prescott, sp.savgol_centered):
        blocked = fn(x)
        assert blocked.value is None and blocked.warnings[0].startswith("research_only") and blocked.params["research_only"] is True
        assert fn(x, mode="live").value is None
        assert fn(x, mode="research").ok


def test_hodrick_prescott_by_hand_and_against_a_dense_solve():
    res = sp.hodrick_prescott([1.0, 0.0, 1.0], smoothing=1.0, mode="research", last_n=10)
    np.testing.assert_allclose(column(res, "filtered").to_numpy(), [5 / 7, 4 / 7, 5 / 7], atol=1e-12)
    line = [float(t) for t in range(12)]
    np.testing.assert_allclose(column(sp.hodrick_prescott(line, smoothing=100.0, mode="research", last_n=N), "filtered").to_numpy(), line, atol=1e-8)
    y = _walk(6, 40).to_numpy()
    d = np.diff(np.eye(40), 2, axis=0)
    dense = np.linalg.solve(np.eye(40) + 1600.0 * d.T @ d, y)
    np.testing.assert_allclose(column(sp.hodrick_prescott(y, mode="research", last_n=N), "filtered").to_numpy(), dense, atol=1e-8)


def test_two_sided_filters_repaint_when_a_bar_arrives():
    y = _walk(7, 60)
    before = column(sp.hodrick_prescott(y.iloc[:50], mode="research", last_n=N), "filtered")
    after = column(sp.hodrick_prescott(y, mode="research", last_n=N), "filtered")
    assert abs(after.iloc[49] - before.iloc[49]) > 1e-3                 # the value of bar 49 changed: it repaints


def test_savgol_centered_by_hand_and_against_the_library():
    quad = [float(t * t) for t in range(15)]
    np.testing.assert_allclose(column(sp.savgol_centered(quad, window=5, polyorder=2, mode="research", last_n=N), "filtered").to_numpy(), quad, atol=1e-8)
    x = _walk(8, 50)
    want = _lib.sp_signal.savgol_filter(x.to_numpy(), 11, 2, mode="interp")
    np.testing.assert_allclose(column(sp.savgol_centered(x, mode="research", last_n=N), "filtered").to_numpy(), want, atol=1e-8)
    assert sp.savgol_centered(x, window=10, mode="research").value is None


def test_dominant_cycle_by_hand():
    sine = np.sin(2 * np.pi * np.arange(128) / 16)
    res = sp.dominant_cycle(sine, window=128)
    assert res.value["period"] == pytest.approx(16.0) and res.value["power_share"] > 0.5
    trend = 0.05 * np.arange(200) + 2 * np.sin(2 * np.pi * np.arange(200) / 20)
    assert sp.dominant_cycle(trend, window=100).value["period"] == pytest.approx(20.0, rel=0.05)
    assert sp.dominant_cycle([1.0] * 128, window=128).value is None


def test_cycle_check_finds_an_injected_cycle_and_is_deterministic():
    t = np.arange(300)
    noisy = _walk(9) + 6 * np.sin(2 * np.pi * t / 20)
    hit = sp.cycle_check(noisy, method="ema", window=5, n_paths=200, seed=1)
    assert hit.value["cycle_detected"] and hit.value["p_value"] < 0.05
    again = sp.cycle_check(noisy, method="ema", window=5, n_paths=200, seed=1)
    assert hit.value == again.value


def test_filters_do_not_invent_cycles_on_simulated_random_walks():
    flagged = 0
    for seed in range(100, 120):
        res = sp.cycle_check(_walk(seed), method="ema", window=10, n_paths=100, seed=seed)
        flagged += bool(res.value["cycle_detected"])
    assert flagged <= 3                                # about alpha (5 percent) of 20 walks, never a systematic cycle


def test_cycle_check_rejects_bad_input():
    assert sp.cycle_check(_walk(1), method="wavelet").value is None
    assert sp.cycle_check(_walk(1), alpha=1.5).value is None
    assert sp.cycle_check([1.0] * 100).value is None
    assert sp.cycle_check([1.0, 2.0]).value is None
    with pytest.raises(BadInput):
        registry.call("indicators.cycle_check", values=_walk(1), n_paths=3000)


@pytest.mark.parametrize("fn", [sp.butterworth, sp.savgol_causal, sp.wavelet_denoise, sp.dominant_cycle])
def test_empty_nan_and_out_of_domain_give_no_value(fn):
    assert fn([]).value is None
    assert fn([1.0, float("nan")] * 80).value is None
    assert fn([1.0, float("inf")] * 80).value is None
    assert fn([1.0, 2.0, 3.0]).value is None            # shorter than any default window


def test_filter_parameters_out_of_domain():
    x = _walk(1, 100)
    assert sp.butterworth(x, cutoff_period=2.0).value is None
    assert sp.butterworth(x, cutoff_period=float("nan")).value is None
    assert sp.savgol_causal(x, window=5, polyorder=5).value is None
    assert sp.wavelet_denoise(x, window=30, level=3).value is None
    with pytest.raises(BadInput):
        sp.butterworth(x, order=0)
    with pytest.raises(BadInput):
        sp.butterworth(x, order=2.5)
    with pytest.raises(BadInput):
        sp.butterworth("abc")


def test_registered_with_the_piece_id_and_called_through_the_registry():
    names = {s.name for s in registry.list("indicators") if "ECT-016" in s.piece_ids}
    assert {"indicators.butterworth", "indicators.savgol_causal", "indicators.wavelet_denoise", "indicators.dominant_cycle",
            "indicators.measure_lag", "indicators.cycle_check", "indicators.hodrick_prescott", "indicators.savgol_centered"} <= names
    res = registry.call("indicators.measure_lag", method="sma", window=5)
    assert res.value["lag_bars"] == pytest.approx(2.0)


def test_causal_filters_on_the_fixed_daily_bars(spy):
    closes = spy["close"]
    res = sp.butterworth(closes, cutoff_period=10.0, last_n=N)
    out = column(res, "filtered")
    assert len(out) == len(closes) and out.notna().all()
    assert abs(out.iloc[-1] - closes.iloc[-1]) < 0.05 * closes.iloc[-1]


def test_filters_on_the_fixed_daily_returns():
    returns = pd.read_csv(Path(__file__).resolve().parents[1] / "fixtures" / "returns_daily.csv", index_col=0, parse_dates=True)["return"]
    level = (1.0 + returns).cumprod()
    for fn, kw in ((sp.savgol_causal, {"window": 11, "polyorder": 2}), (sp.wavelet_denoise, {"window": 64, "level": 3}), (sp.butterworth, {"cutoff_period": 10.0})):
        out = column(fn(level, last_n=N, **kw), "filtered").dropna()
        assert len(out) >= len(level) - 64 and np.isfinite(out).all()
    check = sp.cycle_check(level.reset_index(drop=True), method="ema", window=10, n_paths=100, seed=3)
    assert check.ok and 0.0 < check.value["p_value"] <= 1.0
    assert sp.measure_lag("ema", window=10).value["lag_bars"] == pytest.approx(4.5, abs=1e-6)

"""Pairs pipeline and spread alpha (SPEC-020: ECT-055): selection with the correction for the pairs tested, hedge ratio, exits, effective N."""
import math
from datetime import date, datetime, timezone

import numpy as np
import pytest

from hokma.desks.core.signal import AlphaModel, SignalContext
from hokma.factory import features as ft
from hokma.factory.alphas import pairs_pipeline as pp
from hokma.factory.studies import _h3, ic_decay
from hokma.quant.errors import BadInput

AS_OF = datetime(2025, 3, 3, 21, 0, tzinfo=timezone.utc)
RULES = pp.PairRules(min_correlation=0.7, alpha=0.05, min_half_life_days=5.0, max_half_life_days=60.0, max_hurst=0.5, top_n=20, method="bh",
                     require_economic=True)


def ctx(**facts) -> SignalContext:
    return SignalContext("alpha.test", 7, "SWC", "AAA", AS_OF, "days", "h" * 8, facts={k: np.asarray(v, dtype=float) for k, v in facts.items()})


def random_pairs(n=4850, seed=20251122):
    """n pairs of unrelated names: under the null the p-value of the cointegration test is uniform. Everything else passes its filter."""
    rng = np.random.default_rng(seed)
    return [pp.PairCandidate(f"a{i}", f"b{i}", 0.8, float(p), 20.0, 0.4, True) for i, p in enumerate(rng.uniform(size=n))]


def test_4850_random_pairs_leave_almost_none_after_the_correction_ECT_055():
    result = pp.select_pairs(random_pairs(), RULES)
    assert result.n_tested == 4850 and 200 <= result.n_pass_raw <= 290        # 5 percent of 4,850 is 242.5 pass by luck
    assert len(result.selected) <= 1                                           # the correction leaves none (or almost none)
    assert pp.select_pairs(random_pairs(), RULES).selected == result.selected   # deterministic


def test_real_cointegrated_pairs_survive_among_4850_nulls_with_the_adjusted_p_by_hand():
    nulls = random_pairs()
    real = [pp.PairCandidate("kx", "ky", 0.95, 1e-6, 20.0, 0.3, True), pp.PairCandidate("lx", "ly", 0.9, 2e-6, 10.0, 0.3, True),
            pp.PairCandidate("mx", "my", 0.9, 3e-6, 30.0, 0.3, True)]
    result = pp.select_pairs(nulls + real, RULES)
    assert result.n_tested == 4853
    # Benjamini-Hochberg: p(i) * 4853 / i is 1e-6 * 4853 = 2e-6 * 4853 / 2 = 3e-6 * 4853 / 3 = 0.004853 for the three smallest.
    assert [(s.a, s.adjusted_p) for s in result.selected] == [("lx", pytest.approx(0.004853)), ("kx", pytest.approx(0.004853)), ("mx", pytest.approx(0.004853))]
    assert [s.half_life_days for s in result.selected] == [10.0, 20.0, 30.0]    # equal adjusted p: the faster reversion ranks first


def test_each_filter_names_its_reason():
    c = [pp.PairCandidate("a", "b", 0.5, 1e-9, 20.0, 0.3, True),        # correlation below the prefilter
         pp.PairCandidate("c", "d", 0.9, None, 20.0, 0.3, True),        # the test was not run
         pp.PairCandidate("e", "f", 0.9, 1e-9, 70.0, 0.3, True),        # too slow
         pp.PairCandidate("g", "h", 0.9, 1e-9, 3.0, 0.3, True),         # too fast
         pp.PairCandidate("i", "j", 0.9, 1e-9, 20.0, 0.55, True),       # does not revert
         pp.PairCandidate("k", "l", 0.9, 1e-9, 20.0, 0.3, None),        # no economic reason documented
         pp.PairCandidate("m", "n", 0.9, 0.4, 20.0, 0.3, True),         # not cointegrated
         pp.PairCandidate("o", "p", 0.9, 1e-9, 20.0, 0.3, True),
         pp.PairCandidate("q", "r", 0.9, 1e-9, 25.0, 0.3, True)]
    result = pp.select_pairs(c, pp.PairRules(0.7, 0.05, 5.0, 60.0, 0.5, 1, "bh", True))
    assert result.rejected == {("a", "b"): "correlation", ("c", "d"): "no_cointegration_test", ("e", "f"): "half_life", ("g", "h"): "half_life",
                               ("i", "j"): "hurst", ("k", "l"): "economic_reason", ("m", "n"): "cointegration", ("q", "r"): "rank"}
    assert [(s.a, s.b) for s in result.selected] == [("o", "p")] and result.n_tested == 9
    loose = pp.select_pairs(c, pp.PairRules(0.7, 0.05, 5.0, 60.0, 0.5, 5, "bh", False))
    assert ("k", "l") in [(s.a, s.b) for s in loose.selected]


def test_the_correction_method_is_a_rule_not_a_constant():
    c = [pp.PairCandidate(f"x{i}", f"y{i}", 0.9, p, 20.0, 0.3, True) for i, p in enumerate([0.01, 0.02, 0.03, 0.9])]
    # m = 4. Bonferroni: 0.04, 0.08, 0.12 -> one survives. Holm: 0.04, 0.06, 0.06 -> one. BH: 0.04, 0.04, 0.04 -> three.
    counts = {m: len(pp.select_pairs(c, pp.PairRules(0.7, 0.05, 5.0, 60.0, 0.5, 10, m, True)).selected) for m in ("bonferroni", "holm", "bh")}
    assert counts == {"bonferroni": 1, "holm": 1, "bh": 3}


def test_bad_rules_and_candidates_raise_bad_input():
    with pytest.raises(BadInput):
        pp.select_pairs([], RULES)
    with pytest.raises(BadInput):
        pp.select_pairs(random_pairs(3), pp.PairRules(0.7, 1.5, 5.0, 60.0, 0.5, 5, "bh", True))
    with pytest.raises(BadInput):
        pp.select_pairs(random_pairs(3), pp.PairRules(0.7, 0.05, 60.0, 5.0, 0.5, 5, "bh", True))
    with pytest.raises(BadInput):
        pp.select_pairs(random_pairs(3), pp.PairRules(0.7, 0.05, 5.0, 60.0, 0.5, 5, "sidak", True))


def test_the_monthly_retest_by_hand():
    assert pp.retest_due(date(2025, 1, 2), date(2025, 2, 1), 30) is True and pp.retest_due(date(2025, 1, 2), date(2025, 1, 31), 30) is False
    with pytest.raises(BadInput):
        pp.retest_due(date(2025, 1, 2), date(2025, 2, 1), 0)


def test_the_sharpe_improvement_uses_the_root_of_the_effective_n_by_hand():
    assert pp.effective_breadth is ic_decay.effective_breadth
    assert pp.diversified_sharpe(1.0, 7, 0.0) == pytest.approx(math.sqrt(7))              # 7 independent pairs: 2.6 times
    assert pp.diversified_sharpe(0.5, 20, 0.1) == pytest.approx(0.5 * math.sqrt(20 / 2.9))   # N effective 6.897: 1.313, not 0.5 * sqrt(20 / 6.9)
    assert pp.diversified_sharpe(0.5, 20, 0.1) > 0.5 and pp.diversified_sharpe(0.5, 20, 0.1) != pytest.approx(0.5 * math.sqrt(20 / (20 / 2.9)))


def test_a_hedge_ratio_is_not_equal_dollars():
    assert pp.pair_legs(100_000, 0.9) == (100_000.0, 90_000.0)                             # 100 thousand of A against 90 thousand of B
    assert pp.pair_legs(100_000, 0.9, "dollar") == (100_000.0, 100_000.0)
    for bad in ((0, 0.9), (100, 0), (100, 0.9, "mixed")):
        with pytest.raises(BadInput):
            pp.pair_legs(*bad)


def test_least_squares_hedge_ratio_by_hand_and_kalman_by_injection(monkeypatch):
    lb = np.log(np.array([10.0, 11.0, 12.5, 11.0, 13.0, 14.0]))
    assert pp.ols_hedge_ratio(2.0 * lb + 1.0, lb) == pytest.approx(2.0)                  # exact line: slope 2
    assert pp.hedge_ratio(2.0 * lb + 1.0, lb, "ols") == pytest.approx(2.0)
    betas = pp.hedge_ratio(2.0 * lb, lb, "kalman", kalman_fn=lambda a, b: np.linspace(1.9, 2.0, 6))
    assert betas.tolist() == pytest.approx(np.linspace(1.9, 2.0, 6).tolist())
    with pytest.raises(BadInput):
        pp.hedge_ratio(lb, lb, "kalman", kalman_fn=lambda a, b: [1.0])
    with pytest.raises(BadInput):
        pp.hedge_ratio(lb, lb, "tls")
    with pytest.raises(BadInput):
        pp.ols_hedge_ratio(lb, np.ones(6))
    monkeypatch.setattr(_h3, "ALLOWED_PREFIX", "nowhere.")
    with pytest.raises(_h3.H3Unavailable, match="hokma.quant.stats.kalman is not a quant module"):
        pp.hedge_ratio(lb, lb, "kalman")


def test_the_candidate_table_is_built_with_injected_h3_statistics():
    base = np.cumsum(np.r_[0.0, [0.01, -0.02, 0.015, 0.005, -0.01, 0.02, -0.005, 0.01, 0.0, -0.01]])
    logs = {"a": base, "b": 2.0 * base + 1.0, "c": -base}
    table = pp.build_candidates(logs, economic=lambda x, y: (x, y) == ("a", "b"), bar_days=1.0, half_life_fn=lambda s: 12.0,
                                eg_fn=lambda x, y: 0.01, hurst_fn=lambda s: 0.3)
    assert [(c.a, c.b) for c in table] == [("a", "b"), ("a", "c"), ("b", "c")]
    assert table[0].correlation == pytest.approx(1.0) and table[1].correlation == pytest.approx(-1.0)
    assert (table[0].eg_p, table[0].half_life_days, table[0].hurst, table[0].economic) == (0.01, 12.0, 0.3, True) and table[1].economic is False
    weekly = pp.build_candidates({"a": base, "b": 2.0 * base + 1.0}, bar_days=5.0, half_life_fn=lambda s: 3.0, eg_fn=lambda x, y: 0.5, hurst_fn=lambda s: 0.6)
    assert weekly[0].half_life_days == 15.0 and weekly[0].economic is None
    with pytest.raises(BadInput):
        pp.build_candidates({"a": base, "b": base[:5]}, half_life_fn=lambda s: 1.0, eg_fn=lambda x, y: 0.1, hurst_fn=lambda s: 0.3)


# ---- the spread alpha ----------------------------------------------------------------------------------------------------------

N = 40


def legs(jump):
    """B drifts up; A is 1.2 times B plus a small alternating spread; the last spread is moved by `jump`."""
    lb = np.linspace(4.60, 4.70, N) + 0.002 * np.sin(np.arange(N))
    spread = 0.001 * np.where(np.arange(N) % 2 == 0, 1.0, -1.0)
    spread[-1] += jump
    return np.exp(1.2 * lb + spread), np.exp(lb), lb, spread


def test_a_rich_spread_is_a_short_view_on_the_first_name():
    a, b, lb, spread = legs(0.004)
    alpha = pp.PairsSpread(window=30, history=40)
    assert isinstance(alpha, AlphaModel) and alpha.lookback == 40 and alpha.FACTS == ("close", "pair_close", "half_life")
    z = ft.rolling_zscore(np.log(a) - pp.ols_hedge_ratio(np.log(a)[-30:], np.log(b)[-30:]) * np.log(b), 30)
    assert 2.0 < z[-1] < 4.0
    signal = alpha.signal(ctx(close=a, pair_close=b, half_life=[10.0]))
    assert signal.value == -1.0 and signal.criteria[0].name == "spread_z" and signal.criteria[0].value == pytest.approx(z[-1])
    cheap = legs(-0.004)
    assert alpha.signal(ctx(close=cheap[0], pair_close=cheap[1], half_life=[10.0])).value == 1.0
    quiet = legs(0.0)
    assert alpha.signal(ctx(close=quiet[0], pair_close=quiet[1], half_life=[10.0])).value == 0.0


def test_a_spread_past_the_extreme_level_is_left_alone():
    a, b, *_ = legs(0.05)
    assert pp.PairsSpread(window=30, history=40).signal(ctx(close=a, pair_close=b, half_life=[10.0])).value == 0.0


def test_the_relationship_breaking_turns_the_view_flat():
    a, b, *_ = legs(0.004)
    retest = pp.PairsSpread(window=30, history=40, use_retest=True)
    assert retest.FACTS[-1] == "retest_p"
    holds = retest.signal(ctx(close=a, pair_close=b, half_life=[10.0], retest_p=[0.01]))
    assert holds.value == -1.0 and holds.criteria[-1].passed is True
    broken = retest.signal(ctx(close=a, pair_close=b, half_life=[10.0], retest_p=[0.2]))
    assert broken.value == 0.0 and "re-test" in broken.reason and broken.criteria[-1].passed is False
    events = pp.PairsSpread(window=30, history=40, use_events=True)
    assert events.signal(ctx(close=a, pair_close=b, half_life=[10.0], event=[0] * 39 + [0])).value == -1.0
    hit = events.signal(ctx(close=a, pair_close=b, half_life=[10.0], event=[0] * 39 + [1]))
    assert hit.value == 0.0 and "corporate event" in hit.reason
    assert retest.signal(ctx(close=a, pair_close=b, half_life=[10.0])).abstained


def test_the_time_exit_is_a_count_of_half_lives():
    """A spread rich since bar 36 of 40 is held for 3 half-lives of 1 bar (3 bars) and is flat on the fourth."""
    lb = np.linspace(4.60, 4.70, N)
    spread = 0.001 * np.where(np.arange(N) % 2 == 0, 1.0, -1.0)
    spread[-4:] = 0.006
    a, b = np.exp(1.2 * lb + spread), np.exp(lb)
    alpha = pp.PairsSpread(window=30, history=40, stop_z=20.0, exit_z=0.0, time_stop_half_lives=3.0)
    early = alpha.signal(ctx(close=a[:-1], pair_close=b[:-1], half_life=[1.0]))
    assert early.value == -1.0                                              # 3 bars of the rich spread: still held
    assert alpha.signal(ctx(close=a, pair_close=b, half_life=[1.0])).value == 0.0   # the fourth bar: 3 half-lives are over


def test_kalman_hedge_reads_the_causal_filter_fact():
    a, b, lb, spread = legs(0.004)
    alpha = pp.PairsSpread(window=30, history=40, hedge="kalman")
    assert alpha.FACTS[3] == "kalman_beta"
    value = alpha.signal(ctx(close=a, pair_close=b, half_life=[10.0], kalman_beta=np.full(N, 1.2)))
    assert value.value == -1.0
    assert alpha.signal(ctx(close=a, pair_close=b, half_life=[10.0])).abstained


def test_abstentions_and_bad_parameters():
    a, b, *_ = legs(0.004)
    alpha = pp.PairsSpread(window=30, history=40)
    assert alpha.signal(ctx(close=a[:10], pair_close=b[:10], half_life=[10.0])).abstained
    assert alpha.signal(ctx(close=a, pair_close=b)).abstained
    assert alpha.signal(ctx(close=a, pair_close=np.full(N, 100.0), half_life=[10.0])).abstained        # the partner did not move
    for bad in ({"exit_z": 3.0}, {"history": 20}):
        with pytest.raises(ValueError):
            pp.PairsSpread(window=30, **{"history": 40, **bad})

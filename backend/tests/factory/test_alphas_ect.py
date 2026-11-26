"""Alphas of the strategy ideas (SPEC-020: ECT-052, ECT-053, ECT-054): channel breakout, mean reversion, momentum. Values by hand."""
import math
from datetime import datetime, timezone

import numpy as np
import pytest

from hokma.backtest.spec import FillRules
from hokma.broker.costs import fill_price
from hokma.desks.core.signal import AlphaModel, Signal, SignalContext
from hokma.factory import audit as au
from hokma.factory.alphas import channel_breakout as cb
from hokma.factory.alphas import mean_reversion as mr
from hokma.factory.alphas import momentum as mo
from hokma.factory.alphas.tsmom import tsmom_weight
from hokma.factory.params import validate
from hokma.factory.studies import _h3, ic_decay
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult

AS_OF = datetime(2025, 3, 3, 21, 0, tzinfo=timezone.utc)


def ctx(**facts) -> SignalContext:
    return SignalContext("alpha.test", 7, "SWC", "NQ", AS_OF, "days", "h" * 8, facts={k: np.asarray(v, dtype=float) for k, v in facts.items()})


# ---- ECT-052: channel breakout ------------------------------------------------------------------------------------------------

#          t0      t1      t2       t3 (breakout)
HIGH = [10, 11, 12, 13, 14, 14]
LOW = [8, 9, 10, 11, 12, 10]
CLOSE = [9, 10, 11, 13, 14, 10.5]


def positions(high, low, close, **kw):
    return cb.channel_positions(np.array(high, float), np.array(low, float), np.array(close, float), 3, 2, **kw).tolist()


def test_the_entry_channel_excludes_the_current_bar_by_hand():
    """Bar 3 closes at 13 with a high of 13. The channel of the 3 bars before it is 8 to 12, so 13 breaks it. A channel that held the current
    bar would be 8 to 13 and 13 would not break it."""
    assert positions(HIGH, LOW, CLOSE)[3] == 1
    assert max(HIGH[0:3]) == 12 and max(HIGH[1:4]) == 13 and CLOSE[3] > max(HIGH[0:3]) and not CLOSE[3] > max(HIGH[1:4])


def test_the_exit_channel_of_m_bars_turns_the_view_flat_by_hand():
    """Long from bar 3. At bar 4 the lowest low of bars 2 and 3 is 10 and the close is 14: held. At bar 5 the lowest low of bars 3 and 4 is 11 and
    the close 10.5 is below it: flat. The entry channel (bars 2 to 4, low 10) is not broken by 10.5, so no short starts."""
    assert positions(HIGH, LOW, CLOSE) == [0, 0, 0, 1, 1, 0]


def test_a_short_breakout_and_its_exit():
    high = [-x for x in LOW]
    low = [-x for x in HIGH]
    close = [-x for x in CLOSE]
    assert positions(high, low, close) == [0, 0, 0, -1, -1, 0]            # the mirror image of the long case


H2 = [10, 11, 12, 13, 15, 16, 17, 17, 15, 18]
L2 = [8, 9, 10, 11, 14, 15, 16, 13, 13, 14]
C2 = [9, 10, 11, 13, 15, 16, 17, 13.8, 14.9, 18]


def test_the_skip_rule_passes_over_the_breakout_that_follows_a_winner_by_hand():
    """Long at 13 (bar 3), left at 13.8 on bar 7 (a gain of 0.8) while a short breakout (13.8 below the low 14 of bars 4 to 6) appears.
    Without the rule the short is taken (-1) and left at bar 9 (close 18 above the high 17 of bars 7 and 8: a loss), when a long breakout starts.
    With the rule the short is passed over (0) but followed: it ends at a loss, so the long breakout of bar 9 is taken."""
    assert positions(H2, L2, C2) == [0, 0, 0, 1, 1, 1, 1, -1, -1, 1]
    assert positions(H2, L2, C2, skip_after_winner=True) == [0, 0, 0, 1, 1, 1, 1, 0, 0, 1]


def test_a_filter_can_veto_an_entry():
    ok = np.array([True, True, True, False, True, True])
    assert positions(HIGH, LOW, CLOSE, entry_ok=ok) == [0, 0, 0, 0, 1, 0]       # blocked at bar 3, taken at bar 4 (close 14 above the high 13 of bars 1 to 3)


def test_the_alpha_gives_a_view_in_range_and_declares_its_facts():
    alpha = cb.ChannelBreakout(entry_n=3, exit_m=2, history=6)
    assert isinstance(alpha, AlphaModel) and alpha.lookback == 6 and alpha.FACTS == ("high", "low", "close")
    long = alpha.signal(ctx(high=HIGH[:5], low=LOW[:5], close=CLOSE[:5]))
    assert isinstance(long, Signal) and long.value == 1.0 and long.source == "alpha.channel_breakout"
    assert alpha.signal(ctx(high=HIGH, low=LOW, close=CLOSE)).value == 0.0
    assert alpha.signal(ctx(high=HIGH[:3], low=LOW[:3], close=CLOSE[:3])).abstained                # fewer than entry_n + 1 bars
    assert alpha.signal(ctx(high=HIGH, low=LOW)).abstained
    names = {c.name for c in long.criteria}
    assert names == {"close_vs_entry_high", "close_vs_entry_low"}


def test_the_breakout_fills_at_the_next_open_plus_one_tick():
    """The alpha decides at the close of bar 3. The house fill is the next open plus one tick: an open of 14.00 buys at 14.25 on a 0.25 tick."""
    signal = cb.ChannelBreakout(entry_n=3, exit_m=2, history=6).signal(ctx(high=HIGH[:4], low=LOW[:4], close=CLOSE[:4]))
    assert signal.value == 1.0 and FillRules().next_open_ticks == 1
    assert fill_price("buy", 14.0, ticks=FillRules().next_open_ticks, tick=0.25) == 14.25
    assert fill_price("sell", 14.0, ticks=FillRules().next_open_ticks, tick=0.25) == 13.75


def test_the_regime_filter_of_the_intraday_variant_is_only_the_adx():
    alpha = cb.ChannelBreakout(entry_n=3, exit_m=2, history=6, adx_min=25.0)
    assert alpha.FACTS == ("high", "low", "close", "adx")
    assert {p.name for p in cb.ChannelBreakout.SCHEMA.params} == {"entry_n", "exit_m", "skip_after_winner", "adx_min", "history"}   # no other filter exists
    trending = alpha.signal(ctx(high=HIGH[:5], low=LOW[:5], close=CLOSE[:5], adx=[20, 20, 20, 20, 30]))
    assert trending.value == 1.0 and [c.name for c in trending.criteria][-1] == "adx" and trending.criteria[-1].passed is True
    weak = alpha.signal(ctx(high=HIGH[:5], low=LOW[:5], close=CLOSE[:5], adx=[20, 20, 20, 20, 20]))
    assert weak.value == 0.0 and weak.criteria[-1].passed is False
    assert alpha.signal(ctx(high=HIGH[:5], low=LOW[:5], close=CLOSE[:5])).abstained             # no adx fact: no view
    assert cb.ChannelBreakout().FACTS == ("high", "low", "close")                                # without a threshold the fact is not asked for


def test_the_parameters_next_to_the_defaults_are_valid_and_are_the_quality_gate_inputs():
    """The sensitivity surface (L-44) is the quality gate: every neighbor must build, and the audit fails a result whose neighbor falls."""
    base = validate(cb.ChannelBreakout.SCHEMA, {})
    neighbors = au.parameter_neighbors({k: v for k, v in base.items() if k in ("entry_n", "exit_m")}, 0.2, cb.ChannelBreakout.SCHEMA)
    assert [(n.label, n.params["entry_n"], n.params["exit_m"]) for n in neighbors] == [("entry_n-", 16, 10), ("entry_n+", 24, 10), ("exit_m-", 20, 8), ("exit_m+", 20, 12)]
    for n in neighbors:
        cb.ChannelBreakout(**validate(cb.ChannelBreakout.SCHEMA, n.params))
    with pytest.raises(ValueError):
        cb.ChannelBreakout(entry_n=10, exit_m=10)
    with pytest.raises(ValueError):
        cb.ChannelBreakout(entry_n=20, exit_m=10, history=21)


# ---- ECT-053: mean reversion ---------------------------------------------------------------------------------------------------

def test_the_z_state_machine_by_hand():
    """Entry 2, exit 0.5, stop 4, time exit after 3 bars. The walk below is written bar by bar in the comments of the expected list."""
    z = np.array([0, -2.5, -1.0, -0.4, 0.0, -2.5, -3.9, -4.5, -2.5, -1.0, -2.5])
    #            flat long  long  left  flat long  long  stop  held  free  long again
    assert mr.reversion_positions(z, 2.0, 0.5, 4.0, 3).tolist() == [0, 1, 1, 0, 0, 1, 1, 0, 0, 0, 1]
    assert mr.reversion_positions(-z, 2.0, 0.5, 4.0, 3).tolist() == [0, -1, -1, 0, 0, -1, -1, 0, 0, 0, -1]


def test_the_time_exit_leaves_after_the_stated_half_lives_and_waits_for_a_return_inside_the_band():
    z = np.array([-2.5, -2.4, -2.3, -2.2, -2.1, -1.0, -2.5])
    assert mr.reversion_positions(z, 2.0, 0.5, 4.0, 3).tolist() == [1, 1, 1, 0, 0, 0, 1]      # out at the third bar held, no re-entry at -2.2 and -2.1


def test_a_nan_closes_the_view():
    assert mr.reversion_positions(np.array([-2.5, np.nan, -2.5]), 2.0, 0.5, 4.0, 5).tolist() == [1, 0, 1]


def series_with_a_drop():
    base = [100.0 if i % 2 == 0 else 101.0 for i in range(29)]
    return base + [95.0]


def test_the_half_life_filters_the_entry_and_the_view_follows_the_z_score():
    alpha = mr.MeanReversion(history=30)
    close = series_with_a_drop()
    z_last = float(mr.rolling_zscore(close, 10)[-1])                    # window of 2 half-lives of 5 bars
    assert -4.0 < z_last < -2.0
    in_range = alpha.signal(ctx(close=close, half_life=[5.0]))
    assert in_range.value == 1.0 and in_range.criteria[0].name == "half_life" and in_range.criteria[0].passed is True
    assert in_range.criteria[1].value == pytest.approx(z_last)
    too_slow = alpha.signal(ctx(close=close, half_life=[100.0]))
    assert too_slow.value == 0.0 and too_slow.criteria[0].passed is False
    assert alpha.signal(ctx(close=close, half_life=[float("nan")])).abstained
    assert alpha.signal(ctx(close=close)).abstained
    assert alpha.signal(ctx(close=close[:5], half_life=[5.0])).abstained


def test_the_half_life_is_measured_in_session_minutes_for_dt_and_in_days_for_swc():
    assert mr.half_life_in_unit(12, 5) == 60.0                          # 12 bars of 5 minutes
    assert mr.half_life_in_unit(12, 1) == 12.0                          # 12 daily bars
    close = series_with_a_drop()
    dt = mr.MeanReversion(unit="session_minutes", bar_size=5.0, min_half_life=10.0, max_half_life=120.0, history=30)
    assert dt.signal(ctx(close=close, half_life=[5.0])).criteria[0].value == 25.0
    assert dt.signal(ctx(close=close, half_life=[30.0])).value == 0.0   # 150 minutes: outside the range
    swc = mr.MeanReversion(unit="days", bar_size=1.0, min_half_life=5.0, max_half_life=60.0, history=30)
    assert swc.signal(ctx(close=close, half_life=[5.0])).criteria[0].value == 5.0
    assert swc.signal(ctx(close=close, half_life=[4.0])).value == 0.0   # 4 days: too fast


def test_no_entry_rests_on_a_relative_strength_level():
    assert not [p.name for p in mr.MeanReversion.SCHEMA.params if "rsi" in p.name.lower() or "oversold" in p.name.lower()]
    signal = mr.MeanReversion(history=30).signal(ctx(close=series_with_a_drop(), half_life=[5.0]))
    assert not [c.name for c in signal.criteria if "rsi" in c.name.lower()]
    for bad in ({"entry_z": 1.0, "exit_z": 1.5}, {"min_half_life": 70.0}):
        with pytest.raises(ValueError):
            mr.MeanReversion(**bad)


def test_the_half_life_comes_from_the_h3_function_or_an_injected_one(monkeypatch):
    assert mr.estimate_half_life([1.0, 2.0], lambda v: QuantResult("x", 7.5, "bars", "ar1")) == 7.5
    assert mr.estimate_half_life([1.0, 2.0], lambda v: 4) == 4.0
    assert mr.estimate_half_life([1.0, 2.0], lambda v: QuantResult.empty("x", "bars", "ar1", "no mean reversion")) is None
    monkeypatch.setattr(_h3, "ALLOWED_PREFIX", "nowhere.")
    with pytest.raises(_h3.H3Unavailable, match="hokma.quant.stats.timeseries is not a quant module"):
        mr.estimate_half_life([1.0, 2.0])


# ---- ECT-054: momentum ---------------------------------------------------------------------------------------------------------

def test_momentum_skips_the_last_bars_by_hand():
    close = 100.0 * 1.001 ** np.arange(300)
    assert mo.momentum_return(close, 252, 21) == pytest.approx(1.001 ** 252 - 1)      # close[-22] / close[-274]: 252 steps apart
    crash = np.array([100.0, 110.0, 121.0, 60.0, 50.0])
    assert mo.momentum_return(crash, 2, 2) == pytest.approx(121.0 / 100.0 - 1)       # the last two bars are skipped
    with pytest.raises(BadInput):
        mo.momentum_return(close[:100], 252, 21)


def test_cross_sectional_weights_by_hand():
    scores = {c: float(10 - i) for i, c in enumerate("abcdefghij")}                  # a is best (10), j is worst (1)
    w = mo.cross_sectional_weights(scores, 0.2)
    assert w["a"] == w["b"] == 0.5 and w["i"] == w["j"] == -0.5 and all(w[c] == 0.0 for c in "cdefgh")
    assert mo.cross_sectional_weights(scores, 0.2, long_short=False)["j"] == 0.0
    assert mo.cross_sectional_weights({"x": 1.0, "y": 1.0, "z": 0.0}, 0.34)["x"] == 1.0   # one name each side; the tie goes to the first name
    with pytest.raises(BadInput):
        mo.cross_sectional_weights({"x": 1.0}, 0.2)
    with pytest.raises(BadInput):
        mo.cross_sectional_weights(scores, 0.9)


def test_dual_momentum_holds_the_best_or_the_refuge_by_hand():
    returns = {"spy": 0.12, "efa": 0.08, "agg": 0.02}
    assert mo.dual_momentum_pick(returns, 0.04, "bil") == "spy"
    assert mo.dual_momentum_pick(returns, 0.15, "bil") == "bil"           # the best does not beat cash: the refuge
    assert mo.dual_momentum_pick({"a": 0.1, "b": 0.1}, 0.0, "bil") == "a"
    with pytest.raises(BadInput):
        mo.dual_momentum_pick({}, 0.0, "bil")


def test_volatility_scale_and_the_crash_switch_by_hand():
    assert mo.vol_scale(0.10, 0.20, 1.0) == 0.5 and mo.vol_scale(0.10, 0.05, 1.0) == 1.0 and mo.vol_scale(0.10, 0.05, 2.0) == 2.0
    history = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])       # 90th percentile, linear: 0.9 + 0.1 * (1.0 - 0.9) = 0.91
    assert mo.momentum_off(0.95, history, 90) is True and mo.momentum_off(0.90, history, 90) is False
    assert mo.momentum_off(0.2, history, 90, factor_return_12m=-0.01) is True      # the factor lost money over 12 months
    assert mo.momentum_off(0.2, history, 90, factor_return_12m=0.03) is False
    with pytest.raises(BadInput):
        mo.vol_scale(0.1, 0.0, 1.0)
    with pytest.raises(BadInput):
        mo.momentum_off(0.2, history, 100)


def test_the_cost_of_turnover_is_the_function_of_the_ic_study_and_the_trap_is_corrected():
    """20 bp on 35 percent one way monthly turnover: 0.35 * 12 * 20 bp = 0.84 percent a year (1.68 percent if the 20 bp is charged on each side).
    A gross return of 9.84 percent on a 12 percent volatility is a gross Sharpe of 0.82: net 0.75, or 0.68. The break-even is 234 bp, not 55."""
    assert mo.annual_cost is ic_decay.annual_cost and mo.breakeven_cost_bps is ic_decay.breakeven_cost_bps
    one_way, both_ways = mo.annual_cost(0.35, 12, 20), mo.annual_cost(0.35, 12, 40)
    assert one_way == pytest.approx(0.0084) and both_ways == pytest.approx(0.0168)
    assert (0.0984 - one_way) / 0.12 == pytest.approx(0.75) and (0.0984 - both_ways) / 0.12 == pytest.approx(0.68)
    assert mo.breakeven_cost_bps(0.0984, 0.35, 12) == pytest.approx(234.2857142857, rel=1e-9)


def closes_from(returns, start=100.0):
    return start * np.cumprod(1.0 + np.asarray(returns, dtype=float))


def test_the_momentum_view_is_the_sign_weighted_by_target_volatility_over_realized_by_hand():
    alpha = mo.Momentum(periods=20, skip=5, vol_periods=10, target_vol=0.10, vol_off_percentile=0.0)
    returns = [0.02, 0.0] * 20                                           # 40 returns: 5 gains of 2 percent in each 10
    close = np.r_[100.0, closes_from(returns)]
    window = np.array(returns[-10:])
    sigma = float(window.std(ddof=1)) * math.sqrt(252)                    # 0.01054 * 15.87 = 0.1673 annual
    signal = alpha.signal(ctx(close=close))
    assert signal.value == pytest.approx(0.10 / sigma) == pytest.approx(tsmom_weight(1.0, sigma, 0.10))
    assert signal.value == pytest.approx(0.5976, abs=1e-4)
    assert signal.criteria[0].name == "momentum_return" and signal.criteria[0].value == pytest.approx(mo.momentum_return(close, 20, 5))


def test_a_crash_in_the_skipped_bars_does_not_change_the_sign():
    alpha = mo.Momentum(periods=20, skip=5, vol_periods=10, vol_off_percentile=0.0)
    returns = [0.02, 0.0] * 17 + [0.0, -0.3, 0.0, -0.3, 0.0, 0.0]
    close = np.r_[100.0, closes_from(returns)]
    assert mo.momentum_return(close, 20, 5) > 0
    assert alpha.signal(ctx(close=close)).value is not None


def test_negative_momentum_is_a_short_view_unless_the_book_is_long_only():
    returns = [-0.02, 0.0] * 20
    close = np.r_[100.0, closes_from(returns)]
    both = mo.Momentum(periods=20, skip=5, vol_periods=10, vol_off_percentile=0.0)
    long_only = mo.Momentum(periods=20, skip=5, vol_periods=10, vol_off_percentile=0.0, long_only=True)
    assert both.signal(ctx(close=close)).value < 0 and long_only.signal(ctx(close=close)).value == 0.0


def test_dual_momentum_view_goes_to_the_refuge_when_the_return_does_not_beat_cash():
    alpha = mo.Momentum(periods=20, skip=5, vol_periods=10, vol_off_percentile=0.0, absolute_filter=True, long_only=True)
    close = np.r_[100.0, closes_from([0.02, 0.0] * 20)]
    mom = mo.momentum_return(close, 20, 5)
    assert alpha.FACTS == ("close", "risk_free")
    assert alpha.signal(ctx(close=close, risk_free=[mom - 0.01])).value > 0
    refuge = alpha.signal(ctx(close=close, risk_free=[mom + 0.01]))
    assert refuge.value == 0.0 and "refuge" in refuge.reason
    assert alpha.signal(ctx(close=close)).abstained


def test_the_view_is_switched_off_when_volatility_is_above_its_90th_percentile():
    alpha = mo.Momentum(periods=20, skip=5, vol_periods=10, vol_off_percentile=90.0, vol_history=40)
    calm = [0.001, 0.0] * 15
    wild = [0.05, -0.04] * 5
    close = np.r_[100.0, closes_from(calm + wild)]
    assert mo.momentum_return(close, 20, 5) > 0
    off = alpha.signal(ctx(close=close))
    assert off.value == 0.0 and off.criteria[-1].name == "vol_vs_history" and off.criteria[-1].passed is False and "90th percentile" in off.reason
    steady = np.r_[100.0, closes_from([0.002, 0.0] * 20)]
    on = alpha.signal(ctx(close=steady))
    assert on.value > 0 and on.criteria[-1].passed is True
    assert alpha.signal(ctx(close=close[:30])).abstained


def test_the_factor_gate_switches_the_view_off_when_the_factor_lost_money():
    alpha = mo.Momentum(periods=20, skip=5, vol_periods=10, vol_off_percentile=0.0, factor_gate=True)
    close = np.r_[100.0, closes_from([0.02, 0.0] * 20)]
    assert alpha.signal(ctx(close=close, factor_return_12m=[0.05])).value > 0
    assert alpha.signal(ctx(close=close, factor_return_12m=[-0.05])).value == 0.0
    assert alpha.signal(ctx(close=close)).abstained
    with pytest.raises(ValueError):
        mo.Momentum(periods=20, skip=20)
    with pytest.raises(ValueError):
        mo.Momentum(vol_periods=60, vol_history=100)

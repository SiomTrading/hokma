"""The evidence of the calibration (SPEC-020, section 4.9, step 5) and the reasons of the gate. The numbers come from `hokma.quant`; what is checked here is what this
module hands them and how it reads what comes back."""
import dataclasses
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from hokma.backtest.calibration import gate as gate_mod
from hokma.backtest.calibration import premium
from hokma.backtest.calibration.premium import ChainDay, Trade
from hokma.quant.validation.evidence import EvidenceBundle


def trade(pnl, margin=1000.0, *, finished=True, fill=0):
    return Trade("credit_put_spread", fill - 1, fill, fill + 5, pnl, margin, 1, finished, "profit_take", (pnl,))


def bundle(**changes):
    base = dict(n_observations=2000, n_strategies=12, n_trials=18, best_strategy="credit_put_spread|threshold=1.2", best_sharpe=1.0, cpcv={}, walk_forward={},
                pbo=0.2, dsr=0.99, sr0=0.1, leakage_free=True)
    return EvidenceBundle(**{**base, **changes})


EXPECTANCY = {"n": 40, "mean": 0.05, "ci_low": 0.01, "ci_high": 0.09}


def reasons(gate=None, **changes):
    args = dict(oos=[trade(1.0)] * 40, expectancy=EXPECTANCY, evidence=bundle(), efficiency=0.8, drawdown=6.0, limit=10.0)
    args.update(changes)
    return premium._passes(gate or gate_mod.load_default(), **args)


def test_the_expectancy_per_margin_dollar_is_the_mean_of_result_over_margin_with_a_bootstrap_interval():
    """Twenty trades at 100 on 1,000 of margin (0.10) and twenty at 300 (0.30): the mean is 0.20. A trade that has not closed is left out."""
    trades = [trade(100.0)] * 20 + [trade(300.0)] * 20 + [trade(-9999.0, finished=False)]
    found = premium.expectancy_per_margin_dollar(trades, seed=4)
    assert found["n"] == 40 and found["mean"] == pytest.approx(0.20)
    assert 0.10 < found["ci_low"] < 0.20 < found["ci_high"] < 0.30
    assert premium.expectancy_per_margin_dollar(trades, seed=4) == found                      # the same seed, the same interval
    assert premium.expectancy_per_margin_dollar(trades, seed=5)["ci_low"] != found["ci_low"]


def test_few_or_equal_trades_give_a_degenerate_interval_and_none_gives_nothing():
    assert premium.expectancy_per_margin_dollar([trade(50.0)]) == {"n": 1, "mean": 0.05, "ci_low": 0.05, "ci_high": 0.05}
    same = premium.expectancy_per_margin_dollar([trade(50.0)] * 5)
    assert (same["mean"], same["ci_low"], same["ci_high"]) == (0.05, 0.05, 0.05)
    assert premium.expectancy_per_margin_dollar([]) == {"n": 0, "mean": None, "ci_low": None, "ci_high": None}


def test_the_probability_of_overfitting_leaves_out_the_columns_with_no_trade_in_a_block():
    rng = np.random.default_rng(2)
    frame = pd.DataFrame({"a": rng.normal(0.001, 0.01, 320), "b": rng.normal(0.0, 0.01, 320), "never": np.zeros(320), "late": np.r_[np.zeros(160), rng.normal(0, 0.01, 160)]})
    value, used = premium.traded_pbo(frame, n_blocks=16)
    assert used == 2 and 0.0 <= value <= 1.0                                                  # "never" and "late" have a block of zeros
    assert premium.traded_pbo(frame[["a", "never"]], n_blocks=16) == (None, 1)               # one column is no matrix of tries
    assert premium.traded_pbo(frame.iloc[:20], n_blocks=16) == (None, 0)                     # blocks of one row have no variance


def test_the_worst_drawdown_is_measured_on_the_equity_of_the_out_of_sample_results():
    """Capital 1,000 and results -100, -100, +300, -50: equity 900, 800, 1,100, 1,050. The worst fall from the start is 1,000 to 800: 20 percent."""
    days = [ChainDay(date(2024, 1, 1 + k), Decimal(100), ()) for k in range(4)]
    assert premium.worst_drawdown_pct([-100, -100, 300, -50], [0, 1, 2, 3], days, Decimal(1000)) == pytest.approx(20.0)
    assert premium.worst_drawdown_pct([100, 100, 100, 100], [0, 1, 2, 3], days, Decimal(1000)) == pytest.approx(0.0)
    assert premium.worst_drawdown_pct([-2000, 10], [0, 1], days, Decimal(1000)) == 100.0     # the account is gone
    assert premium.worst_drawdown_pct([5.0], [0], days, Decimal(1000)) is None


def test_a_gate_that_is_met_has_no_reason():
    assert reasons() == []


def test_each_thing_that_is_not_met_has_its_own_stable_reason_in_a_fixed_order():
    assert reasons(oos=[trade(1.0)] * 29) == ["too_few_oos_trades:29<30"]
    assert reasons(expectancy={**EXPECTANCY, "mean": -0.01}) == ["expectancy_not_positive:mean=-0.01"]
    assert reasons(expectancy={"n": 0, "mean": None, "ci_low": None, "ci_high": None})[0].startswith("expectancy_not_positive")
    assert reasons(evidence=bundle(dsr=0.9)) == ["dsr_below_min:0.9<0.95"]
    assert reasons(evidence=bundle(dsr=None))[0].startswith("dsr_below_min:None")
    assert reasons(evidence=bundle(pbo=0.51)) == ["pbo_above_max:0.51>0.5"]
    assert reasons(evidence=bundle(pbo=None)) == ["pbo_above_max:None>0.5"]
    assert reasons(efficiency=0.49) == ["wf_efficiency_below_min:0.49<0.5"] and reasons(efficiency=None) == ["wf_efficiency_below_min:None<0.5"]
    assert reasons(drawdown=10.01) == ["drawdown_above_limit:10.01>10.0"] and reasons(drawdown=None) == ["drawdown_above_limit:None>10.0"]
    assert reasons(limit=None) == ["drawdown_limit_missing"]
    assert reasons(evidence=bundle(leakage_free=None, best_strategy=None, dsr=None, pbo=None, warnings=("need at least 2 non constant strategies",)))[0].startswith("evidence_insufficient")
    assert reasons(evidence=bundle(leakage_free=False))[0].startswith("evidence_insufficient")


def test_every_reason_is_listed_when_nothing_is_met():
    found = reasons(oos=[], expectancy={"n": 0, "mean": None, "ci_low": None, "ci_high": None}, evidence=bundle(dsr=0.1, pbo=0.9), efficiency=-1.0, drawdown=50.0)
    assert [r.split(":")[0] for r in found] == ["too_few_oos_trades", "expectancy_not_positive", "dsr_below_min", "pbo_above_max", "wf_efficiency_below_min", "drawdown_above_limit"]


def test_the_gate_numbers_are_the_ones_that_decide_not_fixed_numbers():
    tight = dataclasses.replace(gate_mod.load_default(), min_oos_trades=50, min_dsr=0.99, max_pbo=0.1, min_wf_efficiency=0.9)
    assert reasons(tight) == ["too_few_oos_trades:40<50", "pbo_above_max:0.2>0.1", "wf_efficiency_below_min:0.8<0.9"]       # a dsr of 0.99 meets a minimum of 0.99

"""Reference vectors for the metrics of a run (SPEC-020: L-30, L-38, V-21, V-23).

The vectors are plain data written down from published test cases of two open source libraries (Apache-2.0 data, no code): the probabilistic
Sharpe ratio cases, the recovery time of a drawdown and the statistics of closed trades. Each vector is run against the function of
hokma.quant that computes the same thing; a vector with no function is listed in MISSING and the test fails.
"""
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from hokma.quant import metrics, registry, validation
from hokma.quant.metrics import drawdown, trades
from hokma.quant.validation import overfit

UTC = timezone.utc

# ---------------------------------------------------------------------------------------------------------------- L-30: PSR
# (per period sharpe, benchmark sharpe, skewness, raw kurtosis, observations) -> probability, rounded to 4 decimals
PSR_MOMENT_VECTORS = [
    ((0.15, 0.0, -0.3, 4.0, 252), 0.9894),
    ((0.10, 0.0, 0.10, 3.2, 100), 0.8407),
    ((0.20, 0.0, -0.20, 3.5, 150), 0.9910),
    ((-0.05, 0.0, 0.05, 4.1, 80), 0.3287),
]
# series cases: the benchmark is the observed (per period) Sharpe ratio of another series; the expected probability is 0.5, 1 and 0
PERFORMANCE = [0.01, 0.02, 0.01, 0.0, 0.0, 3.0]
LOSING = [0.0, 0.0, 0.0, -0.1, 0.0, 0.01, 0.0]


def observed_sharpe(values) -> float:
    s = np.asarray(values, dtype=float)
    return float(s.mean() / s.std(ddof=1))


def series(values) -> pd.Series:
    return pd.Series(values, index=pd.bdate_range("2022-01-03", periods=len(values)))


def test_psr_matches_the_reference_vectors_with_raw_kurtosis_not_annualized_L_30():
    for (sr, bench, skew, kurt, n), expected in PSR_MOMENT_VECTORS:
        got = overfit.psr_moments(sr=sr, sr_benchmark=bench, skew=skew, kurtosis_raw=kurt, n=n).value
        assert round(got, 4) == expected, (sr, got)
    assert overfit.psr_moments(sr=0.15, skew=-0.3, kurtosis_raw=4.0, n=252).value == overfit.psr_moments(sr=0.15, skew=-0.3, kurtosis_raw=4.0, n=252, sr_benchmark=0.0).value


def test_psr_on_series_against_a_benchmark_sharpe_L_30():
    same = validation.psr(series(PERFORMANCE), sr_benchmark=observed_sharpe(PERFORMANCE))
    assert same.value == pytest.approx(0.5, abs=1e-3)                       # the benchmark is the series itself: they cancel
    beats = validation.psr(series(PERFORMANCE), sr_benchmark=observed_sharpe(LOSING)).value      # the reference rounds to 1; raw kurtosis gives 0.9982
    assert 0.995 < beats <= 1.0
    assert validation.psr(series(LOSING), sr_benchmark=observed_sharpe(PERFORMANCE)).value < 0.01      # loses to a good one: the reference says 0
    flat = validation.psr(series([0.0, 0.0, 0.0]))
    assert flat.value is None and flat.warnings                              # undefined is reported, never reported as zero
    assert metrics.psr(series(PERFORMANCE)).value == validation.psr(series(PERFORMANCE)).value      # the metrics entry point is the same function


# ---------------------------------------------------------------------------------------------------------------- L-38: drawdown recovery
RECOVERY_VECTORS = [([100, 90, 100], 2), ([100, 90, 95, 100], 3), ([100, 90, 100, 90, 100], 2), ([100, 90, 100, 90, 80, 100], 3), ([100, 90, 95, 90, 100], 4),
                    ([100, 90, 80, 70], 0), ([100, 90, 95, 90], 0), ([50, 100, 98, 99, 100], 3), ([100, 100, 100], 0), ([100], 0),
                    ([100, 98, 100, 101, 100, 99], 2), ([100, 97, 99, 97, 100], 4)]


def equity_returns(values) -> pd.Series:
    eq = pd.Series(values, index=pd.date_range("2025-01-01", periods=len(values)), dtype=float)
    r = eq.pct_change()
    r.iloc[0] = 0.0
    return r


# ---------------------------------------------------------------------------------------------------------------- L-38: statistics of closed trades
START = datetime(2015, 8, 6, 15, 30, tzinfo=UTC)


def trade(start_min, end_min, pnl, mae, mfe, fee=2.0):
    return {"pnl_usd": pnl, "entry_at": START + timedelta(minutes=start_min), "exit_at": START + timedelta(minutes=end_min), "costs_usd": fee,
            "mae_usd": mae, "mfe_usd": mfe}


# two losers and one winner: -20 (0-20 min), -20 (10-20), +10 (30-40)
TWO_LOSERS_ONE_WINNER = ([trade(0, 20, -20, -30, 5), trade(10, 20, -20, -40, 30), trade(30, 40, 10, -15, 30)],
                         {"n_trades": 3, "n_wins": 1, "n_losses": 2, "total_pnl_usd": -30, "gross_profit_usd": 10, "gross_loss_usd": -40, "largest_win_usd": 10,
                          "largest_loss_usd": -20, "avg_pnl_usd": -10, "avg_win_usd": 10, "avg_loss_usd": -20, "avg_duration_seconds": 800,
                          "avg_win_duration_seconds": 600, "avg_loss_duration_seconds": 900, "max_consecutive_wins": 1, "max_consecutive_losses": 2,
                          "payoff_ratio": 0.5, "win_rate": 1 / 3, "loss_rate": 2 / 3, "avg_mae_usd": -85 / 3, "avg_mfe_usd": 65 / 3, "largest_mae_usd": -40,
                          "largest_mfe_usd": 30, "pnl_std_usd": 17.3205080756888, "profit_factor": 0.25, "total_costs_usd": 6})
# a loser of -80000 (0-20 min) and a winner of +108000 (20-30 min)
LOSER_AND_WINNER = ([trade(0, 20, -80000, -80000, 0), trade(20, 30, 108000, 0, 108000)],
                    {"n_trades": 2, "n_wins": 1, "n_losses": 1, "total_pnl_usd": 28000, "avg_pnl_usd": 14000, "avg_win_usd": 108000, "avg_loss_usd": -80000,
                     "avg_duration_seconds": 900, "avg_win_duration_seconds": 600, "avg_loss_duration_seconds": 1200, "payoff_ratio": 1.35,
                     "profit_factor": 1.35, "avg_mae_usd": -40000, "avg_mfe_usd": 54000, "largest_mae_usd": -80000, "largest_mfe_usd": 108000,
                     "pnl_std_usd": 132936.074863071, "win_rate": 0.5, "loss_rate": 0.5, "max_consecutive_wins": 1, "max_consecutive_losses": 1,
                     "total_costs_usd": 4, "exposure": 1.0})
TRADE_VECTORS = [TWO_LOSERS_ONE_WINNER, LOSER_AND_WINNER]
FUNCTIONS_OF = {"recovery": "metrics.drawdown_duration", "trade_stats": "metrics.trade_stats", "psr": "validation.psr_moments"}


def test_every_reference_vector_has_a_function_in_quant_L_38():
    registry.load_all()
    registered = {s.name for s in registry.list(include_blocked=True)}
    MISSING = sorted(f"{kind}: {name}" for kind, name in FUNCTIONS_OF.items() if name not in registered)
    assert not MISSING, MISSING


def test_drawdown_recovery_vectors_L_38():
    for values, expected in RECOVERY_VECTORS:
        got = drawdown.drawdown_duration(equity_returns(values)).value["max_recovery_days"]
        assert got == expected, values
    assert drawdown.drawdown_duration(equity_returns([100, 90, 80, 70])).value["max_underwater_days"] == 3      # an open drawdown is still measured


@pytest.mark.parametrize("rows, expected", TRADE_VECTORS)
def test_trade_statistics_vectors_L_38(rows, expected):
    value = registry.call("metrics.trade_stats", trades=rows).value
    for key, want in expected.items():
        assert value[key] == pytest.approx(want, rel=1e-9, abs=1e-9), key


# ---------------------------------------------------------------------------------------------------------------- V-21, V-23
def test_the_deflated_sharpe_of_the_backtest_is_the_one_of_validation_V_21():
    r = pd.read_csv(__file__.replace("test_reference_vectors.py", "fixtures/returns_daily.csv"), parse_dates=["date"], index_col="date")["return"]
    via_module = validation.dsr(r, n_trials=50, sr_std=0.01)
    via_registry = registry.call("validation.dsr", returns=r, n_trials=50, sr_std=0.01)
    assert via_module.value == via_registry.value and via_module.piece_ids == ("FM-03", "FF-011", "FT-067")
    assert validation.dsr(r, n_trials=500, sr_std=0.01).value < via_module.value        # more trials deflate the Sharpe more


def test_walk_forward_partitions_keep_purge_and_embargo_V_23():
    folds = validation.split(300, scheme="walk_forward", n_folds=4, purge=5, embargo=2)
    assert folds
    for fold in folds:
        gap = min(abs(int(t) - int(s)) for t in fold.test for s in fold.train)
        assert gap >= 5 and gap >= 5 + 2          # no test bar within 5 bars of a training bar, and the embargo of 2 on top
    assert validation.split is not None and registry.describe("validation.split").fn is not None

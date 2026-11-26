"""Criterion 3 of SPEC-020: optimization (grid, TPE and refinement with the pre-sweep), Monte Carlo and walk forward with PBO and DSR through the tools. The DSR
uses the count of the ledger of trials, the failed runs included, and never a number that the caller of the tool passes."""
import pytest
from backtest.session_support import session_bars
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)
from e2e_h4.support import bars, caller, failed, ok, run_args, use_services

from hokma.backtest import _lib, trials
from hokma.kit import hokma_tools

pytestmark = requires_engine
SPACE = {"axes": [{"name": "fast", "kind": "int", "low": 3, "high": 7, "step": 1}, {"name": "slow", "kind": "int", "low": 10, "high": 30, "step": 10}],
         "constraints": [{"left": "fast", "op": "lt", "right": "slow"}]}              # 15 valid points; a fast of 7 is the point that fails
WHO = caller("hokma")


def rows(db, sql, **params):
    with db.read() as u:
        return u.session.query(sql, params)


def ledger(db):
    (row,) = rows(db, "SELECT COUNT(*) AS n, SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed FROM backtest_trials WHERE strategy_id = 'cross'")
    return row["n"], row["failed"] or 0


@pytest.fixture
def series(db, monkeypatch, tmp_path):
    data = bars(1500)
    use_services(monkeypatch, data, db, tmp_path)
    return data


def test_a_grid_without_the_pre_sweep_runs_every_point_in_the_engine_and_the_failed_ones_are_trials(db, series):
    found = ok("backtest_optimize", run_args(series, space=SPACE, method="grid", n_trials=15, objective="net_pnl", presweep=False), WHO)
    assert found["method"] == "grid" and found["presweep"] is False and found["best"]["status"] == "done" and found["best"]["source"] == "engine"
    assert (found["trials_run"], found["failed_trials"]) == (15, 3)                       # the three points of fast 7 fail and are still tried
    assert ledger(db) == (15, 3) and found["n_trials"] == 15
    assert failed("backtest_optimize", run_args(series, space=SPACE, method="grid", n_trials=14, objective="net_pnl"), WHO)["error_code"] == "bad_input"   # never cut short


def test_a_grid_with_the_pre_sweep_ranks_fast_and_confirms_the_best_in_the_engine_and_both_are_trials(db, series):
    found = ok("backtest_optimize", run_args(series, space=SPACE, method="grid", n_trials=15, objective="net_pnl"), WHO)
    assert found["presweep"] is True and found["best"]["source"] == "engine" and any(w.startswith("presweep_top_k:") for w in found["warnings"])
    confirmed = int(next(w for w in found["warnings"] if w.startswith("presweep_top_k:")).split(":")[1])
    assert 0 < confirmed <= 20 and found["trials_run"] == 15 + confirmed                    # N swept and K confirmed, each one row
    assert ledger(db)[0] == found["n_trials"] == 15 + confirmed and ledger(db)[1] >= 3


@pytest.mark.skipif(not _lib.search_available(), reason="the TPE search library has no wheel on this platform")
def test_tpe_is_seeded_and_a_repeated_point_is_a_cache_hit_and_not_a_new_trial(db, series):
    found = ok("backtest_optimize", run_args(series, space=SPACE, method="tpe", n_trials=40, objective="net_pnl", presweep=False, seed=3), WHO)
    assert found["method"] == "tpe" and found["cache_hits"] > 0 and found["trials_run"] <= 15 and ledger(db)[0] == found["n_trials"] == found["trials_run"]


def test_a_refinement_walks_step_by_step_around_the_best_point_and_counts_every_try(db, series):
    found = ok("backtest_optimize", run_args(series, space=SPACE, method="refine", n_trials=20, objective="net_pnl", presweep=False), WHO)
    assert found["method"] == "refine" and found["best"] is not None and ledger(db)[0] == found["n_trials"] == found["trials_run"] >= 2


def test_the_walk_forward_gives_the_evidence_with_pbo_and_a_dsr_that_counts_the_whole_ledger(db, series):
    done = ok("backtest_run", run_args(series), WHO)
    assert failed("backtest_evidence", {"strategy_id": "cross", "version": 1}, WHO)["error_code"] == "no_data"      # a walk forward is needed first
    forward = ok("backtest_walkforward", run_args(series, space=SPACE, scheme="rolling", train=250, test=250, purge=2, embargo=1), WHO)
    assert forward["leakage_free"] is True and forward["windows"] and forward["efficiency"] is not None
    ok("backtest_montecarlo", {"run_id": done["run_id"], "n": 200, "method": "placebo", "seed": 11}, WHO)
    bundle = ok("backtest_evidence", {"strategy_id": "cross", "version": 1}, WHO)
    count, failures = ledger(db)
    assert count == 1 + 15 and failures == 3                                                # the run and the fifteen points of the walk forward, failed ones included
    assert bundle["n_trials"] == count == bundle["parameters"]["ledger_trials"] == trials.count("cross")
    assert 0.0 <= bundle["pbo"] <= 1.0 and bundle["dsr"] is not None and bundle["sr0"] is not None and bundle["leakage_free"] is True
    assert bundle["parameters"]["walk_forward_run"]["run_id"] == forward["run_id"] and "monte_carlo_missing" not in bundle["warnings"]
    assert "n_trials" not in hokma_tools.TOOLS["backtest_evidence"][1]                      # the count is the ledger's: nobody passes it


def test_the_monte_carlo_resamples_a_run_by_bootstrap_by_permutation_and_by_placebo(db, monkeypatch, tmp_path):
    daily = session_bars("daily")                                                           # real daily sessions: seventy days of returns
    use_services(monkeypatch, daily, db, tmp_path)
    done = ok("backtest_run", run_args(daily, bar="1d", line="SWC", params={"fast": 3, "slow": 8}), WHO)
    assert done["status"] == "done" and done["trades"] >= 1
    boot = ok("backtest_montecarlo", {"run_id": done["run_id"], "n": 300, "method": "bootstrap", "seed": 4}, WHO)
    assert boot["figures"]["sharpe"] and boot["figures"]["cagr"] and boot["n"] == 300 and boot["seed"] == 4
    perm = ok("backtest_montecarlo", {"run_id": done["run_id"], "n": 300, "method": "permutation", "seed": 4}, WHO)
    assert perm["figures"]["max_drawdown"]
    placebo = ok("backtest_montecarlo", {"run_id": done["run_id"], "n": 300, "method": "placebo", "seed": 4}, WHO)
    assert 0.0 < placebo["p_value"] <= 1.0 and placebo["figures"]["placebo"]["median"] is not None

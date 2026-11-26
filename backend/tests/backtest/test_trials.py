"""The ledger of trials (SPEC-020: V-22, L-45): every run counts for the deflated Sharpe ratio, the failed ones with their error, and nothing
can be removed from the ledger."""
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.backtest import results, trials
from hokma.backtest.trials import TrialError


def new_run(database, strategy_id="s1", status="running") -> int:
    with database.write() as u:
        return u.session.insert("backtest_runs", {"kind": "run", "strategy_id": strategy_id, "spec_json": "{}", "spec_hash": "h", "engine_version": "e",
                                                  "seed": 0, "status": status})


def test_after_50_runs_the_count_is_50_even_if_3_failed_and_deleting_cannot_lower_it_V_22(db):
    failing = {7, 23, 41}
    for i in range(50):
        run_id = new_run(db)
        if i in failing:
            results.record_failure(db, run_id, f"engine crashed at bar {i}")
            trials.record("s1", "h1", run_id, {"fast": 5 + i}, "failed", None)
        else:
            trials.record("s1", "h1", run_id, {"fast": 5 + i}, "done", 0.1 * i)
    assert trials.count("s1") == 50 and trials.count("s1", hypothesis_id="h1") == 50 and trials.count("s1", hypothesis_id="other") == 0
    assert trials.count("another") == 0
    with db.write() as u:
        with pytest.raises(Exception, match="append-only"):
            u.session.execute("DELETE FROM backtest_trials WHERE strategy_id = 's1'")
    with db.write() as u:
        with pytest.raises(Exception, match="append-only"):
            u.session.execute("UPDATE backtest_trials SET status = 'done'")
    with db.write() as u:
        with pytest.raises(Exception):                       # a run that a trial points at cannot be deleted either
            u.session.execute("DELETE FROM backtest_runs WHERE strategy_id = 's1'")
    assert trials.count("s1") == 50


def test_the_hypotheses_of_one_strategy_are_counted_apart_and_together_V_22(db):
    for h, n in (("a", 4), ("b", 6)):
        for i in range(n):
            trials.record("s2", h, None, {"i": i}, "done", float(i))
    assert (trials.count("s2"), trials.count("s2", hypothesis_id="a"), trials.count("s2", hypothesis_id="b")) == (10, 4, 6)


def test_three_failed_runs_are_listed_with_their_message_and_count_in_n_trials_L_45(db):
    messages = ["no_data: no bars for CONT:NQ", "bad_spec: start must be before end", "engine crashed at bar 17"]
    for i, message in enumerate(messages):
        run_id = new_run(db, "s3")
        results.record_failure(db, run_id, message)
        trials.record("s3", "h", run_id, {"length": 10 + i}, "failed", None)
    trials.record("s3", "h", new_run(db, "s3", "done"), {"length": 99}, "done", 1.5)
    listed = trials.failed("s3")
    assert [f.error for f in listed] == messages and [dict(f.params) for f in listed] == [{"length": 10}, {"length": 11}, {"length": 12}]
    assert trials.count("s3") == 4                                       # the three failures are inside the number of trials
    assert trials.failed("s3", hypothesis_id="other") == ()


def test_a_failed_trial_with_no_run_keeps_its_message_in_its_parameters_L_45(db):
    trials.record("s4", "h", None, {"x": 1}, "failed", None, error="bad_spec: a stop needs a distance")
    (only,) = trials.failed("s4")
    assert only.run_id is None and only.error == "bad_spec: a stop needs a distance" and dict(only.params) == {"x": 1}
    assert trials.count("s4") == 1


def test_a_value_that_is_not_an_objective_still_counts_and_bad_input_is_refused_V_22(db):
    trials.record("s5", "h", None, {"x": 1}, "done", float("nan"))
    trials.record("s5", "h", None, {"x": 2}, "done", float("inf"))
    assert trials.count("s5") == 2
    for bad in (dict(strategy_id="", hypothesis_id="h", status="done"), dict(strategy_id="s", hypothesis_id="h", status="running"),
                dict(strategy_id="s" * 81, hypothesis_id="h", status="done")):
        with pytest.raises(TrialError):
            trials.record(bad["strategy_id"], bad["hypothesis_id"], None, {}, bad["status"], None)
    with pytest.raises(TrialError):
        trials.record("s", "h", None, {"x": float("nan")}, "done", None)
    with pytest.raises(TrialError):
        trials.record("s", "h", None, {"x": "y" * 21_000}, "done", None)
    with pytest.raises(TrialError):
        trials.count("")

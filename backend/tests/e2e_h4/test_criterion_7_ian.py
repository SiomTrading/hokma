"""Criterion 7 of SPEC-020: Ian with a fake model engine turns a hypothesis into a spec, asks for the backtest, and says nothing is finished without a fresh run id,
stops after three failed attempts and ends with `factory_propose`. He has no tool of a desk and none of an order."""
import json
import re
from dataclasses import replace

import pytest
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)
from e2e_h4.ian import FakeLLM, Session, offered_tools, tool
from e2e_h4.support import SPACE, bars, caller, failed, ok, run_args, spec_text, use_services

from hokma.backtest import trials
from hokma.factory import researcher, research_loop
from hokma.factory.researcher import ClaimRejected, ResearchRun, ToolNotAllowed
from hokma.kit import backtest_tools, hokma_tools
from hokma.kit.backtest_tools import build_spec

pytestmark = requires_engine


def report(run_id=None, *, spec_hash="a" * 64, claims=None, **over):
    base = {"hypothesis": "A fast average above a slow one means the move continues.", "spec": {"strategy_id": "cross", "version": 1, "spec_hash": spec_hash},
            "runs": [] if run_id is None else [{"run_id": run_id, "kind": "run"}], "evidence": [], "risks": ["the sample is one session of 1 minute bars"],
            "next_step": "validate out of sample", "status": "in_progress", "claims": claims or []}
    return json.dumps({**base, **over})


@pytest.fixture
def world(db, monkeypatch, tmp_path):
    data = bars(1500)
    use_services(monkeypatch, data, db, tmp_path)
    return data


def test_ian_goes_from_a_hypothesis_to_a_proposed_spec_with_runs_that_back_every_claim(world, db):
    def final(evidence):
        done = session.last("backtest_run")
        assert evidence["n_trials"] == trials.count("cross")                                              # the evidence he read counts the whole ledger
        return report(done["run_id"], spec_hash=session.last("factory_draft")["spec_hash"], claims=[{"kind": "backtest_done", "run_id": done["run_id"]}],
                      evidence=[{"claim": "The cross result net of costs", "value": float(done["realized_pnl_usd"]), "run_id": done["run_id"]}])

    model = FakeLLM(tool("factory_list", source="ideas", limit=3),
                    tool("factory_draft", spec_yaml=spec_text("cross")),
                    tool("order_place", desk_id=1, order={"orderType": "MARKET"}),                       # a model that tries an order: the engine does not offer it
                    tool("backtest_run", **run_args(world)),
                    tool("backtest_walkforward", **run_args(world, space=SPACE, scheme="rolling", train=250, test=250, purge=2, embargo=1)),
                    tool("backtest_evidence", strategy_id="cross", version=1),
                    final)
    session = Session(model)
    text = session.run()
    assert session.refused == ["order_place"]
    assert [n for n, _a, _e in session.answers] == ["factory_list", "factory_draft", "backtest_run", "backtest_walkforward", "backtest_evidence"]
    assert all(not is_error for _n, _a, is_error in session.answers)
    drafted, done = session.last("factory_draft"), session.last("backtest_run")
    assert (drafted["status"], drafted["version"]) == ("draft", 1) and done["status"] == "done"
    accepted = session.guard().accept(text)                                                              # the report names the run that this session started and that ended done
    assert accepted.runs[0].run_id == done["run_id"] and accepted.status == "in_progress"
    assert ok("factory_propose", {"strategy_id": "cross", "version": 1}, session.who)["status"] == "proposed"
    with db.read() as u:
        assert u.session.scalar("SELECT COUNT(*) FROM exec_proposals") == 0                              # the road ends at a spec: nothing was sent anywhere
        assert u.session.one("SELECT status, proposed_by FROM strategy_specs WHERE strategy_id = 'cross'")["proposed_by"] == researcher.AGENT_SLUG


def test_nothing_is_declared_done_without_a_fresh_run_id_of_this_session(world):
    ok("factory_draft", {"spec_yaml": spec_text("cross")})
    first = Session(FakeLLM(), conversation=1)
    done = ok("backtest_run", run_args(world), first.who)
    second = Session(FakeLLM(), conversation=2)                                                          # another conversation: its ledger holds no run
    for text, code in ((report(None, claims=[{"kind": "backtest_done"}]), "unbacked_claim"),
                       (report(None, next_step="the backtest finished and it works"), "unbacked_claim"),
                       (report(done["run_id"]), "stale_run_id"),
                       (report(done["run_id"], claims=[{"kind": "backtest_done", "run_id": done["run_id"]}]), "stale_run_id"),
                       (report(None, evidence=[{"claim": "Sharpe is 1.8", "value": 1.8}]), "number_without_source")):
        with pytest.raises(ClaimRejected) as caught:
            second.guard().accept(text)
        assert caught.value.code == code, text
    assert first.guard().accept(report(done["run_id"])).runs[0].run_id == done["run_id"]               # in its own session the same run is fine
    with pytest.raises(ClaimRejected) as nobody:                                                       # a run id that does not exist
        first.guard().accept(report(9999))
    assert nobody.value.code == "stale_run_id"


def test_the_tests_of_the_spec_run_first_and_a_spec_without_tests_or_with_a_failing_test_is_never_backtested(world, db):
    ian = caller(researcher.AGENT_SLUG, 3, 3)
    assert failed("backtest_run", run_args(world), ian)["error_code"] == "bad_input"                    # no saved spec at all
    ok("factory_draft", {"spec_yaml": spec_text("cross", signals="", sizes="")}, ian)
    assert failed("backtest_run", run_args(world), ian)["error_code"] == "missing_tests"
    wrong = "\n    - name: wrong\n      facts: {ema_fast: [101, 102, 103], ema_slow: [100, 100, 100]}\n      expect: short\n"
    ok("factory_draft", {"spec_yaml": spec_text("cross", signals=wrong)}, ian)
    broken = failed("backtest_run", run_args(world, version=2), ian)
    assert broken["error_code"] == "bad_input" and "tests_failed" in broken["message"]
    with db.read() as u:
        assert u.session.scalar("SELECT COUNT(*) FROM backtest_runs") == 0                              # nothing ran


def test_after_three_failed_attempts_the_strategy_is_closed_until_the_root_cause_is_written(world):
    ian = caller(researcher.AGENT_SLUG, 4, 4)
    ok("factory_draft", {"spec_yaml": spec_text("cross")}, ian)
    for attempt in range(3):
        answer = ok("backtest_run", run_args(world, params={"fast": 7, "slow": 20}), ian)               # a fast of 7 makes the strategy raise: a failed run
        assert answer["status"] == "failed", attempt
    closed = failed("backtest_run", run_args(world), ian)
    assert closed["error_code"] == "attempt_budget_spent" and "root cause" in closed["message"]
    for other in ("backtest_optimize", "backtest_walkforward"):
        extra = {"space": SPACE, "method": "grid", "n_trials": 15, "objective": "net_pnl"} if other == "backtest_optimize" else \
            {"space": SPACE, "scheme": "rolling", "train": 250, "test": 250, "purge": 2, "embargo": 1}
        assert failed(other, run_args(world, **extra), ian)["error_code"] == "attempt_budget_spent"
    again = failed("factory_draft", {"spec_yaml": spec_text("cross", hypothesis="Same idea with another wording.")}, ian)
    assert again["error_code"] == "attempt_budget_spent"                                                 # a new version needs the cause first
    cause = "A fast average of 7 bars is rejected by the strategy itself, so every point with it fails; the space must stop at 6."
    version2 = ok("factory_draft", {"spec_yaml": spec_text("cross", hypothesis="Same idea with another wording."), "root_cause": cause}, ian)
    assert version2["version"] == 2
    assert ok("backtest_run", run_args(world, version=2), ian)["status"] == "done"                      # the count starts again from the version with the cause


def test_he_is_offered_no_tool_of_a_desk_or_an_order_and_the_guard_refuses_one_even_if_it_is_asked_for(world):
    assert set(offered_tools()) == {*researcher.ENGINE_TOOLS, *researcher.TOOLS} and len(offered_tools()) == 42
    assert not [t for t in offered_tools() if re.search(r"exec|order|desk|broker|submit|approve|flatten|premium|account|position", t)]
    assert set(offered_tools()) - set(researcher.ENGINE_TOOLS) <= set(hokma_tools.TOOLS)                                               # and every tool he is offered exists
    guard = ResearchRun(handlers=backtest_tools._HANDLERS)
    for name in ("order_place", "exec_propose", "desk_list", "premium_calibrate", "task_create", "gmail_send"):
        with pytest.raises(ToolNotAllowed):
            guard.tool(name)
    assert guard.ledger.status(1) is None


def test_the_loop_of_ian_scores_with_the_evaluator_of_optimize_so_a_trial_is_counted_once_and_a_changed_judge_is_refused(world, db):
    spec = build_spec(run_args(world))
    judge = research_loop.Evaluator(spec, "net_pnl", train_end=spec.start)
    loop = research_loop.ResearchLoop(judge, backtest_tools.loop_score("net_pnl"), hypothesis_id="h-cross", params={"fast": 5, "slow": 20})
    loop.baseline()
    for change in ({"fast": 4}, {"slow": 25}, {"fast": 6}):
        loop.experiment(change)
    assert len(loop.history) == 4 and loop.history[0].decision == "keep"
    with db.read() as u:
        assert u.session.scalar("SELECT COUNT(*) FROM backtest_trials WHERE strategy_id = 'cross'") == 4 == trials.count("cross", hypothesis_id="h-cross")
        assert u.session.scalar("SELECT COUNT(*) FROM backtest_runs WHERE kind = 'run'") == 0                # the score recorded nothing: one trial per experiment
    other = research_loop.Evaluator(replace(spec, capital_usd=spec.capital_usd * 2), "net_pnl", train_end=spec.start)
    with pytest.raises(researcher.EvaluatorChanged) as moved:
        loop.experiment({"fast": 3}, evaluator=other)
    assert backtest_tools.classify(moved.value).code == "evaluator_changed"


def test_a_score_that_goes_through_backtest_run_would_count_every_experiment_twice(world, db):
    """The reason for `loop_score`: `backtest.run` writes its own trial, so a loop that used it would double the n_trials of the deflated Sharpe ratio."""
    spec = build_spec(run_args(world))

    def through_run(spec_):
        done = backtest_tools.services().run(spec_)
        return research_loop.LoopEvaluation(float(done.realized_pnl_usd), run_id=done.run_id, spec_hash=spec_.fingerprint())

    loop = research_loop.ResearchLoop(research_loop.Evaluator(spec, "net_pnl", train_end=spec.start), through_run, hypothesis_id="h-wrong", params={"fast": 5, "slow": 20})
    loop.baseline()
    loop.experiment({"fast": 4})
    assert trials.count("cross") == 4                                                                        # two experiments, two runs, four rows

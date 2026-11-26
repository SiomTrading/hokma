"""Ian's safety code (SPEC-020: SKL-001, SKL-003, criterion 7): the report, the claim guard, the budget of attempts and the guarded tools.

A fake model stands in for the engine: it answers with the text a model would write, and the guard reads that text."""
import json

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.factory import researcher, spec
from hokma.factory.researcher import (AttemptBudget, AttemptBudgetSpent, BadReport, ClaimRejected, MissingTests, ResearchRun, RunLedger, ToolNotAllowed,
                                      TestsFailed, claim_guard, parse_report)
from hokma.llm.protocol import LLMReply
from hokma.quant.errors import BadInput


class FakeLLM:
    model = "fake-model"

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, messages, *, schema=None, max_tokens=4096):
        self.prompts.append(messages[-1]["content"])
        return LLMReply(text=self.replies.pop(0), model=self.model)


def report(**over):
    base = {"hypothesis": "A fast average above a slow one means the move continues.",
            "spec": {"strategy_id": "ema_demo", "version": 1, "spec_hash": "a" * 64}, "runs": [], "evidence": [], "risks": ["costs were not stress tested"],
            "next_step": "run the walk-forward", "status": "in_progress", "claims": []}
    return json.dumps({**base, **over})


def says(**over):
    return parse_report(report(**over))


def started(*run_ids, tests=()):
    ledger = RunLedger()
    for run_id in run_ids:
        ledger.start(run_id)
    for spec_hash in tests:
        ledger.tests_passed(spec_hash)
    return ledger


def codes(verdict):
    return [p.code for p in verdict.problems]


# ---------------------------------------------------------------------------------------------------- the report
def test_a_report_is_one_strict_json_object():
    parsed = parse_report("Here it is:\n" + report(runs=[{"run_id": 4, "kind": "run"}]))
    assert parsed.spec.version == 1 and parsed.runs[0].run_id == 4 and parsed.status == "in_progress"
    assert parsed.to_dict()["risks"] == ("costs were not stress tested",)
    for bad in ("no json at all", report(status="done"), report(runs=[{"run_id": 0, "kind": "run"}]), report(runs=[{"run_id": True, "kind": "run"}]),
                report(extra_field=1), report(hypothesis=""), report(claims=[{"kind": "magic"}]), report(runs=[{"run_id": 1}]),
                report(risks="one risk"), report(evidence=[{"claim": "x", "run_id": "7"}]), report(runs=[{"run_id": 1, "kind": "run"}] * 51)):
        with pytest.raises(BadReport) as caught:
            parse_report(bad)
        assert caught.value.code == "bad_report"


# ---------------------------------------------------------------------------------------------------- SKL-001
def test_a_claim_that_a_backtest_finished_without_a_run_id_does_not_validate_SKL_001():
    llm = FakeLLM(report(next_step="The backtest finished and the Sharpe ratio looks good."))
    out = parse_report(llm.complete([{"role": "user", "content": "go"}]).text)
    verdict = claim_guard(out, started(11), status_of={11: "done"}.get)
    assert not verdict.valid and codes(verdict) == ["unbacked_claim"]
    with pytest.raises(ClaimRejected) as caught:
        verdict.raise_for_problems()
    assert caught.value.code == "unbacked_claim"
    for text in ("the backtest, with no errors, is finished", "the optimization is done", "Walk-forward completed with five windows", "We ran the backtest", "the run is ready"):
        assert not claim_guard(says(next_step=text), started(11), status_of={11: "done"}.get).valid, text


def test_a_claim_with_a_run_id_done_in_this_session_validates_SKL_001():
    ok = says(next_step="The backtest finished.", runs=[{"run_id": 11, "kind": "run"}], claims=[{"kind": "backtest_done", "run_id": 11}],
              evidence=[{"claim": "Sharpe ratio 0.9 after costs", "value": 0.9, "run_id": 11}])
    assert claim_guard(ok, started(11), status_of={11: "done"}.get).valid


def test_a_stale_run_id_does_not_validate_even_if_that_run_is_done_SKL_001():
    old = says(next_step="The backtest finished.", runs=[{"run_id": 7, "kind": "run"}], claims=[{"kind": "backtest_done", "run_id": 7}])
    verdict = claim_guard(old, started(11), status_of={7: "done", 11: "done"}.get)
    assert not verdict.valid and "stale_run_id" in codes(verdict)
    only_claim = says(claims=[{"kind": "backtest_done", "run_id": 7}])
    assert {"stale_run_id", "unbacked_claim"} <= set(codes(claim_guard(only_claim, started(11), status_of={7: "done"}.get)))


def test_a_run_of_this_session_that_is_not_done_does_not_validate_SKL_001():
    running = says(next_step="The backtest finished.", runs=[{"run_id": 11, "kind": "run"}])
    verdict = claim_guard(running, started(11), status_of={11: "running"}.get)
    assert "run_not_done" in codes(verdict) and "unbacked_claim" in codes(verdict)
    assert "run_not_done" in codes(claim_guard(running, started(11), status_of={}.get))


def test_negated_and_plain_statements_claim_nothing_SKL_001():
    for text in ("The backtest did not finish: the run failed.", "No run is done yet.", "Tests failed on the second case.", "Next: write the spec."):
        assert claim_guard(says(next_step=text), started(), status_of={}.get).valid, text


def test_tests_pass_needs_the_tests_to_have_run_in_this_session_SKL_001():
    h = "a" * 64
    talk = says(next_step="All the tests pass.")
    assert codes(claim_guard(talk, started(), status_of={}.get)) == ["unbacked_claim"]
    assert claim_guard(talk, started(tests=[h]), status_of={}.get).valid
    typed = says(claims=[{"kind": "tests_pass", "spec_hash": "b" * 64}])
    assert codes(claim_guard(typed, started(tests=[h]), status_of={}.get)) == ["unbacked_claim"]
    assert claim_guard(says(claims=[{"kind": "tests_pass", "spec_hash": h}]), started(tests=[h]), status_of={}.get).valid


def test_a_number_needs_its_run_or_its_module_SKL_001():
    bare = says(evidence=[{"claim": "Sharpe ratio is 1.4", "value": 1.4}])
    assert codes(claim_guard(bare, started(), status_of={}.get)) == ["number_without_source"]
    assert codes(claim_guard(says(evidence=[{"claim": "it has 40 trades"}]), started(), status_of={}.get)) == ["number_without_source"]
    sourced = says(evidence=[{"claim": "deflated Sharpe 0.97", "value": 0.97, "module": "validation.dsr"}, {"claim": "costs are charged"}])
    assert claim_guard(sourced, started(), status_of={}.get).valid


def test_the_default_status_reader_uses_the_table_of_runs(db):
    with db.write() as u:
        done = u.session.insert("backtest_runs", {"kind": "run", "strategy_id": "s", "spec_json": "{}", "spec_hash": "h", "engine_version": "e", "seed": 0, "status": "done"})
    assert researcher.db_status_of(done, db=db) == "done" and researcher.db_status_of(done + 5, db=db) is None


# ---------------------------------------------------------------------------------------------------- SKL-003
def test_three_failed_attempts_block_the_strategy_and_the_fourth_is_refused_SKL_003():
    failures = {"s": 0}
    budget = AttemptBudget(lambda s, since: failures[s])
    for done in (0, 1, 2):
        failures["s"] = done
        budget.check("s")                                                     # attempts one, two and three are allowed
    failures["s"] = 3
    with pytest.raises(AttemptBudgetSpent, match="root cause") as caught:
        budget.check("s")
    assert caught.value.code == "attempt_budget_spent" and budget.spent("s")
    failures["t"] = 0
    budget.check("t")                                                         # another strategy has its own budget


def test_an_absurd_result_counts_as_a_failed_attempt_too_SKL_003():
    budget = AttemptBudget(lambda s, since: 2)
    budget.check("s")
    budget.fail("s", 1)
    assert budget.attempts("s") == 3
    with pytest.raises(AttemptBudgetSpent):
        budget.check("s")
    with pytest.raises(ValueError):
        AttemptBudget(limit=0)


def test_when_the_attempts_are_spent_the_output_must_carry_the_root_cause_SKL_003():
    budget = AttemptBudget(lambda s, since: 3)
    plain = says(next_step="Try again with a wider stop.")
    verdict = claim_guard(plain, started(), status_of={}.get, budget=budget)
    assert codes(verdict) == ["root_cause_required"]
    short = says(status="blocked", root_cause="bad data")
    assert codes(claim_guard(short, started(), status_of={}.get, budget=budget)) == ["root_cause_required"]
    cause = "The bars of the contract stop at the roll date, so the strategy never sees a bar after it and every run ends with no trades."
    assert claim_guard(says(status="blocked", root_cause=cause), started(), status_of={}.get, budget=budget).valid
    assert codes(claim_guard(says(status="blocked"), started(), status_of={}.get)) == ["root_cause_required"]
    assert codes(claim_guard(says(status="blocked", spec=None), started(), status_of={}.get)) == ["root_cause_required"]


def add_run(db, version, status, strategy_id="s"):
    with db.write() as u:
        u.session.insert("backtest_runs", {"kind": "run", "strategy_id": strategy_id, "strategy_version": version, "spec_json": "{}", "spec_hash": "h",
                                           "engine_version": "e", "seed": 0, "status": status})


def test_failed_runs_are_counted_in_the_table_of_runs_per_strategy_across_versions(db):
    for status in ("failed", "failed", "done", "account_blown"):
        add_run(db, 1, status)
    add_run(db, 2, "failed")
    assert researcher.failed_runs("s", db=db) == 3 and researcher.failed_runs("s", 4, db=db) == 1 and researcher.failed_runs("other", db=db) == 0
    budget = AttemptBudget(db=db)
    assert budget.attempts("s") == 3 and budget.spent("s")


SPEC_TEXT = """strategy_id: ema_demo
line: DT
alpha: ema_cross
params: {fast: 3, slow: 5}
hypothesis: A fast average above a slow one means the move continues.
tests:
  signal:
    - {name: fast_above_slow_is_long, facts: {ema_fast: [101, 102, 103], ema_slow: [100, 100, 100]}, expect: long}
    - {name: fast_below_slow_is_short, facts: {ema_fast: [99, 98, 97], ema_slow: [100, 100, 100]}, expect: short}
  position_size:
    - {name: risk_500, contract_id: "FUT:NQ:2025-03", asset_type: future, multiplier: 20, kind: risk_usd, value: 500, price: 20000, equity: 100000, stop_distance: 5, expect_qty: 5}
"""


def session(db, outcomes):
    """Handlers over the real tables: each backtest_run adds a row with the next outcome (done or failed)."""
    queue = list(outcomes)
    budget = AttemptBudget(db=db)

    def backtest_run(strategy_id, version, **_):
        status = queue.pop(0)
        with db.write() as u:
            run_id = u.session.insert("backtest_runs", {"kind": "run", "strategy_id": strategy_id, "strategy_version": version, "spec_json": "{}",
                                                        "spec_hash": "h", "engine_version": "e", "seed": 0, "status": status})
        return {"run_id": run_id, "status": status}

    handlers = {"factory_draft": lambda text, root_cause=None: spec.save_draft(text, root_cause=root_cause, budget=budget, db=db).__dict__, "backtest_run": backtest_run,
                "factory_propose": lambda strategy_id, version: spec.propose(strategy_id, version, db=db).__dict__}
    return ResearchRun(handlers, budget=budget,
                       status_of=lambda run_id: researcher.db_status_of(run_id, db=db), gate=lambda s, v: spec.gate_backtest(s, v, db=db),
                       latest=lambda s: spec.load(s, db=db).version)


def test_ian_turns_a_hypothesis_into_a_spec_runs_it_and_proposes_it_with_a_fresh_run_id(db):
    run = session(db, ["done"])
    saved = run.tool("factory_draft", text=SPEC_TEXT)
    outcome = run.tool("backtest_run", strategy_id="ema_demo", version=saved["version"])
    assert outcome["status"] == "done"
    final = report(spec={"strategy_id": "ema_demo", "version": 1, "spec_hash": saved["spec_hash"]}, status="proposed",
                   runs=[{"run_id": outcome["run_id"], "kind": "run"}], claims=[{"kind": "backtest_done", "run_id": outcome["run_id"]}, {"kind": "tests_pass", "spec_hash": saved["spec_hash"]}],
                   evidence=[{"claim": "the run ended with status done", "run_id": outcome["run_id"]}], next_step="The backtest finished; the judge of the line can read the spec.")
    llm = FakeLLM(final)
    accepted = run.accept(llm.complete([{"role": "user", "content": "report"}]).text)
    assert accepted.status == "proposed"
    assert run.tool("factory_propose", strategy_id="ema_demo", version=1)["status"] == "proposed"


def test_ian_declares_nothing_finished_without_a_fresh_run_id_in_the_session(db):
    run = session(db, [])
    saved = run.tool("factory_draft", text=SPEC_TEXT)
    with db.write() as u:                                               # a finished run of an earlier session, in the table
        old = u.session.insert("backtest_runs", {"kind": "run", "strategy_id": "ema_demo", "strategy_version": 1, "spec_json": "{}", "spec_hash": "h",
                                                 "engine_version": "e", "seed": 0, "status": "done"})
    llm = FakeLLM(report(next_step="The backtest finished.", runs=[{"run_id": old, "kind": "run"}], claims=[{"kind": "backtest_done", "run_id": old}]),
                  report(next_step="The backtest finished."))
    for _ in range(2):
        with pytest.raises(ClaimRejected):
            run.accept(llm.complete([{"role": "user", "content": "report"}]).text)
    assert saved["status"] == "draft"


def test_ian_stops_after_three_failed_attempts_and_the_output_demands_the_root_cause(db):
    run = session(db, ["failed", "failed", "failed"])
    saved = run.tool("factory_draft", text=SPEC_TEXT)
    for _ in range(3):
        assert run.tool("backtest_run", strategy_id="ema_demo", version=saved["version"])["status"] == "failed"
    with pytest.raises(AttemptBudgetSpent) as caught:
        run.tool("backtest_run", strategy_id="ema_demo", version=saved["version"])
    assert caught.value.code == "attempt_budget_spent"
    base = {"spec": {"strategy_id": "ema_demo", "version": 1, "spec_hash": saved["spec_hash"]}, "status": "blocked"}
    llm = FakeLLM(report(**base, next_step="Try another stop."),
                  report(**base, next_step="Stop: three failures.", root_cause="The bars of the contract end at the roll date, so the strategy never sees a bar after it and every run fails."))
    with pytest.raises(ClaimRejected) as no_cause:
        run.accept(llm.complete([{"role": "user", "content": "report"}]).text)
    assert no_cause.value.code == "root_cause_required"
    assert run.accept(llm.complete([{"role": "user", "content": "report"}]).text).status == "blocked"


def test_a_spec_without_tests_never_reaches_the_engine_and_is_never_proposed(db):
    run = session(db, ["done"])
    bare = SPEC_TEXT.split("tests:")[0].replace("ema_demo", "bare")
    saved = run.tool("factory_draft", text=bare)
    with pytest.raises(MissingTests):
        run.tool("backtest_run", strategy_id="bare", version=saved["version"])
    with pytest.raises(MissingTests):
        run.tool("factory_propose", strategy_id="bare", version=saved["version"])
    with db.read() as u:
        assert u.session.scalar("SELECT COUNT(*) FROM backtest_runs") == 0
    wrong = SPEC_TEXT.replace("expect: short", "expect: long").replace("ema_demo", "wrong")
    wrong_saved = run.tool("factory_draft", text=wrong)
    with pytest.raises(TestsFailed):
        run.tool("backtest_run", strategy_id="wrong", version=wrong_saved["version"])


def test_a_new_version_cannot_reset_the_budget_without_a_root_cause_SKL_003(db):
    cause = "The bars of the contract stop at the roll date, so every run of version one ended without a single trade."
    run = session(db, [])
    first = run.tool("factory_draft", text=SPEC_TEXT)
    for _ in range(3):
        add_run(db, 1, "failed", "ema_demo")
    v2 = SPEC_TEXT.replace("slow: 5", "slow: 6")
    for bad in (None, "", "   ", "too short", " " * 30):
        with pytest.raises((AttemptBudgetSpent, BadReport)):
            run.tool("factory_draft", text=v2, **({} if bad is None else {"root_cause": bad}))
    assert spec.load("ema_demo", db=db).version == first["version"] == 1          # nothing was saved
    saved = run.tool("factory_draft", text=v2, root_cause=cause)
    assert saved["version"] == 2
    assert researcher.budget_start("ema_demo", db=db) == 3                        # the window opened after run 3, the last one of the table
    assert researcher.failed_runs("ema_demo", db=db) == 3                         # the old failures stay in the ledger
    assert run.budget.attempts("ema_demo") == 0 and not run.budget.spent("ema_demo")
    for _ in range(3):
        add_run(db, 2, "failed", "ema_demo")
    assert run.budget.attempts("ema_demo") == 3 and researcher.failed_runs("ema_demo", db=db) == 6
    with pytest.raises(AttemptBudgetSpent):
        run.tool("factory_draft", text=SPEC_TEXT.replace("slow: 5", "slow: 7"))
    again = AttemptBudget(db=db)                                                  # the counts live in the database
    assert again.attempts("ema_demo") == 3 and again.spent("ema_demo")
    with pytest.raises(AttemptBudgetSpent):
        again.check("ema_demo")
    assert run.tool("factory_draft", text=SPEC_TEXT.replace("slow: 5", "slow: 7"), root_cause=cause + " Version two failed the same way.")["version"] == 3
    assert AttemptBudget(db=db).attempts("ema_demo") == 0


def test_a_reported_absurd_result_blocks_the_next_version_and_the_claim_guard_follows_the_window(db):
    cause = "The result was absurd because the fills used a price that was never traded in the bars of the test."
    run = session(db, [])
    run.tool("factory_draft", text=SPEC_TEXT)
    for _ in range(3):
        run.budget.fail("ema_demo", 1)
    with pytest.raises(AttemptBudgetSpent):
        run.tool("factory_draft", text=SPEC_TEXT.replace("slow: 5", "slow: 6"))
    ref = {"strategy_id": "ema_demo", "version": 1, "spec_hash": "h"}
    assert codes(claim_guard(parse_report(report(spec=ref, next_step="Try again.")), started(), status_of={}.get, budget=run.budget)) == ["root_cause_required"]
    assert run.tool("factory_draft", text=SPEC_TEXT.replace("slow: 5", "slow: 6"), root_cause=cause)["version"] == 2
    assert claim_guard(parse_report(report(spec={**ref, "version": 2}, next_step="Try again.")), started(), status_of={}.get, budget=run.budget).valid


def test_only_the_latest_version_runs_and_a_late_failure_of_an_old_version_counts_in_the_new_window_SKL_003(db):
    cause = "The bars of the contract stop at the roll date, so every run of version one ended without a single trade."
    run = session(db, ["failed"])
    run.handlers = {**run.handlers, **{n: (lambda **_: {}) for n in ("backtest_optimize", "backtest_walkforward", "backtest_montecarlo", "backtest_evidence")}}
    run.tool("factory_draft", text=SPEC_TEXT)
    for _ in range(3):
        add_run(db, 1, "failed", "ema_demo")
    assert run.budget.spent("ema_demo")
    assert run.tool("factory_draft", text=SPEC_TEXT.replace("slow: 5", "slow: 6"), root_cause=cause)["version"] == 2
    assert run.budget.attempts("ema_demo") == 0
    for name in ("backtest_run", "backtest_optimize", "backtest_walkforward", "backtest_montecarlo", "backtest_evidence"):
        with pytest.raises(researcher.StaleVersion) as stale:
            run.tool(name, strategy_id="ema_demo", version=1)
        assert stale.value.code == "stale_version"
    assert run.budget.attempts("ema_demo") == 0 and run.ledger.status(1) is None       # nothing ran
    add_run(db, 1, "failed", "ema_demo")                                              # the runner executed version one directly and it failed
    assert run.budget.attempts("ema_demo") == 1
    add_run(db, 2, "failed", "ema_demo")
    add_run(db, 1, "failed", "ema_demo")
    assert run.budget.spent("ema_demo") and researcher.failed_runs("ema_demo", researcher.budget_start("ema_demo", db=db), db=db) == 3

"""The research loop and the stall check (SPEC-020: SKL-008, CTS-006). Histories are built by hand; the numbers are chosen so the answer is clear."""
import ast
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.backtest.spec import BacktestSpec, CostModel
from hokma.desks.core import hashing
from hokma.factory import research_loop as rl
from hokma.factory.params import Param, ParamSchema
from hokma.factory.research_loop import (Evaluator, EvaluatorChanged, Experiment, LoopError, LoopEvaluation, ResearchLoop, StallLimits, detect_stall,
                                         pivot_proposals)

T0, T1 = datetime(2025, 1, 2, tzinfo=timezone.utc), datetime(2025, 2, 3, tzinfo=timezone.utc)
TRAIN_END = datetime(2025, 1, 1, tzinfo=timezone.utc)
SCHEMA = ParamSchema((Param("fast", "int", 10, 1, 100), Param("slow", "int", 30, 2, 200)))


def make(**extra) -> BacktestSpec:
    fields = dict(strategy_id="s1", strategy_version=1, params={"fast": 10, "slow": 30}, line="DT", instruments=("FUT:NQ:2025-03",), bar="1m", start=T0,
                  end=T1, as_of=T1, capital_usd=Decimal("50000"), costs=CostModel("ibkr_futures"))
    return BacktestSpec(**{**fields, **extra})


def judge(spec=None) -> Evaluator:
    return Evaluator(spec or make(), "sharpe", TRAIN_END)


SCORES = {(10, 30): 0.50, (12, 30): 0.62, (12, 40): 0.60, (14, 30): 0.70}


def scorer(calls=None, **over):
    def evaluate(spec):
        if calls is not None:
            calls.append(dict(spec.params))
        return LoopEvaluation(SCORES[(spec.params["fast"], spec.params["slow"])], run_id=None, spec_hash=spec.fingerprint(), **over)
    return evaluate


def stored(db):
    with db.read() as u:
        rows = u.session.query("SELECT strategy_id, hypothesis_id, params_json, status, objective FROM backtest_trials ORDER BY id")
    return [{**r, "params": json.loads(r["params_json"])} for r in rows]


# ---------------------------------------------------------------------------------------------------- the judge
def test_the_judge_runs_out_of_sample_and_its_hash_ignores_only_the_parameters():
    assert judge().hash == judge(make(params={"fast": 99, "slow": 3})).hash
    for other in (make(end=datetime(2025, 3, 1, tzinfo=timezone.utc), as_of=datetime(2025, 3, 1, tzinfo=timezone.utc)), make(seed=1), make(capital_usd=Decimal("60000")),
                  make(costs=CostModel("ibkr_futures", slippage_ticks=2), fill=replace(make().fill, next_open_ticks=2))):
        assert judge(other).hash != judge().hash
    assert Evaluator(make(), "calmar", TRAIN_END).hash != judge().hash
    with pytest.raises(LoopError, match="out of sample") as caught:
        Evaluator(make(), "sharpe", datetime(2025, 1, 20, tzinfo=timezone.utc))
    assert caught.value.code == "bad_evaluator"
    with pytest.raises(LoopError, match="time zone"):
        Evaluator(make(), "sharpe", datetime(2025, 1, 1))
    with pytest.raises(LoopError, match="objective"):
        Evaluator(make(), "", TRAIN_END)


def test_the_loop_uses_the_hash_function_of_the_desks_package_and_the_trial_ledger_of_the_backtest(db, monkeypatch):
    assert rl.content_hash is hashing.content_hash
    from hokma.backtest import trials

    seen = []
    monkeypatch.setattr(trials, "record", lambda *a, **k: seen.append((a, k)) or 1)
    rl._TrialLedger(db).record("s1", "h", None, {"a": 1}, "done", 0.5)
    assert seen == [(("s1", "h", None, {"a": 1}, "done", 0.5), {"error": None, "db": db})]


# ---------------------------------------------------------------------------------------------------- SKL-008
def test_one_change_per_experiment_with_keep_or_discard_each_recorded_in_the_trials_SKL_008(db):
    calls = []
    loop = ResearchLoop(judge(), scorer(calls), hypothesis_id="h1", params={"fast": 10, "slow": 30}, schema=SCHEMA, db=db)
    base = loop.baseline()
    assert (base.n, base.decision, base.objective, loop.best_objective) == (1, "keep", 0.50, 0.50)
    one = loop.experiment({"fast": 12})
    assert (one.decision, loop.kept_params, loop.best_objective, one.change) == ("keep", {"fast": 12, "slow": 30}, 0.62, {"fast": 12})
    two = loop.experiment({"slow": 40})                                           # 0.60 does not beat 0.62
    assert (two.decision, loop.kept_params, loop.best_objective) == ("discard", {"fast": 12, "slow": 30}, 0.62)
    three = loop.experiment({"fast": 14})                                         # built from the kept parameters, not from the discarded ones
    assert (three.decision, loop.kept_params) == ("keep", {"fast": 14, "slow": 30})
    assert calls == [{"fast": 10, "slow": 30}, {"fast": 12, "slow": 30}, {"fast": 12, "slow": 40}, {"fast": 14, "slow": 30}]
    rows = stored(db)
    assert [(r["params"]["_decision"], r["params"]["_experiment"], r["status"], r["objective"]) for r in rows] == [
        ("keep", 1, "done", 0.50), ("keep", 2, "done", 0.62), ("discard", 3, "done", 0.60), ("keep", 4, "done", 0.70)]
    assert {r["params"]["_evaluator"] for r in rows} == {loop.evaluator_hash[:16]} and {r["hypothesis_id"] for r in rows} == {"h1"}
    from hokma.backtest import trials

    assert trials.count("s1", db=db) == 4                                         # the discarded ones count for the deflated Sharpe ratio too


def test_an_experiment_changes_exactly_one_known_parameter_to_a_new_value_SKL_008(db):
    loop = ResearchLoop(judge(), scorer(), hypothesis_id="h1", params={"fast": 10, "slow": 30}, schema=SCHEMA, db=db)
    with pytest.raises(LoopError, match="baseline first") as caught:
        loop.experiment({"fast": 12})
    assert caught.value.code == "bad_change"
    loop.baseline()
    for change, code in (({"fast": 12, "slow": 40}, "one_change_only"), ({}, "one_change_only"), ({"nope": 1}, "bad_change"), ({"fast": 10}, "bad_change")):
        with pytest.raises(LoopError) as caught:
            loop.experiment(change)
        assert caught.value.code == code
    with pytest.raises(LoopError, match="first experiment"):
        loop.baseline()
    with pytest.raises(Exception, match="fast"):                                  # the schema of the strategy still holds: 500 is above 100
        loop.experiment({"fast": 500})
    assert len(stored(db)) == 1 and len(loop.history) == 1


def test_changing_the_evaluator_in_the_middle_of_the_loop_fails_SKL_008(db):
    loop = ResearchLoop(judge(), scorer(), hypothesis_id="h1", params={"fast": 10, "slow": 30}, schema=SCHEMA, db=db)
    loop.baseline()
    assert loop.experiment({"fast": 12}, evaluator=judge()).decision == "keep"       # the same judge is fine
    moved = judge(make(end=datetime(2025, 3, 1, tzinfo=timezone.utc), as_of=datetime(2025, 3, 1, tzinfo=timezone.utc)))
    with pytest.raises(EvaluatorChanged) as caught:
        loop.experiment({"slow": 40}, evaluator=moved)
    assert caught.value.code == "evaluator_changed" and len(stored(db)) == 2
    with pytest.raises(EvaluatorChanged):
        loop.experiment({"slow": 40}, evaluator=Evaluator(make(), "calmar", TRAIN_END))
    assert loop.kept_params == {"fast": 12, "slow": 30}


def test_a_run_that_answers_for_another_spec_is_an_evaluator_change_and_is_not_recorded_SKL_008(db):
    def tampered(spec):
        return LoopEvaluation(0.9, spec_hash=replace(spec, seed=5).fingerprint())

    loop = ResearchLoop(judge(), tampered, hypothesis_id="h1", params={"fast": 10, "slow": 30}, db=db)
    with pytest.raises(EvaluatorChanged):
        loop.baseline()
    assert stored(db) == [] and loop.history == []


def test_a_failed_run_is_a_trial_and_is_discarded_SKL_008(db):
    def flaky(spec):
        if spec.params["fast"] == 12:
            raise ValueError("no bars for the range")
        if spec.params["fast"] == 14:
            return None
        return LoopEvaluation(SCORES[(10, 30)], spec_hash=spec.fingerprint())

    loop = ResearchLoop(judge(), flaky, hypothesis_id="h1", params={"fast": 10, "slow": 30}, db=db)
    loop.baseline()
    crashed, empty = loop.experiment({"fast": 12}), loop.experiment({"fast": 14})
    assert (crashed.status, crashed.decision, crashed.error) == ("failed", "discard", "ValueError: no bars for the range")
    assert (empty.status, empty.decision, empty.error) == ("failed", "discard", "no_objective") and loop.kept_params == {"fast": 10, "slow": 30}
    rows = stored(db)
    assert [r["status"] for r in rows] == ["done", "failed", "failed"] and rows[1]["params"]["_error"].startswith("ValueError")
    assert rows[2]["objective"] is None


def test_the_loop_has_a_budget_of_experiments_and_a_minimum_gain(db):
    loop = ResearchLoop(judge(), scorer(), hypothesis_id="h1", params={"fast": 10, "slow": 30}, max_experiments=2, min_gain=0.2, db=db)
    loop.baseline()
    assert loop.experiment({"fast": 12}).decision == "discard"                      # 0.62 - 0.50 is below the gain of 0.2
    with pytest.raises(LoopError, match="2 experiments") as caught:
        loop.experiment({"fast": 14})
    assert caught.value.code == "loop_budget_spent"
    with pytest.raises(Exception):
        ResearchLoop(judge(), scorer(), hypothesis_id="h1", params={}, max_experiments=10**7, db=db)
    with pytest.raises(LoopError):
        ResearchLoop(judge(), scorer(), hypothesis_id="h1", params={}, min_gain=-1, db=db)


# ---------------------------------------------------------------------------------------------------- CTS-006
def exp(n, objective, **ev) -> Experiment:
    evaluation = LoopEvaluation(objective, **{"in_sample_objective": None if objective is None else objective + 0.1, "profit_factor": 1.6, "max_drawdown": 0.10, **ev})
    return Experiment(n, {}, {}, evaluation, "done" if objective is not None else "failed", "keep", n)


PLATEAU = [0.80, 0.82, 0.81, 0.83, 0.82, 0.83, 0.82, 0.83, 0.82, 0.83]
RISING = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00]


def history(objectives, **ev):
    return [exp(i + 1, o, **ev) for i, o in enumerate(objectives)]


def test_ten_experiments_on_a_plateau_are_a_plateau_CTS_006():
    assert detect_stall(history(PLATEAU)) == "plateau"
    assert detect_stall(history(RISING)) is None                                     # the second half beats the first by 0.5
    assert detect_stall(history(PLATEAU)[:9]) is None                                # fewer than ten is not a stall
    assert detect_stall(history([0.5] * 4 + PLATEAU[-6:])) == "plateau"
    assert detect_stall(history(PLATEAU), StallLimits(window=10, plateau_gain=0.0)) is None   # a gain of exactly zero is not below zero


def test_overfit_costs_profit_factor_and_tail_are_found_CTS_006():
    assert detect_stall(history(RISING, in_sample_objective=None)) is None
    assert detect_stall([exp(i + 1, o, in_sample_objective=o + 0.9) for i, o in enumerate(RISING)]) == "overfit"          # mean gap 0.9 against 0.5
    assert detect_stall([exp(i + 1, o, in_sample_objective=o + 0.4) for i, o in enumerate(RISING)]) is None               # a gap of 0.4 is allowed
    warned = [exp(i + 1, o, warnings=("costs_kill_edge",) if i < 6 else ()) for i, o in enumerate(RISING)]
    assert detect_stall(warned) == "costs_kill_edge"                                 # 6 of 10 carry the warning of the cost model (P-07)
    half = [exp(i + 1, o, warnings=("costs_kill_edge",) if i < 5 else ()) for i, o in enumerate(RISING)]
    assert detect_stall(half) is None                                                # 5 of 10 is not most
    assert detect_stall(history(RISING, profit_factor=1.05)) == "low_profit_factor"
    assert detect_stall(history(RISING, profit_factor=1.2)) is None                  # the limit itself is allowed
    assert detect_stall(history(RISING, max_drawdown=0.45)) == "tail_risk"
    assert detect_stall(history(RISING, max_drawdown=-0.45)) == "tail_risk"
    assert detect_stall(history(RISING, max_drawdown=0.30)) is None


def test_the_stalls_are_judged_in_a_fixed_order_and_failed_experiments_are_not_read_CTS_006():
    worst = history(PLATEAU, max_drawdown=0.5, profit_factor=1.0, in_sample_objective=2.0, warnings=("costs_kill_edge",))
    assert detect_stall(worst) == "tail_risk"
    assert detect_stall(history(PLATEAU, profit_factor=1.0, in_sample_objective=2.0, warnings=("costs_kill_edge",))) == "costs_kill_edge"
    assert detect_stall(history(PLATEAU, profit_factor=1.0, in_sample_objective=2.0)) == "overfit"
    assert detect_stall(history(PLATEAU, profit_factor=1.0)) == "low_profit_factor"
    assert rl.STALL_KINDS == ("tail_risk", "costs_kill_edge", "overfit", "low_profit_factor", "plateau")
    failed = [exp(100 + i, None) for i in range(5)]
    assert detect_stall(failed + history(PLATEAU)[:9]) is None                       # five failures do not fill the window
    assert detect_stall(failed + history(PLATEAU)) == "plateau"


def test_a_stall_proposes_changes_of_skeleton_never_another_parameter_tweak_CTS_006():
    assert rl.PIVOT_KINDS == ("invert_assumption", "change_archetype", "redefine_objective")
    for stall in rl.STALL_KINDS:
        proposals = pivot_proposals(stall)
        assert [p.change for p in proposals] == list(rl.PIVOT_KINDS)
        assert all(p.idea.endswith(".") and "parameter value" not in p.idea.lower() for p in proposals)
    assert "reversal" in pivot_proposals("low_profit_factor")[0].idea or "winners" in pivot_proposals("low_profit_factor")[0].idea
    with pytest.raises(LoopError):
        pivot_proposals("tune_more")


def test_a_stalled_loop_refuses_one_more_parameter_experiment_and_names_the_pivots_CTS_006(db):
    values = iter(PLATEAU)

    def evaluate(spec):
        return LoopEvaluation(next(values), in_sample_objective=0.9, profit_factor=1.6, max_drawdown=0.1, spec_hash=spec.fingerprint())

    loop = ResearchLoop(judge(), evaluate, hypothesis_id="h1", params={"fast": 10}, db=db)
    loop.baseline()
    for step in range(1, 9):
        assert loop.review().stall is None
        loop.experiment({"fast": 10 + step})
    assert loop.review().stall is None                                               # nine scored experiments
    loop.experiment({"fast": 19})
    review = loop.review()
    assert review.stall == "plateau" and [p.change for p in review.proposals] == list(rl.PIVOT_KINDS)
    with pytest.raises(LoopError, match="invert_assumption; change_archetype; redefine_objective") as caught:
        loop.experiment({"fast": 20})
    assert caught.value.code == "loop_stalled" and len(stored(db)) == 10


# ---------------------------------------------------------------------------------------------------- no path to real money, no git
SOURCES = [Path(rl.__file__).with_name(n) for n in ("research_loop.py", "researcher.py", "spec.py", "ideas.py")]
FORBIDDEN_IMPORTS = ("hokma.exec", "hokma.broker", "hokma.desks.trading", "hokma.desks.swo", "hokma.desks.service", "hokma.kit", "hokma.schwab",
                      "hokma.connections", "subprocess", "git", "os", "socket", "ctypes")
ALLOWED = ("hokma.kit.integrity",)                                                   # only the folder of the kit
ALLOWED_DESKS = ("hokma.desks.core.signal", "hokma.desks.core.hashing")


def imports(path):
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_the_research_code_has_no_path_to_a_desk_a_broker_or_git_SKL_008(path):
    for name in imports(path):
        assert name in ALLOWED or not any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN_IMPORTS), (path.name, name)
        if name.startswith("hokma.desks"):
            assert name in ALLOWED_DESKS, (path.name, name)
    source = path.read_text(encoding="utf-8")
    for word in ("subprocess", "os.system", "reset --hard", "git ", "submit(", "order_place", "approve("):
        assert word not in source, (path.name, word)

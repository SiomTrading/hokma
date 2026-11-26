"""Search of parameters (SPEC-020, 4.5): B-26, B-28, B-45, L-41 and the engine run of a point. Expected values are worked out by hand in the comments."""
import json
from decimal import Decimal as D
from pathlib import Path

import pandas as pd
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from backtest import search_support as ss
from backtest.search_support import FakeLedger
from backtest import support
from backtest.support import requires_engine
from hokma.backtest import optimize
from hokma.backtest.optimize import (Axis, BadSpace, BudgetTooSmall, Constraint, Evaluation, ParamSpace, SearchError, deflated, edge_of_space, refine,
                                     trial_warning)
from hokma.backtest.spec import SpecError
from hokma.quant import limits, validation
from hokma.quant.errors import BadInput

RETURNS = Path(__file__).resolve().parents[1] / "quant" / "fixtures" / "returns_daily.csv"


def space_ab(hi=3):
    return ParamSpace((Axis("a", "int", 1, hi), Axis("b", "int", 1, hi)))


def bowl(p):
    """Maximum 0 at a=2, b=3."""
    return -((p["a"] - 2) ** 2) - ((p["b"] - 3) ** 2)


def returns():
    frame = pd.read_csv(RETURNS, index_col=0)
    return frame.iloc[:, 0]


# ---------------------------------------------------------------------------------------------------------------- B-26
def test_a_3_by_3_grid_runs_nine_points_and_each_one_is_in_the_ledger_B_26():
    ledger, calls = FakeLedger(), []
    result = optimize.search(support.spec(strategy_id="alpha"), space_ab(), method="grid", n_trials=9, objective="bowl", presweep=False,
                               evaluate=lambda p: calls.append(dict(p)) or bowl(p), ledger=ledger)
    assert len(calls) == 9 and len({tuple(c.values()) for c in calls}) == 9
    assert len(ledger.rows) == 9 and all(r[0] == "alpha" and r[4] == "done" for r in ledger.rows)
    # bowl is 0 only at (2, 3): the best of the nine
    assert result.best.params == {"a": 2, "b": 3} and result.best.objective == 0.0
    assert result.n_trials == 9 and result.warnings == ("edge_of_space:b",)      # b = 3 is the last value of its range of 1 to 3


def test_the_deflated_sharpe_of_the_winner_uses_the_count_of_the_ledger_B_26():
    ledger = FakeLedger()
    result = optimize.search(support.spec(strategy_id="alpha"), space_ab(), method="grid", n_trials=9, objective="bowl", presweep=False, evaluate=bowl,
                               ledger=ledger)
    series = returns()
    assert deflated(result, series, sr_std=0.1).value == validation.dsr(series, n_trials=9, sr_std=0.1).value
    assert deflated(result, series, sr_std=0.1).value != validation.dsr(series, n_trials=2, sr_std=0.1).value
    with pytest.raises(SearchError, match="sr_std_needed"):
        deflated(result, series)


def test_tpe_with_a_fixed_seed_gives_the_same_sequence_twice_and_a_cache_answers_repeated_points_B_26():
    space = ParamSpace((Axis("a", "int", 0, 20), Axis("b", "int", 0, 20)))

    def f(p):
        return -((p["a"] - 7) ** 2) - ((p["b"] - 12) ** 2)

    def run(seed):
        ledger = FakeLedger()
        spec = support.spec(strategy_id="alpha", seed=seed)
        return optimize.search(spec, space, method="tpe", n_trials=40, objective="f", presweep=False, evaluate=f, ledger=ledger), ledger

    (a, la), (b, lb) = run(7), run(7)
    assert [t.params for t in a.trials] == [t.params for t in b.trials] and la.rows == lb.rows
    assert [t.params for t in run(8)[0].trials] != [t.params for t in a.trials]
    assert len(a.trials) + a.cache_hits == 40 and len(la.rows) == len(a.trials)       # a repeated proposal is a hit, not a new trial
    assert a.n_trials == len(a.trials) and len({tuple(t.params.values()) for t in a.trials}) == len(a.trials)


def test_tpe_never_runs_a_point_that_breaks_a_constraint_B_45():
    space = ParamSpace((Axis("fast", "int", 5, 30, 5), Axis("slow", "int", 10, 60, 10)), (Constraint("fast", "lt", "slow"),))
    seen = []
    optimize.search(support.spec(), space, method="tpe", n_trials=25, objective="x", presweep=False, ledger=FakeLedger(),
                      evaluate=lambda p: seen.append(dict(p)) or float(p["slow"] - p["fast"]))
    assert seen and all(p["fast"] < p["slow"] for p in seen)


def test_with_the_real_ledger_every_combination_lands_in_backtest_trials_and_the_count_is_the_deflation_input_B_26(db):
    from hokma.backtest import trials

    def evaluate(p):
        if p["a"] == 3 and p["b"] == 1:
            raise SpecError("bad_spec: no bars")
        return bowl(p)
    result = optimize.search(support.spec(strategy_id="alpha"), space_ab(), method="grid", n_trials=9, objective="bowl", presweep=False, evaluate=evaluate,
                               hypothesis_id="h1")
    with db.read() as u:
        rows = u.session.query("SELECT strategy_id, hypothesis_id, params_json, status FROM backtest_trials ORDER BY id")
    assert len(rows) == 9 and {r["hypothesis_id"] for r in rows} == {"h1"} and [r["status"] for r in rows].count("failed") == 1
    assert trials.count("alpha") == 9 == result.n_trials and trials.count("alpha", hypothesis_id="h1") == 9
    (only,) = trials.failed("alpha")
    assert dict(only.params) == {"a": 3, "b": 1} and "no bars" in only.error                      # the failed point keeps its error
    assert deflated(result, returns(), sr_std=0.1).value == validation.dsr(returns(), n_trials=trials.count("alpha"), sr_std=0.1).value


# ---------------------------------------------------------------------------------------------------------------- B-28
def test_301_combinations_raise_many_trials_and_a_penalized_dsr_and_10_do_not_B_28():
    series = returns()
    big, small = FakeLedger(), FakeLedger()
    wide = optimize.search(support.spec(strategy_id="alpha"), ParamSpace((Axis("x", "int", 1, 301),)), method="grid", n_trials=301, objective="x",
                             presweep=False, evaluate=lambda p: float(p["x"] % 17), ledger=big)
    narrow = optimize.search(support.spec(strategy_id="alpha"), ParamSpace((Axis("x", "int", 1, 10),)), method="grid", n_trials=10, objective="x",
                               presweep=False, evaluate=lambda p: float(p["x"] % 17), ledger=small)
    assert "many_trials:301" in wide.warnings and not any(w.startswith("many_trials") for w in narrow.warnings)
    assert wide.n_trials == 301 and narrow.n_trials == 10
    penalized, plain = deflated(wide, series, sr_std=0.1).value, deflated(narrow, series, sr_std=0.1).value
    assert penalized < plain                                                  # more trials, a lower deflated Sharpe on the same returns
    assert trial_warning(300) == () and trial_warning(301) == ("many_trials:301",)


# ---------------------------------------------------------------------------------------------------------------- B-45
def test_an_empty_grid_fails_with_bad_space_B_45():
    with pytest.raises(BadSpace):
        ParamSpace(())
    with pytest.raises(BadSpace):
        Axis("x", "int", 5, 1)                                                # low above high: no value
    with pytest.raises(BadSpace):
        Axis("x", "choice", choices=())
    with pytest.raises(BadSpace):
        Axis("x", "float", 0.0, 1.0)                                          # a float range needs its step
    with pytest.raises(BadSpace):
        ParamSpace((Axis("a", "int", 1, 3), Axis("a", "int", 1, 3)))
    with pytest.raises(BadSpace):
        ParamSpace((Axis("a", "int", 1, 3),), (Constraint("a", "lt", "zzz"),))
    impossible = ParamSpace((Axis("a", "int", 1, 3), Axis("b", "int", 1, 3)), (Constraint("a", "gt", 3),))
    with pytest.raises(BadSpace, match="no valid combination"):
        impossible.combinations()


def test_the_constraint_fast_below_slow_cuts_the_invalid_combinations_before_any_run_B_45():
    space = ParamSpace((Axis("fast", "int", 5, 15, 5), Axis("slow", "int", 10, 20, 5)), (Constraint("fast", "lt", "slow"),))
    # fast 5: slow 10, 15, 20 (3); fast 10: 15, 20 (2); fast 15: 20 (1); fast 10 with slow 10 and fast 15 with slow 10 or 15 are cut: 9 - 3 = 6
    assert len(space.combinations()) == 6 and space.size == 9
    seen, ledger = [], FakeLedger()
    result = optimize.search(support.spec(), space, method="grid", n_trials=9, objective="x", presweep=False, ledger=ledger,
                               evaluate=lambda p: seen.append(dict(p)) or 0.0)
    assert len(seen) == 6 and all(p["fast"] < p["slow"] for p in seen) and len(ledger.rows) == 6 and result.n_trials == 6


def test_a_single_point_grid_runs_once_and_a_short_budget_is_refused():
    one = ParamSpace((Axis("a", "int", 4, 4),))
    result = optimize.search(support.spec(), one, method="grid", n_trials=1, objective="x", presweep=False, evaluate=lambda p: 1.5, ledger=FakeLedger())
    assert len(result.trials) == 1 and result.best.params == {"a": 4}
    with pytest.raises(BudgetTooSmall):
        optimize.search(support.spec(), space_ab(), method="grid", n_trials=8, objective="x", presweep=False, evaluate=bowl, ledger=FakeLedger())


def test_the_sizes_of_a_search_are_capped_with_the_quant_limits():
    assert limits.PARAM_CAPS["n_combinations"] == 20_000 and limits.PARAM_CAPS["opt_trials"] == 5_000
    assert limits.PARAM_CAPS["top_k"] == 1_000 and limits.PARAM_CAPS["refine_steps"] == 100
    with pytest.raises(BadInput):
        Axis("x", "int", 1, 20_001)
    with pytest.raises(BadInput):
        ParamSpace((Axis("a", "int", 1, 200), Axis("b", "int", 1, 200)))       # 40000 points
    with pytest.raises(BadInput):
        optimize.search(support.spec(), space_ab(), method="tpe", n_trials=5_001, objective="x", presweep=False, evaluate=bowl, ledger=FakeLedger())
    with pytest.raises(BadInput):
        optimize.search(support.spec(), space_ab(), method="grid", n_trials=9, objective="x", presweep=False, evaluate=bowl, ledger=FakeLedger(), top_k=1_001)
    with pytest.raises(BadInput):
        refine(space_ab(), strategy_id="s", hypothesis_id="h", evaluate=bowl, objective="x", n_trials=9, ledger=FakeLedger(), steps=101)
    with pytest.raises(SearchError):
        optimize.search(support.spec(), space_ab(), method="random", n_trials=9, objective="x", evaluate=bowl, ledger=FakeLedger())


def test_a_failed_point_is_a_failed_trial_with_its_error_and_it_counts():
    def evaluate(p):
        if p["a"] == 2:
            raise SpecError("bad_spec: no bars")
        return bowl(p)
    ledger = FakeLedger()
    result = optimize.search(support.spec(strategy_id="alpha"), space_ab(), method="grid", n_trials=9, objective="bowl", presweep=False, evaluate=evaluate,
                               ledger=ledger)
    failed = [t for t in result.trials if t.status == "failed"]
    assert len(failed) == 3 and all("no bars" in t.error and t.objective is None for t in failed)      # a = 2 with b = 1, 2, 3
    assert [r[4] for r in ledger.rows].count("failed") == 3 and result.n_trials == 9 and "failed_trials:3" in result.warnings
    assert sorted(e for e in ledger.errors if e) == ["SpecError: bad_spec: no bars"] * 3                 # the ledger keeps the error of a failed trial
    assert result.best.params == {"a": 1, "b": 3} and result.best.objective == -1.0      # a = 2 is gone: (1, 3) gives -1


def test_an_objective_that_cannot_be_computed_is_a_failed_trial():
    result = optimize.search(support.spec(), space_ab(2), method="grid", n_trials=4, objective="x", presweep=False, ledger=FakeLedger(),
                               evaluate=lambda p: Evaluation(None if p["a"] == 1 else 1.0))
    assert [t.error for t in result.trials if t.status == "failed"] == ["no_objective", "no_objective"]


def test_the_ledger_is_required_and_is_the_trials_module_by_default(monkeypatch):
    from hokma.backtest import trials
    assert optimize.resolve_ledger(None) is trials and optimize.resolve_ledger(FakeLedger()) is not trials
    ss.hide_trials(monkeypatch)
    with pytest.raises(optimize.LedgerMissing):
        optimize.search(support.spec(), space_ab(), method="grid", n_trials=9, objective="x", presweep=False, evaluate=bowl)


# ---------------------------------------------------------------------------------------------------------------- edge of space
def test_an_optimum_on_the_border_of_the_range_is_flagged_edge_of_space():
    space = ParamSpace((Axis("fast", "int", 5, 25, 5), Axis("slow", "int", 391, 400)))
    result = optimize.search(support.spec(), space, method="grid", n_trials=50, objective="x", presweep=False, ledger=FakeLedger(),
                               evaluate=lambda p: float(p["slow"]) - abs(p["fast"] - 15))             # best: slow 400 (the edge), fast 15 (the middle)
    assert result.best.params == {"fast": 15, "slow": 400} and result.edge_of_space == ("slow",) and "edge_of_space:slow" in result.warnings
    assert edge_of_space(space, {"fast": 15, "slow": 395}) == ()
    two = ParamSpace((Axis("a", "int", 1, 2),))
    assert edge_of_space(two, {"a": 2}) == ()                                                          # two values are always borders: not judged


# ---------------------------------------------------------------------------------------------------------------- L-41
def test_refine_reaches_the_known_maximum_and_every_step_adds_to_the_count_L_41():
    space = ParamSpace((Axis("x", "int", 0, 100), Axis("y", "int", 0, 100)))
    ledger, counts = FakeLedger(), []

    def f(p):
        counts.append(len(ledger.rows))
        return -((p["x"] - 37) ** 2) - ((p["y"] - 62) ** 2)
    result = refine(space, strategy_id="alpha", hypothesis_id="h1", evaluate=f, objective="f", n_trials=400, ledger=ledger)
    # the coarse grid is 5 x 5 on 0, 25, 50, 75, 100; the steps then close in with strides 25, 12, 6, 3, 2, 1
    assert result.best.params == {"x": 37, "y": 62}                                  # less than one step from the maximum (distance 0)
    assert counts == list(range(len(counts))) and len(ledger.rows) == len(result.trials) == result.n_trials   # each run adds one row to the ledger
    assert len(result.trials) < 101 * 101 / 10 and result.cache_hits >= 0


def test_refine_through_optimize_respects_the_budget_and_stops_at_a_plateau():
    space = ParamSpace((Axis("x", "int", 0, 100), Axis("y", "int", 0, 100)))
    ledger = FakeLedger()
    result = optimize.search(support.spec(strategy_id="alpha"), space, method="refine", n_trials=30, objective="f", presweep=False, ledger=ledger,
                               evaluate=lambda p: -((p["x"] - 37) ** 2) - ((p["y"] - 62) ** 2))
    assert len(result.trials) <= 30 and len(ledger.rows) == len(result.trials)
    flat = optimize.search(support.spec(strategy_id="beta"), space, method="refine", n_trials=500, objective="f", presweep=False, ledger=FakeLedger(),
                             evaluate=lambda p: 1.0)
    # nothing improves: the 5 x 5 coarse grid, then the strides 25, 12, 6, 3, 1 (and 2) each try at most 4 new neighbours: at most 25 + 6 * 4 points
    assert len(flat.trials) <= 49


def test_refine_handles_a_choice_axis_by_trying_every_option():
    space = ParamSpace((Axis("k", "int", 0, 40), Axis("mode", "choice", choices=("a", "b", "c"))))
    result = refine(space, strategy_id="s", hypothesis_id="h", objective="f", n_trials=200, ledger=FakeLedger(),
                    evaluate=lambda p: -abs(p["k"] - 13) + {"a": 0, "b": 5, "c": 2}[p["mode"]])
    assert result.best.params == {"k": 13, "mode": "b"}


# ---------------------------------------------------------------------------------------------------------------- pre-sweep in front of the engine
def test_with_a_presweep_every_point_is_a_trial_and_only_the_top_k_reach_the_engine():
    space = ParamSpace((Axis("x", "int", 1, 30),))
    ledger, engine_calls = FakeLedger(), []
    fast = lambda points: [-abs(p["x"] - 10.4) for p in points]                    # the fast ranking: closest to 10.4 first: 10, 11, 9, 12, 8
    result = optimize.search(support.spec(strategy_id="alpha"), space, method="grid", n_trials=30, objective="x", presweep=True, fast=fast,
                               evaluate=lambda p: engine_calls.append(p["x"]) or -abs(p["x"] - 11), ledger=ledger, top_k=5)
    assert engine_calls == [10, 11, 9, 12, 8]                                       # the top 5 by the fast score, best first
    assert len(ledger.rows) == 35 and result.n_trials == 35                         # 30 swept points plus 5 confirmations: all of them count
    assert [t.source for t in result.trials].count("fast") == 30 and [t.source for t in result.trials].count("engine") == 5
    assert result.best.source == "engine" and result.best.params == {"x": 11} and "presweep_top_k:5" in result.warnings   # the official one is the engine's


def test_presweep_without_a_fast_scorer_says_so_and_runs_on_the_engine():
    result = optimize.search(support.spec(), space_ab(2), method="grid", n_trials=4, objective="x", presweep=True, evaluate=bowl, ledger=FakeLedger())
    assert "presweep_not_available" in result.warnings and all(t.source == "engine" for t in result.trials)


def test_tpe_and_refine_can_search_on_the_fast_score_and_confirm_the_best():
    space = ParamSpace((Axis("x", "int", 0, 50),))
    for method in ("tpe", "refine"):
        ledger = FakeLedger()
        result = optimize.search(support.spec(strategy_id="alpha"), space, method=method, n_trials=25, objective="x", presweep=True, ledger=ledger,
                                   fast=lambda pts: [-abs(p["x"] - 20) for p in pts], evaluate=lambda p: -abs(p["x"] - 20) * 2, top_k=3)
        assert 1 <= len([t for t in result.trials if t.source == "engine"]) <= 3 and result.best.source == "engine"
        assert result.n_trials == len(ledger.rows) == len(result.trials)


# ---------------------------------------------------------------------------------------------------------------- saved optimizations
def test_an_optimization_is_saved_and_read_back_equal(db):
    spec = support.spec(strategy_id="alpha")
    result = optimize.search(spec, space_ab(), method="grid", n_trials=9, objective="bowl", presweep=False, evaluate=bowl, ledger=FakeLedger())
    opt_id = optimize.save(db, spec, result, engine_version="engine-1.0")
    back = optimize.load(db, opt_id)
    assert back == result and json.dumps(back.to_dict(), sort_keys=True) == json.dumps(result.to_dict(), sort_keys=True)
    with db.read() as u:
        row = u.session.one("SELECT kind, status, strategy_id FROM backtest_runs WHERE id = :i", {"i": opt_id})
    assert (row["kind"], row["status"], row["strategy_id"]) == ("optimize", "done", "alpha")
    with pytest.raises(SearchError, match="bad_run"):
        optimize.load(db, opt_id + 100)


# ---------------------------------------------------------------------------------------------------------------- the engine is run in one place
def test_the_engine_is_started_from_run_one_only_and_no_third_party_library_is_imported_here():
    source = (Path(optimize.__file__)).read_text(encoding="utf-8")
    assert source.count("runner.run_with(") == 1 and "def run_one" in source and "engine.run(" not in source
    modules = {m for _, m in support.imports_of(source)}
    assert not any(support.is_engine_module(m) for m in modules) and not any(m.split(".")[0] in ("optuna", "scipy", "sklearn") for m in modules)


@requires_engine
def test_one_engine_run_per_point_gives_the_objective_of_that_run():
    bars = ss.chained_bars(400)
    spec = support.spec(strategy_id="alpha")

    def build(params):
        return optimize.EngineStrategy(ss.cross_decide(ss.cross_signal(ss.arrays_of(bars), params["fast"], params["slow"])), support.TICK, support.MULT, "NQZ5")
    evaluate = optimize.engine_evaluator(spec, build, bars, "net_pnl")
    space = ParamSpace((Axis("fast", "int", 5, 10, 5), Axis("slow", "int", 30, 40, 10)))
    result = optimize.search(spec, space, method="grid", n_trials=4, objective="net_pnl", presweep=False, evaluate=evaluate, ledger=FakeLedger())
    for t in result.trials:
        direct = ss.engine_run(bars, t.params["fast"], t.params["slow"])
        assert t.status == "done" and t.objective == float(direct.realized_pnl_usd)
    assert max(t.objective for t in result.trials) == result.best.objective
    sharpe = optimize.engine_evaluator(spec, build, bars, "sharpe")({"fast": 5, "slow": 30})
    assert sharpe.objective is None or isinstance(sharpe.objective, float)
    with pytest.raises(SearchError):
        optimize.engine_evaluator(spec, build, bars, "profit")


def test_an_evaluation_that_went_through_a_saved_run_is_not_recorded_a_second_time_B_26():
    """`runner.run` records its own trial; a search whose points are scored by it must not add a second row for the same point (the deflated Sharpe
    would count it twice). The points that name no run are recorded by the search, once."""
    ledger = FakeLedger()
    saved = []

    def through_the_runner(p):
        saved.append(dict(p))
        ledger.record("alpha", "alpha", len(saved), p, "done", bowl(p))        # what runner.run does for the run of this point
        return Evaluation(bowl(p), run_id=len(saved))
    result = optimize.search(support.spec(strategy_id="alpha"), space_ab(), method="grid", n_trials=9, objective="bowl", presweep=False, evaluate=through_the_runner,
                             ledger=ledger)
    assert len(saved) == 9 and len(ledger.rows) == 9 == result.n_trials                    # nine runs, nine rows: not eighteen
    plain = FakeLedger()
    optimize.search(support.spec(strategy_id="alpha"), space_ab(), method="grid", n_trials=9, objective="bowl", presweep=False, evaluate=bowl, ledger=plain)
    assert len(plain.rows) == 9

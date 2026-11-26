"""The public entry points of the backtest service (SPEC-020, section 4.2): their signatures, `run(spec)` alone, `optimize` and its ledger, the
mapping of `walk_forward` to the splitter of H1, `monte_carlo`, `evidence` and `status`. The strategy is a moving average cross of the factory on the
real moves of the NQ fixture; every expected number is worked out in the comments."""
import inspect
import json
import subprocess
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest
from backtest import api_support as api
from backtest.runner_support import NQ, Fixed, source_of, spec
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma import backtest
from hokma.backtest import _lib, engine, queue, trials, walkforward
from hokma.backtest.optimize import Axis, Constraint, ParamSpace
from hokma.backtest.runner import Services

BACKEND = Path(__file__).resolve().parents[2]


def rows(db, sql, **params):
    with db.read() as u:
        return u.session.query(sql, params)


def space():
    return ParamSpace((Axis("fast", "int", 3, 5), Axis("slow", "int", 10, 30, 10)), (Constraint("fast", "lt", "slow"),))


# ---------------------------------------------------------------------------------------------------------------- the signatures of 4.2
def test_the_public_signatures_are_the_ones_of_the_spec():
    def shape(function):
        return [(p.name, p.kind.name, p.default is inspect.Parameter.empty) for p in inspect.signature(function).parameters.values() if p.name != "services"]

    kw = "KEYWORD_ONLY"
    assert shape(backtest.run) == [("spec", "POSITIONAL_OR_KEYWORD", True)]
    assert shape(backtest.walk_forward) == [("spec", "POSITIONAL_OR_KEYWORD", True), ("space", "POSITIONAL_OR_KEYWORD", True), ("scheme", kw, True),
                                           ("train", kw, True), ("test", kw, True), ("purge", kw, True), ("embargo", kw, True)]
    assert shape(backtest.monte_carlo) == [("run_id", "POSITIONAL_OR_KEYWORD", True), ("n", kw, True), ("method", kw, True), ("seed", kw, True)]
    assert shape(backtest.evidence) == [("strategy_id", "POSITIONAL_OR_KEYWORD", True), ("version", "POSITIONAL_OR_KEYWORD", True)]
    assert [(p.name, p.kind.name) for p in inspect.signature(backtest.status).parameters.values() if p.name != "db"] == [("job_id", "POSITIONAL_OR_KEYWORD")]
    from hokma.backtest import optimize

    assert shape(optimize.optimize) == [("spec", "POSITIONAL_OR_KEYWORD", True), ("space", "POSITIONAL_OR_KEYWORD", True), ("method", kw, True),
                                       ("n_trials", kw, True), ("objective", kw, True), ("presweep", kw, False)]
    assert inspect.signature(optimize.optimize).parameters["presweep"].default is True


# ---------------------------------------------------------------------------------------------------------------- one name for the module and the function
@pytest.mark.parametrize("code", [
    "from hokma.backtest import optimize",
    "import hokma.backtest.optimize as optimize",
    "import hokma.backtest\noptimize = hokma.backtest.optimize",
    "import hokma.backtest.optimize\nfrom hokma.backtest import optimize",
    "import importlib\noptimize = importlib.import_module('hokma.backtest.optimize')",
    "from hokma.backtest import *\nimport hokma.backtest.optimize",
])
def test_the_module_and_the_function_are_one_callable_object_whichever_way_it_is_reached(code):
    """Each case is a fresh interpreter: the order of the imports is the whole point."""
    probe = ("from hokma.core import processguard; processguard._role = processguard.SERVER\n" + code + "\n"
             "import sys\nm = sys.modules['hokma.backtest.optimize']\n"
             "assert optimize is m and callable(optimize) and hasattr(optimize, 'Axis') and hasattr(optimize, 'search')\n"
             "import hokma.backtest\nassert hokma.backtest.optimize is m and getattr(m, 'optimize').__name__ == 'optimize'\n"
             "import inspect\nassert 'presweep' in inspect.signature(m.optimize).parameters\nprint('ok')")
    done = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr[-800:]


def test_the_module_answers_the_call_of_its_function(monkeypatch):
    from hokma.backtest import optimize

    seen = []
    monkeypatch.setitem(optimize.__dict__, "optimize", lambda *a, **k: seen.append((a, k)) or "called")
    assert optimize(1, 2, key=3) == "called" and seen == [((1, 2), {"key": 3})]


# ---------------------------------------------------------------------------------------------------------------- run(spec)
@requires_engine
def test_run_of_a_spec_alone_loads_the_strategy_of_the_registry_and_the_bars_of_the_source(db, tmp_path):
    bars = api.series(600)
    result = backtest.run(api.spec_of(bars), services=api.services(bars, db, tmp_path))
    assert result.status == "done" and result.trades and result.engine_version
    (saved,) = rows(db, "SELECT kind, strategy_id, strategy_version, status FROM backtest_runs")
    assert (saved["kind"], saved["strategy_id"], saved["strategy_version"], saved["status"]) == ("run", "cross", 1, "done")
    assert trials.count("cross", db=db) == 1                                      # the run is one trial
    same = backtest.run(api.spec_of(bars), services=api.services(bars, db, tmp_path / "again"))
    assert (same.realized_pnl_usd, same.mtm_pnl_usd, same.data_hash) == (result.realized_pnl_usd, result.mtm_pnl_usd, result.data_hash)


@requires_engine
def test_run_of_an_unknown_id_is_a_failed_run_of_the_signal_stage_and_an_alpha_runs_in_a_composite(db, tmp_path):
    bars = api.series(100)
    unknown = backtest.run(api.spec_of(bars, strategy_id="nothing"), services=api.services(bars, db, tmp_path))
    assert unknown.status == "failed" and "failed_stage:signal" in unknown.warnings and "failed_code:unknown_strategy" in unknown.warnings
    alpha = backtest.run(api.spec_of(bars, strategy_id="ema_cross"), services=Services(db=db, source=source_of({NQ: bars}), curve_dir=tmp_path))
    assert alpha.status == "done" and "failed_code:not_a_strategy" not in alpha.warnings      # the composite of the factory (see test_composite_run)


@requires_engine
def test_the_service_builds_the_strategy_from_a_function_when_it_is_given_one(db, tmp_path):
    bars = api.series(300)
    got = backtest.run(api.spec_of(bars, params={}), services=Services(db=db, source=source_of({NQ: bars}), curve_dir=tmp_path,
                                                                strategy=lambda p: Fixed(enter_at=10, exit_at=20)))
    assert got.status == "done" and len(got.trades) == 1


# ---------------------------------------------------------------------------------------------------------------- optimize and the trial ledger
def count(db, strategy_id="cross"):
    return trials.count(strategy_id, db=db)


@requires_engine
def test_an_optimization_of_n_points_is_n_rows_of_the_ledger_and_no_run_records_a_second_one(db, tmp_path):
    bars = api.series(400)
    result = backtest.optimize(api.spec_of(bars), space(), method="grid", n_trials=9, objective="mtm_pnl", presweep=False, services=api.services(bars, db, tmp_path))
    # fast 3, 4, 5 against slow 10, 20, 30 with fast < slow: all nine are valid -> 9 points
    assert len(result.trials) == count(db) == result.n_trials                           # every executed point is one row, and no more
    assert [r["kind"] for r in rows(db, "SELECT kind FROM backtest_runs")] == ["optimize"]    # no per point run row: nothing went through runner.run
    assert all(t.source == "engine" for t in result.trials) and result.run_id is not None


@requires_engine
def test_a_point_that_fails_is_one_failed_row_and_a_repeated_point_is_not_a_new_trial(db, tmp_path):
    if not _lib.search_available():
        pytest.skip("the search library is not available on this machine: the TPE search answers search_unavailable")
    bars = api.series(300)
    small = ParamSpace((Axis("fast", "int", 6, 8), Axis("slow", "int", 20, 20)))              # 3 points; fast 7 raises inside the strategy
    result = backtest.optimize(api.spec_of(bars), small, method="tpe", n_trials=12, objective="mtm_pnl", presweep=False, services=api.services(bars, db, tmp_path))
    assert len(result.trials) == count(db) == 3 and result.cache_hits >= 9               # 12 proposals, 3 different points, 9 answered from the cache
    failed = trials.failed("cross", db=db)
    assert [f.params["fast"] for f in failed] == [7] and "fast 7" in failed[0].error and [t.status for t in result.trials].count("failed") == 1


@requires_engine
def test_with_the_presweep_the_fast_points_and_the_confirmed_ones_are_rows_each_once(db, tmp_path):
    bars = api.series(400)
    result = backtest.optimize(api.spec_of(bars), space(), method="grid", n_trials=9, objective="net_pnl", presweep=True, services=api.services(bars, db, tmp_path))
    fast = [t for t in result.trials if t.source == "fast"]
    engine_trials = [t for t in result.trials if t.source == "engine"]
    assert len(fast) == 9 and len(engine_trials) == 9 and result.presweep                 # top_k is 20: all nine are confirmed
    assert count(db) == len(result.trials) == 18 == result.n_trials                         # nine swept and nine run in the engine, none twice in a stage
    by_point = {}
    for r in rows(db, "SELECT params_json FROM backtest_trials"):
        by_point[r["params_json"]] = by_point.get(r["params_json"], 0) + 1
    assert set(by_point.values()) == {2}                                                  # once swept, once confirmed


@requires_engine
def test_a_strategy_with_stops_has_no_presweep_and_a_sharpe_objective_has_none_either(db, tmp_path):
    bars = api.series(300)
    one = ParamSpace((Axis("fast", "int", 3, 4), Axis("slow", "int", 20, 20)))
    stops = backtest.optimize(api.spec_of(bars, strategy_id="cross_stop"), one, method="grid", n_trials=2, objective="mtm_pnl", presweep=True,
                              services=api.services(bars, db, tmp_path))
    assert not stops.presweep and "presweep_not_available" in stops.warnings and {t.source for t in stops.trials} == {"engine"}
    sharpe = backtest.optimize(api.spec_of(bars), one, method="grid", n_trials=2, objective="sharpe", presweep=True, services=api.services(bars, db, tmp_path))
    assert not sharpe.presweep and "presweep_not_available" in sharpe.warnings
    assert count(db, "cross_stop") == 2 and count(db) == 2


@requires_engine
def test_the_best_point_of_the_engine_is_the_one_that_a_run_of_its_parameters_gives(db, tmp_path):
    bars = api.series(400)
    services = api.services(bars, db, tmp_path)
    result = backtest.optimize(api.spec_of(bars), space(), method="grid", n_trials=9, objective="mtm_pnl", presweep=False, services=services)
    best = backtest.run(api.spec_of(bars, params=result.best.params), services=services)
    assert float(best.mtm_pnl_usd) == pytest.approx(result.best.objective)              # the same engine, the same fills, the same figure
    assert count(db) == 10                                                              # nine points plus this one run


def test_the_engine_absent_answers_before_any_row(db, tmp_path, monkeypatch):
    monkeypatch.setattr(_lib, "_IMPORT_ERROR", ImportError("no wheel"))
    bars = api.series(50)
    with pytest.raises(_lib.EngineUnavailable):
        backtest.optimize(api.spec_of(bars), space(), method="grid", n_trials=9, objective="mtm_pnl", services=api.services(bars, db, tmp_path))
    assert rows(db, "SELECT * FROM backtest_runs") == [] and count(db) == 0


# ---------------------------------------------------------------------------------------------------------------- walk_forward
def test_train_and_test_in_bars_become_the_folds_of_the_splitter_of_h1():
    # n = 1000, test 100: 10 windows of 100, so n_folds = 9 and the splitter cuts 1000 // 10 = 100 bars: both windows are as asked
    assert walkforward.folds_for(1000, "rolling", 100, 100) == (9, None, ())
    assert walkforward.folds_for(1000, "anchored", 100, 100) == (9, None, ())
    # a train of 300 cannot be cut apart from the test by the splitter: it says so
    assert walkforward.folds_for(1000, "rolling", 300, 100) == (9, None, ("train_window_equalized:300->100",))
    # test 150: 1000 // 150 = 6 windows, n_folds 5, the splitter cuts 1000 // 6 = 166 bars
    assert walkforward.folds_for(1000, "rolling", 150, 150) == (5, None, ("test_window_equalized:150->166", "train_window_equalized:150->166"))
    # cpcv: 10 groups of 100 bars; train 600 leaves 10 - 6 = 4 test groups; a train of 100 or less is capped at 9 of the 10
    assert walkforward.folds_for(1000, "cpcv", 600, 100) == (10, 4, ("cpcv_groups:10x100:test_groups:4",))
    assert walkforward.folds_for(1000, "cpcv", 100, 100)[1] == 9 and walkforward.folds_for(1000, "cpcv", 50, 100)[1] == 9
    assert walkforward.folds_for(1000, "cpcv", 10_000, 100)[1] == 1                       # a train longer than the data leaves one test group
    for bad in ((1000, "rolling", 0, 100), (1000, "rolling", 100, 0), (250, "rolling", 100, 100), (1000, "weekly", 100, 100)):
        with pytest.raises(walkforward.WalkForwardError):
            walkforward.folds_for(*bad)
    from hokma.quant.errors import BadInput

    with pytest.raises(BadInput):
        walkforward.folds_for(1000, "rolling", 2_000_000, 100)                              # above the cap of the quant limits
    with pytest.raises(BadInput):
        walkforward.folds_for(1000, "rolling", 100, True)


@requires_engine
def test_a_rolling_walk_forward_of_the_space_uses_windows_of_the_bars_and_records_each_point_once(db, tmp_path):
    bars = api.series(600)
    services = api.services(bars, db, tmp_path)
    got = backtest.walk_forward(api.spec_of(bars), space(), scheme="rolling", train=100, test=100, purge=2, embargo=1, services=services)
    # 600 bars, windows of 100: n_folds = 5 and the splitter cuts 600 // 6 = 100 bars. Each window is a train of 100, a gap of purge + embargo = 3 and a
    # test of 100, and the next one starts 100 bars later: the tests begin at 103, 203, 303 and 403; the next would end at 603, past the data
    assert got.n_folds == 5 and len(got.windows) == 4 and got.warnings == () and got.leakage_free
    assert [w.test_blocks for w in got.windows] == [((103, 203),), ((203, 303),), ((303, 403),), ((403, 503),)]
    assert all(w.train_rows == 100 for w in got.windows)
    assert got.oos_rows is not None and len(got.oos_rows) == 400 and len(got.oos_pnl) == 400
    assert count(db) == 9 and got.run_id is not None                                      # nine points of the space, each executed once
    (saved,) = rows(db, "SELECT kind, status, data_hash FROM backtest_runs WHERE id = :i", i=got.run_id)
    assert saved["kind"] == "walkforward" and saved["status"] == "done" and saved["data_hash"]
    walked = backtest.walk_forward(api.spec_of(bars), space(), scheme="anchored", train=150, test=100, purge=2, embargo=1, services=services)
    assert "train_window_equalized:150->100" in walked.warnings and count(db) == 18       # another walk forward executes its points again: they are new runs


@requires_engine
def test_a_cpcv_walk_forward_and_the_size_checks(db, tmp_path):
    bars = api.series(600)
    services = api.services(bars, db, tmp_path)
    got = backtest.walk_forward(api.spec_of(bars), space(), scheme="cpcv", train=300, test=100, purge=2, embargo=1, services=services)
    # 6 groups of 100; train 300 leaves 6 - 3 = 3 test groups: C(6, 3) = 20 test sets
    assert got.scheme == "cpcv" and got.n_folds == 6 and len(got.windows) == 20 and got.oos_rows is None and "cpcv_groups:6x100:test_groups:3" in got.warnings
    from hokma.quant.errors import BadInput

    with pytest.raises(BadInput):
        backtest.walk_forward(api.spec_of(bars), space(), scheme="rolling", train=100, test=100, purge=10**7, embargo=1, services=services)
    with pytest.raises(walkforward.WalkForwardError):
        backtest.walk_forward(api.spec_of(bars), space(), scheme="rolling", train=100, test=400, purge=2, embargo=1, services=services)


@requires_engine
def test_a_walk_forward_of_a_strategy_with_stops_runs_the_engine_for_each_point(db, tmp_path):
    bars = api.series(300)
    one = ParamSpace((Axis("fast", "int", 3, 4), Axis("slow", "int", 20, 20)))
    got = backtest.walk_forward(api.spec_of(bars, strategy_id="cross_stop"), one, scheme="rolling", train=50, test=50, purge=1, embargo=1,
                                services=api.services(bars, db, tmp_path))
    assert len(got.windows) == 4 and count(db, "cross_stop") == 2 and got.leakage_free             # 300 bars, windows of 50: tests at 52, 102, 152 and 202


# ---------------------------------------------------------------------------------------------------------------- monte_carlo and evidence
@requires_engine
def test_the_evidence_of_a_strategy_has_the_real_trial_count_the_walk_forward_and_the_monte_carlo(db, tmp_path):
    bars = api.series(1500)
    services = api.services(bars, db, tmp_path)
    ran = backtest.run(api.spec_of(bars), services=services)
    assert ran.status == "done" and len(ran.trades) >= 3
    with pytest.raises(walkforward.WalkForwardError, match="no_walk_forward"):
        backtest.evidence("cross", 1, services=services)
    walked = backtest.walk_forward(api.spec_of(bars), space(), scheme="rolling", train=250, test=250, purge=2, embargo=1, services=services)
    carlo = backtest.monte_carlo(ran.run_id, n=200, method="placebo", seed=11, services=services)
    assert carlo.method == "placebo" and carlo.p_value is not None and carlo.figures["n_trades"] == len([t for t in ran.trades if t.exit_at is not None])
    got = backtest.evidence("cross", 1, services=services)
    assert count(db) == 1 + 9 == got.n_trials == got.parameters["ledger_trials"]            # the run and the nine points of the walk forward
    assert got.parameters["walk_forward_run"]["run_id"] == walked.run_id and got.parameters["monte_carlo"]["method"] == "placebo"
    assert got.parameters["strategy_version"] == 1 and "monte_carlo_missing" not in got.warnings
    assert got.parameters["ambiguous_bars"] == ran.ambiguous_bars and "cost_in_sharpe" in got.parameters
    json.dumps(got.parameters, allow_nan=False)                                          # plain values
    (child,) = rows(db, "SELECT kind, parent_id FROM backtest_runs WHERE kind = 'montecarlo'")
    assert child["parent_id"] == ran.run_id


@requires_engine
def test_the_evidence_refuses_what_it_cannot_rebuild_and_warns_of_a_missing_monte_carlo(db, tmp_path):
    bars = api.series(1500)
    services = api.services(bars, db, tmp_path)
    with pytest.raises(walkforward.WalkForwardError, match="no_run"):
        backtest.evidence("cross", 1, services=services)
    backtest.run(api.spec_of(bars), services=services)
    backtest.walk_forward(api.spec_of(bars), space(), scheme="rolling", train=250, test=250, purge=2, embargo=1, services=services)
    got = backtest.evidence("cross", 1, services=services)
    assert "monte_carlo_missing" in got.warnings and "monte_carlo" not in got.parameters
    with pytest.raises(walkforward.WalkForwardError, match="no_run"):
        backtest.evidence("cross", 2, services=services)                                  # another version has nothing
    other = api.series(1500, seed=2)
    with pytest.raises(walkforward.WalkForwardError, match="data_changed"):
        backtest.evidence("cross", 1, services=api.services(other, db, tmp_path))


@requires_engine
def test_monte_carlo_refuses_a_run_that_is_not_finished_or_whose_data_changed(db, tmp_path):
    bars = api.series(600)
    services = api.services(bars, db, tmp_path)
    from hokma.backtest.placebo import PlaceboError

    with pytest.raises(PlaceboError, match="bad_run"):
        backtest.monte_carlo(999, n=10, method="placebo", seed=1, services=services)
    ran = backtest.run(api.spec_of(bars), services=services)
    with pytest.raises(PlaceboError, match="the hash of the data changed"):
        backtest.monte_carlo(ran.run_id, n=10, method="placebo", seed=1, services=api.services(api.series(600, seed=3), db, tmp_path))


# ---------------------------------------------------------------------------------------------------------------- status
def test_status_is_the_state_of_a_job(db):
    job = queue.submit("replay", [{"n": 1}], db=db)
    state = backtest.status(job, db=db)
    assert state.job_id == job and state.state is queue.JobState.QUEUED
    assert engine.status()["available"] in (True, False) and set(engine.status()) == {"available", "code", "engine_version", "analyzer_hook"}
    with pytest.raises(queue.QueueError):
        backtest.status(job + 100, db=db)

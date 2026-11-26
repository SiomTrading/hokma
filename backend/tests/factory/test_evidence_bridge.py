"""The bridge from what H4 saved to the facts of the audit (SPEC-032, OPT-08 to OPT-11): the table of 4.3, the schemes of 2.3, the Sharpe in and out of
sample, the neighbors that run once and count in the ledger, and the regimes. The strategies are saved specs of the factory and every run is a real run of H4
over the moves of the NQ fixture, laid on several days so that a Sharpe exists (see `multi_day_bars`)."""
import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from backtest import api_support as api
from backtest import search_support as ss
from backtest.runner_support import NQ, source_of
from backtest.support import requires_engine
from factory.test_spec import text

from hokma import backtest
from hokma.backtest import results, runner, trials, walkforward
from hokma.backtest.optimize import Axis, Constraint, ParamSpace
from hokma.backtest.runner import Services
from hokma.backtest.ticks import Bar
from hokma.factory import audit
from hokma.factory import evidence_bridge as eb
from hokma.factory import spec as factory_spec
from hokma.factory import thresholds as th
from hokma.factory.evidence_bridge import BridgeError
from hokma.factory.registry import Entry, Registry
from hokma.quant import metrics
from hokma.quant.metrics.returns import bars_per_year

pytestmark = requires_engine
START = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)
SPACE = ParamSpace((Axis("fast", "int", 3, 5), Axis("slow", "int", 10, 30, 10)), (Constraint("fast", "lt", "slow"),))
STRATEGIES = ("cross", "nowalk", "cpcvdemo")


def multi_day_bars(n: int = 600, per_day: int = 10, seed: int = 1) -> list[Bar]:
    """The bars of `chained_bars` (real moves of the NQ fixture) placed ten to a day on working days, so the run covers three months and has a daily Sharpe."""
    out = []
    for k, bar in enumerate(ss.chained_bars(n, seed)):
        day, minute = divmod(k, per_day)
        out.append(Bar(START + timedelta(days=day + 2 * (day // 5), minutes=minute), bar.open, bar.high, bar.low, bar.close, bar.volume))
    return out


class World:
    def __init__(self, db, services, bars):
        self.db, self.services, self.bars = db, services, bars
        self.thresholds = th.current("DT", db=db)
        self.config = th.to_config(self.thresholds.values)

    def inputs(self, strategy_id="cross", **kw):
        return eb.gather(strategy_id, 1, services=self.services, **kw)

    def spec(self, strategy_id="cross"):
        return api.spec_of(self.bars, strategy_id=strategy_id)

    def result_of(self, run_id):
        return walkforward.load_result(self.db, run_id, curve_dir=self.services.curves())

    def audit(self, inputs, evidence):
        return audit.audit(self.result_of(inputs.run_id), eb.base_spec(inputs, services=self.services), evidence, self.config)

    def rows(self, sql, **params):
        with self.db.read() as u:
            return u.session.query(sql, params)

    def evidence(self, inputs, **answers):
        return eb.build_evidence(inputs, thresholds=self.thresholds.values, answers=answers, services=self.services)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    from hokma.db import get_database, migrate

    base = tmp_path_factory.mktemp("bridge")
    patch = pytest.MonkeyPatch()
    for name, value in (("HOME", base / "home"), ("HOKMA_HOME", base / "work"), ("HOKMA_DATA_DIR", base / "data")):
        patch.setenv(name, str(value))
    patch.setenv("HOKMA_LANG", "en")
    migrate()
    db = get_database()
    bars = multi_day_bars()
    registry = Registry()
    for strategy_id in STRATEGIES:
        registry.add(Entry(strategy_id, "strategy", api.Cross, "DT", "", {"fast": 5, "slow": 20}, "test"))
    services = Services(db=db, registry=registry, source=source_of({NQ: bars}), curve_dir=base / "curves")
    for strategy_id in STRATEGIES:
        factory_spec.save_draft(text(strategy_id=strategy_id), db=db)
        assert services.run(api.spec_of(bars, strategy_id=strategy_id)).status == "done"
    backtest.walk_forward(api.spec_of(bars, strategy_id="cross"), SPACE, scheme="anchored", train=100, test=100, purge=2, embargo=1, services=services)
    backtest.walk_forward(api.spec_of(bars, strategy_id="cpcvdemo"), SPACE, scheme="cpcv", train=200, test=100, purge=2, embargo=1, services=services)
    backtest.optimize(api.spec_of(bars, strategy_id="cross"), SPACE, method="grid", n_trials=9, objective="sharpe", presweep=False, services=services)
    yield World(db, services, bars)
    patch.undo()


# ---- OPT-08: gather and build_evidence ---------------------------------------------------------------------------------------------------

def test_gather_finds_the_latest_saved_rows_and_the_bundle_of_h4(world):
    inputs = world.inputs()
    latest = {kind: world.rows("SELECT MAX(id) AS id FROM backtest_runs WHERE kind = :k AND strategy_id = 'cross'", k=kind)[0]["id"]
              for kind in ("run", "walkforward", "optimize")}
    assert (inputs.strategy_id, inputs.version, inputs.line) == ("cross", 1, "DT")
    assert (inputs.run_id, inputs.walkforward_id, inputs.optimize_id) == (latest["run"], latest["walkforward"], latest["optimize"])
    assert inputs.montecarlo_id is None and "monte_carlo_missing" in inputs.notes and "no_walk_forward" not in inputs.notes
    assert inputs.bundle is not None and inputs.bundle == walkforward.evidence("cross", 1, services=world.services)
    assert inputs.optimization is not None and inputs.optimization.strategy_id == "cross" and inputs.uses_regimes is False
    assert world.inputs(bundle=False).bundle is None


def test_without_a_walk_forward_the_bundle_is_none_the_note_says_so_and_what_depends_on_it_is_unknown(world):
    inputs = world.inputs("nowalk")
    assert inputs.bundle is None and inputs.walkforward_id is None and "no_walk_forward" in inputs.notes and "no_optimization" in inputs.notes
    evidence = world.evidence(inputs)
    assert (evidence.leak_checked, evidence.dsr, evidence.pbo, evidence.validation_scheme, evidence.multiple_testing_applied) == (None, None, None, None, None)
    assert (evidence.is_sharpe, evidence.oos_sharpe, evidence.cost_sharpe, evidence.gross_sharpe, evidence.edge_of_space) == (None, None, None, None, None)
    report = world.audit(inputs, evidence)
    by_number = {i.number: i for i in report.items}
    assert [by_number[n].status for n in (4, 15, 16, 18, 19)] == ["unknown", "unknown", "unknown", "unknown", "unknown"]


def test_build_evidence_fills_every_field_from_its_source_of_the_table_of_4_3(world):
    inputs = world.inputs()
    evidence = world.evidence(inputs, point_in_time=True, holdout_untouched=False)
    bundle, wf = inputs.bundle, inputs.bundle.parameters["walk_forward_run"]
    assert evidence.n_trials == trials.count("cross", db=world.db) and evidence.n_trials >= 9
    assert (evidence.point_in_time, evidence.holdout_untouched, evidence.survivorship_free, evidence.capacity_checked) == (True, False, None, None)
    assert (evidence.leak_checked, evidence.dsr, evidence.pbo) == (bundle.leakage_free, bundle.dsr, bundle.pbo) and evidence.multiple_testing_applied is True
    cost = bundle.parameters["cost_in_sharpe"]
    assert evidence.cost_sharpe == cost["cost_sharpe"] and evidence.gross_sharpe == cost["net_sharpe"] + cost["cost_sharpe"]
    assert evidence.edge_of_space == tuple(inputs.optimization.edge_of_space) and evidence.edge_of_space != ()
    assert evidence.uses_regimes is False and evidence.regime_sharpes is None and wf["scheme"] == "anchored"
    assert evidence.neighbor_sharpes and set(evidence.neighbor_sharpes) <= {n.label for n in audit.parameter_neighbors({"fast": 5, "slow": 20}, 0.1)}
    assert (evidence.is_sharpe, evidence.oos_sharpe) == eb.sample_sharpes(inputs, services=world.services)
    off = eb.build_evidence(inputs, thresholds={**world.thresholds.values, "edge_warns": False}, answers={}, services=world.services)
    assert off.edge_of_space is None
    with pytest.raises(BridgeError) as caught:
        world.evidence(inputs, nonsense=True)
    assert caught.value.code == "bad_input"
    with pytest.raises(BridgeError) as caught:
        world.evidence(dataclasses.replace(inputs, strategy_id="ghost"))                      # a name with no trial in the ledger
    assert caught.value.code == "bad_input" and "run a backtest first" in str(caught.value)


def test_with_an_anchored_walk_forward_the_check_of_the_scheme_passes(world):
    inputs = world.inputs()
    assert inputs.bundle.parameters["walk_forward_run"]["scheme"] == "anchored" and walkforward.SCHEMES["anchored"] == "expanding"
    evidence = world.evidence(inputs)
    assert evidence.validation_scheme == "walk_forward"
    check = {i.number: i for i in world.audit(inputs, evidence).items}[15]
    assert (check.key, check.status) == ("validation_scheme", "pass")


def test_every_scheme_of_the_walk_forward_becomes_one_the_audit_accepts_and_cpcv_stays_cpcv(world):
    assert {eb.VALIDATION[walkforward.SCHEMES[s]] for s in walkforward.SCHEMES} == set(audit.VALIDATION_SCHEMES)
    assert [eb.VALIDATION[walkforward.SCHEMES[s]] for s in ("rolling", "anchored", "cpcv")] == ["walk_forward", "walk_forward", "cpcv"]
    inputs = world.inputs("cpcvdemo")
    assert inputs.bundle.parameters["walk_forward_run"]["scheme"] == "cpcv"
    evidence = world.evidence(inputs)
    assert evidence.validation_scheme == "cpcv" and {i.number: i for i in world.audit(inputs, evidence).items}[15].status == "pass"


def test_gather_refuses_what_it_cannot_find_with_a_stable_code(world):
    for strategy_id, version, code in (("ghost", 1, "no_spec"), ("cross", 9, "no_spec"), ("", 1, "bad_input"), ("cross", 0, "bad_input"), ("cross", True, "bad_input")):
        with pytest.raises(BridgeError) as caught:
            eb.gather(strategy_id, version, services=world.services)
        assert caught.value.code == code and str(caught.value).startswith(f"{code}:")
    factory_spec.save_draft(text(strategy_id="unrun"), db=world.db)
    with pytest.raises(BridgeError) as caught:
        eb.gather("unrun", 1, services=world.services)
    assert caught.value.code == "no_run"


def test_the_line_of_a_version_comes_from_its_saved_spec_before_anything_runs(world):
    assert eb.line_of("cross", 1, services=world.services) == "DT"
    for strategy_id, version in (("ghost", 1), ("cross", 9)):
        with pytest.raises(BridgeError) as caught:
            eb.line_of(strategy_id, version, services=world.services)
        assert caught.value.code == "no_spec"


def test_data_that_changed_since_the_walk_forward_is_a_note_not_a_crash(world):
    shorter = Services(db=world.db, registry=world.services.registry, source=source_of({NQ: world.bars[:-1]}), curve_dir=world.services.curve_dir)
    inputs = eb.gather("cross", 1, services=shorter)
    assert inputs.bundle is None and "data_changed" in inputs.notes
    assert eb.sample_sharpes(dataclasses.replace(inputs, bundle=world.inputs().bundle), services=shorter) == (None, None)


# ---- OPT-09: the Sharpe in and out of sample ---------------------------------------------------------------------------------------------

def rebuilt(world, strategy_id):
    """The saved walk forward read again with the functions that wrote it, and the per bar pnl of its points."""
    inputs = world.inputs(strategy_id)
    row = world.rows("SELECT spec_json, summary_json FROM backtest_runs WHERE id = :i", i=inputs.walkforward_id)[0]
    spec, body = runner.spec_from_canonical(json.loads(row["spec_json"])), json.loads(row["summary_json"])
    pnl, _failed = walkforward.pnl_matrix(spec, world.services, world.services.load(spec), body["points"])
    saved = body["result"]
    args = dict(scheme=saved["scheme"], n_folds=saved["n_folds"], purge=saved["purge"], embargo=saved["embargo"], n_test_folds=body.get("n_test_folds"))
    return inputs, spec, pnl, walkforward.run(body["points"], pnl, objective=saved["objective"], **args), walkforward.windows(len(pnl), **args)


def sharpe_of(values, spec, world):
    ppy = bars_per_year("future", spec.bar)
    return metrics.compute(pd.Series(list(values)) / float(spec.capital_usd), ["sharpe"], periods_per_year=ppy)["sharpe"].value


def test_the_sharpe_out_of_sample_is_the_one_of_quant_metrics_over_the_laid_tests_and_in_sample_the_mean_of_the_train_windows(world):
    inputs, spec, pnl, again, folds = rebuilt(world, "cross")
    got_in, got_out = eb.sample_sharpes(inputs, services=world.services)
    assert again.oos_pnl is not None and got_out == pytest.approx(sharpe_of(again.oos_pnl, spec, world))
    per_window = [sharpe_of(pnl[fold.train, w.column], spec, world) for fold, w in zip(folds, again.windows)]
    assert got_in == pytest.approx(sum(per_window) / len(per_window))


def test_for_cpcv_the_tests_overlap_so_the_out_of_sample_sharpe_is_the_mean_of_the_sharpe_of_each_test_set(world):
    inputs, spec, pnl, again, folds = rebuilt(world, "cpcvdemo")
    assert again.oos_pnl is None
    got_in, got_out = eb.sample_sharpes(inputs, services=world.services)
    expected = [sharpe_of(pnl[fold.test, w.column], spec, world) for fold, w in zip(folds, again.windows)]
    assert got_out == pytest.approx(sum(expected) / len(expected)) and got_in is not None


def test_without_a_walk_forward_there_is_no_sample_sharpe(world):
    assert eb.sample_sharpes(world.inputs("nowalk"), services=world.services) == (None, None)


# ---- OPT-10: neighbors that run once and count --------------------------------------------------------------------------------------------

def ran(world, label):
    return world.rows("SELECT COUNT(*) AS n FROM backtest_trials WHERE strategy_id = 'cross' AND hypothesis_id = :h", h=label)[0]["n"]


def known_points(world):
    return {r["params_json"] for r in world.rows("SELECT t.params_json FROM backtest_trials t JOIN backtest_runs r ON r.id = t.run_id "
                                                 "WHERE t.strategy_id = 'cross' AND r.kind = 'run' AND r.status = 'done'")}


def points_of(spread):
    return {json.dumps(n.params, sort_keys=True, separators=(",", ":")) for n in audit.parameter_neighbors({"fast": 5, "slow": 20}, spread)}


def test_a_second_evaluation_adds_no_trial_and_a_new_spread_only_runs_the_points_that_were_never_run(world):
    inputs = world.inputs()
    before = trials.count("cross", db=world.db)
    new_first = points_of(0.2) - known_points(world)
    first = eb.neighbor_sharpes(inputs, spread=0.2, services=world.services)
    after_first = trials.count("cross", db=world.db)
    assert after_first - before == len(new_first) and set(first) <= {n.label for n in audit.parameter_neighbors({"fast": 5, "slow": 20}, 0.2)} and first
    assert eb.neighbor_sharpes(inputs, spread=0.2, services=world.services) == first
    assert trials.count("cross", db=world.db) == after_first                                   # the second evaluation counts nothing more
    new_second = points_of(0.3) - known_points(world)
    assert 0 < len(new_second) < len(points_of(0.3))                                           # some of the new neighbors were run before and are reused
    eb.neighbor_sharpes(inputs, spread=0.3, services=world.services)
    assert trials.count("cross", db=world.db) - after_first == len(new_second)
    assert ran(world, "cross:neighbors") >= len(new_first) + len(new_second)
    for row in world.rows("SELECT t.run_id AS run_id, r.parent_id AS parent_id FROM backtest_trials t JOIN backtest_runs r ON r.id = t.run_id "
                          "WHERE t.hypothesis_id = 'cross:neighbors'"):
        assert row["parent_id"] == inputs.run_id                                                # each one is a saved run derived from the judged run
    assert world.inputs().run_id == inputs.run_id                                           # a neighbor never becomes the run that is judged


def test_the_sharpe_of_a_neighbor_is_the_one_of_its_saved_run(world):
    inputs = world.inputs()
    got = eb.neighbor_sharpes(inputs, spread=0.2, services=world.services)
    moved = {n.label: n for n in audit.parameter_neighbors({"fast": 5, "slow": 20}, 0.2)}
    for label, sharpe in got.items():
        spec = dataclasses.replace(eb.base_spec(inputs, services=world.services), params=dict(moved[label].params))
        row = world.rows("SELECT id FROM backtest_runs WHERE kind = 'run' AND spec_hash = :h ORDER BY id DESC LIMIT 1", h=spec.fingerprint())[0]
        assert sharpe == world.result_of(row["id"]).stats["sharpe"].value


def test_a_spread_that_is_not_a_share_is_refused_and_a_neighbor_that_would_not_move_is_left_out(world):
    inputs = world.inputs()
    for bad in (0, 1, 1.5, -0.1, True):
        with pytest.raises(Exception) as caught:
            eb.neighbor_sharpes(inputs, spread=bad, services=world.services)
        assert "spread" in str(caught.value)
    assert {n.label for n in audit.parameter_neighbors({"fast": 5, "slow": 20}, 0.1)} == {"fast+", "slow-", "slow+"}      # 5 moved 10% rounds back to 5


def test_the_ideal_point_is_one_run_recorded_as_ideal_and_reused(world):
    inputs = world.inputs()
    params = {"fast": 3, "slow": 25}                                                          # no neighbor of the run is this point
    before = trials.count("cross", db=world.db)
    run_id, sharpe = eb.point_run(inputs, params, services=world.services)
    assert run_id is not None and sharpe == world.result_of(run_id).stats["sharpe"].value
    assert trials.count("cross", db=world.db) == before + 1 and ran_ideal(world) == 1
    assert eb.point_run(inputs, params, services=world.services) == (run_id, sharpe) and trials.count("cross", db=world.db) == before + 1


def ran_ideal(world):
    return world.rows("SELECT COUNT(*) AS n FROM backtest_trials WHERE strategy_id = 'cross' AND hypothesis_id = 'cross:ideal' AND params_json = :p",
                      p='{"fast":3,"slow":25}')[0]["n"]


def test_a_hypothesis_label_is_held_inside_the_length_of_the_ledger():
    label = eb.hypothesis_of("x" * 80, "neighbors")
    assert len(label) == trials.MAX_ID and label.endswith(":neighbors") and eb.hypothesis_of("cross", "ideal") == "cross:ideal"


# ---- OPT-11: regimes ----------------------------------------------------------------------------------------------------------------------

def test_a_spec_declares_regimes_only_when_it_says_so():
    parsed = factory_spec.parse_spec(text())
    assert eb.declares_regimes(parsed) is False                                                  # no filters key in the pieces: the empty list
    for filters in (["regime_adx"], [{"kind": "regime_vol", "period": 14}], {"regime_adx": {"period": 14}}, "regime_vol"):
        assert eb.declares_regimes(dataclasses.replace(parsed, parts={"sizer": {"kind": "fixed_contracts", "n": 1}, "filters": filters})) is True
    for filters in ([], None, ["trend_filter"], [{"kind": "trend_filter"}], 5):
        assert eb.declares_regimes(dataclasses.replace(parsed, parts={"filters": filters})) is False
    assert eb.declares_regimes(dataclasses.replace(parsed, params={**parsed.params, "uses_regimes": True})) is True


def test_a_strategy_without_a_declaration_has_a_regime_check_that_does_not_apply(world):
    inputs = world.inputs()
    evidence = world.evidence(inputs)
    assert (evidence.uses_regimes, evidence.regime_sharpes) == (False, None) and eb.regime_sharpes(inputs, services=world.services) is None
    check = {i.number: i for i in world.audit(inputs, evidence).items}[21]
    assert (check.key, check.status) == ("regimes", "na")


def test_a_strategy_that_declares_regimes_with_too_little_history_stays_unknown_with_its_note(world):
    inputs = dataclasses.replace(world.inputs(), uses_regimes=True)
    evidence = world.evidence(inputs)
    assert evidence.uses_regimes is True and evidence.regime_sharpes is None                      # three months of days are fewer than the 120 sessions a label needs
    check = {i.number: i for i in world.audit(inputs, evidence).items}[21]
    assert check.status == "unknown" and "at least two regimes" in check.note


def test_each_month_takes_its_label_and_the_sharpe_of_a_regime_is_the_one_of_its_months(world, monkeypatch):
    seen = []

    def labeler(daily, as_of):
        seen.append(as_of)
        return ("favorable" if as_of.month % 2 else "hostile"), {}                              # the label of the month; the series it reads is checked below

    monkeypatch.setattr(eb, "derive_regime", labeler)
    inputs = dataclasses.replace(world.inputs(), uses_regimes=True)
    got = eb.regime_sharpes(inputs, services=world.services)
    returns = results.equity_returns(world.result_of(inputs.run_id).equity)
    expected = {}
    for name, months in (("favorable", (1, 3)), ("hostile", (2,))):
        expected[name] = metrics.compute(returns[returns.index.month.isin(months)], ["sharpe"])["sharpe"].value
    assert got == pytest.approx(expected) and [d.month for d in seen] == [1, 2, 3]
    assert all(d.day == 1 and d.tzinfo is not None for d in seen)
    evidence = world.evidence(inputs)
    assert evidence.regime_sharpes == got
    check = {i.number: i for i in world.audit(inputs, evidence).items}[21]
    assert check.status in ("pass", "fail") and "regimes have a positive Sharpe" in check.note


def test_a_month_with_no_label_does_not_count_and_one_label_alone_is_not_enough(world, monkeypatch):
    monkeypatch.setattr(eb, "derive_regime", lambda daily, as_of: ("favorable" if as_of.month == 1 else None, {}))
    assert eb.regime_sharpes(dataclasses.replace(world.inputs(), uses_regimes=True), services=world.services) is None


def test_the_daily_bars_the_labeler_reads_are_the_open_high_low_and_close_of_each_day(world):
    daily = eb._daily_bars(world.bars)
    first_day = [b for b in world.bars if b.ts.date() == world.bars[0].ts.date()]
    row = daily.iloc[0]
    assert (row["open"], row["close"]) == (float(first_day[0].open), float(first_day[-1].close))
    assert (row["high"], row["low"]) == (float(max(b.high for b in first_day)), float(min(b.low for b in first_day)))
    assert len(daily) == len({b.ts.date() for b in world.bars}) == 60


# ---- the rules of the whole -----------------------------------------------------------------------------------------------------------------

def test_the_bridge_holds_no_statistic_of_its_own_and_no_road_to_a_broker():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(eb))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not {m for m in imported if m.startswith(("scipy", "statsmodels", "sklearn"))}
    assert not {m for m in imported if m.split(".")[:2] in (["hokma", "broker"], ["hokma", "exec"], ["hokma", "market"], ["hokma", "schwab"])}
    source = inspect.getsource(eb)
    for banned in ("np.std", ".std(", ".var(", "np.corrcoef", "math.sqrt"):
        assert banned not in source
    assert chr(0x2014) not in source

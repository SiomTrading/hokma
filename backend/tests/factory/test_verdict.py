"""The verdict of the optimizer (SPEC-032, OPT-12 to OPT-15): the evaluation, the checks that the owner takes out and puts back, the recalculation, the three
sentences and the table of changes of section 8 as data, and the ideal result. The strategies are the ones of the bridge tests (saved specs of the factory, real
runs of H4 over the moves of the NQ fixture); the surfaces of the ideal tests are small and written by hand, so every expected value is in a comment."""
import dataclasses
import json
import re

import numpy as np
import pytest
from backtest import support
from backtest.search_support import FakeLedger
from backtest.support import requires_engine
from factory.test_evidence_bridge import SPACE, World, points_of, known_points, world  # noqa: F401  (the world is a fixture of this module too)
from factory.test_spec import text

from hokma import i18n
from hokma.backtest import optimize, surface, trials
from hokma.backtest.optimize import Axis, ParamSpace
from hokma.factory import audit
from hokma.factory import evidence_bridge as eb
from hokma.factory import questions as qs
from hokma.factory import spec as factory_spec
from hokma.factory import thresholds as th
from hokma.factory import verdict
from hokma.factory.evidence_bridge import BridgeError
from hokma.factory.thresholds import ThresholdError
from hokma.factory.verdict import VerdictError

pytestmark = requires_engine


def kmeans(points, k, seed):
    """A small k-means with a farthest point start: it stands in for the one of the portfolio module, which is not built yet (the adapter passes points, k, seed)."""
    rng = np.random.default_rng(seed)
    centers = [points[int(rng.integers(len(points)))]]
    while len(centers) < k:
        far = np.argmax(np.min([np.linalg.norm(points - c, axis=1) for c in centers], axis=0))
        centers.append(points[far])
    centers = np.array(centers, dtype=float)
    for _ in range(50):
        labels = np.argmin(np.linalg.norm(points[:, None, :] - centers[None, :, :], axis=2), axis=1)
        moved = np.array([points[labels == g].mean(axis=0) if (labels == g).any() else centers[g] for g in range(k)])
        if np.allclose(moved, centers):
            break
        centers = moved
    return labels, centers


@pytest.fixture(scope="module", autouse=True)
def grouping():
    patch = pytest.MonkeyPatch()
    patch.setattr(surface, "_default_cluster", lambda: kmeans)
    yield
    patch.undo()


@pytest.fixture(scope="module")
def evaluated(world, grouping):
    return verdict.evaluate("cross", 1, actor="owner", services=world.services)


@pytest.fixture(scope="module")
def short(world, grouping):
    """The verdict of a strategy that has a run and nothing else: no walk forward, no optimization."""
    return verdict.evaluate("nowalk", 1, actor="owner", services=world.services)


def checks(v):
    return {i.key: i for i in v.report.items}


def counts(world):
    with world.db.read() as u:
        return (u.session.scalar("SELECT COUNT(*) FROM backtest_runs"), u.session.scalar("SELECT COUNT(*) FROM backtest_trials"))


def stored(world, verdict_id):
    with world.db.read() as u:
        return u.session.one("SELECT * FROM strategy_verdicts WHERE id = :i", {"i": verdict_id})


def out(v):
    return {e.key for e in v.excluded}


def live(v):
    return [i.status for i in v.report.items if i.key not in out(v)]


# ---- OPT-12: evaluate ---------------------------------------------------------------------------------------------------------------------

def test_evaluate_saves_one_row_that_joins_the_audit_with_the_limits_the_answers_and_the_saved_runs(world, evaluated):
    v, inputs = evaluated, world.inputs()
    assert (v.strategy_id, v.strategy_version, v.line, v.cause, v.parent_id, v.actor) == ("cross", 1, "DT", "evaluate", None, "owner")
    assert (v.run_id, v.walkforward_id, v.optimize_id, v.montecarlo_id) == (inputs.run_id, inputs.walkforward_id, inputs.optimize_id, None)
    assert v.thresholds.line == "DT" and v.thresholds.values == world.thresholds.values and len(v.report.items) == 24 and len(v.sentences) == 3
    assert v.audit_verdict == v.report.verdict and v.verdict == audit.verdict_for(live(v))
    assert verdict.latest("cross", 1, db=world.db) is not None and verdict.latest("cross", 99, db=world.db) is None
    row = stored(world, v.id)
    assert row["thresholds_id"] == v.thresholds.id and row["audit_verdict"] == v.audit_verdict and row["verdict"] == v.verdict
    assert audit.AuditEvidence(**{k: (tuple(x) if k == "edge_of_space" and x is not None else x) for k, x in json.loads(row["evidence_json"]).items()}).n_trials >= 9
    assert "monte_carlo_missing" in v.notes and [q.key for q in v.questions] == [q.key for q in qs.QUESTIONS]


def test_the_checks_that_do_not_apply_come_out_as_exclusions_of_the_system_with_their_reason(world, evaluated):
    by_key = {e.key: e for e in evaluated.excluded}
    assert (by_key["survivorship_free"].reason, by_key["survivorship_free"].by) == ("not_applicable:single_instrument", "system")
    assert "financing_and_borrow" not in by_key and checks(evaluated)["financing_and_borrow"].status == "na"      # the audit itself says it does not apply
    assert checks(evaluated)["survivorship_free"].status == "unknown"                                          # the audit is not changed: the verdict leaves it out
    with pytest.raises(VerdictError) as caught:
        verdict.include(evaluated.id, ["survivorship_free"], actor="owner", db=world.db)
    assert caught.value.code == "bad_input" and "answer" in str(caught.value)


def test_the_same_saved_facts_give_the_same_input_hash_the_same_verdict_and_no_new_trial(world, evaluated):
    first = verdict.evaluate("cross", 1, actor="owner", services=world.services)
    before = counts(world)
    second = verdict.evaluate("cross", 1, actor="owner", services=world.services)
    assert counts(world)[1] == before[1]                                                                        # the second evaluation counts nothing more
    assert second.id != first.id and stored(world, second.id)["inputs_hash"] == stored(world, first.id)["inputs_hash"]
    assert (second.verdict, second.audit_verdict, second.report, second.sentences, second.suggestions, second.ideal) == \
           (first.verdict, first.audit_verdict, first.report, first.sentences, first.suggestions, first.ideal)
    assert len(stored(world, first.id)["inputs_hash"]) == 64


def test_evaluate_inherits_the_exclusions_in_force_and_makes_the_ones_of_the_answers_again(world, short):
    reds = [i.key for i in short.report.items if i.status == "fail" and i.key not in out(short)]
    assert reds
    child = verdict.exclude(short.id, reds[:1], reason="not for this strategy", actor="owner", db=world.db)
    again = verdict.evaluate("nowalk", 1, actor="owner", services=world.services)
    kept = {e.key: e for e in again.excluded if e.by == "owner"}
    assert set(kept) == set(reds[:1]) and kept[reds[0]].reason == "not for this strategy"
    assert kept[reds[0]].at == {e.key: e for e in child.excluded}[reds[0]].at and kept[reds[0]].by == "owner"
    assert [e.key for e in again.excluded if e.by == "system"].count("survivorship_free") == 1               # made again, not copied twice
    assert again.verdict == audit.verdict_for(live(again)) and again.audit_verdict == short.audit_verdict


def test_without_a_walk_forward_the_verdict_asks_for_evidence_and_says_why(world, short):
    assert checks(short)["validation_scheme"].status == "unknown" and "no_walk_forward" in short.notes and short.ideal.reason == "no_optimization"
    assert short.ideal.status == "not_found" and short.ideal.params is None
    assert {s.action for s in short.suggestions} >= {"run_walk_forward", "answer_question"}


def test_evaluate_refuses_what_it_cannot_judge_with_the_code_of_its_module(world):
    for bad in ("", "Owner", "two words", None, 5):
        with pytest.raises(VerdictError) as caught:
            verdict.evaluate("cross", 1, actor=bad, services=world.services)
        assert caught.value.code == "bad_input"
    with pytest.raises(BridgeError) as caught:
        verdict.evaluate("ghost", 1, actor="owner", services=world.services)
    assert caught.value.code == "no_spec"
    factory_spec.save_draft(text(strategy_id="lp_demo").replace("line: DT", "line: LP"), db=world.db)
    before = counts(world)
    with pytest.raises(ThresholdError) as caught:
        verdict.evaluate("lp_demo", 1, actor="owner", services=world.services)
    assert caught.value.code == "no_thresholds" and counts(world) == before                                    # it stops before anything runs


# ---- OPT-13: exclude, include and recompute ---------------------------------------------------------------------------------------------

def test_taking_the_last_red_check_out_changes_no_go_to_what_follows_and_runs_no_backtest(world, evaluated):
    reds = [i.key for i in evaluated.report.items if i.status == "fail"]
    assert evaluated.verdict == "no_go" and reds
    before = counts(world)
    current = evaluated
    for key in reds[:-1]:
        current = verdict.exclude(current.id, [key], reason=None, actor="owner", db=world.db)
        assert current.verdict == "no_go"                                                                       # one red is still in
    last = verdict.exclude(current.id, [reds[-1]], reason="no interest in this one", actor="owner", db=world.db)
    assert last.verdict == audit.verdict_for(live(last)) and last.verdict != "no_go"
    assert last.audit_verdict == evaluated.audit_verdict == "no_go"                                            # the verdict of the audit never changes
    assert counts(world) == before                                                                              # nothing ran: not a run, not a trial
    assert (last.cause, last.parent_id) == ("exclude", current.id) and set(reds) <= out(last)
    mine = {e.key: e for e in last.excluded}[reds[-1]]
    assert (mine.reason, mine.by) == ("no interest in this one", "owner") and mine.at is not None
    assert last.sentences != evaluated.sentences and {s.key for s in last.suggestions}.isdisjoint(reds)
    back = verdict.include(last.id, reds, actor="owner", db=world.db)
    assert back.verdict == "no_go" and (back.cause, back.parent_id) == ("include", last.id) and not set(reds) & out(back) and counts(world) == before


def test_exclude_and_include_refuse_what_is_not_a_check_or_not_out(world, evaluated):
    for keys in ([], "pbo", ["nonsense"], [5], None):
        with pytest.raises(VerdictError) as caught:
            verdict.exclude(evaluated.id, keys, reason=None, actor="owner", db=world.db)
        assert caught.value.code == "bad_input"
    for bad in (0, 10**9):
        with pytest.raises(VerdictError) as caught:
            verdict.exclude(bad, ["pbo"], reason=None, actor="owner", db=world.db)
        assert caught.value.code == "no_verdict"
    with pytest.raises(VerdictError):
        verdict.exclude(evaluated.id, ["pbo"], reason="x" * 501, actor="owner", db=world.db)
    with pytest.raises(VerdictError):
        verdict.exclude(evaluated.id, ["pbo"], reason=None, actor="Not A Name", db=world.db)
    with pytest.raises(VerdictError) as caught:
        verdict.include(evaluated.id, ["pbo"], actor="owner", db=world.db)
    assert caught.value.code == "bad_input" and "pbo" in str(caught.value)
    gone = verdict.exclude(evaluated.id, ["red_flags", "pbo"], reason=None, actor="owner", db=world.db)
    assert verdict.exclude(gone.id, ["pbo"], reason="again", actor="owner", db=world.db).id == gone.id          # already out: the same verdict comes back


def expected_sentences(v, language):
    """The three sentences written from the catalog templates with the parameters that the report of the verdict gives: the counts, the main check, the first change."""
    cat = i18n.catalog(language)
    live_items = [i for i in v.report.items if i.key not in out(v)]
    wanted = {"no_go": "fail", "review": "warn", "needs_evidence": "unknown"}.get(v.verdict)
    kind = [i for i in live_items if i.status == wanted]
    if v.verdict == "go":
        first = cat["verdict.sentence.go"].format(n=len([i for i in live_items if i.status != "na"]))
    else:
        name = cat["verdict.check." + kind[0].key]
        asked = sum(1 for q in v.questions if q.status == "ask" and verdict.CHECK_OF_QUESTION[q.key] not in out(v))
        if v.verdict == "needs_evidence":
            first = cat["verdict.sentence.needs_evidence"].format(q=asked, k=len(kind)) if asked else cat["verdict.sentence.needs_evidence_other"].format(k=len(kind), name=name)
        else:
            first = cat[f"verdict.sentence.{v.verdict}"].format(k=len(kind), name=name)
    ordered = sorted((s for s in v.suggestions if s.key not in out(v)), key=lambda s: (verdict.SEVERITY.index(s.status), s.check))
    if v.verdict == "go" or not ordered:
        third = cat["verdict.sentence.next_go"]
    elif ordered[0].action == "answer_question":
        third = cat["verdict.sentence.next_answer"]
    else:
        third = cat["verdict.sentence.next"].format(text=verdict.render_suggestion(ordered[0], language))
    return first, third


def test_each_of_the_four_verdicts_gives_its_three_sentences_in_both_languages(world, evaluated):
    def statuses(v, status):
        return [i.key for i in v.report.items if i.status == status and i.key not in out(v)]

    chain = [evaluated]
    for status in ("fail", "unknown", "warn"):
        assert statuses(chain[-1], status), status                                                              # the premise: this run has all three
        chain.append(verdict.exclude(chain[-1].id, statuses(chain[-1], status), reason=None, actor="owner", db=world.db))
    assert [v.verdict for v in chain] == ["no_go", "needs_evidence", "review", "go"]
    for v in chain:
        for language in ("es", "en"):
            cat = i18n.catalog(language)
            sentences = v.to_dict(language=language)["sentences"]
            first, third = expected_sentences(v, language)
            assert len(sentences) == 3 and (sentences[0], sentences[2]) == (first, third)
            assert sentences[1].startswith(cat["verdict.sentence.strongest.pbo"].split(":")[0]) or sentences[1] == cat["verdict.sentence.strongest.none"]
            for sentence in sentences:
                assert sentence and "{" not in sentence and not sentence.startswith("verdict.") and chr(0x2014) not in sentence
        assert v.to_dict(language="es")["sentences"] != v.to_dict(language="en")["sentences"]
    assert len({s for v in chain for s in v.to_dict(language="en")["sentences"][:1]}) == 4                      # one different first sentence for each verdict
    assert chain[1].to_dict(language="en")["sentences"][2] == i18n.catalog("en")["verdict.sentence.next_answer"]


def test_recompute_with_new_limits_audits_again_over_the_saved_evidence_and_runs_nothing(world, evaluated):
    assert checks(evaluated)["pbo"].status == "fail"
    new = th.save("DT", {**world.thresholds.values, "pbo_reject": 0.9}, reason="a wider limit", actor="owner", db=world.db)
    try:
        before = counts(world)
        child = verdict.recompute(evaluated.id, cause="thresholds", actor="owner", services=world.services)
        assert counts(world) == before                                                                          # the spread did not change: the neighbors are read, not run
        assert (child.cause, child.parent_id, child.thresholds.id) == ("thresholds", evaluated.id, new.id) and child.thresholds.version == new.version
        assert checks(child)["pbo"].status == "warn" and child.audit_verdict == child.report.verdict and child.verdict == audit.verdict_for(live(child))
        assert child.report != evaluated.report and child.excluded == evaluated.excluded
        assert verdict.recompute(child.id, cause="thresholds", actor="owner", services=world.services).id == child.id      # nothing changed: the same verdict
    finally:
        th.restore("DT", world.thresholds.id, actor="owner", db=world.db)


def test_a_new_neighbor_spread_runs_the_neighbors_that_the_ledger_does_not_have(world, evaluated):
    new_points = points_of(0.2) - known_points(world)
    assert new_points
    th.save("DT", {**world.thresholds.values, "neighbor_spread": 0.2}, reason="wider neighbors", actor="owner", db=world.db)
    try:
        before = trials.count("cross", db=world.db)
        child = verdict.recompute(evaluated.id, cause="thresholds", actor="owner", services=world.services)
        assert trials.count("cross", db=world.db) - before >= len(new_points)
        evidence = json.loads(stored(world, child.id)["evidence_json"])
        assert set(evidence["neighbor_sharpes"]) == {n.label for n in audit.parameter_neighbors({"fast": 5, "slow": 20}, 0.2)}
        again = trials.count("cross", db=world.db)
        verdict.evaluate("cross", 1, actor="owner", services=world.services)
        assert trials.count("cross", db=world.db) == again                                                      # an evaluation at that spread finds everything run
    finally:
        th.restore("DT", world.thresholds.id, actor="owner", db=world.db)


def test_recompute_with_new_answers_changes_what_depends_on_them_and_runs_nothing(world, evaluated):
    assert checks(evaluated)["holdout_untouched"].status == "unknown" and checks(evaluated)["capacity"].status == "unknown"
    qs.answer("cross", 1, "holdout_untouched", "no", note="used it twice", actor="owner", db=world.db)
    qs.answer("cross", 1, "capacity_checked", "na", note=None, actor="owner", db=world.db)
    before = counts(world)
    child = verdict.recompute(evaluated.id, cause="answers", actor="owner", services=world.services)
    assert counts(world) == before and (child.cause, child.parent_id) == ("answers", evaluated.id)
    assert checks(child)["holdout_untouched"].status == "fail" and checks(child)["capacity"].status == "unknown"
    left_out = {e.key: e for e in child.excluded}["capacity"]
    assert (left_out.reason, left_out.by) == ("owner_said_na", "system")                                       # the audit says unknown; the verdict leaves it out
    assert len(json.loads(stored(world, child.id)["answer_ids_json"])) == 2
    assert {q.key: q.answer for q in child.questions if q.status == "answered"} == {"holdout_untouched": "no", "capacity_checked": "na"}
    assert "reserve_holdout" in {s.action for s in child.suggestions} and "capacity" not in {s.key for s in child.suggestions}
    assert verdict.recompute(child.id, cause="answers", actor="owner", services=world.services).id == child.id
    with pytest.raises(VerdictError) as caught:
        verdict.recompute(child.id, cause="exclude", actor="owner", services=world.services)
    assert caught.value.code == "bad_input"


# ---- OPT-14: the sentences and the table of section 8 -------------------------------------------------------------------------------------

TABLE = [  # (check, status, the note that its rule writes, the changes that section 8 gives)
    *[(n, "unknown", "no evidence was given", {"answer_question"}) for n in (1, 2, 3, 5, 7, 8, 11, 12, 14)],
    (1, "fail", "later revisions are in the data", {"use_point_in_time_data"}),
    (2, "fail", "survivors only", {"include_delisted"}),
    (3, "fail", "corporate actions not applied", {"apply_corporate_actions"}),
    (4, "fail", "a feature reads the future", {"fix_lookahead"}),
    (4, "unknown", "no evidence was given", {"fix_lookahead"}),
    (5, "fail", "data problems not handled", {"clean_data"}),
    (6, "unknown", "the number of trials was not given", {"count_trials"}),
    (6, "fail", "the count of trials is below 1", {"count_trials"}),
    (7, "fail", "tuned on data it was tested on", {"tune_in_sample"}),
    (8, "fail", "the holdout was used during development", {"reserve_holdout"}),
    (9, "fail", "0.25 years and 14 closed trades; needs 1.0 years or 200 trades", {"lengthen_history"}),
    (10, "fail", "no_costs: commission 0 and slippage 0 USD: both must be above zero when trades exist", {"charge_costs"}),
    (10, "fail", "cost_share: costs take 40% of the gross Sharpe; the limit is 33%", {"reduce_turnover"}),
    (11, "fail", "no impact model and no argument that it does not matter", {"model_impact"}),
    (12, "fail", "short or financed trades with no financing cost", {"charge_financing"}),
    (13, "fail", "the fill is not the house fill", {"use_house_fill"}),
    (15, "unknown", "the validation scheme was not given", {"run_walk_forward"}),
    (15, "fail", "'holdout' is not one of walk_forward, cpcv", {"run_walk_forward"}),
    (16, "fail", "23 trials and no correction", {"deflate_sharpe"}),
    (17, "fail", "annual Sharpe 0.300 against 0.570", {"beat_chance"}),
    (18, "fail", "DSR 0.500 against the minimum 0.95", {"raise_dsr"}),
    (19, "warn", "PBO 0.300 is at or above 0.2", {"reduce_overfit"}),
    (19, "fail", "PBO 0.600 is at or above 0.5", {"reduce_overfit"}),
    (20, "fail", "worst neighbor fast- loses 45.0% of the base Sharpe; the limit is 30%", {"seek_plateau"}),
    (20, "warn", "edge: best point on the border of: fast; worst neighbor slow+ loses 5.0% of the base Sharpe; the limit is 30%", {"widen_range"}),
    (21, "fail", "40% of 3 regimes have a positive Sharpe; the minimum is 50%", {"regime_filter"}),
    (22, "fail", "sharpe_high: annual Sharpe 3.50 is above 3.0", {"audit_for_bug"}),
    (22, "fail", "top_trade: one trade carries 40% of the profit", {"diversify_trades"}),
    (22, "fail", "oos_ratio: out-of-sample Sharpe is 20% of in-sample; the minimum is 50%", {"stabilize_oos"}),
    (22, "fail", "sharpe_high: annual Sharpe 3.50 is above 3.0; top_trade: one trade carries 40% of the profit; "
                 "oos_ratio: out-of-sample Sharpe is 20% of in-sample; the minimum is 50%", {"audit_for_bug", "diversify_trades", "stabilize_oos"}),
    (23, "fail", "the cost split lacks swap", {"split_costs"}),
    (24, "unknown", "the stats hold no drawdown block", {"report_drawdown"}),
]
NOT_IN_THE_TABLE = [(19, "pass"), (20, "pass"), (10, "pass"), (22, "pass"), (1, "na"), (15, "warn"), (24, "fail"), (13, "unknown"), (21, "unknown")]


def report_of(number, status, note):
    items = [audit.CheckResult(c.number, c.key, c.text, status if c.number == number else "pass", note if c.number == number else "ok") for c in audit.CHECKLIST]
    phases = tuple(audit.PhaseReport(p, tuple(i for i in items if verdict.CHECK_BY_KEY[i.key].phase == p)) for p in audit.PHASES)
    return audit.AuditReport(phases, "no_go")


NO_IDEAL = verdict.Ideal("not_found", None, None, None, None, "no_optimization")
FOUND = verdict.Ideal("found", {"fast": 12, "slow": 160}, 14, 1.2, 845, None)


@pytest.mark.parametrize("number, status, note, actions", TABLE, ids=[f"{n}-{s}-{sorted(a)[0]}" for n, s, _note, a in TABLE])
def test_every_row_of_the_table_of_section_8_gives_its_code_and_its_text_in_both_languages(number, status, note, actions):
    evidence = audit.AuditEvidence(n_trials=23, pbo=0.6, edge_of_space=("fast",), regime_sharpes={"favorable": 1.0, "hostile": -0.5})
    values = th.MEASURES and {m.key: m.low for m in th.MEASURES}
    got = verdict.build_suggestions(report_of(number, status, note), evidence, {**values, "pbo_reject": 0.5, "pbo_warn": 0.2}, NO_IDEAL, None)
    assert {s.action for s in got} == actions and all(s.check == number and s.status == status and s.key == verdict.KEY_OF_CHECK[number] for s in got)
    for suggestion in got:
        for language in ("es", "en"):
            rendered = verdict.render_suggestion(suggestion, language)
            assert rendered and "{" not in rendered and not rendered.startswith("verdict.") and "\u2014" not in rendered


@pytest.mark.parametrize("number, status", NOT_IN_THE_TABLE)
def test_a_status_that_the_table_does_not_name_gives_no_change(number, status):
    assert verdict.build_suggestions(report_of(number, status, "x"), audit.AuditEvidence(), {"pbo_reject": 0.5, "pbo_warn": 0.2}, NO_IDEAL, None) == []


def test_the_table_has_the_28_codes_of_section_8_and_each_has_its_text_in_both_catalogs():
    assert set(verdict.ACTIONS) == set().union(*(a for *_x, a in TABLE)) and len(verdict.ACTIONS) == 28
    for language in ("es", "en"):
        catalog = i18n.catalog(language)
        for action in verdict.ACTIONS:
            assert catalog[f"verdict.action.{action}"]
        assert catalog["verdict.action.seek_plateau_no_ideal"]
        for key in (c.key for c in audit.CHECKLIST):
            assert catalog[f"verdict.check.{key}"]
        for question in qs.QUESTIONS:
            assert catalog[f"verdict.question.{question.key}"]
    es, en = i18n.catalog("es"), i18n.catalog("en")
    placeholder = re.compile(r"\{([a-z_0-9]+)\}")
    mine = [k for k in en if k.startswith("verdict.")]
    assert mine and set(mine) == {k for k in es if k.startswith("verdict.")}
    for key in mine:
        assert set(placeholder.findall(es[key])) == set(placeholder.findall(en[key])) and "\u2014" not in es[key] + en[key], key


def test_the_text_of_a_change_names_its_numbers_and_the_ideal_when_there_is_one():
    evidence = audit.AuditEvidence(n_trials=23, pbo=0.6)
    values = {"pbo_reject": 0.5, "pbo_warn": 0.2}
    cost = verdict.build_suggestions(report_of(10, "fail", "cost_share: costs take 40% of the gross Sharpe; the limit is 33%"), evidence, values, NO_IDEAL, None)[0]
    assert cost.params == {"pct": "40%", "max": "33%"}
    assert verdict.render_suggestion(cost, "en") == "Trade less or aim wider: costs eat 40% of the edge (maximum 33%)."
    assert "40%" in verdict.render_suggestion(cost, "es") and "33%" in verdict.render_suggestion(cost, "es")
    overfit = verdict.build_suggestions(report_of(19, "fail", "PBO"), evidence, values, NO_IDEAL, None)[0]
    assert overfit.params == {"pbo": 0.6, "limit": 0.5}
    plateau = verdict.build_suggestions(report_of(20, "fail", "fragile"), evidence, values, FOUND, None)[0]
    assert plateau.message == "verdict.action.seek_plateau" and plateau.params == {"ideal": "fast=12, slow=160"}
    assert "fast=12, slow=160" in verdict.render_suggestion(plateau, "en")
    alone = verdict.build_suggestions(report_of(20, "fail", "fragile"), evidence, values, NO_IDEAL, None)[0]
    assert alone.message == "verdict.action.seek_plateau_no_ideal" and "{" not in verdict.render_suggestion(alone, "es")
    ask = verdict.build_suggestions(report_of(2, "unknown", "no evidence was given"), evidence, values, NO_IDEAL, None)[0]
    assert ask.params == {"question": "survivorship_free"} and i18n.catalog("en")["verdict.question.survivorship_free"] in verdict.render_suggestion(ask, "en")


def test_the_notes_of_the_real_rules_carry_the_codes_that_the_table_reads(world, evaluated):
    inputs = world.inputs()
    base = world.evidence(inputs)
    result, spec = world.result_of(inputs.run_id), eb.base_spec(inputs, services=world.services)
    config = dataclasses.replace(world.config, max_top_trade_share=0.05)
    worse = dataclasses.replace(base, cost_sharpe=5.0, gross_sharpe=8.0, is_sharpe=1.0, oos_sharpe=0.1, edge_of_space=("fast",))
    report = audit.audit(result, spec, worse, config)
    seen = {i.number: i for i in report.items}
    assert {"cost_share"} <= verdict._codes(seen[10].note) and seen[10].status == "fail"
    assert {"sharpe_high", "top_trade", "oos_ratio"} <= verdict._codes(seen[22].note) and seen[22].status == "fail"
    got = verdict.build_suggestions(report, worse, world.thresholds.values, NO_IDEAL, None)
    assert {"reduce_turnover", "audit_for_bug", "diversify_trades", "stabilize_oos"} <= {s.action for s in got}
    edge = audit.audit(result, spec, dataclasses.replace(base, edge_of_space=("fast",), neighbor_sharpes={"fast-": base.neighbor_sharpes and max(base.neighbor_sharpes.values())}), world.config)
    assert "edge" in verdict._codes({i.number: i for i in edge.items}[20].note)
    ordered = [(SEV, n) for SEV, n in ((verdict.SEVERITY.index(s.status), s.check) for s in got)]
    assert ordered == sorted(ordered)                                                                           # the most serious first


def test_the_suggestions_that_the_owner_took_out_do_not_come_back_until_the_check_does(world, evaluated):
    some = evaluated.suggestions[0].key
    child = verdict.exclude(evaluated.id, [some], reason=None, actor="owner", db=world.db)
    assert some not in {s.key for s in child.suggestions} and len(child.suggestions) < len(evaluated.suggestions)
    assert [s for s in verdict.include(child.id, [some], actor="owner", db=world.db).suggestions] == list(evaluated.suggestions)


def test_the_dictionary_of_the_contract_has_every_field_of_section_9_2(world, evaluated):
    body = evaluated.to_dict()
    assert set(body) == {"verdict_id", "strategy_id", "strategy_version", "line", "verdict", "audit_verdict", "advisory", "sentences", "checks", "excluded",
                         "open_questions", "suggestions", "ideal", "thresholds", "runs", "notes", "created_at"}
    assert body["advisory"] is True and body["thresholds"] == {"line": "DT", "version": evaluated.thresholds.version} and len(body["checks"]) == 24
    assert body["runs"] == {"run": evaluated.run_id, "walkforward": evaluated.walkforward_id, "optimize": evaluated.optimize_id, "montecarlo": None}
    assert set(body["checks"][0]) == {"number", "key", "phase", "status", "note", "excluded"} and body["checks"][18]["phase"] == "overfitting"
    assert set(body["ideal"]) == {"status", "params", "plateau_size", "run_id", "reason"} and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", body["created_at"])
    assert all(set(s) == {"check", "key", "status", "action", "text", "params"} and s["action"] in verdict.ACTIONS for s in body["suggestions"])
    assert all(set(q) == {"key", "scope", "subject"} for q in body["open_questions"]) and {e["key"] for e in body["excluded"]} == out(evaluated)
    assert evaluated.to_dict(language="fr")["sentences"] == body["sentences"]                                   # an unknown language is English
    assert json.loads(json.dumps(body)) == body


# ---- OPT-15: the ideal result -------------------------------------------------------------------------------------------------------------

def surface_of(f, space=None):
    space = space or ParamSpace((Axis("x", "int", 0, 9), Axis("y", "int", 0, 9)))
    return optimize.search(support.spec(strategy_id="alpha"), space, method="grid", n_trials=space.size, objective="sharpe", presweep=False, evaluate=f, ledger=FakeLedger())


def peak_and_plateau(p):
    """An isolated peak of 62 at (0, 0), a wide plateau of nine points of 60 (x and y from 5 to 7) and a gentle slope of x plus y elsewhere."""
    if (p["x"], p["y"]) == (0, 0):
        return 62.0
    if 5 <= p["x"] <= 7 and 5 <= p["y"] <= 7:
        return 60.0
    return float(p["x"] + p["y"])


def script(monkeypatch, neighbors):
    asked = []
    monkeypatch.setattr(eb, "point_run", lambda inputs, params, *, services=None: (asked.append(dict(params)), (inputs.run_id, 8.0))[1])
    monkeypatch.setattr(eb, "neighbor_sharpes", lambda inputs, *, spread, services=None, params=None: neighbors)
    return asked


def synthetic_inputs(world, optimization):
    return dataclasses.replace(world.inputs(bundle=False), optimization=optimization)


def test_with_an_isolated_peak_and_a_wide_plateau_the_ideal_is_the_centre_of_the_plateau_and_not_the_peak(world, monkeypatch):
    found = surface._surface if False else None  # noqa: F841
    optimization = surface_of(peak_and_plateau)
    assert optimization.best.params == {"x": 0, "y": 0}                                                         # the best point of the search is the peak
    asked = script(monkeypatch, {"x-": 8.2, "x+": 7.9})
    got = verdict._ideal_stage1(synthetic_inputs(world, optimization), world.thresholds.values, world.config, world.services)
    base = eb.base_spec(world.inputs(bundle=False), services=world.services)
    assert asked == [{**base.params, "x": 6, "y": 6}]                                                           # the centre of the nine points; the peak was never run
    assert isinstance(got, verdict._Pending) and dict(got.params) == {"x": 6, "y": 6} and (got.size, got.mean) == (9, 60.0) and got.neighbors_ok is True


def test_when_the_centre_of_the_plateau_does_not_hold_its_neighbors_there_is_no_ideal(world, monkeypatch, evaluated):
    script(monkeypatch, {"x-": 1.0, "x+": 0.5})
    got = verdict._ideal_stage1(synthetic_inputs(world, surface_of(peak_and_plateau)), world.thresholds.values, world.config, world.services)
    assert isinstance(got, verdict._Pending) and got.neighbors_ok is False
    ideal = verdict._ideal_stage2(got, evaluated.report, 100.0, world.thresholds.values)
    assert (ideal.status, ideal.reason, dict(ideal.params), ideal.plateau_size) == ("not_found", "plateau_fails_neighbors", {"x": 6, "y": 6}, 9)


def test_an_ideal_that_holds_its_neighbors_still_needs_the_walk_forward(world, monkeypatch, evaluated, short):
    script(monkeypatch, {"x-": 8.2, "x+": 7.9})
    values = world.thresholds.values
    got = verdict._ideal_stage1(synthetic_inputs(world, surface_of(peak_and_plateau)), values, world.config, world.services)
    ratio, oos_total = evaluated.report.oos_to_is, json.loads(stored(world, evaluated.id)["report_json"])["context"]["oos_total"]
    assert checks(evaluated)["validation_scheme"].status == "pass" and ratio >= values["min_oos_to_is"] and oos_total > 0
    ideal = verdict._ideal_stage2(got, evaluated.report, oos_total, values)
    assert (ideal.status, dict(ideal.params), ideal.run_id, ideal.reason) == ("found", {"x": 6, "y": 6}, evaluated.run_id, None)
    assert verdict._ideal_stage2(got, evaluated.report, None, values).status == "found"                         # cpcv has no total out of sample: the ratio decides
    stricter = verdict._ideal_stage2(got, evaluated.report, oos_total, {**values, "min_oos_to_is": 1.0})
    assert (stricter.status, stricter.reason) == ("not_found", "walk_forward_fails") and dict(stricter.params) == {"x": 6, "y": 6}
    assert verdict._ideal_stage2(got, evaluated.report, -5.0, values).reason == "walk_forward_fails"            # it loses out of sample
    assert verdict._ideal_stage2(got, short.report, None, values).reason == "walk_forward_fails"                # no walk forward at all


def test_the_other_reasons_for_no_ideal(world, monkeypatch, short):
    assert (short.ideal.status, short.ideal.reason) == ("not_found", "no_optimization")
    inputs = synthetic_inputs(world, surface_of(peak_and_plateau))

    def refuse():
        raise surface.ClusterUnavailable("cluster_unavailable: the grouping is not there")

    monkeypatch.setattr(surface, "_default_cluster", refuse)
    got = verdict._ideal_stage1(inputs, world.thresholds.values, world.config, world.services)
    assert (got.status, got.reason, got.params) == ("not_found", "plateaus_unavailable", None)
    monkeypatch.setattr(surface, "_default_cluster", lambda: kmeans)
    named = ParamSpace((Axis("x", "int", 0, 4), Axis("mode", "choice", choices=("a", "b"))))
    choice = verdict._ideal_stage1(synthetic_inputs(world, surface_of(lambda p: float(p["x"]), named)), world.thresholds.values, world.config, world.services)
    assert (choice.status, choice.reason) == ("not_found", "plateaus_unavailable")                              # a parameter with no distance has no plateau
    assert set(verdict.IDEAL_REASONS) == {"plateau_fails_neighbors", "walk_forward_fails", "no_optimization", "plateaus_unavailable"}


def test_the_centre_is_taken_to_the_grid_and_a_point_the_constraints_refuse_to_the_nearest_valid_one():
    space = ParamSpace((Axis("a", "int", 1, 5), Axis("b", "int", 1, 5), Axis("c", "float", 0.0, 1.0, 0.25)), (optimize.Constraint("a", "lt", "b"),))
    assert verdict._snap(space, {"a": 2.4, "b": 4.6, "c": 0.4}) == {"a": 2, "b": 5, "c": 0.5}                   # 2.4 is nearest to 2, 4.6 to 5, 0.4 to 0.5
    assert verdict._snap(space, {"a": 2.5, "b": 4.0, "c": 0.125}) == {"a": 2, "b": 4, "c": 0.0}                 # a tie goes to the lower value
    assert isinstance(verdict._snap(space, {"a": 2.4, "b": 4.6, "c": 0.4})["a"], int)
    refused = verdict._snap(space, {"a": 4.6, "b": 4.4, "c": 0.0})                                              # (5, 4) breaks a < b: (4, 5) is the nearest valid point
    assert (refused["a"], refused["b"]) == (4, 5)


def test_the_real_ideal_of_an_evaluation_is_a_point_of_the_grid_and_costs_nothing_the_second_time(world, evaluated):
    ideal = evaluated.ideal
    assert ideal.status in ("found", "not_found") and (ideal.reason is None) == (ideal.status == "found")
    assert ideal.reason is None or ideal.reason in verdict.IDEAL_REASONS
    assert ideal.params is not None and ideal.plateau_size >= 1                                                 # the plateau was found with the grouping of the test
    for axis in SPACE.axes:
        assert ideal.params[axis.name] in axis.values
    assert ideal.run_id is not None
    with world.db.read() as u:
        row = u.session.one("SELECT kind, parent_id FROM backtest_runs WHERE id = :i", {"i": ideal.run_id})
    assert row["kind"] == "run"
    before = counts(world)
    verdict.evaluate("cross", 1, actor="owner", services=world.services)
    assert counts(world) == before


def test_the_verdict_holds_no_road_to_a_broker_and_no_statistic_of_its_own():
    import ast
    import inspect

    source = inspect.getsource(verdict)
    imported = {n.module for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom) and n.module}
    assert not {m for m in imported if m.split(".")[:2] in (["hokma", "broker"], ["hokma", "exec"], ["hokma", "market"], ["hokma", "schwab"])}
    assert not {m for m in imported if m.startswith(("scipy", "statsmodels", "sklearn", "numpy", "pandas"))}
    for banned in ("np.std", ".std(", ".var(", "math.sqrt", "np.mean", ".mean("):
        assert banned not in source
    assert "\u2014" not in source and "TODO" not in source

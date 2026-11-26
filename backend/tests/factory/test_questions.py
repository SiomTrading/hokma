"""The nine questions to the owner (SPEC-032, OPT-07): the catalog, the rules of "does not apply", the suggestions, the answers by scope and the fields of
AuditEvidence. The strategies are saved specs of the factory and the runs are real runs of H4 over the bars of the NQ fixtures."""
import dataclasses
import inspect
from dataclasses import replace

import pytest
from backtest import api_support as api
from backtest import support
from backtest.runner_support import NQ, Fixed, source_of, spec as backtest_spec
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)
from factory.test_spec import text

from hokma import backtest
from hokma.backtest.optimize import Axis, Constraint, ParamSpace
from hokma.backtest.runner import Services
from hokma.factory import audit as au
from hokma.factory import questions as qs
from hokma.factory import spec as factory_spec
from hokma.factory.questions import QuestionError

pytestmark = requires_engine
EXPECTED = [("point_in_time", "data", 1), ("survivorship_free", "data", 2), ("corporate_actions", "data", 3), ("gaps_checked", "data", 5),
            ("in_sample_tuning", "strategy", 7), ("holdout_untouched", "strategy", 8), ("impact_modeled", "strategy", 11),
            ("borrow_costs_modeled", "strategy", 12), ("capacity_checked", "strategy", 14)]


def answers(db):
    with db.read() as u:
        return u.session.query("SELECT * FROM audit_answers ORDER BY id")


def prepare(db, tmp_path, strategy_id="cross", bars=None, **spec_over):
    """Save the factory spec of `strategy_id` and run it over `bars` (real NQ moves). Returns the services of the run."""
    bars = bars if bars is not None else api.series(300)
    factory_spec.save_draft(text(strategy_id=strategy_id), db=db)
    services = api.services(bars, db, tmp_path)
    assert services.run(api.spec_of(bars, strategy_id=strategy_id, **spec_over)).status == "done"
    return services


def by_key(states):
    return {s.key: s for s in states}


# ---- the catalog -------------------------------------------------------------------------------------------------------------------------

def test_the_catalog_has_the_nine_questions_in_order_with_their_scope_check_and_evidence_field():
    assert [(q.key, q.scope, q.check) for q in qs.QUESTIONS] == EXPECTED
    fields = {f.name for f in dataclasses.fields(au.AuditEvidence)}
    assert all(q.evidence_field in fields for q in qs.QUESTIONS)
    keys = {c.number: c.key for c in au.CHECKLIST}
    assert {q.check for q in qs.QUESTIONS} <= set(keys) and qs.ANSWERS == ("yes", "no", "na")


# ---- rules of "does not apply" -----------------------------------------------------------------------------------------------------------

def test_a_future_of_one_instrument_has_no_survivorship_question_and_the_rest_are_asked(db, tmp_path):
    prepare(db, tmp_path)
    got = qs.states("cross", 1, services=api.services(api.series(300), db, tmp_path))
    assert [s.key for s in got] == [q[0] for q in EXPECTED]                                    # one state each: a run holds one instrument
    states = by_key(got)
    assert states["survivorship_free"].status == "na" and states["survivorship_free"].na_reason == "single_instrument"
    assert states["survivorship_free"].answer is None and states["survivorship_free"].subject == NQ
    for key in ("point_in_time", "corporate_actions", "gaps_checked", "in_sample_tuning", "holdout_untouched", "capacity_checked", "impact_modeled"):
        assert states[key].status == "ask" and states[key].answer is None and states[key].answer_id is None and states[key].answered_at is None
    assert states["borrow_costs_modeled"].status == "na" and states["borrow_costs_modeled"].na_reason == "no_shorts_no_financing"   # the cross never goes short
    assert {states[k].subject for k in ("point_in_time", "corporate_actions", "gaps_checked")} == {NQ}                           # data: the contract
    assert {states[k].subject for k in ("in_sample_tuning", "holdout_untouched", "capacity_checked")} == {"cross@1"}             # strategy: id and version
    assert {states[k].scope for k in states} == {"data", "strategy"}


def test_a_short_trade_asks_for_the_borrow_costs(db, tmp_path):
    bars = support.bars_of("T1")
    factory_spec.save_draft(text(strategy_id="short_demo"), db=db)
    services = Services(db=db, source=source_of({NQ: bars}), curve_dir=tmp_path, strategy=lambda params: Fixed(side="sell"))
    assert services.run(backtest_spec(bars, strategy_id="short_demo")).status == "done"
    assert by_key(qs.states("short_demo", 1, services=services))["borrow_costs_modeled"].status == "ask"


def test_a_spec_with_an_impact_model_does_not_ask_about_impact(db, tmp_path):
    bars = support.bars_of("T1")
    factory_spec.save_draft(text(strategy_id="impact_demo"), db=db)
    services = Services(db=db, source=source_of({NQ: bars}), curve_dir=tmp_path, strategy=lambda params: Fixed())
    base = backtest_spec(bars, strategy_id="impact_demo")
    ran = services.run(replace(base, costs=replace(base.costs, impact="volume_share")))
    assert ran.status == "done", (ran.warnings, ran.stats)
    state = by_key(qs.states("impact_demo", 1, services=services))["impact_modeled"]
    assert state.status == "na" and state.na_reason == "impact_in_spec"
    with pytest.raises(QuestionError) as caught:
        qs.answer("impact_demo", 1, "impact_modeled", "yes", note=None, actor="owner", db=db)
    assert caught.value.code == "bad_input" and answers(db) == []


def test_no_saved_spec_or_no_finished_run_is_refused_with_its_code(db, tmp_path):
    with pytest.raises(QuestionError) as caught:
        qs.states("ghost", 1, services=api.services(api.series(60), db, tmp_path))
    assert caught.value.code == "no_spec"
    factory_spec.save_draft(text(strategy_id="unrun"), db=db)
    with pytest.raises(QuestionError) as caught:
        qs.states("unrun", 1, services=api.services(api.series(60), db, tmp_path))
    assert caught.value.code == "no_run"
    for bad in (("", 1), ("x" * 81, 1), ("cross", 0), ("cross", True), ("cross", "1")):
        with pytest.raises(QuestionError) as caught:
            qs.states(*bad, services=api.services(api.series(60), db, tmp_path))
        assert caught.value.code == "bad_input"


# ---- suggestions -------------------------------------------------------------------------------------------------------------------------

def test_suggestions_come_from_the_spec_the_series_and_the_bars_of_the_run(db, tmp_path):
    services = prepare(db, tmp_path)
    states = by_key(qs.states("cross", 1, services=services))
    assert (states["point_in_time"].suggestion, states["point_in_time"].suggestion_reason) == ("yes", "verdict.suggest.point_in_time.prices_only")
    assert (states["corporate_actions"].suggestion, states["corporate_actions"].suggestion_reason) == ("yes", "verdict.suggest.corporate_actions.continuous_series")
    assert (states["gaps_checked"].suggestion, states["gaps_checked"].suggestion_reason) == ("yes", "verdict.suggest.gaps_checked.no_bars_excluded")
    for key in ("in_sample_tuning", "holdout_untouched", "impact_modeled", "capacity_checked"):
        assert states[key].suggestion is None and states[key].suggestion_reason is None
    assert by_key(qs.states("cross", 1))["gaps_checked"].suggestion is None                       # without the services there are no bars to inspect


def test_the_walk_forward_choice_is_suggested_for_the_tuning_question_only_when_the_version_uses_it(db, tmp_path):
    bars = api.series(600)
    factory_spec.save_draft(text(strategy_id="cross"), db=db)
    services = api.services(bars, db, tmp_path)
    space = ParamSpace((Axis("fast", "int", 3, 5), Axis("slow", "int", 10, 30, 10)), (Constraint("fast", "lt", "slow"),))
    walked = backtest.walk_forward(api.spec_of(bars), space, scheme="rolling", train=100, test=100, purge=2, embargo=1, services=services)
    chosen = dict(walked.windows[-1].chosen)
    services.run(replace(api.spec_of(bars), params={"fast": 4, "slow": 15}))                      # a version that is not the choice of the last window
    assert by_key(qs.states("cross", 1, services=services))["in_sample_tuning"].suggestion is None
    services.run(replace(api.spec_of(bars), params=chosen))                                       # the latest run uses the choice
    state = by_key(qs.states("cross", 1, services=services))["in_sample_tuning"]
    assert (state.suggestion, state.suggestion_reason) == ("yes", "verdict.suggest.in_sample_tuning.walk_forward_choice")


def test_a_suggestion_is_not_an_answer_until_the_owner_gives_it(db, tmp_path):
    services = prepare(db, tmp_path)
    assert by_key(qs.states("cross", 1, services=services))["point_in_time"].suggestion == "yes" and answers(db) == []
    assert qs.evidence_fields(qs.states("cross", 1, services=services))["point_in_time"] is None


# ---- answers -----------------------------------------------------------------------------------------------------------------------------

def test_a_data_answer_is_for_the_contract_and_serves_another_strategy_of_the_same_contract(db, tmp_path):
    prepare(db, tmp_path, "cross")
    prepare(db, tmp_path, "cross_stop")
    done = qs.answer("cross", 1, "point_in_time", "yes", note="vendor sends no revisions", actor="owner", db=db)
    assert (done.status, done.answer, done.note, done.subject, done.scope) == ("answered", "yes", "vendor sends no revisions", NQ, "data")
    assert done.answer_id is not None and done.answered_at is not None and done.suggestion is None
    (row,) = answers(db)
    assert (row["scope"], row["subject"], row["question_key"], row["answer"], row["answered_by"]) == ("data", NQ, "point_in_time", "yes", "owner")
    other = by_key(qs.states("cross_stop", 1, services=api.services(api.series(300), db, tmp_path)))
    assert other["point_in_time"].status == "answered" and other["point_in_time"].answer == "yes" and other["point_in_time"].answer_id == done.answer_id
    assert other["corporate_actions"].status == "ask"


def test_a_strategy_answer_is_for_that_version_and_does_not_serve_another_strategy(db, tmp_path):
    prepare(db, tmp_path, "cross")
    prepare(db, tmp_path, "cross_stop")
    done = qs.answer("cross", 1, "holdout_untouched", "no", note=None, actor="owner", db=db)
    assert (done.status, done.answer, done.subject, done.scope, done.note) == ("answered", "no", "cross@1", "strategy", None)
    assert by_key(qs.states("cross_stop", 1))["holdout_untouched"].status == "ask"
    assert [r["subject"] for r in answers(db)] == ["cross@1"]                               # one row, for this version only


def test_changing_an_answer_is_a_new_row_and_the_last_one_is_in_force(db, tmp_path):
    prepare(db, tmp_path)
    first = qs.answer("cross", 1, "capacity_checked", "no", note=None, actor="owner", db=db)
    again = qs.answer("cross", 1, "capacity_checked", "no", note=None, actor="owner", db=db)
    assert again.answer_id == first.answer_id and len(answers(db)) == 1                         # the same answer and note write nothing
    second = qs.answer("cross", 1, "capacity_checked", "yes", note="trades 2 contracts of 90000 a day", actor="owner", db=db)
    assert second.answer == "yes" and second.answer_id != first.answer_id and len(answers(db)) == 2
    third = qs.answer("cross", 1, "capacity_checked", "yes", note="a different note", actor="owner", db=db)
    assert third.answer_id != second.answer_id and third.note == "a different note"
    rows = answers(db)
    assert [r["answer"] for r in rows] == ["no", "yes", "yes"] and rows[0]["id"] == first.answer_id                   # the old rows are as they were
    assert by_key(qs.states("cross", 1))["capacity_checked"].answer_id == third.answer_id
    with db.write() as u:
        with pytest.raises(Exception, match="append-only"):
            u.session.execute("UPDATE audit_answers SET answer = 'no'")


def test_an_answer_na_of_the_owner_is_answered_and_gives_no_evidence(db, tmp_path):
    prepare(db, tmp_path)
    done = qs.answer("cross", 1, "capacity_checked", "na", note="paper account", actor="owner", db=db)
    assert done.status == "answered" and done.answer == "na"
    assert qs.evidence_fields(qs.states("cross", 1))["capacity_checked"] is None


def test_a_wrong_question_value_note_or_actor_is_bad_input_and_nothing_is_written(db, tmp_path):
    prepare(db, tmp_path)
    calls = [("cross", 1, "nope", "yes", None, "owner"), ("cross", 1, "point_in_time", "maybe", None, "owner"), ("cross", 1, "point_in_time", True, None, "owner"),
             ("cross", 1, "point_in_time", "yes", "x" * 1001, "owner"), ("cross", 1, "point_in_time", "yes", 5, "owner"),
             ("cross", 1, "point_in_time", "yes", None, ""), ("cross", 1, "point_in_time", "yes", None, "Owner"), ("cross", 1, "point_in_time", "yes", None, None),
             ("cross", 1, "survivorship_free", "yes", None, "owner"), ("cross", 1, "borrow_costs_modeled", "yes", None, "owner"), ("cross", 0, "point_in_time", "yes", None, "owner")]
    for strategy_id, version, key, value, note, actor in calls:
        with pytest.raises(QuestionError) as caught:
            qs.answer(strategy_id, version, key, value, note=note, actor=actor, db=db)
        assert caught.value.code == "bad_input", (key, value, actor)
    assert answers(db) == []
    with pytest.raises(QuestionError) as caught:
        qs.answer("ghost", 1, "point_in_time", "yes", note=None, actor="owner", db=db)
    assert caught.value.code == "no_spec"
    assert inspect.signature(qs.answer).parameters["actor"].kind is inspect.Parameter.KEYWORD_ONLY


# ---- the fields of AuditEvidence -----------------------------------------------------------------------------------------------------------

def test_evidence_fields_say_yes_no_na_and_ask_as_true_false_and_none(db, tmp_path):
    prepare(db, tmp_path)
    everything = qs.evidence_fields(qs.states("cross", 1))
    assert list(everything) == [q.evidence_field for q in qs.QUESTIONS] and set(everything.values()) == {None}          # nothing answered: all unknown
    qs.answer("cross", 1, "point_in_time", "yes", note=None, actor="owner", db=db)
    qs.answer("cross", 1, "corporate_actions", "yes", note=None, actor="owner", db=db)
    qs.answer("cross", 1, "holdout_untouched", "no", note=None, actor="owner", db=db)
    qs.answer("cross", 1, "capacity_checked", "na", note=None, actor="owner", db=db)
    got = qs.evidence_fields(qs.states("cross", 1))
    assert got["point_in_time"] is True and got["corporate_actions"] is True and got["holdout_untouched"] is False
    assert got["capacity_checked"] is None                                  # na of the owner
    assert got["survivorship_free"] is None and got["borrow_costs_modeled"] is None     # na by rule
    assert got["gaps_checked"] is None and got["in_sample_tuning"] is None and got["impact_modeled"] is None      # still asked
    evidence = au.AuditEvidence(**{k: v for k, v in got.items()})
    assert evidence.point_in_time is True and evidence.holdout_untouched is False


def test_evidence_fields_of_several_contracts_are_false_on_any_no_none_while_asked_and_true_when_all_yes():
    def state(subject, status, answer):
        return qs.QuestionState("survivorship_free", subject, status, None, None, answer, None, None)

    field = lambda *states: qs.evidence_fields(list(states))["survivorship_free"]
    assert field(state("EQ:A", "answered", "yes"), state("EQ:B", "answered", "no")) is False
    assert field(state("EQ:A", "answered", "yes"), state("EQ:B", "ask", None)) is None
    assert field(state("EQ:A", "answered", "no"), state("EQ:B", "ask", None)) is False
    assert field(state("EQ:A", "answered", "yes"), state("EQ:B", "answered", "yes")) is True
    assert field(state("EQ:A", "answered", "yes"), state("EQ:B", "answered", "na")) is True
    assert field(state("EQ:A", "na", None)) is None and field() is None


def test_the_module_has_no_broker_path_no_statistic_and_no_long_dash():
    source = inspect.getsource(qs)
    for banned in ("hokma.exec", "hokma.broker", "numpy", "scipy", "pandas", "subprocess", "\u2014"):
        assert banned not in source, banned

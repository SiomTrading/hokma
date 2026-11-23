"""The cycle of a SWO desk (SPEC-019: B12, AH-22, section 4.3): levels 1, 2 and 3, the stops of the cycle and the learning after the fill."""
import json
from datetime import datetime, timezone

import pytest
from b12_support import AS_OF, Engines, GrantChat, Garbage, runtime
from exec_support import db, desks_db, env, ready  # noqa: F401  (fixtures)
from trader_support import C200, C210, answer_json, sheet

from hokma import exec as hexec
from hokma.backoffice import runs
from hokma.broker import service
from hokma.contracts import sheet as sheet_store
import pandas as pd
from b7_support import ScriptedChat

from hokma.desks.agents import manager as mg
from hokma.desks.core.state import Message
from hokma.desks.swo import regime
from hokma.desks.swo.create import create_swo_desk, desk_agent_slug
from hokma.desks.swo.graph import NODES, CycleError, CycleResult, Mark, learn_after_fill, run_cycle
from hokma.desks.plantel import SEATS
from hokma.desks.core.graph import ModelReply
from hokma.exec import killswitch, levels


@pytest.fixture
def swo(ready):
    """A real SWO desk (the real kit) on its own account of the simulated broker, with the two option sheets and the quotes of the chain."""
    desk = create_swo_desk("Options swing", 1_000_000, db=ready.db)
    ready.swo_id = desk["id"]
    ready.agent = desk_agent_slug(SEATS["A11"], ready.swo_id)
    with ready.db.write() as u:
        account = u.broker.create_account(ready.broker_id, "Sim B", 1_000_000)
        u.broker.link_desk(ready.swo_id, account.id)
        ready.swo_account = account.id
    service.set_limits(ready.swo_id, {"max_premium_usd": 5000, "max_drawdown_pct": 10}, db=ready.db)
    killswitch.beat(ready.swo_id, db=ready.db)                      # the emergency switch has measured the desk
    for cid in (C200, C210):
        sheet_store.save(sheet(cid), db=ready.db)
    ready.board.feed(C200, bid="5.10", ask="5.20", last="5.15")
    ready.board.feed(C210, bid="1.80", ask="1.90", last="1.85")
    ready.board.feed("EQ:AAPL", bid="199.90", ask="200.00", last="199.95")
    return ready


def rows(world, sql, **p):
    with world.db.read() as u:
        return u.session.query(sql, p)


def cycle(r, engines=None, **kw):
    engines = engines or Engines()
    rt = runtime(r, engines, agent=r.agent, **kw.pop("rt", {}))
    return run_cycle(r.swo_id, symbol="AAPL", as_of=AS_OF, runtime=rt, db=r.db, **kw), engines


def record(r, result):
    return runs.get(result.run_id, db=r.db)["record"]["outputs"]


def test_AH_22_level_1_the_mesa_only_informs_the_proposal_is_analysis_only_and_no_order_leaves(swo):
    result, engines = cycle(swo)
    assert isinstance(result, CycleResult) and result.exec_status == "analysis_only", (result, record(swo, result)["delivery"])
    assert result.rating == "APPROVE" and result.proposal_id and result.stopped_at is None
    assert rows(swo, "SELECT * FROM sim_orders") == [] and rows(swo, "SELECT * FROM broker_orders WHERE action = 'place' AND rule_result = 'auto'") == []
    out = record(swo, result)
    assert out["trail"][:3] == ["plan", "analysts", "company_filter"] and out["trail"][-1] == "close" and out["final"]["level"] == 1
    assert set(result.absent) == {"market", "news", "fundamentals", "options"}      # their reports did not arrive and the cycle went on


def test_AH_22_level_2_the_proposal_reaches_the_owner_and_what_is_approved_executes_on_the_simulated_broker(swo):
    levels.set_level(swo.swo_id, 2, who=swo.owner, db=swo.db)
    result, engines = cycle(swo)
    assert result.exec_status == "awaiting_owner" and result.learning is None
    assert rows(swo, "SELECT * FROM sim_orders") == []                          # nothing left before the owner said yes
    done = hexec.approve(result.proposal_id, who=swo.owner, db=swo.db)
    assert done.status in ("filled", "sent"), done
    fills = rows(swo, "SELECT * FROM sim_fills")
    assert len(fills) == 1 and fills[0]["contract_id"] == C200
    # step 9: measured from the price the broker really filled at, never from a close
    mark = Mark(price=9.0, at="2099-01-01 00:00:00", horizon_days=5, multiplier=100.0)
    rt = runtime(swo, engines, agent=swo.agent, mark=mark)
    learning = learn_after_fill(swo.swo_id, result.run_id, runtime=rt, db=swo.db)
    assert learning.status == "reflected", learning
    outcome = learning.settled.outcome
    assert outcome["entry_price"] == float(fills[0]["price"]) and outcome["entry_price"] != 9.0
    assert learning.settled.measured_from and outcome["exit_basis"] == "mark"
    reflection = learning.reflection.written[0]
    assert reflection.status == "settled" and reflection.proposed_by == "desk-manager" and reflection.order_id == learning.settled.order_id
    again = learn_after_fill(swo.swo_id, result.run_id, runtime=rt, db=swo.db)           # the same order is the same lessons
    assert again.settled.id == learning.settled.id and len(rows(swo, "SELECT * FROM lessons")) == 2


def test_AH_22_level_3_sends_alone_under_the_rules_the_owner_approved_and_the_lessons_follow_the_fill(swo):
    levels.set_level(swo.swo_id, 3, rules={"id": "r1", "max_qty": 50, "symbols": ["OPT:AAPL"]}, who=swo.owner, db=swo.db)
    mark = Mark(price=9.0, at="2099-01-01 00:00:00", horizon_days=5, multiplier=100.0)
    result, _ = cycle(swo, rt={"mark": mark})
    assert result.exec_status in ("filled", "sent") and result.learning == "reflected", result
    proposal = rows(swo, "SELECT * FROM exec_proposals WHERE id = :i", i=result.proposal_id)[0]
    assert proposal["approved_by"] == "rule:r1" and proposal["origin"] == "desk" and proposal["level"] == 3
    fills = rows(swo, "SELECT * FROM sim_fills")
    lessons = rows(swo, "SELECT * FROM lessons ORDER BY id")
    assert len(fills) == 1 and len(lessons) == 2 and all(row["status"] == "settled" for row in lessons)
    assert json.loads(lessons[0]["outcome_json"])["entry_price"] == float(fills[0]["price"])
    decision = rows(swo, "SELECT status, decided_by FROM decisions WHERE id = :i", i=lessons[0]["decision_id"])[0]
    assert decision["status"] == "approved" and decision["decided_by"] == "rule:r1"       # layer 2 of the decision log


def test_AH_22_the_cycle_never_calls_a_broker_and_hands_the_proposal_to_exec_propose_only(swo, monkeypatch):
    calls = []
    from hokma.desks.agents import trader as trader_module

    real = trader_module.deliver

    def spy(box, desk_id, result):
        calls.append(box.names())
        return real(box, desk_id, result)

    monkeypatch.setattr("hokma.desks.swo.graph.deliver", spy)
    result, _ = cycle(swo)
    assert calls == [("exec_propose", "exec_status", "contract_get")] and "order_place" not in calls[0] and result.exec_status == "analysis_only"
    import hokma.desks.swo.graph as graph_module
    import inspect

    source = inspect.getsource(graph_module)
    for forbidden in ("place_order", "order_place", "dispatch(", "open_broker", "preview_order"):
        assert forbidden not in source, forbidden


def test_AH_22_the_nine_steps_are_nodes_of_the_core_graph_and_every_step_leaves_a_checkpoint(swo):
    result, _ = cycle(swo)
    out = record(swo, result)
    assert NODES == ("plan", "analysts", "company_filter", "debate", "judge", "trader", "committee", "manager", "learn", "close")
    assert out["trail"] == list(NODES[:7]) + ["manager", "learn", "close"]
    saved = rows(swo, "SELECT node FROM run_checkpoints WHERE run_id = :r ORDER BY node", r=result.run_id)
    assert [r["node"].split(":")[1] for r in saved] == list(NODES[:9])
    layer1 = runs.list_runs(swo.swo_id, kind="desk_debate", db=swo.db)
    assert len(layer1) == 2 and layer1[-1]["record"]["outputs"]["debate"]["rounds"] == 1       # the raw debate, then the judge record


def test_AH_22_a_failed_step_ends_the_cycle_with_no_proposal_and_a_failed_record(swo):
    engines = Engines()
    rt = runtime(swo, engines, agent=swo.agent)
    rt.load = lambda desk_id, symbol, as_of: (_ for _ in ()).throw(RuntimeError("the data source is down"))
    result = run_cycle(swo.swo_id, symbol="AAPL", as_of=AS_OF, runtime=rt, db=swo.db)
    assert result.proposal_id is None and result.reason == "error:analysts:RuntimeError" and result.stopped_at == "analysts"
    assert rows(swo, "SELECT status FROM run_records WHERE id = :i", i=result.run_id)[0]["status"] == "failed"
    assert rows(swo, "SELECT * FROM exec_proposals") == []


@pytest.mark.parametrize("engines, reason", [
    (lambda: Engines(sam=False), "no_analyst_report"),
    (lambda: Engines(judge_rating="REJECT"), "judge:REJECT"),
    (lambda: Engines(judge_rating="REVIEW"), "judge:REVIEW"),
    (lambda: Engines(risk_level="severe"), "committee_severe"),
])
def test_AH_22_a_cycle_that_the_steps_stop_ends_with_a_report_and_no_proposal(swo, engines, reason):
    result, _ = cycle(swo, engines())
    assert result.reason == reason and result.proposal_id is None and result.exec_status is None and result.learning is None
    assert rows(swo, "SELECT * FROM exec_proposals") == [] and rows(swo, "SELECT * FROM sim_orders") == []
    out = record(swo, result)
    assert out["reason"] == reason and out["stopped_at"] == result.stopped_at and out["orders"] == []


def test_AH_22_a_gate_that_does_not_approve_turns_the_judgment_into_review(swo):
    from b7_support import ScriptedChat

    weak = ScriptedChat(ModelReply(json.dumps({"scores": {k: 0.1 for k in ("plausibility", "overfitting", "sample", "regime", "exit", "concentration", "execution_realism", "invalidation")}, "issues": ["thin"]})))
    ok = ScriptedChat(ModelReply(__import__("b12_support").REVIEW))
    result, _ = cycle(swo, Engines(reviewers=(weak, ok)))
    assert result.reason == "judge:REVIEW" and result.proposal_id is None
    assert record(swo, result)["judge"]["gate"] == "REVIEW"


def test_AH_22_two_reviewers_on_the_same_model_are_not_a_gate(swo):
    from b7_support import ScriptedChat

    same = ScriptedChat(ModelReply(__import__("b12_support").REVIEW))
    result, _ = cycle(swo, Engines(reviewers=(same, same)))
    assert result.reason == "reviewers_not_separate" and result.proposal_id is None


def test_AH_22_a_company_the_filter_rejects_does_not_reach_the_debate(swo):
    result, engines = cycle(swo, Engines(), rt={"price": 400})                     # the same company at a price far above its value
    assert result.reason == "filter:rejected_net_bearish" and result.stopped_at == "company_filter"
    assert engines.models["A08"].calls == [] and engines.models["A10"].calls == []         # no debate, no judge
    assert len(rows(swo, "SELECT * FROM desk_signals WHERE run_id = :r", r=result.run_id)) == 7


def test_TA_28_an_analyst_that_is_off_is_absent_and_the_cycle_goes_on(swo):
    from hokma.desks.agents.manager import load_desk_mandate, save_mandate

    data = load_desk_mandate(swo.swo_id, db=swo.db).model_dump(mode="json")
    data["analysts"] = [{"key": "sentiment"}, {"key": "news", "enabled": False}]
    save_mandate(swo.swo_id, data, by_owner=True, db=swo.db)
    result, engines = cycle(swo)
    assert result.proposal_id and result.absent == ("news",)
    assert engines.models["A04"].__class__.__name__ == "Garbage"                              # Tim was never asked
    assert record(swo, result)["analyst_status"] == {"sentiment": "ok"}


def test_TA_13_grant_rates_after_the_committee_and_the_report_stays_in_the_run_record(swo):
    result, engines = cycle(swo)
    final = record(swo, result)["final"]
    assert final["rating"] == "APPROVE" and final["thesis"] and final["would_change"] and final["horizon"] == "weeks" and final["level"] == 1
    assert final["valid"] is True
    # Grant reports the level of the desk and never changes it
    assert levels.get_level(swo.swo_id, db=swo.db)[0] == 1


@pytest.mark.parametrize("patch, reason", [
    ({"rating": "REJECT"}, "manager_rejected"),
    ({"desk_level": 3}, "manager_invalid"),
    ({"level_change": 3}, "manager_invalid"),
])
def test_TA_13_a_final_rating_that_does_not_validate_or_rejects_sends_nothing(swo, patch, reason):
    result, _ = cycle(swo, Engines(grant=GrantChat(final=patch)))
    assert result.reason == reason and result.proposal_id is None
    assert rows(swo, "SELECT * FROM exec_proposals") == []


def test_TA_13_grant_cannot_rate_above_the_judge(swo):
    engines = Engines(judge_rating="APPROVE_WITH_CONDITIONS", judge_over={"conditions": ["Go only while the catalyst date holds"]},
                      grant=GrantChat(final={"rating": "APPROVE"}))
    result, _ = cycle(swo, engines)
    assert result.reason == "manager_invalid" and result.proposal_id is None


def test_ECT_042_premium_selling_is_built_but_the_cycle_does_not_send_it_while_the_switch_is_closed(swo):
    from trader_support import P170, P180

    for cid in (P170, P180):
        sheet_store.save(sheet(cid), db=swo.db)
    short_put = answer_json(structure="short_put", legs=[{"contract_id": P180, "side": "sell"}], thesis=__import__("b12_support").THESIS)
    result, _ = cycle(swo, rt={"trader_answer": short_put})
    assert result.proposal_id is None and result.exec_status is None
    assert result.reason in ("premium_selling_closed",) or result.reason.startswith("trader:"), result.reason
    assert rows(swo, "SELECT * FROM exec_proposals") == []


def test_SKL_009_a_proposal_the_owner_rejected_is_not_opened_again_without_his_order(swo):
    levels.set_level(swo.swo_id, 2, who=swo.owner, db=swo.db)
    first, _ = cycle(swo)
    hexec.reject(first.proposal_id, who=swo.owner, db=swo.db)
    again, _ = cycle(swo)
    assert again.reason == "rejected_before" and again.proposal_id is None
    ordered, _ = cycle(swo, owner_order=True)
    assert ordered.proposal_id is not None and ordered.exec_status == "awaiting_owner"


def test_AH_22_a_desk_that_is_not_swo_and_a_missing_runtime_are_refused(swo):
    with pytest.raises(CycleError) as exc:
        run_cycle(swo.desk_id, symbol="AAPL", as_of=AS_OF, runtime=runtime(swo, Engines()), db=swo.db)
    assert exc.value.code == "not_swo"
    with pytest.raises(CycleError) as missing:
        run_cycle(swo.swo_id, symbol="AAPL", as_of=AS_OF, db=swo.db)
    assert missing.value.code == "no_runtime"


def test_AH_22_the_regime_for_ray_is_derived_in_the_cycle_from_svc_stats_and_is_never_written_by_an_agent(swo):
    result, _ = cycle(swo)
    out = record(swo, result)
    assert out["regime"] in regime.REGIMES and out["regime_facts"]["sources"] == ["stats.yang_zhang", "stats.hurst"]
    assert 0.0 <= out["regime_facts"]["volatility_rank"] <= 1.0 and out["regime_facts"]["last_bar"] < AS_OF.date().isoformat()     # closed bars only
    ray = rows(swo, "SELECT abstained, signal_json FROM desk_signals WHERE run_id = :r AND seat_code = 'A20'", r=result.run_id)[0]
    assert ray["abstained"] == 0 and "data_missing:regime" not in ray["signal_json"]


def test_AH_22_a_regime_that_cannot_be_read_is_none_and_never_neutral(monkeypatch):
    from b12_support import shifted_candles

    assert regime.derive_regime(shifted_candles().head(50), AS_OF)[0] is None
    assert regime.derive_regime(None, AS_OF)[1]["reason"] == "not_enough_closed_bars"
    full = shifted_candles()

    class Boom:
        def call(self, *a, **k):
            raise RuntimeError("down")

        def load_all(self):
            return None

    monkeypatch.setattr(regime, "registry", Boom())
    label, facts = regime.derive_regime(full, AS_OF)
    assert label is None and facts["reason"].startswith("stats_call_failed")


@pytest.mark.parametrize("latest, reading, expected", [(0.09, "persistent", "hostile"), (0.01, "persistent", "favorable"), (0.01, "mean_reverting", "neutral"),
                                                     (0.05, "persistent", "neutral")])
def test_AH_22_the_regime_rule_ranks_the_latest_volatility_inside_its_own_history(monkeypatch, latest, reading, expected):
    from types import SimpleNamespace

    from b12_support import shifted_candles

    history = [0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.025]

    class Stats:
        def load_all(self):
            return None

        def call(self, name, **kw):
            if name == "stats.yang_zhang":
                return SimpleNamespace(value={"latest": latest, "values": history})
            return SimpleNamespace(value={"hurst": 0.6, "reading": reading})

    monkeypatch.setattr(regime, "registry", Stats())
    label, facts = regime.derive_regime(shifted_candles(), AS_OF)
    assert label == expected and "volatility_rank" in facts


def test_CTS_024_the_views_of_an_executed_cycle_come_from_its_records_and_never_from_one_that_did_not_leave(swo):
    levels.set_level(swo.swo_id, 3, rules={"id": "r1", "max_qty": 50, "symbols": ["OPT:AAPL"]}, who=swo.owner, db=swo.db)
    sent, _ = cycle(swo)
    levels.set_level(swo.swo_id, 1, who=swo.owner, db=swo.db)
    held, _ = cycle(swo)                                                       # level 1: analysis only, its views never leave
    assert sent.exec_status in ("filled", "sent") and held.exec_status == "analysis_only"
    views, left_out = mg.executed_views(swo.swo_id, db=swo.db)
    assert {v.order_id for v in views} == {rows(swo, "SELECT broker_order_record_id AS b FROM exec_proposals WHERE id = :i", i=sent.proposal_id)[0]["b"]}
    assert {"A16", "A17", "A18", "A19", "A20", "A21"} <= {v.source for v in views} <= {"A16", "A17", "A18", "A19", "A20", "A21", "A22"} and all(v.bias == 1 and v.entry_price == 199.95 for v in views)
    assert any(f"proposal {held.proposal_id}" in w for w in left_out)
    closes = pd.Series([199.95 + i * 0.5 for i in range(60)], index=pd.bdate_range(views[0].entry_at.date(), periods=60))
    expected = mg.classify_views(views, closes={"AAPL": closes}, as_of=datetime(2027, 3, 1, tzinfo=timezone.utc))
    assert {s["dominant_class"] for s in expected.sources} == {"true_positive", "missed_opportunity"}      # the profiles that stayed neutral missed the gain
    model = ScriptedChat(ModelReply(json.dumps({"summary": "The sources split between right and neutral.", "raw_return_notice": "These are raw price returns from the entry.",
                                                "sources": [{"source": s["source"], "views": s["views"], "dominant_class": s["dominant_class"], "weight_change": "none",
                                                             "text": "Read as the table says."} for s in expected.sources],
                                                "improvements": [], "left_out": [], "data_gaps": []})))
    table, result = mg.postmortem(model, desk_id=swo.swo_id, as_of=datetime(2027, 3, 1, tzinfo=timezone.utc), views=views, closes={"AAPL": closes}, db=swo.db)
    assert result.valid, result.check.problems
    assert {s["source"] for s in table.sources} == {v.source for v in views} and all(s["views"] == 1 for s in table.sources)


def test_AH_22_the_runtime_comes_from_a_factory_the_app_registers_and_a_client_model_answers_as_a_chat_model(swo):
    from hokma.desks.swo import graph as graph_module

    engines = Engines()
    graph_module.register_runtime_factory(lambda desk_id: runtime(swo, engines, agent=swo.agent))
    try:
        result = run_cycle(swo.swo_id, symbol="AAPL", as_of=AS_OF, db=swo.db)
    finally:
        graph_module.register_runtime_factory(None)
    assert result.exec_status == "analysis_only"

    class Client:
        model = "fake"

        def __init__(self):
            self.seen = []

        def complete(self, messages, *, schema=None, max_tokens=4096):
            self.seen.append(messages)
            return type("R", (), {"text": "an answer"})()

    client = Client()
    reply = graph_module.ClientChat(client).chat("be brief", [Message("user", "hello"), Message("assistant", "hi"), Message("tool", "42", name="quant_call")])
    assert reply.content == "an answer" and reply.tool_calls == ()
    assert client.seen[0][0] == {"role": "system", "content": "be brief"} and client.seen[0][-1]["content"] == "Tool result (quant_call): 42"

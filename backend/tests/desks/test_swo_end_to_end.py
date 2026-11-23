"""SWO mesa end to end (SPEC-019 section 8, criteria 2 to 7): the owner creates the desk with the tool, a cycle runs on a fake LLM engine and
recorded data, and each level does what the spec says. Nothing here reaches the network or a real broker."""
import json

import pytest
from b12_support import AS_OF, Engines, runtime
from exec_support import db, desks_db, env, ready  # noqa: F401  (fixtures)
from trader_support import C200, C210, P190, answer_json, sheet

from hokma import exec as hexec
from hokma.backoffice import runs
from hokma.broker import service
from hokma.contracts import sheet as sheet_store
from hokma.desks.plantel import SEATS
from hokma.desks.swo import graph, premium_switch
from hokma.desks.swo.create import desk_agent_slug
from hokma.exec import killswitch, levels
from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller


def call(name, args, agent=None):
    text, err = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": agent} if agent else {}))
    try:
        return json.loads(text), err
    except ValueError:
        return text, err


def rows(world, sql, **p):
    with world.db.read() as u:
        return u.session.query(sql, p)


@pytest.fixture
def swo(ready):
    """A SWO desk made by the tool of the owner, on its own account of the simulated broker, with the chain quotes and a runtime on fake engines."""
    out, err = call("desk_create_swo", {"name": "Options swing", "capital": 1_000_000, "owner_approved": True})
    assert not err, out
    ready.swo_id = out["desk_id"]
    ready.created = out
    ready.agent = desk_agent_slug(SEATS["A11"], ready.swo_id)
    with ready.db.write() as u:
        account = u.broker.create_account(ready.broker_id, "Sim B", 1_000_000)
        u.broker.link_desk(ready.swo_id, account.id)
    service.set_limits(ready.swo_id, {"max_premium_usd": 5000, "max_drawdown_pct": 10}, db=ready.db)
    killswitch.beat(ready.swo_id, db=ready.db)
    for cid in (C200, C210, P190):
        sheet_store.save(sheet(cid), db=ready.db)
    ready.board.feed(C200, bid="5.10", ask="5.20", last="5.15")
    ready.board.feed(C210, bid="1.80", ask="1.90", last="1.85")
    ready.board.feed(P190, bid="2.40", ask="2.50", last="2.45")
    ready.board.feed("EQ:AAPL", bid="199.90", ask="200.00", last="199.95")
    ready.engines, ready.mark, ready.trader_answer = Engines(), None, None
    graph.register_runtime_factory(lambda desk_id: runtime(ready, ready.engines, agent=ready.agent, mark=ready.mark, trader_answer=ready.trader_answer))
    yield ready
    graph.register_runtime_factory(None)


def cycle(r):
    out, err = call("desk_cycle_run", {"desk_id": r.swo_id, "symbol": "AAPL", "as_of": AS_OF.isoformat()})
    assert not err, out
    return out


def test_criterion_2_the_owner_creates_the_desk_with_the_tool_and_an_agent_cannot(swo):
    out = swo.created
    assert out["line"] == "SWO" and out["variant"] == "traditional" and out["level"] == 1 and len(out["seats"]) == 20 and "A23" not in {s["seat_code"] for s in out["seats"]}
    assert out["mandate_approved_by_owner"] is True and {"A11", "A15"} <= {s["seat_code"] for s in out["seats"]}
    assert rows(swo, "SELECT level FROM desk_autonomy WHERE desk_id = :d", d=swo.swo_id)[0]["level"] == 1
    assert rows(swo, "SELECT approved_by FROM desk_mandates WHERE desk_id = :d", d=swo.swo_id)[0]["approved_by"] == "owner"
    for agent in (swo.agent, "desk-chief"):
        refused, err = call("desk_create_swo", {"name": "Another desk", "capital": 5000}, agent=agent)
        assert err and refused["error_code"] == "blocked"
    assert [d["name"] for d in rows(swo, "SELECT name FROM desks WHERE name = 'Another desk'")] == []
    again, err = call("desk_create_swo", {"name": "Options swing", "capital": 5000})
    assert err and "already exists" in again["message"]                                    # a repeated request does not leave two desks


def test_criterion_2_N1_the_mesa_only_informs_and_no_order_row_is_written(swo):
    out = cycle(swo)
    assert out["exec_status"] == "analysis_only" and out["rating"] == "APPROVE" and out["proposal_id"] and out["stopped_at"] is None
    assert rows(swo, "SELECT * FROM broker_orders WHERE action = 'place'") == [] and rows(swo, "SELECT * FROM sim_orders") == []
    status, err = call("desk_cycle_status", {"desk_id": swo.swo_id, "run_id": out["run_id"]})
    assert not err and status["cycle"]["trail"][-1] == "close" and status["cycle"]["proposal_ids"] == [out["proposal_id"]]
    latest, err = call("desk_cycle_status", {"desk_id": swo.swo_id})
    assert not err and [c["run_id"] for c in latest["cycles"]] == [out["run_id"]]
    signals, err = call("desk_signals", {"desk_id": swo.swo_id, "symbol": "AAPL"})
    assert not err and len(signals["signals"]) == 7 and all(s["run_id"] == out["run_id"] for s in signals["signals"])


def test_criterion_3_N2_the_owner_approves_and_the_fill_happens_in_the_simulated_broker(swo):
    levels.set_level(swo.swo_id, 2, who=swo.owner, db=swo.db)
    out = cycle(swo)
    assert out["exec_status"] == "awaiting_owner" and out["learning"] is None
    pending, err = call("exec_pending", {"desk_id": swo.swo_id}, agent=swo.agent)
    assert not err and len(pending["pending"]) == 1
    assert rows(swo, "SELECT * FROM sim_orders") == []                                       # nothing left before the owner said yes
    done = hexec.approve(out["proposal_id"], who=swo.owner, db=swo.db)
    assert done.status in ("filled", "sent"), done
    fills = rows(swo, "SELECT * FROM sim_fills")
    assert len(fills) == 1 and fills[0]["contract_id"] == C200
    swo.mark = graph.Mark(price=9.0, at="2099-01-01 00:00:00", horizon_days=5, multiplier=100.0)
    learned, err = call("desk_cycle_learn", {"desk_id": swo.swo_id, "run_id": out["run_id"]})
    assert not err and learned["status"] == "reflected", learned
    lessons = rows(swo, "SELECT * FROM lessons ORDER BY id")
    assert len(lessons) == 2 and json.loads(lessons[0]["outcome_json"])["entry_price"] == float(fills[0]["price"])   # measured from the real entry fill


def test_criterion_4_N3_a_call_inside_the_rules_goes_alone_and_outside_them_asks_the_owner(swo):
    levels.set_level(swo.swo_id, 3, rules={"id": "r1", "max_qty": 50, "symbols": ["OPT:AAPL"]}, who=swo.owner, db=swo.db)
    swo.mark = graph.Mark(price=9.0, at="2099-01-01 00:00:00", horizon_days=5, multiplier=100.0)
    inside = cycle(swo)
    assert inside["exec_status"] in ("filled", "sent") and inside["learning"] == "reflected"
    proposal = rows(swo, "SELECT * FROM exec_proposals WHERE id = :i", i=inside["proposal_id"])[0]
    assert proposal["approved_by"] == "rule:r1" and proposal["origin"] == "desk" and proposal["level"] == 3
    levels.set_level(swo.swo_id, 3, rules={"id": "r2", "max_qty": 50, "symbols": ["OPT:MSFT"]}, who=swo.owner, db=swo.db)
    swo.engines = Engines()
    outside = call("desk_cycle_run", {"desk_id": swo.swo_id, "symbol": "AAPL", "as_of": AS_OF.replace(hour=16).isoformat()})[0]
    assert outside["exec_status"] == "awaiting_owner"                                         # outside the rules of the owner: it asks him
    assert len(rows(swo, "SELECT * FROM sim_orders")) == 1                                    # only the order inside the rules ever left


def test_criterion_5_premium_selling_is_built_but_the_code_refuses_it_while_no_threshold_exists(swo):
    assert rows(swo, "SELECT * FROM premium_thresholds") == []                                # only H4 writes it
    assert premium_switch.switch_state(swo.swo_id, "AAPL").state == "closed"
    body = {"line": "SWO", "order_type": "limit", "limit_price": "2.40", "thesis": "sell premium for the credit", "structure": "short_put",
            "expiration": "2026-12-18", "net_premium": "-2.40", "max_loss_usd": "18760", "underlying": "AAPL",
            "legs": [{"contract_id": P190, "side": "sell", "effect": "open", "qty": 1}]}
    refused, err = call("exec_propose", {"desk_id": swo.swo_id, "proposal": body}, agent=swo.agent)
    assert err and refused["error_code"] == "refused" and "premium_selling_closed" in refused["message"]
    swo.trader_answer = answer_json(structure="short_put", legs=[{"contract_id": P190, "side": "sell"}], stop_basis="premium",
                                    thesis="The view is a bounded bounce, so selling a put collects the premium of a quiet drift.")
    out = cycle(swo)
    assert out["reason"] == "premium_selling_closed" and out["stopped_at"] == "trader" and out["proposal_id"] is None
    assert rows(swo, "SELECT * FROM sim_orders") == [] and rows(swo, "SELECT * FROM premium_thresholds") == []


def test_criterion_6_the_tools_never_give_the_agent_a_way_to_send_an_order_or_open_the_switch(swo):
    names = set(hokma_tools.TOOLS)
    assert {"desk_create_swo", "desk_cycle_run", "desk_cycle_status", "desk_signals", "lessons_propose"} <= names
    from hokma.kit import desk_tools

    for seat in SEATS.values():
        assert "order_place" not in seat.tools
    import inspect

    source = inspect.getsource(desk_tools)
    for forbidden in ("order_place", "record_calibration", "premium_thresholds", "dispatch(", "place_order"):
        assert forbidden not in source, forbidden
    for bad in ({"desk_id": swo.swo_id, "symbol": "AAPL", "as_of": "2999-01-01"}, {"desk_id": swo.swo_id, "symbol": "../etc"},
                {"desk_id": True, "symbol": "AAPL"}):
        out, err = call("desk_cycle_run", bad)
        assert err, bad


def test_criterion_7_lessons_come_only_from_orders_that_executed(swo):
    levels.set_level(swo.swo_id, 2, who=swo.owner, db=swo.db)
    out = cycle(swo)
    rejected = hexec.reject(out["proposal_id"], who=swo.owner, db=swo.db)
    assert rejected.status == "canceled"
    grant = desk_agent_slug(SEATS["A15"], swo.swo_id)
    row = rows(swo, "SELECT decision_id, broker_order_record_id FROM exec_proposals WHERE id = :i", i=out["proposal_id"])[0]
    answer, err = call("lessons_propose", {"desk_id": swo.swo_id, "items": [
        {"decision_id": row["decision_id"], "order_id": row["broker_order_record_id"], "text": "The entry looked right."}]}, agent=grant)
    assert not err and answer["written"] == [] and answer["rejected"][0]["order_id"] == row["broker_order_record_id"]
    assert "NotExecuted" in answer["rejected"][0]["why"] and rows(swo, "SELECT * FROM lessons") == []
    for bad in ([], [{"decision_id": 1, "order_id": 1}], [{"decision_id": 1, "order_id": 1, "text": "x", "extra": 1}], "text"):
        assert call("lessons_propose", {"desk_id": swo.swo_id, "items": bad}, agent=grant)[1]


def test_criterion_2_the_cycle_record_stays_readable_and_a_desk_of_another_line_is_not_a_swo_desk(swo):
    out = cycle(swo)
    assert runs.get(out["run_id"], db=swo.db)["kind"] == graph.KIND
    nope, err = call("desk_cycle_status", {"desk_id": swo.desk_id})
    assert err and nope["error_code"] == "bad_input"
    missing, err = call("desk_cycle_status", {"desk_id": swo.swo_id, "run_id": 999999})
    assert err and missing["error_code"] == "no_data"

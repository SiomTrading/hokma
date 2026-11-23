"""The tools of Isaac (SPEC-019 OA-51): exec_propose, exec_status and contract_get, no order_place, the thesis as the message,
analysis_only at level 1 and awaiting_owner at level 2."""
import json
from decimal import Decimal as D

import pytest
from exec_support import db, desks_db, env, ready  # noqa: F401
from trader_support import C200, C210, answer_json, context, sheet, sheets

from hokma.contracts import sheet as sheet_store
from hokma.desks.agents import trader
from hokma.desks.agents.trader import TraderError, deliver, mcp_handlers, parse_answer, plan_trade, trader_toolbox
from hokma.desks.core.tools import ToolNotAllowed
from hokma.desks.plantel import SEATS
from hokma.exec import levels
from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller

SEAT = SEATS["A11"]


def fake_handlers(log):
    def make(name):
        def handler(**kw):
            log.append((name, kw))
            return {"status": "analysis_only", "code": "level_1", "why": "", "proposal_id": 9}
        return handler
    return {name: make(name) for name in SEAT.tools}


def test_OA_51_isaac_gets_exactly_the_three_tools_of_his_seat_and_no_order_place():
    assert SEAT.tools == ("exec_propose", "exec_status", "contract_get")
    box = trader_toolbox(fake_handlers([]))
    assert box.names() == ("exec_propose", "exec_status", "contract_get") and "order_place" not in box.names()
    for name in ("order_place", "exec_flatten", "desk_level_set", "quant_call", "exec_pending"):
        with pytest.raises(ToolNotAllowed):
            box.call(name, {})
    for spec in box.specs():
        assert spec.description and spec.input_schema["type"] == "object"
    assert [s.mutates for s in box.specs()] == [True, False, False]


def test_OA_51_a_handler_for_a_tool_outside_the_seat_is_refused_when_the_toolbox_is_built():
    handlers = {**fake_handlers([]), "order_place": lambda **kw: None}
    with pytest.raises(TraderError) as exc:
        trader_toolbox(handlers)
    assert exc.value.code == "tool_not_allowed"
    with pytest.raises(TraderError) as missing:
        trader_toolbox({"exec_propose": lambda **kw: None})
    assert missing.value.code == "tool_missing"


def test_OA_51_the_thesis_travels_as_the_message_of_the_proposal_through_exec_propose_only():
    log = []
    result = plan_trade(parse_answer(answer_json()), context())
    out = deliver(trader_toolbox(fake_handlers(log)), 7, result)
    assert (out.status, out.proposal_id) == ("analysis_only", 9) and [n for n, _ in log] == ["exec_propose"]
    body = log[0][1]["proposal"]
    assert body["thesis"] == result.thesis and body["structure"] == "debit_call_spread" and body["net_premium"] == "3.4"
    assert not {"desk_id", "origin", "created_by"} & set(body) and log[0][1]["desk_id"] == 7
    assert deliver(trader_toolbox(fake_handlers(log)), 7, plan_trade(parse_answer(json.dumps({"action": "stand_aside", "reason": "no"})), context())).status == "none"


def test_OA_51_a_refusal_of_the_door_comes_back_classified():
    from hokma.desks.core.tools import ToolFailure

    def refuse(**kw):
        raise ToolFailure("blocked", "refused: premium_selling_closed: closed")

    handlers = {**fake_handlers([]), "exec_propose": refuse}
    out = deliver(trader_toolbox(handlers), 7, plan_trade(parse_answer(answer_json()), context()))
    assert (out.status, out.code) == ("refused", "blocked") and "premium_selling_closed" in out.why


@pytest.fixture
def swo_desk(ready, monkeypatch):
    """The desk of the shared execution fixture, with the line SWO, the two option sheets and the quotes of the chain."""
    with ready.db.write() as u:
        u.session.update("desk_profile", {"desk_id": ready.desk_id}, {"line": "SWO"})
    for cid in (C200, C210):
        sheet_store.save(sheet(cid), db=ready.db)
    ready.board.feed(C200, bid="5.10", ask="5.20", last="5.15")
    ready.board.feed(C210, bid="1.80", ask="1.90", last="1.85")
    return ready


def _deliver(r):
    result = plan_trade(parse_answer(answer_json()), context(desk_id=r.desk_id))
    assert result.status == "proposed"
    box = trader_toolbox(mcp_handlers(Caller({"HOKMA_AGENT": "hugo"})))
    return deliver(box, r.desk_id, result)


def test_OA_51_at_level_1_the_tool_answers_analysis_only_and_at_level_2_awaiting_owner(swo_desk):
    first = _deliver(swo_desk)
    assert first.status == "analysis_only", first
    levels.set_level(swo_desk.desk_id, 2, who=swo_desk.owner, db=swo_desk.db)
    second = _deliver(swo_desk)
    assert second.status == "awaiting_owner", second
    with swo_desk.db.read() as u:
        row = u.session.one("SELECT proposal_json, status FROM exec_proposals WHERE id = :i", {"i": second.proposal_id})
    stored = json.loads(row["proposal_json"])
    assert stored["thesis"].startswith("[debit_call_spread on AAPL, expires 2026-12-18") and "buy" in json.dumps(stored["legs"])
    assert row["status"] == "awaiting_owner" and "structure" not in stored


def test_OA_51_exec_propose_needs_the_thesis_and_checks_the_swo_fields_against_the_legs(swo_desk):
    handlers = mcp_handlers(Caller({"HOKMA_AGENT": "hugo"}))
    body = plan_trade(parse_answer(answer_json()), context(desk_id=swo_desk.desk_id)).proposal.to_tool_body()
    from hokma.desks.core.tools import ToolFailure

    for bad in ({**body, "thesis": "  "}, {**body, "structure": "credit_call_spread", "net_premium": "-3.4"}, {**body, "underlying": "MSFT"},
                {k: v for k, v in body.items() if k != "thesis"}):
        with pytest.raises(ToolFailure) as exc:
            handlers["exec_propose"](desk_id=swo_desk.desk_id, proposal=bad)
        assert exc.value.kind == "bad_input", bad
    ok = handlers["exec_propose"](desk_id=swo_desk.desk_id, proposal=body)
    assert ok["status"] == "analysis_only"
    status = handlers["exec_status"](proposal_id=ok["proposal_id"])
    assert status["status"] == "analysis_only"
    sheet_out = handlers["contract_get"](symbol=C200)
    assert sheet_out["multiplier"] == "100" and sheet_out["contract_id"] == C200


def test_OA_51_the_line_of_the_desk_is_never_the_one_the_agent_writes(swo_desk):
    handlers = mcp_handlers(Caller({"HOKMA_AGENT": "hugo"}))
    body = plan_trade(parse_answer(answer_json()), context(desk_id=swo_desk.desk_id)).proposal.to_tool_body()
    from hokma.desks.core.tools import ToolFailure

    with pytest.raises(ToolFailure) as exc:
        handlers["exec_propose"](desk_id=swo_desk.desk_id, proposal={**body, "line": "DT"})
    assert exc.value.kind == "blocked" and "line_mismatch" in str(exc.value)


def test_OA_51_a_premium_selling_proposal_built_by_isaac_is_refused_at_the_door_while_the_switch_is_closed(swo_desk):
    from trader_support import C220, P170, P180

    for cid in (P170, P180, C220):
        sheet_store.save(sheet(cid), db=swo_desk.db)
    for cid, (bid, ask) in {P170: ("0.40", "0.50"), P180: ("1.10", "1.20"), C220: ("0.60", "0.70")}.items():
        swo_desk.board.feed(cid, bid=bid, ask=ask, last=ask)
    condor = answer_json(structure="iron_condor", legs=[{"contract_id": P170, "side": "buy"}, {"contract_id": P180, "side": "sell"},
                                                       {"contract_id": C210, "side": "sell"}, {"contract_id": C220, "side": "buy"}])
    result = plan_trade(parse_answer(condor), context(desk_id=swo_desk.desk_id))
    assert result.status == "proposed" and any(w.startswith("premium_selling_closed") for w in result.warnings)
    out = deliver(trader_toolbox(mcp_handlers(Caller({"HOKMA_AGENT": "hugo"}))), swo_desk.desk_id, result)
    assert out.status == "refused" and "premium_selling_closed" in out.why

"""The road of an order (SPEC-018 B2 to B4: AH-66, OA-39, OA-40, OA-41, AH-39, criteria 2, 3 and 4)."""
import json
import threading
from decimal import Decimal

import pytest
from exec_support import AGENT, NQ, SPY, Ready, db, desks_db, env, proposal, ready  # noqa: F401

from hokma import exec as hexec
from hokma.broker import service
from hokma.exec import approvals, levels
from hokma.exec.proposal import OrderProposal, ProposalError


def rows(r, table, where="1=1"):
    with r.db.read() as u:
        return u.session.query(f"SELECT * FROM {table} WHERE {where} ORDER BY id")


def test_level_1_only_records_and_nothing_goes_out(ready):
    out = hexec.submit(proposal(ready), who=AGENT, db=ready.db)
    assert out.status == "analysis_only" and out.code == "level_1"
    assert [x["status"] for x in rows(ready, "exec_proposals")] == ["analysis_only"]
    assert rows(ready, "broker_orders", "action = 'place'") == [] and rows(ready, "sim_orders") == []


def test_level_2_asks_the_owner_then_fills_on_the_sim_broker_at_the_real_price_with_cost(ready):
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready, stop_loss=Decimal("20950"), risk_usd=Decimal("1000")), who=AGENT, db=ready.db)
    assert out.status == "awaiting_owner" and out.decision_id and "x20" in out.ticket and out.fingerprint[:12] in out.ticket
    assert rows(ready, "sim_orders") == []                              # nothing left yet
    done = hexec.approve(out.proposal_id, who=ready.owner, db=ready.db)
    assert done.status == "filled"
    fill = rows(ready, "sim_fills")[0]
    assert Decimal(fill["price"]) >= Decimal("21000.25") and fill["side"] == "buy" and fill["multiplier"] == "20"      # ask plus slippage
    assert Decimal(fill["commission"]) >= 0
    book = rows(ready, "book_entries", "kind = 'fill'")
    assert len(book) == 1 and Decimal(book[0]["amount"]) == -Decimal(fill["price"]) * 20
    assert [x["status"] for x in rows(ready, "exec_proposals")] == ["filled"]
    placed = rows(ready, "broker_orders", "action = 'place' AND rule_result = 'auto'")
    assert placed and placed[0]["approved_by"] == "owner" and placed[0]["broker_order_id"]


def test_an_agent_cannot_approve_its_own_proposal(ready):
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready), who=AGENT, db=ready.db)
    with pytest.raises(service.BrokerError) as e:
        hexec.approve(out.proposal_id, who=AGENT, db=ready.db)
    assert e.value.code == "owner_required"


def test_what_is_approved_is_what_is_sent_an_altered_order_is_refused_hash_mismatch(ready):
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready), who=AGENT, db=ready.db)
    with ready.db.write() as u:                                           # the record is altered after it was shown to the owner
        body = json.loads(u.session.one("SELECT proposal_json FROM exec_proposals WHERE id = :i", {"i": out.proposal_id})["proposal_json"])
        body["legs"][0]["qty"] = "9"
        u.session.update("exec_proposals", {"id": out.proposal_id}, {"proposal_json": json.dumps(body)})
    done = hexec.approve(out.proposal_id, who=ready.owner, db=ready.db)
    assert (done.status, done.code) == ("refused", "hash_mismatch")
    assert rows(ready, "sim_orders") == [] and rows(ready, "sim_fills") == []


def test_two_sends_of_the_same_fingerprint_let_one_out(ready):
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready), who=AGENT, db=ready.db)
    with ready.db.write() as u:
        u.session.execute("UPDATE exec_proposals SET approved_fingerprint = fingerprint, approved_by = 'owner' WHERE id = :i", {"i": out.proposal_id})
    results = []
    threads = [threading.Thread(target=lambda: results.append(approvals.send_approved(out.proposal_id, db=ready.db))) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r.status for r in results) == ["filled", "refused"]
    assert len(rows(ready, "sim_orders")) == 1


def test_level_3_sends_alone_under_the_rules_the_owner_approved_and_asks_when_they_do_not_cover_it(ready):
    with pytest.raises(service.BrokerError):                              # an agent cannot raise the level
        levels.set_level(ready.desk_id, 3, rules={"id": "r1", "max_qty": 2}, who=AGENT, db=ready.db)
    levels.set_level(ready.desk_id, 3, rules={"id": "r1", "max_qty": 2, "symbols": ["FUT:NQ"]}, who=ready.owner, db=ready.db)
    sent = hexec.submit(proposal(ready, qty="2"), who=AGENT, db=ready.db)
    assert sent.status == "filled"
    assert rows(ready, "exec_proposals")[0]["approved_by"] == "rule:r1"
    asks = hexec.submit(proposal(ready, qty="3"), who=AGENT, db=ready.db)
    assert asks.status == "awaiting_owner"
    other = hexec.submit(proposal(ready, contract=SPY), who=AGENT, db=ready.db)
    assert other.status == "awaiting_owner"


def test_the_limits_of_the_committee_apply_at_every_level_and_each_attempt_is_recorded(ready):
    service.set_limits(ready.desk_id, {"max_position_pct": 50}, db=ready.db)
    for level in (1, 2):
        levels.set_level(ready.desk_id, level, who=ready.owner, db=ready.db) if level > 1 else None
        out = hexec.submit(proposal(ready, qty="5"), who=AGENT, db=ready.db)      # 5 NQ at 21000 x 20 = 2.1M on 1M
        assert out.status == "refused" and out.code == "max_position_pct"
    levels.set_level(ready.desk_id, 3, rules={"id": "r1", "max_qty": 9}, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready, qty="5"), who=AGENT, db=ready.db)
    assert out.status == "refused"
    assert len(rows(ready, "exec_proposals")) == 3 and len(rows(ready, "broker_orders", "rule_result = 'refused'")) == 3


def test_a_symbol_with_no_sheet_is_refused_before_the_filter(ready):
    out = hexec.submit(proposal(ready, contract="FUT:ZZZ:2026-12"), who=AGENT, db=ready.db)
    assert (out.status, out.code) == ("refused", "no_contract")


def test_the_owner_order_in_the_chat_is_its_own_approval_and_origin_rule_needs_level_3(ready):
    out = hexec.submit(proposal(ready, origin="chat", created_by="owner"), who=ready.owner, db=ready.db)
    assert out.status == "filled"
    refused = hexec.submit(proposal(ready, origin="rule"), who=AGENT, db=ready.db)
    assert refused.code == "rule_level"


def test_a_market_order_without_a_fresh_quote_is_rejected_and_no_price_is_made_up(ready):
    ready.board.feed("EQ:QQQ", bid="500", ask="500.1", last="500", ts=1.0)      # a quote of long ago
    levels.set_level(ready.desk_id, 3, rules={"id": "r", "max_qty": 5}, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready, contract="EQ:QQQ", qty="1"), who=AGENT, db=ready.db)
    assert out.status in ("rejected_by_broker", "refused") and rows(ready, "sim_fills") == []


def test_a_limit_order_waits_and_fills_when_the_stream_crosses(ready):
    from hokma.broker import sim
    levels.set_level(ready.desk_id, 3, rules={"id": "r", "max_qty": 5, "order_types": ["limit"]}, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready, order_type="limit", limit="20990"), who=AGENT, db=ready.db)
    assert out.status == "sent"                                           # working at the simulated broker
    assert rows(ready, "sim_fills") == []
    ready.board.listeners.append(sim.on_quote)
    ready.board.feed(NQ, bid="20989", ask="20989.5", last="20989.5")
    fills = rows(ready, "sim_fills")
    assert len(fills) == 1 and Decimal(fills[0]["price"]) <= Decimal("20990")
    assert rows(ready, "sim_orders")[0]["status"] == "filled"


def test_proposal_schema_refuses_unknown_fields_and_floats_become_decimals():
    with pytest.raises(ProposalError):
        OrderProposal.from_dict({"line": "DT", "order_type": "market", "legs": [{"contract_id": NQ, "side": "buy", "effect": "open", "qty": 1}],
                                 "color": "red"}, desk_id=1, origin="desk", created_by="x")
    p = OrderProposal.from_dict({"line": "DT", "order_type": "limit", "limit_price": 21000.5, "legs": [{"contract_id": NQ, "side": "buy", "effect": "open", "qty": 2}]},
                                desk_id=1, origin="desk", created_by="x")
    assert isinstance(p.limit_price, Decimal) and p.canonical()["limit_price"] == "21000.5" and p.canonical()["legs"][0]["qty"] == "2"
    q = OrderProposal.from_dict({**{"line": "DT", "order_type": "limit", "limit_price": "21000.50", "legs": [{"contract_id": NQ, "side": "buy", "effect": "open", "qty": "2.0"}],
                                    "thesis": "another thesis"}}, desk_id=1, origin="desk", created_by="y")
    assert p.fingerprint() == q.fingerprint()                              # the thesis and the author are not part of the hash

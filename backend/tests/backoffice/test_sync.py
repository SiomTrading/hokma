"""Sync by listing: one call of orders and positions reconciles the local state (SPEC-018: OA-42, with TA-48 for the decision it closes)."""
from decimal import Decimal
import json
from decimal import Decimal as D

import pytest
from backoffice_support import NQ, db, desks_db, env, ready  # noqa: F401
from exec_support import AGENT, proposal

from hokma import exec as hexec
from hokma.backoffice import book, decisions, sync
from hokma.broker import sim
from hokma.broker.types import Order, Position
from hokma.exec import levels
from account_links_support import link_principal, unlink_all

FINGERPRINT = "f" * 64


class FakeBroker:
    """A broker that counts its calls: the listing of orders, the positions and the orders read by id."""

    def __init__(self, orders, positions, lookup):
        self._orders, self._positions, self._lookup = orders, positions, lookup
        self.calls = {"orders": 0, "positions": 0, "get_order": []}

    def orders(self, account_id, **filters):
        self.calls["orders"] += 1
        return list(self._orders)

    def positions(self, account_id):
        self.calls["positions"] += 1
        return list(self._positions)

    def get_order(self, account_id, order_id):
        self.calls["get_order"].append(order_id)
        return self._lookup.get(order_id)


def order(oid, status, side="BUY", filled=0.0, executions=(), symbol="AAPL"):
    legs = ({"symbol": symbol, "asset_type": "EQUITY", "instruction": side, "position_effect": None, "quantity": filled or 1.0},)
    activity = [{"executionType": "FILL", "executionLegs": [{"legId": 1, "quantity": q, "price": p, "time": t} for q, p, t in executions]}] if executions else []
    return Order(1, symbol, side, filled or 1.0, order_id=oid, status=status, filled_qty=filled, legs=legs, data={"orderActivityCollection": activity})


@pytest.fixture
def real(ready):
    """The desk now trades through an account of a real broker (kind schwab); four of its proposals were sent and none is closed."""
    with ready.db.write() as u:
        unlink_all(u, ready.desk_id)
        broker = u.broker.create_broker("schwab", "Schwab test", {"orders_enabled": False})
        account = u.broker.create_account(broker.id, "Real A", 1_000_000)
        link_principal(u, ready.desk_id, account.id)
        ready.real_account = account.id
        ready.proposals = {}
        for oid in ("1001", "1002", "1003", "1004"):
            record = u.broker.record_order(broker_kind="schwab", action="place", desk_id=ready.desk_id, account_id=account.id, request={"o": oid},
                                           rule_result="auto", approved_by="owner", broker_order_id=oid)
            ready.proposals[oid] = u.session.insert("exec_proposals", {
                "desk_id": ready.desk_id, "line": "DT", "origin": "desk", "level": 3, "proposal_json": json.dumps({"o": oid}), "fingerprint": oid.rjust(64, "0"),
                "status": "sent", "approved_fingerprint": oid.rjust(64, "0"), "approved_by": "rule:r", "created_by": "desk-chief",
                "broker_order_record_id": record.id})
    return ready


def status_of(r, pid):
    with r.db.read() as u:
        return u.session.one("SELECT status FROM exec_proposals WHERE id = :i", {"i": pid})["status"]


def test_one_listing_reconciles_the_local_state_and_a_vanished_order_is_read_once_by_id_OA_42(real):
    listed = [order("1001", "FILLED", filled=100.0, executions=[(60.0, 200.0, "2026-03-02T14:35:00+0000"), (40.0, 200.5, "2026-03-02T14:36:00+0000")]),
              order("1003", "WORKING"), order("9999", "FILLED", filled=5.0, executions=[(5.0, 10.0, "2026-03-02T14:35:00+0000")])]
    lookup = {"1002": order("1002", "FILLED", side="SELL", filled=100.0, executions=[(100.0, 210.0, "2026-03-03T14:35:00+0000")])}
    broker = FakeBroker(listed, [Position(real.real_account, "MSFT", 5.0, 400.0)], lookup)
    report = sync.sync(real.real_account, broker=broker, db=real.db)
    assert broker.calls == {"orders": 1, "positions": 1, "get_order": ["1002", "1004"]}      # one listing, one positions call, one read by id for each missing order
    assert report.orders_listed == 3 and report.positions_listed == 1 and report.looked_up == ["1002"]
    assert status_of(real, real.proposals["1001"]) == "filled" and status_of(real, real.proposals["1002"]) == "filled"      # the final status the broker gave
    assert status_of(real, real.proposals["1003"]) == "sent"                       # still working: nothing to change
    assert status_of(real, real.proposals["1004"]) == "sent" and report.unresolved == ["1004: the broker does not know the order"]
    assert {u["order_id"] for u in report.updated} == {"1001", "1002"}
    entries = book.entries(real.real_account, db=real.db, kinds=["fill"])
    assert [(e.contract_id, e.qty, e.price, e.amount, e.source) for e in entries] == [
        ("EQ:AAPL", D(60), D(200), D(-12000), "schwab"), ("EQ:AAPL", D(40), D("200.5"), D(-8020), "schwab"), ("EQ:AAPL", D(-100), D(210), D(21000), "schwab")]
    assert all(e.desk_id == real.desk_id and e.source_ref.startswith(("1001:", "1002:")) for e in entries)      # the order 9999 was never Hokma's: not booked
    held = book.Book.load(real.real_account, db=real.db)
    assert held.cost_positions() == {} and held.realized() == D("980")             # 100 bought at (12000 + 8020) / 100 = 200.20 and sold at 210: 9.8 * 100
    assert report.booked == 3 and report.position_diffs == [{"contract_id": "EQ:MSFT", "broker_qty": D(5), "book_qty": D(0)}]


def test_running_the_sync_twice_does_not_duplicate_entries_OA_42(real):
    listed = [order("1001", "FILLED", filled=100.0, executions=[(60.0, 200.0, "2026-03-02T14:35:00+0000"), (40.0, 200.5, "2026-03-02T14:36:00+0000")]),
              order("1003", "WORKING")]
    lookup = {"1002": order("1002", "FILLED", side="SELL", filled=100.0, executions=[(100.0, 210.0, "2026-03-03T14:35:00+0000")])}
    sync.sync(real.real_account, broker=FakeBroker(listed, [], lookup), db=real.db)
    before = book.entries(real.real_account, db=real.db, since_reset=False)
    cash = book.cash(real.real_account, db=real.db)
    again = FakeBroker(listed, [], lookup)
    report = sync.sync(real.real_account, broker=again, db=real.db)
    assert report.booked == 0 and report.duplicates == 2 and report.updated == [] and again.calls["get_order"] == ["1004"]      # 1002 is closed now, 1003 is listed
    assert book.entries(real.real_account, db=real.db, since_reset=False) == before
    assert book.cash(real.real_account, db=real.db) == cash


def test_a_partial_fill_and_a_canceled_remainder_end_as_partially_filled_OA_42(real):
    listed = [order("1001", "CANCELED", filled=30.0, executions=[(30.0, 200.0, "2026-03-02T14:35:00+0000")]), order("1002", "REJECTED"), order("1003", "EXPIRED"),
              order("1004", "WORKING", filled=10.0, executions=[(10.0, 200.0, "2026-03-02T14:35:00+0000")])]
    sync.sync(real.real_account, broker=FakeBroker(listed, [], {}), db=real.db)
    assert [status_of(real, real.proposals[o]) for o in ("1001", "1002", "1003", "1004")] == ["partially_filled", "rejected_by_broker", "expired", "partially_filled"]
    assert sum(e.qty for e in book.entries(real.real_account, db=real.db, kinds=["fill"])) == D(40)


def test_an_execution_the_sheet_cannot_price_is_reported_and_not_booked_OA_42(real):
    odd = order("1001", "FILLED", filled=1.0, executions=[(1.0, 5.0, "2026-03-02T14:35:00+0000")], symbol="NOT A SYMBOL!")
    report = sync.sync(real.real_account, broker=FakeBroker([odd], [], {}), db=real.db)
    assert book.entries(real.real_account, db=real.db, since_reset=False) == [] and any("no contract sheet" in x for x in report.unresolved)
    assert status_of(real, real.proposals["1001"]) == "sent"                       # its fill could not be booked: the proposal stays open to be tried again
    lying = Order(1, "AAPL", "BUY", 1.0, order_id="1002", status="FILLED", filled_qty=1.0, multiplier=D(50),
                  legs=({"symbol": "AAPL", "instruction": "BUY"},), data={"executions": [{"id": "e1", "qty": "1", "price": "5", "time": "2026-03-02T14:35:00+0000"}]})
    report = sync.sync(real.real_account, broker=FakeBroker([lying], [], {}), db=real.db)
    assert book.entries(real.real_account, db=real.db, since_reset=False) == [] and "multiplier 50" in " ".join(report.unresolved)


def test_a_filled_order_with_no_executions_and_nothing_booked_is_reported_OA_42(real):
    report = sync.sync(real.real_account, broker=FakeBroker([order("1001", "FILLED", filled=10.0)], [], {}), db=real.db)
    assert any("filled but the broker gave no executions" in x for x in report.unresolved) and report.booked == 0


def test_the_sync_of_a_desk_on_the_simulated_broker_closes_a_working_order_without_duplicating_the_fill_OA_42_TA_48(ready):
    levels.set_level(ready.desk_id, 3, rules={"id": "r", "max_qty": 5, "order_types": ["limit"]}, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready, order_type="limit", limit="20990", stop_loss=Decimal("20980")), who=AGENT, db=ready.db)
    assert out.status == "sent"
    decision_id = decisions.record(ready.desk_id, "Buy one NQ on a pullback to 20990", proposal_id=out.proposal_id, db=ready.db)
    assert decisions.of_proposal(out.proposal_id, db=ready.db)["status"] == "proposed"        # pending until the order ends
    ready.board.listeners.append(sim.on_quote)
    ready.board.feed(NQ, bid="20989", ask="20989.5", last="20989.5")                          # the stream crosses the limit: the broker fills and books by itself
    entries = book.entries(ready.account_id, db=ready.db, kinds=["fill"])
    assert len(entries) == 1
    report = sync.sync_desk(ready.desk_id, db=ready.db)
    assert report.booked == 0 and report.unresolved == [] and report.updated[0]["status"] == "filled"
    assert book.entries(ready.account_id, db=ready.db, kinds=["fill"]) == entries          # the sim fill is not written a second time
    with ready.db.read() as u:
        row = u.session.one("SELECT status FROM exec_proposals WHERE id = :i", {"i": out.proposal_id})
    assert row["status"] == "filled"
    closed = decisions.of_proposal(out.proposal_id, db=ready.db)
    assert closed["id"] == decision_id and closed["status"] == "approved" and closed["resolution"]["order_id"].startswith("S")
    assert sync.sync_desk(ready.desk_id, db=ready.db).updated == []


def test_a_desk_without_an_account_cannot_be_synced_OA_42(ready):
    with ready.db.write() as u:
        unlink_all(u, ready.desk_id)
    with pytest.raises(sync.SyncError):
        sync.sync_desk(ready.desk_id, db=ready.db)

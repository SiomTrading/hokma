"""The simulated broker and the account lifecycle (SPEC-018 B4: AH-39, OA-57, OA-56)."""
from decimal import Decimal

import pytest
from exec_support import NQ, SPY, db, desks_db, env, ready  # noqa: F401

from hokma.backoffice import book as book_module
from hokma.backoffice.book import Book
from hokma.backoffice.snapshot import snapshot
from hokma.broker import service, sim, sim_engine
from hokma.broker.sim import SimBroker
from hokma.broker.types import Fill


def neutral(side="buy", qty="1", order_type="market", limit=None, contract=NQ, mult="20", tick="0.25", asset="future", **extra):
    return {"fingerprint": "a" * 64, "type": order_type, "tif": "day", "limit_price": limit, "stop_price": extra.get("stop"), "stop_loss": None,
            "take_profit": None, "reduce_only": False,
            "legs": [{"contract_id": contract, "symbol": contract, "side": side, "effect": "open", "qty": qty, "multiplier": mult, "tick_size": tick,
                      "asset_type": asset}]}


def broker_of(r):
    class Holder:
        def __enter__(self):
            self.ctx = r.db.read()
            u = self.ctx.__enter__()
            row = u.broker.get_broker(r.broker_id)
            return SimBroker(u.broker, row)

        def __exit__(self, *a):
            return self.ctx.__exit__(*a)
    return Holder()


def set_costs(r, slippage, commission):
    with r.db.write() as u:
        SimBroker(u.broker, u.broker.get_broker(r.broker_id)).set_costs(commission, slippage)


def test_a_market_order_fills_at_the_ask_or_the_bid_worse_by_slippage_with_commission(ready):
    set_costs(ready, 1.0, {"type": "per_share", "value": 2.5})              # 1 bp of slippage, 2.5 USD per contract
    with broker_of(ready) as b:
        buy = b.place_order(ready.account_id, neutral("buy", "2"))
        assert buy["status"] == "FILLED"
        # ask 21000.25 plus 1 bp is 21002.35...: on the grid of the tick of 0.25 a buy rounds up
        assert Decimal(buy["avg_price"]) == Decimal("21002.50")
        sell = b.place_order(ready.account_id, neutral("sell", "2"))
        assert Decimal(sell["avg_price"]) == Decimal("20997.75")             # bid 21000 minus 1 bp is 20997.9: a sell rounds down
    with ready.db.read() as u:
        fills = u.session.query("SELECT * FROM sim_fills ORDER BY id")
        entries = u.session.query("SELECT kind, amount FROM book_entries ORDER BY id")
    assert [f["commission"] for f in fills] == ["5", "5"] and [f["multiplier"] for f in fills] == ["20", "20"]
    assert [e["kind"] for e in entries] == ["fill", "commission", "fill", "commission"]
    assert Decimal(entries[0]["amount"]) == -Decimal("21002.5") * 2 * 20


def test_without_a_fresh_quote_the_order_is_rejected_and_the_reason_is_no_market_price(ready):
    ready.board.feed("EQ:ZZZ", bid="10", ask="10.1", last="10", ts=1.0)
    with broker_of(ready) as b:
        out = b.place_order(ready.account_id, neutral(contract="EQ:ZZZ", mult="1", tick="0.01", asset="equity"))
        gone = b.place_order(ready.account_id, neutral(contract="EQ:NOQUOTE", mult="1", tick="0.01", asset="equity"))
    assert out["status"] == gone["status"] == "REJECTED" and out["reason"] == "no_market_price"
    with ready.db.read() as u:
        assert u.session.query("SELECT * FROM sim_fills") == []


def test_a_limit_order_that_does_not_cross_waits_and_the_stream_fills_it(ready):
    set_costs(ready, 0.0, {"type": "per_share", "value": 0})
    with broker_of(ready) as b:
        waiting = b.place_order(ready.account_id, neutral("buy", "1", "limit", "20990"))
        assert waiting["status"] == "WORKING" and b.get_order(ready.account_id, waiting["order_id"]).cancelable is True
        assert b.process(NQ) == 0                                           # no new quote: nothing fills
    ready.board.feed(NQ, bid="20989.5", ask="20989.75", last="20989.75")
    with broker_of(ready) as b:
        assert b.process(NQ) == 1
        done = b.get_order(ready.account_id, waiting["order_id"])
    assert done.status == "FILLED" and done.filled_qty == 1.0
    with ready.db.read() as u:
        assert Decimal(u.session.query("SELECT price FROM sim_fills")[0]["price"]) == Decimal("20989.75")      # never worse than the limit


def test_cancel_and_replace_a_working_order(ready):
    with broker_of(ready) as b:
        first = b.place_order(ready.account_id, neutral("buy", "1", "limit", "20000"))
        second = b.replace_order(ready.account_id, first["order_id"], neutral("buy", "1", "limit", "20100"))
        assert b.get_order(ready.account_id, first["order_id"]).status == "CANCELED" and second["status"] == "WORKING"
        assert b.cancel_order(ready.account_id, second["order_id"])["status"] == "CANCELED"


def test_the_account_lifecycle_open_add_reduce_close_ends_flat_with_cash_equal_to_the_start_plus_pnl_minus_commissions(ready):
    set_costs(ready, 0.0, {"type": "per_share", "value": 1})                # no slippage, 1 USD per contract
    script = [("buy", "2", "21000.00", "21000.25"), ("buy", "1", "21010.00", "21010.25"), ("sell", "1", "21020.00", "21020.25"), ("sell", "2", "21030.00", "21030.25")]
    with broker_of(ready) as b:
        for side, qty, bid, ask in script:
            ready.board.feed(NQ, bid=bid, ask=ask, last=ask)
            out = b.place_order(ready.account_id, neutral(side, qty))
            assert out["status"] == "FILLED", out
    with broker_of(ready) as b:                                              # a new unit sees what the orders wrote
        assert b.positions(ready.account_id) == []
        cash = b.balance(ready.account_id).cash
    # buys at the ask, sells at the bid: 2@21000.25 + 1@21010.25, then 1@21020.00 + 2@21030.00, multiplier 20
    avg = (Decimal("2") * Decimal("21000.25") + Decimal("21010.25")) / 3
    pnl = ((Decimal("21020.00") - avg) * 1 + (Decimal("21030.00") - avg) * 2) * 20
    assert Decimal(str(cash)) == pytest.approx(Decimal(1_000_000) + pnl - Decimal(6), abs=Decimal("0.0001"))
    book = Book.load(ready.account_id, db=ready.db)
    assert book.cost_positions() == {} and book.realized() == pnl and book.commissions() == 6
    snap = snapshot(ready.account_id, db=ready.db)
    assert snap.positions_value == 0 and snap.equity == snap.cash


def test_the_same_script_booked_from_recorded_schwab_fills_leaves_the_same_book(ready):
    set_costs(ready, 0.0, {"type": "per_share", "value": 1})
    script = [("buy", "2", "21000.00", "21000.25"), ("buy", "1", "21010.00", "21010.25"), ("sell", "1", "21020.00", "21020.25"), ("sell", "2", "21030.00", "21030.25")]
    with broker_of(ready) as b:
        for side, qty, bid, ask in script:
            ready.board.feed(NQ, bid=bid, ask=ask, last=ask)
            b.place_order(ready.account_id, neutral(side, qty))
    sim_book = Book.load(ready.account_id, db=ready.db)
    with ready.db.write() as u:
        other = u.broker.create_account(ready.broker_id, "Recorded", 1_000_000)
    prices = [("buy", 2, 21000.25), ("buy", 1, 21010.25), ("sell", 1, 21020.0), ("sell", 2, 21030.0)]      # what the recorded executions say
    for n, (side, qty, price) in enumerate(prices):
        book_module.record_fill(Fill(other.id, "/NQZ26", side.upper(), float(qty), price, float(qty), "2026-11-20 15:00:00", order_id=str(n),
                                     multiplier=Decimal(20), contract_id=NQ), desk_id=None, source="schwab", source_ref=f"rec-{n}")
    recorded = Book.load(other.id, db=ready.db)
    assert recorded.summary() == sim_book.summary()


def test_orders_enabled_switch_gates_the_sim_broker_like_any_other_broker(ready):
    service.set_orders_enabled(ready.broker_id, False, db=ready.db)
    from hokma import exec as hexec
    from exec_support import AGENT, proposal

    out = hexec.submit(proposal(ready, origin="chat", created_by="owner"), who=ready.owner, db=ready.db)
    assert out.status == "refused" and out.code == "orders_off"

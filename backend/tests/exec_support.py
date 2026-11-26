"""Shared pieces of the execution tests (SPEC-018): a desk with an account of the simulated broker, a fed quote board and its owner."""
from __future__ import annotations

from decimal import Decimal

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures and helper)

from hokma.broker import service, sim_engine
from hokma.broker.service import Who, owner_who
from hokma.contracts import identity
from hokma.exec import levels, prefilter
from hokma.exec.proposal import Leg, OrderProposal
from account_links_support import link_principal

NQ = "FUT:NQ:2026-12"
SPY = "EQ:SPY"
AGENT = Who(agent="desk-chief")


class Ready:
    pass


@pytest.fixture
def ready(desks_db, monkeypatch):
    identity.clear_providers()
    board = sim_engine.QuoteBoard(clock=lambda: 1000.0)
    monkeypatch.setattr(sim_engine, "_board", board)
    monkeypatch.setattr(sim_engine, "_source", None)
    monkeypatch.setattr("hokma.broker.sim.time.time", lambda: 1000.0)
    prefilter.set_mark_source(sim_engine.mark_source)
    r = Ready()
    r.db, r.board = desks_db, board
    desk = make_desk(desks_db, name="NQ desk", capital=1_000_000)
    r.desk_id = desk["id"] if isinstance(desk, dict) else desk.id
    with desks_db.write() as u:
        broker = next(b for b in u.broker.list_brokers() if b.kind == "sim")
        account = u.broker.create_account(broker.id, "Sim A", 1_000_000)
        link_principal(u, r.desk_id, account.id)
        u.session.insert_row("desk_profile", {"desk_id": r.desk_id, "line": "DT"})
        r.broker_id, r.account_id = broker.id, account.id
    service.set_orders_enabled(r.broker_id, True, db=desks_db)
    board.feed(NQ, bid="21000", ask="21000.25", last="21000.25")
    board.feed(SPY, bid="600.00", ask="600.02", last="600.01")
    r.owner = owner_who()
    return r


AUTO = object()
STOP_AWAY = Decimal(5)                       # points: the stop an entry in the tests carries unless the test says otherwise (3390d: an entry in futures needs one)


def proposal(r, *, contract=NQ, side="buy", qty="1", effect="open", origin="desk", order_type="market", limit=None, stop_loss=AUTO, **kw):
    if stop_loss is AUTO:                    # pass stop_loss=None to send an entry WITHOUT a stop
        stop_loss = None
        if effect == "open" and contract.startswith(("FUT:", "CFD:")):
            quote = r.board.get(contract) if hasattr(r.board, "get") else None
            price = Decimal("21000") if contract == NQ else (Decimal(str(quote.mark())) if quote is not None and quote.mark() is not None else None)
            if price is not None:
                stop_loss = price - STOP_AWAY if side == "buy" else price + STOP_AWAY
    return OrderProposal(desk_id=r.desk_id, line=kw.pop("line", "DT"), origin=origin, legs=(Leg(contract, side, effect, Decimal(qty)),),
                         order_type=order_type, limit_price=None if limit is None else Decimal(limit), thesis=kw.pop("thesis", "test"),
                         created_by=kw.pop("created_by", "desk-chief"), stop_loss=stop_loss, **kw)

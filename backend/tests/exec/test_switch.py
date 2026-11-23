"""The same desk moves from the simulated broker to a Schwab account by changing only the assigned account (SPEC-018, criterion 5)."""
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from broker_orders_support import calls, connections, created, desks_db, env, db, fake, keys, ready, rows, sent, server  # noqa: F401

from hokma import exec as hexec
from hokma.broker import service
from hokma.broker.service import Who, owner_who
from hokma.exec import levels, prefilter
from hokma.exec.proposal import Leg, OrderProposal

AGENT = Who(agent="desk-chief")


def folder_hash(path: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def test_the_same_proposal_goes_out_by_schwab_after_assign_account_and_no_file_of_the_desk_changes(ready, env):
    prefilter.set_mark_source(lambda ids: {i: Decimal(10) for i in ids})
    with ready.db.write() as u:
        u.session.insert_row("desk_profile", {"desk_id": ready.desk_id, "line": "LP"})
    owner = owner_who()
    levels.set_level(ready.desk_id, 3, rules={"id": "r", "max_qty": 5, "symbols": ["AAPL"]}, who=owner, db=ready.db)
    proposal = OrderProposal(ready.desk_id, "LP", "desk", (Leg("EQ:AAPL", "buy", "open", Decimal(1)),), "limit", limit_price=Decimal("10"), created_by="desk-chief",
                             thesis="switch")
    out = hexec.submit(proposal, who=AGENT, db=ready.db)
    assert out.status == "sent", out                                  # Schwab took it: it has no fill answer
    posts = sent(ready.fake)
    assert len(posts) == 1 and json.loads(posts[0].body)["orderLegCollection"][0]["instrument"]["symbol"] == "AAPL"
    placed = [r for r in rows(ready) if r.action == "place" and r.broker_kind == "schwab"]
    assert placed and placed[0].rule_result == "auto"
    # moving the desk back to a simulated account changes only the assignment
    with ready.db.write() as u:
        sim_broker = next(b for b in u.broker.list_brokers() if b.kind == "sim")
        account = u.broker.create_account(sim_broker.id, "Sim", 10_000)
    service.unassign_account(ready.desk_id, db=ready.db)
    service.assign_account(ready.desk_id, account.id, db=ready.db)
    with ready.db.read() as u:
        assert u.broker.account_of_desk(ready.desk_id).id == account.id

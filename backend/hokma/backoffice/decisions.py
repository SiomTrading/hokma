"""The decision of a cycle (SPEC-018: TA-48). It is written as pending when the cycle ends, linked to the proposal of the desk
(`exec_proposals.decision_id`). When the order is filled or ends without a fill, `resolve` closes it and records the id of the order.

A proposal that already carries a decision (the one the owner answers at level 2) keeps it: there is one decision per proposal and
the owner's flow is not touched. Nothing here believes what the agent wrote about the order: the status is the one of the proposal row."""
from __future__ import annotations

import json
from typing import Any

from hokma.db import Actor, Database, RuleViolation, get_database

FILLED = ("filled",)
CLOSED_WITHOUT_FILL = ("rejected_by_broker", "canceled", "expired", "refused")
SYSTEM = Actor.system("backoffice")
RESOLVER = Actor.agent("hokma")


class DecisionError(ValueError):
    """The decision cannot be written as asked."""


def _proposer(u: Any, preferred: str | None) -> str:
    if preferred and u.agents.find(preferred) is not None:
        return preferred
    known = u.agents.list()
    if not known:
        raise DecisionError("no agent can be the proposer of the decision")
    return known[0].slug


def record(desk_id: int, summary: str, *, proposal_id: int | None = None, db: Database | None = None) -> int:
    """The pending decision of a cycle. With `proposal_id` it is linked to that proposal (which must belong to the desk); a proposal that already
    has a decision returns that one. Returns the decision id."""
    if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 4000:
        raise DecisionError("the summary of a decision is 1 to 4000 characters")
    with (db or get_database()).write() as u:
        row = None
        if proposal_id is not None:
            row = u.session.one("SELECT * FROM exec_proposals WHERE id = :i", {"i": proposal_id})
            if row is None or row["desk_id"] != desk_id:
                raise DecisionError("the proposal is not one of this desk")
            if row["decision_id"] is not None:
                return int(row["decision_id"])
            if row["status"] == "awaiting_owner":
                raise DecisionError("a proposal that waits for the owner already has its own decision")
        decision = u.decisions.propose(f"Desk {desk_id} cycle: {' '.join(summary.split())}"[:200], summary.strip(), proposed_by=_proposer(u, row and row["created_by"]),
                                       context=None if row is None else json.dumps({"proposal_id": row["id"], "fingerprint": row["fingerprint"]}))
        if row is not None:
            u.session.update("exec_proposals", {"id": row["id"]}, {"decision_id": decision.id})
        return decision.id


def resolve_in(u: Any, proposal_id: int) -> dict[str, Any] | None:
    """Close the decision of a proposal inside an open write unit: filled is approved, an order that ended without a fill is rejected, with the
    id of the order. None when the proposal has no decision or has not reached a final status. Safe to call twice."""
    row = u.session.one("SELECT * FROM exec_proposals WHERE id = :i", {"i": proposal_id})
    if row is None or row["decision_id"] is None or row["status"] not in FILLED + CLOSED_WITHOUT_FILL:
        return None
    order_id = None
    if row["broker_order_record_id"] is not None:
        order = u.session.one("SELECT broker_order_id FROM broker_orders WHERE id = :i", {"i": row["broker_order_record_id"]})
        order_id = None if order is None else order["broker_order_id"]
    decision = u.decisions.get(row["decision_id"])
    resolution = {"proposal_id": row["id"], "status": row["status"], "order_id": order_id, "order_record_id": row["broker_order_record_id"]}
    if decision.status == "proposed":
        u.decisions.decide(decision.id, "approved" if row["status"] in FILLED else "rejected", actor=RESOLVER)
    options = decision.options if isinstance(decision.options, dict) else {}
    if options.get("resolution") == resolution:
        return resolution
    u.session.update("decisions", {"id": decision.id}, {"options": json.dumps({**options, "resolution": resolution}, sort_keys=True)})
    u.journal.add("decision", f"Decision {decision.id} closed: order {order_id or 'none'} is {row['status']}", actor=SYSTEM,
                  outcome="success" if row["status"] in FILLED else "failure", body=None, refs={"decision_id": decision.id, **resolution})
    return resolution


def resolve(proposal_id: int, *, db: Database | None = None) -> dict[str, Any] | None:
    with (db or get_database()).write() as u:
        try:
            return resolve_in(u, proposal_id)
        except RuleViolation:
            return None


def of_proposal(proposal_id: int, *, db: Database | None = None) -> dict[str, Any] | None:
    """The decision of a proposal as plain data (status and resolution), or None."""
    with (db or get_database()).read() as u:
        row = u.session.one("SELECT decision_id FROM exec_proposals WHERE id = :i", {"i": proposal_id})
        if row is None or row["decision_id"] is None:
            return None
        d = u.decisions.get(row["decision_id"])
    return {"id": d.id, "status": d.status, "decided_by": d.decided_by, "resolution": (d.options or {}).get("resolution") if isinstance(d.options, dict) else None}

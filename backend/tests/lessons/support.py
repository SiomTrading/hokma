"""Helpers of the lesson tests: a desk, a decision, an executed order, a refused one and the fills of a trade."""
from __future__ import annotations

from hokma.broker.types import Fill


def make_decision(db, topic="Buy NQ"):
    with db.write() as u:
        return u.decisions.propose(topic, "Go long one contract", proposed_by="hokma").id


def make_order(db, desk_id, *, decision_id=None, broker_order_id="ORD-1", action="place", rule_result="auto", http_status=201,
               price=21400.0, response=None):
    """A row of broker_orders. The defaults are an order that was sent and accepted."""
    request = {"orderType": "LIMIT", "price": price, "orderLegCollection": [
        {"instruction": "BUY", "quantity": 1, "instrument": {"symbol": "NQZ6", "assetType": "FUTURE"}}]}
    with db.write() as u:
        return u.broker.record_order(broker_kind="schwab", action=action, desk_id=desk_id, request=request, rule_result=rule_result,
                                     decision_id=decision_id, approved_by="auto" if rule_result == "auto" else None,
                                     http_status=http_status, broker_order_id=broker_order_id, response=response).id


def fill(order_id="ORD-1", side="BUY", qty=1.0, price=21410.0, at="2026-11-02 15:00:00", commission=2.0, symbol="NQZ6"):
    return Fill(account_id=1, symbol=symbol, side=side, qty=qty, price=price, commission=commission, at=at, order_id=order_id)

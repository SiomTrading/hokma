"""The proposals of the backtest enter live execution unchanged (SPEC-020: L-59, N-16, criterion 5): the same proposal that the backtest bridge builds
goes through `hokma.exec.submit` of the simulated broker, and the two fills use the same table of costs. This test lives outside the backtest package on
purpose: the backtest never imports the door."""
import typing
from decimal import Decimal as D

import pytest
from exec_support import NQ, db, desks_db, env, ready  # noqa: F401  (fixtures)

from hokma import exec as hexec
from hokma.backtest import broker_bridge, costs
from hokma.backtest.broker_bridge import BridgeError, supported_types, to_proposal
from hokma.backtest.spec import CostModel
from hokma.broker import fees, service
from hokma.contracts import identity
from hokma.exec import levels, proposal as proposal_module
from hokma.exec.guards import broker_rules
from hokma.exec.proposal import Leg, OrderProposal, ProposalError
from hokma.exec import submit as door
from hokma.factory.orders import OrderIntent, Size, SizeType, Ticks, buy

SHEET = identity.resolve(NQ)


def test_the_order_types_of_the_bridge_are_the_ones_of_the_proposal_and_of_the_schwab_translation_of_live():
    assert supported_types("schwab") == proposal_module.ORDER_TYPES == broker_bridge.PROPOSAL_TYPES
    assert tuple(door._SCHWAB_TYPES) == tuple(broker_rules._TYPES) == supported_types("schwab")
    for kind in ("sim", "schwab", "alpaca", "ibkr", "crypto"):
        assert supported_types(kind) == proposal_module.ORDER_TYPES


def test_a_type_outside_the_list_fails_in_the_backtest_and_in_live_with_the_same_code():
    trailing = OrderIntent(NQ, "sell", Size(SizeType.CONTRACTS, D(1)), "trailing", stop=D("25000"))
    with pytest.raises(BridgeError) as backtest:
        to_proposal(trailing, desk_id=1, line="DT", broker_kind="schwab")
    with pytest.raises(ProposalError) as live:
        OrderProposal(1, "DT", "desk", (Leg(NQ, "sell", "open", D(1)),), "trailing", stop_price=D("25000"))      # type: ignore[arg-type]
    assert backtest.value.code == live.value.code == "bad_proposal"
    with pytest.raises(ProposalError) as from_json:
        OrderProposal.from_dict({"desk_id": 1, "line": "DT", "origin": "desk", "order_type": "trailing", "legs": [{"contract_id": NQ, "side": "sell", "effect": "open", "qty": "1"}]})
    assert from_json.value.code == "bad_proposal"


def test_the_proposal_of_a_strategy_enters_the_simulated_broker_unchanged_and_both_fills_use_the_same_table_of_costs(ready):
    rule = fees.load("schwab").rules["future"]                                       # the table of the backtest: 2.25 per contract per side
    service.update_costs(ready.broker_id, commission={"type": "per_share", "value": float(rule.value), "min": 0}, slippage_bps=0, db=ready.db)
    intent = buy(NQ, 1, sl=Ticks(20), tp=Ticks(20), tag="t2")
    p = to_proposal(intent, desk_id=ready.desk_id, line="DT", broker_kind="sim", sheet=SHEET, price=D("21000.25"))
    assert (p.stop_loss, p.take_profit, p.risk_usd) == (D("20995.25"), D("21005.25"), D(100))
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    waiting = hexec.submit(p, who=ready.owner, db=ready.db)
    assert waiting.status == "awaiting_owner" and waiting.fingerprint == p.fingerprint()          # what is approved is what the strategy emitted
    done = hexec.approve(waiting.proposal_id, who=ready.owner, db=ready.db)
    assert done.status == "filled" and done.fingerprint == p.fingerprint()
    with ready.db.read() as u:
        fill = u.session.query("SELECT price, commission, multiplier FROM sim_fills")[0]
    price, multiplier = D(fill["price"]), D(fill["multiplier"])
    assert price == D("21000.25") and multiplier == D(20)                                          # the ask, no slippage configured
    backtest_fee = costs.commission_usd(costs.fee_model(CostModel("schwab")), qty=D(1), price=price, multiplier=multiplier)
    assert D(fill["commission"]) == backtest_fee == D("2.25")
    assert hexec.status(done.proposal_id, db=ready.db).status == "filled" and hexec.status(engine_state="filled") == "filled"

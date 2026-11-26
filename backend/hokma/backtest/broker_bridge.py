"""The backtest takes the same orders as live execution (SPEC-020: L-59, N-14, N-16; decision 1215).

`to_proposal` turns what a strategy emits (`OrderIntent`) into the `OrderProposal` that H2 executes, so a strategy goes from backtest to a desk
without changing. What the proposal says about itself is derived, never declared: the line and the desk are arguments, the origin is always
the desk, the effect is read from the quantity held, the protective prices and the risk are worked out from the real entry price and the contract
sheet, and the quantity of a size by money comes from the real price and equity. `to_engine_order` and `to_decision` translate a proposal to what
the backtest engine consumes (`fills.Order`, `fills.Decision`); the engine then fills it with the fill rules and the cost tables of the backtest.

An order type outside the list of the broker of the desk is refused with the code of live (`bad_proposal`): the backtest only emulates what the
broker accepts, so backtest and live do not drift apart. This file never sends an order: the same proposal enters `hokma.exec.submit` of the
simulated broker from the live side.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from hokma.backtest import fills
from hokma.contracts.identity import ContractSheet
from hokma.exec.guards import Leg, OrderProposal
from hokma.factory.orders import OrderIntent, SizeType, intent_quantity, protective_price, stop_units

__all__ = ["BridgeError", "NO_BRACKET", "order_types", "supported_types", "to_decision", "to_engine_order", "to_proposal"]

# The order types an `OrderProposal` can carry: the same four that `hokma.exec.proposal.ORDER_TYPES` lists (a test keeps them equal).
PROPOSAL_TYPES: tuple[str, ...] = ("market", "limit", "stop", "stop_limit")
# What each type of the engine (fills.ORDER_TYPES) and of the strategy (OrderIntent) becomes in a proposal; None is a type no proposal can carry.
_ENGINE_TO_PROPOSAL: dict[str, str | None] = {"market": "market", "limit": "limit", "stop": "stop", "stop_limit": "stop_limit",
                                              "trailing_stop": None, "limit_if_touched": None}
_INTENT_TO_ENGINE = {"trailing": "trailing_stop"}
# A bracket (an exit stop and target) and a list of exits are carried by `stop_loss` and `take_profit` of the proposal.
_LISTS = ("bracket", "oco")
# The order types each broker kind takes in live: the translation tables of its connector (exec.submit, alpaca, ibkr and crypto take these four).
_BROKER_TYPES: dict[str, tuple[str, ...]] = {kind: PROPOSAL_TYPES for kind in ("sim", "schwab", "alpaca", "ibkr", "crypto")}
NO_BRACKET = ("ibkr", "crypto")                  # their connectors refuse stop_loss and take_profit ("brackets are not supported", "a spot account takes no bracket")


class BridgeError(ValueError):
    """The order cannot be carried by a proposal or by the broker of the desk. `code` is the one of live (bad_proposal)."""

    code = "bad_proposal"


def order_types() -> dict[str, str | None]:
    """N-14: every order type of the engine and how a proposal carries it: the proposal type, `stop_loss` plus `take_profit` for the lists of exits
    (bracket, oco), or None when no proposal can carry it (it can never reach a broker)."""
    return {**{name: _ENGINE_TO_PROPOSAL[name] for name in fills.ORDER_TYPES}, **{name: "stop_loss+take_profit" for name in _LISTS}}


def supported_types(broker_kind: str) -> tuple[str, ...]:
    """N-16: the order types the broker of a desk takes in live, so the backtest emulates only those."""
    try:
        return _BROKER_TYPES[broker_kind]
    except (KeyError, TypeError):
        raise BridgeError(f"unknown broker kind {str(broker_kind)[:40]!r}") from None


def _proposal_type(intent_type: str, broker_kind: str) -> str:
    engine_type = _INTENT_TO_ENGINE.get(intent_type, intent_type)
    carried = order_types().get(engine_type)
    if carried is None or carried not in supported_types(broker_kind):
        raise BridgeError(f"the {intent_type} order type is not supported by the {broker_kind} broker")
    return carried


def _quantity(intent: OrderIntent, sheet: ContractSheet | None, price: Decimal | None, equity: Decimal | None, atr: Decimal | None) -> Decimal:
    if sheet is not None and price is not None and equity is not None:
        return intent_quantity(intent, sheet=sheet, price=price, equity=equity, atr=atr)[0]
    if intent.size.kind is SizeType.CONTRACTS:
        return intent.size.value
    raise BridgeError(f"a size of kind {intent.size.kind.value} needs the contract sheet, the price and the equity of the account")


def _effect(intent: OrderIntent, qty: Decimal, held_qty: Decimal | None) -> str:
    """Close when the order only reduces what is held (the quantity held is a fact of the account); without it, only reduce_only says close."""
    if held_qty is None:
        return "close" if intent.reduce_only else "open"
    reduces = (held_qty > 0 and intent.side == "sell" and qty <= held_qty) or (held_qty < 0 and intent.side == "buy" and qty <= -held_qty)
    return "close" if reduces else "open"


def to_proposal(intent: OrderIntent, *, desk_id: int, line: str, broker_kind: str = "sim", sheet: ContractSheet | None = None,
                price: Decimal | None = None, equity: Decimal | None = None, atr: Decimal | None = None, held_qty: Decimal | None = None,
                created_by: str = "strategy") -> OrderProposal:
    """The proposal H2 executes for `intent`. `price` is the real entry price (the price the entry will be valued and the protection measured
    from), `equity` the real equity of the account, `held_qty` the signed quantity held of the contract; `sheet` is its contract sheet."""
    kind = _proposal_type(intent.order_type, broker_kind)
    if intent.has_bracket and broker_kind in NO_BRACKET:
        raise BridgeError(f"the {broker_kind} broker takes no stop loss or take profit")
    qty = _quantity(intent, sheet, price, equity, atr)
    entry = intent.limit if intent.limit is not None else price
    stop_loss = take_profit = risk = None
    if intent.has_bracket:
        if sheet is None or entry is None:
            raise BridgeError("a stop loss or take profit needs the contract sheet and the entry price")
        if intent.sl is not None:
            stop_loss = protective_price(intent.sl, side=intent.side, kind="sl", entry=entry, sheet=sheet, atr=atr)
            risk = stop_units(intent.sl, entry=entry, sheet=sheet, atr=atr) * qty * sheet.multiplier
        if intent.tp is not None:
            take_profit = protective_price(intent.tp, side=intent.side, kind="tp", entry=entry, sheet=sheet, atr=atr)
    leg = Leg(intent.contract_id, intent.side, _effect(intent, qty, held_qty), qty)
    return OrderProposal(desk_id, line, "desk", (leg,), kind, limit_price=intent.limit, stop_price=intent.stop, stop_loss=stop_loss,   # type: ignore[arg-type]
                         take_profit=take_profit, reduce_only=intent.reduce_only, risk_usd=risk, thesis=intent.tag or "", created_by=created_by)


def to_engine_order(proposal: OrderProposal) -> fills.Order:
    """The order of the engine of a one leg proposal: its type, side, quantity, trigger and limit."""
    if len(proposal.legs) != 1:
        raise BridgeError("the backtest engine takes one leg per order")
    leg = proposal.legs[0]
    return fills.Order(proposal.order_type, leg.side, leg.qty, trigger=proposal.stop_price, limit=proposal.limit_price)    # type: ignore[arg-type]


def to_decision(proposal: OrderProposal, *, ref_price: Decimal, held_qty: Decimal | None = None) -> fills.Decision:
    """What the engine runs for a market proposal: an entry with the protective distances worked out from `ref_price` (the price the entry waits
    for), or the exit of the whole position. A partial close is not something the engine can run: it raises."""
    order = to_engine_order(proposal)
    if order.type != "market":
        raise BridgeError(f"the engine runs an entry or an exit as a market order, not a {order.type} order")
    leg = proposal.legs[0]
    if leg.effect == "close":
        if held_qty is None or leg.qty != abs(held_qty):
            raise BridgeError("the engine closes the whole position: the order must close exactly what is held")
        return fills.Decision("exit")
    stop = None if proposal.stop_loss is None else abs(ref_price - proposal.stop_loss)
    target = None if proposal.take_profit is None else abs(proposal.take_profit - ref_price)
    return fills.Decision("enter", "long" if leg.side == "buy" else "short", leg.qty, stop_points=stop, target_points=target)

"""The last guard: the rules of SPEC-015 18.3 and 18.5 that already existed (`hokma.broker.rules.evaluate`), unchanged (SPEC-018).
It reads the proposal as the order of the rules (`rules_order`), with the contract id as the symbol, so one set of rules serves
every broker. `refused` blocks; `needs_owner` passes with the flags, and the order then asks the owner at any level."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from hokma.broker import rules
from hokma.exec.guards import GuardContext, GuardResult, blocked, passed
from hokma.exec.proposal import OrderProposal

_TYPES = {"market": "MARKET", "limit": "LIMIT", "stop": "STOP", "stop_limit": "STOP_LIMIT"}
_ASSET = {"option": "OPTION", "future_option": "OPTION", "future": "FUTURE", "crypto": "CRYPTO", "cfd": "CFD", "bond": "FIXED_INCOME",
          "bill": "FIXED_INCOME"}


def rules_order(p: OrderProposal, contracts: Any) -> dict[str, Any]:
    """The proposal in the shape `hokma.broker.rules` reads. Not sent to any broker."""
    legs = []
    for leg in p.legs:
        sheet = contracts.get(leg.contract_id)
        asset = _ASSET.get(sheet.asset_type, "EQUITY") if sheet is not None else "EQUITY"
        instrument: dict[str, Any] = {"symbol": leg.contract_id, "assetType": asset}
        if sheet is not None and asset == "OPTION":
            instrument.update(underlyingSymbol=sheet.underlying or "", optionMultiplier=int(sheet.multiplier), putCall="CALL" if sheet.put_call == "C" else "PUT")
        legs.append({"instruction": "BUY" if leg.side == "buy" else "SELL", "positionEffect": "OPENING" if leg.effect == "open" else "CLOSING",
                     "quantity": float(leg.qty), "instrument": instrument})
    order: dict[str, Any] = {"orderStrategyType": "SINGLE", "session": "NORMAL", "duration": "DAY" if p.tif == "day" else "GOOD_TILL_CANCEL",
                             "orderType": _TYPES[p.order_type], "orderLegCollection": legs}
    if p.limit_price is not None:
        order["price"] = float(p.limit_price)
    if p.stop_price is not None:
        order["stopPrice"] = float(p.stop_price)
    return order


def rules_positions(positions: Any) -> list[Any]:
    out = []
    for x in positions:
        qty = float(getattr(x, "qty", 0) or 0)
        out.append(SimpleNamespace(symbol=getattr(x, "contract_id", None) or getattr(x, "symbol", ""), qty=qty, long_qty=max(qty, 0.0),
                                   short_qty=max(-qty, 0.0), market_value=getattr(x, "market_value", None),
                                   multiplier=getattr(x, "multiplier", None), day_pl=getattr(x, "day_pl", None)))
    return out


class BrokerRulesGuard:
    name = "broker_rules"
    applies_to_closing = True               # the rules already exempt a closing order from capital and limits

    def check(self, ctx: GuardContext) -> GuardResult:
        result = rules.evaluate(
            action="place", orders_enabled=ctx.orders_enabled, desk_status=ctx.desk_status, account_ok=ctx.account_ok,
            order=rules_order(ctx.proposal, ctx.contracts), preview=ctx.preview, capital=float(ctx.capital),
            positions=rules_positions(ctx.positions), open_orders=list(ctx.open_orders), limits=dict(_plain(ctx.limits)),
            assets=list(ctx.assets), orders_today=ctx.orders_today, marks={k: float(v) for k, v in ctx.marks.items()})
        if result["result"] == "refused":
            return blocked(self.name, result["code"], result["why"], **{k: v for k, v in result["numbers"].items() if isinstance(v, (int, float))})
        if result["result"] == "needs_owner":
            return GuardResult(self.name, "pass", "needs_owner", result["why"],
                               {k: float(v) for k, v in result["numbers"].items() if isinstance(v, (int, float)) and not isinstance(v, bool)},
                               flags=tuple(result["flags"]))
        return passed(self.name)


def _plain(value: Any) -> Any:
    from collections.abc import Mapping

    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    return value

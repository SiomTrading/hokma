"""The projected position against a ceiling (SPEC-018: OA-44). The value of a position is its quantity times its price TIMES THE
MULTIPLIER OF THE CONTRACT SHEET: a guard that forgot the multiplier measured 42 percent where the position was 80 (NQ). A symbol
that has no position gets no exception, and a price that cannot be found blocks: a value that cannot be worked out cannot be held to a limit."""
from __future__ import annotations

from decimal import Decimal

from hokma.exec.guards import GuardContext, GuardResult, base_of, blocked, leg_price, multiplier_of, passed, pending_orders, position_qty, scan_orders, signed
from hokma.exec.money import ZERO


def _projected(ctx: GuardContext, contract_id: str, leg_signed: Decimal) -> Decimal:
    """The largest position the contract can reach with this order, whatever the sign of the position and whichever of the live orders fill:
    max(|pos + leg|, |pos + buys + leg|, |pos - sells + leg|). A pending order that reduces never lowers it."""
    pos = position_qty(ctx, contract_id)
    records = pending_orders(ctx.open_orders)
    buys = sum((q for cid, side, q in records if cid == contract_id and side == "buy"), ZERO)
    sells = sum((q for cid, side, q in records if cid == contract_id and side == "sell"), ZERO)
    return max(abs(pos + leg_signed), abs(pos + buys + leg_signed), abs(pos - sells + leg_signed))


class MaxPositionSizeGuard:
    name = "max_position"
    applies_to_closing = False

    def check(self, ctx: GuardContext) -> GuardResult:
        ceiling = ctx.limits.get("max_position_pct")
        if ceiling is None:
            return passed(self.name, "off")
        base = base_of(ctx)
        if base is None:
            return blocked(self.name, "no_base", "there is no equity or capital to measure the position against")
        if not scan_orders(ctx.open_orders)[1]:
            return blocked(self.name, "unreadable_orders", "an order that is working at the broker cannot be read whole")
        worst = ZERO
        for leg in ctx.proposal.legs:
            mult = multiplier_of(ctx, leg.contract_id)
            if mult is None:
                return blocked(self.name, "no_contract", f"{leg.contract_id} has no contract sheet, so its multiplier is unknown")
            price = leg_price(ctx, leg)
            if price is None:
                return blocked(self.name, "unpriced", f"{leg.contract_id} has no price to value the position")
            projected = _projected(ctx, leg.contract_id, signed(leg))
            pct = projected * price * mult / base * Decimal(100)
            worst = max(worst, pct)
            if pct > Decimal(str(ceiling)):
                return blocked(self.name, "max_position_pct", f"the position in {leg.contract_id} would be {pct:.1f}% of {base:,.2f}; the ceiling is {ceiling}%",
                               position_pct=pct, ceiling_pct=ceiling, multiplier=mult, base=base)
        return passed(self.name, position_pct=worst, ceiling_pct=ceiling)

"""Average cost rebuilt from the book (SPEC-018: OA-49), with the multiplier of the contract: buys and sells in order, partial sales, a
cross through zero and short positions. The cost of a position is the weighted average of what increased it; what reduces it books
realized profit against that average and leaves the average where it is; a cross closes the whole position and opens the other
side at the fill price. Commissions are kept apart so the profit can be shown gross and net."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from decimal import Decimal

from hokma.exec.money import ZERO


@dataclass(frozen=True)
class CostPosition:
    contract_id: str
    qty: Decimal                  # signed: long positive, short negative
    avg_price: Decimal            # of what is open (zero when flat)
    multiplier: Decimal
    realized: Decimal             # gross of commissions
    commissions: Decimal          # positive number: what was paid


def reconstruct(fills: Iterable[dict]) -> dict[str, CostPosition]:
    """`fills` are dicts with contract_id, qty (signed: buy positive, sell negative), price, multiplier and commission, in book order."""
    state: dict[str, CostPosition] = {}
    for f in fills:
        cid, dq, price, mult = f["contract_id"], f["qty"], f["price"], f["multiplier"]
        pos = state.get(cid) or CostPosition(cid, ZERO, ZERO, mult, ZERO, ZERO)
        qty, avg, realized = pos.qty, pos.avg_price, pos.realized
        if qty == 0 or (qty > 0) == (dq > 0):
            total = abs(qty) + abs(dq)
            avg = (abs(qty) * avg + abs(dq) * price) / total
            qty += dq
        else:
            closing = min(abs(dq), abs(qty))
            direction = Decimal(1) if qty > 0 else Decimal(-1)
            realized += (price - avg) * closing * direction * mult
            leftover = abs(dq) - closing
            qty += dq
            if qty == 0:
                avg = ZERO
            elif leftover > 0:                       # crossed through zero: the rest opens the other side at the fill price
                avg = price
        state[cid] = replace(pos, qty=qty, avg_price=avg, multiplier=mult, realized=realized,
                             commissions=pos.commissions + (f.get("commission") or ZERO))
    return state

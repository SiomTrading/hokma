"""The limits of the committee with clamping (SPEC-018: AH-18). The limits live in `desk_limits`; no agent changes them in this
function. An order that exceeds a limit is cut to the cap and the cut is recorded (`exec_proposals.clamps_json`); an order
that does not fit even one unit is refused.

By line: DT the contracts of a funded account (`funding.max_contracts`), SWC the margin (`max_margin_pct`), SWO the premium
(`max_premium_usd`), LP the weight of a position (`max_weight_pct`). Margin is counted at the full notional of the position."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import ROUND_DOWN, Decimal
from typing import Any

from hokma.exec.guards import normalize_effects, priced_with_mark
from hokma.exec.money import ZERO, dec_or_none
from hokma.exec.proposal import Leg, OrderProposal


@dataclass(frozen=True)
class LimitsResult:
    proposal: OrderProposal
    clamps: tuple[dict[str, Any], ...] = ()
    refused: str | None = None              # code: the order does not fit even one unit
    why: str = ""


def _scale(p: OrderProposal, room_units: Decimal, total_units: Decimal) -> OrderProposal | None:
    """Cut every opening leg to the same share of its quantity, in whole units, at least one. None when a leg would vanish."""
    if room_units >= total_units:
        return p
    factor = room_units / total_units
    legs: list[Leg] = []
    for leg in p.legs:
        if leg.effect != "open":
            legs.append(leg)
            continue
        qty = (leg.qty * factor).to_integral_value(rounding=ROUND_DOWN)
        if qty < 1:
            return None
        legs.append(replace(leg, qty=qty))
    return replace(p, legs=tuple(legs))


def _opening(p: OrderProposal) -> list[Leg]:
    return [g for g in p.legs if g.effect == "open"]


def apply_limits(p: OrderProposal, *, limits: Mapping[str, Any], contracts: Mapping[str, Any], positions: Sequence[Any], open_orders: Sequence[Any] = (),
                 equity: Decimal | None, marks: Mapping[str, Decimal]) -> LimitsResult:
    """The proposal after the limits of its line, with the record of every cut."""
    p = normalize_effects(p, positions, open_orders)            # the effect is read from the real positions, not from what the agent declared
    opening = _opening(p)
    if not opening:
        return LimitsResult(p)
    clamps: list[dict[str, Any]] = []
    current = p

    def price_of(leg: Leg) -> Decimal | None:
        return priced_with_mark(p, leg, marks)

    def held(cid: str) -> Decimal:
        return sum((dec_or_none(getattr(x, "qty", 0)) or ZERO for x in positions if getattr(x, "contract_id", None) == cid), ZERO)

    def cut(name: str, room: Decimal, total: Decimal, why: str) -> str | None:
        nonlocal current
        if room >= total:
            return None
        scaled = _scale(current, room, total) if room >= 1 else None
        if scaled is None:
            return f"the order does not fit one unit under {name}: {why}"
        clamps.append({"limit": name, "from_qty": str(total), "to_qty": str(sum((g.qty for g in _opening(scaled)), ZERO)), "why": why})
        current = scaled
        return None

    funding = limits.get("funding") or {}
    if p.line == "DT" and funding.get("max_contracts") is not None:
        cap = Decimal(str(funding["max_contracts"]))
        held_units = sum((abs(dec_or_none(getattr(x, "qty", 0)) or ZERO) for x in positions), ZERO)
        total = sum((g.qty for g in opening), ZERO)
        bad = cut("funding.max_contracts", cap - held_units, total, f"the account holds {held_units} and may hold {cap}")
        if bad:
            return LimitsResult(p, tuple(clamps), "limit_max_contracts", bad)

    if p.line == "SWO" and limits.get("max_premium_usd") is not None:
        cap = Decimal(str(limits["max_premium_usd"]))
        buys = [g for g in opening if g.side == "buy"]
        unit = ZERO
        for leg in buys:
            price, sheet = price_of(leg), contracts.get(leg.contract_id)
            if price is None or sheet is None:
                return LimitsResult(p, (), "limit_unpriced", f"{leg.contract_id} has no price or sheet to count its premium")
            unit += price * sheet.multiplier * leg.qty
        if buys and unit > cap:
            units = sum((g.qty for g in buys), ZERO)
            per_unit = unit / units
            bad = cut("max_premium_usd", (cap / per_unit).to_integral_value(rounding=ROUND_DOWN), units, f"premium {unit:,.2f} over {cap:,.2f}")
            if bad:
                return LimitsResult(p, tuple(clamps), "limit_max_premium", bad)

    if p.line == "SWC" and limits.get("max_margin_pct") is not None and equity:
        cap = equity * Decimal(str(limits["max_margin_pct"])) / Decimal(100)
        used = ZERO
        for x in positions:
            mv = dec_or_none(getattr(x, "market_value", None))
            used += abs(mv) if mv is not None else ZERO
        want = ZERO
        for leg in opening:
            price, sheet = price_of(leg), contracts.get(leg.contract_id)
            if price is None or sheet is None:
                return LimitsResult(p, (), "limit_unpriced", f"{leg.contract_id} has no price or sheet to count its margin")
            want += price * sheet.multiplier * leg.qty
        if used + want > cap:
            units = sum((g.qty for g in opening), ZERO)
            per_unit = want / units
            bad = cut("max_margin_pct", ((cap - used) / per_unit).to_integral_value(rounding=ROUND_DOWN) if cap > used else ZERO, units,
                      f"margin {used + want:,.2f} over {cap:,.2f}")
            if bad:
                return LimitsResult(p, tuple(clamps), "limit_max_margin", bad)

    if p.line == "LP" and limits.get("max_weight_pct") is not None and equity:
        cap = equity * Decimal(str(limits["max_weight_pct"])) / Decimal(100)
        for leg in opening:
            price, sheet = price_of(leg), contracts.get(leg.contract_id)
            if price is None or sheet is None:
                return LimitsResult(p, (), "limit_unpriced", f"{leg.contract_id} has no price or sheet to weigh it")
            unit = price * sheet.multiplier
            now_value = abs(held(leg.contract_id)) * unit
            room = ((cap - now_value) / unit).to_integral_value(rounding=ROUND_DOWN) if cap > now_value else ZERO
            leg_now = next((g for g in _opening(current) if g.contract_id == leg.contract_id), leg)
            bad = cut("max_weight_pct", room, leg_now.qty, f"weight of {leg.contract_id} over {limits['max_weight_pct']}%")
            if bad:
                return LimitsResult(p, tuple(clamps), "limit_max_weight", bad)

    return LimitsResult(current, tuple(clamps))

"""Exposure caps per instrument and per sector (SPEC-020: L-50). A new layer of the filter: no guard of H2 changes.

`max_symbol_pct` caps the value of everything the desk holds in one root (an underlying, a future root, a share) after the order;
`max_sector_pct` caps the value of every root of one sector. Both are a percentage of the base of the account (the equity of the book,
else the capital of the desk), valued with the multiplier of the contract sheet like the position ceiling. The sector of a root comes from
the `sectors` map of `desk_limits` (root to sector name): nothing is guessed, and a root with no sector cannot be held to a sector cap.
A value that cannot be worked out blocks: an exposure that cannot be measured cannot be held to a limit. It blocks openings only.
"""
from __future__ import annotations

from decimal import Decimal

from hokma.exec.guards import GuardContext, GuardResult, base_of, blocked, leg_price, multiplier_of, passed, position_qty, root_of, signed
from hokma.exec.money import ZERO, dec_or_none


def _sector_of(ctx: GuardContext, root: str) -> str | None:
    sectors = ctx.limits.get("sectors") or {}
    return sectors.get(root.upper())


class ExposureGuard:
    name = "exposure"
    applies_to_closing = False

    def check(self, ctx: GuardContext) -> GuardResult:
        symbol_cap, sector_cap = ctx.limits.get("max_symbol_pct"), ctx.limits.get("max_sector_pct")
        if symbol_cap is None and sector_cap is None:
            return passed(self.name, "off")
        base = base_of(ctx)
        if base is None:
            return blocked(self.name, "no_base", "there is no equity or capital to measure the exposure against")
        roots = {root_of(g.contract_id) for g in ctx.proposal.legs}
        sectors: dict[str, str] = {}
        if sector_cap is not None:
            for root in sorted(roots):
                sector = _sector_of(ctx, root)
                if sector is None:
                    return blocked(self.name, "no_sector", f"{root} has no sector in the limits of the desk, so a sector cap cannot be checked")
                sectors[root] = sector
        values = self._projected(ctx, roots, set(sectors.values()))
        if isinstance(values, GuardResult):
            return values
        numbers: dict[str, Decimal] = {}
        if symbol_cap is not None:
            for root in sorted(roots):
                pct = values[root] / base * Decimal(100)
                numbers["symbol_pct"] = max(numbers.get("symbol_pct", ZERO), pct)
                if pct > Decimal(str(symbol_cap)):
                    return blocked(self.name, "symbol_cap", f"{root} would be {pct:.1f}% of {base:,.2f}; the cap per symbol is {symbol_cap}%",
                                   symbol_pct=pct, cap_pct=symbol_cap, base=base)
        if sector_cap is not None:
            for sector in sorted(set(sectors.values())):
                total = sum((v for r, v in values.items() if _sector_of(ctx, r) == sector), ZERO)
                pct = total / base * Decimal(100)
                numbers["sector_pct"] = max(numbers.get("sector_pct", ZERO), pct)
                if pct > Decimal(str(sector_cap)):
                    return blocked(self.name, "sector_cap", f"the sector {sector} would be {pct:.1f}% of {base:,.2f}; the cap per sector is {sector_cap}%",
                                   sector_pct=pct, cap_pct=sector_cap, base=base)
        return passed(self.name, **numbers)

    def _projected(self, ctx: GuardContext, roots: set[str], sector_names: set[str]) -> dict[str, Decimal] | GuardResult:
        """The value of every root that matters (the roots of the order and the roots of their sectors) after the order is filled."""
        legs: dict[str, Decimal] = {}
        for leg in ctx.proposal.legs:
            legs[leg.contract_id] = legs.get(leg.contract_id, ZERO) + signed(leg)
        held: dict[str, Decimal] = {}
        for p in ctx.positions:
            cid = getattr(p, "contract_id", None)
            if cid:
                held[cid] = held.get(cid, ZERO) + (dec_or_none(getattr(p, "qty", 0)) or ZERO)
        values: dict[str, Decimal] = {}
        for cid in sorted(set(held) | set(legs)):
            root = root_of(cid)
            relevant = root in roots or _sector_of(ctx, root) in sector_names
            qty = held.get(cid, ZERO) + legs.get(cid, ZERO)
            if not relevant or qty == 0:
                continue
            priced = self._unit_value(ctx, cid)
            if isinstance(priced, GuardResult):
                return priced
            values[root] = values.get(root, ZERO) + abs(qty) * priced
        for root in roots:
            values.setdefault(root, ZERO)
        return values

    def _unit_value(self, ctx: GuardContext, cid: str) -> Decimal | GuardResult:
        """Price times multiplier of one unit of a contract: the price an order carries (the greater of its limit and the mark), else the mark,
        else the one the broker valued the position with."""
        leg = next((g for g in ctx.proposal.legs if g.contract_id == cid), None)
        mult = multiplier_of(ctx, cid)
        position = next((p for p in ctx.positions if getattr(p, "contract_id", None) == cid), None)
        if mult is None and position is not None:
            mult = dec_or_none(getattr(position, "multiplier", None))
        if mult is None:
            return blocked(self.name, "no_contract", f"{cid} has no contract sheet, so its multiplier is unknown")
        price = leg_price(ctx, leg) if leg is not None else dec_or_none(ctx.marks.get(cid))
        if price is None and position is not None:
            value, qty = dec_or_none(getattr(position, "market_value", None)), dec_or_none(getattr(position, "qty", None))
            if value is not None and qty:
                price = abs(value) / (abs(qty) * mult)
        if price is None:
            return blocked(self.name, "unpriced", f"{cid} has no price to value the exposure")
        return price * mult

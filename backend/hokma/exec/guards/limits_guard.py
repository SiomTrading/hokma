"""The guard that applies the limits of the committee with clamping (SPEC-018: AH-18, guard 7 of the pipeline)."""
from __future__ import annotations

from hokma.exec.guards import GuardContext, GuardResult, base_of, blocked, clamped, passed
from hokma.exec.limits import apply_limits


class LimitsGuard:
    name = "limits"
    applies_to_closing = False

    def check(self, ctx: GuardContext) -> GuardResult:
        result = apply_limits(ctx.proposal, limits=ctx.limits, contracts=ctx.contracts, positions=ctx.positions, open_orders=ctx.open_orders, equity=base_of(ctx), marks=ctx.marks)
        if result.refused:
            return blocked(self.name, result.refused, result.why)
        if result.clamps:
            return clamped(self.name, "clamped", "; ".join(c["why"] for c in result.clamps), result.proposal, result.clamps)
        return passed(self.name)

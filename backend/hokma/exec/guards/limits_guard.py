"""The guard that applies the limits of the committee with clamping (SPEC-018: AH-18, guard 7 of the pipeline).

A size limit that the owner made a NOTICE (SPEC-026) does not cut or refuse: the order goes on as if it were not there, and when it
would have been cut or refused the guard answers `needs_owner` so the owner decides. A cut that a restriction makes wins. Only the trip of a
limit (`limit_max_*`) is softened: a size that cannot be measured (no price or sheet, `limit_unpriced`) still blocks, as it always did."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hokma.exec.guards import GuardContext, GuardResult, base_of, blocked, clamped, passed
from hokma.exec.limits import apply_limits

# the size controls this guard applies and where each one is kept in the limits of the desk
SIZE_CONTROLS = {"size_premium": ("max_premium_usd",), "size_contracts": ("funding", "max_contracts"), "size_margin": ("max_margin_pct",),
                 "size_weight": ("max_weight_pct",)}


def _without(limits: Mapping[str, Any], paths: list[tuple[str, ...]]) -> dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in limits.items()}
    for path in paths:
        if len(path) == 1:
            out.pop(path[0], None)
        elif isinstance(out.get(path[0]), dict):
            out[path[0]].pop(path[1], None)
    return out


class LimitsGuard:
    name = "limits"
    applies_to_closing = False

    def check(self, ctx: GuardContext) -> GuardResult:
        from hokma.broker import rule_modes

        def run(limits: Mapping[str, Any]):
            return apply_limits(ctx.proposal, limits=limits, contracts=ctx.contracts, positions=ctx.positions, open_orders=ctx.open_orders,
                                equity=base_of(ctx), marks=ctx.marks)

        notices = [SIZE_CONTROLS[c] for c in SIZE_CONTROLS if rule_modes.is_notice(ctx.limits, c)]
        result = run(_without(ctx.limits, notices) if notices else ctx.limits)
        if result.refused:
            return blocked(self.name, result.refused, result.why)
        if result.clamps:
            return clamped(self.name, "clamped", "; ".join(c["why"] for c in result.clamps), result.proposal, result.clamps)
        if notices:
            full = run(ctx.limits)
            if full.refused and not full.refused.startswith("limit_max_"):      # a size that cannot be measured (limit_unpriced...) is not a notice: it blocks
                return blocked(self.name, full.refused, full.why)
            if full.refused or full.clamps:
                why = full.why if full.refused else "; ".join(c["why"] for c in full.clamps)
                return GuardResult(self.name, "pass", "needs_owner", why, flags=tuple(f"notice:{c}" for c in self._tripped(ctx, notices, run)))
        return passed(self.name)

    @staticmethod
    def _tripped(ctx: GuardContext, notices: list[tuple[str, ...]], run: Any) -> list[str]:
        """Which size controls in notice mode trip, ONE BY ONE: the order is run with only that control put back (everything else of the notices stays out, and
        that run passed), so a permission for one of them never covers another. When no single one explains it, all of them count."""
        by_path = {path: rule for rule, path in SIZE_CONTROLS.items()}
        alone = []
        for path in notices:
            others = [p for p in notices if p != path]
            one = run(_without(ctx.limits, others))
            if one.refused or one.clamps:
                alone.append(by_path[path])
        return alone or [by_path[p] for p in notices]

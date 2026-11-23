"""The pipeline of guards (SPEC-018: OA-43). The guards read a context that cannot be changed and run in the declared order; the
first BLOCK stops the line with its code and every result so far is kept. A clamp replaces the proposal for the next guards. An
order that only reduces risk skips the guards that declare `applies_to_closing = False`."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from hokma.exec.guards import Guard, GuardContext, GuardResult, blocked, is_closing, passed, scan_orders
from hokma.exec.proposal import OrderProposal


@dataclass(frozen=True)
class PrefilterResult:
    verdict: str                              # "PASS" or "BLOCK"
    proposal: OrderProposal                   # the original, or the clamped one
    results: tuple[GuardResult, ...]
    clamps: tuple[dict[str, Any], ...] = ()
    needs_owner: bool = False                 # a soft condition of the rules of the broker: the owner approves even at level 3
    closing: bool = False
    marks: Any = None                         # the market marks the guards used (a read-only mapping)

    @property
    def blocked_by(self) -> GuardResult | None:
        return next((r for r in self.results if r.verdict == "block"), None)

    def to_json(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "closing": self.closing, "needs_owner": self.needs_owner, "guards": [r.to_json() for r in self.results]}


def run(guards: Sequence[Guard], ctx: GuardContext) -> PrefilterResult:
    if not scan_orders(ctx.open_orders)[1]:        # a live order that cannot be read whole: nothing is projected from a partial list
        return PrefilterResult("BLOCK", ctx.proposal, (blocked("open_orders", "unreadable_orders", "an order that is working at the broker cannot be read whole, so the position it leaves cannot be known"),))
    results: list[GuardResult] = []
    clamps: list[dict[str, Any]] = []
    closing = is_closing(ctx.proposal, ctx.positions, ctx.open_orders)
    needs_owner = False
    current = ctx
    for guard in guards:
        if closing and not guard.applies_to_closing:
            results.append(passed(guard.name, "closing_exempt"))
            continue
        result = guard.check(current)
        results.append(result)
        if result.verdict == "block":
            return PrefilterResult("BLOCK", current.proposal, tuple(results), tuple(clamps), needs_owner, closing)
        if result.verdict == "clamp" and result.clamped is not None:
            clamps.extend(result.clamps)
            current = replace(current, proposal=result.clamped)
        if result.code == "needs_owner":
            needs_owner = True
    return PrefilterResult("PASS", current.proposal, tuple(results), tuple(clamps), needs_owner, closing)

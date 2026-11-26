"""The filter before an order, run by the backtest (SPEC-020: L-48, N-20; decision 1215).

A backtest order is judged by the SAME guards as a live order: this module builds the context of the guards from the simulated account of
the run and calls the pipeline of `hokma.exec.guards` (the fixed order of `registry`, `pipeline.run`). It has no rule of its own: the daily
loss, the drawdown from the peak, the position ceiling, the exposure caps and the rules of the broker are decided by the guards, with their
codes and with the limits of `desk_limits` that the caller passes in. Only the steps that the live filter takes before the pipeline are
repeated here, in the same order: the effect of every leg read from the real positions and the `reduce_only` clip.

Nothing here reads a database or reaches a broker: the account, the marks and the sheets are arguments.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from hokma.backoffice.snapshot import AccountSnapshot
from hokma.exec.guards import GuardContext, OrderProposal, blocked, normalize_effects, registry
from hokma.exec.guards.pipeline import PrefilterResult, run as run_pipeline
from hokma.exec.money import ZERO, clip_reduce_only as clip_to_position, dec_or_none

__all__ = ["SimAccount", "SimPosition", "PrefilterResult", "check", "blocked_code", "monitor_state"]


@dataclass(frozen=True)
class SimPosition:
    """A position of the simulated account with the fields that the guards read from a broker position."""
    contract_id: str
    qty: Decimal                                  # signed: long minus short
    multiplier: Decimal | None = None
    market_value: Decimal | None = None           # signed, as a broker gives it; None while the position is not valued
    day_pl: Decimal | None = None

    @property
    def symbol(self) -> str:
        return self.contract_id


@dataclass(frozen=True)
class SimAccount:
    """The state of the account of a run at one moment. `peak_equity` is the highest equity seen: the drawdown is measured from it."""
    cash: Decimal
    equity: Decimal
    peak_equity: Decimal
    day_pnl: Decimal = ZERO
    total_profit: Decimal = ZERO
    positions: tuple[SimPosition, ...] = ()
    orders_today: int = 0

    def with_equity(self, equity: Decimal) -> "SimAccount":
        """The account after the equity moved: the peak only goes up."""
        return dataclasses.replace(self, equity=equity, peak_equity=max(self.peak_equity, equity))

    def snapshot(self, at: datetime) -> AccountSnapshot:
        values = [p.market_value for p in self.positions]
        valued = None if any(v is None for v in values) else sum((v for v in values if v is not None), ZERO)
        return AccountSnapshot(0, at.strftime("%Y-%m-%d %H:%M:%S"), self.cash, valued, self.equity, self.day_pnl, peak_equity=self.peak_equity,
                               total_profit=self.total_profit, open_contracts=sum((abs(p.qty) for p in self.positions), ZERO),
                               positions=(), missing={} if valued is not None or not self.positions else {"positions": "a position has no value"})


def _clip_reduce_only(p: OrderProposal, positions: Sequence[Any]) -> tuple[OrderProposal | None, list[dict[str, Any]]]:
    """The `reduce_only` clip of the live filter: a leg bigger than the position is cut to it, a leg with nothing to reduce is dropped."""
    held: dict[str, Decimal] = {}
    for x in positions:
        held[x.contract_id] = held.get(x.contract_id, ZERO) + (dec_or_none(x.qty) or ZERO)
    legs, clamps = [], []
    for leg in p.legs:
        qty, clipped = clip_to_position(leg.side, leg.qty, held.get(leg.contract_id, ZERO))
        if clipped:
            clamps.append({"limit": "reduce_only", "contract_id": leg.contract_id, "from_qty": str(leg.qty), "to_qty": str(qty),
                           "why": "a reduce-only order cannot open the opposite position"})
        if qty > 0:
            legs.append(dataclasses.replace(leg, qty=qty, effect="close"))
    return (dataclasses.replace(p, legs=tuple(legs)) if legs else None), clamps


def check(proposal: OrderProposal, account: SimAccount, *, limits: Mapping[str, Any], sheets: Mapping[str, Any], marks: Mapping[str, Decimal],
          now: datetime, capital: Decimal, level: int = 2, assets: Sequence[str] = (), windows: Sequence[Any] = (), state: Mapping[str, Any] | None = None,
          desk_status: str = "active", orders_enabled: bool = True, account_ok: bool = True, open_orders: Sequence[Any] = (),
          preview: Any = None, skip: Sequence[str] = ()) -> PrefilterResult:
    """PASS or BLOCK for `proposal` against the simulated `account`, by the guards of the live filter: the same codes, the same clamps and
    the same limits. `limits` is the content of `desk_limits` of the desk, `state` the clocks of the guards (the cooldown), `windows` the
    calendar windows at `now`. A desk limit that needs a fact the account does not have blocks, as it does live."""
    ctx = GuardContext(proposal=proposal, contracts=sheets, positions=tuple(account.positions), book=account.snapshot(now), limits=limits, level=level,  # type: ignore[arg-type]
                       windows=tuple(windows), now=now, desk_id=proposal.desk_id, assets=tuple(assets), capital=capital, desk_status=desk_status,
                       marks=marks, state=state or {}, orders_enabled=orders_enabled, account_ok=account_ok, preview=preview,
                       open_orders=tuple(open_orders), orders_today=account.orders_today)
    ctx = dataclasses.replace(ctx, proposal=normalize_effects(ctx.proposal, ctx.positions))
    cuts: list[dict[str, Any]] = []
    if proposal.reduce_only:
        reduced, cuts = _clip_reduce_only(proposal, ctx.positions)
        if reduced is None:
            return PrefilterResult("BLOCK", proposal, (blocked("reduce_only", "nothing_to_reduce", "there is no position to reduce"),), tuple(cuts), False, True)
        ctx = dataclasses.replace(ctx, proposal=reduced)
    result = dataclasses.replace(run_pipeline([g for g in registry.build(ctx.limits) if g.name not in skip], ctx), marks=ctx.marks)
    return dataclasses.replace(result, clamps=tuple(cuts) + result.clamps) if cuts else result


def monitor_state(now: datetime) -> dict[str, Any]:
    """The clocks of the guards for a run in which the emergency switch measured the desk at `now`. A backtest has no monitor process: its runner
    measures the account at every bar, so the monitor guard sees a fresh heartbeat. Without it a desk with drawdown or funding limits does not open (as live)."""
    return {"killswitch:heartbeat": {"at": now.strftime("%Y-%m-%d %H:%M:%S")}}


def blocked_code(result: PrefilterResult) -> str | None:
    """The code of the guard that blocked, or None when the order passes."""
    block = result.blocked_by
    return None if block is None else block.code

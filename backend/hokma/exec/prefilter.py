"""The filter before an order (SPEC-018: DTS-06, OA-43 to OA-47, AH-18). `run(desk_id, proposal)` reads the facts (the sheets, the
positions, the snapshot of the book, the limits, the calendar windows, the clocks of the guards) and runs the pipeline of guards:
PASS or BLOCK, the clamped proposal, and every result for the record. The order of the guards is fixed (`guards/registry.py`) and the
rules of SPEC-015 are the last one. The filter runs at every level and for every origin."""
from __future__ import annotations

import contextvars
import dataclasses
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from hokma.db import Database, get_database
from hokma.exec import levels
from hokma.exec.guards import GuardContext, blocked, normalize_effects
from hokma.exec.guards import cooldown as cooldown_guard
from hokma.exec.guards.pipeline import PrefilterResult, run as run_pipeline
from hokma.exec.guards.registry import build
from hokma.exec.money import ZERO, clip_reduce_only as clip_to_position, dec_or_none
from hokma.exec.proposal import OrderProposal

__all__ = ["PrefilterResult", "load_context", "run", "set_mark_source"]

# Set only by `submit.flatten`, around the closes it generates, to the id of the desk it flattens. It is a context variable and not a field of the proposal:
# nothing an agent sends can set it. While it holds the desk of an order that is reduce-only, the desk being paused or the orders switch being off does
# not stop that close (every other guard still applies; the order can neither open nor cross zero).
EMERGENCY_CLOSE: contextvars.ContextVar[tuple[int, str] | None] = contextvars.ContextVar("hokma_emergency_close", default=None)

MarkSource = Callable[[list[str]], Mapping[str, Decimal]]
_mark_source: MarkSource | None = None


def set_mark_source(source: MarkSource | None) -> None:
    """The prices the filter values an order with when the proposal has none (the stream of `hokma.market`, or a test)."""
    global _mark_source
    _mark_source = source


def _day_start(now: datetime) -> str:
    return now.strftime("%Y-%m-%d 00:00:00")


def _normalize_positions(positions: Any) -> tuple[Any, ...]:
    from hokma.contracts import symbols
    from hokma.contracts.identity import NoContract

    out = []
    for p in positions or ():
        if getattr(p, "contract_id", None):
            out.append(p)
            continue
        try:
            cid = symbols.from_provider("schwab", p.symbol)
        except NoContract:
            cid = None
        out.append(dataclasses.replace(p, contract_id=cid) if cid and dataclasses.is_dataclass(p) else p)
    return tuple(out)


def _state(desk_id: int, database: Database) -> dict[str, Any]:
    from hokma.exec import killswitch

    state = dict(cooldown_guard.load_state(desk_id, db=database))
    beat = killswitch.load_heartbeat(desk_id, db=database)
    if beat:
        state["killswitch:heartbeat"] = beat
    return state


def _marks_of(positions: tuple[Any, ...]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    if _mark_source is not None:
        ids = [getattr(x, "contract_id", None) for x in positions if getattr(x, "contract_id", None)]
        if ids:
            out.update({k: dec_or_none(v) for k, v in _mark_source(ids).items() if dec_or_none(v) is not None})
    return out


def _marks(p: OrderProposal, positions: tuple[Any, ...]) -> dict[str, Decimal]:
    marks: dict[str, Decimal] = {}
    for x in positions:
        cid, qty, value = getattr(x, "contract_id", None), dec_or_none(getattr(x, "qty", None)), dec_or_none(getattr(x, "market_value", None))
        mult = dec_or_none(getattr(x, "multiplier", None)) or Decimal(1)
        if cid and qty and value is not None:
            marks[cid] = abs(value) / (abs(qty) * mult)
    if _mark_source is not None:
        marks.update({k: dec_or_none(v) for k, v in _mark_source([g.contract_id for g in p.legs]).items() if dec_or_none(v) is not None})
    return marks


def load_context(desk_id: int, p: OrderProposal, *, db: Database | None = None, now: datetime | None = None, preview: Any = None,
                 sheets: Mapping[str, Any] | None = None) -> GuardContext:
    """Everything the guards read, gathered once. Blocking (the broker's positions may be a network call): call it from a thread."""
    from hokma.backoffice import snapshot as snapshot_module
    from hokma.broker import service
    from hokma.contracts import discipline
    from hokma.econ_calendar import windows as event_windows

    database = db or get_database()
    now = now or datetime.now(timezone.utc)
    level, _rules = levels.get_level(desk_id, db=database)
    positions: tuple[Any, ...] = ()
    open_orders: tuple[Any, ...] = ()
    book = None
    with database.read() as u:                      # the broker lives as long as this unit: its reads happen inside it
        ctx = service._load(u, desk_id)
        pair = service.broker_of_desk(u, desk_id)
        limits = dict(ctx.limits)
        today = u.broker.count_sent_orders(desk_id, _day_start(now))
        assets, capital, status = tuple(ctx.assets), Decimal(str(ctx.desk.capital)), ctx.desk.status
        enabled, account_ok = ctx.enabled, ctx.trading
        if pair is not None:
            broker, account = pair
            if broker.capabilities.positions:
                positions = _normalize_positions(broker.positions(account.id))
            if broker.capabilities.orders:
                open_orders = tuple(broker.orders(account.id) or ())
            balance = broker.balance(account.id) if broker.kind != "sim" else None
            book = snapshot_module.snapshot(account.id, db=database, positions=positions, balance=balance, prices=_marks_of(positions))
    if book is None:
        book = snapshot_module.AccountSnapshot(0, now.strftime("%Y-%m-%d %H:%M:%S"), None, None, None, None,
                                              missing={"account": "the desk has no account"})
    contract_sheets = dict(sheets) if sheets is not None else {k: v for k, v in discipline.load_sheets([g.contract_id for g in p.legs]).items() if v is not None}
    return GuardContext(
        proposal=p, contracts=contract_sheets, positions=positions, book=book, limits=limits, level=level,
        windows=event_windows.windows(desk_id, at=now, limits=limits, db=database), now=now, desk_id=desk_id, assets=assets, capital=capital,
        desk_status=status, marks=_marks(p, positions), state=_state(desk_id, database),
        orders_enabled=enabled, account_ok=account_ok, preview=preview, open_orders=open_orders, orders_today=today)


def clip_reduce_only(p: OrderProposal, positions: tuple[Any, ...]) -> tuple[OrderProposal | None, list[dict[str, Any]]]:
    """OA-56: a `reduce_only` order never opens the opposite position. A leg bigger than the position is cut to it (the cut is recorded);
    a leg with nothing to reduce is dropped; no leg left gives None."""
    held: dict[str, Decimal] = {}
    for x in positions:
        cid = getattr(x, "contract_id", None)
        if cid:
            held[cid] = held.get(cid, ZERO) + (dec_or_none(getattr(x, "qty", None)) or ZERO)
    legs, clamps = [], []
    for leg in p.legs:
        qty, clipped = clip_to_position(leg.side, leg.qty, held.get(leg.contract_id, ZERO))
        if clipped:
            clamps.append({"limit": "reduce_only", "contract_id": leg.contract_id, "from_qty": str(leg.qty), "to_qty": str(qty),
                           "why": "a reduce-only order cannot open the opposite position"})
        if qty > 0:
            legs.append(dataclasses.replace(leg, qty=qty, effect="close"))
    return (dataclasses.replace(p, legs=tuple(legs)) if legs else None), clamps


def run(desk_id: int, p: OrderProposal, *, db: Database | None = None, now: datetime | None = None, preview: Any = None,
        sheets: Mapping[str, Any] | None = None, skip: tuple[str, ...] = (), virtual: tuple[Any, ...] = (),
        exit_budget: Mapping[str, Decimal] | None = None) -> PrefilterResult:
    """PASS or BLOCK for a proposal, with the clamped proposal and every guard result."""
    ctx = load_context(desk_id, p, db=db, now=now, preview=preview, sheets=sheets)
    if p.reduce_only and p.origin == "chat" and EMERGENCY_CLOSE.get() == (desk_id, "owner") and ctx.account_ok:
        # the owner's flatten: a PAUSED desk and the switch of the broker do not stop this close. An archived desk, or any other state, still refuses.
        ctx = dataclasses.replace(ctx, desk_status="active" if ctx.desk_status == "paused" else ctx.desk_status, orders_enabled=True)
    if virtual:                                  # what the ANCESTORS of a nested order's node open, for that node (guards only; the snapshot is the real one)
        ctx = dataclasses.replace(ctx, positions=ctx.positions + tuple(virtual))
    if exit_budget:                              # an exit of a nested order: the whole tree can take out of a contract no more than there is of it
        held: dict[str, Decimal] = {}
        for x in ctx.positions:
            if getattr(x, "contract_id", None):
                held[x.contract_id] = held.get(x.contract_id, ZERO) + (dec_or_none(getattr(x, "qty", 0)) or ZERO)
        for leg in p.legs:
            if leg.contract_id not in exit_budget:
                continue
            have = held.get(leg.contract_id, ZERO)
            if have == 0 or not ((have > 0 and leg.side == "sell") or (have < 0 and leg.side == "buy")) or exit_budget[leg.contract_id] > abs(have):
                return PrefilterResult("BLOCK", p, (blocked("exit_budget", "exit_exceeds",
                                                            f"the exits of this order add up to {exit_budget[leg.contract_id]} of {leg.contract_id} and there is {abs(have)} to exit"),),
                                       (), False, False)
    cuts: list[dict[str, Any]] = []
    ctx = dataclasses.replace(ctx, proposal=normalize_effects(ctx.proposal, ctx.positions, ctx.open_orders))
    if p.reduce_only:
        reduced, cuts = clip_reduce_only(p, ctx.positions)
        if reduced is None:
            return PrefilterResult("BLOCK", p, (blocked("reduce_only", "nothing_to_reduce", "there is no position to reduce"),), tuple(cuts), False, True)
        ctx = dataclasses.replace(ctx, proposal=reduced)
    result = dataclasses.replace(run_pipeline([g for g in build(ctx.limits) if g.name not in skip], ctx), marks=ctx.marks)
    return dataclasses.replace(result, clamps=tuple(cuts) + result.clamps) if cuts else result

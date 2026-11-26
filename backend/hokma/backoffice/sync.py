"""Sync by listing (SPEC-018: OA-42). ONE call to the orders of the broker and ONE call to its positions bring the local state up to date:

- an order that Hokma sent and the broker lists takes the status the broker gives it;
- an order that Hokma sent and the broker no longer lists is read once by its id and ends in the final status the broker gives;
- the executions of a listed or looked up order that are not in the book are written to it (source: the broker, reference: the order and the
  execution), so running the sync twice writes nothing the second time (the book refuses a repeated source, reference and kind);
- the positions of the broker are compared with the book and the differences are returned: this is a report, the reconciliation proper
  (`recon.py`) explains them and nothing here adjusts the book.

The broker calls are made first and the database is written after, in one transaction. What the agent says about an order is never read: the
status, the quantities and the prices are the broker's."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from hokma.backoffice import desk_owner
from hokma.backoffice import book as book_module
from hokma.backoffice import decisions, provenance
from hokma.broker.types import Fill
from hokma.contracts import identity
from hokma.db import Database, RuleViolation, get_database
from hokma.db.repos.conversations import like
from hokma.db.timeutil import to_db_time, utcnow
from hokma.exec.money import ZERO, MoneyError, dec, dec_or_none

OPEN_PROPOSALS = ("sent", "partially_filled")
FINAL_WORDS = {"FILLED": "filled", "CANCELED": "canceled", "REPLACED": "canceled", "REJECTED": "rejected_by_broker", "EXPIRED": "expired"}
MAX_OPEN = 200


class SyncError(RuntimeError):
    """The sync could not read what it needs."""


@dataclass(frozen=True)
class Execution:
    ref: str
    qty: Decimal
    price: Decimal
    at: str
    commission: Decimal
    symbol: str | None = None
    side: str | None = None


@dataclass
class SyncReport:
    account_id: int
    orders_listed: int = 0
    positions_listed: int = 0
    booked: int = 0
    duplicates: int = 0
    updated: list[dict[str, Any]] = field(default_factory=list)
    looked_up: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    position_diffs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "orders_listed": self.orders_listed, "positions_listed": self.positions_listed, "booked": self.booked,
                "duplicates": self.duplicates, "updated": self.updated, "looked_up": self.looked_up, "unresolved": self.unresolved,
                "position_diffs": self.position_diffs}


def _side(instruction: Any) -> str | None:
    word = str(instruction or "").strip().upper()
    return "buy" if word.startswith("BUY") else "sell" if word.startswith("SELL") else None


def executions(order: Any) -> list[Execution]:
    """The executions an order carries: the generic list `data['executions']` ({id, qty, price, time, commission, symbol, side}) or the
    shape of the activity collection of an order of Schwab. An order with no id has none: its executions could never be told apart."""
    oid = getattr(order, "order_id", None)
    data = order.data if isinstance(getattr(order, "data", None), dict) else {}
    if not oid:
        return []
    out: list[Execution] = []
    fallback_time = str(getattr(order, "entered_at", None) or utcnow())
    for i, item in enumerate(data.get("executions") or []):
        if isinstance(item, dict):
            out.append(Execution(f"{oid}:{item.get('id', i)}", dec(item["qty"], "execution qty"), dec(item["price"], "execution price"),
                                 str(item.get("time") or fallback_time), dec(item.get("commission") or 0, "commission"), item.get("symbol"), item.get("side")))
    legs = list(getattr(order, "legs", ()) or ())
    for a, activity in enumerate(data.get("orderActivityCollection") or []):
        for j, leg in enumerate(activity.get("executionLegs") or [] if isinstance(activity, dict) else []):
            index = int(leg.get("legId") or 1) - 1
            owner = legs[index] if 0 <= index < len(legs) else (legs[0] if legs else {})
            out.append(Execution(f"{oid}:{a}.{j}", dec(leg["quantity"], "execution qty"), dec(leg["price"], "execution price"),
                                 str(leg.get("time") or fallback_time), ZERO, owner.get("symbol") or getattr(order, "symbol", None), _side(owner.get("instruction"))))
    return out


def final_status(order: Any) -> str | None:
    """The status of the proposal that an order of the broker means: a final one, or `partially_filled` / None while it still works."""
    word = str(getattr(order, "status", "") or "").upper()
    filled = dec_or_none(getattr(order, "filled_qty", None)) or ZERO
    if word in FINAL_WORDS:
        status = FINAL_WORDS[word]
        return "partially_filled" if status in ("canceled", "expired") and filled > 0 else status
    return "partially_filled" if filled > 0 else None


@dataclass
class Collected:
    account_id: int
    orders: list[Any]
    positions: list[Any]
    looked_up: dict[str, Any]
    unreadable: list[str]


def _contract(symbol: str | None, contract_id: str | None) -> tuple[str, Decimal]:
    try:
        sheet = identity.resolve(contract_id or identity.canonical_id(identity.parse_symbol(symbol or "")))
    except identity.NoContract as exc:
        raise MoneyError(f"{(symbol or contract_id or '?')[:40]} has no contract sheet") from exc
    return sheet.contract_id, sheet.multiplier


def open_order_ids(u: Any, account_id: int) -> dict[str, dict[str, Any]]:
    """The broker order ids of the proposals of this account that are still open, with their proposal row."""
    rows = u.session.query(
        "SELECT p.*, o.broker_order_id AS broker_order_id FROM exec_proposals p JOIN broker_orders o ON o.id = p.broker_order_record_id "
        "WHERE o.account_id = :a AND p.status IN ('sent', 'partially_filled') AND o.broker_order_id IS NOT NULL ORDER BY p.id LIMIT :n",
        {"a": account_id, "n": MAX_OPEN})
    return {r["broker_order_id"]: r for r in rows}


def collect(broker: Any, account_id: int, open_ids: list[str]) -> Collected:
    """The broker calls of one sync: the listing of orders (one call), the positions (one call) and, for each open order that the listing
    does not hold, the order read by its id (one call each)."""
    orders = list(broker.orders(account_id))
    positions = list(broker.positions(account_id))
    listed = {str(o.order_id) for o in orders if getattr(o, "order_id", None) is not None}
    looked_up: dict[str, Any] = {}
    unreadable: list[str] = []
    for oid in open_ids:
        if oid not in listed:
            found = broker.get_order(account_id, oid)
            if found is None:
                unreadable.append(oid)
            else:
                looked_up[oid] = found
    return Collected(account_id, orders, positions, looked_up, unreadable)


def apply(u: Any, got: Collected, open_rows: dict[str, dict[str, Any]]) -> SyncReport:
    """Write what `collect` read, inside an open write unit."""
    account = u.broker.get_account(got.account_id)
    kind = u.broker.get_broker(account.broker_id).kind
    report = SyncReport(got.account_id, len(got.orders), len(got.positions), looked_up=sorted(got.looked_up), unresolved=[f"{o}: the broker does not know the order" for o in got.unreadable])
    if kind not in provenance.SOURCES:
        report.unresolved.append(f"broker kind {kind!r} is not a source of the book")
        return report
    known = {str(r["broker_order_id"]) for r in u.session.query("SELECT broker_order_id FROM broker_orders WHERE account_id = :a AND broker_order_id IS NOT NULL", {"a": got.account_id})}
    by_id = {str(o.order_id): o for o in got.orders if getattr(o, "order_id", None) is not None}
    by_id.update(got.looked_up)
    for oid, order in sorted(by_id.items()):
        if oid not in known:                                    # an order Hokma never sent is not booked here: it is the reconciliation's to find
            continue
        desk_id, _strategy = desk_owner.owner_of_fill(u.session, got.account_id, oid)       # the desk of the order Hokma sent, not the account's first desk
        complete = _book_order(u, kind, got.account_id, desk_id, oid, order, report)
        row = open_rows.get(oid)
        status = final_status(order)
        if complete and row is not None and status is not None and status != row["status"]:       # an order whose fills could not be booked stays open and is tried again
            changed = u.session.execute("UPDATE exec_proposals SET status = :s, updated_at = :t WHERE id = :i AND status IN ('sent', 'partially_filled')",
                                        {"s": status, "t": utcnow(), "i": row["id"]})
            if changed == 1:
                report.updated.append({"proposal_id": row["id"], "order_id": oid, "status": status})
                try:
                    decisions.resolve_in(u, row["id"])
                except RuleViolation:
                    pass
    report.position_diffs = _diffs(u, got)
    return report


def _booked_qty(u: Any, account_id: int, oid: str) -> Decimal:
    rows = u.session.query("SELECT source_ref, qty FROM book_entries WHERE account_id = :a AND kind = 'fill' AND source_ref LIKE :p ESCAPE '\\'",
                           {"a": account_id, "p": like(f"{oid}:")})
    return sum((abs(Decimal(r["qty"])) for r in rows if r["source_ref"].startswith(f"{oid}:") or f":{oid}:" in r["source_ref"]), ZERO)


def _book_order(u: Any, kind: str, account_id: int, desk_id: int | None, oid: str, order: Any, report: SyncReport) -> bool:
    """Book the executions of one order. True when everything it filled is in the book."""
    problems = len(report.unresolved)
    try:
        found = executions(order)
    except (MoneyError, KeyError, TypeError, ValueError) as exc:
        report.unresolved.append(f"{oid}: the executions could not be read ({exc})")
        return False
    for ex in found:
        try:
            symbol = ex.symbol or getattr(order, "symbol", None)
            cid, multiplier = _contract(symbol, getattr(order, "contract_id", None) if not ex.symbol else None)
            side = ex.side or _side(getattr(order, "side", None))
            if side is None:
                raise MoneyError("the side of the execution is neither a buy nor a sell")
            stated = dec_or_none(getattr(order, "multiplier", None))
            if stated is not None and stated != multiplier:
                raise MoneyError(f"the broker multiplier {stated} is not the {multiplier} of the contract sheet")
            fill = Fill(account_id, cid, side, ex.qty, ex.price, ex.commission, to_db_time(ex.at) or utcnow(), order_id=oid, multiplier=multiplier, contract_id=cid)
            made = book_module.record_fill_in(u.session, fill, desk_id=desk_id, source=kind, source_ref=ex.ref)
        except (MoneyError, provenance.MissingProvenance, ValueError) as exc:
            report.unresolved.append(f"{oid}: {exc}")
            continue
        report.booked += 1 if made is not None else 0
        report.duplicates += 1 if made is None else 0
    filled = dec_or_none(getattr(order, "filled_qty", None)) or ZERO
    if filled > _booked_qty(u, account_id, oid) and not found:
        report.unresolved.append(f"{oid}: {filled} filled but the broker gave no executions and none are booked")
    return len(report.unresolved) == problems


def _diffs(u: Any, got: Collected) -> list[dict[str, Any]]:
    held = {cid: p.qty for cid, p in book_module.Book.load(got.account_id, session=u.session).cost_positions().items()}
    theirs: dict[str, Decimal] = {}
    for p in got.positions:
        try:
            cid = getattr(p, "contract_id", None) or identity.canonical_id(identity.parse_symbol(p.symbol))
        except identity.NoContract:
            cid = str(p.symbol)
        theirs[cid] = theirs.get(cid, ZERO) + dec(p.qty, "position qty")
    return [{"contract_id": cid, "broker_qty": theirs.get(cid, ZERO), "book_qty": held.get(cid, ZERO)}
            for cid in sorted(set(held) | set(theirs)) if theirs.get(cid, ZERO) != held.get(cid, ZERO)]


def sync(account_id: int, *, broker: Any, db: Database | None = None) -> SyncReport:
    """The sync of one account with a broker object the caller holds. Do not call it inside a write unit of the same database: the broker is
    read first and the writes come after (use `sync_desk`, which opens and closes the broker around the reads)."""
    database = db or get_database()
    with database.read() as u:
        open_rows = open_order_ids(u, account_id)
    got = collect(broker, account_id, list(open_rows))
    with database.write() as u:
        report = apply(u, got, open_rows)
    _fills_booked(account_id, report)
    return report


def _fills_booked(account_id: int, report: "SyncReport") -> None:
    """A fill that came by the sync is an event of the live freeze (SPEC-026, 3390c): it is measured at once."""
    if report.booked:
        from hokma.exec import live

        live.after_fill(account_id)


def sync_desk(desk_id: int, *, account_id: int | None = None, db: Database | None = None) -> SyncReport:
    """The sync of one account of a desk: the broker is opened for the reads and closed before anything is written. A desk with one account
    syncs it; with several, `account_id` says which (use `sync_desk_accounts` for all). It never picks "the first"."""
    from hokma.broker import service

    database = db or get_database()
    with database.read() as u:
        linked = [a.id for a in u.broker.accounts_of_desk(desk_id)]
        if not linked:
            raise SyncError(f"desk {desk_id} has no account")
        if account_id is None and len(linked) > 1:
            raise SyncError(f"desk {desk_id} has {len(linked)} accounts: say which one ({', '.join(map(str, linked))})")
        try:
            broker, account = service.broker_of(u, desk_id, linked[0] if account_id is None else account_id)
        except service.BrokerError as exc:
            raise SyncError(f"account {account_id} is not an account of desk {desk_id}") from exc
        open_rows = open_order_ids(u, account.id)
        got = collect(broker, account.id, list(open_rows))
    with database.write() as u:
        report = apply(u, got, open_rows)
    _fills_booked(account.id, report)
    return report


def sync_desk_accounts(desk_id: int, *, db: Database | None = None) -> list[SyncReport]:
    """The sync of every account of a desk, one account at a time (an account shared with another desk is synced once per call, whoever asks)."""
    database = db or get_database()
    with database.read() as u:
        linked = [a.id for a in u.broker.accounts_of_desk(desk_id)]
    return [sync_desk(desk_id, account_id=aid, db=database) for aid in linked]

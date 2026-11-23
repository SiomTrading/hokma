"""The book of an account (SPEC-018: FT-103, OA-42, OA-37). Every fill of every broker, simulated or real, is written once as an append
only entry of `book_entries`: a `fill` with the cash it moved (a buy is negative) and a `commission`. `Book` rebuilds the positions,
the summary, the performance and the profit and loss from the entries, with the multiplier of the contract sheet.

The cash of the simulated broker is the amount of the last `open` or `reset` of `account_ledger` plus the sum of the entries after it."""
from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hokma.backoffice import provenance
from hokma.backoffice.cost_basis import CostPosition, reconstruct
from hokma.db import Database, get_database
from hokma.db.timeutil import utcnow
from hokma.exec.money import ZERO, MoneyError, dec, text


@dataclass(frozen=True)
class BookEntry:
    id: int
    account_id: int
    desk_id: int | None
    kind: str
    contract_id: str | None
    qty: Decimal | None
    price: Decimal | None
    multiplier: Decimal | None
    amount: Decimal
    source: str
    source_ref: str
    occurred_at: str
    note: str | None = None

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> "BookEntry":
        def d(v: Any) -> Decimal | None:
            return None if v in (None, "") else Decimal(str(v))

        return cls(r["id"], r["account_id"], r.get("desk_id"), r["kind"], r.get("contract_id"), d(r.get("qty")), d(r.get("price")), d(r.get("multiplier")),
                   Decimal(str(r["amount"])), r["source"], r["source_ref"], str(r["occurred_at"]), r.get("note"))


def _stamp(value: Any) -> str:
    from hokma.db.timeutil import to_db_time

    return to_db_time(value) or utcnow()


def append_in(session: Any, account_id: int, *, kind: str, amount: Decimal, source: str, source_ref: str, occurred_at: Any = None, desk_id: int | None = None,
              contract_id: str | None = None, qty: Decimal | None = None, price: Decimal | None = None, multiplier: Decimal | None = None,
              note: str | None = None) -> int | None:
    """One entry, once, inside the transaction of `session`: the same (source, source_ref, kind) is not written twice (None when it existed)."""
    if kind not in provenance.KINDS:
        raise provenance.MissingProvenance(f"kind must be one of {', '.join(provenance.KINDS)}")
    source, source_ref = provenance.check(source, source_ref)
    if session.one("SELECT 1 AS x FROM book_entries WHERE source = :s AND source_ref = :r AND kind = :k", {"s": source, "r": source_ref, "k": kind}) is not None:
        return None
    return session.insert("book_entries", {
        "account_id": account_id, "desk_id": desk_id, "kind": kind, "contract_id": contract_id, "qty": text(qty), "price": text(price),
        "multiplier": text(multiplier), "amount": text(amount), "currency": "USD", "source": source, "source_ref": source_ref,
        "occurred_at": _stamp(occurred_at), "recorded_at": utcnow(), "note": (note or None) and note[:400]})


def append(account_id: int, *, db: Database | None = None, **fields: Any) -> int | None:
    with (db or get_database()).write() as u:
        return append_in(u.session, account_id, **fields)


def record_fill_in(session: Any, fill: Any, *, desk_id: int | None, source: str, source_ref: str) -> int | None:
    """The fill of any broker as its two entries inside one transaction: the cash of the trade (qty * price * multiplier, a buy is negative)
    and its commission. `fill` is a `broker.types.Fill` with its `contract_id` and `multiplier`; a fill without them cannot be booked."""
    if not getattr(fill, "contract_id", None) or getattr(fill, "multiplier", None) in (None, 0):
        raise provenance.MissingProvenance("a fill needs its contract id and its multiplier from the contract sheet")
    side = str(fill.side).lower()
    buy = side.startswith("buy")
    if not buy and not side.startswith("sell"):
        raise provenance.MissingProvenance(f"side {fill.side!r} is neither a buy nor a sell")
    qty, price, mult = dec(fill.qty, "qty"), dec(fill.price, "price"), dec(fill.multiplier, "multiplier")
    cash = qty * price * mult
    first = append_in(session, fill.account_id, kind="fill", amount=-cash if buy else cash, source=source, source_ref=source_ref, occurred_at=fill.at,
                      desk_id=desk_id, contract_id=fill.contract_id, qty=qty if buy else -qty, price=price, multiplier=mult)
    commission = dec(fill.commission or 0, "commission")
    if commission != 0:
        append_in(session, fill.account_id, kind="commission", amount=-commission, source=source, source_ref=source_ref, occurred_at=fill.at,
                  desk_id=desk_id, contract_id=fill.contract_id)
    return first


def record_fill(fill: Any, *, desk_id: int | None, source: str, source_ref: str, db: Database | None = None) -> int | None:
    with (db or get_database()).write() as u:
        return record_fill_in(u.session, fill, desk_id=desk_id, source=source, source_ref=source_ref)


def entries_in(session: Any, account_id: int, *, as_of: Any = None, kinds: Iterable[str] | None = None, desk_id: int | None = None,
               since_reset: bool = True) -> list[BookEntry]:
    """The entries of an account read through an open session. By default only those after the last reset of a simulated account: what
    came before a reset is history, not position (`since_reset=False` reads everything, for audits)."""
    sql, params = "SELECT * FROM book_entries WHERE account_id = :a", {"a": account_id}
    if since_reset:
        marker = session.one("SELECT id FROM book_entries WHERE account_id = :a AND kind = 'adjustment' AND note = 'reset' ORDER BY id DESC LIMIT 1",
                             {"a": account_id})
        if marker is not None:
            sql, params["m"] = sql + " AND id > :m", marker["id"]
    if as_of is not None:
        sql, params["t"] = sql + " AND occurred_at <= :t", _stamp(as_of)
    if desk_id is not None:
        sql, params["d"] = sql + " AND desk_id = :d", desk_id
    out = [BookEntry.from_row(r) for r in session.query(sql + " ORDER BY occurred_at, id", params)]
    return [e for e in out if kinds is None or e.kind in set(kinds)]


def entries(account_id: int, *, db: Database | None = None, as_of: Any = None, kinds: Iterable[str] | None = None,
            desk_id: int | None = None, since_reset: bool = True) -> list[BookEntry]:
    with (db or get_database()).read() as u:
        return entries_in(u.session, account_id, as_of=as_of, kinds=kinds, desk_id=desk_id, since_reset=since_reset)


def cash_in(session: Any, account_id: int) -> Decimal | None:
    """The cash of a simulated account: the last `open` or `reset` plus the entries after it. None when the account has no ledger."""
    last = session.one("SELECT amount, created_at FROM account_ledger WHERE account_id = :a ORDER BY id DESC LIMIT 1", {"a": account_id})
    if last is None:
        return None
    marker = session.one("SELECT id FROM book_entries WHERE account_id = :a AND kind = 'adjustment' AND note = 'reset' ORDER BY id DESC LIMIT 1",
                         {"a": account_id})
    sql = "SELECT amount FROM book_entries WHERE account_id = :a AND "
    rows = (session.query(sql + "id > :m", {"a": account_id, "m": marker["id"]}) if marker is not None
            else session.query(sql + "recorded_at >= :t", {"a": account_id, "t": last["created_at"]}))
    return Decimal(str(last["amount"])) + sum((Decimal(str(r["amount"])) for r in rows), ZERO)


def cash(account_id: int, *, db: Database | None = None) -> Decimal | None:
    with (db or get_database()).read() as u:
        return cash_in(u.session, account_id)


class Book:
    """The book of one account at a moment. Positions, summary, performance and P&L equal the hand calculation of the tests."""

    def __init__(self, items: Iterable[BookEntry]) -> None:
        self.entries = list(items)
        fills = [{"contract_id": e.contract_id, "qty": e.qty, "price": e.price, "multiplier": e.multiplier, "commission": ZERO}
                 for e in self.entries if e.kind == "fill" and e.contract_id and e.qty is not None and e.price is not None and e.multiplier]
        commissions: dict[str, Decimal] = {}
        for e in self.entries:
            if e.kind == "commission" and e.contract_id:
                commissions[e.contract_id] = commissions.get(e.contract_id, ZERO) + (-e.amount)
        self._cost = reconstruct(fills)
        for cid, paid in commissions.items():
            if cid in self._cost:
                c = self._cost[cid]
                self._cost[cid] = CostPosition(c.contract_id, c.qty, c.avg_price, c.multiplier, c.realized, paid)

    @classmethod
    def load(cls, account_id: int, *, db: Database | None = None, as_of: Any = None, desk_id: int | None = None, session: Any = None) -> "Book":
        if session is not None:
            return cls(entries_in(session, account_id, as_of=as_of, desk_id=desk_id))
        return cls(entries(account_id, db=db, as_of=as_of, desk_id=desk_id))

    def cost_positions(self, *, include_flat: bool = False) -> dict[str, CostPosition]:
        return {k: v for k, v in self._cost.items() if include_flat or v.qty != 0}

    def realized(self) -> Decimal:
        return sum((p.realized for p in self._cost.values()), ZERO)

    def commissions(self) -> Decimal:
        return sum((p.commissions for p in self._cost.values()), ZERO)

    def other_cash(self, kinds: Iterable[str]) -> Decimal:
        wanted = set(kinds)
        return sum((e.amount for e in self.entries if e.kind in wanted), ZERO)

    def summary(self) -> dict[str, Any]:
        """Positions, realized profit, commissions and net profit as text."""
        pos = self.cost_positions()
        return {"positions": {k: {"qty": text(v.qty), "avg_price": text(v.avg_price), "multiplier": text(v.multiplier)} for k, v in pos.items()},
                "realized": text(self.realized()), "commissions": text(self.commissions()), "net_realized": text(self.realized() - self.commissions())}


def load_csv(content: str) -> list[dict[str, str]]:
    """Rows of a CSV text with a header (used by importers and by the tests of the fixture)."""
    return list(csv.DictReader(io.StringIO(content)))


__all__ = ["Book", "BookEntry", "MoneyError", "append", "cash", "entries", "load_csv", "record_fill"]

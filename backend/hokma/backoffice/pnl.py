"""Profit and loss by desk, account and symbol (SPEC-018: FT-106). Realized profit between two moments from the book, with the commissions
subtracted; the result of a period is the realized profit at its end minus the realized profit at its start."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from hokma.backoffice import book as book_module
from hokma.backoffice.book import Book
from hokma.db import Database, get_database
from hokma.exec.money import ZERO, text


@dataclass(frozen=True)
class PnLReport:
    desk_id: int | None
    account_id: int | None
    start: str
    end: str
    realized: Decimal
    commissions: Decimal
    other: Decimal                                  # dividends, interest and accruals of the period
    net: Decimal
    by_symbol: dict[str, dict[str, Decimal]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"desk_id": self.desk_id, "account_id": self.account_id, "start": self.start, "end": self.end, "realized": text(self.realized),
                "commissions": text(self.commissions), "other": text(self.other), "net": text(self.net),
                "by_symbol": {k: {kk: text(vv) for kk, vv in v.items()} for k, v in self.by_symbol.items()}}


def _period(entries: list[book_module.BookEntry], start: str, end: str) -> tuple[Book, Book]:
    return Book([e for e in entries if e.occurred_at <= end]), Book([e for e in entries if e.occurred_at < start])


def for_account(account_id: int, *, start: Any, end: Any, desk_id: int | None = None, db: Database | None = None) -> PnLReport:
    s, e = book_module._stamp(start), book_module._stamp(end)
    entries = book_module.entries(account_id, db=db, desk_id=desk_id)
    upto, before = _period(entries, s, e)
    rows: dict[str, dict[str, Decimal]] = {}
    a, b = upto.cost_positions(include_flat=True), before.cost_positions(include_flat=True)
    for cid, pos in a.items():
        prev = b.get(cid)
        realized = pos.realized - (prev.realized if prev else ZERO)
        paid = pos.commissions - (prev.commissions if prev else ZERO)
        rows[cid] = {"realized": realized, "commissions": paid, "net": realized - paid}
    in_period = [x for x in entries if s <= x.occurred_at <= e and x.kind in ("dividend", "interest", "accrual")]
    other = sum((x.amount for x in in_period), ZERO)
    realized = sum((r["realized"] for r in rows.values()), ZERO)
    commissions = sum((r["commissions"] for r in rows.values()), ZERO)
    return PnLReport(desk_id, account_id, s, e, realized, commissions, other, realized - commissions + other, rows)


def pnl(desk_id: int, *, start: Any, end: Any, db: Database | None = None) -> PnLReport:
    """The profit and loss of a desk between two moments: the entries of its account that carry its desk id."""
    database = db or get_database()
    with database.read() as u:
        account = u.broker.account_of_desk(desk_id)
    if account is None:
        return PnLReport(desk_id, None, book_module._stamp(start), book_module._stamp(end), ZERO, ZERO, ZERO, ZERO)
    return for_account(account.id, start=start, end=end, desk_id=desk_id, db=database)

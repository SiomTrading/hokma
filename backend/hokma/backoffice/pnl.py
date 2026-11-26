"""Profit and loss by desk, account and symbol (SPEC-018: FT-106). Realized profit between two moments from the book, with the commissions
subtracted; the result of a period is the realized profit at its end minus the realized profit at its start."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from hokma.backoffice import desk_owner
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
    strategy_key: str | None = None                 # set when the report is of one strategy of the desk
    unrealized: Decimal | None = None               # the open lots of this desk at the marks given; None while a lot has no mark
    by_account: dict[int, dict[str, Decimal]] = field(default_factory=dict)     # a desk with several accounts: its result in each one

    def to_dict(self) -> dict[str, Any]:
        return {"desk_id": self.desk_id, "account_id": self.account_id, "strategy_key": self.strategy_key, "start": self.start, "end": self.end,
                "realized": text(self.realized), "commissions": text(self.commissions), "other": text(self.other), "net": text(self.net),
                "unrealized": None if self.unrealized is None else text(self.unrealized),
                "by_account": {str(k): {kk: text(vv) for kk, vv in v.items()} for k, v in self.by_account.items()},
                "by_symbol": {k: {kk: text(vv) for kk, vv in v.items()} for k, v in self.by_symbol.items()}}


def _period(entries: list[book_module.BookEntry], start: str, end: str) -> tuple[Book, Book]:
    return Book([e for e in entries if e.occurred_at <= end]), Book([e for e in entries if e.occurred_at < start])


def _unrealized(entries: list[book_module.BookEntry], marks: Mapping[str, Any]) -> Decimal | None:
    """The open lots of these entries valued at `marks` (contract id -> price): (mark - average cost) * quantity * multiplier. None when an open lot has no mark."""
    total = ZERO
    for cid, pos in Book(entries).cost_positions().items():
        if pos.qty == 0:
            continue
        mark = marks.get(cid)
        if mark is None:
            return None
        total += (Decimal(str(mark)) - pos.avg_price) * pos.qty * pos.multiplier
    return total


def for_account(account_id: int, *, start: Any, end: Any, desk_id: int | None = None, strategy_key: str | None = None, marks: Mapping[str, Any] | None = None,
                db: Database | None = None) -> PnLReport:
    """The result of an account between two moments. With `desk_id` (and `strategy_key`) only the lots of that desk (strategy): two desks that
    bought the same symbol in the account each see their own lots, and entries with no desk belong to the desk only when it is the account's only one."""
    s, e = book_module._stamp(start), book_module._stamp(end)
    database = db or get_database()
    entries = book_module.entries(account_id, db=database)
    if desk_id is not None:
        with database.read() as u:
            entries = desk_owner.of_desk(u.session, account_id, entries, desk_id, strategy_key)
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
    open_value = _unrealized([x for x in entries if x.occurred_at <= e], marks) if marks is not None else None
    return PnLReport(desk_id, account_id, s, e, realized, commissions, other, realized - commissions + other, rows, strategy_key, open_value)


def pnl(desk_id: int, *, start: Any, end: Any, account_id: int | None = None, strategy_key: str | None = None,
        marks: Mapping[str, Any] | Callable[[list[str]], Mapping[str, Any]] | None = None, db: Database | None = None) -> PnLReport:
    """The result a desk generated between two moments, in each of its accounts (SPEC-027 section 6): only the entries of its own orders, or of
    nobody when the account is the desk's alone. `account_id` narrows to one account of the desk; `strategy_key` to one strategy. The day figure
    of the broker covers the whole account and is not used here."""
    database = db or get_database()
    with database.read() as u:
        linked = [a.id for a in u.broker.accounts_of_desk(desk_id)]
    if account_id is not None:
        if account_id not in linked:
            raise ValueError(f"account {account_id} is not in desk {desk_id}")
        linked = [account_id]
    s, e = book_module._stamp(start), book_module._stamp(end)
    if not linked:
        return PnLReport(desk_id, None, s, e, ZERO, ZERO, ZERO, ZERO, strategy_key=strategy_key)
    reports: list[PnLReport] = []
    for aid in linked:
        given = marks
        if callable(marks):
            given = dict(marks([c for c in Book(book_module.entries(aid, db=database)).cost_positions()]))
        reports.append(for_account(aid, start=start, end=end, desk_id=desk_id, strategy_key=strategy_key, marks=given, db=database))
    if len(reports) == 1:
        only = reports[0]
        return replace(only, by_account={only.account_id: {"realized": only.realized, "commissions": only.commissions, "other": only.other, "net": only.net}})
    by_symbol: dict[str, dict[str, Decimal]] = {}
    for rep in reports:
        for cid, vals in rep.by_symbol.items():
            have = by_symbol.setdefault(cid, {"realized": ZERO, "commissions": ZERO, "net": ZERO})
            for k, v in vals.items():
                have[k] += v
    unrealized = None if any(x.unrealized is None for x in reports) else sum((x.unrealized for x in reports), ZERO)    # type: ignore[misc]
    return PnLReport(desk_id, None, s, e, sum((x.realized for x in reports), ZERO), sum((x.commissions for x in reports), ZERO),
                     sum((x.other for x in reports), ZERO), sum((x.net for x in reports), ZERO), by_symbol, strategy_key, unrealized,
                     {x.account_id: {"realized": x.realized, "commissions": x.commissions, "other": x.other, "net": x.net} for x in reports})  # type: ignore[misc]

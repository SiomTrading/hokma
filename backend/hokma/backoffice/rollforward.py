"""Roll-forward of the capital of an account or a desk (SPEC-018: AFS-016): opening balance plus deposits plus realized result plus dividends and
interest plus accruals plus adjustments minus commissions and fees minus withdrawals equals the closing balance. Each line points to the book
entries that make it. The balance is on a realized basis: cash plus the cost of what is still open, so an unrealized move is not part of it.

The closing balance is computed twice: as the sum of the lines, and independently as the opening balance plus the change of cash plus the change
of the cost of open positions (or the balance a caller reports, for example the broker's). If the two differ the difference is shown as an
`unexplained` line. It is never absorbed into another line.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from hokma.backoffice import book as book_module
from hokma.backoffice.book import Book, BookEntry
from hokma.db import Database, get_database
from hokma.exec.money import ZERO, dec, dec_or_none, text

MOVEMENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("deposits", ("deposit",)),
    ("dividends_and_interest", ("dividend", "interest")),
    ("accruals", ("accrual",)),
    ("adjustments", ("adjustment",)),
    ("commissions_and_fees", ("commission", "fee")),
    ("withdrawals", ("withdrawal",)),
)


@dataclass(frozen=True)
class RollLine:
    name: str
    amount: Decimal
    entry_ids: tuple[int, ...]


@dataclass(frozen=True)
class RollForward:
    account_id: int
    desk_id: int | None
    start: str
    end: str
    opening: Decimal
    lines: tuple[RollLine, ...]
    closing_from_lines: Decimal
    closing_actual: Decimal
    unexplained: Decimal

    @property
    def balanced(self) -> bool:
        return self.unexplained == 0

    def line(self, name: str) -> RollLine:
        return next(item for item in self.lines if item.name == name)

    def to_dict(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "desk_id": self.desk_id, "start": self.start, "end": self.end, "opening": text(self.opening),
                "lines": [{"name": x.name, "amount": text(x.amount), "entries": list(x.entry_ids)} for x in self.lines],
                "closing_from_lines": text(self.closing_from_lines), "closing_actual": text(self.closing_actual), "unexplained": text(self.unexplained),
                "balanced": self.balanced}


def open_cost(items: Iterable[BookEntry]) -> Decimal:
    """Cost of what is still open (signed: a short is negative): quantity times average price times multiplier."""
    return sum((p.qty * p.avg_price * p.multiplier for p in Book(list(items)).cost_positions().values()), ZERO)


def capital_at(entries: Sequence[BookEntry], *, base: Decimal, before: str) -> Decimal:
    """The balance before `before` (a stamp): the base plus the cash of every earlier entry plus the cost of what was open then."""
    earlier = [e for e in entries if e.occurred_at < before]
    return base + sum((e.amount for e in earlier), ZERO) + open_cost(earlier)


def roll_forward(entries: Sequence[BookEntry], *, opening: Decimal, start: Any, end: Any, account_id: int = 0, desk_id: int | None = None,
                 reported_closing: Decimal | None = None) -> RollForward:
    s, e = book_module._stamp(start), book_module._stamp(end)
    if e < s:
        raise ValueError("the end of the period is before its start")
    opening = dec(opening, "opening")
    upto = [x for x in entries if x.occurred_at <= e]
    before = [x for x in entries if x.occurred_at < s]
    inside = [x for x in upto if x.occurred_at >= s]
    prior, now = Book(before), Book(upto)
    realized_by_contract = {}
    for cid, pos in now.cost_positions(include_flat=True).items():
        prev = prior.cost_positions(include_flat=True).get(cid)
        realized_by_contract[cid] = pos.realized - (prev.realized if prev else ZERO)
    realized = sum(realized_by_contract.values(), ZERO)
    fill_ids = tuple(x.id for x in inside if x.kind == "fill")
    lines = [RollLine("realized_result", realized, fill_ids)]
    for name, kinds in MOVEMENTS:
        picked = [x for x in inside if x.kind in kinds]
        lines.append(RollLine(name, sum((x.amount for x in picked), ZERO), tuple(x.id for x in picked)))
    # order of the table: opening, deposits, realized, dividends and interest, accruals, adjustments, commissions and fees, withdrawals
    order = ["deposits", "realized_result", "dividends_and_interest", "accruals", "adjustments", "commissions_and_fees", "withdrawals"]
    lines.sort(key=lambda x: order.index(x.name))
    from_lines = opening + sum((x.amount for x in lines), ZERO)
    if reported_closing is not None:
        actual = dec(reported_closing, "reported_closing")
    else:
        actual = opening + sum((x.amount for x in inside), ZERO) + open_cost(upto) - open_cost(before)
    gap = actual - from_lines
    shown = [*lines, RollLine("unexplained", gap, ())] if gap != 0 else lines
    return RollForward(account_id, desk_id, s, e, opening, tuple(shown), from_lines, actual, gap)


def for_account(account_id: int, *, start: Any, end: Any, desk_id: int | None = None, opening: Decimal | None = None,
                reported_closing: Decimal | None = None, db: Database | None = None) -> RollForward:
    """The roll-forward of an account (or of one desk of it) from the book. The opening balance is the one the caller gives or, when it is not
    given, the base of the account (its last open or reset in the ledger, or the capital of the desk) plus what the book holds before the start."""
    database = db or get_database()
    items = book_module.entries(account_id, db=database, desk_id=desk_id)
    if opening is None:
        with database.read() as u:
            if desk_id is None:
                last = u.broker.last_entry(account_id)
                base = dec(last.amount) if last is not None else ZERO
            else:
                row = u.session.one("SELECT capital FROM desks WHERE id = :d", {"d": desk_id})
                base = dec(row["capital"]) if row is not None else ZERO
        opening = capital_at(items, base=base, before=book_module._stamp(start))
    return roll_forward(items, opening=opening, start=start, end=end, account_id=account_id, desk_id=desk_id,
                        reported_closing=dec_or_none(reported_closing))


__all__ = ["RollForward", "RollLine", "capital_at", "for_account", "open_cost", "roll_forward"]

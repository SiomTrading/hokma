"""Daily settlement of futures to market (SPEC-020: L-18). Every close, an open future pays or receives the change of its settlement price
times its quantity times its multiplier: a long of 1 NQ that settles 10 points higher moves the cash by 10 * 20 = 200.

The book of the account (book.py) carries a future like any other position: the fill moves the full notional and the position is valued at
the market. Both views give the same equity, and this module proves it: `settle_account` reads the book, settles every close and checks that the
net value of the daily settlement equals the equity of the book at the last price. A position with no settlement price is an error
(`valuation.Unpriced`), never a move of zero. The module only reads: nothing is written to the book.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from hokma.backoffice import book as book_module
from hokma.backoffice import valuation
from hokma.backoffice.book import Book
from hokma.backoffice.cost_basis import CostPosition
from hokma.backoffice.reports.account_report import asset_class
from hokma.db import Database, get_database
from hokma.exec.money import ZERO


class SettlementError(ValueError):
    """The account cannot be settled. `code` is stable: no_ledger, no_closes, mismatch."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Settlement:
    day: date
    prices: Mapping[str, Decimal]       # the settlement price of every open future
    moves: Mapping[str, Decimal]        # cash paid (positive) or received (negative) per contract that day
    cash_move: Decimal
    cash_usd: Decimal                   # cash after the day's settlement
    nav_usd: Decimal                    # net asset value of the futures account: cash, since a settled future carries no value


def variation(qty: Decimal, previous: Decimal, settle: Decimal, multiplier: Decimal) -> Decimal:
    """Cash moved by a position in one settlement: (settle - previous) * signed qty * multiplier."""
    return (settle - previous) * qty * multiplier


def futures_positions(positions: Iterable[CostPosition]) -> tuple[CostPosition, ...]:
    """The open positions that are futures (a canonical id of the form FUT:...). Anything else is settled by its own rules."""
    return tuple(p for p in positions if p.qty != 0 and asset_class(p.contract_id) == "future")


def settle_days(positions: Iterable[CostPosition], closes: Sequence[tuple[date, Mapping[str, Decimal]]], *, cash_usd: Decimal) -> tuple[Settlement, ...]:
    """Settle the open futures at every close, in date order. The first move of a position is against its average entry price, the next ones against
    the previous settlement. `cash_usd` is the cash of the futures account before the first close (the notional of the entries added back)."""
    held = list(positions)
    if not closes:
        raise SettlementError("no_closes", "give at least one settlement close")
    days = [d for d, _ in closes]
    if days != sorted(days) or len(set(days)) != len(days):
        raise SettlementError("no_closes", "closes must be in increasing date order, one per day")
    previous = {p.contract_id: p.avg_price for p in held}
    cash = cash_usd
    out = []
    for day, prices in closes:
        moves: dict[str, Decimal] = {}
        for p in held:
            if p.contract_id not in prices:
                raise valuation.Unpriced(p.contract_id)
            settle = prices[p.contract_id]
            moves[p.contract_id] = variation(p.qty, previous[p.contract_id], settle, p.multiplier)
            previous[p.contract_id] = settle
        moved = sum(moves.values(), ZERO)
        cash += moved
        out.append(Settlement(day, {p.contract_id: prices[p.contract_id] for p in held}, moves, moved, cash, cash))
    return tuple(out)


def settle_account(account_id: int, closes: Sequence[tuple[date, Mapping[str, Decimal]]], *, other_prices: Mapping[str, Decimal] | None = None,
                   db: Database | None = None) -> tuple[Settlement, ...]:
    """Settle the open futures of an account and prove that the book balances.

    Both reads happen in one transaction. The cash is the one of the book (ledger plus entries); a future's notional is added back because
    daily settlement does not keep it. `other_prices` values the positions that are not futures, which stay as the book carries them. The result
    is checked: the last NAV plus the value of the other positions must equal the book's own equity (cash plus positions at the last price).
    """
    if not closes:
        raise SettlementError("no_closes", "give at least one settlement close")
    with (db or get_database()).read() as u:
        cash = book_module.cash_in(u.session, account_id)
        book = Book(book_module.entries_in(u.session, account_id))
    if cash is None:
        raise SettlementError("no_ledger", "the account has no ledger: its cash is not known")
    open_all = book.cost_positions()
    futures = futures_positions(open_all.values())
    others = [p for p in open_all.values() if p not in futures]
    base = cash + sum((p.qty * p.avg_price * p.multiplier for p in futures), ZERO)
    settled = settle_days(futures, closes, cash_usd=base)
    last = closes[-1][1]
    marks = {**(other_prices or {}), **{p.contract_id: last[p.contract_id] for p in futures}}
    valued, errors = valuation.value_all(open_all.values(), marks)
    if errors:
        raise valuation.Unpriced(sorted(errors)[0])
    other_value = valuation.total(v for v in valued if v.contract_id not in {p.contract_id for p in futures})
    equity = cash + valuation.total(valued)
    if settled[-1].nav_usd + other_value != equity:
        raise SettlementError("mismatch", f"the settlement ends at {settled[-1].nav_usd + other_value} and the book at {equity}")
    return settled

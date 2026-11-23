"""The snapshot of an account: cash, positions, value and the profit and loss of the day (SPEC-018: OA-50).

A figure that is not known is `None` with its reason in `missing`; the snapshot never makes up a zero. The guards of the
filter read it and decide on what is there. A position with no price makes the value of the positions `None` (`unpriced` in
`missing`), never a number that leaves it out."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from hokma.backoffice import book as book_module
from hokma.backoffice import valuation
from hokma.backoffice.book import Book
from hokma.backoffice.cost_basis import reconstruct
from hokma.db import Database, get_database
from hokma.exec.money import ZERO, dec_or_none


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: int
    as_of: str
    cash: Decimal | None
    positions_value: Decimal | None
    equity: Decimal | None
    day_pnl: Decimal | None
    peak_equity: Decimal | None = None          # the highest equity the book has seen (the drawdown is measured from it)
    realized_pnl: Decimal | None = None
    total_profit: Decimal | None = None
    best_day_pnl: Decimal | None = None
    open_contracts: Decimal | None = None
    positions: tuple[dict[str, Any], ...] = ()
    missing: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.missing


def _prices_from(positions: Iterable[Any] | None) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for p in positions or ():
        cid, qty, mv = getattr(p, "contract_id", None), dec_or_none(getattr(p, "qty", None)), dec_or_none(getattr(p, "market_value", None))
        mult = dec_or_none(getattr(p, "multiplier", None)) or Decimal(1)
        if cid and qty and mv is not None:
            out[cid] = abs(mv) / (abs(qty) * mult)
    return out


def _fills(entries: list[book_module.BookEntry]) -> list[dict]:
    return [{"contract_id": e.contract_id, "qty": e.qty, "price": e.price, "multiplier": e.multiplier, "commission": ZERO}
            for e in entries if e.kind == "fill" and e.contract_id and e.qty is not None and e.price is not None and e.multiplier]


def _realized(entries: list[book_module.BookEntry]) -> Decimal:
    return sum((p.realized for p in reconstruct(_fills(entries)).values()), ZERO)


def _net(entries: list[book_module.BookEntry]) -> Decimal:
    """Realized profit minus commissions plus dividends and interest of these entries."""
    extra = sum((e.amount for e in entries if e.kind in ("commission", "fee", "dividend", "interest", "accrual")), ZERO)
    return _realized(entries) + extra


def snapshot(account_id: int, *, db: Database | None = None, prices: Mapping[str, Decimal] | None = None, positions: Iterable[Any] | None = None,
             balance: Any = None, now: datetime | None = None) -> AccountSnapshot:
    """The snapshot of an account. `balance` is the broker's own (a real account); a simulated one has its cash in the book."""
    database = db or get_database()
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    entries = book_module.entries(account_id, db=database)
    book = Book(entries)
    missing: dict[str, str] = {}

    cash: Decimal | None
    if balance is not None and getattr(balance, "cash", None) is not None:
        cash = dec_or_none(balance.cash)
    else:
        cash = book_module.cash(account_id, db=database)
        if cash is None:
            missing["cash"] = "the account has no ledger and no broker balance"

    priced = {**_prices_from(positions), **{k: v for k, v in (prices or {}).items()}}
    cost = book.cost_positions()
    valued, errors = valuation.value_all(cost.values(), priced)
    for symbol in errors:
        missing[f"unpriced:{symbol}"] = "no market price"
    positions_value = None if errors else valuation.total(valued)
    equity: Decimal | None = None
    if cash is not None and positions_value is not None:
        equity = cash + positions_value
    elif balance is not None and getattr(balance, "equity", None) is not None:
        equity = dec_or_none(balance.equity)
    if equity is None and "cash" not in missing and errors:
        missing["equity"] = "a position has no price"

    today = stamp[:10]
    before_today = [e for e in entries if e.occurred_at[:10] < today]
    day_pnl = _net(entries) - _net(before_today) if entries else ZERO
    if positions is not None and any(dec_or_none(getattr(p, "day_pl", None)) is not None for p in positions):
        day_pnl = sum((dec_or_none(getattr(p, "day_pl", None)) or ZERO for p in positions), ZERO) + (day_pnl if entries else ZERO)
    else:
        missing.setdefault("day_unrealized", "the move of open positions today is not known: the day result counts realized profit only") if cost else None

    by_day: dict[str, list[book_module.BookEntry]] = {}
    for e in entries:
        by_day.setdefault(e.occurred_at[:10], []).append(e)
    best, running, prefix = None, ZERO, []
    for day in sorted(by_day):
        prefix_before = _net(prefix)
        prefix = prefix + by_day[day]
        gain = _net(prefix) - prefix_before
        best = gain if best is None or gain > best else best
    peak: Decimal | None = None
    base = None
    with database.read() as u:
        last = u.broker.last_entry(account_id)
    if last is not None:
        base = Decimal(str(last.amount))
    if base is not None:
        flow = ZERO
        curve = [base]
        seen: list[book_module.BookEntry] = []
        for n, e in enumerate(entries):
            seen.append(e)
            if e.kind in ("deposit", "withdrawal"):
                flow += e.amount
            if n + 1 == len(entries) or entries[n + 1].source_ref != e.source_ref:      # a fill and its commission are one moment: the curve is read after both
                curve.append(base + flow + _net(seen))
        peak = max(curve + ([equity] if equity is not None else []))
    return AccountSnapshot(
        account_id, stamp, cash, positions_value, equity, day_pnl, peak, _realized(entries) if entries else ZERO, _net(entries) if entries else ZERO,
        best, sum((abs(p.qty) for p in cost.values()), ZERO),
        tuple({"contract_id": v.contract_id, "qty": str(v.qty), "price": str(v.price), "market_value": str(v.market_value)} for v in valued), missing)

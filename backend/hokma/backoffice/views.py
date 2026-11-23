"""The views of a book (SPEC-018: FT-105): positions, portfolio, performance and transactions. Every view is computed from `Book` and
adds up to it to the cent. A figure that is not known is `None` with its reason, never a zero: a position with no price has no market
value, and a portfolio with such a position has no total."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from hokma.backoffice import valuation
from hokma.backoffice.book import Book
from hokma.exec.money import ZERO, text

Prices = Mapping[str, Decimal]


def positions_overview(book: Book, prices: Prices) -> list[dict[str, Any]]:
    """One row per open position: quantity, average price, multiplier, cost, mark, market value and unrealized profit. With no mark the row says
    `unpriced` and holds no value."""
    rows: list[dict[str, Any]] = []
    for cid, pos in sorted(book.cost_positions().items()):
        cost = abs(pos.qty) * pos.avg_price * pos.multiplier
        row: dict[str, Any] = {"contract_id": cid, "qty": pos.qty, "avg_price": pos.avg_price, "multiplier": pos.multiplier, "cost": cost,
                               "price": None, "market_value": None, "unrealized": None, "error": None}
        try:
            valued = valuation.value(pos, prices.get(cid))
        except valuation.Unpriced as exc:
            row["error"] = exc.code
        else:
            row.update(price=valued.price, market_value=valued.market_value, unrealized=valued.unrealized)
        rows.append(row)
    return rows


def portfolio_overview(book: Book, prices: Prices, *, cash: Decimal | None) -> dict[str, Any]:
    """Cash, value of the positions, equity, realized and unrealized profit, commissions. Anything that depends on a missing figure is None and
    named in `missing`."""
    rows = positions_overview(book, prices)
    missing: dict[str, str] = {}
    unpriced = [r["contract_id"] for r in rows if r["error"]]
    value = None if unpriced else sum((r["market_value"] for r in rows), ZERO)
    unrealized = None if unpriced else sum((r["unrealized"] for r in rows), ZERO)
    for cid in unpriced:
        missing[f"unpriced:{cid}"] = "no market price"
    if cash is None:
        missing["cash"] = "no cash figure was given"
    equity = cash + value if cash is not None and value is not None else None
    realized, paid = book.realized(), book.commissions()
    other = book.other_cash(("dividend", "interest", "accrual", "fee"))
    total = realized - paid + other + unrealized if unrealized is not None else None
    return {"cash": cash, "positions_value": value, "equity": equity, "realized": realized, "commissions": paid, "other_income": other,
            "net_realized": realized - paid + other, "unrealized": unrealized, "total_profit": total, "open_positions": len(rows), "missing": missing}


def portfolio_performance(book: Book, prices: Prices, *, start_capital: Decimal | None, cash: Decimal | None) -> dict[str, Any]:
    """The total profit and its return on the starting capital (a fraction: 0.01 is one per cent). None when either is not known."""
    overview = portfolio_overview(book, prices, cash=cash)
    total = overview["total_profit"]
    if start_capital is None or start_capital <= 0:
        return {"total_profit": total, "return": None, "reason": "the starting capital is not known"}
    if total is None:
        return {"total_profit": None, "return": None, "reason": "a position has no price: " + ", ".join(sorted(overview["missing"]))}
    return {"total_profit": total, "return": total / start_capital, "reason": None}


def transactions_overview(book: Book) -> list[dict[str, Any]]:
    """Every line of the book in order, as it was written."""
    return [{"id": e.id, "occurred_at": e.occurred_at, "kind": e.kind, "contract_id": e.contract_id, "qty": e.qty, "price": e.price,
             "multiplier": e.multiplier, "amount": e.amount, "source": e.source, "source_ref": e.source_ref} for e in book.entries]


def transactions_performance(book: Book) -> dict[str, dict[str, Decimal | int]]:
    """By contract: trades, realized profit, commissions and net. The sums equal `Book.realized()` and `Book.commissions()`."""
    out: dict[str, dict[str, Decimal | int]] = {}
    for cid, pos in sorted(book.cost_positions(include_flat=True).items()):
        trades = sum(1 for e in book.entries if e.kind == "fill" and e.contract_id == cid)
        out[cid] = {"trades": trades, "realized": pos.realized, "commissions": pos.commissions, "net": pos.realized - pos.commissions}
    return out


def to_text(view: Any) -> Any:
    """A view as plain data for a tool answer: every Decimal as its stable text."""
    if isinstance(view, Decimal):
        return text(view)
    if isinstance(view, Mapping):
        return {k: to_text(v) for k, v in view.items()}
    if isinstance(view, (list, tuple)):
        return [to_text(v) for v in view]
    return view

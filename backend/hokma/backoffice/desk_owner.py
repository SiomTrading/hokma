"""Which desk and which strategy a book entry belongs to when an account is shared (SPEC-027, piece 027d).

A fill that Hokma sent carries the desk and the strategy of its order (`broker_orders.desk_id` and `strategy_key`, written by 027b). An entry whose
order is unknown (an import, a fill made outside, a row written before 0024 with no desk) belongs to a desk only when the account has exactly one
desk linked: with two or more it stays "without desk". Nothing here writes."""
from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterable, Iterator
from typing import Any

from hokma.backoffice.book import BookEntry

PRINCIPAL = "principal"                                  # the strategy of an order that named none (SPEC-027 3.2)
Owner = tuple[int | None, str | None]


_placing: contextvars.ContextVar[tuple[int, str] | None] = contextvars.ContextVar("hokma_placing_owner", default=None)


@contextlib.contextmanager
def placing(desk_id: int | None, strategy_key: str | None) -> Iterator[None]:
    """Say, around the send of an order, which desk and strategy it belongs to. A simulated broker fills a market order inside the send, before
    `broker_orders` has a row with the broker id, so it reads the owner from here and keeps it with the order. The book is append-only: an entry is
    written with its desk once, never corrected afterwards."""
    token = _placing.set(None if desk_id is None else (desk_id, strategy_key or PRINCIPAL))
    try:
        yield
    finally:
        _placing.reset(token)


def current_owner() -> tuple[int, str] | None:
    """The desk and strategy named by the send in progress, if any."""
    return _placing.get()


def sole_desk(session: Any, account_id: int) -> int | None:
    """The desk of the account when exactly one desk holds it; None when it has none or several."""
    rows = session.query("SELECT desk_id FROM desk_account_links WHERE account_id = :a ORDER BY desk_id LIMIT 2", {"a": account_id})
    return rows[0]["desk_id"] if len(rows) == 1 else None


def order_owner(session: Any, account_id: int, order_ref: str | None) -> Owner:
    """The (desk, strategy) of the order the broker knows by `order_ref`, from the record Hokma kept when it sent it; (None, None) when the
    record is not there or has no desk."""
    if not order_ref:
        return None, None
    row = session.one("SELECT desk_id, strategy_key FROM broker_orders WHERE account_id = :a AND broker_order_id = :o AND desk_id IS NOT NULL "
                      "ORDER BY id LIMIT 1", {"a": account_id, "o": str(order_ref)})
    return (None, None) if row is None else (row["desk_id"], row["strategy_key"] or PRINCIPAL)


def owner_of_fill(session: Any, account_id: int, order_ref: str | None) -> Owner:
    """What a fill of an order is booked with: the desk and strategy of the order; with no record, the only desk of the account (strategy
    `principal`), or no desk when the account is shared."""
    desk_id, strategy = order_owner(session, account_id, order_ref)
    if desk_id is not None:
        return desk_id, strategy
    sole = sole_desk(session, account_id)
    return sole, (PRINCIPAL if sole is not None else None)


def owners(session: Any, account_id: int, entries: Iterable[BookEntry]) -> dict[int, Owner]:
    """entry id -> (desk, strategy) for entries already booked. The desk written on the entry wins; an entry with none takes the desk of its
    order (the reference of an entry holds the order id) and then the only desk of the account."""
    rows = session.query("SELECT broker_order_id, desk_id, strategy_key FROM broker_orders WHERE account_id = :a AND broker_order_id IS NOT NULL "
                         "AND desk_id IS NOT NULL ORDER BY id", {"a": account_id})
    known: dict[str, Owner] = {}
    for row in rows:
        known.setdefault(str(row["broker_order_id"]), (row["desk_id"], row["strategy_key"] or PRINCIPAL))
    sole = sole_desk(session, account_id)
    out: dict[int, Owner] = {}
    for e in entries:
        found = next((known[t] for t in e.source_ref.split(":") if t in known), None)
        desk = e.desk_id if e.desk_id is not None else (found[0] if found else sole)
        strategy = found[1] if (found and found[0] == desk) else (PRINCIPAL if desk is not None else None)
        out[e.id] = (desk, strategy)
    return out


def of_desk(session: Any, account_id: int, entries: Iterable[BookEntry], desk_id: int, strategy_key: str | None = None) -> list[BookEntry]:
    """The entries of an account that belong to a desk (and to one of its strategies when `strategy_key` is given)."""
    items = list(entries)
    mine = owners(session, account_id, items)
    return [e for e in items if mine[e.id][0] == desk_id and (strategy_key is None or mine[e.id][1] == strategy_key)]

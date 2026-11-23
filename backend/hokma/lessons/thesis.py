r"""Life cycle of a thesis (SPEC-017: CTS-023).

A thesis is the idea behind a position on one symbol of a desk. It moves only forward, and each move is a dated row in
`thesis_events`, so the history of why a position was held can be read back:

    open  ->  confirmed  ->  closed
      \          \
       +----------+--->  invalidated  ->  closed

`open` is an idea with its entry still ahead; `confirmed` is a thesis the market has proved right enough to hold, with the entry
real; `invalidated` is a thesis that broke (the position may still be open); `closed` ends it. After `closed` the same symbol can
start a new thesis with `open`. A move that is not on the arrows raises InvalidTransition and writes nothing. Nothing is deleted.
"""
from __future__ import annotations

from dataclasses import dataclass

from hokma.db import Database, get_database
from hokma.db.timeutil import to_db_time, utcnow

STATES = ("open", "confirmed", "invalidated", "closed")
TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"open"}),                                   # no thesis yet
    "open": frozenset({"confirmed", "invalidated", "closed"}),
    "confirmed": frozenset({"invalidated", "closed"}),
    "invalidated": frozenset({"closed"}),
    "closed": frozenset({"open"}),                               # a new thesis on the same symbol
}
MAX_SYMBOL = 40
MAX_NOTE = 4000


class ThesisError(Exception):
    """Base of the errors of this module."""


class InvalidTransition(ThesisError):
    """The move is not allowed from the current state."""


@dataclass(frozen=True)
class ThesisEvent:
    id: int
    desk_id: int
    symbol: str
    state: str
    note: str | None
    created_at: str


def clean_symbol(symbol: str) -> str:
    """The symbol in upper case. 1 to 40 printable characters with no spaces (futures such as `/NQZ26` are allowed)."""
    text = (symbol or "").strip().upper()
    if not 1 <= len(text) <= MAX_SYMBOL or not text.isprintable() or any(c.isspace() for c in text):
        raise ThesisError(f"a symbol has 1 to {MAX_SYMBOL} printable characters and no spaces: {symbol!r}")
    return text


def _event(row: dict) -> ThesisEvent:
    return ThesisEvent(int(row["id"]), int(row["desk_id"]), row["symbol"], row["state"], row.get("note"), row["created_at"])


def current_state(desk_id: int, symbol: str, *, db: Database | None = None) -> str | None:
    """The state of the latest event of the symbol, or None when it never had a thesis."""
    with (db or get_database()).read() as u:
        row = u.session.one("SELECT state FROM thesis_events WHERE desk_id = :d AND symbol = :s ORDER BY id DESC LIMIT 1",
                            {"d": desk_id, "s": clean_symbol(symbol)})
    return None if row is None else str(row["state"])


def record(desk_id: int, symbol: str, state: str, note: str | None = None, *, now: str | None = None,
           db: Database | None = None) -> ThesisEvent:
    """Move the thesis of `symbol` to `state` and date the move. Raises InvalidTransition when it is not allowed."""
    if state not in STATES:
        raise ThesisError(f"unknown state {state!r}: {', '.join(STATES)}")
    symbol = clean_symbol(symbol)
    if note is not None and len(note) > MAX_NOTE:
        raise ThesisError(f"a note has at most {MAX_NOTE} characters")
    with (db or get_database()).write() as u:
        s = u.session
        if s.one("SELECT 1 AS x FROM desks WHERE id = :i", {"i": desk_id}) is None:
            raise ThesisError(f"desk {desk_id} does not exist")
        last = s.one("SELECT state FROM thesis_events WHERE desk_id = :d AND symbol = :s ORDER BY id DESC LIMIT 1",
                     {"d": desk_id, "s": symbol})
        before = None if last is None else str(last["state"])
        if state not in TRANSITIONS[before]:
            raise InvalidTransition(f"{symbol}: cannot go from {before or 'no thesis'} to {state}")
        new_id = s.insert("thesis_events", {"desk_id": desk_id, "symbol": symbol, "state": state, "note": note,
                                            "created_at": to_db_time(now) if now else utcnow()})
        return _event(s.one("SELECT * FROM thesis_events WHERE id = :i", {"i": new_id}))


def history(desk_id: int, symbol: str, *, db: Database | None = None) -> list[ThesisEvent]:
    """Every move of the symbol on the desk, oldest first."""
    with (db or get_database()).read() as u:
        rows = u.session.query("SELECT * FROM thesis_events WHERE desk_id = :d AND symbol = :s ORDER BY id",
                               {"d": desk_id, "s": clean_symbol(symbol)})
    return [_event(r) for r in rows]


def open_theses(desk_id: int, *, db: Database | None = None) -> dict[str, str]:
    """symbol -> state of every thesis of the desk that is not closed."""
    with (db or get_database()).read() as u:
        rows = u.session.query("SELECT symbol, state, id FROM thesis_events WHERE desk_id = :d ORDER BY id", {"d": desk_id})
    latest: dict[str, str] = {}
    for row in rows:
        latest[row["symbol"]] = row["state"]
    return {sym: state for sym, state in latest.items() if state != "closed"}

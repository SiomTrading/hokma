"""The profit and loss of an account as its BROKER reports it (SPEC-026 v2, piece 3390e1, sections 4.1 and 4.5).

Hokma does not work out how much a position or an account has made: the broker owns the position and its result. This module reads what the
broker says, keeps the last good reading of each account and tells the others (the guard that blocks an opening, the freeze, the screen) what
it is:

    latest_pnl(account_id) -> BrokerPnl | None     the last good reading, or None when there is no usable one (never read, or the broker
                                                   gave nothing for 3 reads in a row: "no data from the broker")

A reading is good when the broker gave the equity now and the equity of the start of the day (or the owner declared the start). The day P&L
is `equity_now - equity_start`, a subtraction of two fields of the broker. It is never built from the book nor from prices of Hokma.

Sources (one reader each, same contract):
    schwab   GET /trader/v1/accounts?fields=positions, ONE call for all the accounts, every 5 seconds, and one more read when a message of
             ACCT_ACTIVITY that is not an ERROR arrives (at most one extra read per second). Fields: section 4.1.1 of the spec.
    alpaca   GET /v2/account and GET /v2/positions, two calls per account every 5 seconds (SPEC-026 v2, 3390e1b). Hokma has no stream of
             trade_updates, so there is no extra read on a fill: the 5 seconds are the only trigger. It has its own budget of calls per minute,
             far under the limit of the account, and a 429 puts it to rest for a while (it never touches the order path).
    sim      the simulated broker of Hokma IS the broker: its equity, minus the equity it had when the session of the account began.
    others   no reader (IBKR is the next piece, NinjaTrader is task 3391): the account shows "no data from the broker" and a loss rule cannot be a restriction there.

Polling here is of ACCOUNT data only, never of market data. Blocking: the readers run in a thread."""
from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from hokma.db import Database, get_database
from hokma.i18n import t
from hokma.kit.read_budget import ReadBudget, ReadYielded

log = logging.getLogger("hokma.broker.pnl")

READ_SECONDS = 5.0              # one read of the accounts every 5 seconds (12 calls a minute for all the accounts of Schwab)
STALE_AFTER = 3                 # reads in a row without a good answer: from then on there is "no data from the broker"
MAX_AGE = READ_SECONDS * (STALE_AFTER + 1)      # a reading older than this is not data either (the reader stopped)
YIELD_ALERT = 60.0              # seconds of reads that keep giving way to the orders, after their pause ended, before the owner is told
EXTRA_GAP = 1.0                 # seconds between the reads that a message of the account stream asks for
READERS = ("schwab", "alpaca", "sim")       # the kinds of broker that have a reader of P&L
ALPACA_READS_PER_MIN = 20       # the cap of the reads of the P&L of Alpaca (2 calls each, 24 a minute at 5 s: one read every 5 s is 12): the rest of the quota stays for the orders
ALPACA_REST = 30.0              # seconds with no read after a 429 from Alpaca


# ------------------------------------------------------------------------------------------------------------------ the contract


@dataclass(frozen=True, slots=True)
class PositionPnl:
    """One position as the broker gives it. A field the broker does not give is None, never 0."""
    symbol: str
    qty: float | None
    day_pl: float | None
    open_pl: float | None


@dataclass(frozen=True, slots=True)
class BrokerPnl:
    """What one reading of the broker says about one account (spec 4.1). `day_pnl` is None when `equity_now` or `equity_start` is."""
    account_id: int
    equity_now: float | None
    equity_start: float | None
    day_pnl: float | None
    positions: tuple[PositionPnl, ...]
    as_of: str                              # ISO 8601 UTC, set by Hokma when the answer arrived
    source: str                             # "schwab" | "sim" | "ninjatrader"

    @property
    def usable(self) -> bool:
        return self.equity_now is not None and self.day_pnl is not None

    def as_dict(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "equity_now": self.equity_now, "equity_start": self.equity_start, "day_pnl": self.day_pnl,
                "positions": [{"symbol": p.symbol, "qty": p.qty, "day_pl": p.day_pl, "open_pl": p.open_pl} for p in self.positions],
                "as_of": self.as_of, "source": self.source}


def iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _day(now: float | None, start: float | None) -> float | None:
    return None if now is None or start is None else round(now - start, 4)


def _text_num(value: Any) -> float | None:
    """A number that a broker sends as text ("103820.56", the way Alpaca does) or as a number. Empty, absent, not a number, NaN or infinite: None, never 0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(str(value).strip()) if not isinstance(value, (int, float)) else float(value)
    except ValueError:
        return None
    return out if math.isfinite(out) else None


def has_reader(kind: str) -> bool:
    return kind in READERS


def require_reader(u, account_ids) -> None:
    """A loss rule in restriction needs the broker's P&L (SPEC-026 A12). An account whose broker has no reader refuses it with a clear sentence."""
    from hokma.broker.service import BrokerError

    kinds = {b.id: b.kind for b in u.broker.list_brokers()}
    for account_id in account_ids:
        kind = kinds.get(u.broker.get_account(account_id).broker_id, "unknown")
        if not has_reader(kind):
            raise BrokerError("live_freeze_unavailable", 409, text=t("pnl.err.unavailable", account=str(account_id), kind=kind))


# ------------------------------------------------------------------------------------------------------------------ Schwab mapping


def schwab_positions(securities: Mapping[str, Any]) -> tuple[PositionPnl, ...]:
    """The positions of a public `securitiesAccount`: `currentDayProfitLoss` is the day P&L, `longOpenProfitLoss + shortOpenProfitLoss` the open one."""
    out = []
    for item in securities.get("positions") or []:
        if not isinstance(item, dict):
            continue
        instrument = item.get("instrument") if isinstance(item.get("instrument"), dict) else {}
        long_qty, short_qty = _num(item.get("longQuantity")), _num(item.get("shortQuantity"))
        qty = None if long_qty is None and short_qty is None else (long_qty or 0.0) - (short_qty or 0.0)
        opens = [v for v in (_num(item.get("longOpenProfitLoss")), _num(item.get("shortOpenProfitLoss"))) if v is not None]
        out.append(PositionPnl(str(instrument.get("symbol") or ""), qty, _num(item.get("currentDayProfitLoss")), sum(opens) if opens else None))
    return tuple(out)


def schwab_pnl(account_id: int, securities: Mapping[str, Any], as_of: str, declared: Callable[[], float | None] | None = None) -> BrokerPnl:
    """A public `securitiesAccount` of `GET /accounts?fields=positions` as a BrokerPnl (spec 4.1.1).
    Margin: now `currentBalances.equity`, start `initialBalances.equity`. Cash: now `totalCash + longNonMarginableMarketValue`, start
    `initialBalances.liquidationValue`. A missing start is the one the owner declared, when there is one."""
    current = securities.get("currentBalances") if isinstance(securities.get("currentBalances"), dict) else {}
    initial = securities.get("initialBalances") if isinstance(securities.get("initialBalances"), dict) else {}
    is_cash = securities.get("type") == "CASH" or (securities.get("type") is None and "totalCash" in current)
    if is_cash:
        total, held = _num(current.get("totalCash")), _num(current.get("longNonMarginableMarketValue"))
        # the value of what is held is a field of the broker too: absent with positions open, the equity is unknown (None, never 0)
        has_positions = any(isinstance(i, dict) for i in securities.get("positions") or [])
        now = None if total is None or (held is None and has_positions) else total + (held or 0.0)
        start = _num(initial.get("liquidationValue"))
    else:
        now, start = _num(current.get("equity")), _num(initial.get("equity"))
    if start is None and declared is not None:
        start = declared()
    return BrokerPnl(account_id, now, start, _day(now, start), schwab_positions(securities), as_of, "schwab")


def schwab_accounts_of(db: Database) -> dict[str, int]:
    """{fingerprint: local account id} of the active accounts of Schwab."""
    out: dict[str, int] = {}
    with db.read() as u:
        for row in u.broker.list_brokers():
            if row.kind != "schwab":
                continue
            for account in u.broker.list_accounts(row.id):
                link = u.broker.external_of(account.id) if account.status == "active" else None
                if link is not None:
                    out[link.fingerprint] = account.id
    return out


def read_schwab(db: Database, declared: Callable[[int], float | None] | None = None) -> dict[int, BrokerPnl]:
    """ONE call to Schwab for every account. Returns {local id: reading} for the accounts that came in the answer. A failure of the call raises."""
    import hmac

    from hokma.broker import schwab as schwab_broker
    from hokma.schwab import accounts, endpoints, http

    mine = schwab_accounts_of(db)
    if not mine:
        return {}
    key = schwab_broker.load_key()
    if key is None:
        raise RuntimeError("the fingerprint key of Schwab is not available")
    by_ref: dict[str, int] = {}
    with http.background():                      # a read of an account: it gives way to every order and never pauses one (kit/read_budget.py)
        listed = endpoints.accounts(None, fields="positions")
        entries = accounts.listing()
    for entry in entries:
        fingerprint = schwab_broker.fingerprint(key, accounts.hash_for(entry["account_ref"]))
        for known, local in mine.items():
            if hmac.compare_digest(fingerprint, known):
                by_ref[entry["account_ref"]] = local
    as_of = iso_now()
    out: dict[int, BrokerPnl] = {}
    for item in listed:
        if item is None:
            continue
        data = schwab_broker._securities(accounts.public(item))
        local = by_ref.get(data.get("account_ref"))
        if local is not None:
            out[local] = schwab_pnl(local, data, as_of, (lambda a=local: declared(a)) if declared else None)
    return out


# ------------------------------------------------------------------------------------------------------------------ simulated broker


def sim_accounts_of(db: Database) -> list[int]:
    with db.read() as u:
        return [a.id for row in u.broker.list_brokers() if row.kind == "sim" for a in u.broker.list_accounts(row.id) if a.status == "active"]


_sim_cache: dict[tuple[int, int], tuple[tuple, tuple[frozenset, float, dict[str, float]]]] = {}    # (db, account) -> (what it was built from, what it gave)


def _sim_book(db: Database, u, account_id: int, started: datetime, key: str) -> tuple[frozenset, float, dict[str, float]]:
    """From the ledger of an account: what was held when the session began, what the book moved since, and per contract. Rebuilt only when the last entry
    of the ledger or the session changed; otherwise the answer of the last read is used (a read every 5 seconds must not walk the whole ledger)."""
    from hokma.backoffice import book as book_module
    from hokma.backoffice.book import Book

    last = u.session.one("SELECT COALESCE(MAX(id), 0) AS m, COUNT(*) AS n FROM book_entries WHERE account_id = :a", {"a": account_id})
    stamp = (key, started, int(last["m"]), int(last["n"]))
    cached = _sim_cache.get((id(db), account_id))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    entries = book_module.entries_in(u.session, account_id)
    before = [e for e in entries if e.occurred_at < started]
    after = [e for e in entries if e.occurred_at >= started]
    moved: dict[str, float] = {}
    for e in after:
        if e.contract_id:
            moved[e.contract_id] = moved.get(e.contract_id, 0.0) + float(e.amount)
    built = (frozenset(Book(before).cost_positions()), float(sum((e.amount for e in after), 0)), moved)
    _sim_cache[(id(db), account_id)] = (stamp, built)
    return built


def read_sim(db: Database, ids: list[int], declared: Callable[[int], float | None] | None = None, now: datetime | None = None) -> dict[int, BrokerPnl]:
    """The simulated broker is the broker of its accounts: equity now is its own `Balance.equity`; the equity of the start is the one it had when the
    session of the account began (cash now minus what the book moved since, when the account was flat at the open), else the one it kept, else the
    one the owner declared. The day P&L of a position is what it made since the open of the session when it was not held then; carried over, it is None."""
    from hokma.broker.sim import SimBroker
    from hokma.exec import live
    from hokma.exec import session as sessions

    now = now or datetime.now(UTC)
    out: dict[int, BrokerPnl] = {}
    for account_id in ids:
        try:
            with db.read() as u:
                row = next(r for r in u.broker.list_brokers() if r.kind == "sim" and any(a.id == account_id for a in u.broker.list_accounts(r.id)))
                broker = SimBroker(u.broker, row)
                balance, positions = broker.balance(account_id), broker.positions(account_id)
                calendar = live.calendar_of(u, account_id)
                sess = sessions.session_at(calendar, now)
                held_before, after_total, moved = _sim_book(db, u, account_id, sess.started, sess.key)
                stored = u.session.one("SELECT session_key, equity_start FROM account_live_session WHERE account_id = :a", {"a": account_id})
            equity_now = balance.equity
            start: float | None = None
            if not held_before and balance.cash is not None:
                start = balance.cash - after_total
            elif stored is not None and stored["session_key"] == sess.key and stored["equity_start"] not in (None, ""):
                start = float(stored["equity_start"])
            elif declared is not None:
                start = declared(account_id)
            seen = []
            for p in positions:
                day_pl = None
                if p.contract_id not in held_before and p.market_value is not None:
                    day_pl = round(moved.get(p.contract_id, 0.0) + p.market_value, 4)
                seen.append(PositionPnl(p.symbol, p.qty, day_pl, p.open_pl))
            out[account_id] = BrokerPnl(account_id, equity_now, None if start is None else round(start, 4), _day(equity_now, start), tuple(seen), iso_now(), "sim")
        except Exception:                                              # noqa: BLE001  (one account that cannot be read is a failed read of that account only)
            log.exception("the simulated account %s could not be read", account_id)
    return out


def declared_start(db: Database, account_id: int, now: datetime | None = None) -> float | None:
    """The equity the owner declared for the session the account is in, or None (the answer of the owner when the broker does not give the start)."""
    from hokma.exec import live
    from hokma.exec import session as sessions

    with db.read() as u:
        calendar = live.calendar_of(u, account_id)
    key = sessions.session_at(calendar, now or datetime.now(UTC)).key
    value = live.declared_start(db, account_id, key)
    return None if value is None else float(value)


# ------------------------------------------------------------------------------------------------------------------ Alpaca mapping


def alpaca_positions(raw_positions: Any) -> tuple[PositionPnl, ...]:
    """The positions of `GET /v2/positions`: `unrealized_intraday_pl` is the day P&L, `unrealized_pl` the open one. A short has a negative quantity."""
    out = []
    for item in raw_positions or []:
        qty = _text_num(getattr(item, "qty", None))
        if qty is not None and getattr(getattr(item, "side", None), "value", getattr(item, "side", None)) == "short":
            qty = -abs(qty)
        out.append(PositionPnl(str(getattr(item, "symbol", "") or ""), qty, _text_num(getattr(item, "unrealized_intraday_pl", None)),
                               _text_num(getattr(item, "unrealized_pl", None))))
    return tuple(out)


def alpaca_pnl(account_id: int, raw_account: Any, raw_positions: Any, as_of: str, declared: Callable[[], float | None] | None = None) -> BrokerPnl:
    """`GET /v2/account` and `GET /v2/positions` of Alpaca as a BrokerPnl (piece 3390e1b). Now is `equity`; the start is `last_equity`, the equity of the previous
    trading day at 16:00 ET (official documentation: https://docs.alpaca.markets/reference/getaccount-1). A `last_equity` that is absent or not above zero (an
    account that has not closed a day yet) is not a start: the one the owner declared is used when there is one, else the day P&L is None."""
    now = _text_num(getattr(raw_account, "equity", None))
    start = _text_num(getattr(raw_account, "last_equity", None))
    if start is not None and start <= 0:
        start = None
    if start is None and declared is not None:
        start = declared()
    return BrokerPnl(account_id, now, start, _day(now, start), alpaca_positions(raw_positions), as_of, "alpaca")


_budgets: dict[int, ReadBudget] = {}


def alpaca_budget(row_id: int) -> ReadBudget:
    """The budget of the reads of one Alpaca row: the generic one of `hokma.kit.read_budget`. Alpaca has no door shared with the orders in this process, so
    nothing is reserved here (`reserve=0`): the reads have their own cap per minute and their own pause after a 429, and an order never asks either."""
    return _budgets.setdefault(row_id, ReadBudget(time.monotonic, per_minute=ALPACA_READS_PER_MIN, reserve=0))


def alpaca_links_of(db: Database) -> dict[int, tuple[Any, dict[str, int]]]:
    """{broker row id: (the row, {fingerprint: local account id})} of the active accounts of Alpaca that are linked to their account."""
    out: dict[int, tuple[Any, dict[str, int]]] = {}
    with db.read() as u:
        for row in u.broker.list_brokers():
            if row.kind != "alpaca":
                continue
            mine: dict[str, int] = {}
            for account in u.broker.list_accounts(row.id):
                link = u.broker.external_of(account.id) if account.status == "active" else None
                if link is not None:
                    mine[link.fingerprint] = account.id
            if mine:
                out[row.id] = (row, mine)
    return out


def alpaca_accounts_of(db: Database) -> list[int]:
    return sorted(local for _row, mine in alpaca_links_of(db).values() for local in mine.values())


def read_alpaca(db: Database, declared: Callable[[int], float | None] | None = None, skipped: set[int] | None = None,
                budgets: Callable[[int], ReadBudget] | None = None) -> dict[int, BrokerPnl]:
    """Two calls to Alpaca per broker row (account and positions), inside the budget of the P&L. Returns {local id: reading} for the accounts that were read.
    A row whose call failed or whose account is not the one linked is simply not in the answer (a failed read). A row with no budget left, or resting after
    a 429, is NOT read and is NOT a failure: its accounts go into `skipped` (the reader does not count them toward "no data"; the reading just ages, and
    an old one is not data either). The budget is the reader's own: a 429 here pauses only the reads (`ReadBudget.pause`) and never touches the orders."""
    import hmac

    from hokma.broker import alpaca as alpaca_broker
    from hokma.broker import external

    groups = alpaca_links_of(db)
    if not groups:
        return {}
    key = external.load_key(alpaca_broker.FINGERPRINT_ENTRY)
    if key is None:
        raise RuntimeError("the fingerprint key of Alpaca is not available")
    out: dict[int, BrokerPnl] = {}
    for row_id, (row, mine) in groups.items():
        budget = (budgets or alpaca_budget)(row_id)
        if not budget.admit(0, 1, False):
            log.info("the P&L of Alpaca waits: no calls left in this minute, or resting after a 429")
            if skipped is not None:
                skipped.update(mine.values())
            continue
        try:
            raw_account, raw_positions = alpaca_broker.read_pnl_raw(row)
        except Exception as error:                                         # noqa: BLE001  (429, 401, 5xx, a network error: the read failed)
            if getattr(error, "status_code", None) == 429:
                budget.pause(ALPACA_REST)
                if skipped is not None:
                    skipped.update(mine.values())                              # a 429 is Alpaca asking us to wait, not a failure of the broker
            log.info("the Alpaca account could not be read: %s", type(error).__name__)
            continue
        number = getattr(raw_account, "account_number", None)
        local = next((a for fp, a in mine.items() if number and hmac.compare_digest(external.fingerprint(key, str(number)), fp)), None)
        if local is not None:
            out[local] = alpaca_pnl(local, raw_account, raw_positions, iso_now(), (lambda a=local: declared(a)) if declared else None)
    return out


# ------------------------------------------------------------------------------------------------------------------ what the reads said


@dataclass
class _Entry:
    good: BrokerPnl | None = None
    good_at: float = 0.0
    fails: int = 0
    first_fail: str | None = None


class PnlBoard:
    """The last good reading of each account and how many reads in a row failed since. One lock; no I/O."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[int, _Entry] = {}

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def good(self, reading: BrokerPnl) -> bool:
        """A good read. True when the account had no data and has it again (the owner is told once)."""
        with self._lock:
            e = self._items.setdefault(reading.account_id, _Entry())
            came_back = e.fails >= STALE_AFTER
            e.good, e.good_at, e.fails, e.first_fail = reading, self._clock(), 0, None
            return came_back

    def bad(self, account_id: int, at: str) -> bool:
        """A failed read. True on the one that makes it STALE_AFTER in a row (the owner is told once)."""
        with self._lock:
            e = self._items.setdefault(account_id, _Entry())
            e.fails += 1
            if e.fails == 1:
                e.first_fail = at
            return e.fails == STALE_AFTER

    def _stale(self, e: _Entry) -> bool:
        return e.good is None or e.fails >= STALE_AFTER or self._clock() - e.good_at > MAX_AGE

    def latest(self, account_id: int) -> BrokerPnl | None:
        with self._lock:
            e = self._items.get(account_id)
            return None if e is None or self._stale(e) else e.good

    def view(self, account_id: int, kind: str | None = None) -> dict[str, Any]:
        """What the screen shows. state: ok | no_data (read before, now nothing for 3 reads) | pending (no read yet) | unavailable (no reader)."""
        if kind is not None and not has_reader(kind):
            return {"account_id": account_id, "state": "unavailable", "source": kind, "since": None, "last": None}
        with self._lock:
            e = self._items.get(account_id)
            if e is None or (e.good is None and e.fails == 0):
                return {"account_id": account_id, "state": "pending", "source": kind, "since": None, "last": None}
            stale = self._stale(e)
            since = (e.good.as_of if e.good is not None else e.first_fail) if stale else None
            return {"account_id": account_id, "state": "no_data" if stale else "ok", "source": e.good.source if e.good else kind,
                    "since": since, "last": e.good.as_dict() if e.good is not None else None}

    def since(self, account_id: int) -> str | None:
        with self._lock:
            e = self._items.get(account_id)
            return None if e is None else (e.good.as_of if e.good is not None else e.first_fail)


_board = PnlBoard()


def board() -> PnlBoard:
    return _board


def latest_pnl(account_id: int) -> BrokerPnl | None:
    """The last good reading of the broker for the account, or None when there is no usable one: never read, or no data from the broker for
    3 reads in a row (15 seconds), or the reader stopped. The guard of the openings and the freeze read this and nothing else."""
    return _board.latest(account_id)


def views(db: Database | None = None) -> dict[int, dict[str, Any]]:
    """The view of every active account, for the screen: {account id: view}."""
    database = db or get_database()
    with database.read() as u:
        pairs = [(a.id, b.kind) for b in u.broker.list_brokers() for a in u.broker.list_accounts(b.id) if a.status == "active"]
    return {account_id: _board.view(account_id, kind) for account_id, kind in pairs}


# ------------------------------------------------------------------------------------------------------------------ the reader


def _hms(stamp: str | None) -> str:
    """HH:MM:SS (UTC) of an ISO stamp, for the notices."""
    return (stamp or "")[11:19] or "-"


def _tell(db: Database, text: str) -> None:
    from hokma.exec import telegram_approval

    try:
        with db.write() as u:
            telegram_approval.notice(u, text)
    except Exception:                                                      # noqa: BLE001  (the notice is queued or lost, it never blocks a read)
        log.warning("the notice about the data of the broker could not be queued")


def reads_paused(kind: str) -> bool:
    """True while the reads of that kind of broker rest after a 429: a pause is normal and ends alone, so it does not count as "giving way"."""
    if kind == "schwab":
        from hokma.schwab import http

        return http.reads_paused()
    if kind == "alpaca":
        return any(b.paused() for b in _budgets.values())
    return False


def _evaluate(account_id: int) -> None:
    """Tell the live freeze that a new reading of the account is on the board. A failure of the freeze never breaks the read."""
    try:
        from hokma.exec import live

        live.on_reading(account_id)
    except Exception:                                                      # noqa: BLE001  (the monitor tick measures it on its next pass)
        log.exception("the freeze could not evaluate the new reading")


class PnlReader:
    """The reads of the brokers: `read_all` every 5 seconds, `request_extra` when the account stream of Schwab says something happened."""

    def __init__(self, db: Database | None = None, board: PnlBoard | None = None, *, clock: Callable[[], float] = time.monotonic,
                 notify: Callable[[str], None] | None = None, schwab: Callable[..., dict[int, BrokerPnl]] | None = None,
                 sim: Callable[..., dict[int, BrokerPnl]] | None = None, alpaca: Callable[..., dict[int, BrokerPnl]] | None = None, spawn: Callable[[Callable[[], None]], None] | None = None,
                 later: Callable[[float, Callable[[], None]], None] | None = None) -> None:
        self._db = db
        self.board = board or _board
        self._clock = clock
        self._notify = notify
        self._schwab = schwab or read_schwab
        self._sim = sim or read_sim
        self._alpaca = alpaca or read_alpaca
        self._spawn = spawn or self._thread
        self._later = later or self._timer
        self._lock = threading.Lock()
        self._reading = {kind: threading.Lock() for kind in READERS}
        self._started: dict[str, float] = {}
        self._waiting: set[str] = set()
        self.reads: dict[str, int] = {kind: 0 for kind in READERS}
        self._yield_since: dict[str, float] = {}                              # kind -> when its reads started to give way with no pause left
        self._yield_told: set[str] = set()
        self.stream_on = False

    @property
    def database(self) -> Database:
        return self._db or get_database()

    @staticmethod
    def _thread(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, daemon=True, name="pnl-extra-read").start()

    @staticmethod
    def _timer(delay: float, fn: Callable[[], None]) -> None:
        timer = threading.Timer(delay, fn)
        timer.daemon = True
        timer.start()

    def tell(self, text: str) -> None:
        if self._notify is not None:
            self._notify(text)
        else:
            _tell(self.database, text)

    # ---- one read ----

    def read_all(self) -> None:
        for kind in READERS:
            self.read_kind(kind)

    def read_kind(self, kind: str, periodic: bool = True) -> None:
        """One read of every account of a kind. A failed call, an account missing from the answer, or an answer with no equity is a failed read of it,
        but only the PERIODIC read (every 5 seconds) counts failures toward "no data": an extra read (asked by the account stream) can give a good
        reading, never a bad one. A read that yielded to the orders (`ReadYielded`, no call made) is not a failure either, in any read."""
        database = self.database
        ids = (sorted(schwab_accounts_of(database).values()) if kind == "schwab" else alpaca_accounts_of(database) if kind == "alpaca"
               else sim_accounts_of(database))
        if not ids:
            return
        with self._reading[kind]:
            with self._lock:
                self._started[kind] = self._clock()
            declared = lambda account_id: declared_start(database, account_id)           # noqa: E731
            skipped: set[int] = set()                                      # accounts not read on purpose (budget, 429): not a failed read
            try:
                found = (self._schwab(database, declared) if kind == "schwab" else self._alpaca(database, declared, skipped) if kind == "alpaca"
                         else self._sim(database, ids, declared))
            except ReadYielded:
                log.debug("the %s read gave way to the orders", kind)
                self._yielding(kind, ids, True)
                return
            except Exception as error:                                     # noqa: BLE001  (429, 401, 5xx, a network error: the read failed)
                log.info("the %s accounts could not be read: %s", kind, type(error).__name__)
                found = {}
            self.reads[kind] += 1
            self._yielding(kind, ids, bool(ids) and set(ids) <= skipped)          # every account skipped on purpose (Alpaca's budget or 429) is the same as giving way
            at = iso_now()
            for account_id in ids:
                reading = found.get(account_id)
                if reading is not None and reading.usable:
                    if self.board.good(reading):
                        self.tell(t("pnl.back", account=self._name(account_id)))
                    _evaluate(account_id)                                  # the freeze measures this account in the same event as the reading (SPEC-026 v2, 4.2)
                elif account_id in skipped:
                    continue
                elif periodic and self.board.bad(account_id, at):
                    self.tell(t("pnl.lost", account=self._name(account_id), since=_hms(self.board.since(account_id))))

    def _yielding(self, kind: str, ids: list[int], yielding: bool) -> None:
        """Reads that keep giving way to the orders for more than YIELD_ALERT seconds AFTER their pause ended tell the owner once (the data is going stale and
        nothing else says so: a read that yields is not a failed read). A read that goes through, or a pause, starts the count again."""
        now = self._clock()
        if not yielding:
            self._yield_since.pop(kind, None)
            self._yield_told.discard(kind)
            return
        if reads_paused(kind):
            self._yield_since.pop(kind, None)
            return
        since = self._yield_since.setdefault(kind, now)
        if now - since > YIELD_ALERT and kind not in self._yield_told:
            self._yield_told.add(kind)
            self.tell(t("pnl.yielding", account=", ".join(self._name(a) for a in ids), seconds=int(YIELD_ALERT)))

    def _name(self, account_id: int) -> str:
        try:
            with self.database.read() as u:
                return u.broker.get_account(account_id).name
        except Exception:                                                  # noqa: BLE001
            return str(account_id)

    # ---- the account stream says something happened ----

    def request_extra(self, kind: str = "schwab") -> str:
        """Read `kind` now, at most once per EXTRA_GAP. A message that arrives inside the gap does not read twice: it asks for ONE read for when the gap
        ends, and the messages after it ride on that one. Returns "now", "later" or "queued"."""
        with self._lock:
            now = self._clock()
            if kind in self._waiting:
                return "queued"
            last = self._started.get(kind)
            if last is not None and now - last < EXTRA_GAP:
                self._waiting.add(kind)
                delay = EXTRA_GAP - (now - last)
                run = None
            else:
                self._started[kind] = now
                delay = 0.0
                run = lambda: self.read_kind(kind, periodic=False)         # noqa: E731
        if run is None:
            def fire() -> None:
                with self._lock:
                    self._waiting.discard(kind)
                self.read_kind(kind, periodic=False)

            self._later(delay, fire)
            return "later"
        self._spawn(run)
        return "now"


_reader: PnlReader | None = None


def reader() -> PnlReader | None:
    return _reader


def start(db: Database | None = None) -> PnlReader:
    global _reader
    _reader = PnlReader(db)
    return _reader


def stop() -> None:
    global _reader
    _reader = None
    _stream_wanted.clear()


def on_account_activity(kind: str, message_type: str) -> None:
    """The account stream of a broker said something happened. An ERROR message is not a signal. A process with no reader ignores it."""
    if _reader is not None and kind in READERS and message_type.upper() != "ERROR":
        _reader.request_extra(kind)


def on_fill() -> None:
    """A fill was booked: the simulated accounts are read again at once (the simulator is the broker and fills in this process)."""
    if _reader is not None:
        _reader.request_extra("sim")


async def keep_reading(app: Any, interval: float = READ_SECONDS) -> None:
    """The task of the app: it reads the accounts of the brokers every few seconds, and follows the account stream of Schwab while there are accounts."""
    mine = start(app.state.db)
    try:
        while True:
            try:
                await asyncio.to_thread(mine.read_all)
                await _follow_stream(mine)
            except Exception:                                              # noqa: BLE001  (the next pass reads again)
                log.exception("a pass of the readers of the brokers failed")
            await asyncio.sleep(interval)
    finally:
        stop()


_stream_wanted: set[str] = set()


def wants_activity(kind: str) -> bool:
    """True while an account of that kind exists and the reader runs: this module needs its account stream (the signal for the extra read).
    This module is the only owner of the account stream (the old keeper of the freeze subscriptions was retired in 3390e3): `_follow_stream` asks for it while a Schwab account exists."""
    return kind in _stream_wanted


async def _follow_stream(mine: PnlReader) -> None:
    """ACCT_ACTIVITY is followed while there is an account of Schwab: it is the signal for the extra read. It is asked for only when the state
    changes or when the last ask found no stream (session closed): never on and off every pass."""
    has = bool(await asyncio.to_thread(schwab_accounts_of, mine.database))
    if not has:
        _stream_wanted.discard("schwab")
        return
    if "schwab" in _stream_wanted and mine.stream_on:
        return
    _stream_wanted.add("schwab")
    from hokma.connections import runtime

    try:
        mine.stream_on = bool(await runtime.get_hub().watch_activity("schwab", True))
    except Exception:                                                      # noqa: BLE001  (asked again on the next pass)
        mine.stream_on = False
        log.exception("the account stream could not be followed")

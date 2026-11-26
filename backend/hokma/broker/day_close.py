"""The close of the day of an account (SPEC-026, piece 3390a). When a loss rule in restriction is reached, the account is closed for the day:
no strategy opens in it (the account guard answers `day_closed`) until its session restarts or the owner lifts it by hand.

This piece keeps the state and the record. WHO closes it, on every event of price, is the monitor of the live measure (piece 3390c):
it calls `close_day`. The owner lifts it with `lift`, which asks him to confirm on purpose and writes who, when and the loss of that moment."""
from __future__ import annotations

from typing import Any

from hokma.broker import rule_modes, service
from hokma.broker.service import BrokerError, Who
from hokma.db import Database
from hokma.db.timeutil import utcnow


def _account(u, account_id: int) -> None:
    if u.session.one("SELECT id FROM broker_accounts WHERE id = :a", {"a": account_id}) is None:
        raise BrokerError("account_not_found", 404)


def state_of(u, account_id: int) -> dict[str, Any]:
    """The state of the day of an account as the owner sees it. An account with no row is open."""
    row = u.session.one("SELECT state, closed_at, rule_id, loss, on_trip FROM account_day_state WHERE account_id = :a", {"a": account_id})
    if row is None or row["state"] != "day_closed":
        return {"account_id": account_id, "state": "open"}
    return {"account_id": account_id, "state": "day_closed", "closed_at": str(row["closed_at"]), "rule_id": row["rule_id"],
            "loss": row["loss"], "on_trip": row["on_trip"]}


def is_closed(u, account_id: int) -> bool:
    return state_of(u, account_id)["state"] == "day_closed"


def close_day(account_id: int, rule_id: str, loss: Any, on_trip: str | None = None, db: Database | None = None) -> dict[str, Any]:
    """Mark the account closed for the day. Called by the monitor, never by an agent. Idempotent: a second call keeps the first record."""
    if on_trip is not None and on_trip not in rule_modes.ON_TRIPS:
        raise BrokerError("bad_limits", 400, text="bad on_trip")
    with service._db(db).write() as u:
        _account(u, account_id)
        if is_closed(u, account_id):
            return state_of(u, account_id)
        now = utcnow()
        params = {"a": account_id, "t": now, "r": rule_id[:60], "l": None if loss is None else str(loss), "o": on_trip}
        if u.session.one("SELECT account_id FROM account_day_state WHERE account_id = :a", {"a": account_id}):
            u.session.execute("UPDATE account_day_state SET state = 'day_closed', closed_at = :t, rule_id = :r, loss = :l, on_trip = :o, updated_at = :t "
                              "WHERE account_id = :a", params)
        else:
            u.session.execute("INSERT INTO account_day_state (account_id, state, closed_at, rule_id, loss, on_trip, updated_at) "
                              "VALUES (:a, 'day_closed', :t, :r, :l, :o, :t)", params)
        u.session.execute("INSERT INTO account_day_events (account_id, kind, at, who, rule_id, loss) VALUES (:a, 'closed', :t, 'system', :r, :l)", params)
        return state_of(u, account_id)


def restart(u, account_id: int, note: str) -> bool:
    """The session of the account restarted: the close of the day is lifted by the system, with its record (who: system). True when it was closed."""
    if not is_closed(u, account_id):
        return False
    prior = state_of(u, account_id)
    stamp = utcnow()
    u.session.execute("UPDATE account_day_state SET state = 'open', updated_at = :t WHERE account_id = :a", {"t": stamp, "a": account_id})
    u.session.execute("INSERT INTO account_day_events (account_id, kind, at, who, rule_id, loss, note) VALUES (:a, 'lifted', :t, 'system', :r, :l, :n)",
                      {"a": account_id, "t": stamp, "r": prior["rule_id"], "l": prior["loss"], "n": note})
    return True


def _loss_now(u, account_id: int, db: Database | None) -> str | None:
    """The loss of the day at this moment, live (SPEC-026, 3390c): None when it cannot be measured (it is recorded as unknown, never as zero)."""
    try:
        from hokma.exec import live

        m = live.measure(account_id, db=service._db(db))
        return None if m.loss is None else str(m.loss)
    except Exception:                                                                # noqa: BLE001  (the record says "unknown")
        return None


def lift(account_id: int, who: Who, *, confirm: bool = False, db: Database | None = None) -> dict[str, Any]:
    """The owner takes off the close of the day before the session restarts. Without `confirm` it answers 409 `confirm_required` with what
    closed it, so the owner reads it first; with it, the account opens and the event is written (who, when, loss of that moment). An agent
    never lifts it."""
    if who is None or not getattr(who, "owner", False):
        raise service.owner_only("lifting the close of the day of an account")
    with service._db(db).write() as u:
        _account(u, account_id)
        now = state_of(u, account_id)
        if now["state"] != "day_closed":
            raise BrokerError("not_closed", 409, text="the account is not closed for the day")
        if confirm is not True:
            raise BrokerError("confirm_required", 409, text=f"the account was closed by {now['rule_id']} with a loss of {now['loss']}: confirm to open it again")
        loss = _loss_now(u, account_id, db)
        stamp = utcnow()
        u.session.execute("UPDATE account_day_state SET state = 'open', updated_at = :t WHERE account_id = :a", {"t": stamp, "a": account_id})
        u.session.execute("INSERT INTO account_day_events (account_id, kind, at, who, rule_id, loss, note) VALUES (:a, 'lifted', :t, 'owner', :r, :l, :n)",
                          {"a": account_id, "t": stamp, "r": now["rule_id"], "l": loss, "n": "lifted by hand before the session restarted"})
        return state_of(u, account_id)


def events(account_id: int, db: Database | None = None, limit: int = 20) -> list[dict[str, Any]]:
    with service._db(db).read() as u:
        rows = u.session.query("SELECT kind, at, who, rule_id, loss, note FROM account_day_events WHERE account_id = :a ORDER BY id DESC LIMIT :n",
                               {"a": account_id, "n": limit})
    return [{**r, "at": str(r["at"])} for r in rows]

"""Brokers and accounts for the app and the chat (task 3294, C7-5c). One service behind the routes and the tools.
Only the simulated broker exists in class 7. A reset needs `confirmed=True` here, not only on the screen."""
from __future__ import annotations

from typing import Any, Callable

from hokma.broker.contract import Broker
from hokma.broker.sim import SimBroker
from hokma.db import Database, IntegrityViolation, NotFound, ValidationError, get_database
from hokma.db.repos.broker import BrokerAccount, BrokerRow, NameTaken
from hokma.i18n import t

_MESSAGES: dict[str, Callable[[], str]] = {      # one literal t(...) per message, for the catalog check
    "broker_not_found": lambda: t("broker.err.broker_not_found"),
    "account_not_found": lambda: t("broker.err.account_not_found"),
    "desk_not_found": lambda: t("broker.err.desk_not_found"),
    "invalid": lambda: t("broker.err.invalid"),
    "name_taken": lambda: t("broker.err.name_taken"),
    "not_confirmed": lambda: t("broker.err.not_confirmed"),
    "account_in_use": lambda: t("broker.err.account_in_use"),
    "desk_has_account": lambda: t("broker.err.desk_has_account"),
    "desk_without_account": lambda: t("broker.err.desk_without_account"),
}


class BrokerError(Exception):
    """`code` is stable for the API; `message` is in the student's language."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code, self.status = code, status

    @property
    def message(self) -> str:
        return _MESSAGES[self.code]()


def _db(db: Database | None) -> Database:
    return db or get_database()


def _open(u, row: BrokerRow) -> Broker:
    return SimBroker(u.broker, row)


def _broker(u, id: int) -> Broker:
    try:
        return _open(u, u.broker.get_broker(id))
    except NotFound as exc:
        raise BrokerError("broker_not_found", 404) from exc


def _account(u, id: int) -> tuple[Broker, BrokerAccount]:
    try:
        account = u.broker.get_account(id)
    except NotFound as exc:
        raise BrokerError("account_not_found", 404) from exc
    return _broker(u, account.broker_id), account


def _broker_view(b: SimBroker) -> dict[str, Any]:
    return {"id": b.id, "kind": b.kind, "name": b.row.name, "commission": b.commission, "slippage_bps": b.slippage_bps,
            "capabilities": {"accounts": b.capabilities.accounts, "reset": b.capabilities.reset,
                             "orders": b.capabilities.orders, "positions": b.capabilities.positions}}


def _account_view(u, b: Broker, account: BrokerAccount) -> dict[str, Any]:
    """`cash` and `equity` are None when the broker cannot work them out: never a made-up 0."""
    bal = b.balance(account.id)
    return {"id": account.id, "broker_id": b.id, "broker": b.row.name, "name": account.name,
            "initial_capital": account.initial_capital, "currency": account.currency, "status": account.status,
            "desk_id": u.broker.desk_of_account(account.id),
            "cash": bal.cash, "equity": bal.equity, "as_of": bal.as_of}


def desk_account(u, desk_id: int) -> dict[str, Any] | None:
    """What a desk summary carries: `{id, name, broker, initial_capital, balance, as_of}`, or None while it has no account."""
    account = u.broker.account_of_desk(desk_id)
    if account is None:
        return None
    b = _broker(u, account.broker_id)
    bal = b.balance(account.id)
    return {"id": account.id, "name": account.name, "broker": b.row.name, "initial_capital": account.initial_capital,
            "balance": {"cash": bal.cash, "equity": bal.equity, "currency": bal.currency}, "as_of": bal.as_of}


def _guard(fn):
    """A repeated name is 409 `name_taken`; any other refused input is 400 `invalid`."""
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except IntegrityViolation as exc:
            raise BrokerError("name_taken", 409) from exc
        except NameTaken as exc:
            raise BrokerError("name_taken", 409) from exc
        except ValidationError as exc:
            raise BrokerError("invalid") from exc
    return run


def list_brokers(db: Database | None = None) -> list[dict[str, Any]]:
    with _db(db).read() as u:
        return [_broker_view(_open(u, r)) for r in u.broker.list_brokers()]


@_guard
def create_broker(name: Any, commission: Any = None, slippage_bps: Any = None, db: Database | None = None) -> dict[str, Any]:
    with _db(db).write() as u:
        broker = _open(u, u.broker.create_broker("sim", name))
        broker.set_costs(commission, slippage_bps)
        return _broker_view(broker)


@_guard
def update_costs(broker_id: int, commission: Any = None, slippage_bps: Any = None, db: Database | None = None) -> dict[str, Any]:
    """Change the commission and/or the slippage of a broker; what is not given stays."""
    with _db(db).write() as u:
        b = _broker(u, broker_id)
        b.set_costs(commission, slippage_bps)
        return _broker_view(b)


def list_accounts(broker_id: int, db: Database | None = None) -> list[dict[str, Any]]:
    with _db(db).read() as u:
        b = _broker(u, broker_id)
        return [_account_view(u, b, a) for a in b.list_accounts()]


@_guard
def create_account(broker_id: int, name: Any, capital: Any, db: Database | None = None) -> dict[str, Any]:
    with _db(db).write() as u:
        b = _broker(u, broker_id)
        return _account_view(u, b, b.create_account(name, capital))


def get_account(account_id: int, db: Database | None = None) -> dict[str, Any]:
    with _db(db).read() as u:
        b, account = _account(u, account_id)
        return _account_view(u, b, account)


@_guard
def reset_account(account_id: int, confirmed: Any, db: Database | None = None) -> dict[str, Any]:
    if confirmed is not True:
        raise BrokerError("not_confirmed")
    with _db(db).write() as u:
        b, account = _account(u, account_id)
        return _account_view(u, b, b.reset(account.id))


def assign_account(desk_id: int, account_id: Any, db: Database | None = None) -> dict[str, Any]:
    """One account serves one desk and a desk has one account: both refusals are 409 with their own code."""
    try:
        with _db(db).write() as u:
            if u.desks.find(desk_id) is None:
                raise BrokerError("desk_not_found", 404)
            _account(u, account_id if isinstance(account_id, int) and not isinstance(account_id, bool) else 0)
            owner = u.broker.desk_of_account(account_id)
            if owner == desk_id:
                return desk_account(u, desk_id)                  # same assignment again: nothing changes
            if owner is not None:
                raise BrokerError("account_in_use", 409)
            if u.broker.account_of_desk(desk_id) is not None:
                raise BrokerError("desk_has_account", 409)
            u.broker.link_desk(desk_id, account_id)
            return desk_account(u, desk_id)
    except IntegrityViolation as exc:                            # two assignments at once: the second loses
        raise BrokerError("account_in_use", 409) from exc


def unassign_account(desk_id: int, db: Database | None = None) -> dict[str, Any]:
    with _db(db).write() as u:
        if u.desks.find(desk_id) is None:
            raise BrokerError("desk_not_found", 404)
        if u.broker.account_of_desk(desk_id) is None:
            raise BrokerError("desk_without_account", 404)
        u.broker.unlink_desk(desk_id)
        return {"desk_id": desk_id, "account": None}

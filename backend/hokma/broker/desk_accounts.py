"""The accounts of a desk (SPEC-027, piece 027c, task 3335). A desk is born without accounts; they are added when they are needed, in the
chat (the tools of the chief, which always ask the owner) or with the button of the app, and both come through these functions.

- add_demo_account: the demo account is created and linked in ONE transaction, so a failure leaves no loose account.
- add_account: links an account that already exists in a connected broker. It may be in other desks too (many to many).
- remove_account: refused while a strategy of the desk trades through it or the desk has orders working in it. Positions belong to the
  account and are not touched.
- bind_strategy: says which account a strategy of the desk trades through.

The first account of a desk also becomes the account of its `principal` strategy (a single yes of the owner covers both).
Nothing here reads or writes an account number: an account is its id, its name and its mask."""
from __future__ import annotations

from typing import Any

from hokma.broker import service
from hokma.broker.service import BrokerError
from hokma.db import Database, IntegrityViolation, NotFound, ValidationError
from hokma.db.repos.broker import PRINCIPAL, AccountInUseByStrategy, AlreadyLinked, NameTaken, NotLinked
from hokma.i18n import t
from hokma.schwab.accounts import OPEN_ORDER_STATUSES
from hokma.schwab.http import SchwabError

SIM_KIND = "sim"                                      # the internal broker: the one that can create demo accounts
LIVE_STATUSES = (*OPEN_ORDER_STATUSES, "PARTIALLY_FILLED")


def _id(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _desk(u, desk_id: Any):
    desk = u.desks.find(_id(desk_id))
    if desk is None:
        raise BrokerError("desk_not_found", 404)
    return desk


def _active_account(u, account_id: Any):
    try:
        broker, account = service._account(u, _id(account_id))
    except BrokerError:
        raise
    if account.status != "active":
        raise BrokerError("account_not_found", 404)
    return broker, account


def _view(u, desk_id: int, broker, account, bound: list[str]) -> dict[str, Any]:
    """The account as the broker reads it, plus where it sits: the desks that hold it and the strategies it was tied to now."""
    out = service._account_view(u, broker, account)
    desks = u.broker.desks_of_account(account.id)
    out.update(desk_id=desk_id, desk_ids=desks, shared_with=[d for d in desks if d != desk_id], bound_strategies=bound,
               strategies=u.broker.strategies_of_account(desk_id, account.id))
    return out


def of_desk(u, desk_id: int) -> dict[str, Any]:
    """What a desk summary carries (SPEC-027): its accounts, each with its balance, the desks that share it and the strategies that use it,
    and which account each strategy trades through. A desk without accounts has an empty list: that is a desk that only analyzes."""
    accounts = []
    for account in u.broker.accounts_of_desk(desk_id):
        broker = service._broker(u, account.broker_id)
        bal = broker.balance(account.id)
        link = u.broker.external_of(account.id)
        accounts.append({"id": account.id, "name": account.name, "broker": broker.row.name, "mask": link.mask if link else None,
                         "initial_capital": account.initial_capital, "balance": {"cash": bal.cash, "equity": bal.equity, "currency": bal.currency},
                         "as_of": bal.as_of, "shared_with": [d for d in u.broker.desks_of_account(account.id) if d != desk_id],
                         "strategies": u.broker.strategies_of_account(desk_id, account.id)})
    return {"accounts": accounts, "strategies": u.broker.strategy_bindings(desk_id)}


def link_account(u, desk_id: int, account, added_by: str = "owner") -> list[str]:
    """Put the account in the desk. When it is the first of the desk and the principal strategy has none, the principal trades through
    it. Returns the strategies tied now. Linking twice is not an error: nothing changes."""
    first = not u.broker.accounts_of_desk(desk_id)
    try:
        u.broker.link(desk_id, account.id, added_by)
    except AlreadyLinked:
        return []
    if first and u.broker.account_of_strategy(desk_id, PRINCIPAL) is None:
        u.broker.bind_strategy(desk_id, PRINCIPAL, account.id)
        return [PRINCIPAL]
    return []


def _guarded(fn):
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except NameTaken as exc:
            raise BrokerError("name_taken", 409) from exc
        except AccountInUseByStrategy as exc:
            raise BrokerError("account_in_use_by_strategy", 409, text=t("broker.err.account_in_use_by_strategy")) from exc
        except NotLinked as exc:
            raise BrokerError("account_not_linked", 409, text=t("broker.err.account_not_linked")) from exc
        except IntegrityViolation as exc:                 # two calls at once: the second one loses
            raise BrokerError("invalid", 409) from exc
        except ValidationError as exc:
            raise BrokerError("invalid") from exc
        except NotFound as exc:
            raise BrokerError("account_not_found", 404) from exc
    return run


@_guarded
def add_demo_account(desk_id: Any, name: Any, initial_capital: Any, *, added_by: str = "owner", db: Database | None = None) -> dict[str, Any]:
    """Create a demo account in the internal broker and put it in the desk, in one transaction (a failure leaves nothing)."""
    with service._db(db).write() as u:
        desk = _desk(u, desk_id)
        sim = next((b for b in u.broker.list_brokers() if b.kind == SIM_KIND), None)
        if sim is None:
            raise BrokerError("no_demo_broker", 409, text=t("broker.err.no_demo_broker"))
        broker = service._broker(u, sim.id)
        account = broker.create_account(name if isinstance(name, str) else "", initial_capital if isinstance(initial_capital, int) else 0)
        bound = link_account(u, desk.id, account, added_by)
        return _view(u, desk.id, broker, account, bound)


@_guarded
def add_account(desk_id: Any, account_id: Any, *, added_by: str = "owner", db: Database | None = None) -> dict[str, Any]:
    """Put an existing account in the desk. The account may serve other desks too; the answer says which."""
    with service._db(db).write() as u:
        desk = _desk(u, desk_id)
        broker, account = _active_account(u, account_id)
        bound = link_account(u, desk.id, account, added_by)
        return _view(u, desk.id, broker, account, bound)


def _working_orders(u, desk_id: int, account) -> list[str]:
    """The orders of this desk that are still alive in the account, read from the broker (an order that was sent is on file with the
    desk; whether it is alive is the broker's to say)."""
    refs = set(u.broker.placed_order_refs(desk_id, account.id))
    if not refs:
        return []
    broker = service._broker(u, account.broker_id)
    if not broker.capabilities.orders:
        return []
    try:
        orders = broker.orders(account.id) or ()
    except SchwabError as error:
        raise service._schwab_error(error) from error
    found = []
    for order in orders:
        get = order.get if isinstance(order, dict) else lambda key, default=None, o=order: getattr(o, key, default)
        ref, status = str(get("order_id") or ""), str(get("status") or "")
        if ref in refs and status in LIVE_STATUSES:
            found.append(ref)
    return found


@_guarded
def remove_account(desk_id: Any, account_id: Any, *, db: Database | None = None) -> dict[str, Any]:
    """Take an account out of the desk. 409 `account_in_use_by_strategy` while a strategy trades through it, 409 `working_orders` while
    the desk has orders alive in it. The account, its money and its positions stay."""
    with service._db(db).write() as u:
        desk = _desk(u, desk_id)
        broker, account = service._account(u, _id(account_id))
        if account.id not in {a.id for a in u.broker.accounts_of_desk(desk.id)}:
            raise BrokerError("account_not_linked", 404, text=t("broker.err.account_not_linked"))
        if u.broker.strategies_of_account(desk.id, account.id):
            raise AccountInUseByStrategy(f"account {account.id} is used by a strategy of desk {desk.id}")
        if _working_orders(u, desk.id, account):
            raise BrokerError("working_orders", 409, text=t("broker.err.working_orders"))
        u.broker.unlink(desk.id, account.id)
        return {"desk_id": desk.id, "account_id": account.id, "accounts": [a.id for a in u.broker.accounts_of_desk(desk.id)]}


@_guarded
def bind_strategy(desk_id: Any, strategy_key: Any, account_id: Any, *, db: Database | None = None) -> dict[str, Any]:
    """Say that a strategy of the desk trades through one of the accounts of the desk (it moves the strategy if it had another)."""
    with service._db(db).write() as u:
        desk = _desk(u, desk_id)
        _active_account(u, account_id)
        u.broker.bind_strategy(desk.id, strategy_key, _id(account_id))
        return {"desk_id": desk.id, "strategy_key": strategy_key, "account_id": _id(account_id), "bindings": u.broker.strategy_bindings(desk.id)}

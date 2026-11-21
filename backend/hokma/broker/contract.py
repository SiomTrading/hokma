"""The contract every broker obeys (task 3294, C7-5a; orders SPEC-015 18.2): accounts with capital, balance and reset, and, for
the brokers whose `capabilities.orders` is true, orders and positions. The internal broker has no orders: it raises
`OrdersNotSupported`. The rules that decide whether an order goes out (18.3) are NOT here: a broker only delegates."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from hokma.broker.types import Balance, Capabilities
from hokma.db.repos.broker import BrokerAccount, BrokerRepo, BrokerRow
from hokma.db.timeutil import utcnow

ORDERS_MESSAGE = "this broker does not take orders or report positions"


class OrdersNotSupported(NotImplementedError):
    def __init__(self) -> None:
        super().__init__(ORDERS_MESSAGE)


class NotSupported(NotImplementedError):
    """The broker cannot do this (a reset or a new account in an outside broker). The service turns it into a 409."""


@runtime_checkable
class Broker(Protocol):
    id: int
    kind: str
    capabilities: Capabilities

    def list_accounts(self) -> list[BrokerAccount]: ...
    def account(self, id: int) -> BrokerAccount: ...
    def balance(self, id: int) -> Balance: ...
    def reset(self, id: int) -> BrokerAccount: ...
    def create_account(self, name: str, capital: int) -> BrokerAccount: ...
    def positions(self, account_id: int) -> Any: ...
    def orders(self, account_id: int, **filters: Any) -> Any: ...
    def get_order(self, account_id: int, order_id: Any) -> Any: ...
    def place_order(self, account_id: int, order: dict) -> Any: ...
    def replace_order(self, account_id: int, order_id: Any, order: dict) -> Any: ...
    def cancel_order(self, account_id: int, order_id: Any) -> Any: ...
    def preview_order(self, account_id: int, order: dict) -> Any: ...


class BaseBroker:
    """Accounts over the tables of 0018. The cash is the amount of the last ledger entry; the internal broker has no
    orders, so its equity is its cash. An account of another broker is not found here."""

    capabilities = Capabilities()

    def __init__(self, repo: BrokerRepo, row: BrokerRow) -> None:
        self.repo, self.row = repo, row
        self.id, self.kind = row.id, row.kind

    def list_accounts(self) -> list[BrokerAccount]:
        return self.repo.list_accounts(self.id)

    def account(self, id: int) -> BrokerAccount:
        account = self.repo.get_account(id)
        if account.broker_id != self.id:
            from hokma.db.errors import NotFound

            raise NotFound(f"account {id} not found in broker {self.row.name!r}")
        return account

    def balance(self, id: int) -> Balance:
        account = self.account(id)
        last = self.repo.last_entry(id)
        cash = last.amount if last else None
        return Balance(cash=cash, equity=cash, currency=account.currency, as_of=last.created_at if last else utcnow())

    def reset(self, id: int) -> BrokerAccount:
        self.account(id)
        return self.repo.reset_account(id)

    def create_account(self, name: str, capital: int) -> BrokerAccount:
        return self.repo.create_account(self.id, name, capital)

    def positions(self, *args: Any, **kwargs: Any) -> Any:
        raise OrdersNotSupported

    def orders(self, *args: Any, **kwargs: Any) -> Any:
        raise OrdersNotSupported

    def get_order(self, *args: Any, **kwargs: Any) -> Any:
        raise OrdersNotSupported

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        raise OrdersNotSupported

    def replace_order(self, *args: Any, **kwargs: Any) -> Any:
        raise OrdersNotSupported

    def preview_order(self, *args: Any, **kwargs: Any) -> Any:
        raise OrdersNotSupported

    def cancel_order(self, *args: Any, **kwargs: Any) -> Any:
        raise OrdersNotSupported

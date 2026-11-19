"""The contract every broker obeys (task 3294, C7-5a): accounts with capital, balance and reset. Orders and positions
are declared here and fail with a clear message until class 9."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from hokma.broker.types import Balance, Capabilities
from hokma.db.repos.broker import BrokerAccount, BrokerRepo, BrokerRow
from hokma.db.timeutil import utcnow

ORDERS_MESSAGE = "orders and positions arrive in class 9"


class OrdersNotSupported(NotImplementedError):
    def __init__(self) -> None:
        super().__init__(ORDERS_MESSAGE)


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
    def place_order(self, *args: Any, **kwargs: Any) -> Any: ...
    def positions(self, *args: Any, **kwargs: Any) -> Any: ...
    def cancel_order(self, *args: Any, **kwargs: Any) -> Any: ...


class BaseBroker:
    """Accounts over the tables of 0018. The cash is the amount of the last ledger entry; with no orders (class 7) the
    equity is the cash. An account of another broker is not found here."""

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

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        raise OrdersNotSupported

    def positions(self, *args: Any, **kwargs: Any) -> Any:
        raise OrdersNotSupported

    def cancel_order(self, *args: Any, **kwargs: Any) -> Any:
        raise OrdersNotSupported

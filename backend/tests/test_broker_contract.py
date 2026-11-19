"""Broker contract (task 3294, C7-5a): any Broker creates accounts, reports a balance, resets one account and refuses orders."""
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.broker.contract import ORDERS_MESSAGE, Broker, OrdersNotSupported
from hokma.broker.sim import SimBroker
from hokma.broker.types import Balance
from hokma.db.errors import NotFound, ValidationError
from hokma.db.seeds import SIM_BROKER

BROKERS = [SimBroker]


@pytest.fixture(params=BROKERS)
def broker(request, db):  # noqa: F811
    with db.write() as u:
        yield request.param(u.broker, u.broker.find_broker(SIM_BROKER))


def test_the_simulated_broker_is_seeded_once_with_its_costs(db):  # noqa: F811
    with db.write() as u:
        rows = u.broker.list_brokers()
        assert [(r.kind, r.name) for r in rows] == [("sim", SIM_BROKER)]
        assert rows[0].config == {"commission": {"type": "bps", "value": 1.0, "min": 0.0}, "slippage_bps": 2.0}


def test_it_is_a_broker_and_declares_what_it_cannot_do(broker):
    assert isinstance(broker, Broker) and broker.kind == "sim"
    assert broker.capabilities.accounts and broker.capabilities.reset
    assert not broker.capabilities.orders and not broker.capabilities.positions


def test_create_balance_and_reset_one_account_without_touching_the_others(broker):
    ids = [broker.create_account(n, c).id for n, c in (("A", 1000), ("B", 25000), ("C", 100))]
    assert [a.name for a in broker.list_accounts()] == ["A", "B", "C"]
    first = broker.balance(ids[0])
    assert isinstance(first, Balance) and (first.cash, first.equity, first.currency) == (1000, 1000, "USD") and first.as_of
    broker.repo.s.insert("account_ledger", {"account_id": ids[0], "kind": "open", "amount": 5, "created_at": "x"})  # a later entry
    assert broker.balance(ids[0]).cash == 5
    broker.reset(ids[0])
    assert broker.balance(ids[0]).cash == 1000
    assert [e.kind for e in broker.repo.ledger(ids[0])] == ["open", "reset"]
    assert [broker.balance(i).cash for i in ids[1:]] == [25000, 100]
    assert [len(broker.repo.ledger(i)) for i in ids[1:]] == [1, 1]


def test_balance_is_none_not_zero_when_it_cannot_be_known(broker):
    account = broker.create_account("A", 1000)
    broker.repo.s.execute("DELETE FROM account_ledger")
    balance = broker.balance(account.id)
    assert balance.cash is None and balance.equity is None


@pytest.mark.parametrize("name,capital", [("", 1000), ("x" * 61, 1000), ("  ", 1000), ("A", 99), ("A", 1_000_000_001),
                                           ("A", 1000.5), ("A", True), ("A", "1000")])
def test_the_frontier_rejects_bad_names_and_capitals(broker, name, capital):
    with pytest.raises(ValidationError):
        broker.create_account(name, capital)
    assert broker.list_accounts() == []


def test_limits_are_inclusive_currency_is_usd_and_names_are_unique(broker):
    broker.create_account("min", 100), broker.create_account("max", 1_000_000_000), broker.create_account("x" * 60, 500)
    with pytest.raises(ValidationError):
        broker.create_account("min", 200)
    with pytest.raises(ValidationError):
        broker.repo.create_account(broker.id, "eur", 1000, currency="EUR")


def test_an_account_of_another_broker_is_not_found(broker):
    other = broker.repo.create_broker("sim", "Other")
    foreign = broker.repo.create_account(other.id, "F", 1000)
    for call in (broker.account, broker.balance, broker.reset):
        with pytest.raises(NotFound):
            call(foreign.id)


def test_orders_and_positions_fail_with_the_class_9_message(broker):
    for call in (broker.place_order, broker.positions, broker.cancel_order):
        with pytest.raises(OrdersNotSupported, match=ORDERS_MESSAGE):
            call()
    assert issubclass(OrdersNotSupported, NotImplementedError)

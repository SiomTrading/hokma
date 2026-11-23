"""The same cases against every broker with recorded answers and no network (SPEC-018: AH-40, DTS-08b, OA-55, OA-72).

A broker is tested through a `Harness`: the callables that do the same five things at that broker (list its accounts, read the balance and the
positions, place an order, read it back, cancel it) with the broker's own recorded answers. The cases are written once and driven by what
the broker declares in `capabilities`: a method it declares must work, and one it does not declare must fail with `OrdersNotSupported`. No
identifier of an account may be in anything a broker returns (`secrets` are the sentinels of the recordings).

Harnesses: `sim` and `schwab` here; `alpaca`, `ibkr` and `crypto` register theirs in `harnesses_connectors.py`."""
from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from broker_orders_support import (ORDERS, SECRETS, FIRST_ID, calls, connections, created, desks_db, env, db, fake, keys, limit,  # noqa: F401
                                   order_body, pos, account_body, ACCOUNT, ready, server)

from hokma.broker import sim_engine
from hokma.broker.contract import Broker, OrdersNotSupported
from hokma.broker.schwab import SchwabBroker
from hokma.broker.sim import SimBroker
from hokma.broker.types import Balance, Order, Position
from hokma.db.errors import NotFound


@dataclass
class Harness:
    """What a broker does in these cases. `place` returns the answer of the broker (a dict with order_id)."""
    name: str
    broker: Any
    account_id: int
    secrets: tuple[str, ...]
    place: Callable[[], dict]
    expect_order: Callable[[str], None] = lambda order_id: None       # arms the recorded answer that reads or cancels that order
    prepare_positions: Callable[[], None] = lambda: None
    expects_position: str | None = None                              # the symbol the recorded positions have
    cancel_ok: bool = True
    extra: dict = field(default_factory=dict)


HARNESSES: dict[str, Callable[..., Iterator[Harness]]] = {}


def harness(name: str):
    def wrap(fn):
        HARNESSES[name] = fn
        return fn
    return wrap


@harness("sim")
def sim_harness(request) -> Iterator[Harness]:
    desks_db = request.getfixturevalue("desks_db")
    board = sim_engine.QuoteBoard(clock=lambda: 1000.0)
    sim_engine._board, sim_engine._source = board, None
    import hokma.broker.sim as sim_module

    real_time = sim_module.time.time
    sim_module.time.time = lambda: 1000.0
    board.feed("EQ:SPY", bid="600.00", ask="600.02", last="600.01")
    try:
        with desks_db.write() as u:
            row = next(b for b in u.broker.list_brokers() if b.kind == "sim")
            account = u.broker.create_account(row.id, "Conformance", 100_000)
        with desks_db.read() as u:
            broker = SimBroker(u.broker, next(b for b in u.broker.list_brokers() if b.kind == "sim"))
            order = {"fingerprint": "f" * 64, "type": "limit", "tif": "gtc", "limit_price": "100", "stop_price": None, "stop_loss": None, "take_profit": None,
                     "reduce_only": False, "legs": [{"contract_id": "EQ:SPY", "symbol": "SPY", "side": "buy", "effect": "open", "qty": "1",
                                                     "multiplier": "1", "tick_size": "0.01", "asset_type": "equity"}]}
            yield Harness("sim", broker, account.id, (), lambda: broker.place_order(account.id, order))
    finally:
        sim_module.time.time = real_time


@harness("schwab")
def schwab_harness(request) -> Iterator[Harness]:
    ready = request.getfixturevalue("ready")
    with ready.db.read() as u:
        row = next(r for r in u.broker.list_brokers() if r.kind == "schwab")
        broker = SchwabBroker(u.broker, row)

        def expect(order_id: str) -> None:
            ready.fake.on("GET", f"{ORDERS}/{order_id}", order_body(int(order_id), cancelable=True))
            ready.fake.on("DELETE", f"{ORDERS}/{order_id}", (200, None))

        def positions() -> None:
            ready.fake.on("GET", ACCOUNT, account_body([pos("AAPL", long=10, value=2000.0)]))

        yield Harness("schwab", broker, ready.account_id, SECRETS, lambda: broker.place_order(ready.account_id, limit()), expect, positions, "AAPL")


from broker import harnesses_connectors      # noqa: E402  (the connectors of SPEC-018 B5 add their harnesses here)

harnesses_connectors.register(harness, Harness)


@pytest.fixture(params=sorted(HARNESSES))
def h(request) -> Iterator[Harness]:
    yield from HARNESSES[request.param](request)


def text_of(value: Any) -> str:
    return json.dumps(dataclasses.asdict(value) if dataclasses.is_dataclass(value) else value, default=str)


def test_it_is_a_broker_and_declares_what_it_can_do(h):
    assert isinstance(h.broker, Broker)
    assert h.broker.capabilities.accounts is True and isinstance(h.broker.capabilities.orders, bool)


def test_accounts_and_balance_with_no_identifier_in_them(h):
    accounts = h.broker.list_accounts()
    assert any(a.id == h.account_id for a in accounts)
    balance = h.broker.balance(h.account_id)
    assert isinstance(balance, Balance) and balance.currency == "USD"
    assert balance.cash is None or isinstance(balance.cash, float)
    for secret in h.secrets:
        assert secret not in text_of(accounts) and secret not in text_of(balance)


def test_an_account_of_another_broker_is_not_found(h, db):                # noqa: F811
    other = h.broker.repo.create_broker("sim", f"Other {h.name}")
    foreign = h.broker.repo.create_account(other.id, "F", 1000)
    with pytest.raises(NotFound):
        h.broker.account(foreign.id)


def test_positions_are_signed_and_masked(h):
    if not h.broker.capabilities.positions:
        with pytest.raises(OrdersNotSupported):
            h.broker.positions(h.account_id)
        return
    h.prepare_positions()
    found = h.broker.positions(h.account_id)
    assert isinstance(found, list) and all(isinstance(p, Position) for p in found)
    if h.expects_position:
        assert [p for p in found if p.symbol == h.expects_position]
    for secret in h.secrets:
        assert secret not in text_of(found)


def test_orders_place_read_and_cancel(h):
    if not h.broker.capabilities.orders:
        for call in (h.broker.place_order, h.broker.cancel_order):
            with pytest.raises(OrdersNotSupported):
                call(h.account_id, {})
        return
    placed = h.place()
    order_id = str(placed["order_id"])
    h.expect_order(order_id)
    read = h.broker.get_order(h.account_id, order_id)
    assert read is None or isinstance(read, Order)
    listed = h.broker.orders(h.account_id)
    assert isinstance(listed, list)
    if h.cancel_ok:
        canceled = h.broker.cancel_order(h.account_id, order_id)
        assert isinstance(canceled, dict)
    for secret in h.secrets:
        assert secret not in text_of(placed) and secret not in text_of(read) and secret not in text_of(listed)


def test_a_method_it_does_not_declare_fails_the_suite_instead_of_being_skipped(h):
    caps = h.broker.capabilities
    for method, declared in (("place_order", caps.orders), ("positions", caps.positions)):
        assert callable(getattr(h.broker, method))
        if not declared:
            with pytest.raises(OrdersNotSupported):
                getattr(h.broker, method)(h.account_id, {}) if method == "place_order" else getattr(h.broker, method)(h.account_id)

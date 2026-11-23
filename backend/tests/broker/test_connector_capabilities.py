"""A connector that declares a capability it does not implement fails the suite (SPEC-018 B5: AH-40). The cases of the suite are run, one by one,
against a connector whose declaration was changed, and they must fail; the same cases pass against the honest one."""
from __future__ import annotations

import dataclasses

import pytest
from broker import test_contract_conformance as suite
from broker_orders_support import connections, desks_db, env, db, keys  # noqa: F401  (fixtures)

from hokma.broker.contract import OrdersNotSupported
from hokma.broker.types import Capabilities

KINDS = ("alpaca", "ibkr", "crypto")


@pytest.fixture(params=KINDS)
def honest(request):
    yield from suite.HARNESSES[request.param](request)


def lied(h, capabilities: Capabilities | None = None, **replaced) -> suite.Harness:
    """The same harness over a broker that says something else than it does: its declaration, or one of its methods, is replaced."""
    if capabilities is not None:
        h.broker.capabilities = capabilities
    for name, value in replaced.items():
        setattr(h.broker, name, value)
    return dataclasses.replace(h)


def refuse(*args, **kwargs):
    raise OrdersNotSupported


def test_the_honest_connector_passes_every_case(honest):
    for case in (suite.test_it_is_a_broker_and_declares_what_it_can_do, suite.test_positions_are_signed_and_masked,
                 suite.test_orders_place_read_and_cancel, suite.test_a_method_it_does_not_declare_fails_the_suite_instead_of_being_skipped):
        case(honest)


def test_declaring_orders_without_a_working_place_order_fails_the_suite(honest):
    broken = lied(honest, place_order=refuse)
    with pytest.raises(OrdersNotSupported):
        suite.test_orders_place_read_and_cancel(broken)


def test_declaring_positions_without_working_positions_fails_the_suite(honest):
    broken = lied(honest, positions=refuse)
    with pytest.raises(OrdersNotSupported):
        suite.test_positions_are_signed_and_masked(broken)


FAILS = (AssertionError, pytest.fail.Exception)          # a failed assert and a failed `pytest.raises` inside the case


def test_working_orders_that_are_not_declared_fail_the_suite(honest):
    broken = lied(honest, Capabilities(accounts=True, reset=False, orders=False, positions=True))
    with pytest.raises(FAILS):
        suite.test_orders_place_read_and_cancel(broken)            # the suite expects OrdersNotSupported from a broker that declares no orders


def test_working_positions_that_are_not_declared_fail_the_suite(honest):
    broken = lied(honest, Capabilities(accounts=True, reset=False, orders=True, positions=False))
    with pytest.raises(FAILS):
        suite.test_positions_are_signed_and_masked(broken)


def test_an_identifier_in_an_answer_fails_the_suite(honest):
    leaky = dataclasses.replace(honest, secrets=honest.secrets + ("WORKING",))       # a sentinel that shows in what the broker returns is caught
    with pytest.raises(FAILS):
        suite.test_orders_place_read_and_cancel(leaky)

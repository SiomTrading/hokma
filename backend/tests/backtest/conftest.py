"""Fixtures of the backtest tests (N-41): the four real NQ scenarios, each with its own hand computed result.

The four results are different on purpose (T1 100.50, T2 -114.50, T3 -2059.50, T4 -49.50), so a test that gets the bars of another
scenario cannot pass by accident: test_fixtures.py swaps them and checks that every swap fails."""
import pytest

from backtest import support


@pytest.fixture(scope="session")
def nq():
    return support.load_fixture()


@pytest.fixture(params=support.SCENARIOS)
def scenario_name(request):
    return request.param

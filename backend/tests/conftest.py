"""Tests never probe optional native parts in child processes (task 3288); the probe tests turn it back on."""
import os

from hokma.core import processguard

os.environ.setdefault("HOKMA_NATIVE_PROBE", "off")
# Tests run the sensitive packages in the pytest process, as the server does. Set the module variable directly: the
# public entry points refuse any caller that is not one of Hokma's. Child processes start without it (test_processguard).
processguard._role = processguard.SERVER


import pytest

from hokma.market import alpaca_hours

REAL_ALPACA_FETCH_DAY = alpaca_hours.fetch_day              # the tests of the calendar itself use these two
REAL_ALPACA_PHASE = alpaca_hours.phase


@pytest.fixture(autouse=True)
def no_alpaca_calendar_network(monkeypatch):
    """No test reaches Alpaca's calendar through the network: the hours of Alpaca are "unknown" unless a test feeds them (SPEC-016 A3)."""
    monkeypatch.setattr(alpaca_hours, "fetch_day", lambda *args, **kwargs: None)
    monkeypatch.setattr(alpaca_hours, "phase", lambda *args, **kwargs: None)

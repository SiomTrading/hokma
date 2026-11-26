"""The trade context of a cycle reads the account of the principal strategy and, with none, sizes on an empty book at the desk's capital (SPEC-027 027b)."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from exec_support import Ready, db, desks_db, env, ready  # noqa: F401

from hokma.desks.swo import app_runtime
from account_links_support import unlink_all


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(app_runtime, "_chain", lambda symbol, desk_id: ({}, {}, {}))
    monkeypatch.setattr("hokma.desks.agents.manager.load_desk_mandate", lambda desk_id: SimpleNamespace(risk=SimpleNamespace(max_risk_per_trade_pct=1)))


def test_a_desk_without_accounts_gets_a_context_on_an_empty_book_at_its_capital(ready, stubbed):
    with ready.db.write() as u:
        unlink_all(u, ready.desk_id)
    ctx = app_runtime.trade_context(ready.desk_id, "SPY", datetime.now(timezone.utc))
    assert ctx.current_book == {} and ctx.budget.equity == Decimal(1_000_000) and ctx.budget.day_pnl == 0 and ctx.budget.open_contracts == 0


def test_the_context_of_a_desk_with_an_account_reads_that_account(ready, stubbed):
    ctx = app_runtime.trade_context(ready.desk_id, "SPY", datetime.now(timezone.utc))
    assert ctx.budget.equity is not None and ctx.budget.cash is not None


def test_two_accounts_and_no_account_for_the_principal_strategy_does_not_take_the_first(ready, stubbed):
    with ready.db.write() as u:
        u.broker.unbind_strategy(ready.desk_id, "principal")
        u.broker.link(ready.desk_id, u.broker.create_account(ready.broker_id, "Sim B", 123).id)
    ctx = app_runtime.trade_context(ready.desk_id, "SPY", datetime.now(timezone.utc))
    assert ctx.budget.equity == Decimal(1_000_000)             # the capital of the desk: no account was read

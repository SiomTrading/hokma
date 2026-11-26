"""The blockers of the review of 3390c (SPEC-026, task 3390): the close that does not stop at the first failure and is retried, a desk rule that closes
every account of the desk, the account that stays evaluated after it closed, and the second line wired."""
from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
from account_links_support import link_principal
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures)
from exec_support import NQ, SPY, ready  # noqa: F401  (fixtures)
from test_live_freeze import feed, fill, quote, rule  # noqa: F401  (feed is a fixture)

from hokma.backoffice import book as book_module
from hokma.broker import day_close
from hokma.exec import live, submit
from hokma.i18n import t

D = Decimal


def open_qty(r, account_id=None):
    return {c: p.qty for c, p in book_module.Book.load(account_id or r.account_id, db=r.db).cost_positions().items() if p.qty != 0}


def test_b2_one_contract_that_fails_does_not_stop_the_others_the_text_is_exact_and_the_monitor_retries(ready, monkeypatch):
    fill(ready, ready.account_id, ready.desk_id, SPY, "buy", 10, 600, "b2a")
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "b2b")
    real = submit.dispatch
    broken = {"on": True}

    def flaky(broker, account_id, action, order=None, order_id=None, *, owner=None):
        if broken["on"] and "SPY" in repr(order):
            raise RuntimeError("the broker did not answer")
        return real(broker, account_id, action, order, order_id, owner=owner)

    monkeypatch.setattr(submit, "dispatch", flaky)
    text, remaining = live.close_positions(ready.db, ready.account_id, None, include_long_shares=True)
    assert open_qty(ready) == {SPY: D(10)}                                               # NQ was closed although SPY failed before it
    assert remaining == [SPY] and t("live.close.closed", list=NQ) in text and t("live.close.failed", list=SPY) in text      # exact: what closed and what did not
    mon = live.LiveMonitor(ready.db)
    told: list[str] = []
    monkeypatch.setattr(live, "_notify", lambda database, text: told.append(text))
    mon.retry_close(ready.account_id, None, True)
    mon._retry_closes()                                                                   # still broken: it stays on the list
    assert (ready.account_id, None) in mon._retries and not told
    broken["on"] = False
    mon._retry_closes()
    assert open_qty(ready) == {} and not mon._retries and len(told) == 1


def test_b2_the_retries_have_a_limit_and_the_owner_is_told(ready, monkeypatch):
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "b2c")
    monkeypatch.setattr(submit, "dispatch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    mon = live.LiveMonitor(ready.db)
    told: list[str] = []
    monkeypatch.setattr(live, "_notify", lambda database, text: told.append(text))
    mon.retry_close(ready.account_id, None, True)
    for _ in range(live.CLOSE_RETRIES + 2):
        mon._retry_closes()
    assert not mon._retries and len(told) == 1 and NQ in told[0]


def test_b2_a_rule_of_the_desk_closes_every_account_of_the_desk(ready, feed):
    with ready.db.write() as u:
        second = u.broker.create_account(ready.broker_id, "Sim B", 1_000_000).id
        link_principal(u, ready.desk_id, second)
        u.broker.set_limits(ready.desk_id, {"funding": {"daily_loss_usd": 500}, "rule_modes": {"daily_loss": {"mode": "restriction", "on_trip": "block_and_flatten_all"}}})
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "b2d")
    fill(ready, second, ready.desk_id, NQ, "buy", 1, 21000, "b2e")
    quote(ready, NQ, "21000")                                                             # the sim broker needs a price to close with; the freeze does not read it
    feed.loss(ready.account_id, 1000)                                                     # the broker says 1000 in each account: 2000 in all, over the cap of 500
    feed.loss(second, 1000)
    mon = live.LiveMonitor(ready.db)
    mon.refresh()
    mon.evaluate(ready.account_id)                                                        # the event is in the first account
    assert open_qty(ready) == {} and open_qty(ready, second) == {}                         # and the second account of the desk is closed too


def test_b3_an_account_closed_for_the_day_stays_evaluated_by_the_monitor(ready):
    rule(ready)
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "b3")
    day_close.close_day(ready.account_id, "x", "1")
    mon = live.LiveMonitor(ready.db)
    mon.refresh()
    assert ready.account_id in mon._accounts                                              # a closed account is still looked at: a reopened one trips again
    mon.tick()
    assert ready.account_id in mon._accounts


def test_b1_the_second_line_and_the_mark_source_are_wired_in_the_startup():
    from hokma import features_0031

    source = inspect.getsource(features_0031.on_startup)
    assert "set_mark_source" in source and "KillSwitchTask" in source and "keep_measuring" in source
    assert "killswitch" in inspect.getsource(features_0031.on_shutdown)
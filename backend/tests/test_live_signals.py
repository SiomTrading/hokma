"""What the freeze keeps from the pieces that fed it with prices (SPEC-026 v2, piece 3390e3): the account stream of the broker as a signal to pull the fills, the stream
that follows it, and the owner's declaration of the start equity when the broker does not give it. The hub holds and the keeper of subscriptions are gone with the prices."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db  # noqa: F401  (fixtures)
from exec_support import ready  # noqa: F401  (fixtures)
from test_hub_streaming import Config, FakeStreamSource, History, daily_history, five_history, ny
from test_live_freeze import feed, rule  # noqa: F401  (fixtures)

from hokma.broker.service import BrokerError, Who
from hokma.exec import live
from hokma.market.adapters import ADAPTERS, register_adapter
from hokma.market.hub import MarketHub
from hokma.market.providers import get_catalog

D = Decimal


# ---- fills: the account stream is a signal, the sync is the backup ----------------------------------------------------------------------------

def test_a_message_of_account_activity_pulls_the_fills_of_the_accounts_of_that_broker_that_have_a_rule(ready, monkeypatch):
    pulled: list[int] = []
    monkeypatch.setattr(live, "pull_fills", lambda database, account_id: pulled.append(account_id))
    kinds = {"sim": ready.account_id}
    mon = live.start(ready.db)
    try:
        live.on_account_activity("sim", "ORDER_FILL")                                    # no rule yet: nothing is pulled
        import time
        time.sleep(0.2)
        assert pulled == []
        rule(ready)
        mon.wake_fills("sim", again_after=0)
        for _ in range(50):
            if pulled:
                break
            time.sleep(0.05)
        assert pulled == [ready.account_id]
        live.on_account_activity("sim", "ERROR")                                         # an error message is not a signal
        time.sleep(0.2)
        assert pulled == [ready.account_id]
        live.on_account_activity("other", "ORDER_FILL")
        time.sleep(0.2)
        assert pulled == [ready.account_id] and kinds
    finally:
        live.stop()


def test_the_streamer_subscribes_the_account_service_with_one_key_and_passes_only_the_type():
    from hokma.schwab import stream as streamer
    from streamer_fake_server import StreamerFakeServer
    from test_schwab_stream import Harness

    server = StreamerFakeServer().start()
    streamer.allow_local_for_tests(True)
    got: list[str] = []
    try:
        async def go():
            async with Harness(server) as h:
                h.streamer._on_activity = got.append
                h.streamer.want(streamer.ACCT_ACTIVITY, [streamer.ACTIVITY_KEY])
                await h.until(lambda: server.subscribed.get(streamer.ACCT_ACTIVITY) == {streamer.ACTIVITY_KEY})
                server.data(streamer.ACCT_ACTIVITY, [{"key": streamer.ACTIVITY_KEY, "1": "ACCOUNT-1234", "2": "OrderFill", "3": "{\"x\": 1}"}])
                await h.until(lambda: got)
                h.streamer.drop(streamer.ACCT_ACTIVITY, [streamer.ACTIVITY_KEY])
                await h.until(lambda: not server.subscribed.get(streamer.ACCT_ACTIVITY))
        asyncio.run(go())
    finally:
        streamer.allow_local_for_tests(False)
        server.stop()
    assert got == ["OrderFill"]                                                           # the account and the data never leave the session
    assert [c["command"] for c in server.commands(streamer.ACCT_ACTIVITY)] == ["SUBS", "UNSUBS"]


class RecordingSource(FakeStreamSource):
    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.activity = False

    def watch_account_activity(self, on):
        self.activity = on
        return True


@pytest.fixture
def stream(monkeypatch):
    holder: dict = {}
    previous = ADAPTERS["schwab"]

    def make(spec, secret, url):
        holder["source"] = RecordingSource()
        return holder["source"]

    register_adapter("schwab", make)
    yield holder
    register_adapter("schwab", previous)


def test_the_hub_follows_the_account_stream_only_when_its_source_has_one(stream):
    async def go():
        hub = MarketHub(get_catalog(), Config(), lambda: History(daily_history(), five_history()), clock=lambda: ny(10, 0))
        await hub._ensure_source(hub._runtimes["schwab"])
        assert await hub.watch_activity("schwab", True) and stream["source"].activity is True
        assert await hub.watch_activity("alpaca", True) is False                                                  # no such stream: the sync is the way
        await hub.close()
    asyncio.run(go())


# ---- the owner declares the start equity ---------------------------------------------------------------------------------------------------

def carried(ready, feed):
    """The broker gives the equity now but not the equity of the start of the session (a position carried over): the loss of the day is unknown."""
    rule(ready)
    feed.set(ready.account_id, now=99_000, start=None)


def test_a_broker_that_does_not_give_the_start_leaves_it_unknown_until_the_owner_declares_it(ready, feed):
    carried(ready, feed)
    state = live.start_state(ready.account_id, db=ready.db)
    assert state["needs_declaration"] and not state["measured"] and state["reason"] == "start_unknown"
    with pytest.raises(BrokerError) as agent:
        live.declare_start(ready.account_id, "100000", Who(agent="desk-chief"), confirm=True, db=ready.db)
    assert agent.value.code == "owner_only"
    with pytest.raises(BrokerError) as ask:
        live.declare_start(ready.account_id, "100000", ready.owner, db=ready.db)
    assert ask.value.code == "equity_confirm" and ask.value.status == 409
    for bad in ("0", "-5", "abc", "nan", None):
        with pytest.raises(BrokerError) as e:
            live.declare_start(ready.account_id, bad, ready.owner, confirm=True, db=ready.db)
        assert e.value.code == "bad_equity"
    after = live.declare_start(ready.account_id, "100000", ready.owner, confirm=True, db=ready.db)
    assert after["measured"] and after["declarations"][0]["equity"] == "100000" and after["declarations"][0]["by"]
    m = live.measure(ready.account_id, db=ready.db)
    assert m.measured and m.equity_start == D(100_000) and m.loss == D(1000)                  # the equity now is the broker's; the start is the owner's
    with pytest.raises(BrokerError) as known:
        live.declare_start(ready.account_id, "1", ready.owner, confirm=True, db=ready.db)
    assert known.value.code == "start_known"                                                  # a start that is measured is never overwritten


def test_the_start_the_broker_gives_stands_over_a_declaration(ready, feed):
    carried(ready, feed)
    live.declare_start(ready.account_id, "100000", ready.owner, confirm=True, db=ready.db)
    feed.set(ready.account_id, now=99_000, start=99_500)                                      # now the broker does give it
    m = live.measure(ready.account_id, db=ready.db)
    assert m.equity_start == D(99_500) and m.loss == D(500)


def test_the_declaration_is_recorded_and_it_only_holds_for_its_session(ready, feed):
    carried(ready, feed)
    live.declare_start(ready.account_id, "100000", ready.owner, confirm=True, db=ready.db)
    with ready.db.read() as u:
        rows = u.session.query("SELECT equity, declared_by, declared_at, session_key FROM account_start_declarations WHERE account_id = :a", {"a": ready.account_id})
    assert len(rows) == 1 and rows[0]["declared_by"] and rows[0]["declared_at"]
    later = datetime.now(timezone.utc) + timedelta(days=3)                                   # another session: the declaration does not carry over
    m = live.measure(ready.account_id, db=ready.db, now=later)
    assert m.session_key != rows[0]["session_key"] and not m.measured


def test_the_owner_is_told_once_per_session_that_the_start_is_unknown(ready, feed):
    carried(ready, feed)
    told: list[str] = []
    mon = live.LiveMonitor(ready.db)
    import hokma.exec.live as module
    original = module._notify
    module._notify = lambda database, text: told.append(text)
    try:
        mon.evaluate(ready.account_id)
        mon.evaluate(ready.account_id)
    finally:
        module._notify = original
    assert len(told) == 1 and str(ready.account_id) in told[0]


def test_the_api_declares_the_start_for_the_owner_in_both_languages(ready, feed, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hokma.broker import api
    from hokma.rpc.ws import SESSION_TOKEN

    carried(ready, feed)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    head = {"x-hokma-token": SESSION_TOKEN}
    url = f"/api/accounts/{ready.account_id}/session-start"
    assert client.get(url).status_code == 401                                              # the same token as every route of the brokers
    assert client.get(url, headers=head).status_code == 200
    monkeypatch.setenv("HOKMA_LANG", "es")
    bad = client.post(url, headers=head, json={"equity": "-1"})
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "bad_equity" and "mayor que 0" in bad.json()["detail"]["message"]
    monkeypatch.setenv("HOKMA_LANG", "en")
    bad = client.post(url, headers=head, json={"equity": "-1"})
    assert "greater than 0" in bad.json()["detail"]["message"]
    ask = client.post(url, headers=head, json={"equity": "100000"})
    assert ask.status_code == 409 and ask.json()["detail"]["code"] == "equity_confirm"
    done = client.post(url, headers=head, json={"equity": "100000", "confirm": True})
    assert done.status_code == 200 and done.json()["measured"] is True
    again = client.post(url, headers=head, json={"equity": "100000", "confirm": True})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "start_known"      # now the start of this session is measured: it is not overwritten

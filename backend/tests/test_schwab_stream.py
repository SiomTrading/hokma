"""SPEC-016 T2: the Schwab Streamer session (hokma.schwab.stream) against a fake Streamer that speaks the guide's protocol.
Nothing touches the network and no secret is real: the token and the ids are made up and distinctive so the redaction tests can
look for them."""
import asyncio
import dataclasses
import logging

import pytest

from hokma.market.providers import get_catalog
from hokma.schwab import http, stream
from hokma.schwab.stream import CHART_EQUITY, LEVELONE_EQUITIES, LEVELONE_OPTIONS, SchwabStreamer, StreamConfig
from schwab_support import FAKE_TOKEN, FakeSchwab, load
from streamer_fake_server import CHANNEL, CORREL, CUSTOMER, FUNCTION, StreamerFakeServer

BACKOFF = (0.02, 0.02, 0.05)
FAST = StreamConfig(login_timeout=1.0, command_timeout=1.0, idle_timeout=5.0, idle_logout=60.0, other_session_wait=0.15,
                    rate_pause=0.1, chunk=100)


@pytest.fixture
def server():
    fake = StreamerFakeServer().start()
    stream.allow_local_for_tests(True)
    yield fake
    stream.allow_local_for_tests(False)
    fake.stop()


def preference(url: str) -> dict:
    body = load("user_preference.json")
    info = body[0]["streamerInfo"][0]
    info.update({"streamerSocketUrl": url, "schwabClientCustomerId": CUSTOMER, "schwabClientCorrelId": CORREL,
                 "schwabClientChannel": CHANNEL, "schwabClientFunctionId": FUNCTION})
    return body


class Harness:
    """The streamer, the fake Schwab REST that serves `userPreference`, and everything the streamer reported."""

    def __init__(self, server: StreamerFakeServer, config: StreamConfig = FAST, url: str | None = None) -> None:
        self.server = server
        self.fake = FakeSchwab()
        self.fake.on("GET", "/trader/v1/userPreference", preference(url or server.url))
        self.data: list[tuple] = []
        self.statuses: list[tuple] = []
        self.limits: list[tuple] = []
        self.unavailable: list[str] = []
        self.streamer = SchwabStreamer(
            config, on_data=lambda *a: self.data.append(a), on_status=lambda *a: self.statuses.append(a),
            on_limit=lambda s, k: self.limits.append((s, tuple(k))), on_unavailable=self.unavailable.append, backoff=BACKOFF)

    async def __aenter__(self) -> "Harness":
        self.fake.__enter__()
        await self.streamer.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.streamer.stop()
        self.fake.__exit__()

    async def until(self, predicate, timeout: float = 5.0) -> None:
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"not reached; statuses {self.statuses}; commands {self.server.log}")

    def status_codes(self) -> list[str]:
        return [f"{s}:{c}" for s, c, _ in self.statuses]


def run(coroutine) -> None:
    asyncio.run(coroutine)


# ------------------------------------------------------------------ the conversation


def test_login_comes_first_with_the_token_without_bearer_and_nothing_else_leaves_before_its_answer(server):
    async def go():
        async with Harness(server) as h:
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            await h.until(lambda: server.subscribed.get(LEVELONE_EQUITIES) == {"QQQ"})
    run(go())
    assert server.log[0] == "ADMIN LOGIN" and server.log[1] == "LEVELONE_EQUITIES ADD"
    assert server.logins == [FAKE_TOKEN]                                    # no "Bearer " prefix
    assert {c["customer"] for c in server.received} == {CUSTOMER} and {c["correl"] for c in server.received} == {CORREL}


def test_a_subscription_asked_before_the_socket_is_open_goes_after_the_login(server):
    async def go():
        async with Harness(server) as h:
            pass
        h = Harness(server)
        h.streamer.want(LEVELONE_EQUITIES, ["SPY", "QQQ"])
        async with h:
            await h.until(lambda: server.subscribed.get(LEVELONE_EQUITIES) == {"SPY", "QQQ"})
    run(go())
    assert server.commands(LEVELONE_EQUITIES, "ADD")[0]["keys"] == "QQQ,SPY"
    assert server.commands(LEVELONE_EQUITIES, "ADD")[0]["fields"] == stream.FIELDS[LEVELONE_EQUITIES]


def test_commands_go_one_at_a_time_each_waiting_for_its_answer():
    fake = StreamerFakeServer(answer_delay=0.03).start()
    stream.allow_local_for_tests(True)

    async def go():
        async with Harness(fake) as h:
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            h.streamer.want(LEVELONE_OPTIONS, ["QQQ   261224C00500000"])
            h.streamer.want(CHART_EQUITY, ["QQQ"])
            await h.until(lambda: sum(len(v) for v in fake.subscribed.values()) == 3)
    try:
        run(go())
    finally:
        stream.allow_local_for_tests(False)
        fake.stop()
    assert fake.max_inflight == 1


def test_it_adds_and_removes_with_ADD_and_UNSUBS_and_changes_the_fields_with_VIEW(server):
    async def go():
        async with Harness(server) as h:
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            await h.until(lambda: server.subscribed.get(LEVELONE_EQUITIES) == {"QQQ"})
            h.streamer.want(LEVELONE_EQUITIES, ["SPY"])
            await h.until(lambda: server.subscribed.get(LEVELONE_EQUITIES) == {"QQQ", "SPY"})
            h.streamer.drop(LEVELONE_EQUITIES, ["QQQ"])
            await h.until(lambda: server.subscribed.get(LEVELONE_EQUITIES) == {"SPY"})
            h.streamer.view(LEVELONE_EQUITIES, "0,1,2,3")
            await h.until(lambda: server.fields.get(LEVELONE_EQUITIES) == "0,1,2,3")
    run(go())
    assert [c["command"] for c in server.commands(LEVELONE_EQUITIES)] == ["ADD", "ADD", "UNSUBS", "VIEW"]


# ------------------------------------------------------------------ Change services keep the last value of each field


def test_a_change_message_carries_only_what_changed_and_the_state_is_the_last_value_of_every_field(server):
    async def go():
        async with Harness(server) as h:
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            await h.until(lambda: server.subscribed.get(LEVELONE_EQUITIES) == {"QQQ"})
            server.data(LEVELONE_EQUITIES, [{"key": "QQQ", "1": 500.1, "2": 500.2, "3": 500.15, "8": 1000}])
            await h.until(lambda: len(h.data) == 1)
            server.data(LEVELONE_EQUITIES, [{"key": "QQQ", "3": 500.3}])
            await h.until(lambda: len(h.data) == 2)
            first, second = h.data[0], h.data[1]
            assert first[0] == LEVELONE_EQUITIES and first[1] == "QQQ" and first[3] == {"1", "2", "3", "8"}
            assert second[3] == {"3"}                                       # only the field that changed arrived
            assert second[2]["1"] == 500.1 and second[2]["2"] == 500.2 and second[2]["3"] == 500.3 and second[2]["8"] == 1000
            assert first[4] is False and second[4] is False                 # `delayed` rides on every message
    run(go())


# ------------------------------------------------------------------ limits and codes


def test_code_19_marks_the_keys_that_did_not_enter_and_never_retries_them_in_a_loop():
    fake = StreamerFakeServer(max_symbols=2).start()
    stream.allow_local_for_tests(True)

    async def go():
        async with Harness(fake) as h:
            h.streamer.want(LEVELONE_EQUITIES, ["A", "B"])
            await h.until(lambda: fake.subscribed.get(LEVELONE_EQUITIES) == {"A", "B"})
            h.streamer.want(LEVELONE_EQUITIES, ["C"])
            await h.until(lambda: h.limits == [(LEVELONE_EQUITIES, ("C",))])
            await asyncio.sleep(0.2)
            assert len(fake.commands(LEVELONE_EQUITIES, "ADD")) == 2         # the third ADD was not tried again
            assert h.streamer.wanted(LEVELONE_EQUITIES) == {"A", "B"}
    try:
        run(go())
    finally:
        stream.allow_local_for_tests(False)
        fake.stop()


def test_code_11_leaves_that_service_without_live_and_does_not_touch_the_others(server):
    server.unavailable.add(LEVELONE_OPTIONS)

    async def go():
        async with Harness(server) as h:
            h.streamer.want(LEVELONE_OPTIONS, ["QQQ   261224C00500000"])
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            await h.until(lambda: h.unavailable == [LEVELONE_OPTIONS])
            await h.until(lambda: server.subscribed.get(LEVELONE_EQUITIES) == {"QQQ"})
    run(go())


def test_a_failed_command_code_22_to_25_is_tried_once_more(server):
    server.fail_next = [22]

    async def go():
        async with Harness(server) as h:
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            await h.until(lambda: server.subscribed.get(LEVELONE_EQUITIES) == {"QQQ"})
    run(go())
    assert len(server.commands(LEVELONE_EQUITIES, "ADD")) == 2


# ------------------------------------------------------------------ reconnection


def test_after_a_drop_it_logs_in_again_replays_every_subscription_and_forgets_the_field_state(server):
    async def go():
        async with Harness(server) as h:
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            h.streamer.want(CHART_EQUITY, ["QQQ"])
            await h.until(lambda: server.subscribed.get(CHART_EQUITY) == {"QQQ"})
            server.data(LEVELONE_EQUITIES, [{"key": "QQQ", "1": 500.1}])
            await h.until(lambda: len(h.data) == 1)
            server.subscribed.clear()
            server.close_clients(1011)
            await h.until(lambda: "reconnecting:network" in h.status_codes() or "reconnecting:closed" in h.status_codes())
            await h.until(lambda: server.attempts == 2 and server.subscribed.get(CHART_EQUITY) == {"QQQ"}
                          and server.subscribed.get(LEVELONE_EQUITIES) == {"QQQ"})
            assert h.streamer.last(LEVELONE_EQUITIES, "QQQ") == {}           # a new session starts without old values
            codes = h.status_codes()
            assert "live:connected" in codes[codes.index(next(c for c in codes if c.startswith("reconnecting"))):]
    run(go())
    assert server.log.count("ADMIN LOGIN") == 2


def test_code_12_another_streamer_connection_waits_before_trying_again(server):
    async def go():
        async with Harness(server) as h:
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            await h.until(lambda: server.subscribed.get(LEVELONE_EQUITIES) == {"QQQ"})
            first = asyncio.get_running_loop().time()
            server.respond("ADMIN", "LOGIN", 12, "max connections")
            await h.until(lambda: "reconnecting:other_session" in h.status_codes())
            await h.until(lambda: server.attempts == 2)
            assert asyncio.get_running_loop().time() - first >= FAST.other_session_wait * 0.9
    run(go())


def test_no_heartbeat_and_no_data_for_the_idle_time_closes_and_reconnects():
    config = dataclasses.replace(FAST, idle_timeout=0.3)
    fake = StreamerFakeServer().start()
    stream.allow_local_for_tests(True)

    async def go():
        async with Harness(fake, config) as h:
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            await h.until(lambda: fake.subscribed.get(LEVELONE_EQUITIES) == {"QQQ"})
            await h.until(lambda: "reconnecting:timeout" in h.status_codes(), timeout=3)
    try:
        run(go())
    finally:
        stream.allow_local_for_tests(False)
        fake.stop()


def test_a_heartbeat_keeps_the_connection_and_is_counted_for_the_measuring_tool():
    config = dataclasses.replace(FAST, idle_timeout=1.5)          # 15 beats of margin: a stalled runner does not look like a silent line
    fake = StreamerFakeServer().start()
    stream.allow_local_for_tests(True)

    async def go():
        async with Harness(fake, config) as h:
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            await h.until(lambda: fake.subscribed.get(LEVELONE_EQUITIES) == {"QQQ"})
            for _ in range(8):
                fake.heartbeat()
                await asyncio.sleep(0.1)
            assert "reconnecting:timeout" not in h.status_codes()
            assert h.streamer.stats["heartbeats"] >= 7 and h.streamer.stats["heartbeat_gaps_ms"]
    try:
        run(go())
    finally:
        stream.allow_local_for_tests(False)
        fake.stop()


def test_with_nothing_subscribed_it_says_goodbye_after_the_idle_time_and_reopens_on_demand():
    config = dataclasses.replace(FAST, idle_logout=0.3)
    fake = StreamerFakeServer().start()
    stream.allow_local_for_tests(True)

    async def go():
        async with Harness(fake, config) as h:
            await h.until(lambda: "idle:idle" in h.status_codes(), timeout=3)
            assert fake.log == ["ADMIN LOGIN", "ADMIN LOGOUT"]
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            await h.until(lambda: fake.subscribed.get(LEVELONE_EQUITIES) == {"QQQ"})
            assert fake.attempts == 2
    try:
        run(go())
    finally:
        stream.allow_local_for_tests(False)
        fake.stop()


# ------------------------------------------------------------------ the token


def test_a_denied_login_asks_for_a_new_token_and_logs_in_once_more():
    fake = StreamerFakeServer(tokens=("fresh-token-0123456789",)).start()
    stream.allow_local_for_tests(True)
    tokens = iter(["stale-token-0123456789"])

    async def go():
        h = Harness(fake)
        h.fake.__enter__()
        http.set_token_provider(lambda: "stale-token-0123456789", lambda: "fresh-token-0123456789")
        h.fake.__exit__ = lambda *a: None
        await h.streamer.start()
        try:
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            await h.until(lambda: fake.subscribed.get(LEVELONE_EQUITIES) == {"QQQ"})
        finally:
            await h.streamer.stop()
            http.set_base(None)
            http.set_token_provider(None)
    try:
        run(go())
    finally:
        stream.allow_local_for_tests(False)
        fake.stop()
    assert fake.logins == ["stale-token-0123456789", "fresh-token-0123456789"]


def test_a_second_denial_ends_the_source_as_expired():
    fake = StreamerFakeServer(tokens=()).start()
    stream.allow_local_for_tests(True)

    async def go():
        async with Harness(fake) as h:
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            await h.until(lambda: any(s[0] == "down" for s in h.statuses))
            assert h.statuses[-1][:2] == ("down", "connections.schwab_expired")
    try:
        run(go())
    finally:
        stream.allow_local_for_tests(False)
        fake.stop()
    assert len(fake.logins) == 2


# ------------------------------------------------------------------ security


@pytest.mark.parametrize("url", ["wss://evil.example/ws", "ws://streamer-api.schwab.com/ws", "ws://127.0.0.2:9/ws",
                                 "wss://streamer-api.schwab.com.evil.example/ws", "https://streamer-api.schwab.com/ws"])
def test_an_address_that_is_not_a_listed_wss_host_never_receives_the_token(server, url):
    async def go():
        async with Harness(server, url=url) as h:
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            await h.until(lambda: any(s[0] == "down" for s in h.statuses))
            assert h.statuses[-1][:2] == ("down", "connections.schwab_denied")
    run(go())
    assert server.attempts == 0 and server.logins == []


def test_the_listed_schwab_host_passes_the_address_check():
    stream.allow_local_for_tests(False)
    stream.check_address("wss://streamer-api.schwab.com/ws")
    with pytest.raises(Exception):
        stream.check_address("wss://streamer-api.schwab.com.evil.example/ws")


def test_neither_the_token_nor_the_ids_appear_in_logs_statuses_or_repr(server, caplog):
    caplog.set_level(logging.DEBUG)
    secrets = [FAKE_TOKEN, CUSTOMER, CORREL]

    async def go():
        async with Harness(server) as h:
            h.streamer.want(LEVELONE_EQUITIES, ["QQQ"])
            await h.until(lambda: server.subscribed.get(LEVELONE_EQUITIES) == {"QQQ"})
            server.respond("ADMIN", "LOGIN", 12, "max connections")
            await h.until(lambda: "reconnecting:other_session" in h.status_codes())
            await h.until(lambda: server.attempts == 2)
            text = repr(h.streamer) + repr(h.statuses) + repr(h.data) + repr(h.limits)
            assert not any(s in text for s in secrets)
    run(go())
    everything = "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records)
    assert not any(s in everything for s in secrets), "a secret reached a log line"

"""The hub with ticks: the tick tape, seconds bars and range bars (exact and approximate) over `/ws/market`, and the
linger of the tape. The provider is the fake futures server of `futures_fake_server.py`; the history comes from the
recorded Yahoo answers. Nothing touches the network.

Tests T-6 (the hub half), T-7, T-8 and T-9 of the chart addendum."""
import json
import time

import pytest

from hokma.connections import runtime
from live_fixtures import Collector, live  # noqa: F401  (fixture)
from live_support import wait_until
from market_support import RECORDED_AT, fixture_text
from futures_fake_server import VALID_TOKEN, FuturesFakeServer
from test_futures_adapter import card, save_token
from test_live_market import subscribe

T0 = int(RECORDED_AT * 1000)
T0S = T0 // 1000


@pytest.fixture
def server():
    fake = FuturesFakeServer().start()
    yield fake
    fake.stop()


def open_nq(live, server, client):
    """The futures provider switched on with its key and connected, so NQ=F is served with ticks."""
    live.use({"siomtrading_futures": server.url})
    save_token(client)
    assert wait_until(lambda: card(client)["state"] == "connected")


def with_opening_window(live) -> None:
    """The recorded 1-minute answer of NQ=F starts after the New York open; shifted nine hours back it holds the
    whole 09:30 window of the day, so the opening line exists on 1-minute bars."""
    body = json.loads(fixture_text("nqf_1m.json"))
    result = body["chart"]["result"][0]
    result["timestamp"] = [t - 9 * 3600 for t in result["timestamp"]]
    live.fetcher.overrides["NQ=F"] = (200, json.dumps(body))


def feed_of(symbol: str):
    return runtime.get_hub()._feeds[symbol]


# ------------------------------------------------------------------ seconds (T-8)


def test_t8_seconds_are_built_from_the_tape_and_the_snapshot_says_where_they_start(live, server):
    live.use({"siomtrading_futures": server.url})
    with live.app() as client, live.connect(client) as ws:
        save_token(client)
        c = Collector(ws)
        first = subscribe(c, "s1", "NQ=F", "15s")
        assert first["type"] == "snapshot" and first["interval"] == {"kind": "time", "seconds": 15}
        assert first["bars"] == [] and first["exact_from"] is None            # no trade yet: nothing to draw
        assert wait_until(lambda: server.symbols() == {"NQ"})
        base = T0S // 15 * 15
        for offset, price in ((1, 28300.0), (5, 28302.0), (11, 28299.0), (16, 28301.0), (20, 28305.0)):
            server.tick("NQ", price, (base + offset) * 1000, size=2)
        # the first trade tells the snapshot where the exact bars begin: a new snapshot arrives for the seconds chart
        again = c.find(lambda m: m["type"] == "snapshot" and m["sub"] == "s1" and m["seq"] > first["seq"] and m["exact_from"] == base + 1)
        assert again["interval"]["seconds"] == 15
        assert wait_until(lambda: len(feed_of("NQ=F").tape) == 5)
        second = subscribe(c, "s2", "NQ=F", "15s")
        assert second["exact_from"] == base + 1 and second["approx_until"] is None
        assert [(b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in second["bars"]] == [
            (base, 28300.0, 28302.0, 28299.0, 28299.0, 6.0),
            (base + 15, 28301.0, 28305.0, 28301.0, 28305.0, 4.0)]
        server.tick("NQ", 28306.0, (base + 21) * 1000, size=1)
        live_bar = c.find(lambda m: m["type"] == "bar" and m["sub"] == "s2" and m["bar"]["c"] == 28306.0)
        assert live_bar["bar"]["t"] == base + 15 and live_bar["bar"]["v"] == 5.0 and live_bar["final"] is False


def test_t8_the_delayed_fallback_cannot_draw_seconds(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        error = subscribe(c, "x", "NQ=F", "15s")
        assert error["type"] == "error" and error["code"] == "market.seconds_need_ticks"
        assert subscribe(c, "y", "NQ=F", "1m")["type"] == "snapshot"           # the socket keeps working


def test_seconds_chart_takes_its_opening_line_from_the_one_minute_bars(live, server):
    with_opening_window(live)
    live.use({"siomtrading_futures": server.url})
    with live.app() as client, live.connect(client) as ws:
        save_token(client)
        c = Collector(ws)
        minutes = subscribe(c, "m", "NQ=F", "1m")
        seconds = subscribe(c, "s", "NQ=F", "30s")
        assert seconds["opening_range"] == minutes["opening_range"] and seconds["opening_range"] is not None


# ------------------------------------------------------------------ range bars (T-6, T-7)


def test_t7_range_without_ticks_is_all_approximate_and_says_so(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        snapshot = subscribe(c, "r", "NQ=F", "10r")
        assert snapshot["type"] == "snapshot" and snapshot["fallback"] is True
        assert snapshot["tf"] == "r:10t"
        assert snapshot["interval"] == {"kind": "range", "size": 10, "unit": "t", "tick_size": 0.25}
        assert snapshot["exact_from"] is None and snapshot["approx_until"] is None and snapshot["tick_size"] == 0.25
        bars = snapshot["bars"]
        assert len(bars) > 20 and all(b["approx"] is True for b in bars)
        assert [b["i"] for b in bars] == list(range(bars[0]["i"], bars[0]["i"] + len(bars)))
        assert all(round(b["h"] - b["l"], 4) <= 2.5 for b in bars)             # 10 ticks of 0.25
        times = [b["t"] for b in bars]
        assert times == sorted(times)


def test_range_without_ticks_does_not_resend_the_snapshot_while_the_minute_bars_do_not_change(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        subscribe(c, "r", "NQ=F", "10r")
        time.sleep(1.2)                                       # six polls of the fallback at the test interval
        assert len(c.of("snapshot", "r")) == 1


def test_t7_range_with_ticks_is_approximate_before_the_first_trade_and_exact_after(live, server):
    live.use({"siomtrading_futures": server.url})
    with live.app() as client, live.connect(client) as ws:
        save_token(client)
        c = Collector(ws)
        first = subscribe(c, "r", "NQ=F", "r:10t")
        assert first["fallback"] is False and first["exact_from"] is None and all(b["approx"] for b in first["bars"])
        assert wait_until(lambda: server.symbols() == {"NQ"})
        ts = T0 + 3000
        server.tick("NQ", 28300.0, ts, size=1)
        # the first trade tells the chart where the exact bars begin: a new snapshot for the range chart
        c.find(lambda m: m["type"] == "snapshot" and m["sub"] == "r" and m["seq"] > first["seq"] and m["exact_from"] == ts // 1000)
        for n, price in enumerate((28301.0, 28302.5, 28303.0), start=1):         # R = 2.5: the third tick is on the edge
            server.tick("NQ", price, ts + n * 100, size=1)
        assert wait_until(lambda: len(feed_of("NQ=F").tape) == 4)
        snapshot = subscribe(c, "q", "NQ=F", "r:10t")
        assert snapshot["exact_from"] == ts // 1000 and snapshot["approx_until"] == ts // 1000
        bars = snapshot["bars"]
        exact = [b for b in bars if not b["approx"]]
        assert len(exact) == 2 and bars[-2:] == exact and all(b["approx"] for b in bars[:-2])
        assert [b["i"] for b in bars] == list(range(bars[0]["i"], bars[0]["i"] + len(bars)))
        # by hand, rule 3 and 4: the bar closes on its edge and the next one opens one tick beyond it
        assert (exact[0]["o"], exact[0]["h"], exact[0]["l"], exact[0]["c"]) == (28300.0, 28302.5, 28300.0, 28302.5)
        assert (exact[1]["o"], exact[1]["h"], exact[1]["l"], exact[1]["c"]) == (28302.75, 28303.0, 28302.75, 28303.0)
        assert exact[0]["v"] == 3.0 and exact[1]["v"] == 1.0


def test_t6_ticks_make_closed_bars_final_with_their_index_and_a_jump_makes_the_intermediate_bars(live, server):
    live.use({"siomtrading_futures": server.url})
    with live.app() as client, live.connect(client) as ws:
        save_token(client)
        c = Collector(ws)
        first = subscribe(c, "r", "NQ=F", "r:10t")
        assert wait_until(lambda: server.symbols() == {"NQ"})
        ts = T0 + 3000
        server.tick("NQ", 28300.0, ts, size=3)
        again = c.find(lambda m: m["type"] == "snapshot" and m["sub"] == "r" and m["seq"] > first["seq"] and m["exact_from"] == ts // 1000)
        opened = again["bars"][-1]["i"]                       # the exact bar that the first trade opened
        assert again["bars"][-1]["approx"] is False and again["bars"][-1]["o"] == 28300.0
        server.tick("NQ", 28308.0, ts + 100, size=4)             # a jump of more than two ranges
        done = c.find(lambda m: m["type"] == "bar" and m["sub"] == "r" and m["bar"]["c"] == 28308.0 and not m["final"])
        finals = [m for m in c.of("bar", "r") if m["final"]]
        assert [m["bar"]["i"] for m in finals] == [opened, opened + 1]
        assert [(m["bar"]["o"], m["bar"]["h"], m["bar"]["l"], m["bar"]["c"], m["bar"]["v"]) for m in finals] == [
            (28300.0, 28302.5, 28300.0, 28302.5, 3.0),             # closed exactly on its edge, with the volume it had
            (28302.75, 28305.25, 28302.75, 28305.25, 0.0)]         # the intermediate bar: a complete range, no volume
        assert (done["bar"]["o"], done["bar"]["h"], done["bar"]["l"]) == (28305.5, 28308.0, 28305.5)
        assert done["bar"]["i"] == opened + 2 and done["bar"]["approx"] is False
        assert done["bar"]["v"] == 4.0 and done["price"] == 28308.0 and done["final"] is False


def test_a_price_range_that_is_not_a_multiple_of_the_tick_is_rounded_and_says_so(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        snapshot = subscribe(c, "r", "NQ=F", "r:2.6p")
        assert snapshot["interval"] == {"kind": "range", "size": 2.5, "unit": "p", "tick_size": 0.25, "rounded": True}
        exact = subscribe(c, "q", "NQ=F", "r:2.5p")
        assert exact["interval"]["size"] == 2.5 and "rounded" not in exact["interval"]


def test_range_in_ticks_needs_a_known_tick_size(live):
    live.fetcher.overrides["ZB=F"] = (200, fixture_text("nqf_1m.json"))
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        refused = subscribe(c, "r", "ZB=F", "10r")
        assert refused["type"] == "error" and refused["code"] == "market.bad_request"
        assert refused["message"] == "This asset has no known tick size: use a price range, for example 2.5rp"
        priced = subscribe(c, "p", "ZB=F", "2.5rp")
        assert priced["type"] == "snapshot" and priced["interval"]["tick_size"] is None


def test_the_opening_line_of_a_range_chart_is_the_one_of_the_one_minute_bars(live):
    with_opening_window(live)
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        minutes = subscribe(c, "m", "NQ=F", "1m")
        ranged = subscribe(c, "r", "NQ=F", "r:10t")
        assert ranged["opening_range"] == minutes["opening_range"] and ranged["opening_range"] is not None


def test_ranges_count_toward_the_twelve_intervals_of_a_symbol(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        for index in range(6):
            assert subscribe(c, f"t{index}", "NQ=F", f"{index + 2}m")["type"] == "snapshot"
        for index in range(6):
            assert subscribe(c, f"r{index}", "NQ=F", f"r:{index + 5}t")["type"] == "snapshot"
        refused = subscribe(c, "extra", "NQ=F", "r:50t")
        assert refused["type"] == "error" and refused["code"] == "market.limit"


# ------------------------------------------------------------------ the tape and its linger (T-9)


def test_t9_changing_interval_and_coming_back_keeps_the_tape_and_does_not_reconnect(live, server):
    live.use({"siomtrading_futures": server.url})
    with live.app() as client, live.connect(client) as ws:
        save_token(client)
        runtime.get_hub().linger_seconds = 2.0
        c = Collector(ws)
        subscribe(c, "a", "NQ=F", "7m")
        assert wait_until(lambda: server.symbols() == {"NQ"})
        subscribes = len(server.messages("subscribe"))
        for n in range(6):
            server.tick("NQ", 28300.0 + n, T0 + 3000 + n * 100)
        assert wait_until(lambda: len(feed_of("NQ=F").tape) == 6)
        c.send({"op": "unsubscribe", "sub": "a"})
        time.sleep(0.3)
        ranged = subscribe(c, "b", "NQ=F", "r:10t")                  # inside the linger: a new interval of the same symbol
        assert ranged["exact_from"] == (T0 + 3000) // 1000
        assert len(feed_of("NQ=F").tape) == 6
        c.send({"op": "unsubscribe", "sub": "b"})
        time.sleep(0.3)
        back = subscribe(c, "c", "NQ=F", "7m")
        assert back["type"] == "snapshot" and len(feed_of("NQ=F").tape) == 6
        assert len(server.messages("subscribe")) == subscribes            # the provider was not asked again
        assert not server.messages("unsubscribe")


def test_t9_after_the_linger_the_tape_is_dropped_and_the_provider_is_told(live, server):
    live.use({"siomtrading_futures": server.url})
    with live.app() as client, live.connect(client) as ws:
        save_token(client)
        runtime.get_hub().linger_seconds = 0.5
        c = Collector(ws)
        subscribe(c, "a", "NQ=F", "7m")
        assert wait_until(lambda: server.symbols() == {"NQ"})
        server.tick("NQ", 28300.0, T0 + 3000)
        assert wait_until(lambda: len(feed_of("NQ=F").tape) == 1)
        c.send({"op": "unsubscribe", "sub": "a"})
        time.sleep(0.2)
        assert "NQ=F" in runtime.get_hub()._feeds and not server.messages("unsubscribe")
        assert wait_until(lambda: server.messages("unsubscribe"), timeout=3)
        assert wait_until(lambda: "NQ=F" not in runtime.get_hub()._feeds)
        assert wait_until(lambda: server.symbols() == set())
        fresh = subscribe(c, "b", "NQ=F", "r:10t")                       # a new tape starts empty
        assert fresh["exact_from"] is None


def test_the_tape_keeps_its_cap_and_time_order(live, server):
    live.use({"siomtrading_futures": server.url})
    with live.app() as client, live.connect(client) as ws:
        save_token(client)
        c = Collector(ws)
        subscribe(c, "a", "NQ=F", "7m")
        assert wait_until(lambda: server.symbols() == {"NQ"})
        server.tick("NQ", 28300.0, T0 + 3000)
        server.tick("NQ", 28301.0, T0 + 2000)                           # arrives late: stored in order
        assert wait_until(lambda: len(feed_of("NQ=F").tape) == 2)
        times = [t for t, _, _ in feed_of("NQ=F").tape]
        assert times == sorted(times)
        assert feed_of("NQ=F").tape.capacity == 500_000

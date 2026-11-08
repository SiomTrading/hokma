"""`/ws/market` with free intervals: what `subscribe.tf` accepts, what the snapshot says about it, the refusals with their
text, and the cap of 12 distinct intervals per symbol. The history comes from the recorded Yahoo answers."""
import json

import pytest

from hokma.i18n import catalog
from live_fixtures import Collector, live  # noqa: F401  (fixture)
from live_support import trade_coinbase, wait_until
from market_support import RECORDED_AT
from test_live_market import coinbase, iso, subscribe, ticks_of  # noqa: F401  (fixture)


def error_for(c: Collector, tf: str, sub: str = "x") -> dict:
    c.send({"op": "subscribe", "sub": sub, "symbol": "NQ=F", "tf": tf})
    return c.next(lambda m: m["type"] == "error" and m.get("sub") == sub)


def test_a_free_interval_gets_a_snapshot_with_its_canonical_text_and_parsed_form(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        snapshot = subscribe(c, "c1", "NQ=F", "07m")
        assert snapshot["type"] == "snapshot" and snapshot["tf"] == "7m"
        assert snapshot["interval"] == {"kind": "time", "seconds": 420}
        assert snapshot["exact_from"] is None and snapshot["approx_until"] is None
        assert snapshot["bars"] and len(snapshot["bars"]) <= 300
        assert all(b["t"] % 60 == 0 for b in snapshot["bars"])


def test_the_original_timeframes_keep_their_snapshot_and_gain_the_new_fields(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        snapshot = subscribe(c, "c1", "NQ=F", "5m")
        assert snapshot["tf"] == "5m" and snapshot["interval"] == {"kind": "time", "seconds": 300}
        assert 0 < len(snapshot["bars"]) <= 180


def test_a_refused_interval_is_a_bad_request_that_says_why(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        invalid = error_for(c, "7 m")
        assert invalid["code"] == "market.bad_request" and invalid["message"] == "Write a number and a unit: 30s, 7m, 3h, 2D, 1W"
        assert invalid["params"] == {} and invalid["sub"] == "x"
        months = error_for(c, "3M", "y")
        assert months["code"] == "market.bad_request" and months["message"] == "Months are not available yet"
        for bad in (5, None, ["5m"], "3601s", "r:0t"):
            c.send({"op": "subscribe", "sub": "z", "symbol": "NQ=F", "tf": bad})
            assert c.next(lambda m: m["type"] == "error")["code"] == "market.bad_request"
        assert subscribe(c, "c1", "NQ=F", "5m")["type"] == "snapshot"                # the socket keeps working


def test_a_refusal_speaks_the_active_language(live, monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "es")
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        message = error_for(c, "3M")["message"]
        assert message == catalog("es")["market.error.interval_months"] != catalog("en")["market.error.interval_months"]


def test_range_bars_are_served_in_their_canonical_form(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        in_ticks = subscribe(c, "x", "NQ=F", "10r")
        assert in_ticks["type"] == "snapshot" and in_ticks["tf"] == "r:10t"
        assert in_ticks["interval"] == {"kind": "range", "size": 10, "unit": "t", "tick_size": 0.25}
        in_price = subscribe(c, "y", "NQ=F", "2.5rp")
        assert in_price["tf"] == "r:2.5p" and in_price["interval"]["size"] == 2.5 and in_price["interval"]["unit"] == "p"
        assert all({"i", "approx"} <= set(bar) for bar in in_ticks["bars"])


def test_seconds_without_a_provider_of_ticks_say_so(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        error = error_for(c, "15s")
        assert error["code"] == "market.seconds_need_ticks" and error["params"] == {}
        assert error["message"] == "This source gives 1-minute bars at best. Connect a live source for seconds"


def test_an_interval_the_source_cannot_build_names_the_base_the_source_and_the_symbol(live):
    live.fetcher.overrides["AAPL"] = (422, "{}")                       # the source has no finer bars for this symbol
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        error = subscribe(c, "c1", "AAPL", "7m")
        assert error["type"] == "error" and error["code"] == "market.interval_unavailable"
        assert error["params"] == {"interval": "7m", "base": "1m", "source": "Yahoo Finance", "symbol": "AAPL"}
        assert error["message"] == "7m needs 1m bars, which Yahoo Finance doesn't have for AAPL"
        assert error["sub"] == "c1" and error["data_state"] == "delayed"


def test_at_most_12_distinct_intervals_are_built_at_once_for_one_symbol(live):
    intervals = [f"{n}m" for n in (2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14)]
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        for index, text in enumerate(intervals):
            assert subscribe(c, f"s{index}", "NQ=F", text)["type"] == "snapshot", text
        again = subscribe(c, "same", "NQ=F", "7m")                    # an interval already built does not count again
        assert again["type"] == "snapshot"
        refused = subscribe(c, "extra", "NQ=F", "17m")
        assert refused["type"] == "error" and refused["code"] == "market.limit" and refused["sub"] == "extra"
        other = subscribe(c, "other", "AAPL", "5m")                  # the cap is per symbol
        assert other["type"] == "snapshot"
        c.send({"op": "unsubscribe", "sub": "s0"})
        c.send({"op": "unsubscribe", "sub": "s1"})
        assert subscribe(c, "extra2", "NQ=F", "17m")["type"] == "snapshot"


def test_an_interval_nobody_watches_any_more_stops_counting_toward_the_cap(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        names = [f"{n}m" for n in range(2, 14)]
        for index, text in enumerate(names):
            subscribe(c, f"s{index}", "NQ=F", text)
        for index in range(12):
            c.send({"op": "unsubscribe", "sub": f"s{index}"})
        for index, text in enumerate(["20m", "21m", "22m"]):
            assert subscribe(c, f"n{index}", "NQ=F", text)["type"] == "snapshot"


def test_live_ticks_build_each_watched_interval_from_the_session_anchor(live, coinbase):
    live.use({"coinbase": coinbase.url})
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        assert subscribe(c, "long", "BTC-USD", "4h")["interval"] == {"kind": "time", "seconds": 14400}
        assert subscribe(c, "hour", "BTC-USD", "1h")["type"] == "snapshot"
        assert wait_until(lambda: ticks_of(coinbase))
        ts = RECORDED_AT * 1000 + 5000
        coinbase.send_all(trade_coinbase("BTC-USD", 80500, iso(ts)))
        long = c.find(lambda m: m["type"] == "bar" and m["sub"] == "long" and m["bar"]["c"] == 80500)
        hour = c.find(lambda m: m["type"] == "bar" and m["sub"] == "hour" and m["bar"]["c"] == 80500)
        assert long["bar"]["t"] == ts // 1000 // 14400 * 14400        # crypto sessions start at 00:00 UTC
        assert hour["bar"]["t"] == ts // 1000 // 3600 * 3600

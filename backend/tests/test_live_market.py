"""The live price channel `/ws/market` against fake providers: M1 to M5, M7, M10, M11, M13, M14 and the failure
paths. Nothing touches the network; each provider is a local server speaking its protocol."""
import copy
import itertools
import json
import time
from datetime import datetime, timezone

import pytest
from starlette.websockets import WebSocketDisconnect

from live_fixtures import AUTH, WS_URL, Collector, live  # noqa: F401  (fixture)
from live_support import (FakeWs, alpaca_script, coinbase_script, finnhub_script, trade_alpaca, trade_coinbase,
                          wait_until)
from market_support import RECORDED_AT, fixture_text

KEY_ID, SECRET = "AKTESTKEYID12345", "alpaca-secret-value-67890"
FINNHUB_KEY = "finnhub-key-value-13579"


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def subscribe(c: Collector, sub: str, symbol: str, tf: str) -> dict:
    c.send({"op": "subscribe", "sub": sub, "symbol": symbol, "tf": tf})
    return c.next(lambda m: m.get("sub") == sub and m["type"] in ("snapshot", "error"))


def save_alpaca(client) -> None:
    assert client.put("/api/connections/alpaca/key", json={"key_id": KEY_ID, "secret": SECRET},
                      headers=AUTH).status_code == 200


def save_finnhub(client) -> None:
    assert client.put("/api/connections/finnhub/key", json={"key": FINNHUB_KEY}, headers=AUTH).status_code == 200


def ticks_of(fake: FakeWs, product: str = "BTC-USD") -> list[dict]:
    return [m for m in fake.messages() if isinstance(m, dict) and m.get("channels") == ["ticker", "matches"]
            and product in m.get("product_ids", [])]


@pytest.fixture
def coinbase():
    fake = FakeWs(coinbase_script).start()
    yield fake
    fake.stop()


# ------------------------------------------------------------------ M1, M2, M5


def test_m1_snapshot_then_bars_with_growing_seq_and_no_polling_afterwards(live, coinbase):
    live.use({"coinbase": coinbase.url})
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        snapshot = subscribe(c, "c1", "BTC-USD", "1h")
        assert snapshot["type"] == "snapshot" and snapshot["seq"] == 1
        assert snapshot["source"] == "Coinbase" and snapshot["data_state"] == "live" and snapshot["epoch"]
        assert snapshot["bars"] and snapshot["fallback"] is False and snapshot["asset_class"] == "crypto"
        assert wait_until(lambda: ticks_of(coinbase))
        history_calls = len(live.fetcher.calls)
        base_ms = int(RECORDED_AT * 1000) + 5000
        sent: list[tuple[int, float]] = []
        for i in range(24):
            ts = base_ms + i * 350
            sent.append((ts, time.monotonic()))
            coinbase.send_all(trade_coinbase("BTC-USD", 80500 + i, iso(ts)))
            time.sleep(0.35)
        c.drain(0.5)
        live_messages = [m for m in c.all if m["type"] in ("bar", "last") and m.get("sub") == "c1"]
        assert live_messages
        seqs = [m["seq"] for m in c.all if m.get("sub") == "c1"]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        assert all(m["source"] == "Coinbase" and m["data_state"] == "live" and m["epoch"] == snapshot["epoch"]
                   for m in live_messages)
        latencies = []
        for ts, at in sent:
            arrived = next(m["_at"] for m in live_messages if m["ts_ms"] >= ts and m["_at"] >= at)
            latencies.append(arrived - at)
        latencies.sort()
        assert latencies[int(len(latencies) * 0.95) - 1] <= 0.25
        assert len(live.fetcher.calls) == history_calls                 # nothing asked the history source again
        assert live_messages[-1]["price"] == 80523


def test_m2_two_tabs_on_one_symbol_are_one_subscription_upstream(live, coinbase):
    live.use({"coinbase": coinbase.url})
    with live.app() as client:
        with live.connect(client) as first, live.connect(client) as second:
            a, b = Collector(first), Collector(second)
            subscribe(a, "c1", "BTC-USD", "1h")
            subscribe(b, "c1", "BTC-USD", "1h")
            assert wait_until(lambda: ticks_of(coinbase))
            assert len(ticks_of(coinbase)) == 1
            a.send({"op": "unsubscribe", "sub": "c1"})
            time.sleep(0.4)
            assert not [m for m in coinbase.messages() if m.get("type") == "unsubscribe"]
            b.send({"op": "unsubscribe", "sub": "c1"})
            assert wait_until(lambda: any(m.get("type") == "unsubscribe" and "BTC-USD" in m["product_ids"]
                                          for m in coinbase.messages()))


def test_m2_closing_the_socket_releases_the_subscription(live, coinbase):
    live.use({"coinbase": coinbase.url})
    with live.app() as client:
        with live.connect(client) as ws:
            subscribe(Collector(ws), "c1", "BTC-USD", "1h")
            assert wait_until(lambda: ticks_of(coinbase))
        assert wait_until(lambda: any(m.get("type") == "unsubscribe" for m in coinbase.messages()))


def test_m5_a_hundred_ticks_a_second_reach_the_panel_at_four_messages_a_second_with_the_last_value(live, coinbase):
    live.use({"coinbase": coinbase.url})
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        subscribe(c, "c1", "BTC-USD", "1h")
        assert wait_until(lambda: ticks_of(coinbase))
        c.drain(0.5)
        start = len(c.all)
        base_ms = int(RECORDED_AT * 1000) + 5000
        for i in range(300):
            coinbase.send_all(trade_coinbase("BTC-USD", 81000 + i, iso(base_ms + i * 10)))
            time.sleep(0.01)
        c.drain(0.6)
        live_messages = [m for m in c.all[start:] if m["type"] in ("bar", "last")]
        assert live_messages[-1]["price"] == 81299                       # the last value always arrives
        for i in range(len(live_messages) - 4):
            assert live_messages[i + 4]["_at"] - live_messages[i]["_at"] >= 0.96     # never 5 inside one second
        assert len(live_messages) <= 4 * 5 + 2


# ------------------------------------------------------------------ M3, M4


def test_m3_a_provider_that_drops_and_returns_shows_reconnecting_then_live_without_duplicate_bars(live, coinbase):
    live.use({"coinbase": coinbase.url})
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        first = subscribe(c, "c1", "BTC-USD", "1h")
        assert wait_until(lambda: ticks_of(coinbase))
        coinbase.send_all(trade_coinbase("BTC-USD", 80600, iso(int(RECORDED_AT * 1000) + 6000)))
        c.next(lambda m: m["type"] == "bar")
        coinbase.stop()
        state = c.next(lambda m: m["type"] == "status" and m["state"] == "reconnecting")
        assert state["data_state"] == "live" and state["sub"] == "c1"
        assert c.find(lambda m: m["type"] == "connection_status" and m["provider"] == "coinbase"
                      and m["state"] == "reconnecting")
        mark = len(c.all)
        coinbase.restart()
        again = c.find(lambda m: m["type"] == "snapshot" and m["seq"] > first["seq"], after=mark)
        c.find(lambda m: m["type"] == "connection_status" and m["provider"] == "coinbase" and m["state"] == "connected",
               after=mark)
        times = [bar["t"] for bar in again["bars"]]
        assert times == sorted(set(times))                               # no duplicate and no unordered bar
        assert wait_until(lambda: len(ticks_of(coinbase)) >= 1)           # the symbol was subscribed again
        coinbase.send_all(trade_coinbase("BTC-USD", 80650, iso(int(RECORDED_AT * 1000) + 9000)))
        bar = c.next(lambda m: m["type"] == "bar" and m["seq"] > again["seq"])
        assert bar["source"] == "Coinbase" and bar["data_state"] == "live"


def test_m4_a_new_engine_run_has_a_new_epoch_and_starts_its_sequence_again(live, coinbase):
    live.use({"coinbase": coinbase.url})
    epochs = []
    for _ in range(2):
        live.use({"coinbase": coinbase.url})                              # a fresh hub, as after a restart
        with live.app() as client, live.connect(client) as ws:
            c = Collector(ws)
            snapshot = subscribe(c, "c1", "BTC-USD", "1h")
            assert snapshot["seq"] == 1
            epochs.append(snapshot["epoch"])
    assert epochs[0] != epochs[1]


def test_ping_gets_a_pong_and_bad_requests_get_a_catalog_error(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        c.send({"op": "ping"})
        assert c.next(lambda m: m["type"] == "pong")["epoch"]
        for bad in ({"op": "subscribe", "sub": "x", "symbol": "NQ=F", "tf": "9x"},
                    {"op": "subscribe", "sub": "bad sub!", "symbol": "NQ=F", "tf": "5m"},
                    {"op": "subscribe", "sub": "x", "symbol": "../etc", "tf": "5m"}, {"op": "nope"}):
            c.send(bad)
            assert c.next(lambda m: m["type"] == "error")["code"] == "market.bad_request"
        ws.send_text("not json")
        assert c.next(lambda m: m["type"] == "error")["code"] == "market.bad_request"


def test_an_unknown_symbol_is_an_error_and_the_socket_keeps_working(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        error = subscribe(c, "c1", "ZZZZ", "5m")
        assert error["type"] == "error" and error["code"] == "market.unknown_symbol" and error["sub"] == "c1"
        assert error["message"] and error["data_state"] == "delayed" and error["source"]
        assert subscribe(c, "c2", "AAPL", "5m")["type"] == "snapshot"


# ------------------------------------------------------------------ M7, M10


def bump_last_close(text: str, delta: float) -> str:
    data = json.loads(text)
    quote = data["chart"]["result"][0]["indicators"]["quote"][0]
    quote["close"][-1] += delta
    quote["high"][-1] = max(quote["high"][-1], quote["close"][-1])
    return json.dumps(data)


def test_m7_the_delayed_fallback_arrives_over_the_socket_labelled_delayed(live):
    live.use()
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        snapshot = subscribe(c, "c1", "NQ=F", "1m")
        assert snapshot["fallback"] is True and snapshot["data_state"] == "delayed"
        assert snapshot["source"] == "Yahoo Finance" and snapshot["asset_class"] == "future"
        assert snapshot["delay_seconds"] == 600
        status = c.next(lambda m: m["type"] == "status" and m.get("sub") == "c1")
        assert status["state"] == "delayed"
        c.drain(0.6)
        last_close = snapshot["bars"][-1]["c"]
        live.fetcher.overrides["NQ=F"] = (200, bump_last_close(fixture_text("nqf_1m.json"), 7.25))
        live.yahoo_now += 120                                            # the cached answer is now old
        bar = c.next(lambda m: m["type"] == "bar" and m["bar"]["c"] == last_close + 7.25, timeout=6)
        assert bar["data_state"] == "delayed" and bar["source"] == "Yahoo Finance"


def test_m7_a_closed_market_is_reported_as_closed(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        subscribe(c, "c1", "AAPL", "5m")
        status = c.next(lambda m: m["type"] == "status" and m.get("sub") == "c1")
        assert status["state"] == "closed" and status["code"] == "market.closed" and status["data_state"] == "delayed"


def test_m10_routing_and_labels(live, coinbase):
    alpaca = FakeWs(alpaca_script(KEY_ID, SECRET)).start()
    try:
        live.use({"coinbase": coinbase.url, "alpaca": alpaca.url})
        with live.app() as client, live.connect(client) as ws:
            c = Collector(ws)
            crypto = subscribe(c, "c1", "BTC-USD", "1h")
            assert (crypto["source"], crypto["data_state"], crypto["fallback"]) == ("Coinbase", "live", False)
            stock = subscribe(c, "c2", "AAPL", "5m")                      # no Alpaca key yet
            assert (stock["source"], stock["data_state"], stock["fallback"]) == ("Yahoo Finance", "delayed", True)
            assert stock["asset_class"] == "equity"
            future = subscribe(c, "c3", "NQ=F", "5m")                     # no futures provider at all
            assert (future["data_state"], future["fallback"], future["asset_class"]) == ("delayed", True, "future")
            index = subscribe(c, "c4", "AAPL", "1D")
            assert index["data_state"] == "delayed"
            save_alpaca(client)
            other = subscribe(c, "c5", "AAPL", "5m")                      # the feed of AAPL stays where it is
            assert other["fallback"] is True
            c.send({"op": "unsubscribe", "sub": "c2"})
            c.send({"op": "unsubscribe", "sub": "c4"})
            c.send({"op": "unsubscribe", "sub": "c5"})
            time.sleep(0.3)
            stock = subscribe(c, "c6", "AAPL", "5m")
            assert (stock["source"], stock["data_state"], stock["fallback"]) == ("Alpaca IEX", "live", False)
            assert wait_until(lambda: any(m.get("action") == "subscribe" and m.get("trades") == ["AAPL"]
                                          for m in alpaca.messages()))
            alpaca.send_all(trade_alpaca("AAPL", 351.25, iso(int(RECORDED_AT * 1000) + 1000)))
            bar = c.next(lambda m: m["type"] == "bar" and m.get("sub") == "c6")
            assert bar["data_state"] == "live" and bar["source"] == "Alpaca IEX"
            # nothing labelled delayed is ever labelled live: every message carries its own pair
            assert all(m.get("data_state") in ("live", "delayed") and m.get("source") for m in c.all
                       if m["type"] != "pong")
    finally:
        alpaca.stop()


def test_switching_a_provider_off_moves_its_open_subscriptions_to_the_delayed_fallback(live, coinbase):
    live.fetcher.overrides["BTC-USD"] = (200, fixture_text("btc_1h.json"))
    live.use({"coinbase": coinbase.url})
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        assert subscribe(c, "c1", "BTC-USD", "1h")["source"] == "Coinbase"
        assert client.put("/api/connections/coinbase/enabled", json={"enabled": False}, headers=AUTH).status_code == 200
        moved = c.find(lambda m: m["type"] == "snapshot" and m["seq"] > 2)
        assert (moved["source"], moved["data_state"], moved["fallback"]) == ("Yahoo Finance", "delayed", True)
        assert wait_until(lambda: any(m.get("type") == "unsubscribe" for m in coinbase.messages()) or not coinbase.clients)


# ------------------------------------------------------------------ M11, M13, M14


def many_symbols(count: int) -> list[str]:
    return ["".join(p) for p in itertools.islice(itertools.product("ABCDEFGH", repeat=3), count)]


def serve_every_stock_like_aapl(live, symbols: list[str]) -> None:
    body = fixture_text("aapl_5m.json")
    for symbol in symbols:
        live.fetcher.overrides[symbol] = (200, body)


def test_m11_the_31st_stock_on_alpaca_is_a_limit_error_and_the_other_30_keep_working(live):
    alpaca = FakeWs(alpaca_script(KEY_ID, SECRET)).start()
    try:
        symbols = many_symbols(31)
        serve_every_stock_like_aapl(live, symbols)
        live.use({"alpaca": alpaca.url})
        with live.app() as client, live.connect(client) as ws:
            save_alpaca(client)
            c = Collector(ws)
            results = [subscribe(c, f"s{i}", symbol, "5m") for i, symbol in enumerate(symbols)]
            assert [r["type"] for r in results[:30]] == ["snapshot"] * 30
            assert all(r["source"] == "Alpaca IEX" for r in results[:30])
            assert results[30]["type"] == "error" and results[30]["code"] == "market.limit"
            card = next(x for x in client.get("/api/connections", headers=AUTH).json()["connections"]
                        if x["id"] == "alpaca")
            assert card["symbols_in_use"] == 30 and card["max_symbols"] == 30
            alpaca.send_all(trade_alpaca(symbols[0], 360.5, iso(int(RECORDED_AT * 1000) + 1000)))
            assert c.next(lambda m: m["type"] == "bar" and m["sub"] == "s0")["bar"]["c"] == 360.5
    finally:
        alpaca.stop()


def test_m13_priority_decides_the_provider_and_changes_apply_to_the_next_subscription(live):
    alpaca = FakeWs(alpaca_script(KEY_ID, SECRET)).start()
    finnhub = FakeWs(finnhub_script, reject_token=FINNHUB_KEY).start()
    try:
        serve_every_stock_like_aapl(live, ["MSFT"])
        live.use({"alpaca": alpaca.url, "finnhub": finnhub.url})
        with live.app() as client, live.connect(client) as ws:
            save_alpaca(client)
            save_finnhub(client)
            c = Collector(ws)
            assert subscribe(c, "a", "AAPL", "5m")["source"] == "Alpaca IEX"
            response = client.put("/api/connections/priority", headers=AUTH,
                                  json={"asset_class": "equity", "order": ["finnhub", "alpaca", "databento"]})
            assert response.status_code == 200 and response.json()["groups"]["equity"][0] == "finnhub"
            second = subscribe(c, "b", "MSFT", "5m")
            assert second["source"] == "Finnhub"
            assert wait_until(lambda: any(m.get("type") == "subscribe" and m.get("symbol") == "MSFT"
                                          for m in finnhub.messages()))
            first = [m for m in c.all if m.get("sub") == "a" and m["type"] == "snapshot"][0]
            assert first["source"] == "Alpaca IEX"                            # the open subscription did not move
    finally:
        alpaca.stop()
        finnhub.stop()


def test_m14_finnhub_limit_is_50_and_its_data_state_comes_from_the_providers_file(live):
    """The packaged file declares Finnhub live (its free trade stream is real time); the history is Yahoo, delayed."""
    finnhub = FakeWs(finnhub_script, reject_token=FINNHUB_KEY).start()
    try:
        symbols = many_symbols(51)
        serve_every_stock_like_aapl(live, symbols)
        live.use({"finnhub": finnhub.url})
        with live.app() as client, live.connect(client) as ws:
            save_finnhub(client)
            c = Collector(ws)
            results = [subscribe(c, f"s{i}", symbol, "5m") for i, symbol in enumerate(symbols)]
            assert [r["type"] for r in results[:50]] == ["snapshot"] * 50
            assert results[50]["type"] == "error" and results[50]["code"] == "market.limit"
            assert results[0]["data_state"] == "live" and results[0]["source"] == "Finnhub"
            assert results[0]["delay_seconds"] == 0
            assert results[0]["history"] == {"source": "Yahoo Finance", "delay_seconds": 900}
    finally:
        finnhub.stop()


def test_m14_the_data_state_is_editable_without_code(live):
    finnhub = FakeWs(finnhub_script, reject_token=FINNHUB_KEY).start()
    try:
        live.use({"finnhub": finnhub.url}, finnhub={"data_state": "delayed", "delay_seconds": 900})
        with live.app() as client, live.connect(client) as ws:
            save_finnhub(client)
            c = Collector(ws)
            snapshot = subscribe(c, "a", "AAPL", "5m")
            assert (snapshot["source"], snapshot["data_state"], snapshot["delay_seconds"]) == ("Finnhub", "delayed", 900)
            finnhub.send_all(json.dumps({"type": "trade", "data": [
                {"p": 352.5, "s": "AAPL", "t": int(RECORDED_AT * 1000) + 2000, "v": 5}]}))
            bar = c.next(lambda m: m["type"] == "bar")
            assert bar["data_state"] == "delayed"
    finally:
        finnhub.stop()


# ------------------------------------------------------------------ failover


def test_removing_a_key_moves_its_open_subscriptions_to_the_delayed_fallback(live):
    alpaca = FakeWs(alpaca_script(KEY_ID, SECRET)).start()
    try:
        live.use({"alpaca": alpaca.url})
        with live.app() as client, live.connect(client) as ws:
            save_alpaca(client)
            c = Collector(ws)
            assert subscribe(c, "a", "AAPL", "5m")["source"] == "Alpaca IEX"
            assert client.delete("/api/connections/alpaca/key", headers=AUTH).status_code == 200
            moved = c.find(lambda m: m["type"] == "snapshot" and m.get("sub") == "a" and m["seq"] > 2)
            assert (moved["source"], moved["data_state"], moved["fallback"]) == ("Yahoo Finance", "delayed", True)
    finally:
        alpaca.stop()


def test_a_provider_that_refuses_the_key_after_connecting_hands_its_symbols_to_the_fallback(live):
    alpaca = FakeWs(alpaca_script(KEY_ID, "a-different-secret-xx")).start()
    try:
        live.use({"alpaca": alpaca.url})
        with live.app() as client, live.connect(client) as ws:
            save_alpaca(client)
            c = Collector(ws)
            first = subscribe(c, "a", "AAPL", "5m")
            assert first["source"] in ("Alpaca IEX", "Yahoo Finance")
            status = c.find(lambda m: m["type"] == "connection_status" and m["provider"] == "alpaca"
                            and m["state"] == "error")
            assert status["error_code"] == "connections.auth_failed"
            final = c.find(lambda m: m["type"] == "snapshot" and m.get("sub") == "a" and m["fallback"], timeout=6)
            assert final["data_state"] == "delayed"
    finally:
        alpaca.stop()


def test_a_bad_websocket_is_refused_without_a_token_or_from_a_foreign_origin(live):
    with live.app() as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/market"):
                pass
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/market?token=wrong"):
                pass
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(WS_URL, headers={"origin": "http://evil.example"}):
                pass
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(WS_URL, headers={"host": "evil.example"}):
                pass


def test_a_snapshot_has_what_the_chart_needs(live):
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        snapshot = subscribe(c, "c1", "AAPL", "5m")
        for field in ("bars", "last", "previous_close", "change", "change_pct", "opening_range", "market_open",
                      "delay_seconds", "source", "data_state", "seq", "epoch", "currency", "timezone", "name"):
            assert field in snapshot, field
        assert snapshot["opening_range"]["high"] > 0
        assert copy.deepcopy(snapshot["bars"][-1]).keys() == {"t", "o", "h", "l", "c", "v"}

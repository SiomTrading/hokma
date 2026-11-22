"""Candles from the providers the student connected (task 3289, bug 1204): the chain of `ConnectedSource`, with fake
providers answering in each provider's own format. No network."""
import json
import urllib.request
from datetime import datetime, timezone

import pytest

from hokma.kit import market_tools
from hokma.market import api
from hokma.market.errors import NETWORK, MarketError
from hokma.market.history import ConnectedSource
from hokma.market.hub import MarketHub
from hokma.market.models import Candle, Series
from hokma.market.providers import get_catalog
from hokma.market.service import build_chart

OPEN = int(datetime(2024, 11, 6, 15, 0, tzinfo=timezone.utc).timestamp())      # Wednesday, 10:00 in New York
NOW = OPEN + 40 * 60
KEY = json.dumps({"key_id": "AKTESTKEYID12345", "secret": "alpaca-secret-value-67890"})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the tests must not use the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


class Config:
    """The `ConnectionService` as the chain sees it."""

    loaded = True

    def __init__(self, keys: dict[str, str] | None = None, off: tuple[str, ...] = (), order: dict | None = None) -> None:
        self.keys, self.off, self._order = keys or {}, off, order or {}
        self.catalog = get_catalog()

    def is_enabled(self, provider_id: str) -> bool:
        return provider_id not in self.off

    def has_key(self, provider_id: str) -> bool:
        return provider_id in self.keys

    def order(self, asset_class: str) -> list[str]:
        return list(self._order.get(asset_class, self.catalog.default_priority.get(asset_class, ())))

    def read_secret(self, provider_id: str) -> str | None:
        return self.keys.get(provider_id)


class Yahoo:
    """The last fallback: serves one fixed bar series and counts the calls."""

    name = "Yahoo Finance"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def search(self, query):
        return []

    def history(self, symbol: str, timeframe: str) -> Series:
        self.calls.append((symbol, timeframe))
        step = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1D": 86400}[timeframe]
        candles = [Candle(OPEN + i * step, 1, 2, 0.5, 1.5, 10) for i in range(5)]
        return Series(symbol, symbol, "equity", "NMS", "USD", "America/New_York", -18000, candles, None, OPEN + 4 * step,
                      None, NOW, False, self.name)


class Net:
    """Fake HTTP for the providers: routes by host, counts calls, can refuse (403) or fail by provider."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.status: dict[str, int] = {}

    def __call__(self, url: str, headers: dict, body: bytes | None = None) -> tuple[int, str]:
        self.urls.append(url)
        host = url.split("/")[2]
        if self.status.get(host) == -1:
            raise MarketError(NETWORK, 502)
        if self.status.get(host, 200) != 200:
            return self.status[host], "{}"
        if host == "data.alpaca.markets":
            bar = [{"t": datetime.fromtimestamp(OPEN + i * 300, timezone.utc).isoformat().replace("+00:00", "Z"),
                    "o": 10 + i, "h": 12 + i, "l": 9 + i, "c": 11 + i, "v": 100} for i in range(6)]
            return 200, json.dumps({"bars": {"BTC/USD": bar} if "crypto" in url else bar})
        if host == "api.exchange.coinbase.com":
            return 200, json.dumps([[OPEN + i * 300, 99, 105, 100, 102, 5] for i in (2, 1, 0)])    # newest first
        if host == "finnhub.io":
            return 200, json.dumps({"s": "ok", "t": [OPEN, OPEN + 300], "o": [1, 2], "h": [3, 4], "l": [0, 1],
                                    "c": [2, 3], "v": [7, 8]})
        return 500, ""


def chain(tmp_path, keys=None, off=(), order=None):
    yahoo, net = Yahoo(), Net()
    source = ConnectedSource(yahoo, Config(keys, off, order), get_catalog(), tmp_path / "cache", fetch=net,
                             clock=lambda: NOW)
    return source, yahoo, net


def test_with_alpaca_connected_the_candles_come_from_alpaca_and_the_chart_says_so(tmp_path):
    source, yahoo, net = chain(tmp_path, {"alpaca": KEY})
    chart = build_chart(source, "AAPL", "5m", lambda: NOW)
    assert chart.source == "Alpaca Trading" and len(chart.candles) == 6 and chart.candles[0].o == 10
    assert yahoo.calls == [] and all(u.startswith("https://data.alpaca.markets/v2/stocks/AAPL/bars") for u in net.urls)
    assert chart.last_price == 16 and chart.market_open is True
    assert "AKTESTKEYID12345" not in " ".join(net.urls)                     # keys travel in headers, never in the URL


def test_without_connections_the_candles_come_from_yahoo(tmp_path):
    source, yahoo, net = chain(tmp_path)
    chart = build_chart(source, "AAPL", "5m", lambda: NOW)
    assert chart.source == "Yahoo Finance" and net.urls == []
    assert ("AAPL", "5m") in yahoo.calls


def test_a_provider_that_fails_falls_to_yahoo_and_a_finnhub_key_does_not_serve_stocks(tmp_path):
    """Without a broker the free source is Alpaca. When it fails the candles come from Yahoo as in 1.0.9 (SPEC-016 5.5): a Finnhub key
    saved next to it is not asked for a stock."""
    keys = {"alpaca": KEY, "finnhub": "finnhub-key-value-13579"}
    source, yahoo, net = chain(tmp_path, keys)
    net.status["data.alpaca.markets"] = -1                                  # no network to Alpaca
    chart = build_chart(source, "AAPL", "5m", lambda: NOW)
    assert chart.source == "Yahoo Finance" and ("AAPL", "5m") in yahoo.calls
    assert not any("finnhub.io" in url for url in net.urls)


def test_a_refused_plan_is_not_asked_again_on_every_poll(tmp_path):
    source, yahoo, net = chain(tmp_path, {"finnhub": "finnhub-key-value-13579"}, off=("alpaca",))
    net.status["finnhub.io"] = 403
    for _ in range(5):
        assert source.history("EURUSD=X", "1m").source == "Yahoo Finance"      # Finnhub serves forex
    assert len(net.urls) == 1


def test_the_answer_is_cached_so_repeated_reads_make_one_request(tmp_path):
    source, _, net = chain(tmp_path, {"alpaca": KEY})
    for _ in range(4):
        source.history("AAPL", "5m")
    assert len(net.urls) == 1


def test_a_switched_off_provider_is_not_asked(tmp_path):
    keys = {"alpaca": KEY, "finnhub": "finnhub-key-value-13579"}
    source, _, _ = chain(tmp_path, keys)
    assert source.history("AAPL", "5m").source == "Alpaca Trading"
    source, yahoo, net = chain(tmp_path / "off", keys, off=("alpaca",))
    assert source.history("AAPL", "5m").source == "Yahoo Finance" and net.urls == []


def test_the_finnhub_key_travels_in_a_header_and_never_in_the_url(tmp_path):
    seen = []
    source, _, net = chain(tmp_path, {"finnhub": "finnhub-key-value-13579"}, off=("alpaca",))
    source._fetch = lambda url, headers, body=None: seen.append((url, headers)) or net(url, headers, body)
    assert source.history("EURUSD=X", "5m").source == "Finnhub"
    url, headers = seen[0]
    assert headers == {"X-Finnhub-Token": "finnhub-key-value-13579"}
    assert "token" not in url and "finnhub-key-value-13579" not in url


def test_crypto_uses_coinbase_candles_and_without_it_alpaca_bars(tmp_path):
    source, yahoo, net = chain(tmp_path, {"alpaca": KEY})
    series = source.history("BTC-USD", "5m")
    assert series.source == "Coinbase" and [c.t for c in series.candles] == [OPEN, OPEN + 300, OPEN + 600]
    assert series.kind == "crypto" and series.candles[0].o == 100 and series.candles[0].l == 99
    source, _, net = chain(tmp_path / "off", {"alpaca": KEY}, off=("coinbase",))
    assert source.history("BTC-USD", "5m").source == "Alpaca Trading"
    assert "/v1beta3/crypto/us/bars" in net.urls[0] and "BTC%2FUSD" in net.urls[0]


def test_futures_have_no_connected_history_and_come_from_yahoo(tmp_path):
    source, yahoo, net = chain(tmp_path, {"alpaca": KEY})
    assert source.history("NQ=F", "5m").source == "Yahoo Finance"
    assert ("NQ=F", "5m") in yahoo.calls and net.urls == []


def test_a_desk_with_only_coinbase_active_uses_coinbase_for_btc_and_yahoo_for_stocks(tmp_path):
    source, yahoo, net = chain(tmp_path, {"alpaca": KEY})
    desk = source.for_desk({"coinbase"})
    assert desk.history("BTC-USD", "5m").source == "Coinbase"
    assert desk.history("AAPL", "5m").source == "Yahoo Finance"              # Alpaca is not a connection of this desk
    assert all("alpaca" not in u for u in net.urls)
    assert source.for_desk(None) is source


def test_a_stock_series_keeps_every_session_the_source_sends(tmp_path):
    """SPEC-016 3.4: the data is painted as the source sends it, pre-market included. No hour of ours cuts it."""
    class Wide(Net):
        def __call__(self, url, headers, body=None):
            self.urls.append(url)
            early = int(datetime(2024, 11, 6, 12, 0, tzinfo=timezone.utc).timestamp())        # 07:00 in New York
            bars = [{"t": datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z"), "o": 1, "h": 2,
                     "l": 1, "c": 2, "v": 1} for t in (early, OPEN)]
            return 200, json.dumps({"bars": bars})

    source = ConnectedSource(Yahoo(), Config({"alpaca": KEY}), get_catalog(), tmp_path / "c", fetch=Wide(), clock=lambda: NOW)
    series = source.history("AAPL", "5m")
    assert [c.t for c in series.candles] == [int(datetime(2024, 11, 6, 12, 0, tzinfo=timezone.utc).timestamp()), OPEN]
    assert series.clock_aligned is True


def test_market_candles_and_quote_use_the_same_chain(tmp_path):
    source, yahoo, net = chain(tmp_path, {"alpaca": KEY})
    api.set_source(source)
    api.set_clock(lambda: NOW)
    try:
        out = market_tools.candles("AAPL", "5m", 3)
        assert out["source"] == "Alpaca Trading" and out["count"] == 3
        assert market_tools.quote("AAPL")["source"] == "Alpaca Trading"
        assert market_tools.quote("AAPL")["quality"] == "practice"                        # SPEC-016 3.9: not the price of an execution
        desk = market_tools.candles("BTC-USD", "5m", 2, desk_id=None)
        assert desk["source"] == "Coinbase" and desk["quality"] == "real"
        assert yahoo.calls == []
    finally:
        api.set_source(None)
        api.set_clock(lambda: __import__("time").time())


def test_the_snapshot_reports_the_real_source_of_the_history():
    hub = MarketHub(get_catalog(), Config(), lambda: Yahoo(), clock=lambda: NOW)
    live = hub._runtimes["alpaca"]
    assert hub._history_info(live, "equity", "Alpaca Trading") == {"source": "Alpaca Trading", "delay_seconds": 0}
    assert hub._history_info(live, "equity", "Yahoo Finance") == {"source": "Yahoo Finance", "delay_seconds": 900}
    assert hub._history_info(hub._fallback, "equity", "Yahoo Finance") == {"source": "Yahoo Finance", "delay_seconds": 0}


def test_a_redirect_to_another_host_does_not_carry_the_alpaca_headers():
    from hokma.market.history import _KeepKeysOnHost

    request = urllib.request.Request("https://data.alpaca.markets/v2/stocks/AAPL/bars",
                                     headers={"APCA-API-KEY-ID": "AKTESTKEYID12345", "APCA-API-SECRET-KEY": "s3cret",
                                              "Accept": "application/json"})
    handler = _KeepKeysOnHost()
    other = handler.redirect_request(request, None, 302, "Found", {}, "https://elsewhere.example.com/x")
    assert other is not None and not [n for n in other.headers if n.lower().startswith("apca-")]
    assert other.get_header("Accept") == "application/json"
    same = handler.redirect_request(request, None, 302, "Found", {}, "https://data.alpaca.markets/v2/other")
    assert same.get_header("Apca-api-key-id") == "AKTESTKEYID12345" and same.get_header("Apca-api-secret-key") == "s3cret"

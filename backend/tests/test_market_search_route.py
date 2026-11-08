"""The chart search says which source would serve each result: `asset_class` and `route` per hit, the class filter, the
roots that a connected provider announces, and `preview_route` that connects nothing. Test T-10 of the chart addendum.
Nothing touches the network."""
import pytest

from hokma.connections import runtime
from hokma.market import api as market_api
from hokma.market.models import SymbolHit
from futures_fake_server import FuturesFakeServer
from live_fixtures import AUTH, live  # noqa: F401  (fixture)
from live_support import FakeWs, coinbase_script, finnhub_script, wait_until
from test_futures_adapter import card, save_token


@pytest.fixture
def server():
    fake = FuturesFakeServer().start()
    yield fake
    fake.stop()


class SearchStub:
    """The history of the recorded answers and a search that returns the list the test gives."""

    name = "Yahoo Finance"

    def __init__(self, inner, hits):
        self.inner, self.hits = inner, hits

    def search(self, query):
        return list(self.hits)                              # the source answers the same list; the hub sorts it out

    def history(self, symbol, timeframe):
        return self.inner.history(symbol, timeframe)


HITS = [SymbolHit("BTC-USD", "Bitcoin USD", "crypto", "CCC"), SymbolHit("AAPL", "Apple", "equity", "NASDAQ"),
        SymbolHit("^NDX", "NASDAQ-100", "index", "Index")]


def test_t10_each_result_says_which_source_serves_it_and_the_classes_filter(live):
    coinbase = FakeWs(coinbase_script).start()
    try:
        live.use({"coinbase": coinbase.url})
        market_api.set_source(SearchStub(market_api.get_source(), HITS))
        with live.app() as client:
            body = client.get("/api/market/symbols?q=BTC", headers=AUTH).json()
            item = next(i for i in body["items"] if i["symbol"] == "BTC-USD")
            assert item["asset_class"] == "crypto"
            assert item["route"] == {"provider": "coinbase", "source": "Coinbase", "data_state": "live"}
            assert "crypto" in body["live_classes"] and "equity" not in body["live_classes"]     # Alpaca has no key
            body = client.get("/api/market/symbols?q=A", headers=AUTH).json()
            apple = next(i for i in body["items"] if i["symbol"] == "AAPL")
            assert apple["route"] == {"provider": "yahoo_polling", "source": "Yahoo Finance", "data_state": "delayed"}
            only = client.get("/api/market/symbols?q=A&class=crypto", headers=AUTH).json()
            assert {i["symbol"] for i in only["items"]} == {"BTC-USD"}
            indices = client.get("/api/market/symbols?q=N&class=index", headers=AUTH).json()
            assert {i["symbol"] for i in indices["items"]} == {"^NDX"}
            assert client.get("/api/market/symbols?q=A&class=bonds", headers=AUTH).status_code == 400
            assert coinbase.paths == []                           # looking at the routes opened no connection
    finally:
        coinbase.stop()


def test_t10_with_a_finnhub_key_a_stock_says_live_finnhub_and_not_the_yahoo_fallback(live):
    """The real hub and the packaged providers file, as the app runs them: Finnhub is live for US stocks, so a search
    for AAPL must not say Yahoo Finance delayed once the owner saved the Finnhub key (bug of task 3081)."""
    finnhub = FakeWs(finnhub_script, reject_token="finnhub-key-value-13579").start()
    try:
        live.use({"finnhub": finnhub.url})
        market_api.set_source(SearchStub(market_api.get_source(), HITS))
        with live.app() as client:
            before = next(i for i in client.get("/api/market/symbols?q=A", headers=AUTH).json()["items"] if i["symbol"] == "AAPL")
            assert before["route"]["source"] == "Yahoo Finance" and before["route"]["data_state"] == "delayed"
            assert client.put("/api/connections/finnhub/key", json={"key": "finnhub-key-value-13579"}, headers=AUTH).status_code == 200
            assert wait_until(lambda: len(finnhub.paths) >= 1)    # saving the key connects the provider once
            opened = len(finnhub.paths)
            body = client.get("/api/market/symbols?q=A", headers=AUTH).json()
            apple = next(i for i in body["items"] if i["symbol"] == "AAPL")
            assert apple["route"] == {"provider": "finnhub", "source": "Finnhub", "data_state": "live"}
            assert "equity" in body["live_classes"]
            assert len(finnhub.paths) == opened                   # a preview opens no connection
    finally:
        finnhub.stop()


def test_t10_preview_route_connects_nothing_and_counts_nothing(live):
    coinbase = FakeWs(coinbase_script).start()
    try:
        live.use({"coinbase": coinbase.url})
        with live.app():
            hub = runtime.get_hub()
            assert hub.preview_route("BTC-USD", "crypto")["source"] == "Coinbase"
            assert hub.preview_route("AAPL", "equity")["data_state"] == "delayed"
            assert hub.preview_route("^NDX", None) == {"provider": "yahoo_polling", "source": "Yahoo Finance", "data_state": "delayed"}
            assert coinbase.paths == [] and not hub._feeds and not hub._runtimes["coinbase"].feeds
    finally:
        coinbase.stop()


def test_t10_a_root_announced_by_the_futures_provider_is_offered_even_when_the_search_does_not_return_it(live, server):
    live.use({"siomtrading_futures": server.url})
    market_api.set_source(SearchStub(market_api.get_source(), []))
    with live.app() as client, live.connect(client):
        save_token(client)
        assert wait_until(lambda: card(client)["state"] == "connected")
        body = client.get("/api/market/symbols?q=es", headers=AUTH).json()
        es = next(i for i in body["items"] if i["symbol"] == "ES=F")
        assert es["asset_class"] == "future" and es["route"]["source"] == "SiomTrading Futures"
        assert es["route"]["data_state"] == "live"
        assert "future" in body["live_classes"]
        everything = client.get("/api/market/symbols?class=future", headers=AUTH).json()
        assert {"NQ=F", "ES=F", "GC=F"} <= {i["symbol"] for i in everything["items"]}
        assert client.get("/api/market/symbols?q=zz", headers=AUTH).json()["items"] == []

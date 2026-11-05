"""D9: /api/market over HTTP with a source built on recorded answers. No network."""
import time

import pytest
from fastapi.testclient import TestClient

from hokma.core.app import create_app
from hokma.i18n import t
from hokma.market import api as market_api
from hokma.market.cache import RateLimiter
from hokma.market.yahoo import YahooSource
from hokma.rpc.ws import SESSION_TOKEN
from market_support import RECORDED_AT, FakeFetcher

AUTH = {"X-Hokma-Token": SESSION_TOKEN}


@pytest.fixture
def fetcher():
    return FakeFetcher()


@pytest.fixture
def client(tmp_path, fetcher, monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "en")
    market_api.set_source(YahooSource(tmp_path / "cache", fetch=fetcher, clock=lambda: RECORDED_AT))
    market_api.set_clock(lambda: RECORDED_AT)
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
    yield TestClient(create_app())
    market_api.set_source(None)
    market_api.set_clock(time.time)


def test_routes_need_the_session_token(client):
    assert client.get("/api/market/symbols?q=nq").status_code == 401
    assert client.get("/api/market/candles?symbol=AAPL&tf=5m", headers={"X-Hokma-Token": "nope"}).status_code == 401
    assert client.get(f"/api/market/candles?symbol=AAPL&tf=5m&token={SESSION_TOKEN}").status_code == 200


def test_empty_query_returns_well_known_tickers_without_calling_the_provider(client, fetcher):
    body = client.get("/api/market/symbols", headers=AUTH).json()
    assert body["suggested"] is True
    symbols = [item["symbol"] for item in body["items"]]
    assert "NQ=F" in symbols and "ES=F" in symbols and "AAPL" in symbols
    assert fetcher.calls == []


def test_symbol_search_returns_the_provider_matches(client):
    body = client.get("/api/market/symbols?q=nasdaq", headers=AUTH).json()
    assert body["suggested"] is False
    assert {"symbol": "NQ=F", "name": "Nasdaq 100 Dec 26", "kind": "future", "exchange": "Chicago Mercantile Exchange"} in body["items"]


def test_symbol_search_with_no_match_is_200_and_empty(client):
    response = client.get("/api/market/symbols?q=zzqqxxnotasymbol", headers=AUTH)
    assert response.status_code == 200 and response.json()["items"] == []


def test_query_longer_than_40_characters_is_rejected(client):
    assert client.get("/api/market/symbols?q=" + "a" * 41, headers=AUTH).status_code == 422


def test_candles_response_shape(client):
    body = client.get("/api/market/candles?symbol=AAPL&tf=5m", headers=AUTH).json()
    assert body["symbol"] == "AAPL" and body["timeframe"] == "5m"
    assert body["market_open"] is False and body["delayed"] is False
    assert body["source"] == "Yahoo Finance" and body["stale"] is False
    assert set(body["candles"][0]) == {"t", "o", "h", "l", "c", "v"}
    assert body["opening_range"]["session_date"] == "2026-11-04"
    assert body["last_price"] == body["candles"][-1]["c"]


def test_daily_timeframe_has_no_opening_range(client):
    body = client.get("/api/market/candles?symbol=AAPL&tf=1D", headers=AUTH).json()
    assert body["opening_range"] is None


def test_futures_symbol_with_equals_sign_works(client):
    body = client.get("/api/market/candles?symbol=NQ%3DF&tf=5m", headers=AUTH).json()
    assert body["symbol"] == "NQ=F" and body["market_open"] is True and body["delayed"] is True


def test_default_timeframe_is_5m(client):
    assert client.get("/api/market/candles?symbol=AAPL", headers=AUTH).json()["timeframe"] == "5m"


def test_unknown_symbol_is_404_with_a_translated_message(client):
    response = client.get("/api/market/candles?symbol=ZZZZNOTREAL&tf=5m", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "unknown_symbol", "message": t("market.error.unknown_symbol")}


def test_bad_symbol_and_bad_timeframe_are_rejected_before_any_call(client, fetcher):
    bad = client.get("/api/market/candles?symbol=a;b&tf=5m", headers=AUTH)
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "bad_symbol"
    assert client.get("/api/market/candles?symbol=AAPL&tf=2m", headers=AUTH).status_code == 422
    assert fetcher.calls == []


def test_network_failure_is_502_without_internals(client, fetcher):
    fetcher.fail = "network"
    response = client.get("/api/market/candles?symbol=AAPL&tf=5m", headers=AUTH)
    assert response.status_code == 502
    assert response.json()["detail"] == {"code": "network", "message": t("market.error.network")}
    assert "Traceback" not in response.text


def test_provider_rate_limit_is_429(client, fetcher):
    fetcher.fail = "429"
    response = client.get("/api/market/candles?symbol=AAPL&tf=5m", headers=AUTH)
    assert response.status_code == 429 and response.json()["detail"]["code"] == "rate_limited"


def test_stale_cache_is_served_with_the_flag_when_the_network_drops(client, fetcher, tmp_path):
    source = YahooSource(tmp_path / "c2", fetch=fetcher, clock=lambda: RECORDED_AT)
    market_api.set_source(source)
    assert client.get("/api/market/candles?symbol=AAPL&tf=5m", headers=AUTH).json()["stale"] is False
    source._clock = lambda: RECORDED_AT + 7200
    fetcher.fail = "network"
    body = client.get("/api/market/candles?symbol=AAPL&tf=5m", headers=AUTH).json()
    assert body["stale"] is True and body["candles"]


def test_endpoint_rate_limit(client, monkeypatch):
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=2, window=60))
    assert client.get("/api/market/symbols", headers=AUTH).status_code == 200
    assert client.get("/api/market/symbols", headers=AUTH).status_code == 200
    assert client.get("/api/market/symbols", headers=AUTH).status_code == 429


def test_error_messages_follow_the_language(client, monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "es")
    response = client.get("/api/market/candles?symbol=ZZZZNOTREAL&tf=5m", headers=AUTH)
    assert response.json()["detail"]["message"] == t("market.error.unknown_symbol")
    monkeypatch.setenv("HOKMA_LANG", "en")
    assert response.json()["detail"]["message"] != t("market.error.unknown_symbol")

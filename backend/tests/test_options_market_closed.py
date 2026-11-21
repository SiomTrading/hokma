"""The chain with the market closed (SPEC-014): night, Saturday and Sunday are not an error. The fake clock is a Saturday and
the snapshots are the last ones of Friday's session."""
import pytest
from fastapi.testclient import TestClient
from options_support import CLOSED_NOW, DATA, alpaca, install  # noqa: F401  (fixture)

from hokma.core.app import create_app
from hokma.market import api as market_api
from hokma.market import options
from hokma.market.cache import RateLimiter
from hokma.rpc.ws import SESSION_TOKEN

AUTH = {"X-Hokma-Token": SESSION_TOKEN}
LAST_QUOTE = "2026-11-20T20:59:58Z"


@pytest.fixture
def closed(monkeypatch):
    fake = install(monkeypatch, snapshots="snapshots_indicative_closed.json", now=CLOSED_NOW)
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
    return fake


def test_the_tool_answers_on_a_saturday_with_the_last_quotes(closed):
    out = options.options_chain("AAPL", expiration="2026-12-18", type="call", limit=3)
    assert out["market_open"] is False and out["as_of"] == LAST_QUOTE and out["contracts"]
    assert all(c["quote_time"] == LAST_QUOTE for c in out["contracts"])
    assert out["price_feed"] == "indicative" and out["greeks"] == "estimated"


def test_the_route_answers_200_with_contracts_market_closed_and_the_time_of_the_last_quote(closed):
    client = TestClient(create_app())
    body = client.get("/api/market/options/chain?symbol=AAPL&expirations=2026-12-18,2027-01-15&strikes=6", headers=AUTH)
    assert body.status_code == 200
    data = body.json()
    assert data["market_open"] is False and data["as_of"] == LAST_QUOTE
    assert [g["date"] for g in data["expirations"]] == ["2026-12-18", "2027-01-15"]
    cell = data["expirations"][0]["rows"][0]["call"]
    assert cell["quote_time"] == LAST_QUOTE and cell["bid"] is not None


def test_days_are_counted_from_the_new_york_date_even_on_a_weekend(closed):
    out = options.options_chain("AAPL", expiration="2026-11-23", limit=1)
    assert out["contracts"] == [] and out["scanned"] == 0                 # no such expiration in the fixture: not an error
    out = options.options_chain("AAPL", expiration="2026-12-18", limit=1)
    assert out["contracts"][0]["days"] == 27                              # Saturday 21 November to Friday 18 December


def test_the_underlying_bar_answers_with_the_market_closed(closed):
    body = TestClient(create_app()).get("/api/market/options/underlying?symbol=AAPL", headers=AUTH).json()
    assert body["last"] == 228.4 and body["bid"] == 228.33 and body["ask"] == 228.46
    assert any("/snapshot" in url for url, _ in closed.calls) and closed.hosts() == [DATA]

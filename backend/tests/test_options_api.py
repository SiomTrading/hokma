"""The three routes of the option chain of the dashboard (SPEC-014): token, errors, windows of strikes and Hokma's numbers."""
import math
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from options_support import DATA, DEMO_KEY, LIVE, PAPER, QUOTE, REAL_KEY, alpaca, install, load  # noqa: F401  (fixture)

from hokma.core.app import create_app
from hokma.market import api as market_api
from hokma.market import options
from hokma.market.cache import RateLimiter
from hokma.rpc.ws import SESSION_TOKEN

AUTH = {"X-Hokma-Token": SESSION_TOKEN}
CHAIN = "/api/market/options/chain?symbol=AAPL&expirations="
SNAPS = load("snapshots_indicative_open.json")["snapshots"]


@pytest.fixture
def client(alpaca, monkeypatch):
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
    return TestClient(create_app())


def group(client, day="2026-12-18", strikes="14"):
    response = client.get(f"{CHAIN}{day}&strikes={strikes}", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def strikes_of(body, index=0):
    return [r["strike"] for r in body["expirations"][index]["rows"]]


# ------------------------------------------------------------------------------------------ token and errors

@pytest.mark.parametrize("path", ["/api/market/options/underlying?symbol=AAPL", "/api/market/options/expirations?symbol=AAPL",
                                  CHAIN + "2026-12-18"])
def test_the_three_routes_need_the_session_token(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Hokma-Token": "nope"}).status_code == 401
    assert client.get(path + ("&" if "?" in path else "?") + f"token={SESSION_TOKEN}").status_code == 200


def error(client, path, status, code):
    response = client.get(path, headers=AUTH)
    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == code and response.json()["detail"]["message"]
    return response.json()["detail"]


def test_errors_carry_their_code_and_status(monkeypatch):
    fake = install(monkeypatch, None)
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
    client = TestClient(create_app())
    detail = error(client, CHAIN + "2026-12-18", 409, "options.no_connection")
    assert detail["message"] == "The option chain needs Alpaca IEX: save its key and turn it on in Connections."
    error(client, "/api/market/options/expirations?symbol=AAPL", 409, "options.no_connection")
    assert fake.calls == []


def test_not_equity_and_bad_requests_are_422(client, alpaca):
    error(client, "/api/market/options/expirations?symbol=NQ=F", 422, "options.not_equity")
    error(client, "/api/market/options/chain?symbol=BTC-USD&expirations=2026-12-18", 422, "options.not_equity")
    error(client, CHAIN + "2026-12-18,2027-01-15,2027-01-29,2027-02-19,2027-03-19", 422, "options.bad_request")   # five
    error(client, CHAIN + "18-12-2026", 422, "options.bad_request")
    error(client, CHAIN + "2026-12-18&strikes=7", 422, "options.bad_request")
    error(client, CHAIN + "2026-12-18&strikes=abc", 422, "options.bad_request")
    error(client, "/api/market/options/chain?symbol=AAPL&expirations=%2C", 422, "options.bad_request")
    assert alpaca.calls == []


@pytest.mark.parametrize("status,code,http", [(401, "options.auth_failed", 401), (429, "options.rate_limited", 429),
                                              (500, "options.upstream", 502)])
def test_what_alpaca_answers_reaches_the_dashboard_as_a_code(client, alpaca, status, code, http):
    alpaca.status[PAPER] = status
    error(client, CHAIN + "2026-12-18", http, code)


def test_a_network_failure_is_502_upstream(client, alpaca):
    alpaca.fail = True
    error(client, CHAIN + "2026-12-18", 502, "options.upstream")


# ------------------------------------------------------------------------------------------ expirations

def test_expirations_come_from_the_contracts_with_days_and_multiplier(client, alpaca):
    body = client.get("/api/market/options/expirations?symbol=aapl", headers=AUTH).json()
    assert body["symbol"] == "AAPL" and body["source"] == "Alpaca"
    assert body["expirations"][:3] == [{"date": "2026-11-20", "days": 0, "multiplier": 100},
                                       {"date": "2026-12-18", "days": 28, "multiplier": 100},
                                       {"date": "2027-01-15", "days": 56, "multiplier": 100}]
    query = alpaca.queries(PAPER)[0]
    assert query["type"] == "call" and query["expiration_date_gte"] == "2026-11-20"
    assert query["expiration_date_lte"] == "2027-12-25"                  # 400 days


def test_the_multiplier_is_the_size_of_the_contract_and_100_when_missing(client, alpaca):
    for c in alpaca.contracts:
        if c["expiration_date"] == "2026-12-18":
            c["size"] = "10"
        if c["expiration_date"] == "2027-01-15":
            del c["size"], c["multiplier"]
    by_day = {e["date"]: e["multiplier"] for e in client.get("/api/market/options/expirations?symbol=AAPL", headers=AUTH).json()["expirations"]}
    assert by_day["2026-12-18"] == 10 and by_day["2027-01-15"] == 100


# ------------------------------------------------------------------------------------------ the chain

def test_three_expirations_in_one_call(client, alpaca):
    body = group(client, "2026-11-20,2026-12-18,2027-01-15")
    assert [g["date"] for g in body["expirations"]] == ["2026-11-20", "2026-12-18", "2027-01-15"]
    assert [g["days"] for g in body["expirations"]] == [0, 28, 56]
    assert (body["underlying"], body["underlying_price"], body["source"]) == ("AAPL", 228.4, "Alpaca")
    assert (body["price_feed"], body["greeks"], body["market_open"], body["notes"]) == ("indicative", "estimated", True, [])
    assert body["as_of"] == "2026-11-20T16:00:01Z"
    assert len([q for q in alpaca.queries(PAPER)]) == 3                    # one contracts request per expiration
    assert [q["expiration_date"] for q in alpaca.queries(PAPER)] == ["2026-11-20", "2026-12-18", "2027-01-15"]
    snapshot_queries = [q for q in alpaca.queries(DATA) if q["feed"] == "indicative"]
    assert [q["expiration_date"] for q in snapshot_queries] == ["2026-11-20", "2026-12-18", "2027-01-15"]
    assert all("strike_price_gte" in q and "strike_price_lte" in q for q in snapshot_queries)     # only the window of strikes


def test_the_window_has_14_strikes_around_the_price_half_below_and_half_above(client):
    assert strikes_of(group(client)) == [195.0 + 5 * i for i in range(14)]          # 195 .. 260, price 228.40


def test_the_window_has_6_strikes_when_asked_and_the_first_group_changes_with_it(client):
    assert strikes_of(group(client, strikes="6")) == [215.0, 220.0, 225.0, 230.0, 235.0, 240.0]


def test_strikes_all_returns_every_strike_of_the_expiration(client, alpaca):
    body = group(client, strikes="all")
    assert strikes_of(body) == [190.0 + 5 * i for i in range(17)]
    assert all(q["strike_price_gte"] == "190" and q["strike_price_lte"] == "270"
               for q in alpaca.queries(DATA) if q["feed"] == "indicative")


def test_a_side_that_does_not_reach_is_completed_with_the_other(monkeypatch):
    install(monkeypatch, quote={**QUOTE, "last": 262.0})
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
    body = group(TestClient(create_app()), strikes="6")
    assert strikes_of(body) == [245.0, 250.0, 255.0, 260.0, 265.0, 270.0]        # only 2 strikes above: 4 below


def test_without_a_price_the_window_is_the_central_strikes(monkeypatch):
    install(monkeypatch, quote=None)
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
    body = group(TestClient(create_app()), strikes="6")
    assert body["underlying_price"] is None and strikes_of(body) == [215.0, 220.0, 225.0, 230.0, 235.0, 240.0]
    cell = body["expirations"][0]["rows"][0]["call"]
    assert cell["itm"] is False and cell["intrinsic"] is None and cell["prob_itm"] is None and cell["bid"] is not None
    assert body["expirations"][0]["expected_move"] is None


def test_in_the_money_intrinsic_and_probability_follow_the_price(client):
    rows = {r["strike"]: r for r in group(client)["expirations"][0]["rows"]}
    call, put = rows[220.0]["call"], rows[220.0]["put"]
    assert call["itm"] is True and call["intrinsic"] == 8.4 and put["itm"] is False and put["intrinsic"] == 0.0
    call, put = rows[235.0]["call"], rows[235.0]["put"]
    assert call["itm"] is False and call["intrinsic"] == 0.0 and put["itm"] is True and put["intrinsic"] == 6.6
    probs = [rows[s]["call"]["prob_itm"] for s in sorted(rows)]
    assert all(0 < p < 1 for p in probs) and probs == sorted(probs, reverse=True)       # lower strike, higher probability
    years = options.years_to_expiry(date(2026, 12, 18))
    iv = SNAPS["AAPL261218C00220000"]["impliedVolatility"]
    d2 = (math.log(228.4 / 220) - iv * iv * years / 2) / (iv * math.sqrt(years))
    assert rows[220.0]["call"]["prob_itm"] == round(0.5 * (1 + math.erf(d2 / math.sqrt(2))), 4)
    put_iv = SNAPS["AAPL261218P00220000"]["impliedVolatility"]
    put_d2 = (math.log(228.4 / 220) - put_iv * put_iv * years / 2) / (put_iv * math.sqrt(years))
    assert rows[220.0]["put"]["prob_itm"] == round(0.5 * (1 + math.erf(-put_d2 / math.sqrt(2))), 4)


def test_expiration_iv_is_the_average_of_the_call_and_the_put_of_the_strike_nearest_the_price(client):
    group_ = group(client)["expirations"][0]
    call, put = SNAPS["AAPL261218C00230000"]["impliedVolatility"], SNAPS["AAPL261218P00230000"]["impliedVolatility"]
    assert group_["iv"] == round((call + put) / 2, 4)
    years = options.years_to_expiry(date(2026, 12, 18))
    assert group_["expected_move"] == round(228.4 * group_["iv"] * math.sqrt(years) * math.sqrt(2 / math.pi), 3)


def test_the_expected_move_of_the_capture_is_reproduced_within_three_percent(monkeypatch):
    install(monkeypatch)
    # price 32.52, IV 59.69 %, 29 days: the platform shows +-4.378. The clock sits 29 days before the close of the expiration.
    monkeypatch.setattr(options, "_clock", lambda: datetime(2026, 11, 19, 21, 0, tzinfo=timezone.utc).timestamp())
    years = options.years_to_expiry(date(2026, 12, 18))
    assert years * 365 == pytest.approx(29, abs=0.01)
    assert options.expected_move(32.52, 0.5969, years) == pytest.approx(4.378, rel=0.03)


def test_exchange_letters_are_null_when_the_feed_does_not_bring_them_and_present_when_it_does(client, monkeypatch):
    cell = group(client)["expirations"][0]["rows"][0]["call"]
    assert (cell["last_exchange"], cell["bid_exchange"], cell["ask_exchange"]) == (None, None, None)
    install(monkeypatch, opra=True)
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
    body = group(TestClient(create_app()))
    cell = body["expirations"][0]["rows"][0]["call"]
    assert (cell["last_exchange"], cell["bid_exchange"], cell["ask_exchange"]) == ("N", "C", "X")
    assert (body["price_feed"], body["greeks"]) == ("opra", "calculated")


def test_a_cell_has_the_fields_of_the_dashboard_and_no_gamma_or_vega(client):
    cell = group(client)["expirations"][0]["rows"][3]["put"]
    assert set(cell) == {"symbol", "itm", "last", "last_exchange", "bid", "bid_exchange", "ask", "ask_exchange", "volume",
                         "open_interest", "theta", "delta", "iv", "intrinsic", "prob_itm", "quote_time"}
    assert isinstance(cell["open_interest"], int) and cell["delta"] < 0


def test_the_chain_comes_with_a_real_key_the_same_as_with_a_demo_key_and_with_open_interest(monkeypatch):
    results = []
    for key, host in ((DEMO_KEY, PAPER), (REAL_KEY, LIVE)):
        fake = install(monkeypatch, key)
        monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
        results.append(group(TestClient(create_app()), "2026-12-18,2027-01-15"))
        assert set(fake.paths(host)) == {"/v2/options/contracts"} and len(fake.queries(host)) == 2
    assert results[0] == results[1]
    assert all(isinstance(r["call"]["open_interest"], int) for g in results[1]["expirations"] for r in g["rows"])


def test_refresh_asks_again_inside_the_cache_window(client, alpaca):
    group(client)
    count = len(alpaca.calls)
    group(client)
    assert len(alpaca.calls) == count
    assert client.get(CHAIN + "2026-12-18&refresh=true", headers=AUTH).status_code == 200
    assert len(alpaca.calls) > count


def test_an_expiration_without_contracts_is_an_empty_group_not_an_error(client):
    body = group(client, "2026-11-23")
    assert body["expirations"][0]["rows"] == [] and body["expirations"][0]["iv"] is None


# ------------------------------------------------------------------------------------------ the bar of the underlying

def test_the_underlying_bar_takes_the_chart_numbers_and_the_snapshot_for_bid_ask_and_open(client, alpaca):
    body = client.get("/api/market/options/underlying?symbol=AAPL", headers=AUTH).json()
    assert body == {"symbol": "AAPL", "last": 228.4, "last_exchange": "V", "net_change": -0.65, "bid": 228.33,
                    "bid_exchange": "P", "ask": 228.46, "ask_exchange": "Q", "bid_size": 4, "ask_size": 3,
                    "volume": 731132, "open": 229.05, "high": 229.9, "low": 227.8, "as_of": body["as_of"]}
    assert body["as_of"] == "2026-11-20T20:59:59Z"
    assert alpaca.queries(DATA)[0]["feed"] == "iex"


def test_volume_is_null_when_the_chart_does_not_give_it(monkeypatch):
    install(monkeypatch, quote={**QUOTE, "volume": None})
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
    body = TestClient(create_app()).get("/api/market/options/underlying?symbol=AAPL", headers=AUTH).json()
    assert body["volume"] is None and body["high"] == 229.9


def test_volume_is_null_when_the_chart_comes_from_alpaca_iex_and_kept_from_a_consolidated_source(monkeypatch):
    for source in ("Alpaca", "Alpaca IEX"):
        install(monkeypatch, quote={**QUOTE, "source": source})
        monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
        body = TestClient(create_app()).get("/api/market/options/underlying?symbol=AAPL", headers=AUTH).json()
        assert body["volume"] is None and body["high"] == 229.9
    install(monkeypatch, quote={**QUOTE, "source": "Yahoo Finance"})
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
    body = TestClient(create_app()).get("/api/market/options/underlying?symbol=AAPL", headers=AUTH).json()
    assert body["volume"] == 731132


def test_without_alpaca_the_bar_still_answers_from_the_chart(monkeypatch):
    fake = install(monkeypatch, None)
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
    body = TestClient(create_app()).get("/api/market/options/underlying?symbol=AAPL", headers=AUTH).json()
    assert body["last"] == 228.4 and body["bid"] is None and body["ask"] is None and body["open"] is None
    assert fake.calls == []


def test_without_the_chart_the_bar_falls_back_to_the_snapshot(monkeypatch):
    install(monkeypatch, quote=None)
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
    body = TestClient(create_app()).get("/api/market/options/underlying?symbol=AAPL", headers=AUTH).json()
    assert body["last"] == 228.4 and body["net_change"] == -0.65 and body["volume"] is None
    assert (body["open"], body["high"], body["low"]) == (229.05, 229.9, 227.8)


def test_with_neither_the_chart_nor_alpaca_the_bar_says_alpaca_is_not_connected(monkeypatch):
    install(monkeypatch, None, quote=None)
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
    response = TestClient(create_app()).get("/api/market/options/underlying?symbol=AAPL", headers=AUTH)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "options.no_connection"

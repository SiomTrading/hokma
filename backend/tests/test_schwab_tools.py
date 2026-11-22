"""The Schwab read tools of the agent (SPEC-015 5.11) and the internal API /api/schwab (5.10) against a fake Schwab.

Every tool calls its endpoint with all its arguments, answers source, as_of and delayed when Schwab says it, and without
a Schwab session answers the catalog text schwab.err.not_connected. The API answers the same data, refuses a request
without the session token, takes an account only as account_ref, and has no route and no tool that places, changes or
cancels an order. Nothing touches the network and no answer holds a hash, an account number, a token or the ids of
streamerInfo."""
import datetime as dt
import json
import math
import re
import statistics

import pytest

from dbsupport import db, env  # noqa: F401  (fixtures)
from fastapi.testclient import TestClient
from schwab_support import (FAKE_ACCOUNT_NUMBER, FAKE_ACCOUNT_NUMBER_2, FAKE_HASH, FAKE_HASH_2, FAKE_TOKEN, FakeSchwab,
                            clear_fake, load, register_accounts)

from hokma.core.app import create_app
from hokma.i18n import t
from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller
from hokma.rpc.ws import SESSION_TOKEN
from hokma.schwab import accounts, auth, endpoints, http, reader
from hokma.schwab.routes import router

NOW = dt.datetime(2026, 11, 20, 16, 0, tzinfo=dt.timezone.utc)
AS_OF = "2026-11-20T16:00:00.000Z"
AUTH = {"X-Hokma-Token": SESSION_TOKEN}
H1, H2 = FAKE_HASH, FAKE_HASH_2
MARKET = "/marketdata/v1"
TRADER = "/trader/v1"
OPTION = "FAKE  261218C00100000"
LEAKS = (FAKE_HASH, FAKE_HASH_2, FAKE_ACCOUNT_NUMBER, FAKE_ACCOUNT_NUMBER_2, FAKE_TOKEN, "hashValue", "accountNumber",
         "fake-customer-id", "fake-correl-id", "fake-channel", "fake-function-id", "schwabClient", "fake-login",
         "cdDomainId", "systemUserName")

TOOLS_19 = ("schwab_quotes", "schwab_quote", "schwab_option_chain", "schwab_expirations", "schwab_price_history",
            "schwab_movers", "schwab_market_hours", "schwab_instruments", "schwab_instrument_by_cusip",
            "schwab_account_list", "schwab_accounts", "schwab_orders", "schwab_order", "schwab_transactions",
            "schwab_transaction", "schwab_user_preference", "volatility_history",
            "portfolio_greeks", "account_activity")
ACCOUNT_TOOLS = ("schwab_account_list", "schwab_accounts", "schwab_orders", "schwab_order", "schwab_transactions",
                 "schwab_transaction", "schwab_user_preference", "portfolio_greeks", "account_activity")
ARGUMENTS = {
    "schwab_quotes": {"symbols", "fields", "indicative"},
    "schwab_quote": {"symbol", "fields"},
    "schwab_option_chain": {"symbol", "contractType", "strikeCount", "includeUnderlyingQuote", "strategy", "interval",
                            "strike", "range", "fromDate", "toDate", "volatility", "underlyingPrice", "interestRate",
                            "daysToExpiration", "expMonth", "optionType", "entitlement"},
    "schwab_expirations": {"symbol"},
    "schwab_price_history": {"symbol", "periodType", "period", "frequencyType", "frequency", "startDate", "endDate",
                             "needExtendedHoursData", "needPreviousClose"},
    "schwab_movers": {"index", "sort", "frequency"},
    "schwab_market_hours": {"markets", "date"},
    "schwab_instruments": {"symbol", "projection"},
    "schwab_instrument_by_cusip": {"cusip"},
    "schwab_account_list": set(),
    "schwab_accounts": {"account_ref", "fields"},
    "schwab_orders": {"account_ref", "fromEnteredTime", "toEnteredTime", "status", "maxResults"},
    "schwab_order": {"account_ref", "order_id"},
    "schwab_transactions": {"account_ref", "startDate", "endDate", "types", "symbol"},
    "schwab_transaction": {"account_ref", "transaction_id"},
    "schwab_user_preference": set(),
    "volatility_history": {"symbol", "windows"},
    "portfolio_greeks": {"account_ref"},
    "account_activity": {"account_ref", "startDate", "endDate"},
}


# ------------------------------------------------------------------------------------------------------ fixtures


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def candles(count: int = 80) -> dict:
    rows = []
    for i in range(count):
        close = round(100 + 5 * math.sin(i / 3), 4)
        rows.append({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000 + i,
                     "datetime": 1_700_000_000_000 + i * 86_400_000})
    return {"candles": rows, "empty": False, "symbol": "AAPL"}


def option_quotes(call) -> dict:
    symbols = call.query["symbols"].split(",")
    return {s: {"assetMainType": "OPTION", "symbol": s, "realtime": True,
                "quote": {"delta": 0.5, "gamma": 0.1, "theta": -0.05, "vega": 0.2},
                "reference": {"multiplier": 100}} for s in symbols}


def serve(fake: FakeSchwab) -> None:
    register_accounts(fake)
    on = fake.on
    on("GET", MARKET + "/quotes", load("quotes.json"))
    on("GET", MARKET + "/AAPL/quotes", load("quote_single.json"))
    on("GET", MARKET + "/chains", load("chains.json"))
    on("GET", MARKET + "/expirationchain", load("expirationchain.json"))
    on("GET", MARKET + "/pricehistory", candles())
    on("GET", MARKET + "/movers/%24SPX", load("movers.json"))
    on("GET", MARKET + "/markets", load("markets.json"))
    on("GET", MARKET + "/markets/equity", load("market_equity.json"))
    on("GET", MARKET + "/instruments", load("instruments.json"))
    on("GET", MARKET + "/instruments/037833100", load("instrument_cusip.json"))
    on("GET", TRADER + "/accounts", load("accounts.json"))
    on("GET", f"{TRADER}/accounts/{H1}", load("account_margin.json"))
    on("GET", TRADER + "/orders", load("orders.json"))
    on("GET", f"{TRADER}/accounts/{H1}/orders", load("orders.json"))
    on("GET", f"{TRADER}/accounts/{H2}/orders", [])
    on("GET", f"{TRADER}/accounts/{H1}/orders/1000000001", load("order.json"))
    on("GET", f"{TRADER}/accounts/{H1}/transactions", load("transactions.json"))
    on("GET", f"{TRADER}/accounts/{H2}/transactions", [])
    on("GET", f"{TRADER}/accounts/{H1}/transactions/777001", load("transaction.json"))
    on("GET", TRADER + "/userPreference", load("user_preference.json"))


@pytest.fixture(scope="module")
def fake_server():
    with FakeSchwab() as fake:
        yield fake


@pytest.fixture
def fake(fake_server, monkeypatch, env):  # noqa: F811
    clear_fake(fake_server)
    http.reset_limits()
    accounts.reset()
    reader.clear_cache()
    reader.set_clock(Clock())
    endpoints.set_clock(lambda: NOW)
    monkeypatch.setattr(auth, "is_connected", lambda: True)
    serve(fake_server)
    yield fake_server
    reader.set_clock(None)
    reader.clear_cache()
    endpoints.set_clock(None)
    accounts.reset()


@pytest.fixture
def offline(fake, monkeypatch):
    monkeypatch.setattr(auth, "is_connected", lambda: False)
    return fake


@pytest.fixture
def client(fake):
    return TestClient(create_app())


def run(name: str, args: dict | None = None):
    """(parsed answer or text, is_error) of one tool through the real server entry (hokma_tools.call)."""
    text, is_error = hokma_tools.call(name, args or {}, Caller({"HOKMA_AGENT": "hokma"}))
    if is_error:
        return text, True
    try:
        return json.loads(text), False
    except ValueError:
        return text, False


def calls_to(fake: FakeSchwab, path: str) -> list:
    return [c for c in fake.calls if c.path == path]


def assert_clean(text: str) -> None:
    for leak in LEAKS:
        assert leak not in text, leak


def assert_envelope(answer: dict) -> None:
    assert answer["source"] == "Schwab" and answer["as_of"] == AS_OF and "data" in answer


# --------------------------------------------------------------------------------- tools: schema and descriptions


def test_there_are_nineteen_schwab_tools_with_every_argument_of_the_table():
    assert set(ARGUMENTS) == set(TOOLS_19) and len(TOOLS_19) == 19
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    for name in TOOLS_19:
        schema = listed[name]["inputSchema"]
        assert set(schema["properties"]) == ARGUMENTS[name], name
        assert schema["additionalProperties"] is False
        assert hokma_tools.TOOLS[name][4] is False and name in hokma_tools.NO_UNIT        # read only, no database unit


def test_descriptions_say_read_only_and_the_account_ones_say_no_orders_are_ever_sent():
    for name in TOOLS_19:
        text = hokma_tools.TOOLS[name][0]
        assert "read only" in text.lower(), name
        assert ("Hokma never sends, changes or cancels orders" in text) == (name in ACCOUNT_TOOLS), name
        assert not re.search(r"gex|dealer|gamma flip|wall", text, re.I), name     # the exposure metrics are not offered


def test_the_enums_and_types_of_the_schemas_are_the_ones_of_the_reference():
    props = {d["name"]: d["inputSchema"]["properties"] for d in hokma_tools.definitions()}
    chain = props["schwab_option_chain"]
    assert chain["contractType"]["enum"] == ["CALL", "PUT", "ALL"]
    assert "ANALYTICAL" in chain["strategy"]["enum"] and chain["expMonth"]["enum"][-1] == "ALL"
    assert chain["strikeCount"]["type"] == "integer" and chain["strike"]["type"] == "number"
    assert chain["includeUnderlyingQuote"]["type"] == "boolean" and chain["entitlement"]["enum"] == ["PP", "NP", "PN"]
    assert props["schwab_price_history"]["periodType"]["enum"] == ["day", "month", "year", "ytd"]
    assert props["schwab_price_history"]["startDate"]["type"] == "integer"
    assert "$SPX" in props["schwab_movers"]["index"]["enum"] and "PERCENT_CHANGE_UP" in props["schwab_movers"]["sort"]["enum"]
    assert "positions" in props["schwab_accounts"]["fields"]["enum"] and "WORKING" in props["schwab_orders"]["status"]["enum"]
    assert hokma_tools.validate("schwab_movers", {"index": "$SPX", "sort": "NOPE"}).startswith("sort must be one of")
    assert hokma_tools.validate("schwab_quotes", {"symbols": ["AAPL"]}) is None
    assert hokma_tools.validate("schwab_quotes", {}) == "missing argument: symbols"
    assert hokma_tools.validate("schwab_order", {"account_ref": "A1", "order_id": 1000000001}) is None


def test_every_tool_is_pre_allowed_in_the_settings_because_it_only_reads():
    from pathlib import Path

    settings = Path(hokma_tools.__file__).parent / "templates" / "settings.base.json"
    allow = set(json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"])
    assert {f"mcp__hokma__{n}" for n in TOOLS_19} <= allow


def test_the_read_tools_and_the_internal_schwab_routes_never_write():
    """Orders exist since 1.0.9 (section 18), but only as the desk tools `order_*` (broker service, with the desk rules).
    The `schwab_*` tools and the `/api/schwab` routes stay read only."""
    names = set(hokma_tools.TOOLS)
    assert {n for n in names if n.startswith("schwab_") and "order" in n} == {"schwab_orders", "schwab_order"}
    assert not [n for n in names if n.startswith("schwab_") and re.search(r"place|preview|cancel|replace|submit", n)]
    assert {n for n in names if n.startswith("order_")} == {
        "order_preview", "order_place", "order_replace", "order_cancel", "order_status", "order_list", "order_log"}
    # Every route is a GET but one: the measuring run (POST), which opens subscriptions of market data and sends no order (SPEC-016 T1)
    assert all(route.methods == {"GET"} for route in router.routes if route.path != "/api/schwab/diagnostic/streaming")
    assert next(r for r in router.routes if r.path == "/api/schwab/diagnostic/streaming").methods == {"POST"}


# ------------------------------------------------------------------------------- tools: one call per endpoint

CHAIN_ARGS = {"symbol": "AAPL", "contractType": "CALL", "strikeCount": 5, "includeUnderlyingQuote": True,
              "strategy": "ANALYTICAL", "interval": 5, "strike": 100, "range": "ITM", "fromDate": "2026-12-01",
              "toDate": "2026-12-31", "volatility": 29, "underlyingPrice": 118.5, "interestRate": 4.5,
              "daysToExpiration": 30, "expMonth": "DEC", "optionType": "S", "entitlement": "PP"}
FROM, TO = "2026-10-01T00:00:00.000Z", "2026-11-20T00:00:00.000Z"

# tool, arguments, path of the call, query the call must carry
CALLS = [
    ("schwab_quotes", {"symbols": ["AAPL", "$SPX"], "fields": "quote,reference", "indicative": True},
     MARKET + "/quotes", {"symbols": "AAPL,$SPX", "fields": "quote,reference", "indicative": "true"}),
    ("schwab_quote", {"symbol": "AAPL", "fields": "quote"}, MARKET + "/AAPL/quotes", {"fields": "quote"}),
    ("schwab_option_chain", CHAIN_ARGS, MARKET + "/chains",
     {"symbol": "AAPL", "contractType": "CALL", "strikeCount": "5", "includeUnderlyingQuote": "true",
      "strategy": "ANALYTICAL", "interval": "5", "strike": "100", "range": "ITM", "fromDate": "2026-12-01",
      "toDate": "2026-12-31", "volatility": "29", "underlyingPrice": "118.5", "interestRate": "4.5",
      "daysToExpiration": "30", "expMonth": "DEC", "optionType": "S", "entitlement": "PP"}),
    ("schwab_expirations", {"symbol": "AAPL"}, MARKET + "/expirationchain", {"symbol": "AAPL"}),
    ("schwab_price_history", {"symbol": "AAPL", "periodType": "day", "period": 5, "frequencyType": "minute",
                              "frequency": 15, "startDate": 1763000000000, "endDate": 1763652600000,
                              "needExtendedHoursData": True, "needPreviousClose": False},
     MARKET + "/pricehistory", {"symbol": "AAPL", "periodType": "day", "period": "5", "frequencyType": "minute",
                                "frequency": "15", "startDate": "1763000000000", "endDate": "1763652600000",
                                "needExtendedHoursData": "true", "needPreviousClose": "false"}),
    ("schwab_movers", {"index": "$SPX", "sort": "VOLUME", "frequency": 5}, MARKET + "/movers/%24SPX",
     {"sort": "VOLUME", "frequency": "5"}),
    ("schwab_market_hours", {"markets": ["equity", "option"], "date": "2026-11-20"}, MARKET + "/markets",
     {"markets": "equity,option", "date": "2026-11-20"}),
    ("schwab_market_hours", {"markets": ["equity"], "date": "2026-11-20"}, MARKET + "/markets/equity",
     {"date": "2026-11-20"}),
    ("schwab_instruments", {"symbol": "AAPL,BAC", "projection": "symbol-search"}, MARKET + "/instruments",
     {"symbol": "AAPL,BAC", "projection": "symbol-search"}),
    ("schwab_instrument_by_cusip", {"cusip": "037833100"}, MARKET + "/instruments/037833100", {}),
    ("schwab_account_list", {}, TRADER + "/accounts/accountNumbers", {}),
    ("schwab_accounts", {"account_ref": "A1", "fields": "positions"}, f"{TRADER}/accounts/{H1}", {"fields": "positions"}),
    ("schwab_accounts", {"fields": "positions"}, TRADER + "/accounts", {"fields": "positions"}),
    ("schwab_orders", {"account_ref": "A1", "fromEnteredTime": FROM, "toEnteredTime": TO, "status": "WORKING",
                       "maxResults": 10}, f"{TRADER}/accounts/{H1}/orders",
     {"fromEnteredTime": FROM, "toEnteredTime": TO, "status": "WORKING", "maxResults": "10"}),
    ("schwab_orders", {"fromEnteredTime": "2026-11-01T00:00:00.000Z", "toEnteredTime": TO, "status": "FILLED",
                       "maxResults": 5}, TRADER + "/orders",
     {"fromEnteredTime": "2026-11-01T00:00:00.000Z", "toEnteredTime": TO, "status": "FILLED", "maxResults": "5"}),
    ("schwab_order", {"account_ref": "A1", "order_id": 1000000001}, f"{TRADER}/accounts/{H1}/orders/1000000001", {}),
    ("schwab_transactions", {"account_ref": "A1", "startDate": FROM, "endDate": TO,
                             "types": ["TRADE", "DIVIDEND_OR_INTEREST"], "symbol": "FAKE"},
     f"{TRADER}/accounts/{H1}/transactions",
     {"startDate": FROM, "endDate": TO, "types": "TRADE,DIVIDEND_OR_INTEREST", "symbol": "FAKE"}),
    ("schwab_transaction", {"account_ref": "A1", "transaction_id": "777001"},
     f"{TRADER}/accounts/{H1}/transactions/777001", {}),
    ("schwab_user_preference", {}, TRADER + "/userPreference", {}),
]


@pytest.mark.parametrize("name,args,path,query", CALLS, ids=[f"{c[0]}-{i}" for i, c in enumerate(CALLS)])
def test_each_tool_calls_its_endpoint_with_all_its_arguments(db, fake, name, args, path, query):
    answer, is_error = run(name, args)
    assert not is_error, answer
    assert_envelope(answer)
    made = calls_to(fake, path)
    assert len(made) == 1 and made[0].method == "GET"
    for key, value in query.items():
        assert made[0].query.get(key) == value, key
    assert set(made[0].query) == set(query)
    assert made[0].headers["authorization"] == "Bearer " + FAKE_TOKEN
    assert_clean(json.dumps(answer))


def test_the_chain_answer_says_if_it_is_delayed_and_an_expiration_chain_does_not(db, fake):
    chain, _ = run("schwab_option_chain", {"symbol": "AAPL"})
    assert chain["delayed"] is False and chain["data"]["callExpDateMap"]
    quotes, _ = run("schwab_quotes", {"symbols": "AAPL"})
    flags = [v["realtime"] for v in load("quotes.json").values() if isinstance(v, dict) and "realtime" in v]
    assert quotes["delayed"] is (not all(flags)) and "AAPL" in quotes["data"]
    single, _ = run("schwab_quote", {"symbol": "AAPL"})
    assert single["delayed"] is False
    assert "delayed" not in run("schwab_expirations", {"symbol": "AAPL"})[0]


def test_the_account_list_gives_the_reference_and_the_mask_only(db, fake):
    answer, _ = run("schwab_account_list")
    assert answer["data"] == [{"account_ref": "A1", "mask": "••••5678"}, {"account_ref": "A2", "mask": "••••4321"}]
    assert_clean(json.dumps(answer))


def test_account_tools_return_the_reference_and_the_mask_and_never_the_number_or_the_hash(db, fake):
    for name, args in (("schwab_accounts", {"fields": "positions"}), ("schwab_orders", {}),
                       ("schwab_orders", {"account_ref": "A1"}), ("schwab_order", {"account_ref": "A1", "order_id": 1000000001}),
                       ("schwab_transactions", {"account_ref": "A1"}), ("schwab_transaction", {"account_ref": "A1", "transaction_id": 777001}),
                       ("schwab_user_preference", {}), ("portfolio_greeks", {}), ("account_activity", {})):
        answer, is_error = run(name, args)
        assert not is_error, (name, answer)
        assert_envelope(answer)
        assert_clean(json.dumps(answer))
    accounts_answer, _ = run("schwab_accounts", {"fields": "positions"})
    refs = [a["securitiesAccount"]["account_ref"] for a in accounts_answer["data"]]
    masks = [a["securitiesAccount"]["mask"] for a in accounts_answer["data"]]
    assert refs == ["A1", "A2"] and masks == ["••••5678", "••••4321"]
    orders, _ = run("schwab_orders", {})
    assert {o["account_ref"] for o in orders["data"]} == {"A1", "A2"}


def test_user_preference_keeps_the_socket_url_and_none_of_the_other_ids(db, fake):
    answer, _ = run("schwab_user_preference")
    info = answer["data"]["streamerInfo"]
    assert info == [{"streamerSocketUrl": "wss://fake-streamer.example/ws"}]
    assert_clean(json.dumps(answer))
    transaction, _ = run("schwab_transaction", {"account_ref": "A1", "transaction_id": "777001"})
    assert "user" not in json.dumps(transaction["data"]).replace("userId", "") or "cdDomainId" not in json.dumps(transaction)


def test_a_transaction_search_without_types_asks_for_every_type(db, fake):
    run("schwab_transactions", {"account_ref": "A1"})
    made = calls_to(fake, f"{TRADER}/accounts/{H1}/transactions")[0]
    assert made.query["types"].split(",") == list(endpoints.models.enum_values("TransactionType"))


def test_a_bad_argument_is_an_error_result_with_the_catalog_text_and_no_call(db, fake):
    text, is_error = run("schwab_movers", {"index": "$SPX", "frequency": 7})
    assert is_error and "frequency must be one of" in text
    text, is_error = run("schwab_accounts", {"account_ref": FAKE_ACCOUNT_NUMBER})
    assert is_error and t("schwab.err.bad_request") in text and FAKE_ACCOUNT_NUMBER not in text.replace("••••", "")
    assert not calls_to(fake, f"{TRADER}/accounts/{H1}")
    text, is_error = run("schwab_accounts", {"account_ref": H1})
    assert is_error and not calls_to(fake, f"{TRADER}/accounts/{H1}")


@pytest.mark.parametrize("status,code,key", [
    (403, "connections.schwab_denied", "connections.error.schwab_denied"),
    (401, "connections.schwab_expired", "connections.error.schwab_expired"),
    (429, "connections.rate_limited", "connections.error.rate_limited"),
    (404, "schwab.err.not_found", "schwab.err.not_found"),
    (400, "schwab.err.bad_request", "schwab.err.bad_request"),
    (500, "schwab.err.upstream", "schwab.err.upstream")])
def test_a_schwab_error_is_an_error_result_with_the_text_of_its_code(db, fake, status, code, key):
    fake.on("GET", MARKET + "/expirationchain", (status, {"errors": [{"status": str(status), "title": "x"}]}))
    text, is_error = run("schwab_expirations", {"symbol": "AAPL"})
    assert is_error and t(key) in text


# --------------------------------------------------------------------------------- tools: not connected


@pytest.mark.parametrize("name", TOOLS_19)
def test_without_schwab_connected_each_tool_says_so_and_makes_no_call(db, offline, name):
    args = {"symbol": "AAPL", "symbols": "AAPL", "index": "$SPX", "markets": "equity", "projection": "search",
            "cusip": "037833100", "account_ref": "A1", "order_id": 1, "transaction_id": 1}
    answer, is_error = run(name, {k: v for k, v in args.items() if k in ARGUMENTS[name]})
    assert not is_error and answer == t("schwab.err.not_connected")
    assert offline.calls == []


def test_a_session_that_vanishes_in_the_middle_is_also_the_not_connected_text(db, fake, monkeypatch):
    def gone(**params):
        raise http.SchwabError(http.NOT_CONNECTED, 401)

    monkeypatch.setitem(reader.SPECS, "expirationchain", reader.Spec(gone, 0, {"symbol": "str"}, ("symbol",)))
    assert run("schwab_expirations", {"symbol": "AAPL"})[0] == t("schwab.err.not_connected")


# ------------------------------------------------------------------------------------------- the calculations


def hv(closes: list[float], n: int) -> float:
    window = closes[-n:]
    return statistics.stdev([math.log(b / a) for a, b in zip(window, window[1:])]) * math.sqrt(252)


def test_volatility_history_uses_the_daily_candles_and_the_windows_10_20_30_60_by_default(db, fake):
    answer, is_error = run("volatility_history", {"symbol": "AAPL"})
    assert not is_error, answer
    assert_envelope(answer)
    history = calls_to(fake, MARKET + "/pricehistory")[0].query
    assert history["symbol"] == "AAPL" and history["periodType"] == "year" and history["frequencyType"] == "daily"
    closes = [c["close"] for c in candles()["candles"]]
    data = answer["data"]
    assert data["windows"] == [10, 20, 30, 60] and set(data["hv"]) == {"10", "20", "30", "60"}
    for n in (10, 20, 30, 60):
        assert data["hv"][str(n)] == pytest.approx(hv(closes, n), abs=1e-5)
    assert not calls_to(fake, MARKET + "/chains")                                   # historical volatility needs no chain
    assert "iv_atm_30d" not in data and "spread_iv_hv" not in data


def test_volatility_history_rejects_a_window_that_is_too_small_and_makes_no_call(db, fake):
    answer, _ = run("volatility_history", {"symbol": "AAPL", "windows": [10, 20]})
    assert set(answer["data"]["hv"]) == {"10", "20"}
    text, is_error = run("volatility_history", {"symbol": "AAPL", "windows": [2]})
    assert is_error and "windows must be" in text


def test_portfolio_greeks_uses_the_positions_and_the_quotes_of_the_option_symbols(db, fake):
    fake.on("GET", MARKET + "/quotes", option_quotes)
    answer, is_error = run("portfolio_greeks", {})
    assert not is_error, answer
    assert_envelope(answer)
    assert calls_to(fake, TRADER + "/accounts")[0].query == {"fields": "positions"}
    asked = calls_to(fake, MARKET + "/quotes")
    assert len(asked) == 1 and asked[0].query["symbols"] == OPTION
    position = next(p for a in load("accounts.json")[:1] for p in a["securitiesAccount"]["positions"]
                    if p["instrument"]["symbol"] == OPTION)
    net = position.get("longQuantity", 0) - position.get("shortQuantity", 0)
    row = next(r for r in answer["data"]["positions"] if r["symbol"] == OPTION)
    assert row["account"] == "A1" and row["quantity"] == net
    assert row["delta"] == pytest.approx(net * 100 * 0.5) and row["theta"] == pytest.approx(net * 100 * -0.05)
    stock = next(r for r in answer["data"]["positions"] if r["asset_type"] == "EQUITY")
    assert stock["gamma"] == 0 and answer["data"]["missing_greeks"] == []
    assert set(answer["data"]["by_account"]) <= {"A1", "A2"}
    assert_clean(json.dumps(answer))


def test_portfolio_greeks_of_one_account_reads_that_account(db, fake):
    fake.on("GET", MARKET + "/quotes", option_quotes)
    answer, is_error = run("portfolio_greeks", {"account_ref": "A1"})
    assert not is_error and calls_to(fake, f"{TRADER}/accounts/{H1}")[0].query == {"fields": "positions"}
    assert set(answer["data"]["by_account"]) == {"A1"}


def test_account_activity_combines_transactions_and_orders_of_the_range(db, fake):
    answer, is_error = run("account_activity", {"account_ref": "A1", "startDate": "2026-11-01", "endDate": "2026-11-20"})
    assert not is_error, answer
    assert_envelope(answer)
    tx = calls_to(fake, f"{TRADER}/accounts/{H1}/transactions")[0].query
    assert tx["startDate"] == "2026-11-01T00:00:00.000Z" and tx["endDate"] == "2026-11-20T23:59:59.000Z"
    orders = calls_to(fake, f"{TRADER}/accounts/{H1}/orders")[0].query
    assert orders["fromEnteredTime"] == tx["startDate"] and orders["toEnteredTime"] == tx["endDate"]
    data = answer["data"]
    assert data["accounts"] == ["A1"] and data["transactions"] == 2 and data["orders_count"] == 4
    assert data["net_by_type"]["TRADE"] == pytest.approx(282.25) and data["net_by_type"]["DIVIDEND_OR_INTEREST"] == pytest.approx(282.5)
    assert set(data["exposure"]) == {"by_account", "total"} and "A1" in data["exposure"]["by_account"]
    assert_clean(json.dumps(answer))


def test_account_activity_without_an_account_reads_every_account_and_the_last_30_days(db, fake):
    answer, _ = run("account_activity", {})
    assert answer["data"]["accounts"] == ["A1", "A2"]
    tx = calls_to(fake, f"{TRADER}/accounts/{H1}/transactions")[0].query
    assert tx["startDate"] == "2026-10-21T16:00:00.000Z" and tx["endDate"] == AS_OF
    assert calls_to(fake, f"{TRADER}/accounts/{H2}/transactions")


# ------------------------------------------------------------------------------------------------------- cache

TTLS = {"quotes": 2, "quote": 2, "chains": 30, "expirationchain": 15 * 60, "markets": 15 * 60, "instruments": 86400,
        "instrument": 86400, "movers": 60, "accounts": 15, "orders": 30, "order": 30, "transactions": 30,
        "transaction": 30, "user_preference": 3600}


def test_the_cache_times_are_the_ones_of_the_spec():
    assert {name: reader.SPECS[name].ttl for name in TTLS} == TTLS
    assert reader.SPECS["pricehistory"].ttl == 0 and reader.SPECS["account_list"].ttl == 0
    assert all(reader.SPECS[name].ttl == 0 for name in reader.ANALYTICS_READS)


CACHE_CASES = [
    ("quotes", {"symbols": "AAPL"}, MARKET + "/quotes"),
    ("chains", {"symbol": "AAPL"}, MARKET + "/chains"),
    ("expirationchain", {"symbol": "AAPL"}, MARKET + "/expirationchain"),
    ("markets", {"markets": "equity,option"}, MARKET + "/markets"),
    ("instruments", {"symbol": "AAPL", "projection": "search"}, MARKET + "/instruments"),
    ("movers", {"index": "$SPX"}, MARKET + "/movers/%24SPX"),
    ("accounts", {"fields": "positions"}, TRADER + "/accounts"),
    ("orders", {}, TRADER + "/orders"),
    ("user_preference", {}, TRADER + "/userPreference"),
]


@pytest.mark.parametrize("name,params,path", CACHE_CASES, ids=[c[0] for c in CACHE_CASES])
def test_an_endpoint_is_read_once_inside_its_time_and_again_after_it(fake, name, params, path):
    clock = Clock()
    reader.set_clock(clock)
    ttl = TTLS[name]
    first = reader.read(name, params)
    clock.now += ttl - 0.5
    assert reader.read(name, params) == first and len(calls_to(fake, path)) == 1
    clock.now += 1.0
    reader.read(name, params)
    assert len(calls_to(fake, path)) == 2


def test_the_cache_is_per_parameters_and_an_error_is_never_cached(fake):
    reader.read("quotes", {"symbols": "AAPL"})
    reader.read("quotes", {"symbols": "AAPL,$SPX"})
    assert len(calls_to(fake, MARKET + "/quotes")) == 2
    fake.on("GET", MARKET + "/expirationchain", (500, {"errors": []}))
    with pytest.raises(http.SchwabError):
        reader.read("expirationchain", {"symbol": "AAPL"})
    fake.on("GET", MARKET + "/expirationchain", load("expirationchain.json"))
    assert reader.read("expirationchain", {"symbol": "AAPL"})["data"]
    assert len(calls_to(fake, MARKET + "/expirationchain")) == 2


# ------------------------------------------------------------------------------------------------------- the API

# url, query, tool, the arguments of the tool: the same read through both doors
API = [
    ("/api/schwab/marketdata/quotes", {"symbols": "AAPL,$SPX", "fields": "quote", "indicative": "true"},
     "schwab_quotes", {"symbols": "AAPL,$SPX", "fields": "quote", "indicative": True}),
    ("/api/schwab/marketdata/AAPL/quotes", {"fields": "quote"}, "schwab_quote", {"symbol": "AAPL", "fields": "quote"}),
    ("/api/schwab/marketdata/chains", {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in CHAIN_ARGS.items()},
     "schwab_option_chain", CHAIN_ARGS),
    ("/api/schwab/marketdata/expirationchain", {"symbol": "AAPL"}, "schwab_expirations", {"symbol": "AAPL"}),
    ("/api/schwab/marketdata/pricehistory", {"symbol": "AAPL", "periodType": "day", "period": "5",
                                             "frequencyType": "minute", "frequency": "15", "needPreviousClose": "true"},
     "schwab_price_history", {"symbol": "AAPL", "periodType": "day", "period": 5, "frequencyType": "minute",
                              "frequency": 15, "needPreviousClose": True}),
    ("/api/schwab/marketdata/movers/%24SPX", {"sort": "VOLUME", "frequency": "5"}, "schwab_movers",
     {"index": "$SPX", "sort": "VOLUME", "frequency": 5}),
    ("/api/schwab/marketdata/markets", {"markets": "equity,option", "date": "2026-11-20"}, "schwab_market_hours",
     {"markets": ["equity", "option"], "date": "2026-11-20"}),
    ("/api/schwab/marketdata/markets/equity", {"date": "2026-11-20"}, "schwab_market_hours",
     {"markets": ["equity"], "date": "2026-11-20"}),
    ("/api/schwab/marketdata/instruments", {"symbol": "AAPL,BAC", "projection": "symbol-search"}, "schwab_instruments",
     {"symbol": "AAPL,BAC", "projection": "symbol-search"}),
    ("/api/schwab/marketdata/instruments/037833100", {}, "schwab_instrument_by_cusip", {"cusip": "037833100"}),
    ("/api/schwab/accounts/accountNumbers", {}, "schwab_account_list", {}),
    ("/api/schwab/accounts", {"fields": "positions"}, "schwab_accounts", {"fields": "positions"}),
    ("/api/schwab/accounts/A1", {"fields": "positions"}, "schwab_accounts", {"account_ref": "A1", "fields": "positions"}),
    ("/api/schwab/orders", {"status": "FILLED", "maxResults": "5"}, "schwab_orders", {"status": "FILLED", "maxResults": 5}),
    ("/api/schwab/accounts/A1/orders", {"fromEnteredTime": FROM, "toEnteredTime": TO, "status": "WORKING"},
     "schwab_orders", {"account_ref": "A1", "fromEnteredTime": FROM, "toEnteredTime": TO, "status": "WORKING"}),
    ("/api/schwab/accounts/A1/orders/1000000001", {}, "schwab_order", {"account_ref": "A1", "order_id": 1000000001}),
    ("/api/schwab/accounts/A1/transactions", {"startDate": FROM, "endDate": TO, "types": "TRADE,DIVIDEND_OR_INTEREST",
                                              "symbol": "FAKE"},
     "schwab_transactions", {"account_ref": "A1", "startDate": FROM, "endDate": TO,
                             "types": ["TRADE", "DIVIDEND_OR_INTEREST"], "symbol": "FAKE"}),
    ("/api/schwab/accounts/A1/transactions/777001", {}, "schwab_transaction", {"account_ref": "A1", "transaction_id": 777001}),
    ("/api/schwab/userPreference", {}, "schwab_user_preference", {}),
    ("/api/schwab/analytics/volatility_history", {"symbol": "AAPL", "windows": "10,20"}, "volatility_history",
     {"symbol": "AAPL", "windows": [10, 20]}),
    ("/api/schwab/analytics/portfolio_greeks", {"account_ref": "A1"}, "portfolio_greeks", {"account_ref": "A1"}),
    ("/api/schwab/analytics/account_activity", {"account_ref": "A1", "startDate": "2026-11-01", "endDate": "2026-11-20"},
     "account_activity", {"account_ref": "A1", "startDate": "2026-11-01", "endDate": "2026-11-20"}),
]


@pytest.mark.parametrize("url,query,tool,args", API, ids=[a[0].removeprefix("/api/schwab") for a in API])
def test_each_route_answers_the_same_as_its_tool(db, fake, client, url, query, tool, args):
    fake.on("GET", MARKET + "/quotes", option_quotes) if tool == "portfolio_greeks" else None
    answer = client.get(url, params=query, headers=AUTH)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert_envelope(body)
    assert_clean(answer.text)
    reader.clear_cache()
    via_tool, is_error = run(tool, args)
    assert not is_error and body == via_tool


def test_the_routes_cover_the_19_read_endpoints_and_the_calculations_only():
    paths = {r.path for r in router.routes}
    assert len([p for p in paths if not p.startswith(("/api/schwab/analytics", "/api/schwab/diagnostic"))]) == 19
    assert "/api/schwab/analytics/{calc}" in paths
    assert "/api/schwab/diagnostic/streaming" in paths                     # the measuring tool of SPEC-016: read only
    assert not [p for p in paths if re.search(r"preview|place|cancel|replace", p, re.I)]
    assert set(reader.ANALYTICS_READS) == {"volatility_history", "portfolio_greeks", "account_activity",
                                           "exposure_and_pnl", "market_hours"}
    assert not [n for n in reader.SPECS if re.search(r"gex|flip|wall|dex|vex|tex|max_pain|put_call|skew|term_structure|"
                                                     r"unusual|options_analytics|expected_move|prob_itm", n)]


def test_the_diagnostic_route_is_a_post_and_needs_the_session_token_in_the_header(fake, client):
    route = next(r for r in router.routes if r.path == "/api/schwab/diagnostic/streaming")
    assert route.methods == {"POST"}                                              # it opens subscriptions: not a GET
    assert client.get("/api/schwab/diagnostic/streaming", headers={"X-Hokma-Token": SESSION_TOKEN}).status_code in (404, 405)
    for headers in ({}, {"X-Hokma-Token": "not-the-token"}):
        answer = client.post("/api/schwab/diagnostic/streaming", headers=headers, json={})
        assert answer.status_code == 401 and answer.json()["detail"]["code"] == "unauthorized"
    assert client.post("/api/schwab/diagnostic/streaming?token=" + SESSION_TOKEN, json={}).status_code == 401
    assert fake.calls == []


def test_the_router_is_registered_in_the_app():
    app = create_app()
    assert any(getattr(r, "path", "").startswith("/api/schwab/") for r in app.routes)


@pytest.mark.parametrize("url", [a[0] for a in API])
def test_a_request_without_the_session_token_is_refused_before_any_call(db, fake, client, url):
    for headers in ({}, {"X-Hokma-Token": "not-the-token"}):
        answer = client.get(url, headers=headers)
        assert answer.status_code == 401 and answer.json()["detail"]["code"] == "unauthorized"
    assert fake.calls == []


def test_the_diagnostic_route_answers_busy_with_a_409_and_refuses_a_bad_body_with_a_422(fake, client, monkeypatch):
    from hokma.schwab import diagnostic

    async def busy(*args, **kwargs):
        raise diagnostic.DiagnosticBusy()

    monkeypatch.setattr(diagnostic, "run", busy)
    headers = {"X-Hokma-Token": SESSION_TOKEN}
    answer = client.post("/api/schwab/diagnostic/streaming", headers=headers, json={"symbol": "QQQ", "seconds": 75})
    assert answer.status_code == 409 and answer.json()["detail"]["code"] == "schwab.err.diagnostic_busy"
    assert client.post("/api/schwab/diagnostic/streaming", headers=headers, json={"symbol": 5, "seconds": "x"}).status_code == 422


def test_the_token_in_the_address_is_not_accepted_only_the_header_is(fake, client):
    """A token in a query string is kept by logs and browser histories: these routes read the header only."""
    answer = client.get("/api/schwab/marketdata/expirationchain", params={"symbol": "AAPL", "token": SESSION_TOKEN})
    assert answer.status_code == 401 and answer.json()["detail"]["code"] == "unauthorized"
    assert fake.calls == []
    assert client.get("/api/schwab/marketdata/expirationchain", params={"symbol": "AAPL"}, headers={"X-Hokma-Token": SESSION_TOKEN}).status_code == 200


def test_a_foreign_origin_is_refused_like_in_the_rest_of_the_api(fake, client):
    answer = client.get("/api/schwab/userPreference", headers={**AUTH, "Origin": "http://evil.example"})
    assert answer.status_code == 403 and fake.calls == []


@pytest.mark.parametrize("url", [a[0] for a in API])
def test_without_schwab_connected_every_route_says_so(db, offline, client, url):
    answer = client.get(url, headers=AUTH)
    assert answer.status_code == 409
    assert answer.json()["detail"] == {"code": "schwab.err.not_connected", "message": t("schwab.err.not_connected")}
    assert offline.calls == []


def test_an_account_is_only_a_reference_never_a_number_or_a_hash(fake, client):
    for ref in (FAKE_ACCOUNT_NUMBER, H1, "a1", "A99", "A1%2F..%2F..", "A1;x"):
        answer = client.get(f"/api/schwab/accounts/{ref}/orders", headers=AUTH)
        assert answer.status_code in (404, 422), (ref, answer.status_code)
        assert_clean(answer.text)
    assert not [c for c in fake.calls if "/orders" in c.path or H1 in c.path]


def test_a_query_that_names_an_account_number_or_a_hash_is_refused(fake, client):
    for key in ("accountNumber", "hashValue", "hash", "account"):
        answer = client.get("/api/schwab/orders", params={key: H1}, headers=AUTH)
        assert answer.status_code == 422 and answer.json()["detail"]["code"] == "schwab.err.invalid_param"
    assert not calls_to(fake, TRADER + "/orders")


def test_no_method_but_get_reaches_an_order_or_anything_else(fake, client):
    order = "/api/schwab/accounts/A1/orders"
    for method, url in (("post", order), ("put", order + "/1000000001"), ("delete", order + "/1000000001"),
                        ("patch", order + "/1000000001"), ("post", order + "/preview"),
                        ("post", "/api/schwab/accounts/A1/previewOrder"), ("post", "/api/schwab/orders"),
                        ("post", "/api/schwab/marketdata/quotes")):
        answer = client.request(method.upper(), url, headers=AUTH, json={"orderType": "MARKET"})
        assert answer.status_code in (404, 405), (method, url, answer.status_code)
    assert [c for c in fake.calls if c.method != "GET"] == []


def test_the_door_itself_refuses_a_write_to_an_order_route(fake):
    for method, path in (("POST", f"{TRADER}/accounts/{H1}/orders"), ("PUT", f"{TRADER}/accounts/{H1}/orders/1"),
                         ("DELETE", f"{TRADER}/accounts/{H1}/orders/1"),
                         ("POST", f"{TRADER}/accounts/{H1}/previewOrder")):
        with pytest.raises(http.SchwabError) as refused:
            http.request(method, path, None, body=b"{}")
        assert refused.value.code == http.FORBIDDEN_ROUTE
    assert fake.calls == []


@pytest.mark.parametrize("status,code,key,http_status", [
    (403, "connections.schwab_denied", "connections.error.schwab_denied", 403),
    (401, "connections.schwab_expired", "connections.error.schwab_expired", 401),
    (429, "connections.rate_limited", "connections.error.rate_limited", 429),
    (404, "schwab.err.not_found", "schwab.err.not_found", 404),
    (400, "schwab.err.bad_request", "schwab.err.bad_request", 400),
    (500, "schwab.err.upstream", "schwab.err.upstream", 502)])
def test_a_schwab_error_is_code_and_catalog_text(fake, client, status, code, key, http_status):
    fake.on("GET", MARKET + "/expirationchain", (status, {"errors": [{"status": str(status), "title": "x", "detail": "d"}]}))
    answer = client.get("/api/schwab/marketdata/expirationchain", params={"symbol": "AAPL"}, headers=AUTH)
    assert answer.status_code == http_status
    detail = answer.json()["detail"]
    assert answer.json().keys() == {"detail"} and detail["code"] == code
    if code in reader.REASON_CODES:                                             # a refusal carries the reason of Schwab (C4)
        assert detail["message"].startswith(t(key)) and "x: d" in detail["message"]
    else:
        assert detail["message"] == t(key)                                      # the sign-in states keep their own text


def test_a_refused_parameter_says_which_and_why_in_the_catalog_text(fake, client):
    answer = client.get("/api/schwab/marketdata/chains", headers=AUTH)
    assert answer.status_code == 422
    assert answer.json()["detail"] == {"code": "schwab.err.invalid_param",
                                       "message": t("schwab.err.invalid_param", name="symbol", rule="given")}
    answer = client.get("/api/schwab/marketdata/movers/%24SPX", params={"sort": "NOPE"}, headers=AUTH)
    assert answer.status_code == 422 and answer.json()["detail"]["message"].startswith("sort must be one of")
    answer = client.get("/api/schwab/marketdata/chains", params={"symbol": "AAPL", "strikeCount": "many"}, headers=AUTH)
    assert answer.status_code == 422 and "strikeCount" in answer.json()["detail"]["message"]
    assert fake.calls == []


def test_an_unknown_calculation_is_refused_and_the_positioning_ones_do_not_exist(fake, client):
    for calc in ("gex", "gamma_flip", "walls", "dex", "vex", "tex", "options_analytics", "max_pain", "put_call", "skew",
                 "term_structure", "unusual_volume", "expected_move", "prob_itm", "nope"):
        answer = client.get(f"/api/schwab/analytics/{calc}", params={"symbol": "SPY"}, headers=AUTH)
        assert answer.status_code == 404 and answer.json()["detail"]["code"] == "schwab.err.invalid_param"
    assert fake.calls == []


def test_the_answers_keep_the_user_preference_socket_url_and_nothing_else_of_the_streamer(fake, client):
    answer = client.get("/api/schwab/userPreference", headers=AUTH)
    body = answer.json()["data"]
    assert body["streamerInfo"] == [{"streamerSocketUrl": "wss://fake-streamer.example/ws"}]
    assert [a["account_ref"] for a in body["accounts"]] == ["A1", "A2"]
    assert_clean(answer.text)

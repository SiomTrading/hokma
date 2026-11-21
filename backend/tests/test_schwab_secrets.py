"""Nothing secret leaves Hokma (SPEC-015 5.8 and 5.9): the Client ID and Secret, the access and refresh tokens, the account
numbers, the hashes, the identifiers of UserDetails and of streamerInfo never show in the output of a tool, in a log, in an
answer of /api/schwab or /api/connections/schwab, in the text of an error, in the session card or in a file.

A fake Schwab serves the fixtures of schwab_fixtures with a unique, made-up sentinel value in each of those fields; the
test walks every tool and every route with the logs at DEBUG and fails if one sentinel (or a token of the fake sign-in)
shows anywhere. Nothing opens the network; the credential store is in memory."""
import inspect
import json
import logging
import re
import threading
import urllib.parse
from pathlib import Path

import keyring
import pytest
from fastapi.testclient import TestClient
from live_support import MemoryKeyring
from schwab_support import (FAKE_ACCOUNT_NUMBER, FAKE_ACCOUNT_NUMBER_2, FAKE_HASH, FAKE_HASH_2, FakeSchwab, load)

from dbsupport import db, env  # noqa: F401  (fixtures)
from hokma.connections import runtime
from hokma.connections.service import ConnectionService
from hokma.connections.store import ConnectionSettings
from hokma.core.app import create_app
from hokma.core.config import hokma_data_dir
from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller
from hokma.market.providers import get_catalog
from hokma.rpc.ws import SESSION_TOKEN
from hokma.schwab import accounts, auth, endpoints, http, reader
from hokma.schwab import routes as schwab_routes
from hokma.schwab.http import SchwabError

MARKET, TRADER = "/marketdata/v1", "/trader/v1"
AUTH = {"X-Hokma-Token": SESSION_TOKEN}
T0 = 1_800_000_000
REDIRECT = "https://127.0.0.1:8182"
TOKEN_PATH = "/v1/oauth/token"

# ---- the made-up values: each one unique, none of them a value that any fixture uses by itself
ACC1, ACC2 = "73910482", "28460193"
HASH1, HASH2 = "9F8E7D6C5B4A39281706F5E4D3C2B1A0", "0A1B2C3D4E5F60718293A4B5C6D7E8F9"
CLIENT_ID, CLIENT_SECRET = "sent-client-id-4417", "sent-client-secret-9921"
CODE = "C0.sent-code-3375@"
USER = {"cdDomainId": "SENT-CDDOMAIN-5101", "login": "sent-login-5102", "userId": 48301957,
        "systemUserName": "sent-sysuser-5104", "brokerRepCode": "SENTREP5105"}
STREAMER = {"schwabClientCustomerId": "sent-customer-6101", "schwabClientCorrelId": "sent-correl-6102",
            "schwabClientChannel": "sent-channel-6103", "schwabClientFunctionId": "sent-function-6104"}
KEYED = {**USER, **STREAMER}
SWAP = {FAKE_ACCOUNT_NUMBER: ACC1, FAKE_ACCOUNT_NUMBER_2: ACC2, FAKE_HASH: HASH1, FAKE_HASH_2: HASH2}
DATA_SECRETS = (ACC1, ACC2, HASH1, HASH2, *(str(v) for v in KEYED.values()))
SESSION_SECRETS = (CLIENT_ID, CLIENT_SECRET, CODE, "ID-TOKEN-SECRET")
TOKEN_PATTERN = re.compile(r"(?:AT|RT)-SECRET-\d+")
SECRETS = DATA_SECRETS + SESSION_SECRETS


def inject(node):
    """A fixture with the sentinels where the real answers carry the account numbers, hashes and identifiers."""
    if isinstance(node, dict):
        return {key: (KEYED[key] if key in KEYED else inject(value)) for key, value in node.items()}
    if isinstance(node, list):
        return [inject(item) for item in node]
    return SWAP.get(node, node) if isinstance(node, str) else node


def sentinel(name: str):
    return inject(load(name))


def leaks(text: str, *, auth_url: bool = False) -> list[str]:
    """Every secret value found in the text. The authorization address has to carry the Client ID (Schwab reads it there)."""
    found = [s for s in SECRETS if s in text and not (auth_url and s == CLIENT_ID)]
    found += TOKEN_PATTERN.findall(text)
    return found


def assert_clean(text: str, label: str, *, auth_url: bool = False) -> None:
    assert not leaks(text, auth_url=auth_url), f"{label} leaks {leaks(text, auth_url=auth_url)}"


# ------------------------------------------------------------------------------------------------------- the world


class Issuer:
    """The token endpoint of the fake: numbered tokens, or a refusal that echoes every secret (a hostile server)."""

    def __init__(self) -> None:
        self.n = 0
        self.refuse = False
        self.lock = threading.Lock()

    def __call__(self, call):
        if self.refuse:
            echo = " ".join(SECRETS)
            return 400, {"error": "invalid_grant", "error_description": echo, "errors": [{"title": echo}]}
        with self.lock:
            self.n += 1
            n = self.n
        return 200, {"access_token": f"AT-SECRET-{n}", "refresh_token": f"RT-SECRET-{n}", "expires_in": 1800,
                     "token_type": "Bearer", "scope": "api", "id_token": "ID-TOKEN-SECRET"}


class Clock:
    def __init__(self) -> None:
        self.at = float(T0)

    def __call__(self) -> float:
        return self.at


def candles(count: int = 80) -> dict:
    rows = [{"open": 100.0 + i % 7, "high": 108.0, "low": 95.0, "close": 100.0 + i % 7, "volume": 1000 + i,
             "datetime": 1_700_000_000_000 + i * 86_400_000} for i in range(count)]
    return {"candles": rows, "empty": False, "symbol": "AAPL"}


def option_quotes(call) -> dict:
    return {s: {"assetMainType": "OPTION", "symbol": s, "realtime": True,
                "quote": {"delta": 0.5, "gamma": 0.1, "theta": -0.05, "vega": 0.2}, "reference": {"multiplier": 100}}
            for s in call.query["symbols"].split(",")}


def serve(fake: FakeSchwab, hostile: int | None = None) -> None:
    """Every read endpoint with the sentinel fixtures. With `hostile` (an HTTP status), every one of them refuses and the
    refusal echoes every secret in its title, detail and message."""
    echo = " ".join(SECRETS) + " AT-SECRET-1 RT-SECRET-1"

    def answer(body):
        if hostile is None:
            return body
        return lambda call: (hostile, {"errors": [{"status": str(hostile), "title": echo, "detail": echo}],
                                       "message": echo, "error_description": echo})

    on = lambda method, path, body: fake.on(method, path, answer(body))        # noqa: E731
    fake.on("GET", f"{TRADER}/accounts/accountNumbers", sentinel("account_numbers.json"))
    on("GET", MARKET + "/quotes", lambda call: option_quotes(call) if "C00" in call.query.get("symbols", "")
       else sentinel("quotes.json"))
    on("GET", MARKET + "/AAPL/quotes", sentinel("quote_single.json"))
    on("GET", MARKET + "/chains", sentinel("chains.json"))
    on("GET", MARKET + "/expirationchain", sentinel("expirationchain.json"))
    on("GET", MARKET + "/pricehistory", candles())
    on("GET", MARKET + "/movers/%24SPX", sentinel("movers.json"))
    on("GET", MARKET + "/markets", sentinel("markets.json"))
    on("GET", MARKET + "/markets/equity", sentinel("market_equity.json"))
    on("GET", MARKET + "/instruments", sentinel("instruments.json"))
    on("GET", MARKET + "/instruments/037833100", sentinel("instrument_cusip.json"))
    on("GET", TRADER + "/accounts", sentinel("accounts.json"))
    on("GET", f"{TRADER}/accounts/{HASH1}", sentinel("account_margin.json"))
    on("GET", f"{TRADER}/accounts/{HASH2}", sentinel("account_cash.json"))
    on("GET", TRADER + "/orders", sentinel("orders.json"))
    for h in (HASH1, HASH2):
        on("GET", f"{TRADER}/accounts/{h}/orders", sentinel("orders.json"))
        on("GET", f"{TRADER}/accounts/{h}/transactions", sentinel("transactions.json"))
    on("GET", f"{TRADER}/accounts/{HASH1}/orders/1000000001", sentinel("order.json"))
    on("GET", f"{TRADER}/accounts/{HASH1}/transactions/777001", sentinel("transaction.json"))
    on("GET", TRADER + "/userPreference", sentinel("user_preference.json"))


class World:
    pass


@pytest.fixture
def world(env, db, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    previous = keyring.get_keyring()
    ring = MemoryKeyring()
    keyring.set_keyring(ring)
    ring.set_password("hokma-market", "schwab", json.dumps({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}))
    service = ConnectionService(get_catalog(), settings=ConnectionSettings(hokma_data_dir() / "connections.json"))
    runtime.install(service, None)
    clock = Clock()
    auth.reset()
    auth.set_clock(clock)
    auth.set_notifier(lambda text: None)
    accounts.reset()
    reader.clear_cache()
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: opened.append(url) or True)
    with FakeSchwab() as fake:
        http.set_token_provider(None)                          # the real wiring: the token comes from auth
        w = World()
        w.fake, w.issuer, w.ring, w.clock, w.caplog, w.service, w.opened = fake, Issuer(), ring, clock, caplog, service, opened
        fake.on("POST", TOKEN_PATH, w.issuer)
        serve(fake)
        w.client = TestClient(create_app())
        yield w
    auth.set_notifier(None)
    auth.set_clock(None)
    auth.reset()
    accounts.reset()
    reader.clear_cache()
    runtime.reset()
    keyring.set_keyring(previous)


def state_of(url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]


def callback(url: str) -> str:
    return f"{REDIRECT}/?code={urllib.parse.quote(CODE)}&session=abc&state={state_of(url)}"


@pytest.fixture
def signed_in(world):
    url = auth.start_login()["auth_url"]
    auth.finish_login(callback(url))
    reader.clear_cache()
    return world


# ------------------------------------------------------------------------------------------ what to walk

TOOL_ARGS = {
    "schwab_quotes": {"symbols": ["AAPL", "MSFT"]}, "schwab_quote": {"symbol": "AAPL"},
    "schwab_option_chain": {"symbol": "AAPL"}, "schwab_expirations": {"symbol": "AAPL"},
    "schwab_price_history": {"symbol": "AAPL"}, "schwab_movers": {"index": "$SPX"},
    "schwab_market_hours": {"markets": ["equity"]},
    "schwab_instruments": {"symbol": "AAPL", "projection": "symbol-search"},
    "schwab_instrument_by_cusip": {"cusip": "037833100"}, "schwab_account_list": {},
    "schwab_accounts": {"fields": "positions"}, "schwab_orders": {}, "schwab_order": {"account_ref": "A1", "order_id": 1000000001},
    "schwab_transactions": {"account_ref": "A1"},
    "schwab_transaction": {"account_ref": "A1", "transaction_id": 777001}, "schwab_user_preference": {},
    "volatility_history": {"symbol": "AAPL"}, "portfolio_greeks": {}, "account_activity": {}}
EXTRA_CALLS = [("schwab_accounts", {"account_ref": "A1", "fields": "positions"}),
               ("schwab_accounts", {"account_ref": "A2"}), ("schwab_orders", {"account_ref": "A2"}),
               ("schwab_transactions", {"account_ref": "A2"}), ("portfolio_greeks", {"account_ref": "A1"}),
               ("account_activity", {"account_ref": "A2"})]
PATH_VALUES = {"symbol": "AAPL", "index": "%24SPX", "market_id": "equity", "cusip": "037833100", "account_ref": "A1",
               "order_id": "1000000001", "transaction_id": "777001"}
ANALYTICS_QUERY = {"volatility_history": {"symbol": "AAPL"}}


def tool_names() -> list[str]:
    return sorted(n for n in hokma_tools.TOOLS if n.startswith("schwab_") or n in
                  ("volatility_history", "portfolio_greeks", "account_activity"))


def run_tools(w: World) -> list[tuple[str, str]]:
    out = []
    for name, args in [*TOOL_ARGS.items(), *EXTRA_CALLS]:
        reader.clear_cache()
        text, is_error = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": "hokma"}))
        out.append((f"tool {name} {json.dumps(args)}", text))
    return out


def run_routes(w: World) -> list[tuple[str, str]]:
    out = []
    urls = []
    for path, _name, params in schwab_routes.ROUTES:
        urls.append("/api/schwab" + re.sub(r"\{(\w+)\}", lambda m: PATH_VALUES[m.group(1)], path))
    urls.append("/api/schwab/accounts/A2/orders")
    urls.append("/api/schwab/accounts/A2")
    urls += [f"/api/schwab/analytics/{name}" for name in reader.ANALYTICS_READS]
    for url in urls:
        reader.clear_cache()
        query = ANALYTICS_QUERY.get(url.rsplit("/", 1)[-1], {}) if "/analytics/" in url else {}
        answer = w.client.get(url, params=query, headers=AUTH)
        out.append((f"route {url} -> {answer.status_code}", answer.text))
    for url in ("/api/connections", "/api/connections/schwab/account"):
        answer = w.client.get(url, headers=AUTH)
        out.append((f"route {url} -> {answer.status_code}", answer.text))
    out.append(("session card", json.dumps(auth.session_card(), default=str)))
    return out


def check_all(w: World, results: list[tuple[str, str]]) -> None:
    for label, text in results:
        assert_clean(text, label)
    assert_clean(w.caplog.text, "the logs")
    for record in w.caplog.records:
        assert_clean(record.getMessage(), f"log {record.name}")


# --------------------------------------------------------------------------------------------------------- tests


def test_the_sentinels_do_reach_the_raw_reads_so_the_walk_proves_something(signed_in):
    """Without this, a clean walk could mean the fake never sent the secrets."""
    assert accounts.hash_for("A1") == HASH1 and accounts.hash_for("A2") == HASH2
    raw = json.dumps(reader.models.dump(endpoints.user_preference()))
    for value in STREAMER.values():
        assert value in raw
    raw = json.dumps(reader.models.dump(endpoints.accounts("A1")), default=str)
    assert ACC1 in raw
    raw = json.dumps(reader.models.dump(endpoints.transactions("A1", types="TRADE")), default=str)
    for value in USER.values():
        assert str(value) in raw


def test_the_walk_covers_every_tool_and_every_route(signed_in):
    assert len(tool_names()) == 19 and set(TOOL_ARGS) == set(tool_names())
    assert len(schwab_routes.ROUTES) == 19


def test_no_tool_prints_a_secret_and_none_logs_one(signed_in):
    results = run_tools(signed_in)
    assert len(results) == 19 + len(EXTRA_CALLS)
    assert sum(1 for _, text in results if text.lstrip().startswith("{")) >= 15        # real answers, not only errors
    check_all(signed_in, results)


def test_no_route_prints_a_secret_and_none_logs_one(signed_in):
    results = run_routes(signed_in)
    assert sum(1 for label, _ in results if label.endswith("-> 200")) >= 20
    check_all(signed_in, results)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500])
def test_an_error_that_echoes_every_secret_shows_none_of_them(signed_in, status):
    """A hostile or careless server puts the tokens, hashes and ids in its own error text: Hokma answers with the catalog."""
    w = signed_in
    serve(w.fake, hostile=status)
    results = []
    for name, args in [(n, a) for n, a in TOOL_ARGS.items()] + EXTRA_CALLS:
        http.reset_limits()
        reader.clear_cache()
        accounts.reset()
        text, is_error = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": "hokma"}))
        results.append((f"tool {name} with {status}", text))
    for label, text in run_routes(w):
        results.append((label + f" with {status}", text))
    check_all(w, results)
    for call in (endpoints.quote, endpoints.expirations):
        http.reset_limits()
        with pytest.raises(SchwabError) as caught:
            call("AAPL")
        error = caught.value
        for text in (str(error), repr(error), str(error.detail or ""), reader.message_of(error)):
            assert_clean(text, f"SchwabError {error.code}")


def test_a_refused_sign_in_that_echoes_every_secret_shows_none_of_them(world):
    w = world
    w.issuer.refuse = True
    url = auth.start_login()["auth_url"]
    with pytest.raises(SchwabError) as caught:
        auth.finish_login(callback(url))
    error = caught.value
    for text in (str(error), repr(error), str(error.detail or ""), reader.message_of(error)):
        assert_clean(text, "the sign-in error", auth_url=False)
    started = w.client.post("/api/connections/schwab/login/start", json={}, headers=AUTH)
    assert started.status_code == 200
    assert_clean(started.text, "login/start", auth_url=True)
    answer = w.client.post("/api/connections/schwab/login/finish", json={"redirect_url": callback(started.json()["auth_url"])},
                           headers=AUTH)
    assert answer.status_code >= 400
    assert_clean(answer.text, "login/finish refused")
    assert_clean(json.dumps(auth.session_card()), "the session card")
    check_all(w, [])


def test_the_connections_routes_of_the_sign_in_show_no_secret(world):
    w = world
    started = w.client.post("/api/connections/schwab/login/start", json={}, headers=AUTH)
    assert started.status_code == 200
    assert_clean(started.text, "login/start", auth_url=True)
    url = started.json()["auth_url"]
    opened = w.client.post("/api/connections/schwab/login/open", json={"url": url}, headers=AUTH)
    assert opened.status_code == 200 and w.opened == [url]
    assert_clean(opened.text, "login/open")
    finished = w.client.post("/api/connections/schwab/login/finish", json={"redirect_url": callback(url)}, headers=AUTH)
    assert finished.status_code == 200, finished.text
    assert_clean(finished.text, "login/finish")
    card = auth.session_card()
    assert card["state"] == "signed_in" and [a["account_ref"] for a in card["accounts"]] == ["A1", "A2"]
    assert_clean(json.dumps(card), "the session card")
    for url in ("/api/connections", "/api/connections/schwab/account"):
        answer = w.client.get(url, headers=AUTH)
        assert answer.status_code == 200, answer.text
        assert_clean(answer.text, url)
    check_all(w, [])


def test_the_credential_store_is_the_only_place_that_keeps_the_session_and_it_keeps_no_account_data(signed_in):
    stored = {key: value for key, value in signed_in.ring.items.items()}
    assert any(CLIENT_ID in value for value in stored.values())
    assert any(TOKEN_PATTERN.search(value) for value in stored.values())
    for key, value in stored.items():
        for secret in DATA_SECRETS:
            assert secret not in value, f"{key} holds {secret}"


def test_no_file_holds_a_secret(signed_in, tmp_path):
    """Every file under the data, work and home folders of the run (the database, connections.json, the logs, the
    caches), after the whole walk: the credential store is memory here, so nothing may be found on disk."""
    run_tools(signed_in)
    run_routes(signed_in)
    signed_in.client.get("/api/connections", headers=AUTH)
    files = [p for p in Path(tmp_path).rglob("*") if p.is_file()]
    assert files, "the run left no files: the scan would prove nothing"
    needles = [s.encode() for s in SECRETS] + [b"AT-SECRET-", b"RT-SECRET-"]
    for path in files:
        data = path.read_bytes()
        for needle in needles:
            assert needle not in data, f"{path.name} holds {needle.decode()}"


def test_the_account_table_and_the_streamer_ids_live_only_in_memory(signed_in):
    source = inspect.getsource(accounts)
    for call in ("write_text", "open(", "json.dump(", "pickle", "sqlite", "keyring", "set_password"):
        assert call not in source, call
    assert accounts.listing() == [{"account_ref": "A1", "mask": "••••0482"}, {"account_ref": "A2", "mask": "••••0193"}]
    streamer = reader.read("user_preference")["data"]["streamerInfo"]
    assert streamer == [{"streamerSocketUrl": "wss://fake-streamer.example/ws"}]

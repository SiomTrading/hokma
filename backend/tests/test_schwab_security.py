"""The lock around Schwab (SPEC-015 5.8): one door, read-only, no order ever leaves, nothing secret in a log. The network is
a local fake or is not opened at all."""
import json
import logging
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from hardblock_support import box  # noqa: F401  (fixture)
from schwab_support import FAKE_ACCOUNT_NUMBER, FAKE_HASH, FAKE_TOKEN, FakeSchwab

import hokma
from hokma.kit import hardblock
from hokma.schwab import http
from hokma.schwab.http import SchwabError

ROOT = Path(hokma.__file__).parent
HOST = "api.schwabapi.com"
H = FAKE_HASH
WRITES = ("POST", "PUT", "PATCH", "DELETE")

READS = [
    ("GET", "/marketdata/v1/quotes"), ("GET", "/marketdata/v1/SPY/quotes"), ("GET", "/marketdata/v1/BRK.B/quotes"),
    ("GET", "/marketdata/v1/%24SPX/quotes"), ("GET", "/marketdata/v1/chains"), ("GET", "/marketdata/v1/expirationchain"),
    ("GET", "/marketdata/v1/pricehistory"), ("GET", "/marketdata/v1/movers/%24SPX"), ("GET", "/marketdata/v1/markets"),
    ("GET", "/marketdata/v1/markets/equity"), ("GET", "/marketdata/v1/instruments"),
    ("GET", "/marketdata/v1/instruments/037833100"), ("GET", "/trader/v1/accounts/accountNumbers"),
    ("GET", "/trader/v1/accounts"), ("GET", f"/trader/v1/accounts/{H}"), ("GET", f"/trader/v1/accounts/{H}/orders"),
    ("GET", f"/trader/v1/accounts/{H}/orders/123"), ("GET", "/trader/v1/orders"),
    ("GET", f"/trader/v1/accounts/{H}/transactions"), ("GET", f"/trader/v1/accounts/{H}/transactions/456"),
    ("GET", "/trader/v1/userPreference"), ("POST", "/v1/oauth/token"),
]

ORDER_PATHS = ["/trader/v1/accounts/x/orders", "/trader/v1/accounts/x/orders/1", "/trader/v1/accounts/x/previewOrder",
               "/trader/v1/orders", f"/trader/v1/accounts/{H}/orders", f"/trader/v1/accounts/{H}/orders/123",
               f"/trader/v1/accounts/{H}/previewOrder", "/trader/v1/accounts/x/orders/", "/trader/v1/accounts/x/ORDERS",
               f"/trader/v1/accounts/{H}/orders?x=1"]


@pytest.fixture
def no_network(monkeypatch):
    """Any attempt to open the network fails the test: the lock must refuse first."""
    def boom(*a, **k):
        raise AssertionError("the network was opened")

    monkeypatch.setattr(http, "_send", boom)
    http.reset_limits()
    http.set_token_provider(lambda: FAKE_TOKEN, lambda: FAKE_TOKEN)
    yield
    http.set_token_provider(None)
    http.reset_limits()


def match(method, path, consumer="hokma_backend"):
    return hardblock.read_exception_match(HOST, method, path, consumer)


# ------------------------------------------------------------------------------------------- one door

def test_nothing_in_the_schwab_package_opens_the_network_but_http_py_and_stream_py():
    """The only module that imports an HTTP client or a socket is http.py; auth.py calls http.request. The only one that
    opens a WebSocket is stream.py (the Streamer, SPEC-016 6.2), and it takes no HTTP client: `userPreference` goes by http.py."""
    forbidden = re.compile(r"^\s*(?:import|from)\s+(?:urllib\.request|http\.client|socket|ssl|requests|httpx|aiohttp|"
                           r"websockets?|websocket|hokma\.market\.history|hokma\.core\.tls)\b", re.M)
    for path in (ROOT / "schwab").glob("*.py"):
        if path.name not in ("http.py", "stream.py"):
            assert not forbidden.search(path.read_text(encoding="utf-8")), path.name
            assert "urlopen" not in path.read_text(encoding="utf-8"), path.name
    streamer = (ROOT / "schwab" / "stream.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:import|from)\s+(?:urllib\.request|http\.client|socket|ssl|requests|httpx|aiohttp)\b", streamer, re.M)
    assert "urlopen" not in streamer


def test_every_call_of_auth_goes_through_http_request(monkeypatch):
    """The auth module reaches Schwab only by `http.request`, and only with the token call (a POST, not authed)."""
    import json as _json

    import keyring
    from live_support import MemoryKeyring

    from hokma.connections import runtime
    from hokma.connections.service import ConnectionService
    from hokma.market.providers import get_catalog
    from hokma.schwab import auth

    previous = keyring.get_keyring()
    ring = MemoryKeyring()
    keyring.set_keyring(ring)
    ring.set_password("hokma-market", "schwab", _json.dumps({"client_id": "id-1", "client_secret": "secret-1"}))
    runtime.install(ConnectionService(get_catalog()), None)
    auth.reset()
    seen: list[tuple] = []
    real = http.request
    monkeypatch.setattr(http, "_send", lambda *a: (400, '{"error": "invalid_grant"}', None))
    monkeypatch.setattr(http, "request", lambda method, path, query=None, **kw: (
        seen.append((method, path, kw.get("authed", True))), real(method, path, query, **kw))[1])
    try:
        url = auth.start_login()["auth_url"]
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        with pytest.raises(SchwabError):
            auth.finish_login(f"https://127.0.0.1:8182/?code=c&state={state}")
        ring.set_password("hokma-market", "schwab.session", _json.dumps({
            "access_token": "a", "refresh_token": "r", "expires_in": 1, "obtained_at": 0, "signed_in_at": 0,
            "session_expires_at": 9e12, "notice_sent_for": None}))
        auth.reset()
        with pytest.raises(SchwabError):
            auth.access_token()
        auth.maintain()
    finally:
        auth.reset()
        runtime.reset()
        keyring.set_keyring(previous)
    assert seen and all(s == ("POST", "/v1/oauth/token", False) for s in seen)


def test_http_get_is_http_request(no_network):
    with pytest.raises(SchwabError) as error:
        http.get("/trader/v1/accounts/x/orders/abc")
    assert error.value.code == http.FORBIDDEN_ROUTE


# ------------------------------------------------------------------------------------------- orders never leave
# Through the door of READS (the default consumer) no order ever leaves. The order list is a second list with its own
# consumer, used only by schwab/orders.py (tests of the routes themselves: test_schwab_order_endpoints.py).

BROKER = "hokma_broker"
ORDER_ROUTES = [("POST", f"/trader/v1/accounts/{H}/orders"), ("PUT", f"/trader/v1/accounts/{H}/orders/123"),
                ("DELETE", f"/trader/v1/accounts/{H}/orders/123"), ("POST", f"/trader/v1/accounts/{H}/previewOrder")]

@pytest.mark.parametrize("method", WRITES)
@pytest.mark.parametrize("path", ORDER_PATHS)
@pytest.mark.parametrize("authed", [True, False])
def test_a_write_to_an_order_route_is_refused_before_the_network(no_network, method, path, authed):
    with pytest.raises(SchwabError) as error:
        http.request(method, path, None, body=b"{}", authed=authed)
    assert error.value.code == http.FORBIDDEN_ROUTE and error.value.status == 403


@pytest.mark.parametrize("path", ["/trader/v1/accounts/x/previewOrder", f"/trader/v1/accounts/{H}/previewOrder"])
def test_preview_order_is_refused_even_for_get(no_network, path):
    with pytest.raises(SchwabError):
        http.request("GET", path)
    with pytest.raises(SchwabError):
        http.request("GET", path, consumer=BROKER)


# ------------------------------------------------------------------------------------------- the order door (consumer hokma_broker)

@pytest.mark.parametrize("method, path", ORDER_ROUTES)
def test_the_four_order_routes_pass_only_through_the_order_door(no_network, method, path):
    """Through the read door (default consumer, or an explicit hokma_backend) they are refused before the network; with the
    broker consumer the check passes and the call reaches the (blocked here) network."""
    for consumer in ("hokma_backend", "agent", ""):
        with pytest.raises(SchwabError) as error:
            http.request(method, path, None, body=b"{}", consumer=consumer)
        assert error.value.code == http.FORBIDDEN_ROUTE
    with pytest.raises(AssertionError, match="the network was opened"):
        http.request(method, path, None, body=b"{}", consumer=BROKER)


@pytest.mark.parametrize("method, path", [
    ("PATCH", f"/trader/v1/accounts/{H}/orders"), ("PATCH", f"/trader/v1/accounts/{H}/orders/123"),
    ("PUT", f"/trader/v1/accounts/{H}/orders"), ("DELETE", f"/trader/v1/accounts/{H}/orders"),
    ("POST", f"/trader/v1/accounts/{H}/orders/123"), ("GET", f"/trader/v1/accounts/{H}/previewOrder"),
    ("PUT", f"/trader/v1/accounts/{H}/previewOrder"), ("POST", "/trader/v1/orders"), ("DELETE", "/trader/v1/orders"),
    ("POST", f"/trader/v1/accounts/{H}/orders/abc"), ("PUT", f"/trader/v1/accounts/{H}/orders/12a"),
    ("DELETE", f"/trader/v1/accounts/{H}/orders/123/"), ("POST", f"/trader/v1/accounts/{H}/orders/"),
    ("POST", "/trader/v1/accounts/accountNumbers/orders"), ("POST", f"/trader/v1/accounts/{H}/transactions"),
    ("POST", f"/trader/v1/accounts/{H}/orders?x=1"), ("POST", "/trader/v1/accounts/x/orders"),
    ("POST", "/v1/oauth/token"), ("GET", "/marketdata/v1/quotes"), ("GET", f"/trader/v1/accounts/{H}/orders"),
    ("GET", f"/trader/v1/accounts/{H}/orders/123"), ("GET", "/trader/v1/accounts"), ("POST", "/trader/v1/userPreference"),
    ("POST", "/marketdata/v1/quotes"), ("OPTIONS", f"/trader/v1/accounts/{H}/orders"),
])
def test_the_order_door_refuses_every_other_route_and_method_before_the_network(no_network, method, path):
    with pytest.raises(SchwabError) as error:
        http.request(method, path, None, body=b"{}", consumer=BROKER)
    assert error.value.code == http.FORBIDDEN_ROUTE and error.value.status == 403


def test_an_unknown_consumer_is_refused_for_every_route(no_network):
    for method, path in ORDER_ROUTES + [("GET", "/marketdata/v1/quotes")]:
        for consumer in ("hokma_agent", "Hokma_Broker", "hokma_broker2", "broker", "hokma_backend2"):
            with pytest.raises(SchwabError) as error:
                http.request(method, path, None, body=b"{}", consumer=consumer)
            assert error.value.code == http.FORBIDDEN_ROUTE


def test_the_default_consumer_is_the_read_one_and_the_reads_are_not_open_to_the_broker(no_network):
    import inspect

    assert inspect.signature(http.request).parameters["consumer"].default == "hokma_backend"
    for method, path in READS:
        with pytest.raises(SchwabError) as error:
            http.request(method, path, None, consumer=BROKER, authed=False)
        assert error.value.code == http.FORBIDDEN_ROUTE, (method, path)


def test_the_order_list_is_not_the_read_list():
    brokers = hardblock.load_brokers()
    reads = {(r["method"], r["path"]) for r in brokers["read_exceptions"] if r["host"] == HOST}
    orders = {(r["method"], r["path"]) for r in brokers["order_exceptions"] if r["host"] == HOST}
    assert len(orders) == 4 and not (reads & orders)
    assert {m for m, _ in orders} == {"POST", "PUT", "DELETE"}
    assert not any(hardblock.order_exception_allows(HOST, m, p.replace("{accountNumber}", "ABCDEFGH").replace("{orderId}", "1"),
                                                    "hokma_backend") for m, p in orders)


def test_the_reply_of_the_door_carries_status_correl_and_location_without_a_token():
    with FakeSchwab() as fake:
        fake.on("POST", f"/trader/v1/accounts/{H}/orders", lambda call: (201, None))
        reply = http.request("POST", f"/trader/v1/accounts/{H}/orders", None, body=b"{}", consumer=BROKER, reply=True)
        assert (reply.body, reply.status, reply.correl_id) == (None, 201, "test-correl-id")
        assert FAKE_TOKEN not in repr(reply)
        assert http.request("POST", f"/trader/v1/accounts/{H}/orders", None, body=b"{}", consumer=BROKER) is None


@pytest.mark.parametrize("path", ["/marketdata/v1/quotes/", "/marketdata/v1/movers/NYSE/", "/marketdata/v1/movers//NYSE",
                                  f"/trader/v1/accounts/{H}/orders/", f"/trader/v1/accounts/{H}/orders//", "//trader/v1/accounts",
                                  "/trader/v1/./accounts", "/marketdata/v1/movers/NYSE/../../quotes", "marketdata/v1/quotes",
                                  "/marketdata/v1/quotes?symbols=SPY", "/marketdata/v1/quotes#x"])
def test_an_odd_shaped_path_is_refused_by_the_door_before_the_network(no_network, path):
    with pytest.raises(SchwabError) as error:
        http.get(path)
    assert error.value.code == http.FORBIDDEN_ROUTE


ENCODED_DOTS = [".", "..", "%2e", "%2E", "%2e%2e", "%2E%2E", "%2e.", ".%2e", "%2f", "%2F", "%252e", "%252E", "%252e%252e", "%252f",
                 "%252F", "%5c", "%5C", "%255c", "%25252e"]


@pytest.mark.parametrize("segment", ENCODED_DOTS)
def test_a_dot_or_encoded_dot_or_slash_segment_is_refused_before_the_network_by_every_door(no_network, segment):
    """The symbol and account regexes let `.` and `%` in, so `..` or `%2e%2e` would match a marker: the door and the lock
    refuse the segment itself (C1), for the reads, for the four order routes and for the broker consumer."""
    for path in (f"/marketdata/v1/{segment}/quotes", f"/marketdata/v1/movers/{segment}", f"/marketdata/v1/instruments/{segment}",
                 f"/trader/v1/accounts/{segment}", f"/trader/v1/accounts/{segment}/orders", f"/trader/v1/accounts/{H}/{segment}"):
        with pytest.raises(SchwabError) as error:
            http.get(path)
        assert error.value.code == http.FORBIDDEN_ROUTE, path
    for method, path in (("POST", f"/trader/v1/accounts/{segment}/orders"), ("PUT", f"/trader/v1/accounts/{H}/orders/{segment}"),
                         ("DELETE", f"/trader/v1/accounts/{segment}/orders/123"), ("POST", f"/trader/v1/accounts/{segment}/previewOrder"),
                         ("POST", f"/trader/v1/accounts/{H}/{segment}")):
        with pytest.raises(SchwabError) as error:
            http.request(method, path, None, body=b"{}", consumer=BROKER)
        assert error.value.code == http.FORBIDDEN_ROUTE, path
        assert not hardblock.order_exception_allows(HOST, method, path, BROKER)
    assert not hardblock.read_exception_allows(HOST, "GET", f"/marketdata/v1/{segment}/quotes", "hokma_backend")


@pytest.mark.parametrize("path", ["/marketdata/v1/%24SPX/quotes", "/marketdata/v1/BRK.B/quotes", "/marketdata/v1/movers/%24DJI"])
def test_a_once_encoded_symbol_is_not_refused_by_the_dot_rule(no_network, path):
    """The same door still lets the legitimate encoded `$` through to the (blocked here) network."""
    with pytest.raises(AssertionError, match="the network was opened"):
        http.get(path)


@pytest.mark.parametrize("method, path", [
    ("GET", "/v1/oauth/authorize"), ("GET", "/v1/oauth/token"), ("POST", "/marketdata/v1/quotes"),
    ("GET", "/marketdata/v1/other"), ("GET", "/marketdata/v1/"), ("GET", "/"), ("GET", ""),
    ("GET", "/trader/v1/accounts/x/orders/abc"), ("GET", f"/trader/v1/accounts/{H}/orders/12a"),
    ("GET", f"/trader/v1/accounts/{H}/orders/1/cancel"), ("GET", f"/trader/v1/accounts/{H}/positions"),
    ("GET", "/trader/v1/accounts/accountNumbers/orders"), ("GET", "/trader/v1/accounts/accountNumbers/transactions"),
    ("GET", "/Trader/v1/accounts"), ("GET", "/trader/v1/accounts/"), ("GET", "/trader/v1//accounts"),
    ("GET", "/trader/v1/accounts/../orders"), ("GET", "/marketdata/v1/movers/FOO"), ("GET", "/marketdata/v1/markets/stock"),
    ("GET", "/marketdata/v1/instruments/12345678"), ("GET", "/marketdata/v1/instruments/1234567890"),
    ("DELETE", "/trader/v1/accounts"), ("PUT", "/trader/v1/userPreference"), ("POST", "/trader/v1/userPreference"),
    ("PATCH", "/v1/oauth/token"), ("GET", "/streamer"), ("OPTIONS", "/marketdata/v1/quotes"), ("HEAD", "/marketdata/v1/quotes"),
])
def test_any_route_outside_the_list_is_refused_before_the_network(no_network, method, path):
    with pytest.raises(SchwabError) as error:
        http.request(method, path)
    assert error.value.code == http.FORBIDDEN_ROUTE


@pytest.mark.parametrize("method, path", READS)
def test_every_listed_read_is_allowed_for_the_backend_and_for_nobody_else(method, path):
    assert match(method, path) is not None
    assert match(method, path + "?symbols=SPY") is not None
    for consumer in ("agent", "hokma_agent", "", "Hokma_Backend", "hokma_backend2"):
        assert match(method, path, consumer) is None
    other = "POST" if method == "GET" else "GET"
    assert match(other, path) is None
    for wrong in ("PUT", "DELETE", "PATCH"):
        assert match(wrong, path) is None


def test_the_listed_reads_are_exactly_the_nineteen_get_rules_and_the_token_post():
    rules = [r for r in hardblock.load_brokers()["read_exceptions"] if r["host"] == HOST]
    assert sorted((r["method"], r["path"]) for r in rules).count(("POST", "/v1/oauth/token")) == 1
    assert [r for r in rules if r["method"] not in ("GET", "POST")] == []
    assert [r["path"] for r in rules if r["method"] == "POST"] == ["/v1/oauth/token"]
    assert len([r for r in rules if r["method"] == "GET"]) == 19
    assert all(r["consumer"] == "hokma_backend" for r in rules)
    assert not any("order" in r["path"].lower() and r["method"] != "GET" for r in rules)
    assert not any("preview" in r["path"].lower() for r in rules)


@pytest.mark.parametrize("path", [
    "/marketdata/v1/a/b/quotes", "/marketdata/v1//quotes", "/marketdata/v1/SPY/quotes/x",
    "/marketdata/v1/movers/%24SPX/extra", "/marketdata/v1/movers/", "/marketdata/v1/movers", "/marketdata/v1/movers/NYSE/x",
    "/marketdata/v1/markets/equity/x", "/marketdata/v1/markets/equity,option", "/marketdata/v1/instruments/037833100/x",
    f"/trader/v1/accounts/{H}/x/orders", f"/trader/v1/accounts/{H}/orders/1/2",
    f"/trader/v1/accounts/{H}/{H}", "/trader/v1/accounts/AB", f"/trader/v1/accounts/{H}/transactions/1/2",
])
def test_a_template_marker_is_one_segment_and_the_segment_count_is_fixed(path):
    assert match("GET", path) is None


@pytest.mark.parametrize("value", ["%24DJI", "%24COMPX", "%24SPX", "NYSE", "NASDAQ", "OTCBB", "INDEX_ALL", "EQUITY_ALL",
                                   "OPTION_ALL", "OPTION_PUT", "OPTION_CALL"])
def test_movers_accepts_only_its_enum(value):
    assert match("GET", f"/marketdata/v1/movers/{value}") is not None
    for bad in (value.lower() if value[0] != "%" else "%24spx", value + "X", "X" + value, "$SPX", "%24"):
        assert match("GET", f"/marketdata/v1/movers/{bad}") is None, bad


@pytest.mark.parametrize("value", ["equity", "option", "bond", "future", "forex"])
def test_markets_accepts_only_its_enum(value):
    assert match("GET", f"/marketdata/v1/markets/{value}") is not None
    for bad in (value.upper(), value + "s", "stock", "equity|option", "e" + value):
        assert match("GET", f"/marketdata/v1/markets/{bad}") is None, bad


def test_the_account_marker_is_not_the_word_accountnumbers_and_keeps_its_length():
    assert match("GET", "/trader/v1/accounts/accountNumbers") is not None       # the exact rule, not the template
    assert match("GET", "/trader/v1/accounts/accountNumbers/orders") is None
    assert match("GET", "/trader/v1/accounts/accountNumbers/orders/1") is None
    assert match("GET", "/trader/v1/accounts/ABCDEFG") is None                  # 7 characters
    assert match("GET", "/trader/v1/accounts/ABCDEFGH") is not None
    assert match("GET", "/trader/v1/accounts/" + "A" * 128) is not None
    assert match("GET", "/trader/v1/accounts/" + "A" * 129) is None
    assert match("GET", "/trader/v1/accounts/ABC-EFGH") is None


def test_an_exception_rule_never_allows_a_listed_host_for_another_host():
    assert hardblock.read_exception_match("api.schwabapi.com.evil.com", "GET", "/marketdata/v1/quotes", "hokma_backend") is None
    assert hardblock.read_exception_match("evil.api.schwabapi.com", "GET", "/marketdata/v1/quotes", "hokma_backend") is None
    assert hardblock.read_exception_match("api.alpaca.markets", "GET", "/marketdata/v1/quotes", "hokma_backend") is None


# ------------------------------------------------------------------------------------------- the host

def test_the_host_is_written_only_in_http_auth_and_the_lock_file():
    named = {str(p.relative_to(ROOT)).replace("\\", "/") for p in ROOT.rglob("*")
             if p.is_file() and p.suffix in (".py", ".json", ".ts", ".js", ".md", ".txt", ".yml", ".yaml", ".toml", ".cfg")
             and "__pycache__" not in p.parts and HOST in p.read_text(encoding="utf-8", errors="ignore")}
    assert named == {"schwab/http.py", "schwab/auth.py", "kit/hardblock_brokers.json"}


# ------------------------------------------------------------------------------------------- the agent hook

URL = "https://api.schwabapi.com/marketdata/v1/quotes"


@pytest.mark.parametrize("url", [URL, URL + "?symbols=SPY", "https://api.schwabapi.com/trader/v1/accounts",
                                 "https://api.schwabapi.com/v1/oauth/token", "https://api.schwabapi.com/trader/v1/orders"])
def test_for_an_agent_the_schwab_host_stays_locked_even_for_the_reads(box, url):
    assert box.hit("WebFetch", url=url) == "real_money"
    assert box.bash(f"curl -s {url}") == "real_money"
    assert box.bash(f"curl -s -X GET '{url}' -H 'Authorization: Bearer x'") == "real_money"
    assert box.bash(f"python3 -c \"import urllib.request as u; u.urlopen('{url}')\"") == "real_money"
    assert box.ps(f"Invoke-RestMethod {url}") == "real_money"


def test_the_hook_as_a_process_refuses_the_schwab_quotes_for_an_agent(box):
    done = box.run_hook({"tool_name": "WebFetch", "tool_input": {"url": URL}, "cwd": str(box.work)})
    assert done.returncode == 2 and "HOKMA_BLOCKED real_money" in done.stderr
    done = box.run_hook({"tool_name": "Bash", "tool_input": {"command": f"curl -s {URL}"}, "cwd": str(box.work)})
    assert done.returncode == 2 and "HOKMA_BLOCKED real_money" in done.stderr


def test_the_hook_does_not_read_the_read_exceptions():
    source = (ROOT / "kit" / "hardblock.py").read_text(encoding="utf-8")
    body = source.split("def evaluate", 1)[1]
    assert "read_exception" not in body and "order_exception" not in body


# ------------------------------------------------------------------------------------------- redirects and logs

class _Capture(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Capture.seen.append({k.lower(): v for k, v in self.headers.items()})
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_redirect_to_another_host_carries_no_authorization():
    _Capture.seen = []
    target = _serve(_Capture)

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{target.server_address[1]}/capture")
            self.send_header("Content-Length", "0")
            self.end_headers()

    first = _serve(Redirect)
    http.set_base(f"http://127.0.0.1:{first.server_address[1]}")
    http.set_token_provider(lambda: FAKE_TOKEN, lambda: FAKE_TOKEN)
    http.reset_limits()
    try:
        assert http.get("/marketdata/v1/quotes", {"symbols": "SPY"}) == {"ok": True}
    finally:
        http.set_base(None)
        http.set_token_provider(None)
        first.shutdown()
        target.shutdown()
        for server in (first, target):
            server.server_close()
    assert len(_Capture.seen) == 1 and "authorization" not in _Capture.seen[0]


def test_the_bearer_goes_in_the_header_only_never_in_the_address():
    with FakeSchwab() as fake:
        fake.on("GET", "/marketdata/v1/quotes", {"SPY": {}})
        http.get("/marketdata/v1/quotes", {"symbols": "SPY"})
        call = fake.calls[0]
        assert call.headers["authorization"] == "Bearer " + FAKE_TOKEN
        assert FAKE_TOKEN not in json.dumps(call.query) and FAKE_TOKEN not in call.path


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_no_log_or_error_carries_a_token_secret_code_account_number_or_hash(caplog, status):
    caplog.set_level(logging.DEBUG)
    leaks = [FAKE_TOKEN, "client-secret-value", "auth-code-value", FAKE_ACCOUNT_NUMBER, FAKE_HASH]
    body = {"errors": [{"id": "1", "status": str(status), "title": "t", "detail": "d", "source": {"pointer": "/x"}}],
            "error": "invalid_grant"}
    with FakeSchwab() as fake:
        fake.on("GET", f"/trader/v1/accounts/{FAKE_HASH}/orders", (status, body))
        fake.on("GET", "/trader/v1/accounts/accountNumbers", [{"accountNumber": FAKE_ACCOUNT_NUMBER, "hashValue": FAKE_HASH}])
        fake.on("POST", "/v1/oauth/token", (status, body))
        problems = []
        for call in (lambda: http.get(f"/trader/v1/accounts/{FAKE_HASH}/orders", {"status": "FILLED"}),
                     lambda: http.get("/trader/v1/accounts/accountNumbers"),
                     lambda: http.request("POST", "/v1/oauth/token", None, body=b"code=auth-code-value&client_secret=client-secret-value",
                                          headers={"Authorization": "Basic Y2xpZW50LXNlY3JldC12YWx1ZQ=="}, authed=False),
                     lambda: http.request("PUT", f"/trader/v1/accounts/{FAKE_HASH}/orders/1")):
            try:
                call()
            except SchwabError as error:
                problems.append(f"{error} {error.code} {error.detail} {error.errors}")
    everything = caplog.text + "\n".join(problems)
    for leak in leaks:
        assert leak not in everything, leak
    assert "/trader/v1/accounts/{accountNumber}/orders" in caplog.text or status == 429   # the path is logged as its template


def test_the_log_line_has_method_template_status_milliseconds_and_correl_id(caplog):
    caplog.set_level(logging.INFO)
    with FakeSchwab() as fake:
        fake.on("GET", f"/trader/v1/accounts/{FAKE_HASH}/orders/77", {"orderId": 77})
        http.get(f"/trader/v1/accounts/{FAKE_HASH}/orders/77")
    line = [r.getMessage() for r in caplog.records if r.name == "hokma.schwab"][0]
    assert "GET /trader/v1/accounts/{accountNumber}/orders/{orderId} -> 200 in " in line and "correl=test-correl-id" in line
    assert FAKE_HASH not in line and "77" not in line.replace("-> 200", "")

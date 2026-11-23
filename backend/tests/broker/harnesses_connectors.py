"""Harnesses of the connectors for the conformance suite (SPEC-018 B5: AH-40, DTS-08b, OA-55, OA-72).

Each connector gets its own recorded answers (made-up JSON under `fixtures/`, no network, no real data or key) through the injection point its module offers:
Alpaca a client whose HTTP session answers the recorded bodies (the real SDK reads them), IBKR a transport function, crypto an exchange object with the
unified replies. Every recording carries one sentinel string where the account identifier would be; the suite fails when it shows in anything a broker returns.
`register` is called by `test_contract_conformance.py` with its own `harness` decorator and `Harness` class.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import ccxt
import requests
from alpaca.trading.client import TradingClient

from hokma.broker import alpaca as alpaca_module
from hokma.broker import crypto as crypto_module
from hokma.broker import ibkr as ibkr_module
from hokma.broker.alpaca import AlpacaBroker
from hokma.broker.crypto import CryptoBroker
from hokma.broker.ibkr import IbkrBroker

FIXTURES = Path(__file__).parent / "fixtures"
ALPACA_SENTINEL = "ALPSENT9371"
IBKR_SENTINEL = "DUSENT4455"
CRYPTO_SENTINEL = "CRYSENT7788KEY"
ALPACA_ORDER = "61e69015-8549-4bfd-b9c3-01e75843f47d"
IBKR_ORDER = "987654321"
CRYPTO_ORDER = "OABC12-DEF34-GH56"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def neutral(contract_id: str = "EQ:AAPL", *, qty: str = "1", side: str = "buy", effect: str = "open", kind: str = "limit", tif: str = "gtc",
            limit: str | None = "100", stop: str | None = None, asset_type: str = "equity", **more) -> dict:
    """An order in the shape `hokma.exec.submit.to_native` builds for a broker that is not Schwab."""
    return {"fingerprint": "a" * 64, "type": kind, "tif": tif, "limit_price": limit, "stop_price": stop, "stop_loss": None, "take_profit": None,
            "reduce_only": False, "legs": [{"contract_id": contract_id, "symbol": "ignored", "side": side, "effect": effect, "qty": qty, "multiplier": "1",
                                            "tick_size": "0.01", "asset_type": asset_type}], **more}


# ------------------------------------------------------------------------------------------------------------------ alpaca

class Reply:
    def __init__(self, status: int, body) -> None:
        self.status_code, self.text = status, "" if body is None else json.dumps(body)

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class RecordedSession:
    """Stands in for the HTTP session of the Alpaca SDK: answers (method, path) from a table and keeps the calls."""

    def __init__(self, routes: dict[tuple[str, str], tuple[int, object]]) -> None:
        self.routes, self.calls = routes, []

    def request(self, method: str, url: str, **opts):
        path = url.split("/v2", 1)[1].split("?")[0]
        self.calls.append((method, path, opts.get("json") or opts.get("params")))
        if (method, path) == ("GET", "/account") and "down" in self.routes:
            raise requests.ConnectionError("down")
        return Reply(*self.routes.get((method, path), (404, {"code": 40410000, "message": "not found"})))


def alpaca_routes() -> dict:
    return {("GET", "/account"): (200, fixture("alpaca_account.json")), ("GET", "/positions"): (200, fixture("alpaca_positions.json")),
            ("POST", "/orders"): (200, fixture("alpaca_order_accepted.json")), ("GET", "/orders"): (200, fixture("alpaca_orders_open.json")),
            ("GET", f"/orders/{ALPACA_ORDER}"): (200, fixture("alpaca_order_accepted.json")), ("DELETE", f"/orders/{ALPACA_ORDER}"): (204, None),
            ("PATCH", f"/orders/{ALPACA_ORDER}"): (200, fixture("alpaca_order_accepted.json"))}


def alpaca_install(routes: dict | None = None) -> RecordedSession:
    session = RecordedSession(routes if routes is not None else alpaca_routes())

    def factory(broker) -> TradingClient:
        client = TradingClient(api_key="test-key-id", secret_key="test-secret", paper=True)
        client._session = session
        return client

    alpaca_module.set_client_factory(factory)
    return session


# -------------------------------------------------------------------------------------------------------------------- ibkr

class Gateway:
    """Stands in for the local gateway of IBKR: answers (method, path) with a status and a body and keeps the calls."""

    BASE = "https://localhost:5000/v1/api"

    def __init__(self, routes: dict[tuple[str, str], tuple[int, object]]) -> None:
        self.routes, self.calls, self.down = routes, [], False

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, str]:
        from hokma.broker.ibkr.http import UNAVAILABLE, IbkrError

        if self.down:
            raise IbkrError(UNAVAILABLE)
        path = url.removeprefix(self.BASE).split("?")[0]
        self.calls.append((method, path, None if body is None else json.loads(body)))
        status, reply = self.routes.get((method, path), (404, {"error": "not found"}))
        return status, "" if reply is None else json.dumps(reply)


def ibkr_routes() -> dict:
    return {("GET", "/iserver/auth/status"): (200, fixture("ibkr_auth_status.json")), ("GET", "/portfolio/accounts"): (200, fixture("ibkr_accounts.json")),
            ("GET", f"/portfolio/{IBKR_SENTINEL}/summary"): (200, fixture("ibkr_summary.json")),
            ("GET", f"/portfolio/{IBKR_SENTINEL}/positions/0"): (200, fixture("ibkr_positions.json")),
            ("GET", "/iserver/account/orders"): (200, fixture("ibkr_orders.json")),
            ("POST", "/iserver/secdef/search"): (200, fixture("ibkr_secdef_stk.json")),
            ("POST", f"/iserver/account/{IBKR_SENTINEL}/orders"): (200, fixture("ibkr_reply_placed.json")),
            ("POST", f"/iserver/account/{IBKR_SENTINEL}/orders/whatif"): (200, fixture("ibkr_whatif.json")),
            ("POST", f"/iserver/account/{IBKR_SENTINEL}/order/{IBKR_ORDER}"): (200, fixture("ibkr_reply_placed.json")),
            ("DELETE", f"/iserver/account/{IBKR_SENTINEL}/order/{IBKR_ORDER}"): (200, fixture("ibkr_cancel.json"))}


def ibkr_install(routes: dict | None = None) -> Gateway:
    gateway = Gateway(routes if routes is not None else ibkr_routes())
    ibkr_module.set_transport(gateway)
    return gateway


# ----------------------------------------------------------------------------------------------------------------- crypto

class FakeExchange:
    """An exchange object with the unified methods of the library, answering the recorded replies; keeps the calls it receives."""

    id = "kraken"
    apiKey = CRYPTO_SENTINEL
    has = {"editOrder": False}

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.balance = fixture("crypto_balance.json")
        self.open = [fixture("crypto_order_open.json")]
        self.down = False

    def _up(self) -> None:
        if self.down:
            raise ccxt.NetworkError("down")

    def load_markets(self):
        self._up()
        return {}

    def market(self, symbol):
        if symbol != "BTC/USD":
            raise ccxt.BadSymbol("unknown pair")
        return fixture("crypto_market_btc.json")

    def amount_to_precision(self, symbol, amount):
        return format(Decimal(repr(amount)).quantize(Decimal("1e-8")), "f").rstrip("0").rstrip(".")

    def price_to_precision(self, symbol, price):
        return format(Decimal(repr(price)).quantize(Decimal("1e-2")), "f")

    def fetch_balance(self):
        self._up()
        return self.balance

    def fetch_ticker(self, symbol):
        self._up()
        if symbol != "BTC/USD":
            raise ccxt.BadSymbol("unknown pair")
        return fixture("crypto_ticker_btc.json")

    def fetch_open_orders(self):
        self._up()
        return self.open

    def fetch_closed_orders(self):
        self._up()
        return []

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        self._up()
        self.calls.append(("create_order", symbol, type, side, amount, price, params))
        return fixture("crypto_order_open.json") if type == "limit" else fixture("crypto_order_filled.json")

    def cancel_order(self, id, symbol=None):
        self._up()
        self.calls.append(("cancel_order", id, symbol))
        return {"id": id, "status": "canceled"}


def crypto_install(exchange: FakeExchange | None = None) -> FakeExchange:
    fake = exchange or FakeExchange()
    crypto_module.set_exchange_factory(lambda broker: fake)
    return fake


# ------------------------------------------------------------------------------------------------------------------ register

def _row(u, kind: str):
    return next(r for r in u.broker.list_brokers() if r.kind == kind)


def register(harness, Harness) -> None:
    """Add the three connectors to the conformance suite."""

    @harness("alpaca")
    def alpaca_harness(request):
        desks_db = request.getfixturevalue("desks_db")
        request.getfixturevalue("connections")
        alpaca_install()
        try:
            account_id = alpaca_module.sync_accounts(db=desks_db)[0]
            with desks_db.read() as u:
                broker = AlpacaBroker(u.broker, _row(u, "alpaca"))
                yield Harness("alpaca", broker, account_id, (ALPACA_SENTINEL, "904837e3-3b76-47ec-b432-046db621571b"),
                              lambda: broker.place_order(account_id, neutral()), lambda order_id: None, lambda: None, "AAPL")
        finally:
            alpaca_module.set_client_factory(None)

    @harness("ibkr")
    def ibkr_harness(request):
        desks_db = request.getfixturevalue("desks_db")
        request.getfixturevalue("connections")
        ibkr_install()
        try:
            account_id = ibkr_module.sync_accounts(db=desks_db)[0]
            with desks_db.read() as u:
                broker = IbkrBroker(u.broker, _row(u, "ibkr"))
                yield Harness("ibkr", broker, account_id, (IBKR_SENTINEL,), lambda: broker.place_order(account_id, neutral()),
                              lambda order_id: None, lambda: None, "AAPL")
        finally:
            ibkr_module.set_transport(None)

    @harness("crypto")
    def crypto_harness(request):
        desks_db = request.getfixturevalue("desks_db")
        request.getfixturevalue("connections")
        crypto_install()
        try:
            account_id = crypto_module.sync_accounts(db=desks_db)[0]
            with desks_db.read() as u:
                broker = CryptoBroker(u.broker, _row(u, "crypto"))
                order = neutral("CRY:BTC-USD", qty="0.01", limit="50000", asset_type="crypto")
                yield Harness("crypto", broker, account_id, (CRYPTO_SENTINEL,), lambda: broker.place_order(account_id, order),
                              lambda order_id: None, lambda: None, "BTC/USD")
        finally:
            crypto_module.set_exchange_factory(None)

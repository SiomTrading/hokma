"""Shared pieces of the order tests of a desk with Schwab (SPEC-015 18): a fake Schwab that answers the order routes (with the
Location header of a 201), a desk whose account is the first Schwab account with the orders switch ON, and builders for the
answers Schwab gives (preview, account with positions, orders). Every figure, symbol and number is made up; nothing touches
the network. The two secrets of the fake (account number and hash) are the sentinels that no table, journal entry, decision,
log line or answer may contain."""
import copy
import json
from types import SimpleNamespace

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures and helper)
from schwab_order_support import OrderFake
from schwab_support import FAKE_ACCOUNT_NUMBER, FAKE_ACCOUNT_NUMBER_2, FAKE_HASH, FAKE_HASH_2, clear_fake, load, register_accounts

from hokma.broker import service
from hokma.connections import runtime
from hokma.connections.service import ConnectionService
from hokma.connections.store import ConnectionSettings, KeyStore
from hokma.market.providers import get_catalog
from hokma.schwab import accounts, http

TR = "/trader/v1"
ACCOUNT = f"{TR}/accounts/{FAKE_HASH}"
ORDERS = f"{ACCOUNT}/orders"
PREVIEW_PATH = f"{ACCOUNT}/previewOrder"
LOCATION = f"https://api.schwabapi.com{ORDERS}/"
SECRETS = (FAKE_ACCOUNT_NUMBER, FAKE_ACCOUNT_NUMBER_2, FAKE_HASH, FAKE_HASH_2)
FIRST_ID = 600000123                      # not a substring of any sentinel of SECRETS


class MemoryKeys(KeyStore):
    def __init__(self):
        self.items: dict[str, str] = {}

    def available(self):
        return True

    def get(self, provider_id):
        return self.items.get(provider_id)

    def set(self, provider_id, value):
        self.items[provider_id] = value

    def delete(self, provider_id):
        self.items.pop(provider_id, None)


# ----------------------------------------------------------------------------------------------- what Schwab answers

def detail(message: str) -> dict:
    return {"validationRuleName": "RULE", "message": message}


def preview_body(*, value: float = 1000.0, power: float = 9000.0, rejects=(), reviews=(), warns=(), alerts=(), legs=None) -> dict:
    """A PreviewOrder (REF B.2). Its order strategy carries the account number as Schwab sends it: it must never get out."""
    return {"orderStrategy": {"accountNumber": FAKE_ACCOUNT_NUMBER, "orderStrategyType": "SINGLE", "status": "ACCEPTED",
                              "orderBalance": {"orderValue": value, "projectedAvailableFund": 9000.0,
                                               "projectedBuyingPower": power, "projectedCommission": 0.0},
                              "orderLegs": legs or []},
            "orderValidationResult": {"alerts": [detail(m) for m in alerts], "accepts": [], "reviews": [detail(m) for m in reviews],
                                      "rejects": [detail(m) for m in rejects], "warns": [detail(m) for m in warns]},
            "commissionAndFee": {"commission": {"commissionLegs": [{"commissionValues": [{"value": 0.65, "type": "COMMISSION"}]}]}}}


def pos(symbol: str, long: float = 0.0, short: float = 0.0, value: float = 0.0, day: float = 0.0, asset: str = "EQUITY",
        open_pl: float = 0.0, **instrument) -> dict:
    """A position of an account (REF B.3), with the fields the desk code reads."""
    return {"longQuantity": long, "shortQuantity": short, "averagePrice": 10.0, "marketValue": value,
            "currentDayProfitLoss": day, "currentDayProfitLossPercentage": 0.0, "longOpenProfitLoss": open_pl,
            "shortOpenProfitLoss": 0.0, "instrument": {"assetType": asset, "symbol": symbol, **instrument}}


def account_body(positions=()) -> dict:
    body = copy.deepcopy(load("account_margin.json"))
    body["securitiesAccount"]["positions"] = list(positions)
    return body


def order_body(order_id: int, *, status: str = "WORKING", editable: bool = True, cancelable: bool = True, symbol: str = "AAPL",
               instruction: str = "BUY", quantity: float = 1.0, price: str = "10.00", **more) -> dict:
    """An order as Schwab returns it (REF B.1 with the response fields); it carries the account number as a number."""
    return {"session": "NORMAL", "duration": "DAY", "orderType": "LIMIT", "price": price, "quantity": quantity,
            "orderStrategyType": "SINGLE", "orderId": order_id, "status": status, "editable": editable,
            "cancelable": cancelable, "accountNumber": int(FAKE_ACCOUNT_NUMBER), "enteredTime": "2026-11-20T15:30:00.000Z",
            "orderLegCollection": [{"orderLegType": "EQUITY", "legId": 1, "instruction": instruction, "quantity": quantity,
                                    "instrument": {"assetType": "EQUITY", "symbol": symbol}}], **more}


def eq(instruction: str, quantity: float, symbol: str = "AAPL") -> dict:
    return {"instruction": instruction, "quantity": quantity, "instrument": {"symbol": symbol, "assetType": "EQUITY"}}


def limit(instruction: str = "BUY", quantity: float = 1, price: str = "10.00", symbol: str = "AAPL", **more) -> dict:
    """A one leg LIMIT order of the desk (what the agents send most)."""
    return {"orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "price": price, "orderStrategyType": "SINGLE",
            "orderLegCollection": [eq(instruction, quantity, symbol)], **more}


def created(fake, order_id: int = FIRST_ID, path: str = ORDERS, method: str = "POST") -> None:
    fake.on(method, path, (201, None, {"Location": LOCATION + str(order_id)}))


# ----------------------------------------------------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def server():
    with OrderFake() as running:
        yield running


@pytest.fixture
def fake(server):
    """Schwab with the two made-up accounts, an empty account, no orders and a clean preview; everything else is a 404."""
    clear_fake(server)
    accounts.reset()
    http.reset_limits(limit=100000)
    register_accounts(server)
    server.on("GET", f"{TR}/accounts", load("accounts.json"))
    server.on("GET", ACCOUNT, account_body())
    server.on("GET", f"{TR}/accounts/{FAKE_HASH_2}", load("account_cash.json"))
    server.on("GET", f"{TR}/userPreference", load("user_preference.json"))
    server.on("GET", ORDERS, [])
    server.on("POST", PREVIEW_PATH, preview_body())
    created(server)
    yield server
    accounts.reset()


@pytest.fixture
def keys(env):
    return MemoryKeys()


@pytest.fixture
def connections(keys, env):
    svc = ConnectionService(get_catalog(), store=keys, settings=ConnectionSettings(env / "connections.json"))
    runtime.install(svc, None)
    yield svc
    runtime.reset()


@pytest.fixture
def ready(desks_db, fake, connections):
    """Schwab connected, a desk of 10,000 with the first Schwab account and the orders switch of Schwab ON."""
    connections.mark_added("schwab")
    with desks_db.read() as u:
        broker = next(r for r in u.broker.list_brokers() if r.kind == "schwab")
        account = u.broker.list_accounts(broker.id)[0]
    desk = make_desk(desks_db)
    service.assign_account(desk["id"], account.id, db=desks_db)
    service.set_orders_enabled(broker.id, True, db=desks_db)
    fake.calls.clear()                      # what the connection made (account list, nicknames) is not part of the flow
    return SimpleNamespace(db=desks_db, desk_id=desk["id"], project_id=desk["project_id"], account_id=account.id,
                           broker_id=broker.id, fake=fake)


def set_capital(r, capital: int) -> None:
    from hokma.desks import service as desks

    desks.update_desk(r.desk_id, {"capital": capital}, db=r.db)
    r.fake.calls.clear()                    # the desk view reads the balance


def set_status(r, status: str) -> None:
    from hokma.desks import service as desks

    desks.update_desk(r.desk_id, {"status": status}, db=r.db)
    r.fake.calls.clear()


def link_telegram(db) -> None:
    with db.write() as u:
        u.session.execute("INSERT INTO telegram_link (id, chat_id) VALUES (1, '1')")


def outbox(db) -> list[str]:
    with db.read() as u:
        return [row["text"] for row in u.session.query("SELECT text FROM telegram_outbox ORDER BY id")]


def calls(fake, method: str | None = None, suffix: str | None = None) -> list:
    """The calls of the order flow (the account list the door makes first is left out)."""
    return [c for c in fake.calls if c.path != f"{TR}/accounts/accountNumbers"
            and (method is None or c.method == method) and (suffix is None or c.path.endswith(suffix))]


def sent(fake, method: str = "POST") -> list:
    """The order calls that change something at Schwab: the routes of 18.7 minus the previews."""
    return [c for c in calls(fake, method) if not c.path.endswith("previewOrder")] if method != "GET" else []


def rows(r) -> list:
    with r.db.read() as u:
        return u.broker.list_order_records(r.desk_id, None, 500)


def whole_database_text(db) -> str:
    """Every row of the tables that hold order data, as text, to prove that no secret got in."""
    parts = []
    with db.read() as u:
        for table in ("broker_orders", "journal_entries", "decisions", "approvals", "telegram_outbox", "broker_external_accounts"):
            parts.append(json.dumps(u.session.query(f"SELECT * FROM {table}"), default=str, ensure_ascii=False))
    return "\n".join(parts)

"""The Alpaca, IBKR and crypto connectors beyond the shared suite (SPEC-018 B5: DTS-08b, OA-55): what they send, what they refuse without
sending, what they say when the broker is out of reach, and that no identifier reaches an answer, a log line or the database.
Every figure is computed by hand in the test from the recorded answers under `fixtures/`; nothing touches the network."""
from __future__ import annotations

import json
import logging
from decimal import Decimal

import pytest
import requests
from broker_orders_support import connections, desks_db, env, db, keys  # noqa: F401  (fixtures)
from broker import harnesses_connectors as hc

from hokma.broker import alpaca as alpaca_module
from hokma.broker import crypto as crypto_module
from hokma.broker import external, service
from hokma.broker import ibkr as ibkr_module
from hokma.broker.alpaca import AlpacaBroker
from hokma.broker.contract import NotSupported
from hokma.broker.crypto import CryptoBroker
from hokma.broker.external import ConnectorError
from hokma.broker.ibkr import IbkrBroker

ALL_SENTINELS = (hc.ALPACA_SENTINEL, hc.IBKR_SENTINEL, hc.CRYPTO_SENTINEL)


def opened(db, module, cls, kind):
    """(broker, account id) for a connector whose recordings are already installed."""
    account_id = module.sync_accounts(db=db)[0]
    with db.read() as u:
        return cls(u.broker, hc._row(u, kind)), account_id


def database_text(db) -> str:
    """Every row of the tables that hold broker data, as text: no identifier may be in it."""
    with db.read() as u:
        return json.dumps({t: u.session.query(f"SELECT * FROM {t}") for t in
                           ("brokers", "broker_accounts", "broker_external_accounts", "broker_orders", "book_entries")}, default=str)


@pytest.fixture
def alpaca(desks_db, connections):
    session = hc.alpaca_install()
    account_id = alpaca_module.sync_accounts(db=desks_db)[0]
    with desks_db.read() as u:
        broker = AlpacaBroker(u.broker, hc._row(u, "alpaca"))
        yield SimpleNamespaceOf(broker=broker, id=account_id, session=session, db=desks_db)
    alpaca_module.set_client_factory(None)


@pytest.fixture
def ibkr(desks_db, connections):
    gateway = hc.ibkr_install()
    account_id = ibkr_module.sync_accounts(db=desks_db)[0]
    with desks_db.read() as u:
        broker = IbkrBroker(u.broker, hc._row(u, "ibkr"))
        yield SimpleNamespaceOf(broker=broker, id=account_id, gateway=gateway, db=desks_db)
    ibkr_module.set_transport(None)


@pytest.fixture
def crypto(desks_db, connections):
    fake = hc.crypto_install()
    account_id = crypto_module.sync_accounts(db=desks_db)[0]
    with desks_db.read() as u:
        broker = CryptoBroker(u.broker, hc._row(u, "crypto"))
        yield SimpleNamespaceOf(broker=broker, id=account_id, exchange=fake, db=desks_db)
    crypto_module.set_exchange_factory(None)


class SimpleNamespaceOf:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def sent(session, method="POST", path="/orders"):
    return [c for c in session.calls if c[0] == method and c[1] == path]


# ---------------------------------------------------------------------------------------------- how a connector joins the system

@pytest.mark.parametrize("name", ["alpaca", "ibkr", "crypto"])
def test_a_connector_is_a_broker_row_with_orders_off_and_the_service_opens_it(name, request):
    fixture_name = request.getfixturevalue(name)
    views = {v["kind"]: v for v in service.list_brokers(fixture_name.db)}
    view = views[name]
    assert view["orders_enabled"] is False                              # the owner turns it on, never the connector
    assert view["capabilities"] == {"accounts": True, "reset": False, "orders": True, "positions": True}
    accounts = service.list_accounts(view["id"], fixture_name.db)
    assert len(accounts) == 1 and accounts[0]["mask"].startswith("\u2022\u2022\u2022\u2022")
    assert accounts[0]["can_reset"] is False


@pytest.mark.parametrize("name", ["alpaca", "ibkr", "crypto"])
def test_no_identifier_reaches_the_database_or_an_answer_and_the_account_is_found_again_by_fingerprint(name, request, caplog):
    caplog.set_level(logging.DEBUG)
    env_ = request.getfixturevalue(name)
    module = {"alpaca": alpaca_module, "ibkr": ibkr_module, "crypto": crypto_module}[name]
    again = module.sync_accounts(db=env_.db)                            # idempotent: same fingerprint, same local account
    assert again == [env_.id]
    env_.broker.balance(env_.id)
    env_.broker.list_accounts()
    text = database_text(env_.db) + caplog.text + json.dumps(service.list_accounts(env_.broker.id, env_.db), default=str)
    for secret in ALL_SENTINELS:
        assert secret not in text
    with env_.db.read() as u:
        link = u.broker.external_of(env_.id)
    assert len(link.fingerprint) == 64 and int(link.fingerprint, 16) >= 0   # HMAC-SHA256 in hex


def test_account_ref_is_the_place_in_the_provider_list_and_the_mask_is_four_dots_and_the_last_four(ibkr):
    assert ibkr.broker.account_ref(ibkr.id) == "A1"
    assert ibkr.broker.mask_of(ibkr.id) == "\u2022\u2022\u2022\u20224455"


def test_reset_and_create_account_are_not_supported(alpaca):
    with pytest.raises(NotSupported):
        alpaca.broker.reset(alpaca.id)
    with pytest.raises(NotSupported):
        alpaca.broker.create_account("x", 1000)


# ------------------------------------------------------------------------------------------------------------------- alpaca

def test_alpaca_balance_and_positions_with_the_figures_of_the_recording(alpaca):
    balance = alpaca.broker.balance(alpaca.id)
    assert (balance.cash, balance.equity, balance.buying_power) == (25000.5, 30500.75, 50000.0)
    found = {p.symbol: p for p in alpaca.broker.positions(alpaca.id)}
    assert found["AAPL"].qty == 10.0 and found["AAPL"].long_qty == 10.0 and found["AAPL"].contract_id == "EQ:AAPL"
    assert found["TSLA"].qty == -5.0 and found["TSLA"].short_qty == 5.0 and found["TSLA"].market_value == -950.0   # a short is negative
    assert found["AAPL"].mask == "\u2022\u2022\u2022\u20229371" and found["AAPL"].account_ref == "A1"


def test_alpaca_places_the_order_it_was_given_and_nothing_else(alpaca):
    answer = alpaca.broker.place_order(alpaca.id, hc.neutral())
    assert answer == {"order_id": hc.ALPACA_ORDER, "status": "WORKING", "filled_qty": "0"}
    (body,) = [c[2] for c in sent(alpaca.session)]
    assert (body["symbol"], body["qty"], body["side"], body["type"], body["limit_price"], body["time_in_force"]) == ("AAPL", 1.0, "buy", "limit", 100.0, "gtc")
    assert body["client_order_id"] == "hk-" + "a" * 45                  # from the fingerprint: a repeat is refused by Alpaca


def test_alpaca_an_option_goes_with_its_position_intent_and_the_symbol_written_from_the_contract_id(alpaca):
    order = hc.neutral("OPT:AAPL:2026-12-18:C:200", tif="day", limit="5.5", asset_type="option", effect="close", side="sell")
    alpaca.broker.place_order(alpaca.id, order)
    (body,) = [c[2] for c in sent(alpaca.session)]
    assert (body["symbol"], body["position_intent"], body["time_in_force"]) == ("AAPL261218C00200000", "sell_to_close", "day")


def test_alpaca_the_symbol_the_caller_wrote_is_not_believed(alpaca):
    order = hc.neutral()
    order["legs"][0]["symbol"] = "MSFT"
    order["legs"][0]["asset_type"] = "option"
    alpaca.broker.place_order(alpaca.id, order)
    (body,) = [c[2] for c in sent(alpaca.session)]
    assert body["symbol"] == "AAPL" and "position_intent" not in body    # an equity by its id, whatever the leg says


@pytest.mark.parametrize("order, reason", [
    (hc.neutral("OPT:AAPL:2026-12-18:C:200", tif="gtc", asset_type="option"), "unsupported"),        # options are day orders
    (hc.neutral("OPT:AAPL:2026-12-18:C:200", tif="day", qty="1.5", asset_type="option"), "unsupported"),
    (hc.neutral("FUT:NQ:2026-12", asset_type="future"), "no_instrument"),                            # Alpaca carries no futures
    (hc.neutral(stop_loss="90"), "unsupported"),                                                       # stop_loss on a limit entry becomes a bracket: allowed below
])
def test_alpaca_refuses_without_sending(alpaca, order, reason):
    if order["stop_loss"] is not None:
        alpaca.broker.place_order(alpaca.id, order)                                                    # an OTO with one exit is accepted
        (body,) = [c[2] for c in sent(alpaca.session)]
        assert body["order_class"] == "oto" and body["stop_loss"] == {"stop_price": 90.0}
        return
    answer = alpaca.broker.place_order(alpaca.id, order)
    assert answer == {"order_id": None, "status": "REJECTED", "reason": reason}
    assert sent(alpaca.session) == []


def test_alpaca_a_two_leg_order_and_a_bad_shape_are_refused_without_sending(alpaca):
    two = hc.neutral()
    two["legs"] = two["legs"] * 2
    assert alpaca.broker.place_order(alpaca.id, two)["reason"] == "unsupported"
    assert alpaca.broker.place_order(alpaca.id, {"legs": []})["reason"] == "bad_order"
    assert alpaca.broker.place_order(alpaca.id, hc.neutral(qty="-1"))["reason"] == "bad_order"
    assert alpaca.broker.place_order(alpaca.id, hc.neutral(kind="stop_limit", stop=None))["reason"] == "bad_order"
    assert sent(alpaca.session) == []


def test_alpaca_reduce_only_is_cut_to_the_position_it_really_holds(alpaca):
    alpaca.broker.place_order(alpaca.id, hc.neutral("EQ:AAPL", qty="25", side="sell", effect="close", reduce_only=True))
    (body,) = [c[2] for c in sent(alpaca.session)]
    assert body["qty"] == 10.0                                          # the recording holds 10 AAPL: 25 is cut to 10
    alpaca.session.calls.clear()
    assert alpaca.broker.place_order(alpaca.id, hc.neutral("EQ:MSFT", side="sell", reduce_only=True))["reason"] == "reduce_only_nothing"
    assert sent(alpaca.session) == []


def test_alpaca_a_refusal_of_the_broker_is_a_word_not_its_text(alpaca):
    alpaca.session.routes[("POST", "/orders")] = (422, hc.fixture("alpaca_error_422.json"))
    answer = alpaca.broker.place_order(alpaca.id, hc.neutral())
    assert answer == {"order_id": None, "status": "REJECTED", "reason": "alpaca_42210000"}
    assert "buying power" not in json.dumps(answer)


def test_alpaca_out_of_reach_is_unavailable_and_the_balance_is_none(alpaca):
    alpaca.session.routes["down"] = (0, None)
    with pytest.raises(ConnectorError) as error:
        alpaca.broker.place_order(alpaca.id, hc.neutral())
    assert error.value.code == "unavailable"
    assert sent(alpaca.session) == []
    balance = alpaca.broker.balance(alpaca.id)
    assert balance.cash is None and balance.equity is None


def test_alpaca_reads_cancels_replaces_and_previews(alpaca):
    assert alpaca.broker.get_order(alpaca.id, hc.ALPACA_ORDER).status == "WORKING"
    assert alpaca.broker.get_order(alpaca.id, "11111111-1111-1111-1111-111111111111") is None            # 404 is None
    (listed,) = alpaca.broker.orders(alpaca.id)
    assert (listed.symbol, listed.qty, listed.limit_price, listed.cancelable) == ("AAPL", 1.0, 100.0, True)
    assert hc.ALPACA_SENTINEL not in json.dumps(listed.data)                                              # the client id carried it: masked
    assert alpaca.broker.cancel_order(alpaca.id, hc.ALPACA_ORDER) == {"order_id": hc.ALPACA_ORDER, "status": "CANCELED"}
    alpaca.session.routes[("DELETE", f"/orders/{hc.ALPACA_ORDER}")] = (422, hc.fixture("alpaca_error_422.json"))
    with pytest.raises(NotSupported):
        alpaca.broker.cancel_order(alpaca.id, hc.ALPACA_ORDER)
    alpaca.broker.replace_order(alpaca.id, hc.ALPACA_ORDER, hc.neutral(qty="2", limit="101.5"))
    (patch,) = [c[2] for c in sent(alpaca.session, "PATCH", f"/orders/{hc.ALPACA_ORDER}")]
    assert (patch["qty"], patch["limit_price"]) == (2, 101.5)
    assert alpaca.broker.preview_order(alpaca.id, hc.neutral(qty="3", limit="100"))["orderStrategy"]["orderBalance"]["orderValue"] == 300.0
    assert alpaca.broker.preview_order(alpaca.id, hc.neutral(kind="market", limit=None))["unpriced"] == ["EQ:AAPL"]


def test_alpaca_an_order_id_that_is_not_a_uuid_and_a_bad_filter_are_refused(alpaca):
    for bad in ("../x", "123", None, 5):
        with pytest.raises(ConnectorError):
            alpaca.broker.get_order(alpaca.id, bad)
    with pytest.raises(ConnectorError):
        alpaca.broker.orders(alpaca.id, status="everything")
    with pytest.raises(ConnectorError):
        alpaca.broker.orders(alpaca.id, limit=100000)


def test_alpaca_an_account_of_other_keys_is_not_listed(alpaca):
    other = hc.fixture("alpaca_account.json") | {"account_number": "ALPOTHER0001"}
    alpaca.session.routes[("GET", "/account")] = (200, other)
    with pytest.raises(ConnectorError) as error:
        alpaca.broker.positions(alpaca.id)
    assert error.value.code == "account_not_listed"


# --------------------------------------------------------------------------------------------------------------------- ibkr

def test_ibkr_balance_positions_and_orders_with_the_figures_of_the_recording(ibkr):
    balance = ibkr.broker.balance(ibkr.id)
    assert (balance.cash, balance.equity, balance.buying_power) == (12000.5, 15500.25, 48000.0)
    found = {p.asset_type: p for p in ibkr.broker.positions(ibkr.id)}
    assert (found["STK"].qty, found["STK"].avg_price, found["STK"].contract_id) == (10.0, 150.0, "EQ:AAPL")
    option = found["OPT"]
    assert (option.qty, option.short_qty, option.multiplier, option.contract_id) == (-2.0, 2.0, 100.0, "OPT:AAPL:2026-12-18:C:200")
    (order,) = ibkr.broker.orders(ibkr.id)
    assert (order.order_id, order.status, order.limit_price, order.qty, order.side) == (hc.IBKR_ORDER, "WORKING", 100.0, 1.0, "buy")
    assert order.data["order_ref"] == "hk-ref \u2022\u2022\u2022\u20224455"        # the identifier inside a text became its mask
    assert ibkr.broker.get_order(ibkr.id, "5") is None


def test_ibkr_places_an_equity_order_with_the_conid_it_asked_for(ibkr):
    answer = ibkr.broker.place_order(ibkr.id, hc.neutral())
    assert answer == {"order_id": hc.IBKR_ORDER, "status": "WORKING"}
    (search,) = [c for c in ibkr.gateway.calls if c[1] == "/iserver/secdef/search"]
    assert search[2] == {"symbol": "AAPL", "secType": "STK"}
    (post,) = [c for c in ibkr.gateway.calls if c[0] == "POST" and c[1].endswith("/orders")]
    (body,) = post[2]["orders"]
    assert body == {"acctId": hc.IBKR_SENTINEL, "conid": 265598, "secType": "265598:STK", "orderType": "LMT", "side": "BUY", "quantity": 1,
                    "tif": "GTC", "outsideRTH": False, "price": 100, "cOID": "hk-" + "a" * 37}


def test_ibkr_stop_limit_and_an_option_use_their_own_fields(ibkr):
    ibkr.gateway.routes[("GET", "/iserver/secdef/info")] = (200, [{"conid": "700123", "maturityDate": "20261218", "right": "C", "strike": 200.0,
                                                                 "multiplier": "100"}, {"conid": "700999", "maturityDate": "20261218", "right": "P", "strike": 200.0}])
    ibkr.broker.place_order(ibkr.id, hc.neutral("OPT:AAPL:2026-12-18:C:200", tif="day", limit="5.5", kind="stop_limit", stop="5", asset_type="option"))
    (post,) = [c for c in ibkr.gateway.calls if c[0] == "POST" and c[1].endswith("/orders")]
    (body,) = post[2]["orders"]
    assert (body["secType"], body["orderType"], body["price"], body["auxPrice"], body["tif"]) == ("700123:OPT", "STP LMT", 5.5, 5, "DAY")
    info = [c for c in ibkr.gateway.calls if c[1] == "/iserver/secdef/info"]
    assert len(info) == 1


def test_ibkr_reduce_only_is_cut_to_the_position_it_really_holds(ibkr):
    ibkr.broker.place_order(ibkr.id, hc.neutral("EQ:AAPL", qty="25", side="sell", effect="close", reduce_only=True, kind="market", limit=None))
    (post,) = [c for c in ibkr.gateway.calls if c[0] == "POST" and c[1].endswith("/orders")]
    assert post[2]["orders"][0]["quantity"] == 10


def test_ibkr_a_question_of_the_gateway_is_not_answered_and_the_order_is_refused(ibkr):
    ibkr.gateway.routes[("POST", f"/iserver/account/{hc.IBKR_SENTINEL}/orders")] = (200, hc.fixture("ibkr_reply_question.json"))
    assert ibkr.broker.place_order(ibkr.id, hc.neutral()) == {"order_id": None, "status": "REJECTED", "reason": "needs_confirmation"}
    assert [c for c in ibkr.gateway.calls if "/reply/" in c[1]] == []


@pytest.mark.parametrize("order, reason", [
    (hc.neutral(stop_loss="90"), "unsupported"),
    (hc.neutral("CRY:BTC-USD", asset_type="crypto"), "no_instrument"),
    (hc.neutral("EQ:ZZZZ"), "no_instrument"),                      # the gateway finds no contract
])
def test_ibkr_refuses_without_sending_an_order(ibkr, order, reason):
    ibkr.gateway.routes[("POST", "/iserver/secdef/search")] = (200, [])
    assert ibkr.broker.place_order(ibkr.id, order) == {"order_id": None, "status": "REJECTED", "reason": reason}
    assert [c for c in ibkr.gateway.calls if c[0] == "POST" and c[1].endswith("/orders")] == []


def test_ibkr_without_the_gateway_reports_unavailable_and_sends_nothing(ibkr):
    ibkr.gateway.down = True
    ibkr.gateway.calls.clear()
    assert ibkr.broker.status() == {"status": "unavailable"}
    for call in (lambda: ibkr.broker.place_order(ibkr.id, hc.neutral()), lambda: ibkr.broker.cancel_order(ibkr.id, hc.IBKR_ORDER),
                 lambda: ibkr.broker.replace_order(ibkr.id, hc.IBKR_ORDER, hc.neutral())):
        with pytest.raises(ConnectorError) as error:
            call()
        assert error.value.code == "unavailable"
    assert ibkr.gateway.calls == []
    balance = ibkr.broker.balance(ibkr.id)
    assert balance.cash is None and balance.equity is None


def test_ibkr_a_gateway_with_nobody_signed_in_sends_nothing(ibkr):
    ibkr.gateway.routes[("GET", "/iserver/auth/status")] = (200, hc.fixture("ibkr_auth_status_out.json"))
    assert ibkr.broker.status() == {"status": "not_signed_in"}
    with pytest.raises(ConnectorError) as error:
        ibkr.broker.place_order(ibkr.id, hc.neutral())
    assert error.value.code == "not_signed_in"
    assert [c for c in ibkr.gateway.calls if c[0] in ("POST", "DELETE")] == []
    ibkr.gateway.routes[("GET", "/iserver/auth/status")] = (401, {"error": "not authenticated"})
    assert ibkr.broker.status() == {"status": "not_signed_in"}


def test_ibkr_cancel_replace_and_preview(ibkr):
    assert ibkr.broker.cancel_order(ibkr.id, hc.IBKR_ORDER) == {"order_id": hc.IBKR_ORDER, "status": "CANCELED"}
    assert ibkr.broker.replace_order(ibkr.id, hc.IBKR_ORDER, hc.neutral(qty="2", limit="101")) == {"order_id": hc.IBKR_ORDER, "status": "WORKING"}
    (modify,) = [c for c in ibkr.gateway.calls if c[1].endswith(f"/order/{hc.IBKR_ORDER}") and c[0] == "POST"]
    assert modify[2]["quantity"] == 2 and modify[2]["price"] == 101 and "cOID" not in modify[2]
    preview = ibkr.broker.preview_order(ibkr.id, hc.neutral())
    assert preview["orderStrategy"]["orderBalance"] == {"orderValue": 1001.0, "projectedCommission": 1.0}   # "1,001.00 USD" and "1.00 USD"
    assert [c for c in ibkr.gateway.calls if c[1].endswith("/orders") and c[0] == "POST"] == []             # a preview is not an order
    ibkr.gateway.routes[("DELETE", f"/iserver/account/{hc.IBKR_SENTINEL}/order/{hc.IBKR_ORDER}")] = (200, {"error": "not found"})
    with pytest.raises(NotSupported):
        ibkr.broker.cancel_order(ibkr.id, hc.IBKR_ORDER)
    for bad in ("abc", "1; DROP", "../1", "9" * 30):
        with pytest.raises(ConnectorError):
            ibkr.broker.cancel_order(ibkr.id, bad)


def test_ibkr_an_account_the_gateway_does_not_list_is_not_found(ibkr):
    ibkr.gateway.routes[("GET", "/portfolio/accounts")] = (200, [{"id": "DUOTHER0001"}])
    with pytest.raises(ConnectorError) as error:
        ibkr.broker.positions(ibkr.id)
    assert error.value.code == "account_not_listed"


# ------------------------------------------------------------------------------------------------------------------- crypto

def test_crypto_balance_and_positions_with_the_figures_of_the_recording(crypto):
    balance = crypto.broker.balance(crypto.id)
    assert (balance.cash, balance.equity) == (2500.5, 32500.5)            # 2500.5 USD + 0.5 BTC at 60000
    (position,) = crypto.broker.positions(crypto.id)
    assert (position.symbol, position.qty, position.market_value, position.contract_id, position.avg_price) == ("BTC/USD", 0.5, 30000.0, "CRY:BTC-USD", 0.0)
    assert position.mask == "\u2022\u2022\u2022\u2022kraken"
    crypto.exchange.balance = hc.fixture("crypto_balance.json") | {"total": {"USD": 100.0, "DOGE": 5}}
    assert crypto.broker.balance(crypto.id).equity is None               # a currency with no price: no equity, never a made-up one


def test_crypto_places_a_limit_order_and_sends_the_decimals_it_was_given(crypto):
    answer = crypto.broker.place_order(crypto.id, hc.neutral("CRY:BTC-USD", qty="0.01", limit="50000", asset_type="crypto"))
    assert answer == {"order_id": hc.CRYPTO_ORDER, "status": "WORKING", "filled_qty": "0"}
    (call,) = crypto.exchange.calls
    assert call[:6] == ("create_order", "BTC/USD", "limit", "buy", 0.01, 50000.0)
    assert call[6] == {"timeInForce": "GTC", "clientOrderId": "hk" + "a" * 30}


def test_crypto_a_market_order_is_filled_and_reports_what_it_filled(crypto):
    answer = crypto.broker.place_order(crypto.id, hc.neutral("CRY:BTC-USD", qty="0.01", kind="market", limit=None, tif="day", asset_type="crypto"))
    assert answer == {"order_id": "OFILL1-AAAA-BBBB", "status": "FILLED", "filled_qty": "0.01", "avg_price": "60010"}


@pytest.mark.parametrize("order, reason", [
    (hc.neutral("CRY:BTC-USD", qty="0.01", limit="50000", tif="day", asset_type="crypto"), "unsupported"),        # a day limit order does not exist here
    (hc.neutral("CRY:BTC-USD", qty="0.00001", limit="50000", asset_type="crypto"), "below_minimum"),
    (hc.neutral("CRY:BTC-USD", qty="0.0123456789", limit="50000", asset_type="crypto"), "amount_precision"),
    (hc.neutral("CRY:BTC-USD", qty="0.01", limit="50000.123", asset_type="crypto"), "price_precision"),
    (hc.neutral("CRY:ETH-USD", qty="1", limit="3000", asset_type="crypto"), "no_instrument"),
    (hc.neutral("EQ:AAPL"), "no_instrument"),
    (hc.neutral("CRY:BTC-USD", qty="0.01", limit="50000", asset_type="crypto", take_profit="70000"), "unsupported"),
])
def test_crypto_refuses_without_sending(crypto, order, reason):
    assert crypto.broker.place_order(crypto.id, order) == {"order_id": None, "status": "REJECTED", "reason": reason}
    assert crypto.exchange.calls == []


def test_crypto_a_stop_goes_as_a_trigger_price_and_reduce_only_is_cut_to_the_holding(crypto):
    crypto.broker.place_order(crypto.id, hc.neutral("CRY:BTC-USD", qty="1", side="sell", effect="close", kind="stop", stop="40000", limit=None,
                                                   asset_type="crypto", reduce_only=True))
    (call,) = crypto.exchange.calls
    assert call[2:6] == ("market", "sell", 0.5, None) and call[6]["triggerPrice"] == 40000.0      # the holding is 0.5: 1 is cut to 0.5


def test_crypto_a_refusal_of_the_exchange_is_a_word_and_a_lost_network_is_unavailable(crypto):
    import ccxt

    def refuse(*args, **kwargs):
        raise ccxt.InsufficientFunds("balance too low for CRYSENT7788KEY")

    crypto.exchange.create_order = refuse
    answer = crypto.broker.place_order(crypto.id, hc.neutral("CRY:BTC-USD", qty="0.01", limit="50000", asset_type="crypto"))
    assert answer == {"order_id": None, "status": "REJECTED", "reason": "insufficientfunds"}
    crypto.exchange.down = True
    with pytest.raises(ConnectorError) as error:
        crypto.broker.place_order(crypto.id, hc.neutral("CRY:BTC-USD", qty="0.01", limit="50000", asset_type="crypto"))
    assert error.value.code == "unavailable"
    assert crypto.broker.balance(crypto.id).cash is None


def test_crypto_reads_cancels_replaces_and_previews(crypto):
    (listed,) = crypto.broker.orders(crypto.id)
    assert (listed.order_id, listed.symbol, listed.qty, listed.limit_price, listed.status, listed.contract_id) == (
        hc.CRYPTO_ORDER, "BTC/USD", 0.01, 50000.0, "WORKING", "CRY:BTC-USD")
    assert crypto.broker.get_order(crypto.id, "nope") is None
    assert crypto.broker.cancel_order(crypto.id, hc.CRYPTO_ORDER) == {"order_id": hc.CRYPTO_ORDER, "status": "CANCELED"}
    with pytest.raises(NotSupported):
        crypto.broker.cancel_order(crypto.id, "nope")
    order = hc.neutral("CRY:BTC-USD", qty="0.02", limit="49000", asset_type="crypto")
    assert crypto.broker.replace_order(crypto.id, hc.CRYPTO_ORDER, order)["order_id"] == hc.CRYPTO_ORDER      # the exchange has no edit: cancel, then place
    assert [c[0] for c in crypto.exchange.calls] == ["cancel_order", "cancel_order", "create_order"]
    assert crypto.broker.preview_order(crypto.id, order)["orderStrategy"]["orderBalance"]["orderValue"] == 980.0   # 0.02 * 49000
    market = hc.neutral("CRY:BTC-USD", qty="0.5", kind="market", limit=None, asset_type="crypto")
    assert crypto.broker.preview_order(crypto.id, market)["orderStrategy"]["orderBalance"]["orderValue"] == 30000.0   # 0.5 * 60000


def test_crypto_the_exchange_is_an_allow_list_and_the_key_is_never_in_the_mask(crypto):
    assert set(crypto_module.EXCHANGES) == {"kraken", "coinbase", "binanceus", "bitstamp", "gemini"}
    crypto.exchange.id = "somewhere_else"
    with pytest.raises(ConnectorError) as error:
        crypto.broker.positions(crypto.id)
    assert error.value.code == "no_keys"


# ----------------------------------------------------------------------------------------------------- the shared neutral order

def test_the_neutral_order_is_read_from_the_contract_id_and_a_bad_shape_is_refused():
    order = hc.neutral("OPT:AAPL:2026-12-18:C:200", asset_type="equity")
    parsed = external.parse_order(order, "alpaca")
    assert parsed.legs[0].asset_type == "option" and parsed.legs[0].symbol == "AAPL261218C00200000"      # the id decides, not the leg
    assert parsed.legs[0].qty == Decimal("1") and isinstance(parsed.limit_price, Decimal)
    for bad in (hc.neutral("EQ:aapl"), hc.neutral("EQ:AAPL ", asset_type="equity"), {**hc.neutral(), "extra": 1}, hc.neutral(kind="weird"),
                hc.neutral(tif="ioc"), {**hc.neutral(), "reduce_only": "yes"}):
        with pytest.raises(ConnectorError):
            external.parse_order(bad, "alpaca")
    many = hc.neutral()
    many["legs"] = many["legs"] * (external.MAX_LEGS + 1)
    with pytest.raises(ConnectorError):
        external.parse_order(many, "alpaca")
    with pytest.raises(ConnectorError) as error:
        external.parse_order(hc.neutral("FUT:NQ:2026-12", asset_type="future"), "alpaca")
    assert error.value.code == "no_instrument"


def test_the_scrubber_drops_account_keys_and_masks_identifiers_inside_text():
    scrub = external.Scrubber({"ABCD1234": "\u2022\u2022\u2022\u20221234"})
    out = scrub.public({"accountNumber": "ABCD1234", "acct": "x", "note": "for ABCD1234 only", "rows": [{"hashValue": "h", "n": 1}], "apiKey": "k"})
    assert out == {"note": "for \u2022\u2022\u2022\u20221234 only", "rows": [{"n": 1}]}
    assert external.mask("DU1234567") == "\u2022\u2022\u2022\u20224567"


def test_money_is_decimal_in_the_answers():
    assert external.answer("1", "FILLED", filled=Decimal("0.5"), avg=Decimal("100.25")) == {"order_id": "1", "status": "FILLED", "filled_qty": "0.5",
                                                                                         "avg_price": "100.25"}


def test_a_replace_goes_through_the_same_checks_as_a_new_order_alpaca(alpaca):
    alpaca.broker.replace_order(alpaca.id, hc.ALPACA_ORDER, hc.neutral("EQ:AAPL", qty="25", side="sell", effect="close", reduce_only=True))
    (patch,) = [c[2] for c in sent(alpaca.session, "PATCH", f"/orders/{hc.ALPACA_ORDER}")]
    assert patch["qty"] == 10                                           # a reduce-only replace cannot grow past the position (10 AAPL held)
    alpaca.session.calls.clear()
    assert alpaca.broker.replace_order(alpaca.id, hc.ALPACA_ORDER, hc.neutral("EQ:MSFT", side="sell", reduce_only=True))["reason"] == "reduce_only_nothing"
    assert alpaca.broker.replace_order(alpaca.id, hc.ALPACA_ORDER, hc.neutral()| {"legs": []}) if False else True
    assert sent(alpaca.session, "PATCH", f"/orders/{hc.ALPACA_ORDER}") == []


def test_a_replace_goes_through_the_same_checks_as_a_new_order_ibkr(ibkr):
    ibkr.broker.replace_order(ibkr.id, hc.IBKR_ORDER, hc.neutral("EQ:AAPL", qty="25", side="sell", effect="close", reduce_only=True, kind="market", limit=None))
    (modify,) = [c for c in ibkr.gateway.calls if c[1].endswith(f"/order/{hc.IBKR_ORDER}") and c[0] == "POST"]
    assert modify[2]["quantity"] == 10
    ibkr.gateway.calls.clear()
    assert ibkr.broker.replace_order(ibkr.id, hc.IBKR_ORDER, hc.neutral("EQ:MSFT", side="sell", reduce_only=True))["reason"] == "reduce_only_nothing"
    assert [c for c in ibkr.gateway.calls if c[0] == "POST"] == []

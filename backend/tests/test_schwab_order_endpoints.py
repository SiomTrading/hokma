"""Schwab orders (SPEC-015 18.2, 18.7, 18.8): the exact body sent for every kind of order, the route and method of the four
order calls against a fake Schwab, the id read from `Location`, the 400 of previewOrder with its wrapped retry, and the
local validation of the OrderRequest against REF B.1 (nothing is sent when it fails). Bodies follow REF 4 and the schema of
REF B.1; every figure and symbol is made up. Nothing touches the network."""
import copy
import datetime as dt
import json
import logging
import re
from pathlib import Path

import pytest
from schwab_order_support import OrderFake
from schwab_support import (FAKE_ACCOUNT_NUMBER, FAKE_HASH, FAKE_TOKEN, REF_FIELDS, assert_full, clear_fake, extras,
                            register_accounts)

import hokma
from hokma.schwab import accounts, http, models, orders
from hokma.schwab.http import BAD_REQUEST, INVALID_PARAM, SchwabError

TR = "/trader/v1"
ORDERS = f"{TR}/accounts/{FAKE_HASH}/orders"
PREVIEW_PATH = f"{TR}/accounts/{FAKE_HASH}/previewOrder"
LOCATION = f"https://api.schwabapi.com{ORDERS}/"
ROOT = Path(hokma.__file__).parent


@pytest.fixture(scope="module")
def server():
    with OrderFake() as running:
        yield running


@pytest.fixture
def fake(server):
    clear_fake(server)
    accounts.reset()
    http.reset_limits(limit=100000)
    register_accounts(server)
    yield server
    orders.PREVIEW_WRAPPED_FIRST = False
    accounts.reset()


def created(fake, order_id=987654321, path=ORDERS, method="POST"):
    fake.on(method, path, (201, None, {"Location": LOCATION + str(order_id)}))


def order_calls(fake):
    return [c for c in fake.calls if c.path != TR + "/accounts/accountNumbers"]


def refuses(fn, *args, **kwargs):
    with pytest.raises(SchwabError) as error:
        fn(*args, **kwargs)
    assert error.value.code == INVALID_PARAM and error.value.status == 422 and error.value.detail
    return error.value


# ============================================================================================ the order of REF 4 and B.1

def eq(instruction, quantity, symbol="XYZ"):
    return {"instruction": instruction, "quantity": quantity, "instrument": {"symbol": symbol, "assetType": "EQUITY"}}


def opt(instruction, quantity, symbol):
    return {"instruction": instruction, "quantity": quantity, "instrument": {"symbol": symbol, "assetType": "OPTION"}}


def single(order_type, legs, **more):
    return {"orderType": order_type, "session": "NORMAL", "duration": "DAY", "orderStrategyType": "SINGLE",
            "orderLegCollection": legs, **more}


C45, C50 = "XYZ   261218C00045000", "XYZ   261218C00050000"
P40, P45, C55, C60 = "XYZ   261218P00040000", "XYZ   261218P00045000", "XYZ   261218C00055000", "XYZ   261218C00060000"
NEAR, FAR = "XYZ   261218C00050000", "XYZ   270115C00050000"

# REF 4 recipes as printed (a price is text there), and the types of REF 3.5 that REF 4 does not show, built from B.1.
RECIPES = {
    "market": {"orderType": "MARKET", "session": "NORMAL", "duration": "DAY", "orderStrategyType": "SINGLE",
               "orderLegCollection": [eq("BUY", 15)]},
    "limit_gtc": {"orderType": "LIMIT", "session": "NORMAL", "duration": "GOOD_TILL_CANCEL", "price": "210.50",
                  "orderStrategyType": "SINGLE", "orderLegCollection": [eq("SELL", 10, "AAPL")]},
    "limit_number_price": single("LIMIT", [eq("BUY", 3)], price=12.25),
    "stop": single("STOP", [eq("SELL", 20)], stopPrice="95.00"),
    "stop_limit": {"orderType": "STOP_LIMIT", "session": "NORMAL", "duration": "DAY", "price": "99.50", "stopPrice": "100.00",
                   "orderStrategyType": "SINGLE", "orderLegCollection": [eq("SELL", 20)]},
    "trailing_stop_percent": {"orderType": "TRAILING_STOP", "session": "NORMAL", "duration": "DAY",
                              "stopPriceLinkBasis": "LAST", "stopPriceLinkType": "PERCENT", "stopPriceOffset": 5,
                              "orderStrategyType": "SINGLE", "orderLegCollection": [eq("SELL", 10)]},
    "trailing_stop_value": single("TRAILING_STOP", [eq("SELL", 10)], stopPriceLinkBasis="BID", stopPriceLinkType="VALUE",
                                  stopPriceOffset=1.5),
    "trailing_stop_limit": single("TRAILING_STOP_LIMIT", [eq("SELL", 10)], stopPriceLinkBasis="MARK",
                                  stopPriceLinkType="VALUE", stopPriceOffset=2, priceLinkBasis="MARK", priceLinkType="VALUE",
                                  price="0.25"),
    "market_on_close": {"orderType": "MARKET_ON_CLOSE", "session": "NORMAL", "duration": "DAY", "orderStrategyType": "SINGLE",
                        "orderLegCollection": [eq("SELL", 5)]},
    "limit_on_close": single("LIMIT_ON_CLOSE", [eq("SELL", 5)], price="42.10"),
    "extended_hours_limit": {"orderType": "LIMIT", "session": "SEAMLESS", "duration": "DAY", "price": "10.10",
                             "orderStrategyType": "SINGLE", "specialInstruction": "ALL_OR_NONE", "taxLotMethod": "FIFO",
                             "orderLegCollection": [eq("BUY", 100)]},
    "option_one_leg_occ": {"orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "price": "2.35",
                           "orderStrategyType": "SINGLE", "orderLegCollection": [opt("BUY_TO_OPEN", 1, "AAPL  261218C00250000")]},
    "vertical_net_debit": {"orderType": "NET_DEBIT", "session": "NORMAL", "duration": "DAY", "price": "1.20",
                           "complexOrderStrategyType": "VERTICAL", "orderStrategyType": "SINGLE",
                           "orderLegCollection": [opt("BUY_TO_OPEN", 1, C45), opt("SELL_TO_OPEN", 1, C50)]},
    "vertical_net_credit": single("NET_CREDIT", [opt("SELL_TO_OPEN", 2, C55), opt("BUY_TO_OPEN", 2, C60)], price="0.85",
                                  complexOrderStrategyType="VERTICAL"),
    "calendar": single("NET_DEBIT", [opt("SELL_TO_OPEN", 1, NEAR), opt("BUY_TO_OPEN", 1, FAR)], price="0.60",
                       complexOrderStrategyType="CALENDAR"),
    "iron_condor": single("NET_CREDIT", [opt("BUY_TO_OPEN", 1, P40), opt("SELL_TO_OPEN", 1, P45), opt("SELL_TO_OPEN", 1, C55),
                                         opt("BUY_TO_OPEN", 1, C60)], price="1.10", complexOrderStrategyType="IRON_CONDOR"),
    "trigger_with_child": {
        "orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "price": "34.97", "orderStrategyType": "TRIGGER",
        "orderLegCollection": [eq("BUY", 10)],
        "childOrderStrategies": [{"orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "price": "42.03",
                                  "orderStrategyType": "SINGLE", "orderLegCollection": [eq("SELL", 10)]}]},
    "oco": {"orderStrategyType": "OCO", "childOrderStrategies": [
        {"orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "price": "45.97", "orderStrategyType": "SINGLE",
         "orderLegCollection": [eq("SELL", 2)]},
        {"orderType": "STOP_LIMIT", "session": "NORMAL", "duration": "DAY", "price": "37.00", "stopPrice": "37.03",
         "orderStrategyType": "SINGLE", "orderLegCollection": [eq("SELL", 2)]}]},
    "bracket_trigger_with_oco": {
        "orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "price": "40.00", "orderStrategyType": "TRIGGER",
        "orderLegCollection": [eq("BUY", 10)],
        "childOrderStrategies": [{"orderStrategyType": "OCO", "childOrderStrategies": [
            single("LIMIT", [eq("SELL", 10)], price="45.00"),
            single("STOP", [eq("SELL", 10)], stopPrice="38.00")]}]},
    "exercise": single("EXERCISE", [opt("SELL_TO_CLOSE", 1, C45)], specialInstruction="DO_NOT_REDUCE"),
}


def raw_body(call):
    return call.body.decode("utf-8")


# ------------------------------------------------------------------------------------------- the exact body, per type

@pytest.mark.parametrize("name", sorted(RECIPES))
def test_place_order_sends_the_exact_body_to_the_right_route(fake, name):
    order = RECIPES[name]
    created(fake)
    result = orders.place_order("A1", copy.deepcopy(order))
    call = order_calls(fake)[-1]
    assert (call.method, call.path, call.query) == ("POST", ORDERS, {})
    assert json.loads(call.body) == order                       # exactly the OrderRequest: nothing added, nothing lost
    assert call.headers["content-type"] == "application/json"
    assert call.headers["authorization"] == "Bearer " + FAKE_TOKEN
    assert result == {"order_id": 987654321, "correl_id": "test-correl-id"}
    assert len(order_calls(fake)) == 1


@pytest.mark.parametrize("name", sorted(RECIPES))
def test_preview_order_sends_the_same_body_to_previewOrder(fake, name):
    order = RECIPES[name]
    fake.on("POST", PREVIEW_PATH, PREVIEW)
    preview = orders.preview_order("A1", copy.deepcopy(order))
    call = order_calls(fake)[-1]
    assert (call.method, call.path) == ("POST", PREVIEW_PATH) and json.loads(call.body) == order
    assert isinstance(preview, models.PreviewOrder) and len(order_calls(fake)) == 1


@pytest.mark.parametrize("name", sorted(RECIPES))
def test_replace_order_puts_the_exact_body_and_returns_the_new_id(fake, name):
    path = f"{ORDERS}/55"
    created(fake, 56, path, "PUT")
    result = orders.replace_order("A1", 55, copy.deepcopy(RECIPES[name]))
    call = order_calls(fake)[-1]
    assert (call.method, call.path) == ("PUT", path) and json.loads(call.body) == RECIPES[name]
    assert result == {"order_id": 56, "correl_id": "test-correl-id"}


def test_the_price_goes_as_it_came_text_as_text_and_a_number_as_a_number(fake):
    created(fake)
    orders.place_order("A1", RECIPES["limit_gtc"])
    assert '"price":"210.50"' in raw_body(order_calls(fake)[-1])
    orders.place_order("A1", RECIPES["limit_number_price"])
    assert '"price":12.25' in raw_body(order_calls(fake)[-1])


def test_the_body_is_compact_json_in_the_order_given(fake):
    created(fake)
    orders.place_order("A1", RECIPES["market"])
    assert raw_body(order_calls(fake)[-1]) == json.dumps(RECIPES["market"], separators=(",", ":"))


def test_the_account_ref_is_translated_to_the_hash_and_the_second_account_has_its_own(fake):
    from schwab_support import FAKE_HASH_2

    created(fake)
    created(fake, 7, f"{TR}/accounts/{FAKE_HASH_2}/orders")
    orders.place_order("A1", RECIPES["market"])
    orders.place_order("A2", RECIPES["market"])
    assert [c.path for c in order_calls(fake)] == [ORDERS, f"{TR}/accounts/{FAKE_HASH_2}/orders"]


@pytest.mark.parametrize("ref", ["A9", "", None, 1, FAKE_HASH, FAKE_ACCOUNT_NUMBER, "a1"])
def test_an_unknown_account_ref_is_refused_without_sending_anything(fake, ref):
    for call in (lambda: orders.place_order(ref, RECIPES["market"]), lambda: orders.preview_order(ref, RECIPES["market"]),
                 lambda: orders.replace_order(ref, 1, RECIPES["market"]), lambda: orders.cancel_order(ref, 1)):
        refuses(call)
    assert order_calls(fake) == []


# ------------------------------------------------------------------------------------------- Location and the answers

@pytest.mark.parametrize("location, expected", [
    (LOCATION + "123456789012", 123456789012), (LOCATION + "7", 7), (LOCATION + "7/", 7), (LOCATION + "0012", 12),
    (LOCATION + "abc", None), (LOCATION + "1" * 20, None), (None, None), ("", None), (LOCATION, None),
])
def test_the_order_id_is_the_last_segment_of_location(fake, location, expected):
    fake.on("POST", ORDERS, (201, None, {"Location": location} if location is not None else {}))
    assert orders.place_order("A1", RECIPES["market"]) == {"order_id": expected, "correl_id": "test-correl-id"}


def test_cancel_order_sends_a_delete_and_reads_the_empty_200(fake):
    fake.on("DELETE", f"{ORDERS}/321", (200, None))
    result = orders.cancel_order("A1", "321")
    call = order_calls(fake)[-1]
    assert (call.method, call.path, call.body) == ("DELETE", f"{ORDERS}/321", b"")
    assert "content-type" not in call.headers
    assert result == {"order_id": 321, "correl_id": "test-correl-id"}


@pytest.mark.parametrize("bad", ["", "abc", "12a", "-1", "1.5", " 1", "1 ", "1" * 20, None, True, 1.0, -3, "../1", "1/2", [1]])
def test_a_bad_order_id_is_refused_without_sending(fake, bad):
    refuses(orders.cancel_order, "A1", bad)
    refuses(orders.replace_order, "A1", bad, RECIPES["market"])
    assert order_calls(fake) == []


def test_an_order_id_can_be_a_number_or_its_text(fake):
    fake.on("DELETE", f"{ORDERS}/55", (200, None))
    assert orders.cancel_order("A1", 55)["order_id"] == orders.cancel_order("A1", "55")["order_id"] == 55


@pytest.mark.parametrize("status, code", [(400, "schwab.err.bad_request"), (401, "connections.schwab_expired"),
                                          (403, "connections.schwab_denied"), (404, "schwab.err.not_found"),
                                          (429, "connections.rate_limited"), (500, "schwab.err.upstream"),
                                          (503, "schwab.err.upstream")])
def test_the_errors_of_schwab_keep_their_code_and_errors(fake, status, code):
    body = {"errors": [{"id": "e1", "status": str(status), "title": "t", "detail": "the detail", "source": {"pointer": "/x"}}]}
    for method, path, call in (("POST", ORDERS, lambda: orders.place_order("A1", RECIPES["market"])),
                               ("PUT", f"{ORDERS}/5", lambda: orders.replace_order("A1", 5, RECIPES["market"])),
                               ("DELETE", f"{ORDERS}/5", lambda: orders.cancel_order("A1", 5)),
                               ("POST", PREVIEW_PATH, lambda: orders.preview_order("A1", RECIPES["market"]))):
        http.reset_limits(limit=100000)
        fake.on(method, path, (status, body))
        with pytest.raises(SchwabError) as error:
            call()
        assert error.value.code == code and error.value.errors[0]["detail"] == "the detail"
        assert error.value.correl_id == "test-correl-id"


def test_a_401_renews_the_token_once_and_sends_the_same_order_again(server):
    clear_fake(server)
    accounts.reset()
    register_accounts(server)
    seen = []

    def answer(call):
        seen.append(call.headers["authorization"])
        return (401, {"errors": []}) if call.headers["authorization"].endswith("old") else (201, None, {"Location": LOCATION + "9"})

    server.on("POST", ORDERS, answer)
    http.set_token_provider(lambda: "old", lambda: "new")
    try:
        assert orders.place_order("A1", RECIPES["market"])["order_id"] == 9
    finally:
        http.set_token_provider(lambda: FAKE_TOKEN, lambda: FAKE_TOKEN)
        accounts.reset()
    assert seen == ["Bearer old", "Bearer new"]
    posts = [c for c in server.calls if c.method == "POST" and c.path == ORDERS]
    assert len(posts) == 2 and posts[0].body == posts[1].body


def test_a_network_failure_on_place_or_replace_says_the_outcome_is_unknown(fake):
    accounts.hash_for("A1")                                     # the table is loaded while the fake is up
    http.set_base("http://127.0.0.1:1")                         # nobody listens: the call fails before any answer
    try:
        for call in (lambda: orders.place_order("A1", RECIPES["market"]), lambda: orders.replace_order("A1", 1, RECIPES["market"])):
            with pytest.raises(SchwabError) as error:
                call()
            assert error.value.code == http.UNREACHABLE and "outcome is unknown" in error.value.detail
        for call in (lambda: orders.preview_order("A1", RECIPES["market"]), lambda: orders.cancel_order("A1", 1)):
            with pytest.raises(SchwabError) as error:
                call()
            assert error.value.code == http.UNREACHABLE and error.value.detail is None
    finally:
        http.set_base(f"http://127.0.0.1:{fake._server.server_address[1]}")


# ------------------------------------------------------------------------------------------- previewOrder, 18.8 point 1

def test_preview_400_is_sent_again_wrapped_in_orderStrategy(fake):
    order = RECIPES["bracket_trigger_with_oco"]

    def answer(call):
        body = json.loads(call.body)
        if "orderStrategy" in body:
            return PREVIEW
        return 400, {"errors": [{"status": "400", "title": "Bad Request", "detail": "no orderStrategy"}]}

    fake.on("POST", PREVIEW_PATH, answer)
    preview = orders.preview_order("A1", copy.deepcopy(order))
    calls = order_calls(fake)
    assert [(c.method, c.path) for c in calls] == [("POST", PREVIEW_PATH)] * 2
    assert json.loads(calls[0].body) == order
    assert json.loads(calls[1].body) == {"orderStrategy": order}
    assert extras(preview) == [] and preview.orderValidationResult.accepts


def test_preview_does_not_retry_when_the_first_answer_is_good(fake):
    fake.on("POST", PREVIEW_PATH, PREVIEW)
    orders.preview_order("A1", RECIPES["market"])
    assert len(order_calls(fake)) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_preview_retries_only_on_a_400(fake, status):
    fake.on("POST", PREVIEW_PATH, (status, {"errors": []}))
    with pytest.raises(SchwabError):
        orders.preview_order("A1", RECIPES["market"])
    assert len([c for c in order_calls(fake) if c.path == PREVIEW_PATH]) == (2 if status == 401 else 1)   # 401: the token renewal
    assert all("orderStrategy" not in json.loads(c.body) for c in order_calls(fake))


def test_preview_with_two_400s_raises_the_last_one_after_exactly_two_calls(fake):
    fake.on("POST", PREVIEW_PATH, (400, {"errors": [{"status": "400", "title": "Bad Request", "detail": "bad"}]}))
    with pytest.raises(SchwabError) as error:
        orders.preview_order("A1", RECIPES["market"])
    assert error.value.code == BAD_REQUEST and error.value.status == 400 and error.value.errors[0]["detail"] == "bad"
    assert len(order_calls(fake)) == 2


def test_preview_can_try_the_wrapped_form_first(fake):
    order = RECIPES["market"]
    orders.PREVIEW_WRAPPED_FIRST = True
    fake.on("POST", PREVIEW_PATH, lambda call: PREVIEW if "orderStrategy" in json.loads(call.body) else (400, {"errors": []}))
    orders.preview_order("A1", order)
    assert [json.loads(c.body) for c in order_calls(fake)] == [{"orderStrategy": order}]
    fake.calls.clear()
    fake.on("POST", PREVIEW_PATH, lambda call: (400, {"errors": []}) if "orderStrategy" in json.loads(call.body) else PREVIEW)
    orders.preview_order("A1", order)
    assert [json.loads(c.body) for c in order_calls(fake)] == [{"orderStrategy": order}, order]


def test_a_preview_answer_that_is_not_an_object_is_an_upstream_error(fake):
    fake.on("POST", PREVIEW_PATH, [1, 2])
    with pytest.raises(SchwabError) as error:
        orders.preview_order("A1", RECIPES["market"])
    assert error.value.code == http.UPSTREAM


PREVIEW = {
    "orderId": 0,
    "orderStrategy": {
        "accountNumber": FAKE_ACCOUNT_NUMBER, "advancedOrderType": "NONE", "closeTime": "2026-11-20T21:00:00.000Z",
        "enteredTime": "2026-11-20T15:30:00.000Z",
        "orderBalance": {"orderValue": 2105.0, "projectedAvailableFund": 9000.5, "projectedBuyingPower": 18000.0,
                         "projectedCommission": 0.0},
        "orderStrategyType": "SINGLE", "orderVersion": 1.0, "session": "NORMAL", "status": "ACCEPTED", "allOrNone": False,
        "discretionary": False, "duration": "GOOD_TILL_CANCEL", "filledQuantity": 0.0, "orderType": "LIMIT",
        "orderValue": 2105.0, "price": 210.5, "quantity": 10.0, "remainingQuantity": 10.0, "sellNonMarginableFirst": False,
        "settlementInstruction": "REGULAR", "strategy": "NONE", "amountIndicator": "SHARES",
        "orderLegs": [{"askPrice": 210.6, "bidPrice": 210.4, "lastPrice": 210.5, "markPrice": 210.5,
                       "projectedCommission": 0.0, "quantity": 10.0, "finalSymbol": "AAPL", "legId": 1, "assetType": "EQUITY",
                       "instruction": "SELL"}]},
    "orderValidationResult": {
        "alerts": [{"validationRuleName": "RULE_A", "message": "an alert", "activityMessage": "alert activity",
                    "originalSeverity": "ALERT", "overrideName": "OVERRIDE_A", "overrideSeverity": "ALERT"}],
        "accepts": [{"validationRuleName": "RULE_B", "message": "accepted", "activityMessage": "ok",
                     "originalSeverity": "ACCEPT", "overrideName": "OVERRIDE_B", "overrideSeverity": "ACCEPT"}],
        "rejects": [], "reviews": [{"validationRuleName": "RULE_C", "message": "review it", "activityMessage": "rv",
                                    "originalSeverity": "REVIEW", "overrideName": "OVERRIDE_C", "overrideSeverity": "REVIEW"}],
        "warns": [{"validationRuleName": "RULE_D", "message": "a warning", "activityMessage": "wn",
                   "originalSeverity": "REJECT", "overrideName": "OVERRIDE_D", "overrideSeverity": "ALERT"}]},
    "commissionAndFee": {
        "commission": {"commissionLegs": [{"commissionValues": [{"value": 0.0, "type": "COMMISSION"}]}]},
        "fee": {"feeLegs": [{"feeValues": [{"value": 0.02, "type": "SEC_FEE"}, {"value": 0.01, "type": "TAF_FEE"}]}]},
        "trueCommission": {"commissionLegs": [{"commissionValues": [{"value": 0.0, "type": "COMMISSION"}]}]}},
}


def test_the_preview_is_read_complete(fake):
    fake.on("POST", PREVIEW_PATH, PREVIEW)
    preview = orders.preview_order("A1", RECIPES["limit_gtc"])
    assert extras(preview) == [] and models.dump(preview) == PREVIEW
    assert_full(preview, REF_FIELDS["PreviewOrder"])
    assert_full(preview.orderStrategy, REF_FIELDS["OrderStrategy"])
    assert_full(preview.orderStrategy.orderBalance, REF_FIELDS["OrderBalance"])
    assert_full(preview.orderStrategy.orderLegs[0], REF_FIELDS["OrderLeg"])
    result = preview.orderValidationResult
    assert_full(result, REF_FIELDS["OrderValidationResult"])
    assert_full(result.warns[0], REF_FIELDS["OrderValidationDetail"])
    assert_full(preview.commissionAndFee, REF_FIELDS["CommissionAndFee"])
    assert preview.orderStrategy.orderBalance.projectedBuyingPower == 18000.0
    assert [d.message for d in result.reviews] == ["review it"] and result.rejects == []


def test_the_preview_goes_public_without_the_account_number_or_the_hash(fake):
    fake.on("POST", PREVIEW_PATH, PREVIEW)
    public = accounts.public(orders.preview_order("A1", RECIPES["limit_gtc"]))
    text = json.dumps(public)
    assert FAKE_ACCOUNT_NUMBER not in text and FAKE_HASH not in text
    strategy = public["orderStrategy"]
    assert strategy["account_ref"] == "A1" and strategy["mask"] == "••••5678" and "accountNumber" not in strategy


# ============================================================================================ local validation

def one(**more):
    base = single("LIMIT", [eq("BUY", 1)], price="1.00")
    base.update(more)
    return base


def leg_with(**more):
    leg = eq("BUY", 1)
    leg.update(more)
    return single("LIMIT", [leg], price="1.00")


def instrument_with(**more):
    leg = eq("BUY", 1)
    leg["instrument"].update(more)
    return single("LIMIT", [leg], price="1.00")


def option_leg(symbol, kind="OPTION"):
    return single("LIMIT", [{"instruction": "BUY_TO_OPEN", "quantity": 1, "instrument": {"symbol": symbol, "assetType": kind}}],
                  price="1.00")


def deep(levels):
    node = single("LIMIT", [eq("SELL", 1)], price="1.00")
    for _ in range(levels):
        node = {"orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "price": "1", "orderStrategyType": "TRIGGER",
                "orderLegCollection": [eq("BUY", 1)], "childOrderStrategies": [node]}
    return node


BAD = {
    "not an object": [1, 2], "a string": "MARKET", "none": None, "empty": {},
    "no strategy type": {"orderType": "MARKET", "session": "NORMAL", "duration": "DAY", "orderLegCollection": [eq("BUY", 1)]},
    "single without legs": {"orderType": "MARKET", "session": "NORMAL", "duration": "DAY", "orderStrategyType": "SINGLE"},
    "single without order type": {"session": "NORMAL", "duration": "DAY", "orderStrategyType": "SINGLE",
                                  "orderLegCollection": [eq("BUY", 1)]},
    "single without session": {"orderType": "MARKET", "duration": "DAY", "orderStrategyType": "SINGLE",
                               "orderLegCollection": [eq("BUY", 1)]},
    "single without duration": {"orderType": "MARKET", "session": "NORMAL", "orderStrategyType": "SINGLE",
                                "orderLegCollection": [eq("BUY", 1)]},
    "empty legs": one(orderLegCollection=[]), "legs not a list": one(orderLegCollection=eq("BUY", 1)),
    "oco without children": {"orderStrategyType": "OCO"}, "oco empty children": {"orderStrategyType": "OCO", "childOrderStrategies": []},
    "trigger without children": {**single("LIMIT", [eq("BUY", 1)], price="1"), "orderStrategyType": "TRIGGER"},
    "order type UNKNOWN": one(orderType="UNKNOWN"), "order type invented": one(orderType="ICEBERG"),
    "order type lowercase": one(orderType="limit"), "order type a number": one(orderType=1),
    "session invented": one(session="OVERNIGHT"), "duration UNKNOWN": one(duration="UNKNOWN"), "duration invented": one(duration="GTC"),
    "strategy type invented": one(orderStrategyType="BRACKET"),
    "complex type invented": one(complexOrderStrategyType="SPREAD"),
    "stop link basis": one(stopPriceLinkBasis="LASTT"), "stop link type": one(stopPriceLinkType="PCT"),
    "price link basis": one(priceLinkBasis="X"), "price link type": one(priceLinkType="X"), "stop type": one(stopType="X"),
    "tax lot": one(taxLotMethod="RANDOM"), "special instruction": one(specialInstruction="ALL"),
    "status": one(status="DONE"), "status UNKNOWN": one(status="UNKNOWN"),
    "instruction": leg_with(instruction="HOLD"), "no instruction": leg_with(instruction=None),
    "position effect": leg_with(positionEffect="OPEN"), "position effect UNKNOWN": leg_with(positionEffect="UNKNOWN"),
    "quantity type": leg_with(quantityType="UNITS"), "div cap gains": leg_with(divCapGains="KEEP"),
    "leg type": leg_with(orderLegType="BOND"), "leg id": leg_with(legId="1"), "to symbol": leg_with(toSymbol=""),
    "leg unknown field": leg_with(price=1),
    "no instrument": single("LIMIT", [{"instruction": "BUY", "quantity": 1}], price="1"),
    "instrument not an object": single("LIMIT", [{"instruction": "BUY", "quantity": 1, "instrument": "XYZ"}], price="1"),
    "no asset type": single("LIMIT", [{"instruction": "BUY", "quantity": 1, "instrument": {"symbol": "XYZ"}}], price="1"),
    "asset type invented": instrument_with(assetType="FUTURE"), "no symbol": single(
        "LIMIT", [{"instruction": "BUY", "quantity": 1, "instrument": {"assetType": "EQUITY"}}], price="1"),
    "symbol with spaces": instrument_with(symbol="AAPL 123"), "symbol too long": instrument_with(symbol="A" * 21),
    "symbol with a comma": instrument_with(symbol="A,B"), "symbol empty": instrument_with(symbol=""),
    "symbol a number": instrument_with(symbol=5), "instrument unknown field": instrument_with(strike=5),
    "equity with option fields": instrument_with(putCall="CALL"),
    "quantity zero": leg_with(quantity=0), "quantity negative": leg_with(quantity=-1), "quantity text": leg_with(quantity="10"),
    "quantity bool": leg_with(quantity=True), "quantity nan": leg_with(quantity=float("nan")),
    "quantity infinite": leg_with(quantity=float("inf")), "no quantity": {**one(), "orderLegCollection": [
        {"instruction": "BUY", "instrument": {"symbol": "XYZ", "assetType": "EQUITY"}}]},
    "order quantity zero": one(quantity=0),
    "price text letters": one(price="abc"), "price text negative": one(price="-1.00"), "price exponent": one(price="1e5"),
    "price empty text": one(price=""), "price spaces": one(price=" 1.00"), "price comma": one(price="1,00"),
    "price negative": one(price=-1), "price bool": one(price=True), "price none": one(price=None), "price nan": one(price=float("nan")),
    "stop price bad": one(stopPrice="x"), "offset negative": one(stopPriceOffset=-1), "activation bad": one(activationPrice=[1]),
    "cancel time not iso": one(cancelTime="tomorrow"), "release time date only": one(releaseTime="2026-12-01"),
    "entered time bad": one(enteredTime=5), "cancelable text": one(cancelable="yes"), "editable number": one(editable=1),
    "order id text": one(orderId="1"), "order id negative": one(orderId=-1),
    "account number": one(accountNumber=int(FAKE_ACCOUNT_NUMBER)), "account number text": one(accountNumber=FAKE_ACCOUNT_NUMBER),
    "requested destination": one(requestedDestination="NYSE"), "tag": one(tag="x"), "unknown field": one(leverage=5),
    "destination link name empty": one(destinationLinkName=""), "status description control": one(statusDescription="a\nb"),
    "activity collection bad": one(orderActivityCollection="x"),
    "children not a list": {"orderStrategyType": "OCO", "childOrderStrategies": {"x": 1}},
    "child invalid": {"orderStrategyType": "OCO", "childOrderStrategies": [single("LIMIT", [eq("SELL", 1)], price="1"), {"x": 1}]},
    "replacing not a list": one(replacingOrderCollection="x"), "replacing invalid": one(replacingOrderCollection=[{"orderType": "X"}]),
    "too deep": deep(9), "not json": one(price=1, destinationLinkName="x", orderActivityCollection=[{"a": {1, 2}}]),
    "option symbol 20 chars": option_leg("XYZ   261218C0005000"), "option symbol 22 chars": option_leg("XYZ   261218C000500000"),
    "option lowercase": option_leg("xyz   261218c00050000"), "option no padding": option_leg("XYZ261218C00050000   "[:21]),
    "option root 7": option_leg("XYZABCD261218C0005000"), "option bad month": option_leg("XYZ   261318C00050000"),
    "option bad day": option_leg("XYZ   261232C00050000"), "option feb 30": option_leg("XYZ   260230C00050000"),
    "option bad right": option_leg("XYZ   261218X00050000"), "option strike zero": option_leg("XYZ   261218C00000000"),
    "option strike letters": option_leg("XYZ   261218C0005000A"), "option plain symbol": option_leg("XYZ"),
    "option dashes": option_leg("XYZ---261218C00050000"), "option padding after the date": option_leg("XYZ26121 8C00050000"),
    "option empty": option_leg(""), "option leading space": option_leg(" XYZ  261218C00050000"),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_a_bad_order_is_refused_locally_and_nothing_is_sent(fake, name):
    order = BAD[name]
    for call in (lambda: orders.place_order("A1", order), lambda: orders.preview_order("A1", order),
                 lambda: orders.replace_order("A1", 1, order)):
        error = refuses(call)
        assert FAKE_HASH not in error.detail and FAKE_TOKEN not in error.detail
    assert order_calls(fake) == []


def test_the_detail_names_the_path_of_the_problem(fake):
    nested = {"orderStrategyType": "TRIGGER", "orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "price": "1",
              "orderLegCollection": [eq("BUY", 1)], "childOrderStrategies": [{
                  "orderStrategyType": "OCO", "childOrderStrategies": [
                      single("LIMIT", [eq("SELL", 1)], price="1"), single("STOP", [eq("HOLD", 1)], stopPrice="1")]}]}
    detail = refuses(orders.place_order, "A1", nested).detail
    assert detail.startswith("order.childOrderStrategies[0].childOrderStrategies[1].orderLegCollection[0].instruction")
    assert "BUY_TO_OPEN" in refuses(orders.place_order, "A1", leg_with(instruction="HOLD")).detail
    assert "orderType" in refuses(orders.place_order, "A1", one(orderType="UNKNOWN")).detail
    assert "account_ref" in refuses(orders.place_order, "A1", one(accountNumber=1)).detail


def test_the_account_number_in_a_bad_order_never_comes_back_in_the_error(fake):
    error = refuses(orders.place_order, "A1", one(accountNumber=int(FAKE_ACCOUNT_NUMBER)))
    assert FAKE_ACCOUNT_NUMBER not in error.detail and FAKE_ACCOUNT_NUMBER not in repr(error.args)


def test_a_symbol_with_a_secret_looking_text_in_an_unknown_key_is_not_echoed_whole(fake):
    error = refuses(orders.place_order, "A1", one(**{"k" * 100: 1}))
    assert len(error.detail) < 200


ENUM_FIELDS = {"session": "Session", "duration": "Duration", "orderType": "OrderTypeRequest",
               "complexOrderStrategyType": "ComplexOrderStrategyType", "stopPriceLinkBasis": "StopPriceLinkBasis",
               "stopPriceLinkType": "StopPriceLinkType", "stopType": "StopType", "priceLinkBasis": "PriceLinkBasis",
               "priceLinkType": "PriceLinkType", "taxLotMethod": "TaxLotMethod", "specialInstruction": "SpecialInstruction"}


@pytest.mark.parametrize("field", sorted(ENUM_FIELDS))
def test_every_value_of_every_order_enum_is_accepted_except_unknown(field):
    for value in models.enum_values(ENUM_FIELDS[field]):
        order = one(**{field: value})
        if value == "UNKNOWN":
            with pytest.raises(SchwabError):
                orders.validate_order(order)
        else:
            assert orders.validate_order(order) is order, (field, value)


def test_orderType_has_no_unknown_and_all_thirteen_types_pass():
    values = models.enum_values("OrderTypeRequest")
    assert "UNKNOWN" not in values and len(values) == 14
    for value in values:
        orders.validate_order(one(orderType=value))


@pytest.mark.parametrize("strategy", ["SINGLE", "CANCEL", "RECALL", "PAIR", "FLATTEN", "TWO_DAY_SWAP", "BLAST_ALL", "OCO", "TRIGGER"])
def test_every_order_strategy_type_is_accepted_with_what_it_needs(strategy):
    order = {"orderStrategyType": strategy}
    if strategy in ("SINGLE", "TRIGGER"):
        order.update(single("LIMIT", [eq("BUY", 1)], price="1"), orderStrategyType=strategy)
    if strategy in ("OCO", "TRIGGER"):
        order["childOrderStrategies"] = [single("LIMIT", [eq("SELL", 1)], price="1")]
    assert orders.validate_order(order) is order


@pytest.mark.parametrize("field, enum", [("instruction", "Instruction"), ("positionEffect", "PositionEffect"),
                                         ("quantityType", "QuantityType"), ("divCapGains", "DivCapGains"),
                                         ("orderLegType", "OrderLegType")])
def test_every_value_of_every_leg_enum_is_accepted_except_unknown(field, enum):
    for value in models.enum_values(enum):
        order = leg_with(**{field: value})
        if value == "UNKNOWN":
            with pytest.raises(SchwabError):
                orders.validate_order(order)
        else:
            assert orders.validate_order(order) is order, (field, value)


def test_every_instrument_kind_is_accepted_with_its_own_fields():
    for kind in models.enum_values("AccountAssetType"):
        symbol = "XYZ   261218C00050000" if kind == "OPTION" else "XYZ"
        leg = {"instruction": "BUY", "quantity": 1, "instrument": {"symbol": symbol, "assetType": kind}}
        assert orders.validate_order(single("MARKET", [leg]))
    full_option = {"assetType": "OPTION", "symbol": "XYZ   261218C00050000", "cusip": "123456789", "description": "an option",
                   "instrumentId": 5, "netChange": -0.5, "putCall": "CALL", "optionMultiplier": 100, "type": "VANILLA",
                   "underlyingSymbol": "XYZ", "optionDeliverables": [{"symbol": "XYZ", "deliverableUnits": 100.0,
                                                                      "apiCurrencyType": "USD", "assetType": "EQUITY"}]}
    assert orders.validate_order(single("LIMIT", [{"instruction": "BUY_TO_OPEN", "quantity": 1, "instrument": full_option}],
                                        price="1"))
    for kind, extra in (("CASH_EQUIVALENT", {"type": "MONEY_MARKET_FUND"}),
                        ("FIXED_INCOME", {"maturityDate": "2030-01-01T00:00:00.000Z", "factor": 1.0, "variableRate": 0.5})):
        leg = {"instruction": "BUY", "quantity": 1, "instrument": {"symbol": "XYZ", "assetType": kind, **extra}}
        assert orders.validate_order(single("MARKET", [leg]))
    assert orders.validate_order(single("MARKET", [{"instruction": "SELL", "quantityType": "ALL_SHARES",
                                                    "instrument": {"symbol": "XYZ", "assetType": "EQUITY"}}]))


def test_every_field_of_the_schema_is_accepted_in_one_order():
    order = {"session": "AM", "duration": "GOOD_TILL_CANCEL", "orderType": "STOP_LIMIT", "cancelTime": "2026-12-01T15:00:00.000Z",
             "complexOrderStrategyType": "NONE", "quantity": 5, "filledQuantity": 0, "remainingQuantity": 5,
             "destinationLinkName": "dest", "releaseTime": "2026-11-30T14:30:00+00:00", "stopPrice": 9.9,
             "stopPriceLinkBasis": "MANUAL", "stopPriceLinkType": "VALUE", "stopPriceOffset": "0.5", "stopType": "STANDARD",
             "priceLinkBasis": "MANUAL", "priceLinkType": "VALUE", "price": 10, "taxLotMethod": "HIGH_COST",
             "orderLegCollection": [{"orderLegType": "EQUITY", "legId": 1, "instruction": "BUY", "positionEffect": "OPENING",
                                     "quantity": 5, "quantityType": "SHARES", "divCapGains": "REINVEST", "toSymbol": "ABC",
                                     "instrument": {"symbol": "XYZ", "assetType": "EQUITY"}}],
             "activationPrice": 9.5, "specialInstruction": "ALL_OR_NONE_DO_NOT_REDUCE", "orderStrategyType": "SINGLE",
             "orderId": 12, "cancelable": True, "editable": True, "status": "WORKING", "enteredTime": "2026-11-20T15:00:00.000Z",
             "closeTime": "2026-11-20T21:00:00.000Z", "orderActivityCollection": [{"activityType": "EXECUTION"}],
             "replacingOrderCollection": [single("LIMIT", [eq("BUY", 5)], price="9")],
             "childOrderStrategies": [single("LIMIT", [eq("SELL", 5)], price="12")], "statusDescription": "text"}
    assert orders.validate_order(order) is order


def test_occ_symbol_follows_ref_4():
    assert orders.occ_symbol("AAPL", dt.date(2026, 12, 18), "C", 250) == "AAPL  261218C00250000"
    assert orders.occ_symbol("XYZ", dt.date(2026, 12, 18), "C", 45) == "XYZ   261218C00045000"
    assert orders.occ_symbol("SPXW", dt.date(2027, 1, 15), "P", 4500.5) == "SPXW  270115P04500500"
    assert orders.occ_symbol("ABCDEF", dt.date(2026, 1, 2), "P", 0.5) == "ABCDEF260102P00000500"
    for symbol in ("AAPL  261218C00250000", "ABCDEF260102P00000500"):
        assert len(symbol) == 21
        assert orders.validate_order(option_leg(symbol))
    for args in (("ABCDEFG", dt.date(2026, 1, 2), "C", 1), ("A", dt.date(2026, 1, 2), "X", 1), ("A", dt.date(2026, 1, 2), "C", 0),
                 ("a", dt.date(2026, 1, 2), "C", 1), ("", dt.date(2026, 1, 2), "C", 1)):
        refuses(orders.occ_symbol, *args)


def test_the_four_calls_validate_before_hashing_so_a_bad_order_never_needs_the_table(fake):
    refuses(orders.place_order, "A9", {})                       # the order is checked first: either way nothing is sent
    assert order_calls(fake) == []


# ============================================================================================ nothing secret leaves

@pytest.mark.parametrize("status", [201, 400, 403, 404, 500])
def test_no_log_error_or_return_carries_the_hash_the_number_or_the_token(fake, caplog, status):
    caplog.set_level(logging.DEBUG)
    leaks = [FAKE_HASH, FAKE_ACCOUNT_NUMBER, FAKE_TOKEN]
    body = {"errors": [{"id": "1", "status": str(status), "title": "t", "detail": "d", "source": {"pointer": "/x"}}]}
    answer = (201, None, {"Location": LOCATION + "5"}) if status == 201 else (status, body)
    fake.on("POST", ORDERS, answer)
    fake.on("PUT", f"{ORDERS}/5", answer)
    fake.on("DELETE", f"{ORDERS}/5", (200, None) if status == 201 else (status, body))
    fake.on("POST", PREVIEW_PATH, PREVIEW if status == 201 else (status, body))
    seen = []
    for call in (lambda: orders.place_order("A1", RECIPES["market"]), lambda: orders.replace_order("A1", 5, RECIPES["market"]),
                 lambda: orders.cancel_order("A1", 5), lambda: orders.preview_order("A1", RECIPES["market"])):
        http.reset_limits(limit=100000)
        try:
            seen.append(repr(call()) if status != 201 else json.dumps(accounts.public(call()), default=repr))
        except SchwabError as error:
            seen.append(f"{error} {error.code} {error.detail} {error.errors}")
    everything = caplog.text + "\n".join(seen)
    for leak in leaks:
        assert leak not in everything, leak
    logged = [r.getMessage() for r in caplog.records if r.name == "hokma.schwab"]
    assert any("POST /trader/v1/accounts/{accountNumber}/orders" in line for line in logged)
    assert any("DELETE /trader/v1/accounts/{accountNumber}/orders/{orderId}" in line for line in logged)
    assert any("POST /trader/v1/accounts/{accountNumber}/previewOrder" in line for line in logged)
    assert not any("987654" in line or "XYZ" in line or "BUY" in line for line in logged)       # no id, no body


def test_the_body_of_an_order_is_never_logged(fake, caplog):
    caplog.set_level(logging.DEBUG)
    created(fake)
    orders.place_order("A1", RECIPES["limit_gtc"])
    assert "AAPL" not in caplog.text and "210.50" not in caplog.text and "orderLegCollection" not in caplog.text


# ============================================================================================ where the code lives

def test_endpoints_py_stays_read_only_and_orders_py_is_the_only_writer():
    source = (ROOT / "schwab" / "endpoints.py").read_text(encoding="utf-8")
    assert not re.search(r"request\(|consumer|\"POST\"|\"PUT\"|\"DELETE\"|place_order|cancel_order|preview_order", source)
    for path in (ROOT / "schwab").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name not in ("orders.py", "http.py", "auth.py"):
            assert not re.search(r"http\.request\(\s*\"(?:POST|PUT|DELETE|PATCH)\"", text), path.name
    assert not re.search(r"^\s*(?:import|from)\s+(?:urllib|http\.client|socket|requests|httpx)", 
                         (ROOT / "schwab" / "orders.py").read_text(encoding="utf-8"), re.M)


def test_orders_py_names_the_consumer_and_never_the_host():
    source = (ROOT / "schwab" / "orders.py").read_text(encoding="utf-8")
    assert 'CONSUMER = "hokma_broker"' in source and "api.schwabapi.com" not in source
    assert source.count("consumer=CONSUMER") == 1               # one place sends: `_sent`

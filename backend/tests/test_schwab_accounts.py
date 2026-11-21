"""accounts.py (SPEC-015 5.9 and 5.7): a stable 'account_ref' per account, the hash that never leaves, every accountNumber of
every schema masked, UserDetails and StreamerInfo reduced, and the data of the "Ver cuenta" panel."""
import datetime as dt
import json

import pytest
from schwab_support import (FAKE_ACCOUNT_NUMBER, FAKE_ACCOUNT_NUMBER_2, FAKE_HASH, FAKE_HASH_2, FIXTURES, FakeSchwab,
                            clear_fake, load, register_accounts)

from hokma.schwab import accounts, endpoints, http, models
from hokma.schwab.http import EXPIRED, INVALID_PARAM, SchwabError

TR = "/trader/v1"
NOW = dt.datetime(2026, 11, 20, 15, 30, tzinfo=dt.timezone.utc)
MASK_1, MASK_2 = "••••5678", "••••4321"
ACCOUNT_NUMBERS = TR + "/accounts/accountNumbers"
OPEN_STATES = {"AWAITING_PARENT_ORDER", "AWAITING_CONDITION", "AWAITING_STOP_CONDITION", "AWAITING_MANUAL_REVIEW", "ACCEPTED",
               "AWAITING_UR_OUT", "PENDING_ACTIVATION", "QUEUED", "WORKING", "NEW", "AWAITING_RELEASE_TIME",
               "PENDING_ACKNOWLEDGEMENT", "PENDING_CANCEL", "PENDING_REPLACE", "PENDING_RECALL"}      # 5.7


@pytest.fixture(scope="module")
def server():
    with FakeSchwab() as running:
        yield running


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake(server, clock):
    clear_fake(server)
    accounts.reset()
    accounts.set_clock(clock)
    endpoints.set_clock(lambda: NOW)
    http.reset_limits(limit=100000)
    register_accounts(server)
    yield server
    accounts.set_clock()
    endpoints.set_clock()
    accounts.reset()


def numbers_calls(server):
    return [call for call in server.calls if call.path == ACCOUNT_NUMBERS]


def walk(value):
    """Every key and every scalar of a JSON value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)
    else:
        yield value


def assert_no_secret(out):
    text = json.dumps(out, ensure_ascii=False)
    for secret in (FAKE_ACCOUNT_NUMBER, FAKE_ACCOUNT_NUMBER_2, FAKE_HASH, FAKE_HASH_2):
        assert secret not in text
    assert "accountNumber" not in set(walk(out)) and "hashValue" not in set(walk(out))


# ------------------------------------------------------------------------------------------------- the table


def test_account_ref_follows_the_order_of_the_answer_and_masks_the_number(fake):
    assert accounts.listing() == [{"account_ref": "A1", "mask": MASK_1}, {"account_ref": "A2", "mask": MASK_2}]
    assert accounts.MASK_DOTS == "•" * 4 and MASK_1.endswith(FAKE_ACCOUNT_NUMBER[-4:])
    assert accounts.hash_for("A1") == FAKE_HASH and accounts.hash_for("A2") == FAKE_HASH_2
    assert len(numbers_calls(fake)) == 1                                # built once, on first use
    assert numbers_calls(fake)[0].method == "GET"


def test_the_table_is_read_again_after_an_hour_and_keeps_every_reference(fake, clock):
    accounts.listing()
    clock.now += 3599
    accounts.listing()
    assert len(numbers_calls(fake)) == 1
    clock.now += 2
    fake.on("GET", ACCOUNT_NUMBERS, [{"accountNumber": FAKE_ACCOUNT_NUMBER_2, "hashValue": "B" * 32},
                                     {"accountNumber": FAKE_ACCOUNT_NUMBER, "hashValue": "C" * 32}])      # other order, new hashes
    assert accounts.hash_for("A1") == "C" * 32 and accounts.hash_for("A2") == "B" * 32                      # same accounts, same refs
    assert accounts.listing() == [{"account_ref": "A1", "mask": MASK_1}, {"account_ref": "A2", "mask": MASK_2}]
    assert len(numbers_calls(fake)) == 2


def test_a_new_account_takes_the_next_reference_and_a_gone_one_is_never_reused(fake, clock):
    accounts.listing()
    fake.on("GET", ACCOUNT_NUMBERS, [{"accountNumber": FAKE_ACCOUNT_NUMBER, "hashValue": FAKE_HASH}])      # A2 is gone
    clock.now += 3601
    assert [x["account_ref"] for x in accounts.listing()] == ["A1"]
    with pytest.raises(SchwabError) as error:
        accounts.hash_for("A2")
    assert error.value.code == INVALID_PARAM
    fake.on("GET", ACCOUNT_NUMBERS, [{"accountNumber": FAKE_ACCOUNT_NUMBER, "hashValue": FAKE_HASH},
                                     {"accountNumber": "55556666", "hashValue": "D" * 32}])
    clock.now += 3601
    assert accounts.listing() == [{"account_ref": "A1", "mask": MASK_1}, {"account_ref": "A3", "mask": "••••6666"}]


def test_an_unknown_reference_a_hash_or_a_number_is_refused_without_naming_them(fake):
    for bad in ("A9", "a1", FAKE_HASH, FAKE_ACCOUNT_NUMBER, "", None, 1, ["A1"]):
        with pytest.raises(SchwabError) as error:
            accounts.hash_for(bad)
        assert error.value.code == INVALID_PARAM and error.value.status == 422
        assert FAKE_HASH not in str(error.value.detail) and FAKE_ACCOUNT_NUMBER not in str(error.value.detail)


def test_the_first_build_raises_but_a_failed_refresh_keeps_the_old_table(fake, clock, caplog):
    fake.on("GET", ACCOUNT_NUMBERS, (401, {"errors": []}))
    with pytest.raises(SchwabError) as error:
        accounts.hash_for("A1")
    assert error.value.code == EXPIRED
    fake.on("GET", ACCOUNT_NUMBERS, load("account_numbers.json"))
    assert accounts.hash_for("A1") == FAKE_HASH
    fake.on("GET", ACCOUNT_NUMBERS, (500, {"errors": []}))
    clock.now += 3601
    before = len(numbers_calls(fake))
    with caplog.at_level("DEBUG"):
        assert accounts.hash_for("A1") == FAKE_HASH                     # served from the old table
        assert accounts.hash_for("A2") == FAKE_HASH_2
    assert len(numbers_calls(fake)) == before + 1                       # the failed refresh is not repeated at once
    text = " ".join(r.getMessage() for r in caplog.records)
    assert FAKE_HASH not in text and FAKE_ACCOUNT_NUMBER not in text and "upstream" in text
    clock.now += 61
    fake.on("GET", ACCOUNT_NUMBERS, load("account_numbers.json"))
    accounts.listing()
    assert len(numbers_calls(fake)) == before + 2


def test_the_table_ignores_pairs_without_number_or_hash(fake):
    fake.on("GET", ACCOUNT_NUMBERS, [{"accountNumber": FAKE_ACCOUNT_NUMBER}, {"hashValue": FAKE_HASH}, {},
                                     {"accountNumber": FAKE_ACCOUNT_NUMBER_2, "hashValue": FAKE_HASH_2}])
    assert accounts.listing() == [{"account_ref": "A1", "mask": MASK_2}]


def test_reset_forgets_the_table(fake):
    accounts.listing()
    accounts.reset()
    accounts.listing()
    assert len(numbers_calls(fake)) == 2


# ----------------------------------------------------------------------------------------------------- public

def test_public_masks_the_account_number_of_an_account_and_keeps_the_rest(fake):
    data = load("account_margin.json")
    out = accounts.public(models.Account.read(data))
    secured = out["securitiesAccount"]
    assert (secured["account_ref"], secured["mask"]) == ("A1", MASK_1) and "accountNumber" not in secured
    assert_no_secret(out)
    expected = {k: v for k, v in data["securitiesAccount"].items() if k != "accountNumber"}
    assert {k: v for k, v in secured.items() if k not in ("account_ref", "mask")} == expected
    cash = accounts.public(models.Account.read(load("account_cash.json")))
    assert (cash["securitiesAccount"]["account_ref"], cash["securitiesAccount"]["mask"]) == ("A2", MASK_2)


def test_public_masks_the_integer_account_number_of_an_order_and_of_its_nested_orders(fake):
    data = load("orders.json")
    out = accounts.public([models.Order.read(item) for item in data])
    assert [(o["account_ref"], o["mask"]) for o in out] == [("A1", MASK_1), ("A1", MASK_1), ("A2", MASK_2), ("A2", MASK_2)]
    first = out[0]
    assert first["childOrderStrategies"][0]["account_ref"] == "A1" and first["replacingOrderCollection"][0]["mask"] == MASK_1
    assert first["orderId"] == 1000000001 and first["status"] == "WORKING"
    assert len(first["orderActivityCollection"][0]["executionLegs"]) == 2
    assert_no_secret(out)


def test_public_masks_the_account_number_of_a_transaction(fake):
    out = accounts.public([models.Transaction.read(item) for item in load("transactions.json")])
    assert (out[0]["account_ref"], out[0]["mask"]) == ("A1", MASK_1) and out[0]["activityId"] == 777001
    assert len(out[0]["transferItems"]) == 11
    assert_no_secret(out)


def test_public_masks_every_account_of_the_user_preferences_and_reduces_the_streamer(fake):
    out = accounts.public(models.UserPreference.read(load("user_preference.json")[0]))
    assert [(a["account_ref"], a["mask"]) for a in out["accounts"]] == [("A1", MASK_1), ("A2", MASK_2)]
    assert out["accounts"][0]["nickName"] and out["accounts"][0]["primaryAccount"] is True
    assert out["streamerInfo"] == [{"streamerSocketUrl": "wss://fake-streamer.example/ws"}]
    assert out["offers"][0]["level2Permissions"] is True
    assert_no_secret(out)


def test_public_masks_the_account_number_of_an_order_strategy_of_a_preview(fake):
    preview = models.PreviewOrder.read({"orderId": 1, "orderStrategy": {"accountNumber": FAKE_ACCOUNT_NUMBER, "quantity": 2,
                                                                         "orderLegs": [{"finalSymbol": "FAKE"}]}})
    out = accounts.public(preview)
    assert out["orderStrategy"]["account_ref"] == "A1" and out["orderStrategy"]["mask"] == MASK_1
    assert out["orderStrategy"]["quantity"] == 2.0 and out["orderStrategy"]["orderLegs"] == [{"finalSymbol": "FAKE"}]
    assert_no_secret(out)


def test_public_drops_the_hash_of_the_pairs(fake):
    out = accounts.public(endpoints.account_numbers())
    assert out == [{"account_ref": "A1", "mask": MASK_1}, {"account_ref": "A2", "mask": MASK_2}]
    assert_no_secret(out)


def test_public_reduces_user_details_to_type_first_name_and_last_name(fake):
    transaction = models.Transaction.read(load("transactions.json")[0])
    assert transaction.user.login == "fake-login"                        # the model has it all
    out = accounts.public(transaction)
    assert out["user"] == {"type": "CLIENT_USER", "firstName": "Ana", "lastName": "Prueba"}
    standalone = models.UserDetails.read({**models.dump(transaction.user), "newIdentifier": "x"})
    assert accounts.public(standalone) == {"type": "CLIENT_USER", "firstName": "Ana", "lastName": "Prueba"}
    text = json.dumps(out)
    for removed in ("cdDomainId", "login", "userId", "systemUserName", "brokerRepCode", "FAKE_DOMAIN", "fake-login", "424242",
                    "fake-system-user", "FAKEREP"):
        assert removed not in text


def test_public_reduces_stream_info_to_the_socket_url(fake):
    info = models.StreamerInfo.read({**load("user_preference.json")[0]["streamerInfo"][0], "newId": "x"})
    assert info.schwabClientCustomerId == "fake-customer-id"
    assert accounts.public(info) == {"streamerSocketUrl": "wss://fake-streamer.example/ws"}
    text = json.dumps(accounts.public(models.UserPreference.read(load("user_preference.json")[0])))
    for removed in ("schwabClientCustomerId", "schwabClientCorrelId", "schwabClientChannel", "schwabClientFunctionId",
                    "fake-customer-id", "fake-correl-id", "fake-channel", "fake-function-id"):
        assert removed not in text


def test_public_also_cleans_what_stayed_in_extra_as_raw_json(fake):
    order = models.Order.read({"orderId": 1, "futureField": {"accountNumber": FAKE_ACCOUNT_NUMBER, "hashValue": FAKE_HASH,
                                                              "nested": [{"accountNumber": 87654321}]},
                               "user": {"login": "fake-login", "type": "CLIENT_USER", "userId": 4},
                               "streamerInfo": [{"streamerSocketUrl": "wss://x", "schwabClientChannel": "c"}]})
    out = accounts.public(order)
    assert out["futureField"] == {"account_ref": "A1", "mask": MASK_1, "nested": [{"account_ref": "A2", "mask": MASK_2}]}
    assert out["user"] == {"type": "CLIENT_USER"} and out["streamerInfo"] == [{"streamerSocketUrl": "wss://x"}]
    assert_no_secret(out)
    assert accounts.public({"accountNumber": 12345678, "hashValue": FAKE_HASH, "k": [1, {"accountNumber": "87654321"}]}) == {
        "account_ref": "A1", "mask": MASK_1, "k": [1, {"account_ref": "A2", "mask": MASK_2}]}


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.json")))
def test_no_fixture_leaves_a_number_or_a_hash_through_public(fake, name):
    readers = {"quotes.json": models.QuoteResponse, "quote_single.json": models.EquityResponse,
               "chains.json": models.OptionChain, "expirationchain.json": models.ExpirationChain,
               "pricehistory.json": models.CandleList, "movers.json": models.ScreenerList,
               "instruments.json": models.InstrumentList, "instruments_fundamental.json": models.InstrumentList,
               "instrument_cusip.json": models.InstrumentResponse, "account_margin.json": models.Account,
               "account_cash.json": models.Account, "order.json": models.Order, "market_equity.json": None,
               "markets.json": None}
    lists = {"account_numbers.json": models.AccountNumberHash, "accounts.json": models.Account,
             "accounts_no_positions.json": models.Account, "orders.json": models.Order,
             "transactions.json": models.Transaction, "transaction.json": models.Transaction,
             "user_preference.json": models.UserPreference}
    data = load(name)
    if name in readers and readers[name] is not None:
        obj = readers[name].read(data)
    elif name in lists:
        obj = [lists[name].read(item) for item in data]
    else:
        obj = data
    out = accounts.public(obj)
    assert_no_secret(out)
    json.dumps(out)                                                      # only JSON types


def test_public_keeps_extra_and_leaves_scalars_and_quotes_alone(fake):
    assert accounts.public(5) == 5 and accounts.public(None) is None and accounts.public("x") == "x"
    order = models.Order.read({"accountNumber": 12345678, "newKey": [1, 2]})
    assert accounts.public(order) == {"account_ref": "A1", "mask": MASK_1, "newKey": [1, 2]}
    quotes = models.QuoteResponse.read(load("quotes.json"))
    assert accounts.public(quotes) == load("quotes.json")


def test_an_account_the_table_does_not_know_shows_only_its_mask_and_refreshes_at_most_every_30_seconds(fake, clock):
    accounts.listing()
    assert len(numbers_calls(fake)) == 1
    out = accounts.public(models.Order.read({"accountNumber": 99990001}))
    assert out == {"account_ref": None, "mask": "••••0001"} and len(numbers_calls(fake)) == 2
    accounts.public(models.Order.read({"accountNumber": 99990002}))
    assert len(numbers_calls(fake)) == 2                                 # not again within 30 s
    clock.now += 31
    accounts.public(models.Order.read({"accountNumber": 99990002}))
    assert len(numbers_calls(fake)) == 3


# -------------------------------------------------------------------------------------------------- panel


def serve_panel(fake, accounts_data=None, orders_data=None):
    fake.on("GET", TR + "/accounts", accounts_data if accounts_data is not None else load("accounts.json"))
    fake.on("GET", TR + "/orders", orders_data if orders_data is not None else load("orders.json"))


def test_panel_data_has_the_balances_positions_and_open_orders_of_each_account(fake):
    serve_panel(fake)
    panel = accounts.panel_data()
    assert panel["as_of"] == "2026-11-20T15:30:00.000Z"
    margin, cash = panel["accounts"]
    source = load("accounts.json")
    m, c = source[0]["securitiesAccount"], source[1]["securitiesAccount"]
    assert (margin["account_ref"], margin["mask"], margin["type"]) == ("A1", MASK_1, "MARGIN")
    assert margin["currentBalances"] == {k: m["currentBalances"][k] for k in ("equity", "buyingPower", "availableFunds")}
    assert margin["initialBalances"] == {"liquidationValue": m["initialBalances"]["liquidationValue"]}
    assert (cash["account_ref"], cash["mask"], cash["type"]) == ("A2", MASK_2, "CASH")
    assert cash["currentBalances"] == {k: c["currentBalances"][k] for k in ("totalCash", "cashAvailableForTrading")}
    assert cash["initialBalances"] == {"liquidationValue": c["initialBalances"]["liquidationValue"]}
    # positions: the 8 fields of 5.7
    assert len(margin["positions"]) == 6 and len(cash["positions"]) == 1
    first = margin["positions"][0]
    assert set(first) == {"symbol", "assetType", "longQuantity", "shortQuantity", "averagePrice", "marketValue",
                          "currentDayProfitLoss", "currentDayProfitLossPercentage"}
    raw = m["positions"][0]
    assert first == {"symbol": raw["instrument"]["symbol"], "assetType": raw["instrument"]["assetType"],
                     **{k: raw[k] for k in ("longQuantity", "shortQuantity", "averagePrice", "marketValue",
                                            "currentDayProfitLoss", "currentDayProfitLossPercentage")}}
    assert [p["assetType"] for p in margin["positions"]] == ["EQUITY", "OPTION", "FIXED_INCOME", "MUTUAL_FUND", "CASH_EQUIVALENT", "INDEX"]
    # orders: only the open ones, by account
    assert len(margin["orders"]) == 1 and len(cash["orders"]) == 1
    order = margin["orders"][0]
    assert set(order) == {"legs", "orderType", "price", "stopPrice", "duration", "status", "filledQuantity",
                          "remainingQuantity", "enteredTime"}
    assert order["status"] == "WORKING" and cash["orders"][0]["status"] == "QUEUED"
    leg_source = load("orders.json")[0]["orderLegCollection"][0]
    assert order["legs"] == [{"symbol": leg_source["instrument"]["symbol"], "instruction": leg_source["instruction"],
                              "quantity": leg_source["quantity"]}]
    assert_no_secret(panel)


def test_panel_data_asks_for_positions_and_the_last_59_days_23_hours_of_orders(fake):
    serve_panel(fake)
    accounts.panel_data()
    by_path = {call.path: call for call in fake.calls}
    assert by_path[TR + "/accounts"].method == "GET" and by_path[TR + "/accounts"].query == {"fields": "positions"}
    query = by_path[TR + "/orders"].query
    assert set(query) == {"fromEnteredTime", "toEnteredTime"}               # no status: it takes one value, Hokma filters
    assert query["toEnteredTime"] == "2026-11-20T15:30:00.000Z"
    assert query["fromEnteredTime"] == (NOW - dt.timedelta(days=59, hours=23)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert set(by_path) == {ACCOUNT_NUMBERS, TR + "/accounts", TR + "/orders"}
    assert not any(path.endswith(("/previewOrder",)) or call.method != "GET" for path, call in by_path.items())


def test_panel_data_keeps_exactly_the_15_open_states_of_the_status_enum(fake):
    base = load("orders.json")[0]
    statuses = models.enum_values("OrderStatus")
    assert len(statuses) == 21 and len(OPEN_STATES) == 15 and set(accounts.OPEN_ORDER_STATUSES) == OPEN_STATES
    orders = [{**base, "orderId": 2000 + i, "status": status, "childOrderStrategies": [], "replacingOrderCollection": []}
              for i, status in enumerate(statuses)]
    serve_panel(fake, orders_data=orders)
    panel = accounts.panel_data()
    kept = [o["status"] for o in panel["accounts"][0]["orders"]]
    assert set(kept) == OPEN_STATES and len(kept) == 15
    assert panel["accounts"][1]["orders"] == []
    assert not (set(statuses) - OPEN_STATES) & set(kept)


def test_panel_data_of_cash_and_margin_accounts_shows_each_kind_of_balances(fake):
    only_cash = [load("accounts.json")[1]]
    serve_panel(fake, accounts_data=only_cash, orders_data=[])
    panel = accounts.panel_data()
    assert len(panel["accounts"]) == 1 and panel["accounts"][0]["type"] == "CASH"
    assert set(panel["accounts"][0]["currentBalances"]) == {"totalCash", "cashAvailableForTrading"}
    assert panel["accounts"][0]["orders"] == []
    no_balances = [{"securitiesAccount": {"type": "MARGIN", "accountNumber": FAKE_ACCOUNT_NUMBER}}]
    fake.on("GET", TR + "/accounts", no_balances)
    accounts.reset()
    accounts.set_clock(Clock())
    panel = accounts.panel_data()
    assert panel["accounts"][0]["currentBalances"] == {"equity": None, "buyingPower": None, "availableFunds": None}
    assert panel["accounts"][0]["positions"] == []


def test_panel_data_is_cached_15_seconds(fake, clock):
    serve_panel(fake)
    first = accounts.panel_data()
    count = len(fake.calls)
    clock.now += 14.9
    assert accounts.panel_data() == first and len(fake.calls) == count
    first["accounts"][0]["type"] = "CHANGED"                                # what a caller does to its copy stays there
    assert accounts.panel_data()["accounts"][0]["type"] == "MARGIN"
    clock.now += 0.2
    accounts.panel_data()
    assert len(fake.calls) == count + 2                                     # /accounts and /orders again
    assert len(numbers_calls(fake)) == 1


def test_panel_data_does_not_cache_a_failure(fake, clock):
    fake.on("GET", TR + "/accounts", load("accounts.json"))
    fake.on("GET", TR + "/orders", (500, {"errors": []}))
    with pytest.raises(SchwabError):
        accounts.panel_data()
    serve_panel(fake)
    assert len(accounts.panel_data()["accounts"]) == 2


def test_panel_data_without_session_raises_what_the_door_raises(fake):
    fake.on("GET", ACCOUNT_NUMBERS, (401, {"errors": []}))
    with pytest.raises(SchwabError) as error:
        accounts.panel_data()
    assert error.value.code == EXPIRED

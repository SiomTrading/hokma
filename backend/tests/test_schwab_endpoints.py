"""endpoints.py (SPEC-015 5.10, 15): one test per read endpoint (the 19 of the table of 15): route, method GET, every
parameter the function sends with every value of its enums, the answer read COMPLETE (every field of the schema in the
object, nothing in 'extra', the oneOf resolved) and the local validations. A fake Schwab answers; nothing leaves the machine."""
import datetime as dt
import itertools
import json

import pytest
from schwab_support import (FAKE_HASH, FAKE_HASH_2, REF_BALANCES, REF_FIELDS, FakeSchwab, assert_full, clear_fake,
                            extras, load, register_accounts)

from hokma.schwab import accounts, endpoints, http, models
from hokma.schwab.http import INVALID_PARAM, NOT_FOUND, SchwabError

MD, TR = "/marketdata/v1", "/trader/v1"
NOW = dt.datetime(2026, 11, 20, 15, 30, tzinfo=dt.timezone.utc)
ACCOUNT_NUMBERS = TR + "/accounts/accountNumbers"


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.fixture(scope="module")
def server():
    with FakeSchwab() as running:
        yield running


@pytest.fixture
def fake(server):
    clear_fake(server)
    accounts.reset()
    endpoints.set_clock(lambda: NOW)
    http.reset_limits(limit=100000)                         # the sweeps make hundreds of calls
    register_accounts(server)
    yield server
    endpoints.set_clock()
    accounts.reset()


def last(server):
    return server.calls[-1]


def sent(server, path):
    return [call for call in server.calls if call.path == path]


def refuses(fn, *args, **kwargs):
    with pytest.raises(SchwabError) as error:
        fn(*args, **kwargs)
    assert error.value.code == INVALID_PARAM and error.value.status == 422 and error.value.detail
    return error.value


def nothing_sent(server, *, allow=(ACCOUNT_NUMBERS,)):
    assert [call.path for call in server.calls if call.path not in allow] == []


# ============================================================================================ 1. GET /quotes

QUOTE_NODES = {
    "AAPL": ("EquityResponse", "QuoteEquity", "ReferenceEquity"),
    "$SPX": ("IndexResponse", "QuoteIndex", "ReferenceIndex"),
    "AMZN  230317C01360000": ("OptionResponse", "QuoteOption", "ReferenceOption"),
    "/ESH23": ("FutureResponse", "QuoteFuture", "ReferenceFuture"),
    "./ADUF23C0.55": ("FutureOptionResponse", "QuoteFutureOption", "ReferenceFutureOption"),
    "AUD/CAD": ("ForexResponse", "QuoteForex", "ReferenceForex"),
    "AAAHX": ("MutualFundResponse", "QuoteMutualFund", "ReferenceMutualFund"),
}
RESPONSE_FIELDS = "assetMainType assetSubType ssid symbol realtime quoteType"


def test_endpoint_1_quotes_route_parameters_and_complete_read(fake):
    data = load("quotes.json")
    fake.on("GET", MD + "/quotes", data)
    symbols = list(QUOTE_NODES)
    result = endpoints.quotes(symbols, fields=["quote", "reference"], indicative=True)
    call = last(fake)
    assert (call.method, call.path) == ("GET", MD + "/quotes")
    assert call.query == {"symbols": ",".join(symbols), "fields": "quote,reference", "indicative": "true"}
    assert call.headers["authorization"].startswith("Bearer ")
    # the answer is read complete
    assert isinstance(result, models.QuoteResponse) and list(result) == symbols
    assert extras(result) == [] and models.dump(result) == data
    for symbol, (response, quote, reference) in QUOTE_NODES.items():
        item = result[symbol]
        assert type(item).__name__ == response
        assert_full(item, RESPONSE_FIELDS)
        assert type(item.quote).__name__ == quote and type(item.reference).__name__ == reference
        assert_full(item.quote, REF_FIELDS[quote])
        assert_full(item.reference, REF_FIELDS[reference])
    equity = result["AAPL"]
    assert_full(equity.extended, REF_FIELDS["ExtendedMarket"])
    assert_full(equity.regular, REF_FIELDS["RegularMarket"])
    assert_full(equity.fundamental, REF_FIELDS["Fundamental"])
    assert_full(result["AAAHX"].fundamental, REF_FIELDS["Fundamental"])
    assert equity.quote.week52High is not None and equity.quote.week52Low is not None
    assert isinstance(result["AMZN  230317C01360000"].quote.openInterest, float)
    assert isinstance(result["/ESH23"].quote.openInterest, int)
    assert type(result.errors) is models.QuoteError
    assert result.errors.invalidSymbols == ["ZZZZ"] and result.errors.invalidCusips == ["000000000"]
    assert result.errors.invalidSSIDs == [1122334455]


@pytest.mark.parametrize("value", [*models.enum_values("QuoteField"), "all"])
def test_endpoint_1_quotes_sends_each_value_of_fields(fake, value):
    fake.on("GET", MD + "/quotes", {})
    endpoints.quotes("AAPL,MSFT", fields=value)
    assert last(fake).query == {"symbols": "AAPL,MSFT", "fields": value}


def test_endpoint_1_quotes_sends_only_what_it_is_given_and_all_five_nodes(fake):
    fake.on("GET", MD + "/quotes", {})
    endpoints.quotes([" AAPL ", "MSFT"], fields="quote,fundamental,extended,reference,regular", indicative=False)
    assert last(fake).query == {"symbols": "AAPL,MSFT", "fields": "quote,fundamental,extended,reference,regular",
                                "indicative": "false"}
    endpoints.quotes("SPY")
    assert last(fake).query == {"symbols": "SPY"}


def test_endpoint_1_quotes_encodes_the_dollar_and_the_spaces_in_the_url(fake, monkeypatch):
    urls = []

    def spy(method, url, headers, body):
        urls.append(url)
        return 200, "{}", None

    monkeypatch.setattr(http, "_send", spy)
    endpoints.quotes(["AAPL", "$SPX", "AMZN  230317C01360000"])
    assert "symbols=AAPL,%24SPX,AMZN%20%20230317C01360000" in urls[0]
    assert "$" not in urls[0] and " " not in urls[0] and "+" not in urls[0]


def test_endpoint_1_quotes_validations(fake):
    for bad in ("", [], [""], ["AAPL", " "], None, 5, ["AAPL\n"]):
        refuses(endpoints.quotes, bad)
    refuses(endpoints.quotes, [f"S{i}" for i in range(501)])
    refuses(endpoints.quotes, "AAPL", fields="quote,bogus")
    refuses(endpoints.quotes, "AAPL", fields=["price"])
    refuses(endpoints.quotes, "AAPL", fields="")
    refuses(endpoints.quotes, "AAPL", indicative="true")
    refuses(endpoints.quotes, "AAPL", indicative=1)
    nothing_sent(fake)
    fake.on("GET", MD + "/quotes", {})
    endpoints.quotes([f"S{i}" for i in range(500)])                     # REF A.7: 500 is the limit
    assert len(last(fake).query["symbols"].split(",")) == 500


# ================================================================================= 2. GET /{symbol_id}/quotes


def test_endpoint_2_quote_route_fields_and_complete_read(fake):
    flat = load("quote_single.json")
    fake.on("GET", MD + "/TSLA/quotes", flat)
    result = endpoints.quote("TSLA", fields="quote,reference")
    call = last(fake)
    assert (call.method, call.path, call.query) == ("GET", MD + "/TSLA/quotes", {"fields": "quote,reference"})
    assert type(result) is models.EquityResponse and extras(result) == [] and models.dump(result) == flat
    assert_full(result, RESPONSE_FIELDS + " quote reference extended fundamental regular")
    assert_full(result.quote, REF_FIELDS["QuoteEquity"])
    for value in [*models.enum_values("QuoteField"), "all"]:
        endpoints.quote("TSLA", fields=value)
        assert last(fake).query == {"fields": value}
    endpoints.quote("TSLA")
    assert last(fake).query == {}


def test_endpoint_2_quote_reads_the_map_form_too(fake):
    fake.on("GET", MD + "/AAPL/quotes", {"AAPL": load("quote_single.json")})
    result = endpoints.quote("AAPL")
    assert type(result) is models.EquityResponse and result.symbol is not None and extras(result) == []
    fake.on("GET", MD + "/ZZZ/quotes", {"errors": {"invalidSymbols": ["ZZZ"]}})
    assert endpoints.quote("ZZZ").errors.invalidSymbols == ["ZZZ"]


def test_endpoint_2_quote_encodes_the_symbol_for_the_gate(fake):
    fake.on("GET", MD + "/%24SPX/quotes", {})
    fake.on("GET", MD + "/BRK%5FB/quotes", {})
    fake.on("GET", MD + "/BRK.B/quotes", {})
    fake.on("GET", MD + "/%5EVIX/quotes", {})
    fake.on("GET", MD + "/AMZN%20%20230317C01360000/quotes", {})
    for symbol in ("$SPX", "BRK_B", "BRK.B", "^VIX", "AMZN  230317C01360000"):
        endpoints.quote(symbol)
        assert last(fake).path.endswith("/quotes") and "$" not in last(fake).path and " " not in last(fake).path
    assert fake.paths() == [MD + "/%24SPX/quotes", MD + "/BRK%5FB/quotes", MD + "/BRK.B/quotes", MD + "/%5EVIX/quotes",
                            MD + "/AMZN%20%20230317C01360000/quotes"]


def test_endpoint_2_quote_validations(fake):
    for bad in ("", "  ", "A,B", "/ES", "../x", "..", ".", "$", None, 5, "A" * 41, "AAPL\n", "A" * 33):
        refuses(endpoints.quote, bad)
    refuses(endpoints.quote, "AAPL", fields="nope")
    nothing_sent(fake)


# ================================================================================================ 3. GET /chains

CHAIN_FIELDS = REF_FIELDS["OptionChain"]


def test_endpoint_3_option_chain_route_every_parameter_and_complete_read(fake):
    data = load("chains.json")
    fake.on("GET", MD + "/chains", data)
    result = endpoints.option_chain(
        "AAPL", contractType="ALL", strikeCount=10, includeUnderlyingQuote=True, strategy="ANALYTICAL", interval=5.0,
        strike=100.5, range="NTM", fromDate="2026-12-01", toDate=dt.date(2026, 12, 31), volatility=30, underlyingPrice=200,
        interestRate=4.5, daysToExpiration=30, expMonth="DEC", optionType="S", entitlement="NP")
    call = last(fake)
    assert (call.method, call.path) == ("GET", MD + "/chains")
    assert call.query == {
        "symbol": "AAPL", "contractType": "ALL", "strikeCount": "10", "includeUnderlyingQuote": "true",
        "strategy": "ANALYTICAL", "interval": "5.0", "strike": "100.5", "range": "NTM", "fromDate": "2026-12-01",
        "toDate": "2026-12-31", "volatility": "30", "underlyingPrice": "200", "interestRate": "4.5",
        "daysToExpiration": "30", "expMonth": "DEC", "optionType": "S", "entitlement": "NP"}
    assert type(result) is models.OptionChain and extras(result) == [] and models.dump(result) == data
    assert_full(result, CHAIN_FIELDS)
    assert_full(result.underlying, REF_FIELDS["Underlying"])
    assert list(result.callExpDateMap) == ["2026-12-18:28", "2027-01-15:56"] and list(result.putExpDateMap) == list(result.callExpDateMap)
    assert list(result.callExpDateMap["2026-12-18:28"]) == ["100.0", "105.0"]
    for side, putcall in ((result.callExpDateMap, "CALL"), (result.putExpDateMap, "PUT")):
        for expiration in side.values():
            for strike, contracts in expiration.items():
                assert isinstance(contracts, list) and len(contracts) == 1
                contract = contracts[0]
                assert type(contract) is models.OptionContract and contract.putCall == putcall
                assert float(strike) == contract.strikePrice
                assert_full(contract, REF_FIELDS["OptionContract"])
                assert_full(contract.optionDeliverablesList[0], REF_FIELDS["OptionDeliverables"])
    contract = result.callExpDateMap["2026-12-18:28"]["100.0"][0]
    assert contract.quoteTimeInLong > 2**31 and isinstance(contract.openInterest, float)   # int32 in the REF, read as 64 bits


@pytest.mark.parametrize("enum, name", [("ChainContractType", "contractType"), ("Strategy", "strategy"),
                                        ("ExpMonth", "expMonth"), ("Entitlement", "entitlement"), ("Range", "range")])
def test_endpoint_3_option_chain_sends_each_value_of_each_enum(fake, enum, name):
    fake.on("GET", MD + "/chains", {})
    for value in models.enum_values(enum):
        endpoints.option_chain("SPY", **{name: value})
        assert last(fake).query == {"symbol": "SPY", name: value}


def test_endpoint_3_option_chain_sends_nothing_that_was_not_given(fake):
    fake.on("GET", MD + "/chains", {})
    endpoints.option_chain("SPY")
    assert last(fake).query == {"symbol": "SPY"}
    endpoints.option_chain("SPY", includeUnderlyingQuote=False, strike=0, strikeCount=1)
    assert last(fake).query == {"symbol": "SPY", "includeUnderlyingQuote": "false", "strike": "0", "strikeCount": "1"}


def test_endpoint_3_option_chain_reads_the_chain_of_the_ref_example_shape(fake):
    fake.on("GET", MD + "/chains", {"symbol": "X", "callExpDateMap": {"2026-12-18:28": {"100.0": [{"putCall": "CALL"}]}},
                                    "putExpDateMap": {}, "newThing": 1})
    result = endpoints.option_chain("X")
    assert result.extra == {"newThing": 1} and result.putExpDateMap == {}
    assert result.callExpDateMap["2026-12-18:28"]["100.0"][0].putCall == "CALL"


def test_endpoint_3_option_chain_validations(fake):
    for bad in ("", "A,B", None, "  "):
        refuses(endpoints.option_chain, bad)
    refuses(endpoints.option_chain, "SPY", contractType="BOTH")
    refuses(endpoints.option_chain, "SPY", contractType="call")
    refuses(endpoints.option_chain, "SPY", strategy="IRON")
    refuses(endpoints.option_chain, "SPY", expMonth="DECEMBER")
    refuses(endpoints.option_chain, "SPY", entitlement="XX")
    refuses(endpoints.option_chain, "SPY", range="it m")
    refuses(endpoints.option_chain, "SPY", optionType="not valid!")
    refuses(endpoints.option_chain, "SPY", strikeCount=0)
    refuses(endpoints.option_chain, "SPY", strikeCount="ten")
    refuses(endpoints.option_chain, "SPY", strikeCount=True)
    refuses(endpoints.option_chain, "SPY", includeUnderlyingQuote="yes")
    refuses(endpoints.option_chain, "SPY", interval=-1)
    refuses(endpoints.option_chain, "SPY", strike=float("nan"))
    refuses(endpoints.option_chain, "SPY", fromDate="2026-12-1")
    refuses(endpoints.option_chain, "SPY", fromDate="2026-02-30")
    refuses(endpoints.option_chain, "SPY", toDate=dt.datetime(2026, 12, 1))
    refuses(endpoints.option_chain, "SPY", fromDate="2026-12-31", toDate="2026-12-01")
    nothing_sent(fake)


@pytest.mark.parametrize("name, value", [("volatility", 30), ("underlyingPrice", 200), ("interestRate", 4.5),
                                         ("daysToExpiration", 30)])
def test_endpoint_3_option_chain_analytical_parameters_need_the_analytical_strategy(fake, name, value):
    refuses(endpoints.option_chain, "SPY", **{name: value})
    refuses(endpoints.option_chain, "SPY", strategy="SINGLE", **{name: value})
    nothing_sent(fake)
    fake.on("GET", MD + "/chains", {})
    endpoints.option_chain("SPY", strategy="ANALYTICAL", **{name: value})
    assert last(fake).query[name] == str(value)


# ====================================================================================== 4. GET /expirationchain


def test_endpoint_4_expirations_route_parameter_and_complete_read(fake):
    data = load("expirationchain.json")
    fake.on("GET", MD + "/expirationchain", data)
    result = endpoints.expirations("AAPL")
    call = last(fake)
    assert (call.method, call.path, call.query) == ("GET", MD + "/expirationchain", {"symbol": "AAPL"})
    assert type(result) is models.ExpirationChain and result.status == "SUCCESS"
    assert extras(result) == [] and models.dump(result) == data
    example, schema, last_one = result.expirationList
    assert (example.expirationDate, example.daysToExpiration, example.expirationType, example.standard) == ("2026-11-27", 7, "W", True)
    assert_full(schema, REF_FIELDS["Expiration"])                       # expiration, settlementType, optionRoots...
    assert schema.expirationDate == "2026-12-18" and last_one.expirationType == "Q" and last_one.settlementType == "A"
    refuses(endpoints.expirations, "")
    refuses(endpoints.expirations, "A,B")
    assert sent(fake, MD + "/expirationchain") == [call]


# ======================================================================================== 5. GET /pricehistory

HISTORY_TABLE = {                                                      # REF 2.5, copied
    "day": ((1, 2, 3, 4, 5, 10), {"minute": (1, 5, 10, 15, 30)}),
    "month": ((1, 2, 3, 6), {"daily": (1,), "weekly": (1,)}),
    "year": ((1, 2, 3, 5, 10, 15, 20), {"daily": (1,), "weekly": (1,), "monthly": (1,)}),
    "ytd": ((1,), {"daily": (1,), "weekly": (1,)}),
}


def test_endpoint_5_price_history_route_every_parameter_and_complete_read(fake):
    data = load("pricehistory.json")
    fake.on("GET", MD + "/pricehistory", data)
    result = endpoints.price_history("AAPL", periodType="day", period=5, frequencyType="minute", frequency=5,
                                     startDate=1735689600000, endDate=1735776000000, needExtendedHoursData=True,
                                     needPreviousClose=False)
    call = last(fake)
    assert (call.method, call.path) == ("GET", MD + "/pricehistory")
    assert call.query == {"symbol": "AAPL", "periodType": "day", "period": "5", "frequencyType": "minute", "frequency": "5",
                          "startDate": "1735689600000", "endDate": "1735776000000", "needExtendedHoursData": "true",
                          "needPreviousClose": "false"}
    assert type(result) is models.CandleList and extras(result) == [] and models.dump(result) == data
    assert_full(result, REF_FIELDS["CandleList"])
    assert len(result.candles) == 2
    for candle in result.candles:
        assert_full(candle, REF_FIELDS["Candle"])
        assert isinstance(candle.datetime, int) and isinstance(candle.volume, int)


def test_endpoint_5_price_history_accepts_every_valid_combination_of_the_ref_table(fake):
    fake.on("GET", MD + "/pricehistory", {"candles": [], "empty": True})
    count = 0
    for period_type, (periods, kinds) in HISTORY_TABLE.items():
        for period, (kind, steps) in itertools.product(periods, kinds.items()):
            for step in steps:
                endpoints.price_history("SPY", periodType=period_type, period=period, frequencyType=kind, frequency=step)
                assert last(fake).query == {"symbol": "SPY", "periodType": period_type, "period": str(period),
                                            "frequencyType": kind, "frequency": str(step)}
                count += 1
    assert count == 61
    for period_type in HISTORY_TABLE:                                  # the defaults of the table need no other parameter
        endpoints.price_history("SPY", periodType=period_type)
        assert last(fake).query == {"symbol": "SPY", "periodType": period_type}
    endpoints.price_history("SPY")
    assert last(fake).query == {"symbol": "SPY"}
    endpoints.price_history("SPY", periodType="day", frequency=15)
    assert last(fake).query == {"symbol": "SPY", "periodType": "day", "frequencyType": "minute", "frequency": "15"}


def test_endpoint_5_price_history_sends_the_default_frequency_type_it_works_out_when_only_the_frequency_is_given(fake):
    fake.on("GET", MD + "/pricehistory", {"candles": [], "empty": True})
    for period_type, default_kind in (("day", "minute"), ("month", "weekly"), ("year", "monthly"), ("ytd", "weekly")):
        endpoints.price_history("SPY", periodType=period_type, frequency=1)
        assert last(fake).query == {"symbol": "SPY", "periodType": period_type, "frequencyType": default_kind,
                                    "frequency": "1"}, period_type
    endpoints.price_history("SPY", periodType="year", frequencyType="daily", frequency=1)       # an explicit one wins
    assert last(fake).query["frequencyType"] == "daily"
    endpoints.price_history("SPY", periodType="year")                                          # no frequency: none sent
    assert "frequencyType" not in last(fake).query


def test_endpoint_5_price_history_refuses_every_invalid_combination(fake):
    count = 0
    for period_type, (periods, kinds) in HISTORY_TABLE.items():
        for period in (0, 7, 25, 12):
            if period not in periods:
                refuses(endpoints.price_history, "SPY", periodType=period_type, period=period)
                count += 1
        for kind in models.enum_values("FrequencyType"):
            if kind not in kinds:
                refuses(endpoints.price_history, "SPY", periodType=period_type, frequencyType=kind)
                count += 1
        for kind, steps in kinds.items():
            for step in (2, 7, 60):
                if step not in steps:
                    refuses(endpoints.price_history, "SPY", periodType=period_type, frequencyType=kind, frequency=step)
                    count += 1
        refuses(endpoints.price_history, "SPY", periodType=period_type, frequency=99)
    assert count > 40
    refuses(endpoints.price_history, "SPY", periodType="week")
    refuses(endpoints.price_history, "SPY", frequencyType="hourly")
    refuses(endpoints.price_history, "SPY", period=5)                  # a period needs its periodType
    refuses(endpoints.price_history, "SPY", frequency=5)               # a frequency needs its frequencyType
    refuses(endpoints.price_history, "SPY", frequencyType="minute", frequency=45)
    refuses(endpoints.price_history, "SPY", periodType="day", period=True)
    refuses(endpoints.price_history, "SPY", needExtendedHoursData="true")
    refuses(endpoints.price_history, "SPY", needPreviousClose=1)
    refuses(endpoints.price_history, "")
    nothing_sent(fake)


def test_endpoint_5_price_history_with_dates_takes_a_frequency_without_period_type(fake):
    fake.on("GET", MD + "/pricehistory", {"candles": []})                # the example of REF 2.5
    endpoints.price_history("SPY", frequencyType="minute", frequency=1, startDate=1735689600000, endDate=1735776000000)
    assert last(fake).query == {"symbol": "SPY", "frequencyType": "minute", "frequency": "1", "startDate": "1735689600000",
                                "endDate": "1735776000000"}
    endpoints.price_history("SPY", startDate=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc), endDate="1735776000000")
    assert last(fake).query["startDate"] == "1735689600000" and last(fake).query["endDate"] == "1735776000000"
    refuses(endpoints.price_history, "SPY", startDate=5, endDate=4)
    refuses(endpoints.price_history, "SPY", startDate=-1)
    refuses(endpoints.price_history, "SPY", startDate="yesterday")
    refuses(endpoints.price_history, "SPY", endDate=1.5)


# ======================================================================================== 6. GET /movers/{symbol_id}

MOVER_PATHS = ["%24DJI", "%24COMPX", "%24SPX", "NYSE", "NASDAQ", "OTCBB", "INDEX_ALL", "EQUITY_ALL", "OPTION_ALL",
               "OPTION_PUT", "OPTION_CALL"]


def test_endpoint_6_movers_route_parameters_and_complete_read(fake):
    data = load("movers.json")
    for path in MOVER_PATHS:
        fake.on("GET", f"{MD}/movers/{path}", data)
    indexes = ["$DJI", "$COMPX", "$SPX", "NYSE", "NASDAQ", "OTCBB", "INDEX_ALL", "EQUITY_ALL", "OPTION_ALL", "OPTION_PUT",
               "OPTION_CALL"]
    assert tuple(indexes) == models.enum_values("MoversIndex")
    for index, path in zip(indexes, MOVER_PATHS):
        result = endpoints.movers(index)
        call = last(fake)
        assert (call.method, call.path, call.query) == ("GET", f"{MD}/movers/{path}", {})
    result = endpoints.movers("%24SPX", sort="PERCENT_CHANGE_UP", frequency=5)         # an already encoded index is accepted
    assert last(fake).path == MD + "/movers/%24SPX" and last(fake).query == {"sort": "PERCENT_CHANGE_UP", "frequency": "5"}
    assert type(result) is models.ScreenerList and extras(result) == [] and models.dump(result) == data
    up, down = result.screeners
    assert_full(up, "change description direction last symbol totalVolume") and assert_full(down, "change description direction last symbol totalVolume")
    assert (up.direction, down.direction) == ("up", "down")


def test_endpoint_6_movers_sends_each_value_of_sort_and_frequency(fake):
    fake.on("GET", MD + "/movers/%24DJI", {})
    for sort in ("VOLUME", "TRADES", "PERCENT_CHANGE_UP", "PERCENT_CHANGE_DOWN"):
        endpoints.movers("$DJI", sort=sort)
        assert last(fake).query == {"sort": sort}
    for frequency in (0, 1, 5, 10, 30, 60):
        endpoints.movers("$DJI", frequency=frequency)
        assert last(fake).query == {"frequency": str(frequency)}


def test_endpoint_6_movers_validations(fake):
    for bad in ("SPX", "$DJI/../x", "", None, "$dji", "NYSE,NASDAQ", 5):
        refuses(endpoints.movers, bad)
    refuses(endpoints.movers, "$DJI", sort="PRICE")
    refuses(endpoints.movers, "$DJI", sort="volume")
    refuses(endpoints.movers, "$DJI", frequency=2)
    refuses(endpoints.movers, "$DJI", frequency="5")
    refuses(endpoints.movers, "$DJI", frequency=True)
    nothing_sent(fake)


# ================================================================================================ 7. GET /markets


def test_endpoint_7_markets_route_parameters_and_complete_read(fake):
    data = load("markets.json")
    fake.on("GET", MD + "/markets", data)
    result = endpoints.market_hours(["equity", "option"], date="2026-12-24")
    call = last(fake)
    assert (call.method, call.path, call.query) == ("GET", MD + "/markets", {"markets": "equity,option", "date": "2026-12-24"})
    assert list(result) == ["equity", "option"] and list(result["option"]) == ["EQO", "IND"]
    assert extras(result) == [] and models.dump(result) == data
    for market in result.values():
        for hours in market.values():
            assert type(hours) is models.Hours
            assert_full(hours, "date marketType product productName isOpen sessionHours")
            assert type(hours.sessionHours) is models.SessionHours
            assert all(type(i) is models.Interval and i.start and i.end for i in hours.sessionHours.regularMarket)
    equity = result["equity"]["EQ"].sessionHours
    assert_full(equity, "preMarket regularMarket postMarket")
    assert result["option"]["IND"].sessionHours.regularMarket[0].end == "2026-11-20T16:15:00-05:00"
    assert result["option"]["EQO"].sessionHours.preMarket is None


def test_endpoint_7_markets_sends_each_market_and_every_list(fake):
    fake.on("GET", MD + "/markets", {})
    for market in models.enum_values("Market"):
        endpoints.market_hours([market])                               # a list always uses /markets
        assert (last(fake).path, last(fake).query) == (MD + "/markets", {"markets": market})
    endpoints.market_hours(["equity", "option", "bond", "future", "forex"])
    assert last(fake).query == {"markets": "equity,option,bond,future,forex"}
    endpoints.market_hours("equity,option", date=dt.date(2026, 11, 20))
    assert last(fake).path == MD + "/markets" and last(fake).query == {"markets": "equity,option", "date": "2026-11-20"}


def test_endpoint_7_markets_validations(fake):
    for bad in (None, [], "", ["stocks"], "equity,stocks", [5]):
        refuses(endpoints.market_hours, bad)                           # 'markets' is required
    refuses(endpoints.market_hours, ["equity"], date="2026-13-01")
    refuses(endpoints.market_hours, ["equity"], date="20/11/2026")
    refuses(endpoints.market_hours, ["equity"], date="2026-11-10")     # in the past
    refuses(endpoints.market_hours, ["equity"], date="2028-01-01")     # more than a year ahead
    nothing_sent(fake)
    fake.on("GET", MD + "/markets", {})
    for day in ("2026-11-20", "2026-11-19", "2027-11-20", "2026-12-24"):
        endpoints.market_hours(["equity"], date=day)
        assert last(fake).query["date"] == day


# ========================================================================================= 8. GET /markets/{market_id}


def test_endpoint_8_market_route_each_market_and_complete_read(fake):
    data = load("market_equity.json")
    for market in models.enum_values("Market"):
        fake.on("GET", f"{MD}/markets/{market}", data)
        result = endpoints.market_hours(market, date="2026-12-24")
        call = last(fake)
        assert (call.method, call.path, call.query) == ("GET", f"{MD}/markets/{market}", {"date": "2026-12-24"})
    assert extras(result) == [] and models.dump(result) == data
    hours = result["equity"]["EQ"]
    assert_full(hours, "date marketType exchange category product productName isOpen sessionHours")
    assert hours.category == "NULL"
    endpoints.market_hours("equity")
    assert last(fake).query == {}


def test_endpoint_8_market_reads_a_product_map_without_the_market_level(fake):
    fake.on("GET", MD + "/markets/equity", {"EQ": {"date": "2026-11-20", "marketType": "EQUITY", "isOpen": False}})
    result = endpoints.market_hours("equity")
    assert result["equity"]["EQ"].isOpen is False and extras(result) == []


def test_endpoint_8_market_validations(fake):
    for bad in ("stocks", "equity/../x", "", "Equity"):
        refuses(endpoints.market_hours, bad)
    refuses(endpoints.market_hours, "equity", date="tomorrow")
    nothing_sent(fake)


# ========================================================================================== 9. GET /instruments


def test_endpoint_9_instruments_route_parameters_and_complete_read(fake):
    data = load("instruments.json")
    fake.on("GET", MD + "/instruments", data)
    result = endpoints.instruments(["AAPL", "BAC"], projection="symbol-search")
    call = last(fake)
    assert (call.method, call.path, call.query) == ("GET", MD + "/instruments", {"symbol": "AAPL,BAC", "projection": "symbol-search"})
    assert type(result) is models.InstrumentList and extras(result) == [] and models.dump(result) == data
    assert [(i.symbol, i.assetType) for i in result.instruments] == [("AAPL", "EQUITY"), ("BAC", "EQUITY")]
    assert_full(result.instruments[0], "cusip symbol description exchange assetType")


def test_endpoint_9_instruments_sends_each_projection(fake):
    fake.on("GET", MD + "/instruments", {"instruments": []})
    for projection in ("symbol-search", "symbol-regex", "desc-search", "desc-regex", "search", "fundamental"):
        endpoints.instruments("AAPL", projection=projection)
        assert last(fake).query == {"symbol": "AAPL", "projection": projection}
    endpoints.instruments("Apple.{1,3}", projection="desc-regex")        # a regex keeps its comma
    assert last(fake).query["symbol"] == "Apple.{1,3}"
    endpoints.instruments("AAPL, BAC", projection="search")
    assert last(fake).query["symbol"] == "AAPL,BAC"


def test_endpoint_9_instruments_fundamental_is_read_complete(fake):
    data = load("instruments_fundamental.json")
    fake.on("GET", MD + "/instruments", data)
    result = endpoints.instruments("AAPL", projection="fundamental")
    instrument = result.instruments[0]
    assert extras(result) == [] and models.dump(result) == data
    assert type(instrument.fundamental) is models.FundamentalInst
    assert set(data["instruments"][0]["fundamental"]) == set(models.fields_of("FundamentalInst")) and len(models.fields_of("FundamentalInst")) == 58
    assert_full(instrument.fundamental, " ".join(models.fields_of("FundamentalInst")))
    assert type(instrument.instrumentInfo) is models.Instrument and type(instrument.bondInstrumentInfo) is models.Bond
    assert_full(instrument, "cusip symbol description exchange assetType bondFactor bondMultiplier bondPrice fundamental "
                            "instrumentInfo bondInstrumentInfo type")
    assert instrument.fundamental.dividendFreq == 4


def test_endpoint_9_instruments_validations(fake):
    refuses(endpoints.instruments, "AAPL")                              # projection is required
    refuses(endpoints.instruments, "AAPL", projection=None)
    refuses(endpoints.instruments, "AAPL", projection="regex")
    refuses(endpoints.instruments, "", projection="search")
    refuses(endpoints.instruments, [], projection="search")
    refuses(endpoints.instruments, "", projection="desc-regex")
    refuses(endpoints.instruments, "AAPL\n", projection="search")
    nothing_sent(fake)


# ================================================================================= 10. GET /instruments/{cusip_id}


def test_endpoint_10_instrument_by_cusip_route_and_complete_read(fake):
    data = load("instrument_cusip.json")
    fake.on("GET", MD + "/instruments/037833100", data)
    result = endpoints.instrument_by_cusip("037833100")
    call = last(fake)
    assert (call.method, call.path, call.query) == ("GET", MD + "/instruments/037833100", {})
    assert type(result) is models.InstrumentResponse and extras(result) == [] and models.dump(result) == data
    assert_full(result, "cusip symbol description exchange assetType")
    fake.on("GET", MD + "/instruments/060505104", {"instruments": [load("instruments.json")["instruments"][1]]})
    assert endpoints.instrument_by_cusip("060505104").symbol == "BAC"
    fake.on("GET", MD + "/instruments/000000000", {"instruments": []})
    assert endpoints.instrument_by_cusip("000000000") is None


def test_endpoint_10_instrument_by_cusip_validations_and_errors(fake):
    for bad in ("12345678", "0378331000", "03783310!", "037 33100", "", None, 37833100, "../x/1234"):
        refuses(endpoints.instrument_by_cusip, bad)
    nothing_sent(fake)
    with pytest.raises(SchwabError) as error:
        endpoints.instrument_by_cusip("999999999")                     # the fake answers 404
    assert error.value.code == NOT_FOUND


# ======================================================================= 11. GET /accounts/accountNumbers


def test_endpoint_11_account_numbers_route_and_complete_read(fake):
    result = endpoints.account_numbers()
    call = last(fake)
    assert (call.method, call.path, call.query) == ("GET", ACCOUNT_NUMBERS, {})
    assert [type(p) for p in result] == [models.AccountNumberHash] * 2 and extras(result) == []
    assert_full(result[0], "accountNumber hashValue")
    assert [(p.accountNumber, p.hashValue) for p in result] == [("12345678", FAKE_HASH), ("87654321", FAKE_HASH_2)]
    assert models.dump(result) == load("account_numbers.json")


# ============================================================================== 12. GET /accounts


def test_endpoint_12_accounts_route_fields_and_complete_read(fake):
    data = load("accounts.json")
    fake.on("GET", TR + "/accounts", data)
    result = endpoints.accounts(fields="positions")
    call = last(fake)
    assert (call.method, call.path, call.query) == ("GET", TR + "/accounts", {"fields": "positions"})
    assert [type(a) for a in result] == [models.Account, models.Account] and extras(result) == []
    assert models.dump(result) == data
    margin, cash = (a.securitiesAccount for a in result)
    assert type(margin) is models.MarginAccount and type(cash) is models.CashAccount
    assert (margin.type, cash.type) == ("MARGIN", "CASH")
    for account in (margin, cash):
        assert_full(account, "type accountNumber roundTrips isDayTrader isClosingOnlyRestricted pfcbFlag positions "
                             "initialBalances currentBalances projectedBalances")
    for block, schema in ((margin.initialBalances, "MarginInitialBalance"), (margin.currentBalances, "MarginBalance"),
                          (margin.projectedBalances, "MarginBalance"), (cash.initialBalances, "CashInitialBalance"),
                          (cash.currentBalances, "CashBalance"), (cash.projectedBalances, "CashBalance")):
        assert type(block).__name__ == schema
        assert_full(block, REF_BALANCES[schema])
    # the positions and their oneOf instruments
    assert [type(p.instrument).__name__ for p in margin.positions] == [
        "AccountEquity", "AccountOption", "AccountFixedIncome", "AccountMutualFund", "AccountCashEquivalent", "AccountsBaseInstrument"]
    for position in margin.positions:
        assert_full(position, REF_FIELDS["Position"])
    option = margin.positions[1].instrument
    assert_full(option, "assetType cusip symbol description instrumentId netChange optionDeliverables putCall optionMultiplier type underlyingSymbol")
    assert_full(option.optionDeliverables[0], "symbol deliverableUnits apiCurrencyType assetType")
    assert_full(margin.positions[2].instrument, "maturityDate factor variableRate")
    assert margin.positions[4].instrument.type == "MONEY_MARKET_FUND"
    assert len(cash.positions) == 1


def test_endpoint_12_accounts_without_fields_sends_no_query(fake):
    fake.on("GET", TR + "/accounts", load("accounts_no_positions.json"))
    result = endpoints.accounts()
    assert last(fake).query == {} and all(a.securitiesAccount.positions is None for a in result)
    assert extras(result) == []
    endpoints.accounts(fields=["positions"])
    assert last(fake).query == {"fields": "positions"}
    refuses(endpoints.accounts, fields="orders")
    refuses(endpoints.accounts, fields="positions,orders")
    refuses(endpoints.accounts, fields=5)


# ============================================================================== 13. GET /accounts/{accountNumber}


def test_endpoint_13_account_route_translates_the_account_ref_and_reads_complete(fake):
    margin, cash = load("account_margin.json"), load("account_cash.json")
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}", margin)
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH_2}", cash)
    one = endpoints.accounts("A1", fields="positions")
    call = last(fake)
    assert (call.method, call.path, call.query) == ("GET", f"{TR}/accounts/{FAKE_HASH}", {"fields": "positions"})
    assert type(one) is models.Account and type(one.securitiesAccount) is models.MarginAccount
    assert extras(one) == [] and models.dump(one) == margin
    two = endpoints.accounts("A2")
    assert (last(fake).path, last(fake).query) == (f"{TR}/accounts/{FAKE_HASH_2}", {})
    assert type(two.securitiesAccount) is models.CashAccount and extras(two) == []
    assert_full(two.securitiesAccount.currentBalances, REF_BALANCES["CashBalance"])
    assert len(sent(fake, ACCOUNT_NUMBERS)) == 1                       # the table is built once


def test_endpoint_13_account_takes_an_account_ref_never_a_hash_or_a_number(fake):
    for bad in (FAKE_HASH, "12345678", "A9", "a1", "", 1, "A1/orders"):
        error = refuses(endpoints.accounts, bad)
        assert FAKE_HASH not in str(error.detail) and "12345678" not in str(error.detail)
        refuses(endpoints.orders, bad)
        refuses(endpoints.order, bad, 1)
        refuses(endpoints.transactions, bad, types="TRADE")
        refuses(endpoints.transaction, bad, 1)
    refuses(endpoints.order, None, 1)                                   # None means 'all accounts' only where that exists
    refuses(endpoints.transactions, None, types="TRADE")
    refuses(endpoints.transaction, None, 1)
    nothing_sent(fake)


# ====================================================================== 14. GET /accounts/{accountNumber}/orders

ORDER_FIELDS = REF_FIELDS["Order"]


def test_endpoint_14_account_orders_route_parameters_and_complete_read(fake):
    data = load("orders.json")
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}/orders", data)
    result = endpoints.orders("A1", fromEnteredTime="2026-03-29T00:00:00.000Z", toEnteredTime="2026-04-28T23:59:59.000Z",
                              status="FILLED", maxResults=500)
    call = last(fake)
    assert (call.method, call.path) == ("GET", f"{TR}/accounts/{FAKE_HASH}/orders")
    assert call.query == {"maxResults": "500", "fromEnteredTime": "2026-03-29T00:00:00.000Z",
                          "toEnteredTime": "2026-04-28T23:59:59.000Z", "status": "FILLED"}
    assert [type(o) for o in result] == [models.Order] * 4 and extras(result) == [] and models.dump(result) == data
    working = result[0]
    assert_full(working, ORDER_FIELDS)
    assert_full(working.orderLegCollection[0], REF_FIELDS["OrderLegCollection"])
    assert type(working.orderLegCollection[0].instrument) is models.AccountEquity
    assert type(result[1].orderLegCollection[0].instrument) is models.AccountOption
    activity = working.orderActivityCollection[0]
    assert_full(activity, REF_FIELDS["OrderActivity"])
    assert len(activity.executionLegs) == 2 and all(type(x) is models.ExecutionLeg for x in activity.executionLegs)
    assert_full(activity.executionLegs[0], REF_FIELDS["ExecutionLeg"])
    child, replacing = working.childOrderStrategies[0], working.replacingOrderCollection[0]
    assert type(child) is models.Order and child.orderId == 1000000011 and child.orderStrategyType == "OCO"
    assert type(replacing) is models.Order and replacing.status == "REPLACED"
    assert isinstance(working.accountNumber, int) and working.accountNumber == 12345678   # int64 in Order


def test_endpoint_14_account_orders_sends_each_status_of_the_enum(fake):
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}/orders", [])
    values = models.enum_values("OrderStatus")
    assert len(values) == 21
    for status in values:
        assert endpoints.orders("A1", status=status) == []
        assert last(fake).query["status"] == status
    endpoints.orders("A1")
    assert "status" not in last(fake).query and "maxResults" not in last(fake).query


def test_endpoint_14_account_orders_window_is_at_most_one_year(fake):
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}/orders", [])
    endpoints.orders("A1")                                              # the widest window: 364 days 23 hours up to now
    assert last(fake).query["toEnteredTime"] == stamp(NOW)
    assert last(fake).query["fromEnteredTime"] == stamp(NOW - dt.timedelta(days=364, hours=23))
    endpoints.orders("A1", fromEnteredTime=NOW - dt.timedelta(days=365), toEnteredTime=NOW)       # exactly one year: valid
    assert last(fake).query["fromEnteredTime"] == stamp(NOW - dt.timedelta(days=365))
    endpoints.orders("A1", fromEnteredTime="2024-03-29T00:00:00.000Z", toEnteredTime="2025-03-29T00:00:00.000Z")   # old, but one year
    refuses(endpoints.orders, "A1", fromEnteredTime=NOW - dt.timedelta(days=366), toEnteredTime=NOW)
    refuses(endpoints.orders, "A1", fromEnteredTime="2024-01-01T00:00:00.000Z", toEnteredTime="2026-01-01T00:00:00.000Z")
    refuses(endpoints.orders, "A1", fromEnteredTime="2026-02-01T00:00:00.000Z", toEnteredTime="2026-01-01T00:00:00.000Z")


def test_endpoint_14_account_orders_instants_and_other_validations(fake):
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}/orders", [])
    endpoints.orders("A1", fromEnteredTime="2026-11-01T00:00:00-05:00", toEnteredTime="2026-11-02T00:00:00.5+0100")
    assert last(fake).query["fromEnteredTime"] == "2026-11-01T05:00:00.000Z"
    assert last(fake).query["toEnteredTime"] == "2026-11-01T23:00:00.500Z"
    endpoints.orders("A1", fromEnteredTime=dt.datetime(2026, 11, 1), toEnteredTime=dt.datetime(2026, 11, 2, 12, 0, 0, 123000))
    assert last(fake).query["fromEnteredTime"] == "2026-11-01T00:00:00.000Z"
    assert last(fake).query["toEnteredTime"] == "2026-11-02T12:00:00.123Z"
    for bad in ("2026-11-01", "yesterday", "2026-13-01T00:00:00.000Z", "2026-11-01 00:00:00", 5, ""):
        refuses(endpoints.orders, "A1", fromEnteredTime=bad)
        refuses(endpoints.orders, "A1", toEnteredTime=bad)
    refuses(endpoints.orders, "A1", status="OPEN")
    refuses(endpoints.orders, "A1", status="filled")
    refuses(endpoints.orders, "A1", status=["FILLED", "WORKING"])      # one status only (REF 3.2)
    refuses(endpoints.orders, "A1", maxResults=0)
    refuses(endpoints.orders, "A1", maxResults="all")
    refuses(endpoints.orders, "A1", maxResults=2.5)


# ============================================================ 16. GET /accounts/{accountNumber}/orders/{orderId}


def test_endpoint_16_order_route_and_complete_read(fake):
    data = load("order.json")
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH_2}/orders/1000000001", data)
    result = endpoints.order("A2", 1000000001)
    call = last(fake)
    assert (call.method, call.path, call.query) == ("GET", f"{TR}/accounts/{FAKE_HASH_2}/orders/1000000001", {})
    assert type(result) is models.Order and extras(result) == [] and models.dump(result) == data
    assert_full(result, ORDER_FIELDS)
    assert (result.status, result.orderType, result.cancelable, result.editable) == ("WORKING", "LIMIT", result.cancelable, result.editable)
    assert result.cancelable is not None and result.editable is not None
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}/orders/9223372036854775807", data)
    endpoints.order("A1", "9223372036854775807")                        # int64 as text
    assert last(fake).path.endswith("/9223372036854775807")


def test_endpoint_16_order_validations(fake):
    for bad in ("abc", -1, "1/2", "12345678901234567890", "", None, True, 1.5, "1 "):
        refuses(endpoints.order, "A1", bad)
    nothing_sent(fake)


# ============================================================================================ 19. GET /orders


def test_endpoint_19_all_orders_route_parameters_and_complete_read(fake):
    data = load("orders.json")
    fake.on("GET", TR + "/orders", data)
    result = endpoints.orders(fromEnteredTime=NOW - dt.timedelta(days=30), toEnteredTime=stamp(NOW), status="WORKING",
                              maxResults=3000)
    call = last(fake)
    assert (call.method, call.path) == ("GET", TR + "/orders")
    assert call.query == {"maxResults": "3000", "fromEnteredTime": stamp(NOW - dt.timedelta(days=30)),
                          "toEnteredTime": stamp(NOW), "status": "WORKING"}
    assert [type(o) for o in result] == [models.Order] * 4 and extras(result) == [] and models.dump(result) == data
    for order in result:
        assert_full(order, "orderId status accountNumber session duration orderType enteredTime closeTime quantity "
                           "filledQuantity remainingQuantity orderLegCollection orderActivityCollection statusDescription")
    assert {o.accountNumber for o in result} == {12345678, 87654321}
    assert fake.paths("GET").count(ACCOUNT_NUMBERS) == 0                # no account in the path: no table needed


def test_endpoint_19_all_orders_are_limited_to_60_days(fake):
    fake.on("GET", TR + "/orders", [])
    endpoints.orders()                                                  # the default window: 59 days 23 hours up to now
    assert last(fake).query == {"fromEnteredTime": stamp(NOW - dt.timedelta(days=59, hours=23)), "toEnteredTime": stamp(NOW)}
    endpoints.orders(fromEnteredTime=NOW - dt.timedelta(days=59, hours=23))
    assert last(fake).query["fromEnteredTime"] == stamp(NOW - dt.timedelta(days=59, hours=23))
    # a request for "up to 60 days" is moved to 59 days 23 hours: the server counts from its own clock
    endpoints.orders(fromEnteredTime=NOW - dt.timedelta(days=60) + dt.timedelta(minutes=10))
    assert last(fake).query["fromEnteredTime"] == stamp(NOW - dt.timedelta(days=59, hours=23))
    endpoints.orders(fromEnteredTime=NOW - dt.timedelta(days=60) + dt.timedelta(hours=2))
    assert last(fake).query["fromEnteredTime"] == stamp(NOW - dt.timedelta(days=60) + dt.timedelta(hours=2))
    count = len(fake.calls)
    refuses(endpoints.orders, fromEnteredTime=NOW - dt.timedelta(days=61))
    refuses(endpoints.orders, fromEnteredTime="2026-01-01T00:00:00.000Z", toEnteredTime="2026-11-20T00:00:00.000Z")
    refuses(endpoints.orders, fromEnteredTime=NOW - dt.timedelta(days=10), toEnteredTime=NOW - dt.timedelta(days=11))
    assert len(fake.calls) == count


def test_endpoint_19_all_orders_status_takes_each_value(fake):
    fake.on("GET", TR + "/orders", [])
    for status in models.enum_values("OrderStatus"):
        endpoints.orders(status=status)
        assert last(fake).query["status"] == status
    refuses(endpoints.orders, status="NOPE")


# ================================================================ 21. GET /accounts/{accountNumber}/transactions

TRANSACTION_TYPES = ("TRADE", "RECEIVE_AND_DELIVER", "DIVIDEND_OR_INTEREST", "ACH_RECEIPT", "ACH_DISBURSEMENT", "CASH_RECEIPT",
                     "CASH_DISBURSEMENT", "ELECTRONIC_FUND", "WIRE_OUT", "WIRE_IN", "JOURNAL", "MEMORANDUM", "MARGIN_CALL",
                     "MONEY_MARKET", "SMA_ADJUSTMENT")


def test_endpoint_21_transactions_route_parameters_and_complete_read(fake):
    data = load("transactions.json")
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}/transactions", data)
    result = endpoints.transactions("A1", types="TRADE", startDate="2026-10-01T00:00:00.000Z",
                                    endDate="2026-11-20T23:59:59.000Z", symbol="AAPL")
    call = last(fake)
    assert (call.method, call.path) == ("GET", f"{TR}/accounts/{FAKE_HASH}/transactions")
    assert call.query == {"startDate": "2026-10-01T00:00:00.000Z", "endDate": "2026-11-20T23:59:59.000Z", "symbol": "AAPL",
                          "types": "TRADE"}
    assert [type(t) for t in result] == [models.Transaction] * 2 and extras(result) == [] and models.dump(result) == data
    trade = result[0]
    assert_full(trade, REF_FIELDS["Transaction"])
    assert_full(trade.user, REF_FIELDS["UserDetails"])                  # the sanitizing is accounts.public's job, not this
    assert trade.user.login == "fake-login" and trade.accountNumber == "12345678"
    kinds = [type(item.instrument).__name__ for item in trade.transferItems]
    assert kinds == ["TransactionEquity", "TransactionOption", "TransactionFixedIncome", "TransactionMutualFund",
                     "TransactionCashEquivalent", "CollectiveInvestment", "Currency", "Forex", "Future", "Index", "Product"]
    for item in trade.transferItems:
        assert_full(item, REF_FIELDS["TransferItem"])
        assert_full(item.instrument, "assetType cusip symbol description instrumentId netChange")
    option = trade.transferItems[1].instrument
    assert type(option.deliverable) is models.TransactionEquity
    assert_full(option, "expirationDate optionDeliverables optionPremiumMultiplier putCall strikePrice underlyingSymbol "
                        "underlyingCusip deliverable type")
    assert_full(option.optionDeliverables[0], "rootSymbol strikePercent deliverableNumber deliverableUnits deliverable assetType")
    forex = trade.transferItems[7].instrument
    assert type(forex.baseCurrency) is models.Currency and type(forex.counterCurrency) is models.Currency
    assert_full(trade.transferItems[8].instrument, "activeContract expirationDate lastTradingDate firstNoticeDate multiplier type")
    assert_full(trade.transferItems[3].instrument, "fundFamilyName fundFamilySymbol fundGroup exchangeCutoffTime purchaseCutoffTime redemptionCutoffTime type")
    assert_full(trade.transferItems[2].instrument, "maturityDate factor multiplier variableRate type")


def test_endpoint_21_transactions_types_are_required_and_each_one_is_sent(fake):
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}/transactions", [])
    assert models.enum_values("TransactionType") == TRANSACTION_TYPES
    for kind in TRANSACTION_TYPES:
        endpoints.transactions("A1", types=kind)
        assert last(fake).query["types"] == kind
    endpoints.transactions("A1", types=["TRADE", "DIVIDEND_OR_INTEREST"])
    assert last(fake).query["types"] == "TRADE,DIVIDEND_OR_INTEREST"
    assert "symbol" not in last(fake).query
    count = len(fake.calls)
    for bad in (None, "", [], "TRADE,BOGUS", "trade", ["WIRE"]):
        refuses(endpoints.transactions, "A1", types=bad)
    refuses(endpoints.transactions, "A1")
    assert len(fake.calls) == count


def test_endpoint_21_transactions_window_symbol_and_validations(fake):
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}/transactions", [])
    endpoints.transactions("A1", types="TRADE")
    assert last(fake).query == {"startDate": stamp(NOW - dt.timedelta(days=364, hours=23)), "endDate": stamp(NOW), "types": "TRADE"}
    endpoints.transactions("A1", types="TRADE", startDate=NOW - dt.timedelta(days=365), endDate=NOW)
    refuses(endpoints.transactions, "A1", types="TRADE", startDate=NOW - dt.timedelta(days=366), endDate=NOW)   # one year at most
    refuses(endpoints.transactions, "A1", types="TRADE", startDate="2026-12-01T00:00:00.000Z", endDate="2026-11-01T00:00:00.000Z")
    refuses(endpoints.transactions, "A1", types="TRADE", startDate="2026-12-01")
    refuses(endpoints.transactions, "A1", types="TRADE", symbol="")
    refuses(endpoints.transactions, "A1", types="TRADE", symbol="A\n")


def test_endpoint_21_transactions_symbol_with_special_characters_is_encoded_once(fake, monkeypatch):
    urls = []

    def spy(method, url, headers, body):
        urls.append(url)
        return 200, "[]", None

    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}/transactions", [])
    endpoints.transactions("A1", types="TRADE")                         # builds the table with the real fake first
    monkeypatch.setattr(http, "_send", spy)
    endpoints.transactions("A1", types="TRADE", symbol="$SPX")
    endpoints.transactions("A1", types="TRADE", symbol="AMZN  230317C01360000")
    assert "symbol=%24SPX" in urls[0] and "symbol=AMZN%20%20230317C01360000" in urls[1] and "%25" not in "".join(urls)


# ================================================ 22. GET /accounts/{accountNumber}/transactions/{transactionId}


def test_endpoint_22_transaction_route_and_complete_read(fake):
    data = load("transaction.json")
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}/transactions/777001", data)
    result = endpoints.transaction("A1", 777001)
    call = last(fake)
    assert (call.method, call.path, call.query) == ("GET", f"{TR}/accounts/{FAKE_HASH}/transactions/777001", {})
    assert type(result) is models.Transaction and extras(result) == [] and models.dump([result]) == data   # the REF: a list of one
    assert_full(result, REF_FIELDS["Transaction"])
    assert len(result.transferItems) == 2
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}/transactions/5", data[0])                                    # an object is read the same
    assert endpoints.transaction("A1", "5").activityId == 777001
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}/transactions/6", [])
    assert endpoints.transaction("A1", 6) is None


def test_endpoint_22_transaction_validations(fake):
    for bad in ("abc", -1, "1/2", "12345678901234567890", "", None, False):
        refuses(endpoints.transaction, "A1", bad)
    nothing_sent(fake)


# ================================================================================ 23. GET /userPreference


def test_endpoint_23_user_preference_route_and_complete_read(fake):
    data = load("user_preference.json")
    fake.on("GET", TR + "/userPreference", data)
    result = endpoints.user_preference()
    call = last(fake)
    assert (call.method, call.path, call.query) == ("GET", TR + "/userPreference", {})
    assert type(result) is models.UserPreference and extras(result) == [] and models.dump([result]) == data
    assert_full(result, REF_FIELDS["UserPreference"])
    assert [type(a) for a in result.accounts] == [models.UserPreferenceAccount] * 2
    for account in result.accounts:
        assert_full(account, REF_FIELDS["UserPreferenceAccount"])
    assert_full(result.streamerInfo[0], REF_FIELDS["StreamerInfo"])
    assert_full(result.offers[0], REF_FIELDS["Offer"])
    assert result.offers[0].level2Permissions is True and result.accounts[0].primaryAccount is True
    fake.on("GET", TR + "/userPreference", data[0])
    assert endpoints.user_preference().offers[0].mktDataPermission == "NP"


# =============================================================================== what is not here, and errors


def test_the_four_routes_that_write_have_no_code_and_every_call_is_a_get(fake):
    for name in ("place_order", "replace_order", "cancel_order", "preview_order", "post", "put", "delete"):
        assert not hasattr(endpoints, name)
    fake.on("GET", MD + "/quotes", {})
    fake.on("GET", MD + "/expirationchain", {})
    fake.on("GET", TR + "/userPreference", [])
    endpoints.quotes("A")
    endpoints.expirations("A")
    endpoints.user_preference()
    assert {call.method for call in fake.calls} == {"GET"}


def test_a_server_error_reaches_the_caller_as_the_door_raised_it(fake):
    fake.on("GET", MD + "/expirationchain", (404, {"errors": [{"status": "404", "title": "Not Found"}]}))
    with pytest.raises(SchwabError) as error:
        endpoints.expirations("ZZZZ")
    assert error.value.code == NOT_FOUND and error.value.correl_id == "test-correl-id"
    fake.on("GET", MD + "/quotes", (400, {"errors": [{"status": "400", "title": "Bad Request", "detail": "x"}]}))
    with pytest.raises(SchwabError) as error:
        endpoints.quotes("AAPL")
    assert error.value.status == 400 and error.value.errors[0]["title"] == "Bad Request"


def test_nothing_the_functions_log_or_raise_carries_the_account_hash(fake, caplog):
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}/orders", (404, {"errors": []}))
    with caplog.at_level("DEBUG"):
        with pytest.raises(SchwabError) as error:
            endpoints.orders("A1")
    text = " ".join(r.getMessage() for r in caplog.records) + json.dumps([str(error.value), error.value.detail, error.value.errors])
    assert FAKE_HASH not in text and "12345678" not in text

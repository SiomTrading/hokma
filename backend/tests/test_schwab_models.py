"""models.py (SPEC-015 5.10, 15): every enum has exactly the values of the REF, every schema all its fields, the reading is
tolerant (new field to 'extra', unknown enum value kept with a warning that carries no data) and 'dump' gives the whole JSON back."""
import json
import logging

import pytest
from schwab_support import FIXTURES, REF_BALANCES, REF_ENUMS, REF_FIELDS, assert_full, extras, load

from hokma.schwab import models

# Enums that the REF gives inline in Appendix A and B or in the parameter tables of REF 2 (the tables of 2.11 and 3.5 are
# in schwab_support.REF_ENUMS).
INLINE_ENUMS = {
    "AdvancedOrderType": ("NONE", "OTO", "OCO", "OTOCO", "OT2OCO", "OT3OCO", "BLAST_ALL", "OTA", "PAIR"),
    "OrderLegType": ("EQUITY", "OPTION", "INDEX", "MUTUAL_FUND", "CASH_EQUIVALENT", "FIXED_INCOME", "CURRENCY",
                     "COLLECTIVE_INVESTMENT"),
    "PositionEffect": ("OPENING", "CLOSING", "AUTOMATIC", "UNKNOWN"),               # B.1 has 3, B.6 adds UNKNOWN: the union
    "QuantityType": ("ALL_SHARES", "DOLLARS", "SHARES"),
    "DivCapGains": ("REINVEST", "PAYOUT"),
    "OrderActivityType": ("EXECUTION", "ORDER_ACTION"),
    "ExecutionType": ("FILL",),
    "AccountType": ("CASH", "MARGIN"),
    "AccountAssetType": ("EQUITY", "OPTION", "INDEX", "MUTUAL_FUND", "CASH_EQUIVALENT", "FIXED_INCOME", "CURRENCY",
                         "COLLECTIVE_INVESTMENT"),
    "CashEquivalentType": ("SWEEP_VEHICLE", "SAVINGS", "MONEY_MARKET_FUND", "UNKNOWN"),
    "PutCall": ("PUT", "CALL", "UNKNOWN"),                                            # A.6 has PUT, CALL; B.4 adds UNKNOWN
    "OptionType": ("VANILLA", "BINARY", "BARRIER", "UNKNOWN"),
    "ApiCurrencyType": ("USD", "CAD", "EUR", "JPY"),
    "CollectiveInvestmentType": ("UNIT_INVESTMENT_TRUST", "EXCHANGE_TRADED_FUND", "CLOSED_END_FUND", "INDEX", "UNITS"),
    "TransactionEquityType": ("COMMON_STOCK", "PREFERRED_STOCK", "DEPOSITORY_RECEIPT", "PREFERRED_DEPOSITORY_RECEIPT",
                              "RESTRICTED_STOCK", "COMPONENT_UNIT", "RIGHT", "WARRANT", "CONVERTIBLE_PREFERRED_STOCK",
                              "CONVERTIBLE_STOCK", "LIMITED_PARTNERSHIP", "WHEN_ISSUED", "UNKNOWN"),
    "TransactionFixedIncomeType": ("BOND_UNIT", "CERTIFICATE_OF_DEPOSIT", "CONVERTIBLE_BOND",
                                   "COLLATERALIZED_MORTGAGE_OBLIGATION", "CORPORATE_BOND", "GOVERNMENT_MORTGAGE",
                                   "GNMA_BONDS", "MUNICIPAL_ASSESSMENT_DISTRICT", "MUNICIPAL_BOND", "OTHER_GOVERNMENT",
                                   "SHORT_TERM_PAPER", "US_TREASURY_BOND", "US_TREASURY_BILL", "US_TREASURY_NOTE"),
    "ForexType": ("STANDARD", "NBBO", "UNKNOWN"),
    "FutureType": ("STANDARD", "UNKNOWN"),
    "IndexType": ("BROAD_BASED", "NARROW_BASED", "UNKNOWN"),
    "MutualFundType": ("NOT_APPLICABLE", "OPEN_END_NON_TAXABLE", "OPEN_END_TAXABLE", "NO_LOAD_NON_TAXABLE",
                       "NO_LOAD_TAXABLE", "UNKNOWN"),
    "ProductType": ("TBD", "UNKNOWN"),
    "TransactionStatus": ("VALID", "INVALID", "PENDING", "UNKNOWN"),
    "SubAccount": ("CASH", "MARGIN", "SHORT", "DIV", "INCOME", "UNKNOWN"),
    "TransactionActivityType": ("ACTIVITY_CORRECTION", "EXECUTION", "ORDER_ACTION", "TRANSFER", "UNKNOWN"),
    "UserType": ("ADVISOR_USER", "BROKER_USER", "CLIENT_USER", "SYSTEM_USER", "UNKNOWN"),
    "InstrumentAssetType": ("BOND", "EQUITY", "ETF", "EXTENDED", "FOREX", "FUTURE", "FUTURE_OPTION", "FUNDAMENTAL", "INDEX",
                            "INDICATOR", "MUTUAL_FUND", "OPTION", "UNKNOWN"),
    "Direction": ("up", "down"),
    "UnderlyingExchangeName": ("IND", "ASE", "NYS", "NAS", "NAP", "PAC", "OPR", "BATS"),
    "ErrorStatus": ("400", "401", "404", "500"),
    "QuoteField": ("quote", "fundamental", "extended", "reference", "regular"),
    "Strategy": ("SINGLE", "ANALYTICAL", "COVERED", "VERTICAL", "CALENDAR", "STRANGLE", "STRADDLE", "BUTTERFLY", "CONDOR",
                 "DIAGONAL", "COLLAR", "ROLL"),
    "ChainContractType": ("CALL", "PUT", "ALL"),
    "Range": ("ITM", "NTM", "OTM"),
    "ExpMonth": ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC", "ALL"),
    "Entitlement": ("PP", "NP", "PN"),
    "PeriodType": ("day", "month", "year", "ytd"),
    "FrequencyType": ("minute", "daily", "weekly", "monthly"),
    "MoversIndex": ("$DJI", "$COMPX", "$SPX", "NYSE", "NASDAQ", "OTCBB", "INDEX_ALL", "EQUITY_ALL", "OPTION_ALL",
                    "OPTION_PUT", "OPTION_CALL"),
    "MoversSort": ("VOLUME", "TRADES", "PERCENT_CHANGE_UP", "PERCENT_CHANGE_DOWN"),
    "MoversFrequency": (0, 1, 5, 10, 30, 60),
    "Market": ("equity", "option", "bond", "future", "forex"),
    "Projection": ("symbol-search", "symbol-regex", "desc-search", "desc-regex", "search", "fundamental"),
    "AccountFields": ("positions",),
}
# REF B.6: TransferItem.feeType lists these 16, REF 3.5 lists 25: the enum is the 25 (the union).
TRANSFER_FEE_TYPES = ("COMMISSION", "SEC_FEE", "STR_FEE", "R_FEE", "CDSC_FEE", "OPT_REG_FEE", "ADDITIONAL_FEE",
                      "MISCELLANEOUS_FEE", "FUTURES_EXCHANGE_FEE", "LOW_PROCEEDS_COMMISSION", "BASE_CHARGE",
                      "GENERAL_CHARGE", "GST_FEE", "TAF_FEE", "INDEX_OPTION_FEE", "UNKNOWN")

# Fields of the schemas the REF describes in prose or in a table the extraction in schwab_support does not cover.
OTHER_FIELDS = {
    "ReferenceOption": "contractType cusip daysToExpiration deliverables description exchange exchangeName exerciseType "
                       "expirationDay expirationMonth expirationType expirationYear isPennyPilot lastTradingDay "
                       "multiplier settlementType strikePrice underlying uvExpirationType",         # uvExpirationType: REF 2.1
    "Screener": "change description direction last symbol totalVolume",
    "Hours": "date marketType exchange category product productName isOpen sessionHours",
    "Interval": "start end",
    "QuoteRequest": "cusips fields ssids symbols realtime indicative",
    "QuoteResponseObject": "assetMainType assetSubType ssid symbol realtime quoteType",
    "InstrumentList": "instruments",
    "ScreenerList": "screeners",
    "SessionHours": "preMarket regularMarket postMarket",
    "Commission": "commissionLegs", "CommissionLeg": "commissionValues", "CommissionValue": "value type",
    "Fees": "feeLegs", "FeeLeg": "feeValues", "FeeValue": "value type",
    "FundamentalInst": "symbol high52 low52 dividendAmount dividendYield dividendDate dividendPayAmount dividendPayDate "
                       "divGrowthRate3Year dividendFreq declarationDate nextDividendPayDate nextDividendDate peRatio "
                       "pegRatio pbRatio prRatio pcfRatio grossMarginTTM grossMarginMRQ netProfitMarginTTM "
                       "netProfitMarginMRQ operatingMarginTTM operatingMarginMRQ returnOnEquity returnOnAssets "
                       "returnOnInvestment quickRatio currentRatio interestCoverage totalDebtToCapital ltDebtToEquity "
                       "totalDebtToEquity epsTTM epsChangePercentTTM epsChangeYear epsChange eps revChangeYear "
                       "revChangeTTM revChangeIn sharesOutstanding marketCapFloat marketCap bookValuePerShare "
                       "shortIntToFloat shortIntDayToCover beta vol1DayAvg vol10DayAvg vol3MonthAvg avg10DaysVolume "
                       "avg1DayVolume avg3MonthVolume dtnVolume corpactionDate fundLeverageFactor fundStrategy",
    "AccountsBaseInstrument": "assetType cusip symbol description instrumentId netChange",
    "AccountEquity": "assetType cusip symbol description instrumentId netChange",
    "AccountMutualFund": "assetType cusip symbol description instrumentId netChange",
    "AccountCashEquivalent": "assetType cusip symbol description instrumentId netChange type",
    "AccountFixedIncome": "assetType cusip symbol description instrumentId netChange maturityDate factor variableRate",
    "AccountOption": "assetType cusip symbol description instrumentId netChange optionDeliverables putCall "
                     "optionMultiplier type underlyingSymbol",
    "AccountAPIOptionDeliverable": "symbol deliverableUnits apiCurrencyType assetType",
    "TransactionBaseInstrument": "assetType cusip symbol description instrumentId netChange",
    "Currency": "assetType cusip symbol description instrumentId netChange",
    "TransactionCashEquivalent": "assetType cusip symbol description instrumentId netChange type",
    "CollectiveInvestment": "assetType cusip symbol description instrumentId netChange type",
    "TransactionEquity": "assetType cusip symbol description instrumentId netChange type",
    "TransactionFixedIncome": "assetType cusip symbol description instrumentId netChange maturityDate factor multiplier "
                              "variableRate type",
    "Forex": "assetType cusip symbol description instrumentId netChange baseCurrency counterCurrency type",
    "Future": "assetType cusip symbol description instrumentId netChange activeContract expirationDate lastTradingDate "
              "firstNoticeDate multiplier type",
    "Index": "assetType cusip symbol description instrumentId netChange activeContract type",
    "TransactionMutualFund": "assetType cusip symbol description instrumentId netChange fundFamilyName fundFamilySymbol "
                             "fundGroup exchangeCutoffTime purchaseCutoffTime redemptionCutoffTime type",
    "TransactionOption": "assetType cusip symbol description instrumentId netChange expirationDate optionDeliverables "
                         "optionPremiumMultiplier putCall strikePrice underlyingSymbol underlyingCusip deliverable type",
    "Product": "assetType cusip symbol description instrumentId netChange type",
    "MarginAccount": "type accountNumber roundTrips isDayTrader isClosingOnlyRestricted pfcbFlag positions initialBalances "
                     "currentBalances projectedBalances",
    "CashAccount": "type accountNumber roundTrips isDayTrader isClosingOnlyRestricted pfcbFlag positions initialBalances "
                   "currentBalances projectedBalances",
    "SecuritiesAccountBase": "type accountNumber roundTrips isDayTrader isClosingOnlyRestricted pfcbFlag positions",
    **REF_BALANCES,
}
SCHEMA_NAMES_OF_THE_SPEC = """Bond Instrument InstrumentResponse FundamentalInst Candle CandleList Screener Hours Interval
EquityResponse ForexResponse FutureOptionResponse FutureResponse IndexResponse MutualFundResponse OptionResponse QuoteError
QuoteEquity QuoteForex QuoteFuture QuoteFutureOption QuoteIndex QuoteMutualFund QuoteOption QuoteRequest ReferenceEquity
ReferenceForex ReferenceFuture ReferenceFutureOption ReferenceIndex ReferenceMutualFund ReferenceOption ExtendedMarket
RegularMarket Fundamental OptionChain Underlying OptionDeliverables OptionContract ExpirationChain Expiration ErrorResponse
Error ErrorSource AccountNumberHash Order OrderRequest OrderLegCollection OrderActivity ExecutionLeg PreviewOrder
OrderStrategy OrderLeg OrderBalance OrderValidationResult OrderValidationDetail CommissionAndFee Commission CommissionLeg
CommissionValue Fees FeeLeg FeeValue DateParam ServiceError Account MarginAccount CashAccount Position MarginInitialBalance
MarginBalance CashInitialBalance CashBalance AccountEquity AccountMutualFund AccountCashEquivalent AccountFixedIncome
AccountOption AccountAPIOptionDeliverable Currency TransactionCashEquivalent CollectiveInvestment TransactionEquity
TransactionFixedIncome Forex Future Index TransactionMutualFund TransactionOption Product TransactionAPIOptionDeliverable
Transaction UserDetails TransferItem UserPreference UserPreferenceAccount StreamerInfo Offer""".split()


@pytest.fixture(autouse=True)
def fresh_warnings():
    models.clear_warnings()


# -------------------------------------------------------------------------------------------------------- enums


@pytest.mark.parametrize("name", sorted(REF_ENUMS))
def test_enum_of_the_ref_tables_has_exactly_its_values(name):
    assert models.enum_values(name) == REF_ENUMS[name]


@pytest.mark.parametrize("name", sorted(INLINE_ENUMS))
def test_enum_of_the_appendices_and_parameters_has_exactly_its_values(name):
    assert models.enum_values(name) == INLINE_ENUMS[name]


def test_every_enum_of_the_module_is_checked_by_a_list_of_the_ref():
    assert set(models.ENUMS) == set(REF_ENUMS) | set(INLINE_ENUMS)


def test_the_two_lists_of_fee_types_are_joined_in_one_enum():
    assert set(TRANSFER_FEE_TYPES) <= set(models.enum_values("FeeType"))
    assert len(models.enum_values("FeeType")) == 25


def test_order_type_request_is_order_type_without_unknown():
    assert models.enum_values("OrderTypeRequest") == tuple(v for v in models.enum_values("OrderType") if v != "UNKNOWN")


def test_an_enum_is_a_literal():
    from typing import get_args, get_origin, Literal

    assert get_origin(models.Session) is Literal and get_args(models.Session) == ("NORMAL", "AM", "PM", "SEAMLESS")


# ------------------------------------------------------------------------------------------------------ schemas


@pytest.mark.parametrize("name", sorted(set(REF_FIELDS) - set(OTHER_FIELDS)))
def test_schema_has_every_field_of_the_ref(name):
    assert set(models.fields_of(name)) == set(REF_FIELDS[name].split())


@pytest.mark.parametrize("name", sorted(OTHER_FIELDS))
def test_schema_described_in_prose_has_every_field_of_the_ref(name):
    assert set(models.fields_of(name)) == set(OTHER_FIELDS[name].split())


@pytest.mark.parametrize("name", SCHEMA_NAMES_OF_THE_SPEC)
def test_every_schema_of_the_spec_exists_as_a_class(name):
    cls = getattr(models, name)
    assert cls is models.MODELS[name] and cls.SCHEMA == name


def test_the_oneof_envelopes_and_maps_exist():
    assert models.QuoteResponse and models.InstrumentList and models.ScreenerList and models.SecuritiesAccountBase


def test_the_names_that_are_not_python_names_have_an_attribute():
    quote = models.QuoteEquity.read({"52WeekHigh": 169, "52WeekLow": 1.1})
    assert (quote.week52High, quote.week52Low) == (169.0, 1.1)
    assert models.dump(quote) == {"52WeekHigh": 169.0, "52WeekLow": 1.1}


# ----------------------------------------------------------------------------------------- tolerant reading


def test_an_unknown_enum_value_is_kept_as_text_and_warns_without_data(caplog):
    with caplog.at_level(logging.WARNING, logger="hokma.schwab"):
        order = models.Order.read({"session": "WEIRD_SESSION_VALUE", "status": "WORKING"})
    assert order.session == "WEIRD_SESSION_VALUE" and order.status == "WORKING"
    assert len(caplog.records) == 1
    text = caplog.records[0].getMessage()
    assert "Session" in text and "Order.session" in text and "WEIRD_SESSION_VALUE" not in text


def test_the_warning_comes_once_per_enum_and_field_until_cleared(caplog):
    with caplog.at_level(logging.WARNING, logger="hokma.schwab"):
        for _ in range(3):
            models.Order.read({"session": "NEW_ONE"})
        assert len(caplog.records) == 1
        models.clear_warnings()
        models.Order.read({"session": "NEW_ONE"})
    assert len(caplog.records) == 2


def test_a_known_value_and_a_null_do_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="hokma.schwab"):
        models.Order.read({"session": "NORMAL", "duration": None})
        models.Fundamental.read({"divFreq": 12, "fundStrategy": None})
    assert caplog.records == []


def test_div_freq_of_the_ref_example_that_is_not_in_the_enum_is_kept(caplog):
    with caplog.at_level(logging.WARNING, logger="hokma.schwab"):
        fundamental = models.Fundamental.read({"divFreq": 0})
    assert fundamental.divFreq == 0 and len(caplog.records) == 1


def test_a_new_field_goes_to_extra_at_every_level_and_dump_gives_it_back():
    data = {"symbol": "AAPL", "brandNew": {"a": 1},
            "callExpDateMap": {"2026-12-18:28": {"100.0": [{"putCall": "CALL", "contractNew": [1, 2]}]}}}
    chain = models.OptionChain.read(data)
    assert chain.extra == {"brandNew": {"a": 1}}
    contract = chain.callExpDateMap["2026-12-18:28"]["100.0"][0]
    assert contract.putCall == "CALL" and contract.extra == {"contractNew": [1, 2]}
    assert extras(chain) == [("root", ["brandNew"]), ("root.callExpDateMap[2026-12-18:28][100.0][0]", ["contractNew"])]
    assert models.dump(chain) == data


def test_a_field_that_the_schema_has_never_goes_to_extra():
    order = models.Order.read({"orderId": 5, "price": 1.5, "tag": "TA_x"})
    assert order.extra == {} and (order.orderId, order.price, order.tag) == (5, 1.5, "TA_x")


def test_reading_something_that_is_not_an_object_gives_none():
    assert models.Order.read(None) is None and models.Order.read([1]) is None and models.QuoteResponse.read("x") is None


def test_numbers_of_the_wrong_width_are_kept():
    contract = models.OptionContract.read({"quoteTimeInLong": 1763652600123, "tradeTimeInLong": 1763652600999,
                                           "openInterest": 12, "totalVolume": 7, "strikePrice": 100,
                                           "lastTradingDay": 1766091600000, "tradeDate": 1763652600000})
    assert contract.quoteTimeInLong == 1763652600123 and contract.tradeTimeInLong == 1763652600999    # int32 in the REF
    assert contract.openInterest == 12.0 and isinstance(contract.openInterest, float)                  # a double
    assert isinstance(contract.strikePrice, float) and isinstance(contract.totalVolume, int)
    assert models.OptionContract.read({"totalVolume": 7.0}).totalVolume == 7
    assert models.QuoteFuture.read({"openInterest": 5}).openInterest == 5 and isinstance(
        models.QuoteFuture.read({"openInterest": 5}).openInterest, int)                                 # int32 in futures
    assert isinstance(models.QuoteOption.read({"openInterest": 5}).openInterest, float)                 # double in options


def test_order_account_number_is_an_integer_and_the_others_are_text():
    assert models.Order.read({"accountNumber": 12345678}).accountNumber == 12345678
    assert models.Transaction.read({"accountNumber": "12345678"}).accountNumber == "12345678"
    assert models.UserPreferenceAccount.read({"accountNumber": "12345678"}).accountNumber == "12345678"


def test_expiration_reads_expiration_and_expiration_date():
    first = models.Expiration.read({"expirationDate": "2026-11-27", "expirationType": "W", "standard": True})
    second = models.Expiration.read({"expiration": "2026-12-18", "settlementType": "P", "optionRoots": "AAPL"})
    assert first.expirationDate == "2026-11-27" and first.extra == {}
    assert second.expiration == "2026-12-18" and second.extra == {} and second.optionRoots == "AAPL"


def test_the_repr_never_prints_values():
    order = models.Order.read({"accountNumber": 12345678, "orderId": 99})
    assert "12345678" not in repr(order) and "99" not in repr(order)
    assert "12345678" not in repr(models.Account.read({"securitiesAccount": {"type": "CASH", "accountNumber": "12345678"}}))


def test_dump_drops_what_the_json_did_not_have_and_returns_only_json_types():
    order = models.Order.read({"orderId": 5, "orderLegCollection": [{"legId": 1}], "status": "QUEUED"})
    assert models.dump(order) == {"orderId": 5, "orderLegCollection": [{"legId": 1}], "status": "QUEUED"}
    json.dumps(models.dump(order))
    assert models.dump([order, {"a": order}])[1]["a"]["orderId"] == 5


# ------------------------------------------------------------------------------------------------------ oneOf


def test_securities_account_is_margin_or_cash_by_type():
    margin = models.Account.read({"securitiesAccount": {"type": "MARGIN", "currentBalances": {"availableFunds": 5, "sma": 1}}})
    cash = models.Account.read({"securitiesAccount": {"type": "CASH", "currentBalances": {"cashCall": 2, "totalCash": 3}}})
    assert isinstance(margin.securitiesAccount, models.MarginAccount)
    assert isinstance(margin.securitiesAccount.currentBalances, models.MarginBalance)
    assert margin.securitiesAccount.currentBalances.sma == 1.0 and margin.securitiesAccount.currentBalances.extra == {}
    assert isinstance(cash.securitiesAccount, models.CashAccount)
    assert isinstance(cash.securitiesAccount.currentBalances, models.CashBalance)
    assert cash.securitiesAccount.currentBalances.cashCall == 2.0 and cash.securitiesAccount.currentBalances.extra == {}


def test_securities_account_without_a_type_is_told_by_its_balances():
    assert isinstance(models.Account.read({"securitiesAccount": {"currentBalances": {"buyingPower": 1}}}).securitiesAccount,
                      models.MarginAccount)
    assert isinstance(models.Account.read({"securitiesAccount": {"currentBalances": {"cashAvailableForTrading": 1}}}).securitiesAccount,
                      models.CashAccount)
    other = models.Account.read({"securitiesAccount": {"type": "IRA", "currentBalances": {"x": 1}}}).securitiesAccount
    assert type(other) is models.SecuritiesAccountBase and other.type == "IRA" and other.extra == {"currentBalances": {"x": 1}}


@pytest.mark.parametrize("asset, cls", [
    ("EQUITY", "AccountEquity"), ("OPTION", "AccountOption"), ("MUTUAL_FUND", "AccountMutualFund"),
    ("FIXED_INCOME", "AccountFixedIncome"), ("CASH_EQUIVALENT", "AccountCashEquivalent"),
    ("INDEX", "AccountsBaseInstrument"), ("CURRENCY", "AccountsBaseInstrument"),
    ("COLLECTIVE_INVESTMENT", "AccountsBaseInstrument")])
def test_accounts_instrument_is_resolved_by_asset_type(asset, cls):
    leg = models.OrderLegCollection.read({"instrument": {"assetType": asset, "symbol": "FAKE"}})
    assert type(leg.instrument) is getattr(models, cls) and leg.instrument.symbol == "FAKE"


def test_accounts_instrument_of_the_ref_example_has_a_type_and_no_asset_type():
    position = models.Position.read({"instrument": {"cusip": "1", "symbol": "X", "type": "SWEEP_VEHICLE"}})
    assert type(position.instrument) is models.AccountCashEquivalent and position.instrument.type == "SWEEP_VEHICLE"
    assert position.instrument.extra == {}
    assert type(models.Position.read({"instrument": {"symbol": "X", "type": "VANILLA"}}).instrument) is models.AccountOption
    assert type(models.Position.read({"instrument": {"symbol": "X", "maturityDate": "2030-01-01"}}).instrument) is \
        models.AccountFixedIncome
    assert type(models.Position.read({"instrument": {"symbol": "X"}}).instrument) is models.AccountsBaseInstrument


@pytest.mark.parametrize("asset, cls", [
    ("EQUITY", "TransactionEquity"), ("OPTION", "TransactionOption"), ("MUTUAL_FUND", "TransactionMutualFund"),
    ("FIXED_INCOME", "TransactionFixedIncome"), ("CASH_EQUIVALENT", "TransactionCashEquivalent"),
    ("COLLECTIVE_INVESTMENT", "CollectiveInvestment"), ("CURRENCY", "Currency"), ("INDEX", "Index"),
    ("FOREX", "Forex"), ("FUTURE", "Future"), ("PRODUCT", "Product")])
def test_transaction_instrument_is_resolved_by_asset_type(asset, cls):
    item = models.TransferItem.read({"instrument": {"assetType": asset, "symbol": "FAKE"}})
    assert type(item.instrument) is getattr(models, cls)
    assert models.TransferItem.read({"instrument": {"symbol": "FAKE"}}).instrument.__class__ is models.TransactionBaseInstrument


def test_the_nested_one_of_of_a_transaction_option_is_resolved_too():
    option = models.TransferItem.read({"instrument": {
        "assetType": "OPTION", "deliverable": {"assetType": "EQUITY", "type": "COMMON_STOCK"},
        "optionDeliverables": [{"rootSymbol": "FAKE", "deliverable": {"assetType": "FOREX", "baseCurrency": {"assetType": "CURRENCY"}}}]}}).instrument
    assert type(option.deliverable) is models.TransactionEquity
    inner = option.optionDeliverables[0].deliverable
    assert type(inner) is models.Forex and type(inner.baseCurrency) is models.Currency


@pytest.mark.parametrize("main, cls", [
    ("EQUITY", "EquityResponse"), ("OPTION", "OptionResponse"), ("FOREX", "ForexResponse"), ("FUTURE", "FutureResponse"),
    ("FUTURE_OPTION", "FutureOptionResponse"), ("INDEX", "IndexResponse"), ("MUTUAL_FUND", "MutualFundResponse"),
    ("BOND", "QuoteResponseObject")])
def test_quote_response_object_is_resolved_by_asset_main_type(main, cls):
    answer = models.QuoteResponse.read({"X": {"assetMainType": main, "symbol": "X", "quote": {"lastPrice": 1}}})
    assert type(answer["X"]) is getattr(models, cls)
    if cls != "QuoteResponseObject":
        assert type(answer["X"].quote).__name__ == "Quote" + cls.removesuffix("Response")
    else:
        assert answer["X"].extra == {"quote": {"lastPrice": 1}}


def test_quote_response_is_a_map_by_symbol_plus_quote_error():
    answer = models.QuoteResponse.read({"AAPL": {"assetMainType": "EQUITY"}, "$SPX": {"assetMainType": "INDEX"},
                                        "errors": {"invalidSymbols": ["ZZZ"], "invalidCusips": ["1"], "invalidSSIDs": [2]},
                                        "other": 5})
    assert list(answer) == ["AAPL", "$SPX"] and len(answer) == 2 and "AAPL" in answer and answer.get("nope") is None
    assert type(answer.errors) is models.QuoteError
    assert (answer.errors.invalidSymbols, answer.errors.invalidCusips, answer.errors.invalidSSIDs) == (["ZZZ"], ["1"], [2])
    assert answer.extra == {"other": 5}
    assert models.dump(answer)["errors"] == {"invalidCusips": ["1"], "invalidSSIDs": [2], "invalidSymbols": ["ZZZ"]}


# --------------------------------------------------------------------------------------------- the fixtures

READERS = {
    "quotes.json": models.QuoteResponse, "quote_single.json": models.EquityResponse, "chains.json": models.OptionChain,
    "expirationchain.json": models.ExpirationChain, "pricehistory.json": models.CandleList,
    "movers.json": models.ScreenerList, "instruments.json": models.InstrumentList,
    "instruments_fundamental.json": models.InstrumentList, "instrument_cusip.json": models.InstrumentResponse,
    "account_margin.json": models.Account, "account_cash.json": models.Account, "order.json": models.Order,
}
LIST_READERS = {
    "account_numbers.json": models.AccountNumberHash, "accounts.json": models.Account,
    "accounts_no_positions.json": models.Account, "orders.json": models.Order, "transactions.json": models.Transaction,
    "transaction.json": models.Transaction, "user_preference.json": models.UserPreference,
}


@pytest.mark.parametrize("name", sorted(READERS))
def test_a_fixture_is_read_completely_and_dumped_back_identical(name):
    data = load(name)
    obj = READERS[name].read(data)
    assert extras(obj) == []
    assert models.dump(obj) == data


@pytest.mark.parametrize("name", sorted(LIST_READERS))
def test_a_list_fixture_is_read_completely_and_dumped_back_identical(name):
    data = load(name)
    objs = [LIST_READERS[name].read(item) for item in data]
    assert extras(objs) == []
    assert models.dump(objs) == data


def test_the_market_hours_fixtures_are_read_completely():
    for name in ("markets.json", "market_equity.json"):
        data = load(name)
        read = {m: {p: models.Hours.read(h) for p, h in products.items()} for m, products in data.items()}
        assert extras(read) == [] and models.dump(read) == data


def test_the_fixtures_are_made_up_data_only():
    text = "".join(path.read_text(encoding="utf-8") for path in FIXTURES.glob("*.json"))
    assert "12345678" in text and "87654321" in text
    for real_looking in ("Bearer", "refresh_token", "access_token", "client_secret"):
        assert real_looking not in text

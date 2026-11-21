"""Typed readers for every schema of the Schwab Trader API, read only (SPEC-015 5.10, 15).

Source: REF-schwab-trader-api.md (sections 2.11, 3.5, Appendix A and Appendix B). Every schema is declared once, in
SCHEMAS, as '<jsonName>:<type>' tokens and becomes a dataclass of the same name (all fields optional, keyword only):

    s str, i int, f float, b bool, any raw value
    E:Name  enum (a Literal of ENUMS)            S:Name  nested schema          O:Name  oneOf, resolved by its discriminator
    [T] list of T                                 {T} map of text key to T

Tolerant reading (the REF is not the server): a key the schema does not have goes to 'extra'; an enum value that is not
in the Literal is kept as text and logs one warning per enum and field (the log carries no data); a number of the
wrong width is kept (int32 fields that arrive as 64 bit values are plain Python ints; a double that arrives as 12 is
read as 12.0). 'dump(obj)' returns the whole JSON again, extra included. Nothing here calls the network.

The order schemas (OrderRequest, PreviewOrder and their nested ones) are typed so the order task can start from them;
no code of 1.0.9 sends them.
"""
import keyword
import logging
from dataclasses import dataclass, field, make_dataclass
from typing import Any, ClassVar, Literal, get_args

log = logging.getLogger("hokma.schwab")

# ----------------------------------------------------------------------------------------------------------- enums

ENUMS: dict[str, Any] = {}


def _enum(name: str, *values: Any) -> Any:
    literal = Literal[tuple(values)]
    ENUMS[name] = literal
    return literal


def enum_values(name: str) -> tuple:
    """The exact values of an enum, in the order of the REF."""
    return get_args(ENUMS[name])


# REF 3.5 (orders and transactions)
Session = _enum("Session", "NORMAL", "AM", "PM", "SEAMLESS")
Duration = _enum("Duration", "DAY", "GOOD_TILL_CANCEL", "FILL_OR_KILL", "IMMEDIATE_OR_CANCEL", "END_OF_WEEK",
                 "END_OF_MONTH", "NEXT_END_OF_MONTH", "UNKNOWN")
_ORDER_TYPES = ("MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP", "CABINET", "NON_MARKETABLE", "MARKET_ON_CLOSE",
                "EXERCISE", "TRAILING_STOP_LIMIT", "NET_DEBIT", "NET_CREDIT", "NET_ZERO", "LIMIT_ON_CLOSE")
OrderType = _enum("OrderType", *_ORDER_TYPES, "UNKNOWN")
OrderTypeRequest = _enum("OrderTypeRequest", *_ORDER_TYPES)                       # no UNKNOWN as input
ComplexOrderStrategyType = _enum(
    "ComplexOrderStrategyType", "NONE", "COVERED", "VERTICAL", "BACK_RATIO", "CALENDAR", "DIAGONAL", "STRADDLE",
    "STRANGLE", "COLLAR_SYNTHETIC", "BUTTERFLY", "CONDOR", "IRON_CONDOR", "VERTICAL_ROLL", "COLLAR_WITH_STOCK",
    "DOUBLE_DIAGONAL", "UNBALANCED_BUTTERFLY", "UNBALANCED_CONDOR", "UNBALANCED_IRON_CONDOR", "UNBALANCED_VERTICAL_ROLL",
    "MUTUAL_FUND_SWAP", "CUSTOM")
RequestedDestination = _enum("RequestedDestination", "INET", "ECN_ARCA", "CBOE", "AMEX", "PHLX", "ISE", "BOX", "NYSE",
                             "NASDAQ", "BATS", "C2", "AUTO")
_LINK_BASIS = ("MANUAL", "BASE", "TRIGGER", "LAST", "BID", "ASK", "ASK_BID", "MARK", "AVERAGE")
StopPriceLinkBasis = _enum("StopPriceLinkBasis", *_LINK_BASIS)
PriceLinkBasis = _enum("PriceLinkBasis", *_LINK_BASIS)
StopPriceLinkType = _enum("StopPriceLinkType", "VALUE", "PERCENT", "TICK")
PriceLinkType = _enum("PriceLinkType", "VALUE", "PERCENT", "TICK")
StopType = _enum("StopType", "STANDARD", "BID", "ASK", "LAST", "MARK")
TaxLotMethod = _enum("TaxLotMethod", "FIFO", "LIFO", "HIGH_COST", "LOW_COST", "AVERAGE_COST", "SPECIFIC_LOT",
                     "LOSS_HARVESTER")
SpecialInstruction = _enum("SpecialInstruction", "ALL_OR_NONE", "DO_NOT_REDUCE", "ALL_OR_NONE_DO_NOT_REDUCE")
OrderStrategyType = _enum("OrderStrategyType", "SINGLE", "CANCEL", "RECALL", "PAIR", "FLATTEN", "TWO_DAY_SWAP",
                          "BLAST_ALL", "OCO", "TRIGGER")
OrderStatus = _enum(                                                                # REF: 'status' and 'apiOrderStatus'
    "OrderStatus", "AWAITING_PARENT_ORDER", "AWAITING_CONDITION", "AWAITING_STOP_CONDITION", "AWAITING_MANUAL_REVIEW",
    "ACCEPTED", "AWAITING_UR_OUT", "PENDING_ACTIVATION", "QUEUED", "WORKING", "REJECTED", "PENDING_CANCEL", "CANCELED",
    "PENDING_REPLACE", "REPLACED", "FILLED", "EXPIRED", "NEW", "AWAITING_RELEASE_TIME", "PENDING_ACKNOWLEDGEMENT",
    "PENDING_RECALL", "UNKNOWN")
AmountIndicator = _enum("AmountIndicator", "DOLLARS", "SHARES", "ALL_SHARES", "PERCENTAGE", "UNKNOWN")
SettlementInstruction = _enum("SettlementInstruction", "REGULAR", "CASH", "NEXT_DAY", "UNKNOWN")
APIRuleAction = _enum("APIRuleAction", "ACCEPT", "ALERT", "REJECT", "REVIEW", "UNKNOWN")
Instruction = _enum("Instruction", "BUY", "SELL", "BUY_TO_COVER", "SELL_SHORT", "BUY_TO_OPEN", "BUY_TO_CLOSE",
                    "SELL_TO_OPEN", "SELL_TO_CLOSE", "EXCHANGE", "SELL_SHORT_EXEMPT")
AssetType = _enum("AssetType", "EQUITY", "MUTUAL_FUND", "OPTION", "FUTURE", "FOREX", "INDEX", "CASH_EQUIVALENT",
                  "FIXED_INCOME", "PRODUCT", "CURRENCY", "COLLECTIVE_INVESTMENT")
# REF 3.5 lists 25 values; REF B.6 (TransferItem.feeType) lists 16, all inside the 25 (the union is the 25).
FeeType = _enum("FeeType", "COMMISSION", "SEC_FEE", "STR_FEE", "R_FEE", "CDSC_FEE", "OPT_REG_FEE", "ADDITIONAL_FEE",
                "MISCELLANEOUS_FEE", "FTT", "FUTURES_CLEARING_FEE", "FUTURES_DESK_OFFICE_FEE", "FUTURES_EXCHANGE_FEE",
                "FUTURES_GLOBEX_FEE", "FUTURES_NFA_FEE", "FUTURES_PIT_BROKERAGE_FEE", "FUTURES_TRANSACTION_FEE",
                "LOW_PROCEEDS_COMMISSION", "BASE_CHARGE", "GENERAL_CHARGE", "GST_FEE", "TAF_FEE", "INDEX_OPTION_FEE",
                "TEFRA_TAX", "STATE_TAX", "UNKNOWN")
TransactionType = _enum("TransactionType", "TRADE", "RECEIVE_AND_DELIVER", "DIVIDEND_OR_INTEREST", "ACH_RECEIPT",
                        "ACH_DISBURSEMENT", "CASH_RECEIPT", "CASH_DISBURSEMENT", "ELECTRONIC_FUND", "WIRE_OUT",
                        "WIRE_IN", "JOURNAL", "MEMORANDUM", "MARGIN_CALL", "MONEY_MARKET", "SMA_ADJUSTMENT")

# REF Appendix B
AdvancedOrderType = _enum("AdvancedOrderType", "NONE", "OTO", "OCO", "OTOCO", "OT2OCO", "OT3OCO", "BLAST_ALL", "OTA",
                          "PAIR")
OrderLegType = _enum("OrderLegType", "EQUITY", "OPTION", "INDEX", "MUTUAL_FUND", "CASH_EQUIVALENT", "FIXED_INCOME",
                     "CURRENCY", "COLLECTIVE_INVESTMENT")
# REF B.1 (OrderLegCollection) has 3 values, REF B.6 (TransferItem) adds UNKNOWN: the union.
PositionEffect = _enum("PositionEffect", "OPENING", "CLOSING", "AUTOMATIC", "UNKNOWN")
QuantityType = _enum("QuantityType", "ALL_SHARES", "DOLLARS", "SHARES")
DivCapGains = _enum("DivCapGains", "REINVEST", "PAYOUT")
OrderActivityType = _enum("OrderActivityType", "EXECUTION", "ORDER_ACTION")
ExecutionType = _enum("ExecutionType", "FILL")
AccountType = _enum("AccountType", "CASH", "MARGIN")
AccountAssetType = _enum("AccountAssetType", "EQUITY", "OPTION", "INDEX", "MUTUAL_FUND", "CASH_EQUIVALENT",
                         "FIXED_INCOME", "CURRENCY", "COLLECTIVE_INVESTMENT")           # REF B.4 (a subset of AssetType)
CashEquivalentType = _enum("CashEquivalentType", "SWEEP_VEHICLE", "SAVINGS", "MONEY_MARKET_FUND", "UNKNOWN")
# REF B.4 (AccountOption) has PUT, CALL, UNKNOWN; REF A.6 (OptionContract.putCall) has PUT, CALL: the union.
PutCall = _enum("PutCall", "PUT", "CALL", "UNKNOWN")
OptionType = _enum("OptionType", "VANILLA", "BINARY", "BARRIER", "UNKNOWN")
ApiCurrencyType = _enum("ApiCurrencyType", "USD", "CAD", "EUR", "JPY")
CollectiveInvestmentType = _enum("CollectiveInvestmentType", "UNIT_INVESTMENT_TRUST", "EXCHANGE_TRADED_FUND",
                                 "CLOSED_END_FUND", "INDEX", "UNITS")
TransactionEquityType = _enum(
    "TransactionEquityType", "COMMON_STOCK", "PREFERRED_STOCK", "DEPOSITORY_RECEIPT", "PREFERRED_DEPOSITORY_RECEIPT",
    "RESTRICTED_STOCK", "COMPONENT_UNIT", "RIGHT", "WARRANT", "CONVERTIBLE_PREFERRED_STOCK", "CONVERTIBLE_STOCK",
    "LIMITED_PARTNERSHIP", "WHEN_ISSUED", "UNKNOWN")
# The portal cuts this list ('...'): the values below are the ones the REF prints; others are kept as text.
TransactionFixedIncomeType = _enum(
    "TransactionFixedIncomeType", "BOND_UNIT", "CERTIFICATE_OF_DEPOSIT", "CONVERTIBLE_BOND",
    "COLLATERALIZED_MORTGAGE_OBLIGATION", "CORPORATE_BOND", "GOVERNMENT_MORTGAGE", "GNMA_BONDS",
    "MUNICIPAL_ASSESSMENT_DISTRICT", "MUNICIPAL_BOND", "OTHER_GOVERNMENT", "SHORT_TERM_PAPER", "US_TREASURY_BOND",
    "US_TREASURY_BILL", "US_TREASURY_NOTE")
ForexType = _enum("ForexType", "STANDARD", "NBBO", "UNKNOWN")
FutureType = _enum("FutureType", "STANDARD", "UNKNOWN")
IndexType = _enum("IndexType", "BROAD_BASED", "NARROW_BASED", "UNKNOWN")
MutualFundType = _enum("MutualFundType", "NOT_APPLICABLE", "OPEN_END_NON_TAXABLE", "OPEN_END_TAXABLE",
                       "NO_LOAD_NON_TAXABLE", "NO_LOAD_TAXABLE", "UNKNOWN")
ProductType = _enum("ProductType", "TBD", "UNKNOWN")
TransactionStatus = _enum("TransactionStatus", "VALID", "INVALID", "PENDING", "UNKNOWN")
SubAccount = _enum("SubAccount", "CASH", "MARGIN", "SHORT", "DIV", "INCOME", "UNKNOWN")
TransactionActivityType = _enum("TransactionActivityType", "ACTIVITY_CORRECTION", "EXECUTION", "ORDER_ACTION",
                                "TRANSFER", "UNKNOWN")
UserType = _enum("UserType", "ADVISOR_USER", "BROKER_USER", "CLIENT_USER", "SYSTEM_USER", "UNKNOWN")

# REF Appendix A and 2.11 (market data)
InstrumentAssetType = _enum("InstrumentAssetType", "BOND", "EQUITY", "ETF", "EXTENDED", "FOREX", "FUTURE",
                            "FUTURE_OPTION", "FUNDAMENTAL", "INDEX", "INDICATOR", "MUTUAL_FUND", "OPTION", "UNKNOWN")
Direction = _enum("Direction", "up", "down")
AssetMainType = _enum("AssetMainType", "BOND", "EQUITY", "FOREX", "FUTURE", "FUTURE_OPTION", "INDEX", "MUTUAL_FUND",
                      "OPTION")
EquityAssetSubType = _enum("EquityAssetSubType", "COE", "PRF", "ADR", "GDR", "CEF", "ETF", "ETN", "UIT", "WAR", "RGT", "")
MutualFundAssetSubType = _enum("MutualFundAssetSubType", "OEF", "CEF", "MMF", "")
ContractType = _enum("ContractType", "P", "C")
SettlementType = _enum("SettlementType", "A", "P")
ExpirationType = _enum("ExpirationType", "M", "Q", "S", "W")
FundStrategy = _enum("FundStrategy", "A", "L", "P", "Q", "S")
ExerciseType = _enum("ExerciseType", "A", "E")
DivFreq = _enum("DivFreq", 1, 2, 3, 4, 6, 11, 12)
QuoteType = _enum("QuoteType", "NBBO", "NFL")
UnderlyingExchangeName = _enum("UnderlyingExchangeName", "IND", "ASE", "NYS", "NAS", "NAP", "PAC", "OPR", "BATS")
ErrorStatus = _enum("ErrorStatus", "400", "401", "404", "500")

# REF 2: values of request parameters
QuoteField = _enum("QuoteField", "quote", "fundamental", "extended", "reference", "regular")
Strategy = _enum("Strategy", "SINGLE", "ANALYTICAL", "COVERED", "VERTICAL", "CALENDAR", "STRANGLE", "STRADDLE",
                 "BUTTERFLY", "CONDOR", "DIAGONAL", "COLLAR", "ROLL")
ChainContractType = _enum("ChainContractType", "CALL", "PUT", "ALL")
Range = _enum("Range", "ITM", "NTM", "OTM")                                          # the REF says 'ITM, NTM, OTM, etc.'
ExpMonth = _enum("ExpMonth", "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC", "ALL")
Entitlement = _enum("Entitlement", "PP", "NP", "PN")
PeriodType = _enum("PeriodType", "day", "month", "year", "ytd")
FrequencyType = _enum("FrequencyType", "minute", "daily", "weekly", "monthly")
MoversIndex = _enum("MoversIndex", "$DJI", "$COMPX", "$SPX", "NYSE", "NASDAQ", "OTCBB", "INDEX_ALL", "EQUITY_ALL",
                    "OPTION_ALL", "OPTION_PUT", "OPTION_CALL")
MoversSort = _enum("MoversSort", "VOLUME", "TRADES", "PERCENT_CHANGE_UP", "PERCENT_CHANGE_DOWN")
MoversFrequency = _enum("MoversFrequency", 0, 1, 5, 10, 30, 60)
Market = _enum("Market", "equity", "option", "bond", "future", "forex")
Projection = _enum("Projection", "symbol-search", "symbol-regex", "desc-search", "desc-regex", "search", "fundamental")
AccountFields = _enum("AccountFields", "positions")

# ---------------------------------------------------------------------------------------------------------- schemas

# Default type of a quote field by name (REF A.4); a token 'name:type' overrides it.
_QUOTE_FIELD_TYPES = {
    "52WeekHigh": "f", "52WeekLow": "f", "askMICId": "s", "askPrice": "f", "askSize": "i", "askTime": "i",
    "bidMICId": "s", "bidPrice": "f", "bidSize": "i", "bidTime": "i", "closePrice": "f", "highPrice": "f",
    "lastMICId": "s", "lastPrice": "f", "lastSize": "i", "lowPrice": "f", "mark": "f", "markChange": "f",
    "markPercentChange": "f", "netChange": "f", "netPercentChange": "f", "openPrice": "f", "quoteTime": "i",
    "securityStatus": "s", "totalVolume": "i", "tradeTime": "i", "volatility": "f", "tick": "f", "tickAmount": "f",
    "futurePercentChange": "f", "quotedInSession": "b", "settleTime": "i", "settlemetPrice": "f", "nAV": "f",
    "delta": "f", "gamma": "f", "theta": "f", "vega": "f", "rho": "f", "impliedYield": "f", "moneyIntrinsicValue": "f",
    "theoreticalOptionValue": "f", "timeValue": "f", "underlyingPrice": "f", "indAskPrice": "f", "indBidPrice": "f",
    "indQuoteTime": "i",
}

_RESPONSE_COMMON = "assetMainType:E:AssetMainType ssid:i symbol:s realtime:b quoteType:E:QuoteType"

_INSTRUMENT_BASE = "assetType:E:AccountAssetType cusip:s symbol:s description:s instrumentId:i netChange:f"
_TX_INSTRUMENT_BASE = "assetType:E:AssetType cusip:s symbol:s description:s instrumentId:i netChange:f"

_ORDER_COMMON = (
    "session:E:Session duration:E:Duration cancelTime:s complexOrderStrategyType:E:ComplexOrderStrategyType "
    "quantity:f filledQuantity:f remainingQuantity:f destinationLinkName:s releaseTime:s stopPrice:f "
    "stopPriceLinkBasis:E:StopPriceLinkBasis stopPriceLinkType:E:StopPriceLinkType stopPriceOffset:f stopType:E:StopType "
    "priceLinkBasis:E:PriceLinkBasis priceLinkType:E:PriceLinkType price:f taxLotMethod:E:TaxLotMethod "
    "orderLegCollection:[S:OrderLegCollection] activationPrice:f specialInstruction:E:SpecialInstruction "
    "orderStrategyType:E:OrderStrategyType orderId:i cancelable:b editable:b status:E:OrderStatus enteredTime:s "
    "closeTime:s accountNumber:i orderActivityCollection:[S:OrderActivity] statusDescription:s")

SCHEMAS: dict[str, str] = {
    # ---- A.1 instruments
    "Bond": "cusip:s symbol:s description:s exchange:s assetType:E:InstrumentAssetType bondFactor:s bondMultiplier:s "
            "bondPrice:f type:E:InstrumentAssetType",
    "Instrument": "cusip:s symbol:s description:s exchange:s assetType:E:InstrumentAssetType type:E:InstrumentAssetType",
    "InstrumentResponse": "cusip:s symbol:s description:s exchange:s assetType:E:InstrumentAssetType bondFactor:s "
                          "bondMultiplier:s bondPrice:f fundamental:S:FundamentalInst instrumentInfo:S:Instrument "
                          "bondInstrumentInfo:S:Bond type:E:InstrumentAssetType",
    "InstrumentList": "instruments:[S:InstrumentResponse]",                         # the envelope of GET /instruments
    "FundamentalInst": "symbol:s high52:f low52:f dividendAmount:f dividendYield:f dividendDate:s dividendPayAmount:f "
                       "dividendPayDate:s divGrowthRate3Year:f dividendFreq:i declarationDate:s nextDividendPayDate:s "
                       "nextDividendDate:s peRatio:f pegRatio:f pbRatio:f prRatio:f pcfRatio:f grossMarginTTM:f "
                       "grossMarginMRQ:f netProfitMarginTTM:f netProfitMarginMRQ:f operatingMarginTTM:f "
                       "operatingMarginMRQ:f returnOnEquity:f returnOnAssets:f returnOnInvestment:f quickRatio:f "
                       "currentRatio:f interestCoverage:f totalDebtToCapital:f ltDebtToEquity:f totalDebtToEquity:f "
                       "epsTTM:f epsChangePercentTTM:f epsChangeYear:f epsChange:f eps:f revChangeYear:f "
                       "revChangeTTM:f revChangeIn:f sharesOutstanding:f marketCapFloat:f marketCap:f "
                       "bookValuePerShare:f shortIntToFloat:f shortIntDayToCover:f beta:f vol1DayAvg:f vol10DayAvg:f "
                       "vol3MonthAvg:f avg10DaysVolume:i avg1DayVolume:i avg3MonthVolume:i dtnVolume:i "
                       "corpactionDate:s fundLeverageFactor:f fundStrategy:s",
    # ---- A.2 history, movers, hours
    "Candle": "close:f high:f low:f open:f volume:i datetime:i datetimeISO8601:s",
    "CandleList": "candles:[S:Candle] empty:b previousClose:f previousCloseDate:i previousCloseDateISO8601:s symbol:s",
    "Screener": "change:f description:s direction:E:Direction last:f symbol:s totalVolume:i",
    "ScreenerList": "screeners:[S:Screener]",                                         # the envelope of GET /movers
    "Interval": "start:s end:s",
    "SessionHours": "preMarket:[S:Interval] regularMarket:[S:Interval] postMarket:[S:Interval]",
    "Hours": "date:s marketType:E:InstrumentAssetType exchange:s category:s product:s productName:s isOpen:b "
             "sessionHours:S:SessionHours",
    # ---- A.3 quote responses
    "QuoteError": "invalidCusips:[s] invalidSSIDs:[i] invalidSymbols:[s]",
    "QuoteRequest": "cusips:[s] fields:s ssids:[i] symbols:[s] realtime:b indicative:b",
    "QuoteResponseObject": _RESPONSE_COMMON + " assetSubType:s",                      # fallback of an unknown assetMainType
    "EquityResponse": _RESPONSE_COMMON + " assetSubType:E:EquityAssetSubType quote:S:QuoteEquity "
                      "reference:S:ReferenceEquity extended:S:ExtendedMarket fundamental:S:Fundamental "
                      "regular:S:RegularMarket",
    "OptionResponse": _RESPONSE_COMMON + " assetSubType:s quote:S:QuoteOption reference:S:ReferenceOption",
    "ForexResponse": _RESPONSE_COMMON + " assetSubType:s quote:S:QuoteForex reference:S:ReferenceForex",
    "FutureResponse": _RESPONSE_COMMON + " assetSubType:s quote:S:QuoteFuture reference:S:ReferenceFuture",
    "FutureOptionResponse": _RESPONSE_COMMON + " assetSubType:s quote:S:QuoteFutureOption "
                            "reference:S:ReferenceFutureOption",
    "IndexResponse": _RESPONSE_COMMON + " assetSubType:s quote:S:QuoteIndex reference:S:ReferenceIndex",
    "MutualFundResponse": _RESPONSE_COMMON + " assetSubType:E:MutualFundAssetSubType quote:S:QuoteMutualFund "
                          "reference:S:ReferenceMutualFund fundamental:S:Fundamental",
    # ---- A.4 quote nodes
    "QuoteEquity": "52WeekHigh 52WeekLow askMICId askPrice askSize askTime bidMICId bidPrice bidSize bidTime closePrice "
                   "highPrice lastMICId lastPrice lastSize lowPrice mark markChange markPercentChange netChange "
                   "netPercentChange openPrice quoteTime securityStatus totalVolume tradeTime volatility",
    "QuoteForex": "52WeekHigh 52WeekLow askPrice askSize bidPrice bidSize closePrice highPrice lastPrice lastSize "
                  "lowPrice mark netChange netPercentChange openPrice quoteTime securityStatus tick tickAmount "
                  "totalVolume tradeTime",
    "QuoteFuture": "askMICId askPrice askSize askTime bidMICId bidPrice bidSize bidTime closePrice futurePercentChange "
                   "highPrice lastMICId lastPrice lastSize lowPrice mark netChange openInterest:i openPrice quoteTime "
                   "quotedInSession securityStatus settleTime tick tickAmount totalVolume tradeTime",
    "QuoteFutureOption": "askMICId askPrice askSize bidMICId bidPrice bidSize closePrice highPrice lastMICId lastPrice "
                         "lastSize lowPrice mark markChange netChange netPercentChange openInterest:i openPrice "
                         "quoteTime securityStatus settlemetPrice tick tickAmount totalVolume tradeTime",
    "QuoteIndex": "52WeekHigh 52WeekLow closePrice highPrice lastPrice lowPrice netChange netPercentChange openPrice "
                  "securityStatus totalVolume tradeTime",
    "QuoteMutualFund": "52WeekHigh 52WeekLow closePrice nAV netChange netPercentChange securityStatus totalVolume "
                       "tradeTime",
    "QuoteOption": "52WeekHigh 52WeekLow askPrice askSize bidPrice bidSize closePrice highPrice lastPrice lastSize "
                   "lowPrice mark markChange markPercentChange netChange netPercentChange openInterest:f openPrice "
                   "quoteTime securityStatus totalVolume tradeTime volatility delta gamma theta vega rho impliedYield "
                   "moneyIntrinsicValue theoreticalOptionValue timeValue underlyingPrice indAskPrice indBidPrice "
                   "indQuoteTime",
    "ExtendedMarket": "askPrice:f askSize:i bidPrice:f bidSize:i lastPrice:f lastSize:i mark:f quoteTime:i "
                      "totalVolume:i tradeTime:i",
    "RegularMarket": "regularMarketLastPrice:f regularMarketLastSize:i regularMarketNetChange:f "
                     "regularMarketPercentChange:f regularMarketTradeTime:i",
    "Fundamental": "avg10DaysVolume:f avg1YearVolume:f declarationDate:s divAmount:f divExDate:s "
                   "divFreq:E:DivFreq divPayAmount:f divPayDate:s divYield:f eps:f fundLeverageFactor:f "
                   "fundStrategy:E:FundStrategy nextDivExDate:s nextDivPayDate:s peRatio:f",
    # ---- A.5 reference nodes
    "ReferenceEquity": "cusip:s description:s exchange:s exchangeName:s fsiDesc:s htbQuantity:i htbRate:f "
                       "isHardToBorrow:b isShortable:b otcMarketTier:s",
    "ReferenceForex": "description:s exchange:s exchangeName:s isTradable:b marketMaker:s product:s tradingHours:s",
    "ReferenceFuture": "description:s exchange:s exchangeName:s futureActiveSymbol:s futureExpirationDate:i "
                       "futureIsActive:b futureIsTradable:b futureMultiplier:f futurePriceFormat:s "
                       "futureSettlementPrice:f futureTradingHours:s product:s",
    "ReferenceFutureOption": "contractType:E:ContractType description:s exchange:s exchangeName:s multiplier:f "
                             "expirationDate:i expirationStyle:s strikePrice:f underlying:s",
    "ReferenceIndex": "description:s exchange:s exchangeName:s",
    "ReferenceMutualFund": "cusip:s description:s exchange:s exchangeName:s",
    "ReferenceOption": "contractType:E:ContractType cusip:s daysToExpiration:i deliverables:s description:s exchange:s "
                       "exchangeName:s exerciseType:E:ExerciseType expirationDay:i expirationMonth:i "
                       "expirationType:E:ExpirationType expirationYear:i isPennyPilot:b lastTradingDay:i multiplier:f "
                       "settlementType:E:SettlementType strikePrice:f underlying:s uvExpirationType:s",
    # ---- A.6 option chain and expirations
    "OptionChain": "symbol:s status:s underlying:S:Underlying strategy:E:Strategy interval:f isDelayed:b isIndex:b "
                   "daysToExpiration:f interestRate:f underlyingPrice:f volatility:f "
                   "callExpDateMap:{{[S:OptionContract]}} putExpDateMap:{{[S:OptionContract]}}",
    "Underlying": "ask:f bid:f change:f close:f askSize:i bidSize:i delayed:b description:s "
                  "exchangeName:E:UnderlyingExchangeName fiftyTwoWeekHigh:f fiftyTwoWeekLow:f highPrice:f last:f "
                  "lowPrice:f mark:f markChange:f markPercentChange:f openPrice:f percentChange:f quoteTime:i "
                  "tradeTime:i totalVolume:i symbol:s",
    "OptionDeliverables": "symbol:s assetType:s deliverableUnits:s currencyType:s",
    "OptionContract": "putCall:E:PutCall symbol:s description:s exchangeName:s bidPrice:f askPrice:f lastPrice:f "
                      "markPrice:f bidSize:i askSize:i lastSize:i highPrice:f lowPrice:f openPrice:f closePrice:f "
                      "totalVolume:i tradeDate:i quoteTimeInLong:i tradeTimeInLong:i netChange:f volatility:f "
                      "delta:f gamma:f theta:f vega:f rho:f timeValue:f openInterest:f isInTheMoney:b "
                      "theoreticalOptionValue:f theoreticalVolatility:f isMini:b isNonStandard:b "
                      "optionDeliverablesList:[S:OptionDeliverables] strikePrice:f expirationDate:s daysToExpiration:i "
                      "expirationType:E:ExpirationType lastTradingDay:i multiplier:f settlementType:E:SettlementType "
                      "deliverableNote:s isIndexOption:b percentChange:f markChange:f markPercentChange:f "
                      "isPennyPilot:b intrinsicValue:f optionRoot:s",
    "ExpirationChain": "status:s expirationList:[S:Expiration]",
    # The schema says 'expiration', the REF 2.4 example says 'expirationDate': both are read.
    "Expiration": "daysToExpiration:i expiration:s expirationDate:s expirationType:E:ExpirationType standard:b "
                  "settlementType:E:SettlementType optionRoots:s",
    # ---- A.7 errors
    "ErrorSource": "pointer:[s] parameter:s header:s",
    "Error": "id:s status:E:ErrorStatus title:s detail:s source:S:ErrorSource",
    "ErrorResponse": "errors:[S:Error]",
    # ---- B.1 orders
    "AccountNumberHash": "accountNumber:s hashValue:s",
    "Order": _ORDER_COMMON + " orderType:E:OrderType requestedDestination:E:RequestedDestination tag:s "
             "replacingOrderCollection:[S:Order] childOrderStrategies:[S:Order]",
    "OrderRequest": _ORDER_COMMON + " orderType:E:OrderTypeRequest replacingOrderCollection:[S:OrderRequest] "
                    "childOrderStrategies:[S:OrderRequest]",
    "OrderLegCollection": "orderLegType:E:OrderLegType legId:i instrument:O:AccountsInstrument "
                          "instruction:E:Instruction positionEffect:E:PositionEffect quantity:f "
                          "quantityType:E:QuantityType divCapGains:E:DivCapGains toSymbol:s",
    "OrderActivity": "activityType:E:OrderActivityType executionType:E:ExecutionType quantity:f "
                     "orderRemainingQuantity:f executionLegs:[S:ExecutionLeg]",
    "ExecutionLeg": "legId:i price:f quantity:f mismarkedQuantity:f instrumentId:i time:s",
    "DateParam": "date:s",
    "ServiceError": "message:s errors:[s]",
    # ---- B.2 preview
    "PreviewOrder": "orderId:i orderStrategy:S:OrderStrategy orderValidationResult:S:OrderValidationResult "
                    "commissionAndFee:S:CommissionAndFee",
    "OrderStrategy": "accountNumber:s advancedOrderType:E:AdvancedOrderType closeTime:s enteredTime:s "
                     "orderBalance:S:OrderBalance orderStrategyType:E:OrderStrategyType orderVersion:f "
                     "session:E:Session status:E:OrderStatus allOrNone:b discretionary:b duration:E:Duration "
                     "filledQuantity:f orderType:E:OrderType orderValue:f price:f quantity:f remainingQuantity:f "
                     "sellNonMarginableFirst:b settlementInstruction:E:SettlementInstruction "
                     "strategy:E:ComplexOrderStrategyType amountIndicator:E:AmountIndicator orderLegs:[S:OrderLeg]",
    "OrderLeg": "askPrice:f bidPrice:f lastPrice:f markPrice:f projectedCommission:f quantity:f finalSymbol:s legId:i "
                "assetType:E:AssetType instruction:E:Instruction",
    "OrderBalance": "orderValue:f projectedAvailableFund:f projectedBuyingPower:f projectedCommission:f",
    "OrderValidationResult": "alerts:[S:OrderValidationDetail] accepts:[S:OrderValidationDetail] "
                             "rejects:[S:OrderValidationDetail] reviews:[S:OrderValidationDetail] "
                             "warns:[S:OrderValidationDetail]",
    "OrderValidationDetail": "validationRuleName:s message:s activityMessage:s originalSeverity:E:APIRuleAction "
                             "overrideName:s overrideSeverity:E:APIRuleAction",
    "CommissionAndFee": "commission:S:Commission fee:S:Fees trueCommission:S:Commission",
    "Commission": "commissionLegs:[S:CommissionLeg]",
    "CommissionLeg": "commissionValues:[S:CommissionValue]",
    "CommissionValue": "value:f type:E:FeeType",
    "Fees": "feeLegs:[S:FeeLeg]",
    "FeeLeg": "feeValues:[S:FeeValue]",
    "FeeValue": "value:f type:E:FeeType",
    # ---- B.3 accounts and positions
    "Account": "securitiesAccount:O:SecuritiesAccount",
    "SecuritiesAccountBase": "type:E:AccountType accountNumber:s roundTrips:i isDayTrader:b isClosingOnlyRestricted:b "
                             "pfcbFlag:b positions:[S:Position]",                      # fallback of an unknown type
    "MarginAccount": "+SecuritiesAccountBase initialBalances:S:MarginInitialBalance currentBalances:S:MarginBalance "
                     "projectedBalances:S:MarginBalance",
    "CashAccount": "+SecuritiesAccountBase initialBalances:S:CashInitialBalance currentBalances:S:CashBalance "
                   "projectedBalances:S:CashBalance",
    "Position": "shortQuantity:f averagePrice:f currentDayProfitLoss:f currentDayProfitLossPercentage:f longQuantity:f "
                "settledLongQuantity:f settledShortQuantity:f agedQuantity:f instrument:O:AccountsInstrument "
                "marketValue:f maintenanceRequirement:f averageLongPrice:f averageShortPrice:f "
                "taxLotAverageLongPrice:f taxLotAverageShortPrice:f longOpenProfitLoss:f shortOpenProfitLoss:f "
                "previousSessionLongQuantity:f previousSessionShortQuantity:f currentDayCost:f",
    "MarginInitialBalance": "accruedInterest:f availableFundsNonMarginableTrade:f bondValue:f buyingPower:f "
                            "cashBalance:f cashAvailableForTrading:f cashReceipts:f dayTradingBuyingPower:f "
                            "dayTradingBuyingPowerCall:f dayTradingEquityCall:f equity:f equityPercentage:f "
                            "liquidationValue:f longMarginValue:f longOptionMarketValue:f longStockValue:f "
                            "maintenanceCall:f maintenanceRequirement:f margin:f marginEquity:f moneyMarketFund:f "
                            "mutualFundValue:f regTCall:f shortMarginValue:f shortOptionMarketValue:f "
                            "shortStockValue:f totalCash:f isInCall:b unsettledCash:f pendingDeposits:f "
                            "marginBalance:f shortBalance:f accountValue:f",
    "MarginBalance": "availableFunds:f availableFundsNonMarginableTrade:f buyingPower:f "
                     "buyingPowerNonMarginableTrade:f dayTradingBuyingPower:f dayTradingBuyingPowerCall:f equity:f "
                     "equityPercentage:f longMarginValue:f maintenanceCall:f maintenanceRequirement:f "
                     "marginBalance:f regTCall:f shortBalance:f shortMarginValue:f sma:f isInCall:b "
                     "stockBuyingPower:f optionBuyingPower:f",
    "CashInitialBalance": "accruedInterest:f cashAvailableForTrading:f cashAvailableForWithdrawal:f cashBalance:f "
                          "bondValue:f cashReceipts:f liquidationValue:f longOptionMarketValue:f longStockValue:f "
                          "moneyMarketFund:f mutualFundValue:f shortOptionMarketValue:f shortStockValue:f isInCall:b "
                          "unsettledCash:f cashDebitCallValue:f pendingDeposits:f accountValue:f",
    "CashBalance": "cashAvailableForTrading:f cashAvailableForWithdrawal:f cashCall:f longNonMarginableMarketValue:f "
                   "totalCash:f cashDebitCallValue:f unsettledCash:f",
    # ---- B.4 instruments in accounts and orders
    "AccountsBaseInstrument": _INSTRUMENT_BASE,
    "AccountEquity": _INSTRUMENT_BASE,
    "AccountMutualFund": _INSTRUMENT_BASE,
    "AccountCashEquivalent": _INSTRUMENT_BASE + " type:E:CashEquivalentType",
    "AccountFixedIncome": _INSTRUMENT_BASE + " maturityDate:s factor:f variableRate:f",
    "AccountOption": _INSTRUMENT_BASE + " optionDeliverables:[S:AccountAPIOptionDeliverable] putCall:E:PutCall "
                     "optionMultiplier:i type:E:OptionType underlyingSymbol:s",
    "AccountAPIOptionDeliverable": "symbol:s deliverableUnits:f apiCurrencyType:E:ApiCurrencyType assetType:E:AssetType",
    # ---- B.5 instruments in transactions
    "TransactionBaseInstrument": _TX_INSTRUMENT_BASE,
    "Currency": _TX_INSTRUMENT_BASE,
    "TransactionCashEquivalent": _TX_INSTRUMENT_BASE + " type:E:CashEquivalentType",
    "CollectiveInvestment": _TX_INSTRUMENT_BASE + " type:E:CollectiveInvestmentType",
    "TransactionEquity": _TX_INSTRUMENT_BASE + " type:E:TransactionEquityType",
    "TransactionFixedIncome": _TX_INSTRUMENT_BASE + " maturityDate:s factor:f multiplier:f variableRate:f "
                              "type:E:TransactionFixedIncomeType",
    "Forex": _TX_INSTRUMENT_BASE + " baseCurrency:S:Currency counterCurrency:S:Currency type:E:ForexType",
    "Future": _TX_INSTRUMENT_BASE + " activeContract:b expirationDate:s lastTradingDate:s firstNoticeDate:s "
              "multiplier:f type:E:FutureType",
    "Index": _TX_INSTRUMENT_BASE + " activeContract:b type:E:IndexType",
    "TransactionMutualFund": _TX_INSTRUMENT_BASE + " fundFamilyName:s fundFamilySymbol:s fundGroup:s "
                             "exchangeCutoffTime:s purchaseCutoffTime:s redemptionCutoffTime:s type:E:MutualFundType",
    "TransactionOption": _TX_INSTRUMENT_BASE + " expirationDate:s optionDeliverables:[S:TransactionAPIOptionDeliverable] "
                         "optionPremiumMultiplier:i putCall:E:PutCall strikePrice:f underlyingSymbol:s "
                         "underlyingCusip:s deliverable:O:TransactionInstrument type:E:OptionType",
    "Product": _TX_INSTRUMENT_BASE + " type:E:ProductType",
    "TransactionAPIOptionDeliverable": "rootSymbol:s strikePercent:i deliverableNumber:i deliverableUnits:f "
                                       "deliverable:O:TransactionInstrument assetType:E:AssetType",
    # ---- B.6 transactions and user
    "Transaction": "activityId:i time:s user:S:UserDetails description:s accountNumber:s type:E:TransactionType "
                   "status:E:TransactionStatus subAccount:E:SubAccount tradeDate:s settlementDate:s positionId:i "
                   "orderId:i netAmount:f activityType:E:TransactionActivityType transferItems:[S:TransferItem]",
    "UserDetails": "cdDomainId:s login:s type:E:UserType userId:i systemUserName:s firstName:s lastName:s "
                   "brokerRepCode:s",
    "TransferItem": "instrument:O:TransactionInstrument amount:f cost:f price:f feeType:E:FeeType "
                    "positionEffect:E:PositionEffect",
    "UserPreference": "accounts:[S:UserPreferenceAccount] streamerInfo:[S:StreamerInfo] offers:[S:Offer]",
    "UserPreferenceAccount": "accountNumber:s type:s nickName:s displayAcctId:s primaryAccount:b accountColor:s "
                             "autoPositionEffect:b",
    "StreamerInfo": "streamerSocketUrl:s schwabClientCustomerId:s schwabClientCorrelId:s schwabClientChannel:s "
                    "schwabClientFunctionId:s",
    "Offer": "level2Permissions:b mktDataPermission:s",
}

# ---------------------------------------------------------------------------------------------------- reading

MODELS: dict[str, type] = {}
_ATTR = {"52WeekHigh": "week52High", "52WeekLow": "week52Low"}                      # not valid Python names
_WARNED: set[tuple[str, str]] = set()


def clear_warnings() -> None:
    """Tests only: let the 'unknown enum value' warning be logged again."""
    _WARNED.clear()


@dataclass(kw_only=True, repr=False)
class Model:
    """Base of every schema: 'extra' keeps the keys the schema does not have. Reading: 'Cls.read(json)'."""
    extra: dict = field(default_factory=dict)

    SCHEMA: ClassVar[str] = ""
    _by_json: ClassVar[dict] = {}

    def __repr__(self) -> str:                                                    # never print values (account numbers)
        return f"{type(self).__name__}(...)"

    @classmethod
    def read(cls, data: Any):
        """The object for a decoded JSON dict (None for anything that is not a dict)."""
        if not isinstance(data, dict):
            return None
        values: dict = {}
        extra: dict = {}
        for key, value in data.items():
            known = cls._by_json.get(key)
            if known is None:
                extra[key] = value
            else:
                values[known[0]] = known[1](value)
        return cls(extra=extra, **values)

    def to_json(self) -> dict:
        return dump(self)


def _warn_enum(enum: str, where: str) -> None:
    mark = (enum, where)
    if mark not in _WARNED:
        _WARNED.add(mark)
        log.warning("schwab model: value outside enum %s in %s (kept as read)", enum, where)


def _compile(tp: str, where: str):
    """The function that reads one value of type 'tp'."""
    if tp[0] == "[":
        inner = _compile(tp[1:-1], where)

        def read_list(value):
            if isinstance(value, list):
                return [inner(item) for item in value]
            return inner(value)                                                   # a single object where a list was expected
        return read_list
    if tp[0] == "{":
        inner = _compile(tp[1:-1], where)

        def read_map(value):
            if isinstance(value, dict):
                return {key: inner(item) for key, item in value.items()}
            return value
        return read_map
    if tp == "f":
        return lambda v: float(v) if isinstance(v, int) and not isinstance(v, bool) else v
    if tp == "i":
        return lambda v: int(v) if isinstance(v, float) and v.is_integer() else v
    if tp in ("s", "b", "any"):
        return lambda v: v
    kind, _, name = tp.partition(":")
    if kind == "E":
        allowed = frozenset(enum_values(name))

        def read_enum(value):
            if value is None or (isinstance(value, (str, int)) and value in allowed):
                return value
            _warn_enum(name, where)
            return value
        return read_enum
    if kind == "S":
        return lambda v: MODELS[name].read(v) if isinstance(v, dict) else v
    if kind == "O":
        return lambda v: resolve(name, v).read(v) if isinstance(v, dict) else v
    raise ValueError(f"bad type {tp!r} in {where}")


def _tokens(name: str) -> list[str]:
    out: list[str] = []
    for token in SCHEMAS[name].split():
        out.extend(_tokens(token[1:]) if token.startswith("+") else [token])
    return out


def _build(name: str) -> type:
    attrs, by_json, fields_ = [], {}, []
    for token in _tokens(name):
        json_name, _, tp = token.partition(":")
        tp = tp or _QUOTE_FIELD_TYPES[json_name]
        attr = _ATTR.get(json_name, json_name)
        if not attr.isidentifier() or keyword.iskeyword(attr):
            raise ValueError(f"{name}.{json_name} is not a Python name")
        by_json[json_name] = (attr, _compile(tp, f"{name}.{json_name}"))
        fields_.append((attr, Any, field(default=None)))
        attrs.append(attr)
    cls = make_dataclass(name, fields_, bases=(Model,), kw_only=True, repr=False, namespace={"__module__": __name__})
    cls.SCHEMA = name
    cls._by_json = by_json
    return cls


for _name in SCHEMAS:
    MODELS[_name] = globals()[_name] = _build(_name)


def fields_of(name: str) -> tuple[str, ...]:
    """The JSON field names of a schema, in the order of the REF."""
    return tuple(MODELS[name]._by_json)


# ------------------------------------------------------------------------------------------------------- oneOf

_RESPONSE_BY_MAIN_TYPE = {
    "EQUITY": "EquityResponse", "OPTION": "OptionResponse", "FOREX": "ForexResponse", "FUTURE": "FutureResponse",
    "FUTURE_OPTION": "FutureOptionResponse", "INDEX": "IndexResponse", "MUTUAL_FUND": "MutualFundResponse",
}
_ACCOUNT_INSTRUMENT = {
    "EQUITY": "AccountEquity", "OPTION": "AccountOption", "MUTUAL_FUND": "AccountMutualFund",
    "FIXED_INCOME": "AccountFixedIncome", "CASH_EQUIVALENT": "AccountCashEquivalent",
}                                  # INDEX, CURRENCY and COLLECTIVE_INVESTMENT take the base class
_TX_INSTRUMENT = {
    "EQUITY": "TransactionEquity", "OPTION": "TransactionOption", "MUTUAL_FUND": "TransactionMutualFund",
    "FIXED_INCOME": "TransactionFixedIncome", "CASH_EQUIVALENT": "TransactionCashEquivalent",
    "COLLECTIVE_INVESTMENT": "CollectiveInvestment", "CURRENCY": "Currency", "INDEX": "Index", "FOREX": "Forex",
    "FUTURE": "Future", "PRODUCT": "Product",
}


def resolve(name: str, data: dict) -> type:
    """The class a oneOf schema takes for this JSON, by its discriminator."""
    if name == "QuoteResponseObject":
        return MODELS[_RESPONSE_BY_MAIN_TYPE.get(data.get("assetMainType"), "QuoteResponseObject")]
    if name == "SecuritiesAccount":
        kind = data.get("type")
        if kind not in ("MARGIN", "CASH"):                                         # no discriminator: look at the balances
            current = data.get("currentBalances")
            current = current if isinstance(current, dict) else {}
            kind = "MARGIN" if ("availableFunds" in current or "buyingPower" in current) else (
                "CASH" if "cashAvailableForTrading" in current else None)
        return MODELS[{"MARGIN": "MarginAccount", "CASH": "CashAccount"}.get(kind, "SecuritiesAccountBase")]
    if name == "AccountsInstrument":
        kind = _ACCOUNT_INSTRUMENT.get(data.get("assetType"))
        if kind is None and data.get("assetType") is None:                         # the REF example has 'type' only
            kind = _ACCOUNT_INSTRUMENT.get(_account_kind_by_shape(data))
        return MODELS[kind or "AccountsBaseInstrument"]
    if name == "TransactionInstrument":
        return MODELS[_TX_INSTRUMENT.get(data.get("assetType"), "TransactionBaseInstrument")]
    raise KeyError(name)


def _account_kind_by_shape(data: dict) -> str | None:
    kind = data.get("type")
    if kind in ("SWEEP_VEHICLE", "SAVINGS", "MONEY_MARKET_FUND"):
        return "CASH_EQUIVALENT"
    if kind in ("VANILLA", "BINARY", "BARRIER") or "putCall" in data or "optionDeliverables" in data:
        return "OPTION"
    if "maturityDate" in data or "variableRate" in data or "factor" in data:
        return "FIXED_INCOME"
    return None


# --------------------------------------------------------------------------------------------- quote response


class QuoteResponse:
    """Answer of GET /quotes: a map symbol to the response object of its assetMainType, plus 'errors' (QuoteError).
    'quotes' holds the symbols; 'errors' the QuoteError or None; 'extra' any other top-level key."""

    SCHEMA = "QuoteResponse"

    def __init__(self, quotes: dict | None = None, errors: Any = None, extra: dict | None = None) -> None:
        self.quotes: dict[str, Any] = quotes or {}
        self.errors = errors
        self.extra: dict = extra or {}

    @classmethod
    def read(cls, data: Any) -> "QuoteResponse | None":
        if not isinstance(data, dict):
            return None
        quotes, extra, errors = {}, {}, None
        for key, value in data.items():
            if key == "errors" and isinstance(value, dict):
                errors = MODELS["QuoteError"].read(value)
            elif isinstance(value, dict) and ("assetMainType" in value or "quote" in value or "symbol" in value
                                              or "reference" in value):
                quotes[key] = read_quote_object(value)
            else:
                extra[key] = value
        return cls(quotes, errors, extra)

    def __getitem__(self, symbol: str):
        return self.quotes[symbol]

    def __contains__(self, symbol: object) -> bool:
        return symbol in self.quotes

    def __iter__(self):
        return iter(self.quotes)

    def __len__(self) -> int:
        return len(self.quotes)

    def get(self, symbol: str, default: Any = None):
        return self.quotes.get(symbol, default)

    def items(self):
        return self.quotes.items()

    def __repr__(self) -> str:
        return f"QuoteResponse({len(self.quotes)} symbols)"

    def to_json(self) -> dict:
        out: dict = {symbol: dump(obj) for symbol, obj in self.quotes.items()}
        if self.errors is not None:
            out["errors"] = dump(self.errors)
        out.update(dump(self.extra))
        return out


def read_quote_object(data: Any):
    """One QuoteResponseObject (the oneOf of 7), by assetMainType."""
    return resolve("QuoteResponseObject", data).read(data) if isinstance(data, dict) else None


# ------------------------------------------------------------------------------------------------------- dump


def dump(obj: Any) -> Any:
    """The whole JSON of an object of this module (extra included), made only of dict, list and scalars."""
    if isinstance(obj, Model):
        out: dict = {}
        for json_name, (attr, _) in type(obj)._by_json.items():
            value = getattr(obj, attr)
            if value is not None:
                out[json_name] = dump(value)
        for key, value in obj.extra.items():
            out[key] = dump(value)
        return out
    if isinstance(obj, QuoteResponse):
        return obj.to_json()
    if isinstance(obj, (list, tuple)):
        return [dump(item) for item in obj]
    if isinstance(obj, dict):
        return {key: dump(value) for key, value in obj.items()}
    return obj

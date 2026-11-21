"""A fake Schwab for the SPEC-015 tests: a local HTTP server that answers by (method, path) with the JSON a test gives it,
records every request (method, path, query, headers) and wires `hokma.schwab.http` to it. Nothing touches the network.

    with FakeSchwab() as fake:
        fake.on("GET", "/marketdata/v1/quotes", {"AAPL": {...}})            # a body, or a callable(request) -> (status, body)
        http.get("/marketdata/v1/quotes", {"symbols": "AAPL"})
        fake.calls                                                         # [Call(method, path, query, headers, body)]

The access token is `FAKE_TOKEN`; the paths are the real ones (`/trader/v1/accounts/{hash}/orders`), the hash is `FAKE_HASH`.
Fixtures (answers built from the schemas of REF-schwab-trader-api.md) live in `schwab_fixtures/`.
"""
import json
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hokma.schwab import http

FIXTURES = Path(__file__).parent / "schwab_fixtures"
FAKE_TOKEN = "fake-access-token-0123456789"
FAKE_HASH = "A1B2C3D4E5F60718293A4B5C6D7E8F90"           # a made-up hash; the real format is not published
FAKE_ACCOUNT_NUMBER = "12345678"


def load(name: str):
    """A fixture of `schwab_fixtures/`."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass
class Call:
    method: str
    path: str
    query: dict
    headers: dict
    body: bytes


class FakeSchwab:
    def __init__(self, token: str = FAKE_TOKEN) -> None:
        self.token = token
        self.calls: list[Call] = []
        self._routes: dict[tuple[str, str], object] = {}
        self._server: ThreadingHTTPServer | None = None

    # ---- routes ----
    def on(self, method: str, path: str, answer) -> "FakeSchwab":
        """`answer` is a JSON-able body (status 200), a `(status, body)` pair, or a callable(call) returning either."""
        self._routes[(method.upper(), path)] = answer
        return self

    def paths(self, method: str | None = None) -> list[str]:
        return [c.path for c in self.calls if method is None or c.method == method.upper()]

    # ---- life ----
    def __enter__(self) -> "FakeSchwab":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _serve(self) -> None:
                parts = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                call = Call(self.command, parts.path, dict(urllib.parse.parse_qsl(parts.query)),
                            {k.lower(): v for k, v in self.headers.items()}, body)
                fake.calls.append(call)
                answer = fake._routes.get((self.command, parts.path))
                if callable(answer):
                    answer = answer(call)
                if answer is None:
                    status, payload = 404, {"errors": [{"status": "404", "title": "Not Found"}]}
                elif isinstance(answer, tuple):
                    status, payload = answer
                else:
                    status, payload = 200, answer
                raw = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Schwab-Client-CorrelId", "test-correl-id")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        http.set_base(f"http://127.0.0.1:{self._server.server_address[1]}")
        http.set_token_provider(lambda: self.token, lambda: self.token)
        http.reset_limits()
        return self

    def __exit__(self, *exc) -> None:
        http.set_base(None)
        http.set_token_provider(None)
        http.reset_limits()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


# ------------------------------------------------------------------------------------- helpers of the data tests (T4 and T5)
FAKE_ACCOUNT_NUMBER_2 = "87654321"
FAKE_HASH_2 = "F0E1D2C3B4A5968778695A4B3C2D1E0F"


def register_accounts(fake: "FakeSchwab") -> "FakeSchwab":
    """The two made-up accounts of 'account_numbers.json' (A1: FAKE_ACCOUNT_NUMBER and FAKE_HASH; A2: the second pair)."""
    return fake.on("GET", "/trader/v1/accounts/accountNumbers", load("account_numbers.json"))


def clear_fake(fake: "FakeSchwab") -> None:
    """Forget the routes and the calls of a running fake (a module scoped fake serves many tests; stopping a server takes
    half a second)."""
    fake.calls.clear()
    fake._routes.clear()


def attr_name(json_name: str) -> str:
    """The attribute of a model for a JSON field ('52WeekHigh' is 'week52High')."""
    from hokma.schwab import models

    return models._ATTR.get(json_name, json_name)


def assert_full(obj, field_names: str) -> None:
    """Every field of the list (space separated, the names of the REF) is present in the object, none lost in 'extra'."""
    missing = [name for name in field_names.split() if getattr(obj, attr_name(name), None) is None]
    assert not missing, f"{type(obj).__name__} lost {missing}"


def extras(obj, where: str = "root") -> list:
    """Every 'extra' that is not empty in an object read by models: [(path, keys)]. A complete read gives []."""
    from hokma.schwab import models

    found: list = []
    if isinstance(obj, models.Model):
        if obj.extra:
            found.append((where, sorted(obj.extra)))
        for json_name, (attr, _) in type(obj)._by_json.items():
            found += extras(getattr(obj, attr), f"{where}.{json_name}")
    elif isinstance(obj, models.QuoteResponse):
        if obj.extra:
            found.append((where, sorted(obj.extra)))
        found += extras(obj.errors, f"{where}.errors")
        for symbol, item in obj.quotes.items():
            found += extras(item, f"{where}[{symbol}]")
    elif isinstance(obj, (list, tuple)):
        for index, item in enumerate(obj):
            found += extras(item, f"{where}[{index}]")
    elif isinstance(obj, dict):
        for key, item in obj.items():
            found += extras(item, f"{where}[{key}]")
    return found


# ---- what the REF says, typed from REF-schwab-trader-api.md (sections 2.11, 3.1, 3.5 and Appendices A and B)
REF_ENUMS = {
    "AssetMainType": ("BOND", "EQUITY", "FOREX", "FUTURE", "FUTURE_OPTION", "INDEX", "MUTUAL_FUND", "OPTION"),
    "EquityAssetSubType": ("COE", "PRF", "ADR", "GDR", "CEF", "ETF", "ETN", "UIT", "WAR", "RGT", ""),
    "MutualFundAssetSubType": ("OEF", "CEF", "MMF", ""),
    "ContractType": ("P", "C"),
    "SettlementType": ("A", "P"),
    "ExpirationType": ("M", "Q", "S", "W"),
    "FundStrategy": ("A", "L", "P", "Q", "S"),
    "ExerciseType": ("A", "E"),
    "DivFreq": (1, 2, 3, 4, 6, 11, 12),
    "QuoteType": ("NBBO", "NFL"),
    "Session": ("NORMAL", "AM", "PM", "SEAMLESS"),
    "Duration": ("DAY", "GOOD_TILL_CANCEL", "FILL_OR_KILL", "IMMEDIATE_OR_CANCEL", "END_OF_WEEK", "END_OF_MONTH", "NEXT_END_OF_MONTH", "UNKNOWN"),
    "OrderType": ("MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP", "CABINET", "NON_MARKETABLE", "MARKET_ON_CLOSE", "EXERCISE", "TRAILING_STOP_LIMIT", "NET_DEBIT", "NET_CREDIT", "NET_ZERO", "LIMIT_ON_CLOSE", "UNKNOWN"),
    "ComplexOrderStrategyType": ("NONE", "COVERED", "VERTICAL", "BACK_RATIO", "CALENDAR", "DIAGONAL", "STRADDLE", "STRANGLE", "COLLAR_SYNTHETIC", "BUTTERFLY", "CONDOR", "IRON_CONDOR", "VERTICAL_ROLL", "COLLAR_WITH_STOCK", "DOUBLE_DIAGONAL", "UNBALANCED_BUTTERFLY", "UNBALANCED_CONDOR", "UNBALANCED_IRON_CONDOR", "UNBALANCED_VERTICAL_ROLL", "MUTUAL_FUND_SWAP", "CUSTOM"),
    "RequestedDestination": ("INET", "ECN_ARCA", "CBOE", "AMEX", "PHLX", "ISE", "BOX", "NYSE", "NASDAQ", "BATS", "C2", "AUTO"),
    "StopPriceLinkBasis": ("MANUAL", "BASE", "TRIGGER", "LAST", "BID", "ASK", "ASK_BID", "MARK", "AVERAGE"),
    "PriceLinkBasis": ("MANUAL", "BASE", "TRIGGER", "LAST", "BID", "ASK", "ASK_BID", "MARK", "AVERAGE"),
    "StopPriceLinkType": ("VALUE", "PERCENT", "TICK"),
    "PriceLinkType": ("VALUE", "PERCENT", "TICK"),
    "StopType": ("STANDARD", "BID", "ASK", "LAST", "MARK"),
    "TaxLotMethod": ("FIFO", "LIFO", "HIGH_COST", "LOW_COST", "AVERAGE_COST", "SPECIFIC_LOT", "LOSS_HARVESTER"),
    "SpecialInstruction": ("ALL_OR_NONE", "DO_NOT_REDUCE", "ALL_OR_NONE_DO_NOT_REDUCE"),
    "OrderStrategyType": ("SINGLE", "CANCEL", "RECALL", "PAIR", "FLATTEN", "TWO_DAY_SWAP", "BLAST_ALL", "OCO", "TRIGGER"),
    "OrderStatus": ("AWAITING_PARENT_ORDER", "AWAITING_CONDITION", "AWAITING_STOP_CONDITION", "AWAITING_MANUAL_REVIEW", "ACCEPTED", "AWAITING_UR_OUT", "PENDING_ACTIVATION", "QUEUED", "WORKING", "REJECTED", "PENDING_CANCEL", "CANCELED", "PENDING_REPLACE", "REPLACED", "FILLED", "EXPIRED", "NEW", "AWAITING_RELEASE_TIME", "PENDING_ACKNOWLEDGEMENT", "PENDING_RECALL", "UNKNOWN"),
    "AmountIndicator": ("DOLLARS", "SHARES", "ALL_SHARES", "PERCENTAGE", "UNKNOWN"),
    "SettlementInstruction": ("REGULAR", "CASH", "NEXT_DAY", "UNKNOWN"),
    "APIRuleAction": ("ACCEPT", "ALERT", "REJECT", "REVIEW", "UNKNOWN"),
    "Instruction": ("BUY", "SELL", "BUY_TO_COVER", "SELL_SHORT", "BUY_TO_OPEN", "BUY_TO_CLOSE", "SELL_TO_OPEN", "SELL_TO_CLOSE", "EXCHANGE", "SELL_SHORT_EXEMPT"),
    "AssetType": ("EQUITY", "MUTUAL_FUND", "OPTION", "FUTURE", "FOREX", "INDEX", "CASH_EQUIVALENT", "FIXED_INCOME", "PRODUCT", "CURRENCY", "COLLECTIVE_INVESTMENT"),
    "FeeType": ("COMMISSION", "SEC_FEE", "STR_FEE", "R_FEE", "CDSC_FEE", "OPT_REG_FEE", "ADDITIONAL_FEE", "MISCELLANEOUS_FEE", "FTT", "FUTURES_CLEARING_FEE", "FUTURES_DESK_OFFICE_FEE", "FUTURES_EXCHANGE_FEE", "FUTURES_GLOBEX_FEE", "FUTURES_NFA_FEE", "FUTURES_PIT_BROKERAGE_FEE", "FUTURES_TRANSACTION_FEE", "LOW_PROCEEDS_COMMISSION", "BASE_CHARGE", "GENERAL_CHARGE", "GST_FEE", "TAF_FEE", "INDEX_OPTION_FEE", "TEFRA_TAX", "STATE_TAX", "UNKNOWN"),
    "TransactionType": ("TRADE", "RECEIVE_AND_DELIVER", "DIVIDEND_OR_INTEREST", "ACH_RECEIPT", "ACH_DISBURSEMENT", "CASH_RECEIPT", "CASH_DISBURSEMENT", "ELECTRONIC_FUND", "WIRE_OUT", "WIRE_IN", "JOURNAL", "MEMORANDUM", "MARGIN_CALL", "MONEY_MARKET", "SMA_ADJUSTMENT"),
    "OrderTypeRequest": ("MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP", "CABINET", "NON_MARKETABLE", "MARKET_ON_CLOSE", "EXERCISE", "TRAILING_STOP_LIMIT", "NET_DEBIT", "NET_CREDIT", "NET_ZERO", "LIMIT_ON_CLOSE"),
}

REF_FIELDS = {
    "Bond": "cusip symbol description exchange assetType bondFactor bondMultiplier bondPrice type",
    "Instrument": "cusip symbol description exchange assetType type",
    "InstrumentResponse": "cusip symbol description exchange assetType bondFactor bondMultiplier bondPrice fundamental instrumentInfo bondInstrumentInfo type",
    "Candle": "close high low open volume datetime datetimeISO8601",
    "CandleList": "candles empty previousClose previousCloseDate previousCloseDateISO8601 symbol",
    "Interval": "start end",
    "QuoteError": "invalidCusips invalidSSIDs invalidSymbols",
    "QuoteEquity": "52WeekHigh 52WeekLow askMICId askPrice askSize askTime bidMICId bidPrice bidSize bidTime closePrice highPrice lastMICId lastPrice lastSize lowPrice mark markChange markPercentChange netChange netPercentChange openPrice quoteTime securityStatus totalVolume tradeTime volatility",
    "QuoteForex": "52WeekHigh 52WeekLow askPrice askSize bidPrice bidSize closePrice highPrice lastPrice lastSize lowPrice mark netChange netPercentChange openPrice quoteTime securityStatus tick tickAmount totalVolume tradeTime",
    "QuoteFuture": "askMICId askPrice askSize askTime bidMICId bidPrice bidSize bidTime closePrice futurePercentChange highPrice lastMICId lastPrice lastSize lowPrice mark netChange openInterest openPrice quoteTime quotedInSession securityStatus settleTime tick tickAmount totalVolume tradeTime",
    "QuoteFutureOption": "askMICId askPrice askSize bidMICId bidPrice bidSize closePrice highPrice lastMICId lastPrice lastSize lowPrice mark markChange netChange netPercentChange openInterest openPrice quoteTime securityStatus settlemetPrice tick tickAmount totalVolume tradeTime",
    "QuoteIndex": "52WeekHigh 52WeekLow closePrice highPrice lastPrice lowPrice netChange netPercentChange openPrice securityStatus totalVolume tradeTime",
    "QuoteMutualFund": "52WeekHigh 52WeekLow closePrice nAV netChange netPercentChange securityStatus totalVolume tradeTime",
    "QuoteOption": "52WeekHigh 52WeekLow askPrice askSize bidPrice bidSize closePrice highPrice lastPrice lastSize lowPrice mark markChange markPercentChange netChange netPercentChange openInterest openPrice quoteTime securityStatus totalVolume tradeTime volatility delta gamma theta vega rho impliedYield moneyIntrinsicValue theoreticalOptionValue timeValue underlyingPrice indAskPrice indBidPrice indQuoteTime",
    "ExtendedMarket": "askPrice askSize bidPrice bidSize lastPrice lastSize mark quoteTime totalVolume tradeTime",
    "RegularMarket": "regularMarketLastPrice regularMarketLastSize regularMarketNetChange regularMarketPercentChange regularMarketTradeTime",
    "Fundamental": "avg10DaysVolume avg1YearVolume declarationDate divAmount divExDate divFreq divPayAmount divPayDate divYield eps fundLeverageFactor fundStrategy nextDivExDate nextDivPayDate peRatio",
    "ReferenceEquity": "cusip description exchange exchangeName fsiDesc htbQuantity htbRate isHardToBorrow isShortable otcMarketTier",
    "ReferenceForex": "description exchange exchangeName isTradable marketMaker product tradingHours",
    "ReferenceFuture": "description exchange exchangeName futureActiveSymbol futureExpirationDate futureIsActive futureMultiplier futurePriceFormat futureSettlementPrice futureTradingHours product futureIsTradable",
    "ReferenceFutureOption": "contractType description exchange exchangeName multiplier expirationDate expirationStyle strikePrice underlying",
    "ReferenceIndex": "description exchange exchangeName",
    "ReferenceMutualFund": "cusip description exchange exchangeName",
    "ReferenceOption": "contractType cusip daysToExpiration deliverables description exchange exchangeName exerciseType expirationDay expirationMonth expirationType expirationYear isPennyPilot lastTradingDay multiplier settlementType strikePrice underlying",
    "OptionChain": "symbol status underlying strategy interval isDelayed isIndex daysToExpiration interestRate underlyingPrice volatility callExpDateMap putExpDateMap",
    "Underlying": "ask bid change close askSize bidSize delayed description exchangeName fiftyTwoWeekHigh fiftyTwoWeekLow highPrice last lowPrice mark markChange markPercentChange openPrice percentChange quoteTime tradeTime totalVolume symbol",
    "OptionDeliverables": "symbol assetType deliverableUnits currencyType",
    "OptionContract": "putCall symbol description exchangeName bidPrice askPrice lastPrice markPrice bidSize askSize lastSize highPrice lowPrice openPrice closePrice totalVolume tradeDate quoteTimeInLong tradeTimeInLong netChange volatility delta gamma theta vega rho timeValue openInterest isInTheMoney theoreticalOptionValue theoreticalVolatility isMini isNonStandard optionDeliverablesList strikePrice expirationDate daysToExpiration expirationType lastTradingDay multiplier settlementType deliverableNote isIndexOption percentChange markChange markPercentChange isPennyPilot intrinsicValue optionRoot",
    "ExpirationChain": "status expirationList",
    "Expiration": "daysToExpiration expiration expirationType standard settlementType optionRoots expirationDate",
    "ErrorResponse": "errors",
    "Error": "id status title detail source",
    "ErrorSource": "pointer parameter header",
    "AccountNumberHash": "accountNumber hashValue",
    "Order": "orderType requestedDestination tag session duration cancelTime complexOrderStrategyType quantity filledQuantity remainingQuantity destinationLinkName releaseTime stopPrice stopPriceLinkBasis stopPriceLinkType stopPriceOffset stopType priceLinkBasis priceLinkType price taxLotMethod orderLegCollection activationPrice specialInstruction orderStrategyType orderId cancelable editable status enteredTime closeTime accountNumber orderActivityCollection replacingOrderCollection childOrderStrategies statusDescription",
    "OrderLegCollection": "orderLegType legId instrument instruction positionEffect quantity quantityType divCapGains toSymbol",
    "OrderActivity": "activityType executionType quantity orderRemainingQuantity executionLegs",
    "ExecutionLeg": "legId price quantity mismarkedQuantity instrumentId time",
    "DateParam": "date",
    "ServiceError": "message errors",
    "PreviewOrder": "orderId orderStrategy orderValidationResult commissionAndFee",
    "OrderStrategy": "accountNumber advancedOrderType closeTime enteredTime orderBalance orderStrategyType orderVersion session status allOrNone discretionary duration filledQuantity orderType orderValue price quantity remainingQuantity sellNonMarginableFirst settlementInstruction strategy amountIndicator orderLegs",
    "OrderLeg": "askPrice bidPrice lastPrice markPrice projectedCommission quantity finalSymbol legId assetType instruction",
    "OrderBalance": "orderValue projectedAvailableFund projectedBuyingPower projectedCommission",
    "OrderValidationResult": "alerts accepts rejects reviews warns",
    "OrderValidationDetail": "validationRuleName message activityMessage originalSeverity overrideName overrideSeverity",
    "CommissionAndFee": "commission fee trueCommission",
    "Account": "securitiesAccount",
    "Position": "shortQuantity averagePrice currentDayProfitLoss currentDayProfitLossPercentage longQuantity settledLongQuantity settledShortQuantity agedQuantity instrument marketValue maintenanceRequirement averageLongPrice averageShortPrice taxLotAverageLongPrice taxLotAverageShortPrice longOpenProfitLoss shortOpenProfitLoss previousSessionLongQuantity previousSessionShortQuantity currentDayCost",
    "TransactionAPIOptionDeliverable": "rootSymbol strikePercent deliverableNumber deliverableUnits deliverable assetType",
    "Transaction": "activityId time user description accountNumber type status subAccount tradeDate settlementDate positionId orderId netAmount activityType transferItems",
    "UserDetails": "cdDomainId login type userId systemUserName firstName lastName brokerRepCode",
    "TransferItem": "instrument amount cost price feeType positionEffect",
    "UserPreference": "accounts streamerInfo offers",
    "UserPreferenceAccount": "accountNumber type nickName displayAcctId primaryAccount accountColor autoPositionEffect",
    "StreamerInfo": "streamerSocketUrl schwabClientCustomerId schwabClientCorrelId schwabClientChannel schwabClientFunctionId",
    "Offer": "level2Permissions mktDataPermission",
}

REF_BALANCES = {
    "MarginInitialBalance": "accruedInterest availableFundsNonMarginableTrade bondValue buyingPower cashBalance cashAvailableForTrading cashReceipts dayTradingBuyingPower dayTradingBuyingPowerCall dayTradingEquityCall equity equityPercentage liquidationValue longMarginValue longOptionMarketValue longStockValue maintenanceCall maintenanceRequirement margin marginEquity moneyMarketFund mutualFundValue regTCall shortMarginValue shortOptionMarketValue shortStockValue totalCash isInCall unsettledCash pendingDeposits marginBalance shortBalance accountValue",
    "MarginBalance": "availableFunds availableFundsNonMarginableTrade buyingPower buyingPowerNonMarginableTrade dayTradingBuyingPower dayTradingBuyingPowerCall equity equityPercentage longMarginValue maintenanceCall maintenanceRequirement marginBalance regTCall shortBalance shortMarginValue sma isInCall stockBuyingPower optionBuyingPower",
    "CashInitialBalance": "accruedInterest cashAvailableForTrading cashAvailableForWithdrawal cashBalance bondValue cashReceipts liquidationValue longOptionMarketValue longStockValue moneyMarketFund mutualFundValue shortOptionMarketValue shortStockValue isInCall unsettledCash cashDebitCallValue pendingDeposits accountValue",
    "CashBalance": "cashAvailableForTrading cashAvailableForWithdrawal cashCall longNonMarginableMarketValue totalCash cashDebitCallValue unsettledCash",
}


"""One blocking function per read endpoint of the Schwab API (SPEC-015 5.10 and 15; REF sections 2 and 3).

Every function validates ALL its parameters locally (enum values, ranges, dates, the valid combinations of REF 2.5)
and raises SchwabError(INVALID_PARAM, 422, detail=...) before any call, sends them through 'http.get' (the only door to
the host) and returns objects of 'models' (read tolerantly: nothing the server adds is lost, see models.py). The
four order routes that write (POST, PUT, DELETE of orders and previewOrder) have no code here and the door refuses them.

Parameter names are the ones of the REF (camelCase), the same names the agent tools and the internal API use. A path
that carries an account takes 'account_ref' ("A1", "A2"...), never a hash or a number: 'accounts.hash_for' translates
it. Account numbers and hashes are not logged and never leave in an error. Call these from a thread
(asyncio.to_thread): they block.

The data of one account comes back with its real 'accountNumber'; pass it through 'accounts.public' before it leaves
the backend (5.9).
"""
import datetime as dt
import math
import re
from collections.abc import Callable, Iterable
from typing import Any

from hokma.schwab import http, models
from hokma.schwab.http import INVALID_PARAM, MARKETDATA, TRADER, SchwabError

QUOTES_MAX_SYMBOLS = 500                              # REF A.7: 'Search combination should not exceed 500'
ORDERS_MAX_AGE = dt.timedelta(days=60)                # REF 3.2: GET /orders, 'within 60 days from today'
ORDERS_RECENT = dt.timedelta(days=59, hours=23)       # margin: a request for 'up to 60 days' starts here
ACCOUNT_MAX_RANGE = dt.timedelta(days=365)            # REF 3.2 and 3.3: one year per account
ACCOUNT_DEFAULT_RANGE = dt.timedelta(days=364, hours=23)
MARKET_DATE_SLACK_DAYS = 1                            # REF 2.7 'from today'; one day of margin for the time zone
MARKET_DATE_AHEAD_DAYS = 366                          # REF 2.7 'up to 1 year ahead'
CHAIN_ANALYTICAL_ONLY = ("volatility", "underlyingPrice", "interestRate", "daysToExpiration")   # REF 2.3

# REF 2.5, table of valid combinations: periodType -> (periods, frequencyTypes, default frequencyType)
_HISTORY = {
    "day": ((1, 2, 3, 4, 5, 10), ("minute",), "minute"),
    "month": ((1, 2, 3, 6), ("daily", "weekly"), "weekly"),
    "year": ((1, 2, 3, 5, 10, 15, 20), ("daily", "weekly", "monthly"), "monthly"),
    "ytd": ((1,), ("daily", "weekly"), "weekly"),
}
_FREQUENCIES = {"minute": (1, 5, 10, 15, 30), "daily": (1,), "weekly": (1,), "monthly": (1,)}

_now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?(Z|[+-]\d{2}:?\d{2})")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_CUSIP = re.compile(r"[A-Za-z0-9]{9}")
_ID = re.compile(r"[0-9]{1,19}")
_SEGMENT_SAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-")


def set_clock(now: Callable[[], dt.datetime] | None = None) -> None:
    """Tests only: a fake 'now' (aware datetime) for the date windows. None restores the real clock."""
    global _now
    _now = now or (lambda: dt.datetime.now(dt.timezone.utc))


# ------------------------------------------------------------------------------------------------ validation


def _bad(detail: str) -> SchwabError:
    return SchwabError(INVALID_PARAM, 422, detail=detail)


def _choice(name: str, value: Any, enum: str) -> Any:
    allowed = models.enum_values(enum)
    if isinstance(value, bool) or not isinstance(value, (str, int)) or value not in allowed:
        raise _bad(f"{name} must be one of {', '.join(str(v) for v in allowed)}")
    return value


def _choices(name: str, value: Any, enum: str, *, extra: tuple = (), required: bool = False) -> str | None:
    """A comma separated text, or a list, of values of one enum ('extra' are accepted besides the enum)."""
    if value is None:
        if required:
            raise _bad(f"{name} is required")
        return None
    if not isinstance(value, (str, list, tuple)):
        raise _bad(f"{name} must be text or a list")
    items = [part.strip() for part in value.split(",")] if isinstance(value, str) else list(value)
    if not items:
        raise _bad(f"{name} is required" if required else f"{name} is empty")
    allowed = models.enum_values(enum) + extra
    for item in items:
        if not isinstance(item, str) or item not in allowed:
            raise _bad(f"{name} must be values of {', '.join(str(v) for v in allowed)}")
    return ",".join(items)


def _int(name: str, value: Any, minimum: int | None = None, maximum: int | None = None) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(f"{name} must be an integer")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise _bad(f"{name} is out of range")
    return value


def _number(name: str, value: Any, minimum: float | None = None) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _bad(f"{name} must be a number")
    if minimum is not None and value < minimum:
        raise _bad(f"{name} is out of range")
    return value


def _flag(name: str, value: Any) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _bad(f"{name} must be true or false")
    return value


def _text(name: str, value: Any, limit: int = 64) -> str:
    if not isinstance(value, str) or not value.strip() or _CONTROL.search(value) or len(value.strip()) > limit:
        raise _bad(f"{name} must be a short text")
    return value.strip()


def _symbol(name: str, value: Any) -> str:
    """One symbol (a stock, an option like 'AMZN  230317C01360000', an index like '$SPX'): no comma, no control."""
    text = _text(name, value, 40)
    if "," in text or not any(c.isalnum() for c in text):
        raise _bad(f"{name} must be one symbol")
    return text


def _symbols(name: str, value: Any, limit: int) -> str:
    items = value.split(",") if isinstance(value, str) else (list(value) if isinstance(value, Iterable) else [])
    cleaned = []
    for item in items:
        if not isinstance(item, str) or not item.strip() or _CONTROL.search(item):
            raise _bad(f"{name} must be symbols")
        cleaned.append(item.strip())
    if not cleaned:
        raise _bad(f"{name} is required")
    if len(cleaned) > limit:
        raise _bad(f"{name} has more than {limit} symbols")
    return ",".join(cleaned)


def _ymd(name: str, value: Any) -> str | None:
    """A date 'yyyy-MM-dd' (REF 2.3, 2.7)."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        raise _bad(f"{name} must be a date yyyy-MM-dd")
    text = value.isoformat() if isinstance(value, dt.date) else value
    if not isinstance(text, str) or not _DAY.fullmatch(text):
        raise _bad(f"{name} must be a date yyyy-MM-dd")
    try:
        dt.date.fromisoformat(text)
    except ValueError:
        raise _bad(f"{name} must be a real date yyyy-MM-dd") from None
    return text


def _millis(name: str, value: Any) -> int | None:
    """An instant in milliseconds since the Unix epoch (REF 2.5); a datetime is converted."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return int(_utc(value).timestamp() * 1000)
    return _int(name, value, 0)


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)


def _parse_time(name: str, value: Any) -> dt.datetime:
    """An instant 'yyyy-MM-dd'T'HH:mm:ss.SSSZ' (REF 1), or a datetime, as an aware UTC datetime."""
    if isinstance(value, dt.datetime):
        return _utc(value)
    match = _TIME.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise _bad(f"{name} must be yyyy-MM-dd'T'HH:mm:ss.SSSZ")
    year, month, day, hour, minute, second, frac, zone = match.groups()
    try:
        offset = dt.timedelta(0)
        if zone != "Z":
            digits = zone[1:].replace(":", "")
            offset = dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            offset = offset if zone[0] == "+" else -offset
        moment = dt.datetime(int(year), int(month), int(day), int(hour), int(minute), int(second),
                             int((frac or "0").ljust(3, "0")) * 1000, tzinfo=dt.timezone(offset))
    except ValueError:
        raise _bad(f"{name} must be a real instant yyyy-MM-dd'T'HH:mm:ss.SSSZ") from None
    return moment.astimezone(dt.timezone.utc)


def _format_time(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _window(first: str, last: str, start: Any, end: Any, *, recent: bool) -> tuple[str, str]:
    """The pair of instants of GET /orders (recent: within 60 days, REF 3.2) or of an account (at most one year, REF 3.2
    and 3.3). A missing end is now; a missing start is the widest allowed window. A request for up to 60 days that
    starts in the last hour of that limit is moved to 59 days 23 hours (margin for the server clock)."""
    now = _now()
    end_at = _parse_time(last, end) if end is not None else now
    if recent:
        margin = now - ORDERS_RECENT
        start_at = _parse_time(first, start) if start is not None else margin
        if start_at < now - ORDERS_MAX_AGE:
            raise _bad(f"{first} is older than 60 days")
        if end_at < start_at:
            raise _bad(f"{last} is before {first}")
        if start_at < margin <= end_at:
            start_at = margin
    else:
        start_at = _parse_time(first, start) if start is not None else end_at - ACCOUNT_DEFAULT_RANGE
        if end_at < start_at:
            raise _bad(f"{last} is before {first}")
        if end_at - start_at > ACCOUNT_MAX_RANGE:
            raise _bad(f"{first} and {last} are more than one year apart")
    return _format_time(start_at), _format_time(end_at)


def _segment(text: str) -> str:
    """A path segment for the gate's regex ('[A-Za-z0-9.%^-]{1,32}'): everything but letters, digits, '.' and '-' as
    %XX, so '$SPX' is '%24SPX' and 'BRK_B' is 'BRK%5FB'."""
    return "".join(c if c in _SEGMENT_SAFE else "".join(f"%{b:02X}" for b in c.encode("utf-8")) for c in text)


def _hash(account_ref: Any) -> str:
    from hokma.schwab import accounts as account_table                         # lazy: accounts.py imports this module

    return account_table.hash_for(account_ref)


def _id(name: str, value: Any) -> str:
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if not isinstance(text, str) or not _ID.fullmatch(text):
        raise _bad(f"{name} must be a number")
    return text


def _listed(cls, data: Any) -> list:
    if data is None:
        return []
    return [cls.read(item) for item in (data if isinstance(data, list) else [data])]


def _first(cls, data: Any):
    items = _listed(cls, data)
    return items[0] if items else None


# ------------------------------------------------------------------------------------------- Market Data (REF 2)


def quotes(symbols: Any, fields: Any = None, indicative: bool | None = None) -> models.QuoteResponse:
    """GET /marketdata/v1/quotes (REF 2.1, A.3 to A.5). 'symbols': a list or a comma text (stocks, ETFs, funds, indexes
    like '$SPX', options, futures like '/ESH23', future options, forex like 'AUD/CAD'), at most 500 (REF A.7);
    'fields': 'quote', 'fundamental', 'extended', 'reference', 'regular' or 'all'; 'indicative': the '$ABC.IV' quote of
    an ETF. Returns a QuoteResponse: symbol to the response object of its assetMainType, and 'errors' (QuoteError)."""
    query = {"symbols": _symbols("symbols", symbols, QUOTES_MAX_SYMBOLS),
             "fields": _choices("fields", fields, "QuoteField", extra=("all",)),
             "indicative": _flag("indicative", indicative)}
    return models.QuoteResponse.read(http.get(f"{MARKETDATA}/quotes", query))


def quote(symbol: Any, fields: Any = None):
    """GET /marketdata/v1/{symbol_id}/quotes (REF 2.2). One symbol; 'fields' as in quotes(). Returns the response object of
    its assetMainType (REF 6: the portal example is wrong; the answer is a QuoteResponseObject, or a map with the
    symbol as key, which is read the same). A symbol with '/' (a future) has to go through quotes()."""
    text = _symbol("symbol", symbol)
    if "/" in text:
        raise _bad("symbol with '/' goes through quotes()")
    segment = _segment(text)
    if len(segment) > 32:
        raise _bad("symbol is too long for a path")
    data = http.get(f"{MARKETDATA}/{segment}/quotes", {"fields": _choices("fields", fields, "QuoteField", extra=("all",))})
    if isinstance(data, dict) and "assetMainType" not in data and "quote" not in data:
        nodes = [v for k, v in data.items() if k != "errors" and isinstance(v, dict)]
        if nodes:
            data = nodes[0]
        else:
            return models.QuoteResponse.read(data)
    return models.read_quote_object(data)


def option_chain(symbol: Any, contractType: str | None = None, strikeCount: int | None = None,  # noqa: N803
                 includeUnderlyingQuote: bool | None = None, strategy: str | None = None,
                 interval: float | None = None, strike: float | None = None, range: str | None = None,  # noqa: A002
                 fromDate: Any = None, toDate: Any = None, volatility: float | None = None,
                 underlyingPrice: float | None = None, interestRate: float | None = None,
                 daysToExpiration: int | None = None, expMonth: str | None = None, optionType: str | None = None,
                 entitlement: str | None = None) -> models.OptionChain:
    """GET /marketdata/v1/chains (REF 2.3, A.6). contractType CALL|PUT|ALL; strikeCount (strikes above and below the
    money); includeUnderlyingQuote; strategy (SINGLE...ROLL, default SINGLE); interval and strike (numbers); range
    (ITM, NTM, OTM...: the REF says 'etc.', so only its shape is checked); fromDate and toDate (yyyy-MM-dd); volatility,
    underlyingPrice, interestRate and daysToExpiration only with strategy ANALYTICAL; expMonth (JAN..DEC, ALL);
    optionType (the REF gives no values: its shape is checked); entitlement PP|NP|PN (REF 2.3: retail tokens only;
    1.0.9 does not send it). Returns an OptionChain: 'callExpDateMap' and 'putExpDateMap' are
    {'yyyy-MM-dd:days': {'strike': [OptionContract]}}."""
    if range is not None and not (isinstance(range, str) and re.fullmatch(r"[A-Z_]{2,16}", range)):
        raise _bad("range must be a code like ITM, NTM or OTM")
    if optionType is not None and not (isinstance(optionType, str) and re.fullmatch(r"[A-Za-z_]{1,16}", optionType)):
        raise _bad("optionType must be a short code")
    start, end = _ymd("fromDate", fromDate), _ymd("toDate", toDate)
    if start and end and end < start:
        raise _bad("toDate is before fromDate")
    chosen = _choice("strategy", strategy, "Strategy") if strategy is not None else None
    analytical = {"volatility": _number("volatility", volatility, 0),
                  "underlyingPrice": _number("underlyingPrice", underlyingPrice, 0),
                  "interestRate": _number("interestRate", interestRate),
                  "daysToExpiration": _int("daysToExpiration", daysToExpiration, 0)}
    if chosen != "ANALYTICAL" and any(analytical[k] is not None for k in CHAIN_ANALYTICAL_ONLY):
        raise _bad("volatility, underlyingPrice, interestRate and daysToExpiration need strategy ANALYTICAL")
    query = {"symbol": _symbol("symbol", symbol),
             "contractType": (_choice("contractType", contractType, "ChainContractType")
                              if contractType is not None else None),
             "strikeCount": _int("strikeCount", strikeCount, 1),
             "includeUnderlyingQuote": _flag("includeUnderlyingQuote", includeUnderlyingQuote),
             "strategy": chosen, "interval": _number("interval", interval, 0), "strike": _number("strike", strike, 0),
             "range": range, "fromDate": start, "toDate": end, **analytical,
             "expMonth": _choice("expMonth", expMonth, "ExpMonth") if expMonth is not None else None,
             "optionType": optionType,
             "entitlement": _choice("entitlement", entitlement, "Entitlement") if entitlement is not None else None}
    return models.OptionChain.read(http.get(f"{MARKETDATA}/chains", query))


def expirations(symbol: Any) -> models.ExpirationChain:
    """GET /marketdata/v1/expirationchain (REF 2.4, A.6). One symbol. Returns an ExpirationChain: 'expirationList' of
    Expiration (the date is read from 'expiration' or from 'expirationDate', the REF has both)."""
    return models.ExpirationChain.read(http.get(f"{MARKETDATA}/expirationchain", {"symbol": _symbol("symbol", symbol)}))


def price_history(symbol: Any, periodType: str | None = None, period: int | None = None,  # noqa: N803
                  frequencyType: str | None = None, frequency: int | None = None, startDate: Any = None,
                  endDate: Any = None, needExtendedHoursData: bool | None = None,
                  needPreviousClose: bool | None = None) -> models.CandleList:
    """GET /marketdata/v1/pricehistory (REF 2.5, A.2). periodType day|month|year|ytd with its period, frequencyType and
    frequency as in the REF table (day: 1,2,3,4,5,10 and minute 1,5,10,15,30; month: 1,2,3,6 and daily|weekly 1; year:
    1,2,3,5,10,15,20 and daily|weekly|monthly 1; ytd: 1 and daily|weekly 1); startDate and endDate in milliseconds
    (or a datetime); with them frequencyType and frequency may go without periodType (REF 2.5 example).
    needExtendedHoursData and needPreviousClose are booleans. Returns a CandleList."""
    chosen = _choice("periodType", periodType, "PeriodType") if periodType is not None else None
    kind = _choice("frequencyType", frequencyType, "FrequencyType") if frequencyType is not None else None
    count, step = _int("period", period, 1), _int("frequency", frequency, 1)
    if chosen is None:
        if count is not None:
            raise _bad("period needs periodType")
        if step is not None and kind is None:
            raise _bad("frequency needs frequencyType")
    else:
        periods, kinds, default_kind = _HISTORY[chosen]
        if count is not None and count not in periods:
            raise _bad(f"period for {chosen} must be one of {', '.join(map(str, periods))}")
        if kind is not None and kind not in kinds:
            raise _bad(f"frequencyType for {chosen} must be one of {', '.join(kinds)}")
        kind = kind or (default_kind if step is not None else None)
    if step is not None and step not in _FREQUENCIES[kind]:
        raise _bad(f"frequency for {kind} must be one of {', '.join(map(str, _FREQUENCIES[kind]))}")
    first, last = _millis("startDate", startDate), _millis("endDate", endDate)
    if first is not None and last is not None and last < first:
        raise _bad("endDate is before startDate")
    query = {"symbol": _symbol("symbol", symbol), "periodType": chosen, "period": count, "frequencyType": kind,
             "frequency": step, "startDate": first, "endDate": last,
             "needExtendedHoursData": _flag("needExtendedHoursData", needExtendedHoursData),
             "needPreviousClose": _flag("needPreviousClose", needPreviousClose)}
    return models.CandleList.read(http.get(f"{MARKETDATA}/pricehistory", query))


def movers(index: Any, sort: str | None = None, frequency: int | None = None) -> models.ScreenerList:
    """GET /marketdata/v1/movers/{symbol_id} (REF 2.6, A.2). 'index': one of $DJI, $COMPX, $SPX, NYSE, NASDAQ, OTCBB,
    INDEX_ALL, EQUITY_ALL, OPTION_ALL, OPTION_PUT, OPTION_CALL ('%24DJI' is accepted too); 'sort': VOLUME, TRADES,
    PERCENT_CHANGE_UP, PERCENT_CHANGE_DOWN; 'frequency' in minutes: 0, 1, 5, 10, 30, 60. Returns a ScreenerList."""
    name = index.replace("%24", "$", 1) if isinstance(index, str) else index
    chosen = _choice("index", name, "MoversIndex")
    query = {"sort": _choice("sort", sort, "MoversSort") if sort is not None else None,
             "frequency": _choice("frequency", frequency, "MoversFrequency") if frequency is not None else None}
    return models.ScreenerList.read(http.get(f"{MARKETDATA}/movers/{chosen.replace('$', '%24')}", query))


def market_hours(markets: Any, date: Any = None) -> dict[str, dict[str, models.Hours]]:
    """GET /marketdata/v1/markets (REF 2.7) for a list of markets, or GET /marketdata/v1/markets/{market_id} (REF 2.8)
    for one market given as text. Markets: equity, option, bond, future, forex ('markets' is required); 'date'
    yyyy-MM-dd from today to one year ahead (default today). Returns {market: {product: Hours}} (the single market adds
    'exchange' and 'category' to each Hours)."""
    single = isinstance(markets, str) and "," not in markets
    listed = _choices("markets", markets, "Market", required=True)
    day = _ymd("date", date)
    if day is not None:
        today = _now().date()
        if not today - dt.timedelta(days=MARKET_DATE_SLACK_DAYS) <= dt.date.fromisoformat(day) <= today + dt.timedelta(
                days=MARKET_DATE_AHEAD_DAYS):
            raise _bad("date must be from today to one year ahead")
    if single:
        data = http.get(f"{MARKETDATA}/markets/{listed}", {"date": day})
    else:
        data = http.get(f"{MARKETDATA}/markets", {"markets": listed, "date": day})
    return _hours_map(data, listed if single else None)


def _hours_map(data: Any, single_market: str | None) -> dict[str, dict[str, models.Hours]]:
    out: dict[str, dict[str, models.Hours]] = {}
    for key, value in (data if isinstance(data, dict) else {}).items():
        if not isinstance(value, dict):
            continue
        if "isOpen" in value or "marketType" in value or "sessionHours" in value:      # a product directly: one market
            out.setdefault(single_market or str(value.get("marketType", "")).lower(), {})[key] = models.Hours.read(value)
        else:
            out[key] = {product: models.Hours.read(item) for product, item in value.items()}
    return out


def instruments(symbol: Any, projection: Any = None) -> models.InstrumentList:
    """GET /marketdata/v1/instruments (REF 2.9, A.1). 'symbol': a symbol, a comma text or a list (for the two regex
    projections the text goes as given); 'projection' (required): symbol-search, symbol-regex, desc-search,
    desc-regex, search, fundamental. Returns an InstrumentList of InstrumentResponse ('fundamental' has FundamentalInst)."""
    if projection is None:
        raise _bad("projection is required")
    chosen = _choice("projection", projection, "Projection")
    if chosen in ("symbol-regex", "desc-regex"):
        text = _text("symbol", symbol, 200)
    else:
        text = _symbols("symbol", symbol, 100)
    return models.InstrumentList.read(http.get(f"{MARKETDATA}/instruments", {"symbol": text, "projection": chosen}))


def instrument_by_cusip(cusip: Any) -> models.InstrumentResponse | None:
    """GET /marketdata/v1/instruments/{cusip_id} (REF 2.10). A 9 character CUSIP. Returns one InstrumentResponse (if the
    server wraps it in 'instruments', the first one)."""
    if not isinstance(cusip, str) or not _CUSIP.fullmatch(cusip):
        raise _bad("cusip must be 9 letters or digits")
    data = http.get(f"{MARKETDATA}/instruments/{cusip}")
    if isinstance(data, dict) and isinstance(data.get("instruments"), list):
        data = data["instruments"][0] if data["instruments"] else None
    return models.InstrumentResponse.read(data)


# ------------------------------------------------------------------------------ Accounts and Trading (REF 3)


def account_numbers() -> list[models.AccountNumberHash]:
    """GET /trader/v1/accounts/accountNumbers (REF 3.1, B.1). No parameters. Returns the pairs plain number and hash.
    INTERNAL: only accounts.py reads this; neither value may leave the backend."""
    return _listed(models.AccountNumberHash, http.get(f"{TRADER}/accounts/accountNumbers"))


def accounts(account_ref: str | None = None, fields: Any = None):
    """GET /trader/v1/accounts (REF 3.1, B.3) without 'account_ref': a list of Account; GET
    /trader/v1/accounts/{accountNumber} with it: one Account. 'fields': 'positions' (the positions come only with it). Margin and cash accounts come as
    MarginAccount and CashAccount, by 'type'."""
    query = {"fields": _choices("fields", fields, "AccountFields")}
    if account_ref is None:
        return _listed(models.Account, http.get(f"{TRADER}/accounts", query))
    return models.Account.read(http.get(f"{TRADER}/accounts/{_hash(account_ref)}", query))


def orders(account_ref: str | None = None, fromEnteredTime: Any = None, toEnteredTime: Any = None,  # noqa: N803
           status: str | None = None, maxResults: int | None = None) -> list[models.Order]:
    """GET /trader/v1/orders (REF 3.2) for all accounts (within 60 days), or GET /trader/v1/accounts/{accountNumber}/orders
    with 'account_ref' (at most one year between the two instants). Instants: 'yyyy-MM-dd'T'HH:mm:ss.SSSZ' or datetimes;
    a missing end is now and a missing start the widest window (59 days 23 hours for all accounts, 364 days 23 hours for
    one); a start inside the last hour of the 60 days is moved to 59 days 23 hours. 'status' takes ONE value of the
    status enum; 'maxResults' (default 3000 on the server)."""
    start, end = _window("fromEnteredTime", "toEnteredTime", fromEnteredTime, toEnteredTime, recent=account_ref is None)
    query = {"maxResults": _int("maxResults", maxResults, 1), "fromEnteredTime": start, "toEnteredTime": end,
             "status": _choice("status", status, "OrderStatus") if status is not None else None}
    path = f"{TRADER}/orders" if account_ref is None else f"{TRADER}/accounts/{_hash(account_ref)}/orders"
    return _listed(models.Order, http.get(path, query))


def order(account_ref: str, order_id: Any) -> models.Order | None:
    """GET /trader/v1/accounts/{accountNumber}/orders/{orderId} (REF 3.2). 'order_id': a whole number (int64)."""
    number = _id("order_id", order_id)
    return models.Order.read(http.get(f"{TRADER}/accounts/{_hash(account_ref)}/orders/{number}"))


def transactions(account_ref: str, types: Any = None, startDate: Any = None, endDate: Any = None,  # noqa: N803
                 symbol: str | None = None) -> list[models.Transaction]:
    """GET /trader/v1/accounts/{accountNumber}/transactions (REF 3.3, B.6). 'types' (required): one or several of
    TRADE, RECEIVE_AND_DELIVER, DIVIDEND_OR_INTEREST, ACH_RECEIPT, ACH_DISBURSEMENT, CASH_RECEIPT, CASH_DISBURSEMENT,
    ELECTRONIC_FUND, WIRE_OUT, WIRE_IN, JOURNAL, MEMORANDUM, MARGIN_CALL, MONEY_MARKET, SMA_ADJUSTMENT; startDate and
    endDate as in orders() (at most one year); 'symbol' filters (pass it as it is: the query is encoded here)."""
    kinds = _choices("types", types, "TransactionType", required=True)
    start, end = _window("startDate", "endDate", startDate, endDate, recent=False)
    query = {"startDate": start, "endDate": end, "symbol": _text("symbol", symbol, 40) if symbol is not None else None,
             "types": kinds}
    return _listed(models.Transaction, http.get(f"{TRADER}/accounts/{_hash(account_ref)}/transactions", query))


def transaction(account_ref: str, transaction_id: Any) -> models.Transaction | None:
    """GET /trader/v1/accounts/{accountNumber}/transactions/{transactionId} (REF 3.3). The REF declares a list with one
    Transaction (REF 6): the first one is returned, or None."""
    number = _id("transaction_id", transaction_id)
    return _first(models.Transaction, http.get(f"{TRADER}/accounts/{_hash(account_ref)}/transactions/{number}"))


def user_preference() -> models.UserPreference | None:
    """GET /trader/v1/userPreference (REF 3.4, B.6). No parameters. The REF shows a list with one UserPreference: the
    first one is returned (an object is read the same). Its 'streamerInfo' carries ids that stay in the backend."""
    return _first(models.UserPreference, http.get(f"{TRADER}/userPreference"))

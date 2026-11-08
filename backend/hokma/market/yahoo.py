"""Yahoo Finance as a `MarketDataSource`: the public v8 chart endpoint and the search endpoint, over plain HTTPS
with the standard library. No key, no extra dependency.

Every answer goes through the disk cache. When the network fails, the last cached answer is served and marked stale.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from hokma.market.cache import DiskCache, RateLimiter
from hokma.market.errors import BAD_RESPONSE, NETWORK, RATE_LIMITED, UNKNOWN_SYMBOL, UPSTREAM, MarketError
from hokma.market.models import Candle, Series, SessionPeriod, SymbolHit

BASE = "https://query1.finance.yahoo.com"
USER_AGENT = "Mozilla/5.0 (compatible; Hokma)"
TIMEOUT_SECONDS = 8
MAX_BODY_BYTES = 8 * 1024 * 1024

# interval and range sent upstream, the longest each base allows (verified against the real service: 1m answers at most
# 8 days per request, 5m and 15m need the start within 60 days, `730d` for 1h returns more than 730 days so `2y` is
# used, which returns exactly 730). Any other interval of the chart is aggregated from one of these bases.
UPSTREAM_PARAMS = {
    "1m": ("1m", "7d"),
    "5m": ("5m", "60d"),
    "15m": ("15m", "60d"),
    "1h": ("1h", "2y"),
    "1D": ("1d", "10y"),
}
# Seconds a cached answer is considered fresh.
FRESH_FOR = {"1m": 20, "5m": 45, "15m": 90, "1h": 240, "1D": 900}
SEARCH_FRESH_FOR = 24 * 3600
SEARCH_TYPES = {
    "EQUITY": "equity", "ETF": "etf", "INDEX": "index", "FUTURE": "future",
    "CRYPTOCURRENCY": "crypto", "CURRENCY": "currency", "MUTUALFUND": "other",
}

Fetcher = Callable[[str], tuple[int, str]]


def http_fetch(url: str) -> tuple[int, str]:
    """GET `url`. Returns (status, body). Raises MarketError(network) when nothing came back."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read(MAX_BODY_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            body = error.read(MAX_BODY_BYTES).decode("utf-8", "replace")
        except OSError:
            body = ""
        return error.code, body
    except (urllib.error.URLError, TimeoutError, OSError):
        raise MarketError(NETWORK, 502) from None


class YahooSource:
    name = "Yahoo Finance"

    def __init__(self, cache_dir: Path, fetch: Fetcher = http_fetch, clock: Callable[[], float] = time.time,
                 limiter: RateLimiter | None = None) -> None:
        self._cache = DiskCache(cache_dir)
        self._fetch = fetch
        self._clock = clock
        self._limiter = limiter or RateLimiter(limit=40, window=60.0)

    # ---------- transport ----------

    def _get(self, url: str, fresh_for: int) -> tuple[str, int, bool]:
        """Body, fetched_at and stale flag, from cache when fresh, else from the network, else from an old cache."""
        now = int(self._clock())
        cached = self._cache.get(url)
        if cached and now - cached[0] <= fresh_for:
            return cached[1], cached[0], False
        try:
            if not self._limiter.allow():
                raise MarketError(RATE_LIMITED, 429)
            status, body = self._fetch(url)
            if status == 404:
                raise MarketError(UNKNOWN_SYMBOL, 404)
            if status == 429:
                raise MarketError(RATE_LIMITED, 429)
            if status != 200:
                raise MarketError(UPSTREAM, 502)
            self._cache.put(url, body, now)
            return body, now, False
        except MarketError as error:
            if error.code in (UNKNOWN_SYMBOL,):
                raise
            if cached:
                return cached[1], cached[0], True
            raise

    # ---------- search ----------

    def search(self, query: str) -> list[SymbolHit]:
        params = urllib.parse.urlencode({"q": query, "quotesCount": 8, "newsCount": 0, "listsCount": 0})
        body, _, _ = self._get(f"{BASE}/v1/finance/search?{params}", SEARCH_FRESH_FOR)
        data = _loads(body)
        hits: list[SymbolHit] = []
        for quote in data.get("quotes") or []:
            kind = SEARCH_TYPES.get(str(quote.get("quoteType", "")).upper())
            symbol = quote.get("symbol")
            if not kind or not isinstance(symbol, str) or not valid_symbol(symbol):
                continue
            name = quote.get("longname") or quote.get("shortname") or symbol
            hits.append(SymbolHit(symbol=symbol, name=str(name), kind=kind,
                                  exchange=str(quote.get("exchDisp") or quote.get("exchange") or "")))
        return hits

    # ---------- bars ----------

    def history(self, symbol: str, timeframe: str) -> Series:
        interval, span = UPSTREAM_PARAMS[timeframe]
        params = urllib.parse.urlencode({"interval": interval, "range": span, "includePrePost": "false"})
        url = f"{BASE}/v8/finance/chart/{urllib.parse.quote(symbol, safe='')}?{params}"
        body, fetched_at, stale = self._get(url, FRESH_FOR[timeframe])
        return parse_chart(_loads(body), symbol, fetched_at, stale, self.name)


def valid_symbol(symbol: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9^=.\-]{1,20}", symbol) is not None


def _loads(body: str) -> dict:
    try:
        data = json.loads(body)
    except ValueError:
        raise MarketError(BAD_RESPONSE, 502) from None
    if not isinstance(data, dict):
        raise MarketError(BAD_RESPONSE, 502)
    return data


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def parse_chart(data: dict, symbol: str, fetched_at: int, stale: bool, source: str) -> Series:
    chart = data.get("chart")
    if not isinstance(chart, dict):
        raise MarketError(BAD_RESPONSE, 502)
    error = chart.get("error")
    if error:
        code = str(error.get("code", "")) if isinstance(error, dict) else ""
        raise MarketError(UNKNOWN_SYMBOL if code == "Not Found" else UPSTREAM, 404 if code == "Not Found" else 502)
    results = chart.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise MarketError(UNKNOWN_SYMBOL, 404)
    result = results[0]
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    stamps = result.get("timestamp") or []
    quotes = (result.get("indicators") or {}).get("quote") or [{}]
    quote = quotes[0] if quotes and isinstance(quotes[0], dict) else {}
    opens, highs, lows, closes, volumes = (quote.get(k) or [] for k in ("open", "high", "low", "close", "volume"))

    candles: list[Candle] = []
    seen: set[int] = set()
    for index, stamp in enumerate(stamps):
        if not isinstance(stamp, int) or stamp in seen:
            continue
        try:
            o, h, l, c = (_number(series[index]) for series in (opens, highs, lows, closes))
        except IndexError:
            continue
        if None in (o, h, l, c):
            continue  # Yahoo leaves holes (null) where there was no trade
        volume = _number(volumes[index]) if index < len(volumes) else None
        seen.add(stamp)
        candles.append(Candle(t=stamp, o=o, h=h, l=l, c=c, v=volume or 0.0))
    candles.sort(key=lambda candle: candle.t)

    regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    session = None
    if isinstance(regular.get("start"), int) and isinstance(regular.get("end"), int):
        session = SessionPeriod(start=regular["start"], end=regular["end"])
    last_trade = meta.get("regularMarketTime")
    offset = meta.get("gmtoffset")
    return Series(
        symbol=str(meta.get("symbol") or symbol),
        name=str(meta.get("longName") or meta.get("shortName") or meta.get("symbol") or symbol),
        kind=SEARCH_TYPES.get(_kind_key(meta.get("instrumentType")), "other"),
        exchange=str(meta.get("fullExchangeName") or meta.get("exchangeName") or ""),
        currency=str(meta.get("currency") or ""),
        timezone=str(meta.get("exchangeTimezoneName") or "UTC"),
        utc_offset_seconds=offset if isinstance(offset, int) else 0,
        candles=candles,
        previous_close=_number(meta.get("previousClose")),
        last_trade_at=last_trade if isinstance(last_trade, int) else None,
        session=session,
        fetched_at=fetched_at,
        stale=stale,
        source=source,
    )


def _kind_key(instrument_type) -> str:
    return str(instrument_type or "").upper()

"""Headlines by symbol and global headlines (SPEC-017: TA-77). The first source is Finnhub when the owner has it connected with its key
(shares and ETFs for a symbol, the general market feed for the global ones); Yahoo is only the fallback and says its name
(`source = "yahoo_news"`, `meta.provider = "Yahoo Finance"`). A `fetch` passed by the caller bypasses both and keeps the Yahoo label.

News feeds return their latest items whatever window is asked for. Items are cut to the window ([start, end + 1 day), never
past `as_of`), an undated item is kept only when the window reaches the present, and a window the feed cannot have observed is
`DATA_UNAVAILABLE` with the reason `window_not_observed`: that is not an absence of news.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from hokma.data import _lib, pit, sentinels
from hokma.data.providers import NoDataError, RateLimitError
from hokma.data.sentinels import DataResult, parse_time, utcnow

SOURCE = "yahoo_news"                           # the fallback feed, named as what it is
FINNHUB_SOURCE = "finnhub_news"                 # the owner's own connection, first when it is on and has its key
YAHOO_LABEL = {"provider": "Yahoo Finance", "fallback": True}
FINNHUB_LABEL = {"provider": "Finnhub", "fallback": False}
FINNHUB_URL = "https://finnhub.io/api/v1"
Fetch = Callable[..., Sequence[Mapping[str, Any]]]


def _published(raw: Mapping[str, Any]) -> datetime | None:
    value = raw.get("pubDate") or raw.get("providerPublishTime") or raw.get("published")
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        return parse_time(value)
    except (ValueError, OSError, OverflowError):
        return None


def extract_article(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """One normalised article from a nested record (`content` holding title, summary, provider, link) or a flat one. An
    article without a title is dropped."""
    data = raw.get("content") if isinstance(raw.get("content"), Mapping) else raw
    title = data.get("title")
    if not title:
        return None
    provider = data.get("provider")
    link = data.get("canonicalUrl") or data.get("clickThroughUrl") or data.get("link") or {}
    published = _published(data)
    return {
        "title": str(title), "summary": str(data.get("summary") or ""),
        "publisher": (provider.get("displayName") if isinstance(provider, Mapping) else data.get("publisher")) or "",
        "link": link.get("url", "") if isinstance(link, Mapping) else str(link),
        "published_at": published.isoformat() if published else None,
    }


def default_fetch(query: str, *, count: int = 30) -> list[dict]:
    """Headlines of a symbol or a search text from the free feed. Used only when no `fetch` is given."""
    try:
        return _lib.news_headlines(query, count)
    except OSError as error:
        raise RateLimitError(f"news feed unreachable: {type(error).__name__}", reason=sentinels.REASON_UNREACHABLE) from error
    except Exception as error:  # noqa: BLE001
        if "ratelimit" in type(error).__name__.lower():
            raise RateLimitError("news feed throttled", reason=sentinels.REASON_RATE_LIMITED) from error
        raise


# ---------- Finnhub: the first source of the headlines when the owner has it connected ----------

def _finnhub_key() -> str | None:
    """The saved Finnhub key when the owner has the connection turned on, else None (then the Yahoo fallback answers). Never raises:
    a connection service that cannot be read is "not connected"."""
    try:
        from hokma.connections import runtime

        service = runtime.get_service()
        if not service.loaded:
            service.load()
        if not service.is_enabled("finnhub") or not service.has_key("finnhub"):
            return None
        return service.read_secret("finnhub") or None
    except Exception:                                   # noqa: BLE001
        return None


def _http_get(url: str, headers: dict) -> tuple[int, str]:
    from hokma.market.history import http_get

    return http_get(url, headers)


def _finnhub_article(item: Mapping[str, Any]) -> dict[str, Any]:
    """A Finnhub item (headline, summary, source, url, datetime in seconds) in the flat shape `extract_article` reads."""
    return {"title": item.get("headline"), "summary": item.get("summary") or "", "publisher": item.get("source") or "",
            "link": item.get("url") or "", "providerPublishTime": item.get("datetime")}


def finnhub_fetch(query: str, *, key: str, start: datetime | None = None, end: datetime | None = None,
                  get: Callable[[str, dict], tuple[int, str]] | None = None) -> list[dict]:
    """Headlines of one symbol (`/company-news`, by day) or, with no symbol, the general market ones (`/news?category=general`).
    A 401 or 403 (key refused, plan without it) and a 429 are `RateLimitError` with their reason: the caller then uses the fallback."""
    import json
    import urllib.parse

    from hokma.market.errors import MarketError

    if query:
        stop = end or utcnow()
        begin = start or stop - timedelta(days=7)
        params = {"symbol": query, "from": begin.strftime("%Y-%m-%d"), "to": stop.strftime("%Y-%m-%d")}
        url = f"{FINNHUB_URL}/company-news?{urllib.parse.urlencode(params)}"
    else:
        url = f"{FINNHUB_URL}/news?{urllib.parse.urlencode({'category': 'general'})}"
    try:
        status, text = (get or _http_get)(url, {"X-Finnhub-Token": key})
    except (MarketError, OSError) as error:
        raise RateLimitError(f"finnhub unreachable: {type(error).__name__}", reason=sentinels.REASON_UNREACHABLE) from error
    if status in (401, 403):
        raise RateLimitError("finnhub refused the key or the plan", reason=sentinels.REASON_UNREACHABLE)
    if status == 429:
        raise RateLimitError("finnhub throttled", reason=sentinels.REASON_RATE_LIMITED)
    if status != 200:
        raise RateLimitError(f"finnhub answered {status}", reason=sentinels.REASON_UNREACHABLE)
    try:
        rows = json.loads(text)
    except ValueError:
        raise RateLimitError("finnhub sent something that is not JSON", reason=sentinels.REASON_UNREACHABLE) from None
    if not isinstance(rows, list):
        raise RateLimitError("finnhub sent an unexpected answer", reason=sentinels.REASON_UNREACHABLE)
    return [_finnhub_article(r) for r in rows if isinstance(r, Mapping)]


def _is_stock(symbol: str) -> bool:
    """Finnhub company news covers shares and ETFs; futures, indices, forex and crypto go to the fallback."""
    from hokma.market import symbols

    return symbols.classify(symbol) == "equity"


def _window(raw: Sequence[Mapping[str, Any]], start: datetime, end: datetime, now: datetime) -> tuple[list[dict], list[datetime | None]]:
    """Items in the day window [start, end + 1 day) and not later than `end` itself: `end` is the exact cut (`as_of`)."""
    articles = [a for a in (extract_article(r) for r in raw) if a is not None]
    dates = [parse_time(a["published_at"]) if a["published_at"] else None for a in articles]
    kept = [a for a, d in zip(articles, dates) if pit.in_window(d, start, end, now=now) and (d is None or d <= end)]
    kept.sort(key=lambda a: a["published_at"] or "", reverse=True)
    return kept, dates


def _result(kept: list[dict], dates: list[datetime | None], start: datetime, end: datetime, subject: str, cut: datetime,
            now: datetime, received: datetime, source: str = SOURCE, warnings: tuple[str, ...] = (), meta: dict | None = None) -> DataResult:
    if kept:
        return sentinels.ok(kept, source=source, as_of=cut, received_at=received, warnings=warnings, meta=meta)
    if source == FINNHUB_SOURCE:                      # Finnhub answered for the days asked: an empty list is an absence of news
        return sentinels.no_data(source, sentinels.REASON_EMPTY, as_of=cut, received_at=received, warnings=warnings, meta=meta)
    gap = pit.coverage_gap(dates, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), "news feed", subject, now=now)
    if gap is not None:
        return sentinels.unavailable(source, gap.reason, as_of=cut, received_at=received, warnings=(*warnings, gap.message), meta=meta)
    return sentinels.no_data(source, sentinels.REASON_EMPTY, as_of=cut, received_at=received, warnings=warnings, meta=meta)


def news(symbol: str, *, as_of: datetime, start: datetime | None = None, end: datetime | None = None, fetch: Fetch | None = None,
         clock: Callable[[], datetime] = utcnow) -> DataResult:
    """Headlines about `symbol` published in [start, end] and not after `as_of`. Default window: the 7 days before `as_of`."""
    cut, now = parse_time(as_of), clock()
    stop = min(parse_time(end), cut) if end else cut
    begin = parse_time(start) if start else stop - timedelta(days=7)
    source, warnings, meta = SOURCE, (), None
    raw = None
    if fetch is None:
        key = _finnhub_key() if _is_stock(symbol) else None
        if key:
            try:
                raw, source, meta = finnhub_fetch(symbol, key=key, start=begin, end=stop), FINNHUB_SOURCE, FINNHUB_LABEL
            except RateLimitError as error:             # Finnhub could not answer: Yahoo does, named as the fallback it is
                warnings = (f"finnhub_failed: {error}",)
        if source == SOURCE:
            meta = YAHOO_LABEL
    try:
        if raw is None:
            raw = (fetch or default_fetch)(symbol)
    except NoDataError as error:
        return sentinels.no_data(SOURCE, error.detail or sentinels.REASON_EMPTY, as_of=cut, received_at=now, warnings=warnings, meta=meta)
    except RateLimitError as error:
        return sentinels.unavailable(SOURCE, error.reason, as_of=cut, received_at=now, warnings=(*warnings, str(error)), meta=meta)
    kept, dates = _window(raw or [], begin, stop, now)
    return _result(kept, dates, begin, stop, f"headlines about {symbol}", cut, now, now, source, warnings, meta)


DEFAULT_GLOBAL_QUERIES = (
    "Federal Reserve interest rates inflation", "S&P 500 earnings GDP economic outlook", "geopolitical risk trade war sanctions",
    "ECB Bank of England BOJ central bank policy", "oil commodities supply chain energy",
)


def global_news(*, as_of: datetime, look_back_days: int = 7, queries: Sequence[str] = DEFAULT_GLOBAL_QUERIES, limit: int = 20,
                fetch: Fetch | None = None, clock: Callable[[], datetime] = utcnow) -> DataResult:
    """Market-wide headlines from a few searches, deduplicated by title and cut at `as_of`."""
    cut, now = parse_time(as_of), clock()
    begin = cut - timedelta(days=look_back_days)
    seen: set[str] = set()
    kept: list[dict] = []
    all_dates: list[datetime | None] = []
    source, warnings, meta = SOURCE, (), (YAHOO_LABEL if fetch is None else None)
    if fetch is None and (key := _finnhub_key()):
        try:
            items, dates = _window(finnhub_fetch("", key=key), begin, cut, now)
            for item in items:
                if item["title"] not in seen:
                    seen.add(item["title"])
                    kept.append(item)
            kept.sort(key=lambda a: a["published_at"] or "", reverse=True)
            return _result(kept[:limit], dates, begin, cut, "market headlines", cut, now, now, FINNHUB_SOURCE, (), FINNHUB_LABEL)
        except RateLimitError as error:
            warnings = (f"finnhub_failed: {error}",)
    try:
        for query in queries:
            items, dates = _window((fetch or default_fetch)(query), begin, cut, now)
            all_dates += dates
            for item in items:
                if item["title"] not in seen:
                    seen.add(item["title"])
                    kept.append(item)
    except RateLimitError as error:
        return sentinels.unavailable(SOURCE, error.reason, as_of=cut, received_at=now, warnings=(*warnings, str(error)), meta=meta)
    kept.sort(key=lambda a: a["published_at"] or "", reverse=True)
    return _result(kept[:limit], all_dates, begin, cut, "market headlines", cut, now, now, source, warnings, meta)

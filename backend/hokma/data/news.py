"""Headlines by symbol and global headlines (SPEC-017: TA-77).

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

SOURCE = "yahoo_news"
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


def _window(raw: Sequence[Mapping[str, Any]], start: datetime, end: datetime, now: datetime) -> tuple[list[dict], list[datetime | None]]:
    """Items in the day window [start, end + 1 day) and not later than `end` itself: `end` is the exact cut (`as_of`)."""
    articles = [a for a in (extract_article(r) for r in raw) if a is not None]
    dates = [parse_time(a["published_at"]) if a["published_at"] else None for a in articles]
    kept = [a for a, d in zip(articles, dates) if pit.in_window(d, start, end, now=now) and (d is None or d <= end)]
    kept.sort(key=lambda a: a["published_at"] or "", reverse=True)
    return kept, dates


def _result(kept: list[dict], dates: list[datetime | None], start: datetime, end: datetime, subject: str, cut: datetime,
            now: datetime, received: datetime) -> DataResult:
    if kept:
        return sentinels.ok(kept, source=SOURCE, as_of=cut, received_at=received)
    gap = pit.coverage_gap(dates, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), "news feed", subject, now=now)
    if gap is not None:
        return sentinels.unavailable(SOURCE, gap.reason, as_of=cut, received_at=received, warnings=(gap.message,))
    return sentinels.no_data(SOURCE, sentinels.REASON_EMPTY, as_of=cut, received_at=received)


def news(symbol: str, *, as_of: datetime, start: datetime | None = None, end: datetime | None = None, fetch: Fetch | None = None,
         clock: Callable[[], datetime] = utcnow) -> DataResult:
    """Headlines about `symbol` published in [start, end] and not after `as_of`. Default window: the 7 days before `as_of`."""
    cut, now = parse_time(as_of), clock()
    stop = min(parse_time(end), cut) if end else cut
    begin = parse_time(start) if start else stop - timedelta(days=7)
    try:
        raw = (fetch or default_fetch)(symbol)
    except NoDataError as error:
        return sentinels.no_data(SOURCE, error.detail or sentinels.REASON_EMPTY, as_of=cut, received_at=now)
    except RateLimitError as error:
        return sentinels.unavailable(SOURCE, error.reason, as_of=cut, received_at=now, warnings=(str(error),))
    kept, dates = _window(raw or [], begin, stop, now)
    return _result(kept, dates, begin, stop, f"headlines about {symbol}", cut, now, now)


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
    try:
        for query in queries:
            items, dates = _window((fetch or default_fetch)(query), begin, cut, now)
            all_dates += dates
            for item in items:
                if item["title"] not in seen:
                    seen.add(item["title"])
                    kept.append(item)
    except RateLimitError as error:
        return sentinels.unavailable(SOURCE, error.reason, as_of=cut, received_at=now, warnings=(str(error),))
    kept.sort(key=lambda a: a["published_at"] or "", reverse=True)
    return _result(kept[:limit], all_dates, begin, cut, "market headlines", cut, now, now)

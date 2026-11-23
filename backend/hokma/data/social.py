"""Social and prediction-market signals (SPEC-017: TA-71, TA-72, TA-73).

None of these sources keeps a history, so none is served for a past `as_of`: prediction-market odds are denied, and feeds
are cut to the window and report a window they never observed as `DATA_UNAVAILABLE`. A failed fetch is never presented as
"no posts": the two are different claims. `fetch` is a transport `(url, params, headers) -> (status, body)`.
"""
from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from hokma.data import pit, sentinels
from hokma.data.providers import NoDataError, RateLimitError, Transport, check_status, http_transport
from hokma.data.sentinels import DataResult, parse_time, utcnow

POLYMARKET_URL = "https://gamma-api.polymarket.com/public-search"
STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
REDDIT_URL = "https://www.reddit.com/r/{sub}/search.rss"
USER_AGENT = "hokma-data/1.0"
MAX_BODY_BYTES = 2_000_000
DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")
_CRYPTO = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "BCH", "DOT", "AVAX", "LINK"}


def _get(transport: Transport, source: str, url: str, params: Mapping[str, Any], headers: Mapping[str, str]) -> bytes:
    status, body = transport(url, params, headers)
    check_status(source, status, body)
    if len(body) > MAX_BODY_BYTES:
        raise NoDataError(source, None, "answer larger than the allowed size")
    return body


def _day(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------------------------------- TA-71
def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _forward_looking(market: Mapping[str, Any], now: datetime) -> bool:
    """Open markets that resolve in the future with prices and outcomes."""
    if market.get("closed"):
        return False
    end = market.get("endDate")
    if end:
        try:
            if parse_time(end) < now:
                return False
        except ValueError:
            pass
    return bool(_json_list(market.get("outcomePrices"))) and bool(_json_list(market.get("outcomes")))


def prediction_markets(topic: str, *, as_of: datetime, limit: int = 6, transport: Transport = http_transport,
                       clock: Callable[[], datetime] = utcnow) -> DataResult:
    """Market-implied probabilities of an event, most traded first. Odds have no vintage: for a past `as_of` they are denied."""
    cut, now = parse_time(as_of), clock()
    source = "polymarket"
    if _day(cut) < _day(now):
        return sentinels.no_data(source, sentinels.REASON_UNSUPPORTED, as_of=cut, received_at=now,
                                 warnings=(f"odds are withheld for {_day(cut)}: the source serves only live odds with no history",))
    try:
        body = _get(transport, source, POLYMARKET_URL, {"q": topic, "limit_per_type": 20}, {"User-Agent": USER_AGENT})
        data = json.loads(body)
    except RateLimitError as error:
        return sentinels.unavailable(source, error.reason, as_of=cut, received_at=now, warnings=(str(error),))
    except (NoDataError, json.JSONDecodeError) as error:
        return sentinels.unavailable(source, "bad_response", as_of=cut, received_at=now, warnings=(str(error),))
    candidates = [m for event in data.get("events", []) for m in event.get("markets", []) if _forward_looking(m, now)]
    candidates.sort(key=lambda m: m.get("volumeNum") or 0, reverse=True)
    rows = []
    for market in candidates[:limit]:
        try:
            probability = float(_json_list(market.get("outcomePrices"))[0])
        except (ValueError, IndexError):
            continue
        outcomes = _json_list(market.get("outcomes"))
        rows.append({"question": market.get("question"), "outcome": outcomes[0] if outcomes else "Yes", "probability": probability,
                     "volume": market.get("volumeNum") or 0, "resolves": (market.get("endDate") or "")[:10],
                     "one_week_change": market.get("oneWeekPriceChange")})
    if not rows:
        return sentinels.no_data(source, sentinels.REASON_EMPTY, as_of=cut, received_at=now)
    return sentinels.ok(rows, source=source, as_of=cut, received_at=now, warnings=("odds_are_live_not_a_forecast",))


# ---------------------------------------------------------------------------------------------------- TA-72
def stocktwits_symbol(ticker: str) -> str:
    """Crypto pairs are listed as `BASE.X`; other symbols pass through upper-cased."""
    compact = ticker.strip().upper().rstrip("+").replace("-", "")
    for quote in ("USDT", "USDC", "USD"):
        if compact.endswith(quote) and compact[: -len(quote)] in _CRYPTO:
            return f"{compact[: -len(quote)]}.X"
    return ticker.strip().upper()


def _created(message: Mapping[str, Any]) -> datetime | None:
    try:
        return parse_time(message["created_at"])
    except (KeyError, ValueError):
        return None


def stocktwits(symbol: str, *, as_of: datetime, start: datetime | None = None, limit: int = 30, transport: Transport = http_transport,
               clock: Callable[[], datetime] = utcnow) -> DataResult:
    """Bullish and bearish counts of the messages of a symbol in [start, as_of]. The public stream serves only recent
    messages: a window it cannot reach is `DATA_UNAVAILABLE` (window_not_observed), not silence."""
    cut, now = parse_time(as_of), clock()
    begin = parse_time(start) if start else cut - timedelta(days=1)
    source = "stocktwits"
    try:
        body = _get(transport, source, STOCKTWITS_URL.format(ticker=stocktwits_symbol(symbol)), {}, {"User-Agent": USER_AGENT, "Accept": "application/json"})
        data = json.loads(body)
    except RateLimitError as error:
        return sentinels.unavailable(source, error.reason, as_of=cut, received_at=now, warnings=(f"stocktwits unavailable: {error}",))
    except (NoDataError, json.JSONDecodeError) as error:
        return sentinels.unavailable(source, "bad_response", as_of=cut, received_at=now, warnings=(str(error),))
    fetched = data.get("messages", []) if isinstance(data, dict) else []
    kept = [m for m in fetched if pit.in_window(_created(m), begin, cut, now=now) and (_created(m) is None or _created(m) <= cut)]
    if not kept:
        gap = pit.coverage_gap((_created(m) for m in fetched), _day(begin), _day(cut), "StockTwits", f"messages about {symbol.upper()}", now=now)
        if gap is not None:
            return sentinels.unavailable(source, gap.reason, as_of=cut, received_at=now, warnings=(gap.message,))
        return sentinels.no_data(source, sentinels.REASON_EMPTY, as_of=cut, received_at=now)
    bullish = bearish = unlabeled = 0
    items = []
    for message in kept[:limit]:
        entities = message.get("entities") or {}
        sentiment = (entities.get("sentiment") or {}).get("basic") if isinstance(entities.get("sentiment"), Mapping) else None
        bullish += sentiment == "Bullish"
        bearish += sentiment == "Bearish"
        unlabeled += sentiment not in ("Bullish", "Bearish")
        items.append({"created_at": message.get("created_at"), "user": (message.get("user") or {}).get("username", "?"),
                      "sentiment": sentiment or "no-label", "body": (message.get("body") or "").replace("\n", " ").strip()[:280]})
    total = bullish + bearish + unlabeled
    value = {"messages": items, "bullish": bullish, "bearish": bearish, "unlabeled": unlabeled,
             "bullish_pct": round(100 * bullish / total) if total else 0, "bearish_pct": round(100 * bearish / total) if total else 0}
    return sentinels.ok(value, source=source, as_of=cut, received_at=now)


# ---------------------------------------------------------------------------------------------------- TA-73
_ATOM = {"atom": "http://www.w3.org/2005/Atom"}


def strip_html(content: str) -> str:
    """Plain text of the HTML body of a feed entry."""
    if not content:
        return ""
    if "<!-- SC_OFF -->" in content and "<!-- SC_ON -->" in content:
        content = content.split("<!-- SC_OFF -->")[1].split("<!-- SC_ON -->")[0]
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", content)).split())


def parse_atom(body: bytes) -> list[dict[str, Any]]:
    """Entries of an Atom feed: title, link, published time (UTC) and text."""
    if b"<!DOCTYPE" in body[:4096].upper() or b"<!ENTITY" in body.upper():
        raise ET.ParseError("feeds that declare a document type or entities are refused")   # no entity expansion from a feed
    root = ET.fromstring(body)
    out = []
    for entry in root.findall("atom:entry", _ATOM):
        published = entry.findtext("atom:published", default=None, namespaces=_ATOM) or entry.findtext("atom:updated", default=None, namespaces=_ATOM)
        try:
            when = parse_time(published) if published else None
        except ValueError:
            when = None
        link = entry.find("atom:link", _ATOM)
        out.append({"title": (entry.findtext("atom:title", default="", namespaces=_ATOM) or "").strip(),
                    "link": link.get("href") if link is not None else "", "published_at": when,
                    "text": strip_html(entry.findtext("atom:content", default="", namespaces=_ATOM) or "")})
    return out


def reddit_posts(symbol: str, *, as_of: datetime, start: datetime | None = None, subreddits: Sequence[str] = DEFAULT_SUBREDDITS,
                 limit: int = 20, transport: Transport = http_transport, clock: Callable[[], datetime] = utcnow) -> DataResult:
    """Posts that mention a symbol, from the public search feed (one request for all the subreddits). The feed serves only the
    last week and carries no scores. A failed fetch is `DATA_UNAVAILABLE`; `NO_DATA` means the window was observed and empty."""
    cut, now = parse_time(as_of), clock()
    begin = parse_time(start) if start else cut - timedelta(days=7)
    source = "reddit"
    sub = "+".join(subreddits)
    params = {"q": symbol, "restrict_sr": "on", "sort": "new", "t": "week", "limit": 100}
    try:
        posts = parse_atom(_get(transport, source, REDDIT_URL.format(sub=sub), params, {"User-Agent": USER_AGENT}))
    except RateLimitError as error:
        return sentinels.unavailable(source, error.reason, as_of=cut, received_at=now, warnings=(f"reddit unavailable: {error}",))
    except (NoDataError, ET.ParseError) as error:
        return sentinels.unavailable(source, "bad_response", as_of=cut, received_at=now, warnings=(str(error),))
    kept = [p for p in posts if pit.in_window(p["published_at"], begin, cut, now=now) and (p["published_at"] is None or p["published_at"] <= cut)]
    if not kept:
        dates = [p["published_at"] for p in posts]
        if len(posts) < 100:
            dates.append(now - timedelta(days=7))   # the search is limited to the last week, which bounds its coverage
        gap = pit.coverage_gap(dates, _day(begin), _day(cut), "Reddit", f"posts about {symbol.upper()}", now=now)
        if gap is not None:
            return sentinels.unavailable(source, gap.reason, as_of=cut, received_at=now, warnings=(gap.message,))
        return sentinels.no_data(source, sentinels.REASON_EMPTY, as_of=cut, received_at=now)
    rows = [{**p, "published_at": p["published_at"].isoformat() if p["published_at"] else None, "text": p["text"][:400]} for p in kept[:limit]]
    return sentinels.ok(rows, source=source, as_of=cut, received_at=now)

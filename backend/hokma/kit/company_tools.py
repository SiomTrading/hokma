"""Read-only company data for the agents of Hokma, from the providers the student connected in Connections.

Tools: `company_profile`, `company_news`, `earnings_calendar`, `earnings_surprises` and `basic_financials`.

The providers are asked in the order of the use "news" of Connections (Finnhub first by default). When the student has
no Finnhub (or switched it off, or the desk does not use it, or the plan refuses that data), the next connected provider
that has it is asked (Alpaca has news). When none can, the error says
which connection is missing and where it is connected. The saved key is read from the system store only to make the
request, goes in a request header, is never part of an answer or an error, and every answer is scrubbed of it.

Limits: a short cache, a per-minute call limiter per provider (under the free Finnhub plan of 60 calls a minute,
leaving room for the chart), a cap on every list and on the size of each text. Everything goes out over
`tls.outbound_context`. Nothing here writes anything.

Failures raise `LookupError` / `ValueError` with a message the model can read; the MCP server turns them into tool
errors."""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from hokma.i18n import t
from hokma.market.cache import RateLimiter
from hokma.market.errors import MarketError
from hokma.market.history import http_get

MAX_NEWS = 20
DEFAULT_NEWS = 10
MAX_SURPRISES = 12
DEFAULT_SURPRISES = 4
MAX_CALENDAR = 100
MAX_TEXT = 400                      # characters of one headline or summary
MAX_BODY = 2 * 1024 * 1024          # a provider answer bigger than this is not read
NEWS_DAYS = 90
CALENDAR_DAYS = 92
DEFAULT_NEWS_DAYS = 7
DEFAULT_CALENDAR_DAYS = 7
CACHE_SECONDS = {"company_profile": 900, "basic_financials": 900}
CACHE_DEFAULT = 300
CACHE_MAX = 200
DENIED_MEMORY = 3600
CALLS_PER_MINUTE = {"finnhub": 30, "alpaca": 100}       # Finnhub free plan: 60 a minute, shared with the chart
IMPACT = {"low": 1, "medium": 2, "high": 3}              # Finnhub's own words for how much an event moves markets
SYMBOL = re.compile(r"[A-Z0-9]{1,6}([.\-][A-Z])?")

METRICS = ("52WeekHigh", "52WeekLow", "marketCapitalization", "peTTM", "peBasicExclExtraTTM", "forwardPE", "psTTM",
           "pbQuarterly", "epsTTM", "epsGrowthTTMYoy", "epsGrowth3Y", "revenueGrowthTTMYoy", "revenueGrowth3Y",
           "revenuePerShareTTM", "grossMarginTTM", "operatingMarginTTM", "netProfitMarginTTM", "roeTTM", "roaTTM",
           "currentRatioQuarterly", "totalDebt/totalEquityQuarterly", "bookValuePerShareQuarterly", "beta",
           "dividendYieldIndicatedAnnual", "10DayAverageTradingVolume", "3MonthAverageTradingVolume")

# provider id -> (name shown, tools it has)
PROVIDERS = {
    "finnhub": ("Finnhub", ("company_profile", "company_news", "earnings_calendar", "earnings_surprises",
                            "basic_financials", "economic_calendar", "market_news")),
    "alpaca": ("Alpaca", ("company_news", "market_news")),
}


def _what(tool: str) -> str:
    return {"company_profile": t("company.what.profile"), "company_news": t("company.what.news"),
            "earnings_calendar": t("company.what.calendar"), "earnings_surprises": t("company.what.surprises"),
            "basic_financials": t("company.what.financials"), "economic_calendar": t("company.what.economic"),
            "market_news": t("company.what.market_news")}[tool]


Fetch = Callable[[str, dict], tuple[int, str]]
_fetch: Fetch = http_get
_clock: Callable[[], float] = time.time
_cache: dict[tuple, tuple[float, dict]] = {}
_denied: dict[tuple[str, str], float] = {}
_limiters: dict[str, RateLimiter] = {}


def reset() -> None:
    """Forget the cache, the limiters and the remembered refusals (tests, and when a key changes)."""
    _cache.clear()
    _denied.clear()
    _limiters.clear()


def forget_cache() -> None:
    """Forget only the short cache (the Refresh button): the limiters and remembered refusals stay."""
    _cache.clear()


class _Skip(Exception):
    """A provider that cannot answer this call; `reason` is the sentence for the agent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Denied(Exception):
    """The provider answered that the account has no access to this data (402, 403)."""


class _BadKey(Exception):
    """The provider rejected the saved key (401)."""


class _Limited(Exception):
    """The provider itself answered 429."""


class _Empty(Exception):
    """The provider answered, with nothing for this symbol or range."""


# ---------------------------------------------------------------------------------------------- arguments

def _symbol(value: Any) -> str:
    symbol = value.strip().upper() if isinstance(value, str) else ""
    if not SYMBOL.fullmatch(symbol):
        raise ValueError(t("company.err.symbol", symbol=str(value)[:20]))
    return symbol


def _today() -> date:
    return datetime.fromtimestamp(_clock(), timezone.utc).date()


def _day(value: Any) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        raise ValueError(t("company.err.dates"))
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(t("company.err.dates")) from None


def _range(start: Any, end: Any, default_from: Callable[[date], date], default_to: Callable[[date], date],
           longest: int) -> tuple[date, date]:
    today = _today()
    last = default_to(today) if end is None else _day(end)
    first = default_from(last) if start is None else _day(start)
    if first > last:
        raise ValueError(t("company.err.dates"))
    if (last - first).days > longest:
        raise ValueError(t("company.err.span", days=longest))
    return first, last


def _count(value: Any, default: int, most: int) -> int:
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= most:
        raise ValueError(t("company.err.limit", most=most))
    return value


# ---------------------------------------------------------------------------------------------- numbers and text

def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 6) if number == number and abs(number) != float("inf") else None


def _text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1].rstrip() + "…"


def _iso(seconds: Any) -> str | None:
    number = _num(seconds)
    if number is None or number <= 0:
        return None
    return datetime.fromtimestamp(number, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(text: str) -> Any:
    if len(text) > MAX_BODY:
        raise _Skip(t("company.err.bad_response", provider="{provider}"))
    try:
        return json.loads(text)
    except ValueError:
        raise _Skip(t("company.err.bad_response", provider="{provider}")) from None


# ---------------------------------------------------------------------------------------------- Finnhub

FINNHUB = "https://finnhub.io/api/v1"


def _finnhub(tool: str, args: dict, secret: str, fetch: Fetch) -> dict:
    headers = {"X-Finnhub-Token": secret}

    def get(path: str, **query: Any) -> Any:
        url = f"{FINNHUB}/{path}?{urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})}"
        status, text = fetch(url, headers)
        _status(status)
        return _json(text)

    symbol = args.get("symbol")
    if tool == "company_profile":
        data = get("stock/profile2", symbol=symbol)
        if not isinstance(data, dict) or not data.get("name"):
            raise _Empty
        return {"symbol": data.get("ticker") or symbol, "name": _text(data.get("name")),
                "exchange": data.get("exchange"), "country": data.get("country"), "currency": data.get("currency"),
                "industry": data.get("finnhubIndustry"), "ipo": data.get("ipo"),
                "market_cap_millions": _num(data.get("marketCapitalization")),
                "shares_outstanding_millions": _num(data.get("shareOutstanding")), "website": data.get("weburl")}
    if tool == "company_news":
        data = get("company-news", symbol=symbol, **{"from": args["desde"].isoformat(), "to": args["hasta"].isoformat()})
        if not isinstance(data, list):
            raise _Skip(t("company.err.bad_response", provider="{provider}"))
        rows = sorted((r for r in data if isinstance(r, dict) and r.get("headline")),
                      key=lambda r: _num(r.get("datetime")) or 0, reverse=True)[:args["limite"]]
        if not rows:
            raise _Empty
        return {"symbol": symbol, "count": len(rows), "news": [
            {"time": _iso(r.get("datetime")), "headline": _text(r.get("headline")), "summary": _text(r.get("summary")),
             "source": _text(r.get("source")), "url": r.get("url")} for r in rows]}
    if tool == "earnings_calendar":
        data = get("calendar/earnings", symbol=symbol, **{"from": args["desde"].isoformat(), "to": args["hasta"].isoformat()})
        rows = data.get("earningsCalendar") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise _Skip(t("company.err.bad_response", provider="{provider}"))
        rows = sorted((r for r in rows if isinstance(r, dict) and r.get("date")), key=lambda r: str(r["date"]))
        if not rows:
            raise _Empty
        return {"count": min(len(rows), MAX_CALENDAR), "truncated": len(rows) > MAX_CALENDAR, "earnings": [
            {"symbol": r.get("symbol"), "date": r.get("date"), "hour": r.get("hour"), "year": r.get("year"),
             "quarter": r.get("quarter"), "eps_estimate": _num(r.get("epsEstimate")), "eps_actual": _num(r.get("epsActual")),
             "revenue_estimate": _num(r.get("revenueEstimate")), "revenue_actual": _num(r.get("revenueActual"))}
            for r in rows[:MAX_CALENDAR]]}
    if tool == "market_news":
        data = get("news", category=args["categoria"])
        if not isinstance(data, list):
            raise _Skip(t("company.err.bad_response", provider="{provider}"))
        rows = sorted((r for r in data if isinstance(r, dict) and r.get("headline")),
                      key=lambda r: _num(r.get("datetime")) or 0, reverse=True)[:args["limite"]]
        if not rows:
            raise _Empty
        return {"category": args["categoria"], "count": len(rows), "news": [
            {"time": _iso(r.get("datetime")), "headline": _text(r.get("headline")), "summary": _text(r.get("summary")),
             "source": _text(r.get("source")), "url": r.get("url")} for r in rows]}
    if tool == "economic_calendar":
        data = get("calendar/economic", **{"from": args["desde"].isoformat(), "to": args["hasta"].isoformat()})
        rows = data.get("economicCalendar") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise _Skip(t("company.err.bad_response", provider="{provider}"))
        wanted = args["paises"]
        rows = sorted((r for r in rows if isinstance(r, dict) and r.get("event")
                       and (not wanted or str(r.get("country", "")).upper() in wanted)
                       and IMPACT.get(str(r.get("impact", "")).lower(), 0) >= IMPACT[args["importancia"]]),
                      key=lambda r: str(r.get("time")))
        if not rows:
            raise _Empty
        return {"count": min(len(rows), MAX_CALENDAR), "truncated": len(rows) > MAX_CALENDAR, "events": [
            {"time": r.get("time"), "country": r.get("country"), "event": _text(r.get("event")),
             "impact": r.get("impact"), "unit": r.get("unit"), "actual": _num(r.get("actual")),
             "estimate": _num(r.get("estimate")), "previous": _num(r.get("prev"))} for r in rows[:MAX_CALENDAR]]}
    if tool == "earnings_surprises":
        data = get("stock/earnings", symbol=symbol, limit=args["limite"])
        if not isinstance(data, list):
            raise _Skip(t("company.err.bad_response", provider="{provider}"))
        rows = sorted((r for r in data if isinstance(r, dict) and r.get("period")), key=lambda r: str(r["period"]),
                      reverse=True)[:args["limite"]]
        if not rows:
            raise _Empty
        return {"symbol": symbol, "count": len(rows), "surprises": [
            {"period": r.get("period"), "year": r.get("year"), "quarter": r.get("quarter"),
             "eps_estimate": _num(r.get("estimate")), "eps_actual": _num(r.get("actual")),
             "surprise": _num(r.get("surprise")), "surprise_pct": _num(r.get("surprisePercent"))} for r in rows]}
    data = get("stock/metric", symbol=symbol, metric="all")                 # basic_financials
    metrics = data.get("metric") if isinstance(data, dict) else None
    if not isinstance(metrics, dict) or not metrics:
        raise _Empty
    kept = {k: _num(metrics[k]) for k in METRICS if _num(metrics.get(k)) is not None}
    if not kept:
        raise _Empty
    return {"symbol": symbol, "metrics": kept}


def _status(status: int) -> None:
    if status == 401:
        raise _BadKey
    if status in (402, 403):
        raise _Denied
    if status == 429:
        raise _Limited
    if status == 404:
        raise _Empty
    if status != 200:
        raise _Skip(t("company.err.upstream", provider="{provider}"))


# ---------------------------------------------------------------------------------------------- Alpaca

ALPACA_NEWS = "https://data.alpaca.markets/v1beta1/news"


def _alpaca(tool: str, args: dict, secret: str, fetch: Fetch) -> dict:
    try:
        keys = json.loads(secret)
        headers = {"APCA-API-KEY-ID": keys["key_id"], "APCA-API-SECRET-KEY": keys["secret"]}
    except (ValueError, KeyError, TypeError):
        raise _BadKey from None
    symbol = args.get("symbol")                                              # none: general market news
    query = urllib.parse.urlencode({**({"symbols": symbol} if symbol else {}), "start": args["desde"].isoformat() + "T00:00:00Z",
                                    "end": args["hasta"].isoformat() + "T23:59:59Z", "limit": args["limite"],
                                    "sort": "desc", "include_content": "false"})
    status, text = fetch(f"{ALPACA_NEWS}?{query}", headers)
    _status(status)
    data = _json(text)
    rows = data.get("news") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise _Skip(t("company.err.bad_response", provider="{provider}"))
    rows = [r for r in rows if isinstance(r, dict) and r.get("headline")][:args["limite"]]
    if not rows:
        raise _Empty
    return {**({"symbol": symbol} if symbol else {}), "count": len(rows), "news": [
        {"time": r.get("created_at"), "headline": _text(r.get("headline")), "summary": _text(r.get("summary")),
         "source": _text(r.get("source")), "url": r.get("url")} for r in rows]}


ADAPTERS = {"finnhub": _finnhub, "alpaca": _alpaca}


# ---------------------------------------------------------------------------------------------- the chain

def _connections():
    from hokma.connections import runtime

    service = runtime.get_service()
    if not getattr(service, "loaded", True):
        service.load()
    return service


def _allowed(desk_id: int | None) -> set[str] | None:
    if desk_id is None:
        return None
    from hokma.connections.runtime import _desk_providers

    return _desk_providers(desk_id)


def _missing(tool: str) -> str:
    if tool in ("company_news", "market_news"):
        return t("company.err.missing_news")
    return t("company.err.missing")


def _scrub(payload: dict, secrets: list[str]) -> dict:
    text = json.dumps(payload, ensure_ascii=False)
    for secret in secrets:
        for piece in {secret, *re.findall(r'"([^"]{6,})"', secret)}:
            if len(piece) >= 6:
                text = text.replace(piece, "[key]").replace(json.dumps(piece)[1:-1], "[key]")
    return json.loads(text)


def _cache_key(provider: str, tool: str, args: dict) -> tuple:
    return (provider, tool, tuple(sorted((k, str(v)) for k, v in args.items())))


def _remember(key: tuple, payload: dict, now: float, seconds: int) -> None:
    if len(_cache) >= CACHE_MAX:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    _cache[key] = (now + seconds, payload)


def _ask(tool: str, args: dict, desk_id: int | None) -> dict:
    """The first provider, in order, that has this data for this student. Raises LookupError saying what is missing."""
    service = _connections()
    allowed = _allowed(desk_id)
    reasons: list[str] = []
    secrets: list[str] = []
    for provider in service.order("news"):                   # the order of the use "news" of Connections (SPEC-015 4.4)
        if provider not in PROVIDERS or tool not in PROVIDERS[provider][1]:
            continue
        name = PROVIDERS[provider][0]
        now = _clock()
        if not service.is_enabled(provider) or not service.has_key(provider):
            continue
        if allowed is not None and provider not in allowed:
            reasons.append(t("company.err.not_on_desk", provider=name))
            continue
        what = _what(tool)
        key = _cache_key(provider, tool, args)
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        if _denied.get((provider, tool), 0) > now:
            reasons.append(t("company.err.denied", provider=name, what=what))
            continue
        limiter = _limiters.setdefault(provider, RateLimiter(CALLS_PER_MINUTE[provider], 60.0, clock=_clock))
        if not limiter.allow():
            reasons.append(t("company.err.rate", provider=name, limit=CALLS_PER_MINUTE[provider]))
            continue
        secret = service.read_secret(provider)
        if not secret:
            continue
        secrets.append(secret)
        try:
            body = ADAPTERS[provider](tool, args, secret, _fetch)
        except _Denied:
            _denied[(provider, tool)] = now + DENIED_MEMORY
            reasons.append(t("company.err.denied", provider=name, what=what))
            continue
        except _BadKey:
            reasons.append(t("company.err.bad_key", provider=name))
            continue
        except _Limited:
            reasons.append(t("company.err.rate", provider=name, limit=CALLS_PER_MINUTE[provider]))
            continue
        except _Empty:
            reasons.append(t("company.err.empty", provider=name, what=what, symbol=args.get("symbol") or "-"))
            continue
        except _Skip as skip:
            reasons.append(skip.reason.replace("{provider}", name))
            continue
        except MarketError:
            reasons.append(t("company.err.network", provider=name))
            continue
        out = _scrub({"source": name, "fetched_at": _iso(now), **body}, secrets)
        _remember(key, out, now, CACHE_SECONDS.get(tool, CACHE_DEFAULT))
        return out
    raise LookupError(" ".join(reasons) if reasons else _missing(tool))


# ---------------------------------------------------------------------------------------------- the tools

def company_profile(symbol: Any, desk_id: int | None = None) -> dict:
    return _ask("company_profile", {"symbol": _symbol(symbol)}, desk_id)


def company_news(symbol: Any, desde: Any = None, hasta: Any = None, limite: Any = None,
                 desk_id: int | None = None) -> dict:
    first, last = _range(desde, hasta, lambda d: d - timedelta(days=DEFAULT_NEWS_DAYS), lambda d: d, NEWS_DAYS)
    return _ask("company_news", {"symbol": _symbol(symbol), "desde": first, "hasta": last,
                                 "limite": _count(limite, DEFAULT_NEWS, MAX_NEWS)}, desk_id)


def earnings_calendar(desde: Any = None, hasta: Any = None, symbol: Any = None, desk_id: int | None = None) -> dict:
    first, last = _range(desde, hasta, lambda d: min(d, _today()),
                         lambda d: d + timedelta(days=DEFAULT_CALENDAR_DAYS), CALENDAR_DAYS)
    args = {"desde": first, "hasta": last}
    if symbol is not None:
        args["symbol"] = _symbol(symbol)
    return _ask("earnings_calendar", args, desk_id)


def earnings_surprises(symbol: Any, limite: Any = None, desk_id: int | None = None) -> dict:
    return _ask("earnings_surprises", {"symbol": _symbol(symbol),
                                       "limite": _count(limite, DEFAULT_SURPRISES, MAX_SURPRISES)}, desk_id)


def basic_financials(symbol: Any, desk_id: int | None = None) -> dict:
    return _ask("basic_financials", {"symbol": _symbol(symbol)}, desk_id)

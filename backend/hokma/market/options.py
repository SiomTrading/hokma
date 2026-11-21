"""Option chain of US stocks and ETFs, read only, from the Alpaca key the student saved in Connections (SPEC-014).

Two sources, always with the same key (headers only, never in a URL, a log or an answer):

* contracts and open interest: `GET /v2/options/contracts` on the trading host of the key. A demo key (prefix PK) goes to
  `paper-api.alpaca.markets`; a real key (prefix AK) goes to `api.alpaca.markets`. This module is the ONLY code of Hokma that
  talks to the real host, with ONE request: that GET, on that exact path. The host, the path and the method are
  constants here and are checked against `read_exceptions` of `kit/hardblock_brokers.json` before every call. Orders, positions and the
  account are never asked. For an agent the real host stays refused by the hook, with or without this module.
* price, greeks and implied volatility: `GET /v1beta1/options/snapshots/{symbol}` on `data.alpaca.markets`. Feed `opra` first; a 403
  (the plan has no OPRA) repeats the request with `indicative` and remembers the refusal for one hour.

The agent tool (`options_chain`) and the three routes of the dashboard (`market/api.py`) use the functions below. Limits: a
cache of 30 seconds for the chain and 15 minutes for contracts and expirations, at most 5 pages per request, 60 calls a
minute and 2 MB per answer. Nothing here writes anything. Failures raise `OptionsError`, a `LookupError` that is also a
`ValueError` and carries the code and the HTTP status the dashboard uses.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.parse
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from hokma.i18n import t
from hokma.market import symbols as symbol_map
from hokma.market.cache import RateLimiter
from hokma.market.errors import MarketError
from hokma.market.history import http_get
from hokma.market.tz import NEW_YORK
from hokma.market.yahoo import valid_symbol

PAPER_HOST = "https://paper-api.alpaca.markets"
LIVE_HOST = "https://api.alpaca.markets"
DATA_HOST = "https://data.alpaca.markets"
CONTRACTS_PATH = "/v2/options/contracts"
CONSUMER = "hokma_backend"

MAX_PAGES = 5
PAGE_SIZE = 1000
MAX_BODY = 2 * 1024 * 1024
CHAIN_SECONDS = 30
CONTRACT_SECONDS = 900
FEED_DENIED_SECONDS = 3600
CALLS_PER_MINUTE = 60
CACHE_MAX = 200
MAX_EXPIRATIONS = 4
EXPIRATION_DAYS = 400
MAX_DAYS = 1100
DEFAULT_MAX_DAYS = 60
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
STRIKE_CHOICES = (4, 6, 8, 10, 12, 14, 16, 20, 30, 50)
DEFAULT_STRIKES = 14
MINUTES_PER_YEAR = 525600

Fetch = Callable[[str, dict], tuple[int, str]]
_fetch: Fetch = http_get
_clock: Callable[[], float] = time.time
_cache: dict[tuple, tuple[float, Any]] = {}
_feed_denied: dict[str, float] = {}
_host_memory: dict[str, str] = {}
_limiter: RateLimiter | None = None


def reset() -> None:
    """Forget the cache, the limiter, the remembered feed refusals and the remembered hosts (tests, and a new key)."""
    global _limiter
    _cache.clear()
    _feed_denied.clear()
    _host_memory.clear()
    _limiter = None


def forget_cache() -> None:
    """Forget only the short caches (the Refresh button): the limiter and the remembered refusals stay."""
    _cache.clear()


class OptionsError(LookupError, ValueError):
    """What went wrong, in the student's language. `code` is the short code of the dashboard (`no_connection`,
    `auth_failed`, `not_equity`, `bad_request`, `rate_limited`, `upstream`) and `status` its HTTP status."""

    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def _text(code: str, symbol: str = "") -> str:
    if code == "no_connection":
        return t("options.err.no_connection")
    if code == "desk":
        return t("options.err.desk")
    if code == "not_equity":
        return t("options.err.not_equity", symbol=symbol)
    if code == "auth_failed":
        return t("options.err.auth_failed")
    if code == "rate_limited":
        return t("options.err.rate_limited")
    return t("options.err.upstream")


def _fail(code: str, status: int, symbol: str = "") -> OptionsError:
    return OptionsError(code, status, _text(code, symbol))


def _bad(name: str, rule: str) -> OptionsError:
    return OptionsError("bad_request", 422, t("options.err.args", name=name, rule=rule))


# ---------------------------------------------------------------------------------------------- time and numbers

def _now() -> float:
    return _clock()


def _today() -> date:
    return datetime.fromtimestamp(_now(), NEW_YORK).date()


def market_open(now: float | None = None) -> bool:
    """The regular session of US stocks: Monday to Friday, 09:30 to 16:00 in New York (holidays are not known here)."""
    local = datetime.fromtimestamp(_now() if now is None else now, NEW_YORK)
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and 9 * 60 + 30 <= minutes < 16 * 60


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(value: Any, digits: int = 6) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if number == number and abs(number) != float("inf") else None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def years_to_expiry(expiration: date, now: float | None = None) -> float:
    """Years from now to 16:00 New York of the expiration day (minutes / 525600). Zero or negative: already expired."""
    close = datetime(expiration.year, expiration.month, expiration.day, 16, 0, tzinfo=NEW_YORK).timestamp()
    return ((close - (_now() if now is None else now)) / 60.0) / MINUTES_PER_YEAR


def intrinsic_value(kind: str, spot: float | None, strike: float) -> float | None:
    if spot is None:
        return None
    return round(max(0.0, spot - strike) if kind == "call" else max(0.0, strike - spot), 2)


def in_the_money(kind: str, spot: float | None, strike: float) -> bool:
    if spot is None:
        return False
    return strike < spot if kind == "call" else strike > spot


def prob_itm(kind: str, spot: float | None, strike: float, iv: float | None, years: float) -> float | None:
    """Probability of expiring in the money: N(d2) for a call and N(-d2) for a put, with the contract's IV, r = 0 and
    q = 0. At or after the close of the expiration day it is 1 for a contract in the money and 0 for the rest."""
    if spot is None or spot <= 0 or strike <= 0:
        return None
    if years <= 0:
        return 1.0 if in_the_money(kind, spot, strike) else 0.0
    if iv is None or iv <= 0:
        return None
    d2 = (math.log(spot / strike) - iv * iv * years / 2.0) / (iv * math.sqrt(years))
    return round(_cdf(d2 if kind == "call" else -d2), 4)


def expected_move(spot: float | None, iv: float | None, years: float) -> float | None:
    """S x IV x sqrt(T) x sqrt(2 / pi): the average absolute move implied by the volatility of the expiration."""
    if spot is None or iv is None or iv <= 0 or years <= 0:
        return None
    return round(spot * iv * math.sqrt(years) * math.sqrt(2.0 / math.pi), 3)


# ---------------------------------------------------------------------------------------------- the key

class _Creds:
    """The saved Alpaca key of the student. Lives only for the call that needs it."""

    def __init__(self, key_id: str, secret: str, raw: str) -> None:
        self.key_id, self.secret, self.raw = key_id, secret, raw
        self.headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        self.ident = hashlib.sha256(key_id.encode("utf-8")).hexdigest()[:16]

    @property
    def secrets(self) -> list[str]:
        return [self.raw, self.key_id, self.secret]

    def hosts(self) -> list[str]:
        """The trading hosts to try for the contracts, in order: by the prefix of the key id (PK demo, AK real), and for
        any other prefix the host that answered last time first, then the other one."""
        prefix = self.key_id[:2].upper()
        if prefix == "PK":
            return [PAPER_HOST]
        if prefix == "AK":
            return [LIVE_HOST]
        remembered = _host_memory.get(self.ident)
        order = [PAPER_HOST, LIVE_HOST]
        return [remembered] + [h for h in order if h != remembered] if remembered else order


def _credentials(desk_id: int | None) -> _Creds:
    from hokma.connections import runtime

    service = runtime.get_service()
    if not getattr(service, "loaded", True):
        service.load()
    if not service.is_enabled("alpaca") or not service.has_key("alpaca"):
        raise _fail("no_connection", 409)
    if desk_id is not None:
        allowed = runtime._desk_providers(desk_id)
        if allowed is not None and "alpaca" not in allowed:
            raise _fail("desk", 409)
    raw = service.read_secret("alpaca")
    if not raw:
        raise _fail("no_connection", 409)
    try:
        keys = json.loads(raw)
        key_id, secret = keys["key_id"], keys["secret"]
        if not isinstance(key_id, str) or not isinstance(secret, str) or not key_id or not secret:
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise _fail("auth_failed", 401) from None
    return _Creds(key_id, secret, raw)


# ---------------------------------------------------------------------------------------------- the requests

def _call(url: str, creds: _Creds) -> tuple[int, str]:
    """The one place that makes a request: a GET with the key in the headers, under the limiter and the size cap."""
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(CALLS_PER_MINUTE, 60.0, clock=lambda: _clock())
    if not _limiter.allow():
        raise _fail("rate_limited", 429)
    try:
        status, text = _fetch(url, creds.headers)
    except MarketError:
        raise _fail("upstream", 502) from None
    if len(text) > MAX_BODY:
        raise _fail("upstream", 502)
    return status, text


def _json(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError:
        raise _fail("upstream", 502) from None
    if not isinstance(data, dict):
        raise _fail("upstream", 502)
    return data


def _query(params: dict) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


def _contracts_url(host: str, params: dict) -> str:
    """The only URL this module builds for a trading host. Host and path are constants; a request to the real host
    must be one the lock allows for this consumer (GET on exactly this path), or nothing is sent."""
    from hokma.kit.hardblock import read_exception_allows

    if host == LIVE_HOST and not read_exception_allows(urllib.parse.urlsplit(host).netloc, "GET", CONTRACTS_PATH,
                                                       CONSUMER):
        raise _fail("upstream", 502)
    return f"{host}{CONTRACTS_PATH}?{_query(params)}"


def _scrubbed(payload: Any, creds: _Creds) -> Any:
    from hokma.kit.company_tools import _scrub

    return _scrub(payload, creds.secrets)


def _contract_pages(creds: _Creds, params: dict) -> tuple[list[dict], bool]:
    """All the contracts of a query (at most 5 pages). The host is chosen by the key; a key with another prefix tries
    the demo host and then the real one. Returns (contracts, truncated)."""
    contracts: list[dict] = []
    token: str | None = None
    truncated = False
    hosts = creds.hosts()
    for page in range(MAX_PAGES):
        paged = {**params, "limit": PAGE_SIZE, "page_token": token}
        status, text = 0, ""
        for index, host in enumerate(hosts):
            status, text = _call(_contracts_url(host, paged), creds)
            if status in (401, 403) and index + 1 < len(hosts):
                continue                                           # another host may know this key
            if status == 200:
                _host_memory[creds.ident] = host
                hosts = [host]
            break
        if status in (401, 403):
            raise _fail("auth_failed", 401)
        if status == 429:
            raise _fail("rate_limited", 429)
        if status == 404:
            break
        if status != 200:
            raise _fail("upstream", 502)
        body = _json(text)
        rows = body.get("option_contracts")
        if not isinstance(rows, list):
            raise _fail("upstream", 502)
        contracts += [c for c in (_contract(r) for r in rows) if c]
        token = body.get("next_page_token") or None
        if not token:
            break
        if page + 1 == MAX_PAGES:
            truncated = True
    return contracts, truncated


def _contract(row: Any) -> dict | None:
    if not isinstance(row, dict):
        return None
    try:
        expiration = date.fromisoformat(str(row["expiration_date"]))
        strike = float(row["strike_price"])
        kind = str(row["type"]).lower()
        symbol = str(row["symbol"])
    except (KeyError, ValueError, TypeError):
        return None
    if kind not in ("call", "put") or not symbol:
        return None
    size = _int(row.get("size")) or _int(row.get("multiplier")) or 100
    return {"symbol": symbol, "type": kind, "strike": strike, "expiration": expiration, "size": size,
            "open_interest": _int(row.get("open_interest")), "open_interest_date": row.get("open_interest_date")}


def _feed_state(creds: _Creds) -> str:
    return "indicative" if _feed_denied.get(creds.ident, 0) > _now() else "opra"


def _snapshot_pages(creds: _Creds, symbol: str, params: dict) -> tuple[dict[str, dict], str, bool]:
    """The snapshots of a query (at most 5 pages). `opra` first; a 403 repeats it with `indicative` and remembers
    the refusal for an hour. Returns (snapshots by OCC symbol, feed used, truncated)."""
    feed = _feed_state(creds)
    base = f"{DATA_HOST}/v1beta1/options/snapshots/{urllib.parse.quote(symbol, safe='')}"
    while True:
        out: dict[str, dict] = {}
        token: str | None = None
        truncated = False
        denied = False
        for page in range(MAX_PAGES):
            status, text = _call(f"{base}?{_query({**params, 'feed': feed, 'limit': PAGE_SIZE, 'page_token': token})}",
                                 creds)
            if status == 403 and feed == "opra":
                _feed_denied[creds.ident] = _now() + FEED_DENIED_SECONDS
                feed, denied = "indicative", True
                break
            if status in (401, 403):
                raise _fail("auth_failed", 401)
            if status == 429:
                raise _fail("rate_limited", 429)
            if status == 404:
                break
            if status != 200:
                raise _fail("upstream", 502)
            body = _json(text)
            snaps = body.get("snapshots")
            if not isinstance(snaps, dict):
                raise _fail("upstream", 502)
            out.update({k: v for k, v in snaps.items() if isinstance(v, dict)})
            token = body.get("next_page_token") or None
            if not token:
                break
            if page + 1 == MAX_PAGES:
                truncated = True
        if not denied:
            return out, feed, truncated


def _cached(key: tuple, seconds: int, build: Callable[[], Any]) -> Any:
    now = _now()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = build()
    if len(_cache) >= CACHE_MAX:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    _cache[key] = (now + seconds, value)
    return value


# ---------------------------------------------------------------------------------------------- joining

def _quote(snap: dict) -> dict:
    quote = snap.get("latestQuote") if isinstance(snap.get("latestQuote"), dict) else {}
    trade = snap.get("latestTrade") if isinstance(snap.get("latestTrade"), dict) else {}
    daily = snap.get("dailyBar") if isinstance(snap.get("dailyBar"), dict) else {}
    greeks = snap.get("greeks") if isinstance(snap.get("greeks"), dict) else {}
    bid, ask = _num(quote.get("bp")), _num(quote.get("ap"))
    both = bid is not None and ask is not None and bid > 0 and ask > 0
    mid = round((bid + ask) / 2.0, 4) if both else None
    return {"bid": bid, "ask": ask, "mid": mid, "spread_pct": round((ask - bid) / mid * 100.0, 2) if both else None,
            "bid_exchange": quote.get("bx") or None, "ask_exchange": quote.get("ax") or None,
            "last": _num(trade.get("p")), "last_exchange": trade.get("x") or None, "volume": _int(daily.get("v")),
            "delta": _num(greeks.get("delta")), "gamma": _num(greeks.get("gamma")), "theta": _num(greeks.get("theta")),
            "vega": _num(greeks.get("vega")), "iv": _num(snap.get("impliedVolatility")),
            "quote_time": quote.get("t") or None}


def _label_feed(feed: str) -> tuple[str, str, str]:
    """(price_feed, greeks, note) of a feed."""
    if feed == "opra":
        return "opra", "calculated", t("options.note.opra")
    return "indicative", "estimated", t("options.note.indicative")


def _latest(times: list[str | None]) -> str | None:
    found = [x for x in times if x]
    return max(found) if found else None


def _check_equity(symbol: Any) -> str:
    text = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not valid_symbol(text):
        raise _bad("symbol", "a US stock or ETF symbol, for example AAPL")
    if symbol_map.classify(text) != "equity":
        raise _fail("not_equity", 422, text)
    return text


def _underlying_price(symbol: str, desk_id: int | None) -> float | None:
    from hokma.kit import market_tools

    try:
        return _num(market_tools.quote(symbol, desk_id).get("last"))
    except (LookupError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------- the agent tool

def _whole(value: Any, name: str, low: int, high: int | None, default: int | None) -> int | None:
    value = default if value is None else value
    if value is None:
        return None
    rule = f"a whole number from {low} to {high}" if high is not None else f"a whole number of {low} or more"
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        raise _bad(name, rule)
    return value


def _decimal(value: Any, name: str, low: float, high: float | None, *, above: bool = False) -> float | None:
    if value is None:
        return None
    rule = (f"a number from {low:g} to {high:g}" if high is not None else
            (f"a number greater than {low:g}" if above else f"a number of {low:g} or more"))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise _bad(name, rule)
    if value < low or (above and value <= low) or (high is not None and value > high):
        raise _bad(name, rule)
    return float(value)


def _date(value: Any, name: str) -> date:
    try:
        if not isinstance(value, str) or len(value.strip()) != 10:
            raise ValueError
        return date.fromisoformat(value.strip())
    except ValueError:
        raise _bad(name, "a date as YYYY-MM-DD") from None


def options_chain(symbol: Any, expiration: Any = None, type: Any = None, min_days: Any = None, max_days: Any = None,
                  min_delta: Any = None, max_delta: Any = None, min_open_interest: Any = None,
                  max_spread_pct: Any = None, limit: Any = None, desk_id: int | None = None) -> dict:
    """The contracts of one US stock or ETF that pass the filters, with quotes, greeks and open interest."""
    symbol = _check_equity(symbol)
    if type is not None and type not in ("call", "put"):
        raise _bad("type", "call or put")
    exact = _date(expiration, "expiration") if expiration is not None else None
    low_days = _whole(min_days, "min_days", 0, MAX_DAYS, 0)
    high_days = _whole(max_days, "max_days", 0, MAX_DAYS, DEFAULT_MAX_DAYS)
    if exact is None and low_days > high_days:
        raise _bad("min_days", "not greater than max_days")
    low_delta, high_delta = _decimal(min_delta, "min_delta", 0, 1), _decimal(max_delta, "max_delta", 0, 1)
    if low_delta is not None and high_delta is not None and low_delta > high_delta:
        raise _bad("min_delta", "not greater than max_delta")
    low_oi = _whole(min_open_interest, "min_open_interest", 0, None, None)
    top_spread = _decimal(max_spread_pct, "max_spread_pct", 0, None, above=True)
    keep = _whole(limit, "limit", 1, MAX_LIMIT, DEFAULT_LIMIT)

    creds = _credentials(desk_id)
    today = _today()
    window = ({"expiration_date": exact.isoformat()} if exact else
              {"expiration_date_gte": (today + timedelta(days=low_days)).isoformat(),
               "expiration_date_lte": (today + timedelta(days=high_days)).isoformat()})
    params = {"status": "active", **window, "type": type}
    key = ("chain", creds.ident, symbol, tuple(sorted((k, str(v)) for k, v in params.items())))

    def build() -> dict:
        contracts, cut_a = _contract_pages(creds, {"underlying_symbols": symbol, **params})
        snaps, feed, cut_b = _snapshot_pages(creds, symbol, params)
        return {"contracts": contracts, "snaps": snaps, "feed": feed, "truncated": cut_a or cut_b}

    data = _cached(key, CHAIN_SECONDS, build)
    price_feed, greeks, note = _label_feed(data["feed"])
    rows = []
    for contract in data["contracts"]:
        snap = data["snaps"].get(contract["symbol"])
        if snap is None:
            continue                                            # a snapshot without contract is dropped; the reverse has no price
        rows.append({"symbol": contract["symbol"], "type": contract["type"], "strike": contract["strike"],
                     "expiration": contract["expiration"].isoformat(), "days": (contract["expiration"] - today).days,
                     **_quote(snap), "open_interest": contract["open_interest"],
                     "open_interest_date": contract["open_interest_date"]})
    rows.sort(key=lambda r: (r["expiration"], r["strike"], r["type"]))

    def passes(row: dict) -> bool:
        delta = abs(row["delta"]) if row["delta"] is not None else None
        if low_delta is not None and (delta is None or delta < low_delta):
            return False
        if high_delta is not None and (delta is None or delta > high_delta):
            return False
        if low_oi and (row["open_interest"] is None or row["open_interest"] < low_oi):
            return False
        if top_spread is not None and (row["spread_pct"] is None or row["spread_pct"] > top_spread):
            return False
        return True

    matched = [r for r in rows if passes(r)]
    notes = [t("options.note.truncated")] if data["truncated"] else []
    names = ("symbol", "type", "strike", "expiration", "days", "bid", "ask", "mid", "spread_pct", "last", "volume",
             "open_interest", "open_interest_date", "delta", "gamma", "theta", "vega", "iv", "quote_time")
    out = {"underlying": symbol, "underlying_price": _underlying_price(symbol, desk_id), "source": "Alpaca",
           "price_feed": price_feed, "greeks": greeks, "note": note, "market_open": market_open(),
           "as_of": _latest([r["quote_time"] for r in rows]) or _iso(_now()), "scanned": len(rows),
           "matched": len(matched), "notes": notes, "contracts": [{k: r[k] for k in names} for r in matched[:keep]]}
    return _scrubbed(out, creds)


# ---------------------------------------------------------------------------------------------- dashboard: expirations

def expirations(symbol: Any, desk_id: int | None = None) -> dict:
    """The expirations of the next 400 days, from the contracts: date, days and multiplier."""
    symbol = _check_equity(symbol)
    creds = _credentials(desk_id)
    today = _today()

    def build() -> dict:
        contracts, _ = _contract_pages(creds, {
            "underlying_symbols": symbol, "status": "active", "expiration_date_gte": today.isoformat(),
            "expiration_date_lte": (today + timedelta(days=EXPIRATION_DAYS)).isoformat(), "type": "call"})
        found: dict[date, int] = {}
        for c in contracts:
            found.setdefault(c["expiration"], c["size"])
        return {"symbol": symbol, "source": "Alpaca", "expirations": [
            {"date": d.isoformat(), "days": (d - today).days, "multiplier": found[d]} for d in sorted(found)]}

    return _scrubbed(_cached(("expirations", creds.ident, symbol, today.isoformat()), CONTRACT_SECONDS, build), creds)


# ---------------------------------------------------------------------------------------------- dashboard: chain

def _window(strikes: list[float], spot: float | None, count: int | None) -> list[float]:
    """The `count` strikes around the price, half below and half above; a side that does not reach is completed with
    the other. Without a price, the central ones. `None` means all."""
    strikes = sorted(set(strikes))
    if count is None or count >= len(strikes):
        return strikes
    if spot is None:
        start = (len(strikes) - count) // 2
        return strikes[start:start + count]
    below = [s for s in strikes if s < spot][::-1]
    above = [s for s in strikes if s >= spot]
    half = count // 2
    take_below, take_above = min(half, len(below)), min(count - half, len(above))
    take_below = min(len(below), count - take_above)
    take_above = min(len(above), count - take_below)
    return sorted(below[:take_below] + above[:take_above])


def _cell(contract: dict, snap: dict | None, spot: float | None, years: float) -> dict:
    quote = _quote(snap) if snap else {}
    itm = in_the_money(contract["type"], spot, contract["strike"])
    iv = quote.get("iv")
    return {"symbol": contract["symbol"], "itm": itm, "last": quote.get("last"),
            "last_exchange": quote.get("last_exchange"), "bid": quote.get("bid"),
            "bid_exchange": quote.get("bid_exchange"), "ask": quote.get("ask"), "ask_exchange": quote.get("ask_exchange"),
            "volume": quote.get("volume"), "open_interest": contract["open_interest"], "theta": quote.get("theta"),
            "delta": quote.get("delta"), "iv": iv, "intrinsic": intrinsic_value(contract["type"], spot, contract["strike"]),
            "prob_itm": prob_itm(contract["type"], spot, contract["strike"], iv, years),
            "quote_time": quote.get("quote_time")}


def _strike_count(strikes: Any) -> int | None:
    if strikes is None or strikes == "":
        return DEFAULT_STRIKES
    if strikes == "all":
        return None
    try:
        count = int(str(strikes))
    except ValueError:
        raise _bad("strikes", "one of " + ", ".join(str(n) for n in STRIKE_CHOICES) + " or all") from None
    if count not in STRIKE_CHOICES:
        raise _bad("strikes", "one of " + ", ".join(str(n) for n in STRIKE_CHOICES) + " or all")
    return count


def _one_expiration(creds: _Creds, symbol: str, day: date, count: int | None, spot: float | None, today: date) -> dict:
    """Contracts of the day (all strikes, 15 minutes), then the window of strikes, then the snapshots of that window
    only (30 seconds). Returns what `chain` puts in an expiration group, plus the feed used."""
    contracts, cut_a = _cached(("contracts", creds.ident, symbol, day.isoformat()), CONTRACT_SECONDS,
                               lambda: _contract_pages(creds, {"underlying_symbols": symbol, "status": "active",
                                                               "expiration_date": day.isoformat()}))
    if not contracts:
        return {"date": day.isoformat(), "days": (day - today).days, "multiplier": 100, "iv": None,
                "expected_move": None, "rows": [], "feed": None, "truncated": cut_a}
    wanted = _window([c["strike"] for c in contracts], spot, count)
    low, high = wanted[0], wanted[-1]
    spot_key = None if spot is None else round(spot, 2)

    def build() -> tuple[dict[str, dict], str, bool]:
        return _snapshot_pages(creds, symbol, {"expiration_date": day.isoformat(), "strike_price_gte": f"{low:g}",
                                               "strike_price_lte": f"{high:g}"})

    snaps, feed, cut_b = _cached(("snapshots", creds.ident, symbol, day.isoformat(), low, high, spot_key),
                                 CHAIN_SECONDS, build)
    years = years_to_expiry(day)
    by_strike: dict[float, dict] = {s: {"strike": s, "call": None, "put": None} for s in wanted}
    for c in contracts:
        if c["strike"] in by_strike:
            by_strike[c["strike"]][c["type"]] = _cell(c, snaps.get(c["symbol"]), spot, years)
    rows = [by_strike[s] for s in wanted]
    nearest = min(rows, key=lambda r: abs(r["strike"] - spot)) if spot is not None else rows[len(rows) // 2]
    ivs = [c["iv"] for c in (nearest["call"], nearest["put"]) if c and c["iv"] is not None]
    iv = round(sum(ivs) / len(ivs), 4) if ivs else None
    return {"date": day.isoformat(), "days": (day - today).days, "multiplier": contracts[0]["size"], "iv": iv,
            "expected_move": expected_move(spot, iv, years), "rows": rows, "feed": feed, "truncated": cut_a or cut_b}


def chain(symbol: Any, dates: Any, strikes: Any = None, desk_id: int | None = None) -> dict:
    """Up to 4 expirations of one US stock or ETF, calls and puts side by side, for the dashboard."""
    symbol = _check_equity(symbol)
    days = [x.strip() for x in dates.split(",") if x.strip()] if isinstance(dates, str) else list(dates or [])
    if not days or len(days) > MAX_EXPIRATIONS:
        raise _bad("expirations", f"from 1 to {MAX_EXPIRATIONS} dates as YYYY-MM-DD")
    parsed = [_date(x, "expirations") for x in days]
    count = _strike_count(strikes)
    creds = _credentials(desk_id)
    today = _today()
    spot = _underlying_price(symbol, desk_id)
    groups = [_one_expiration(creds, symbol, day, count, spot, today) for day in parsed]
    feed = next((g["feed"] for g in groups if g["feed"]), None) or _feed_state(creds)
    price_feed, greeks, _ = _label_feed(feed)
    notes = [t("options.note.truncated")] if any(g.pop("truncated") for g in groups) else []
    for g in groups:
        g.pop("feed")
    out = {"underlying": symbol, "underlying_price": spot, "source": "Alpaca", "price_feed": price_feed,
           "greeks": greeks, "market_open": market_open(),
           "as_of": _latest([c["quote_time"] for g in groups for r in g["rows"] for c in (r["call"], r["put"]) if c])
           or _iso(_now()), "notes": notes, "expirations": groups}
    return _scrubbed(out, creds)


# ---------------------------------------------------------------------------------------------- dashboard: underlying

def _is_iex(source: Any) -> bool:
    """True when the chart's own source is Alpaca (IEX feed): its volume is one exchange's, not the consolidated one."""
    return isinstance(source, str) and source.strip().lower().startswith("alpaca")


def underlying(symbol: Any) -> dict:
    """The bar above the chain: last, change, bid, ask, sizes, volume, open, high and low. The price, the change, the
    volume, the high and the low are the chart's own (`market_tools.quote`); bid, ask, sizes and exchange letters
    come from the Alpaca stock snapshot on the IEX feed and the open from its daily bar, when Alpaca is connected."""
    from hokma.kit import market_tools

    symbol = _check_equity(symbol)
    try:
        quote = market_tools.quote(symbol)
    except (LookupError, ValueError):
        quote = None
    snap: dict = {}
    creds: _Creds | None = None
    try:
        creds = _credentials(None)
        status, text = _call(f"{DATA_HOST}/v2/stocks/{urllib.parse.quote(symbol, safe='')}/snapshot?feed=iex", creds)
        if status == 200:
            snap = _json(text)
        elif status == 429:
            raise _fail("rate_limited", 429)
    except OptionsError as exc:
        if quote is None or exc.code == "rate_limited":
            raise
    if quote is None and not snap:
        raise _fail("upstream", 502)
    latest_quote = snap.get("latestQuote") if isinstance(snap.get("latestQuote"), dict) else {}
    latest_trade = snap.get("latestTrade") if isinstance(snap.get("latestTrade"), dict) else {}
    daily = snap.get("dailyBar") if isinstance(snap.get("dailyBar"), dict) else {}
    previous = snap.get("prevDailyBar") if isinstance(snap.get("prevDailyBar"), dict) else {}
    q = quote or {}
    last = _num(q.get("last")) if quote else _num(latest_trade.get("p"))
    change = _num(q.get("change")) if quote else (
        round(last - float(previous["c"]), 4) if last is not None and _num(previous.get("c")) is not None else None)
    out = {"symbol": symbol, "last": last, "last_exchange": latest_trade.get("x") or None, "net_change": change,
           "bid": _num(latest_quote.get("bp")), "bid_exchange": latest_quote.get("bx") or None,
           "ask": _num(latest_quote.get("ap")), "ask_exchange": latest_quote.get("ax") or None,
           "bid_size": _int(latest_quote.get("bs")), "ask_size": _int(latest_quote.get("as")),
           "volume": _int(q.get("volume")) if quote and not _is_iex(q.get("source")) else None,
           "open": _num(daily.get("o")),
           "high": _num(q.get("high")) if quote else _num(daily.get("h")),
           "low": _num(q.get("low")) if quote else _num(daily.get("l")),
           "as_of": (q.get("time") and _iso(q["time"])) or latest_quote.get("t") or latest_trade.get("t") or _iso(_now())}
    return _scrubbed(out, creds) if creds else out

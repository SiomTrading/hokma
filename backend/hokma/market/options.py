"""Option chain of US stocks and ETFs, read only, from Schwab or from the Alpaca key the student saved in Connections
(SPEC-014 and SPEC-015).

The source comes from the order of the use "options" of Connections (Schwab first by default). A source that is not
connected, is switched off or is not on the desk is skipped; when the first one fails (an ended session, a limit, a server
error) the next one answers in the same call, and `source` says which one did. Without any, the code is `no_connection`.

**Schwab** (5.6): `GET /marketdata/v1/chains` and `/expirationchain` through `hokma.schwab.endpoints` (the only door to the
host is `schwab/http.py`). Prices and greeks are Schwab's (`price_feed: "schwab"`, `greeks: "provider"`); Prob. ITM and the
intrinsic value are Hokma's, as in 1.0.8. Its failures leave as `schwab.auth`, `schwab.rate_limited` and `schwab.upstream`.
Whether the market is open comes from `GET /markets` (holidays included) when Schwab is connected.

**Alpaca** (two sources, always with the same key; headers only, never in a URL, a log or an answer):

* contracts and open interest: `GET /v2/options/contracts` on the trading host of the key. A demo key (prefix PK) goes to
  `paper-api.alpaca.markets`; a real key (prefix AK) goes to `api.alpaca.markets`. This module is the ONLY code of Hokma that
  talks to the real host, with ONE request: that GET, on that exact path. The host, the path and the method are
  constants here and are checked against `read_exceptions` of `kit/hardblock_brokers.json` before every call. Orders, positions and the
  account are never asked. For an agent the real host stays refused by the hook, with or without this module.
* price, greeks and implied volatility: `GET /v1beta1/options/snapshots/{symbol}` on `data.alpaca.markets`. Feed `opra` first; a 403
  (the plan has no OPRA) repeats the request with `indicative` and remembers the refusal for one hour.

The agent tool (`options_chain`) and the three routes of the dashboard (`market/api.py`) use the functions below. Limits: a
cache of 30 seconds for the chain and 15 minutes for contracts and expirations, at most 5 pages per request, 60 calls a
minute and 2 MB per answer (Alpaca). Nothing here writes anything. Failures raise `OptionsError`, a `LookupError` that is also a
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
from hokma.schwab import endpoints as schwab_endpoints
from hokma.schwab import http as schwab_http
from hokma.schwab import models as schwab_models

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
SCHWAB, ALPACA = "schwab", "alpaca"
DEFAULT_ORDER = (SCHWAB, ALPACA)
NO_FALLBACK = ("bad_request", "not_equity", "desk")       # the next source would refuse the same way
RATE_CODES = ("rate_limited", "schwab.rate_limited")
HOURS_SECONDS = 300
HOURS_FAILED_SECONDS = 60
PHASE_SECONDS = 60
DEFAULT_MULTIPLIER = 100
NO_DATA = -999.0                                         # Schwab's "no data" is this or less (SPEC-015 12, point 9)

Fetch = Callable[[str, dict], tuple[int, str]]
_fetch: Fetch = http_get
_clock: Callable[[], float] = time.time
_cache: dict[tuple, tuple[float, Any]] = {}
_feed_denied: dict[str, float] = {}
_host_memory: dict[str, str] = {}
_limiter: RateLimiter | None = None
_hours_failed_until = 0.0


def reset() -> None:
    """Forget the cache, the limiter, the remembered feed refusals and the remembered hosts (tests, and a new key)."""
    global _limiter, _hours_failed_until
    _cache.clear()
    _feed_denied.clear()
    _host_memory.clear()
    _limiter = None
    _hours_failed_until = 0.0


def forget_cache() -> None:
    """Forget only the short caches (the Refresh button): the limiter and the remembered refusals stay."""
    _cache.clear()


class OptionsError(LookupError, ValueError):
    """What went wrong, in the student's language. `code` is the short code of the dashboard (`no_connection`,
    `auth_failed`, `not_equity`, `bad_request`, `rate_limited`, `upstream`; Schwab's own are `schwab.auth`,
    `schwab.rate_limited` and `schwab.upstream`) and `status` its HTTP status. All of them start with `options.` on the
    wire, so the client never takes one as the end of Hokma's own session."""

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
    if code == "schwab.auth":
        return t("options.err.schwab.auth")
    if code == "schwab.rate_limited":
        return t("options.err.schwab.rate_limited")
    if code == "schwab.upstream":
        return t("options.err.schwab.upstream")
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


def reference_price(last: float | None, bid: float | None, market_open: bool | None) -> float | None:
    """The price of the underlying that intrinsic value, in the money and Prob. ITM are measured against.

    thinkorswim, with the market closed, uses the underlying's BID, not its last price: on Sunday 22-nov-2026 QQQ had last 617.74 and bid 617.50
    and thinkorswim showed intrinsic 6.50 for the 611 call and 0.50 for the 618 put (617.50 - 611 and 618 - 617.50), where Hokma showed 6.74 and
    0.26 (tarea 3328). With the market open, or when it is not known, the last price is used; a closed market with no bid falls back to the last."""
    if market_open is False and bid is not None and bid > 0:
        return bid
    return last


def in_the_money(kind: str, spot: float | None, strike: float) -> bool:
    if spot is None:
        return False
    return strike < spot if kind == "call" else strike > spot


def prob_itm(kind: str, spot: float | None, strike: float, iv: float | None, years: float) -> float | None:
    """Probability of expiring in the money, the way thinkorswim does it (Black-Scholes, tarea 3328):

        d2 = (ln(S / K) - IV^2 * T / 2) / (IV * sqrt(T))        call: N(d2)        put: N(-d2)

    S is the reference price of the underlying (`reference_price`), IV the contract's own implied volatility as Schwab gives it (a fraction),
    T the years to 16:00 New York of the expiration day in minutes over 525600 (`years_to_expiry`), r = 0 and q = 0. Checked on the 22-nov-2026
    evidence: the 23 NOV 618 and 617 calls with Schwab's IV (64.26 % and 63.23 %) give 48.4 % and 50.4 % at S = 617.50 against thinkorswim's
    48.47 % and 50.66 %. At or after the close of the expiration day it is 1 for a contract in the money and 0 for the rest."""
    if spot is None or spot <= 0 or strike <= 0:
        return None
    if years <= 0:
        return 1.0 if in_the_money(kind, spot, strike) else 0.0
    if iv is None or iv <= 0:
        return None
    d2 = (math.log(spot / strike) - iv * iv * years / 2.0) / (iv * math.sqrt(years))
    return round(_cdf(d2 if kind == "call" else -d2), 4)


def expected_move(spot: float | None, iv: float | None, years: float) -> float | None:
    """S x IV x sqrt(T) x sqrt(2 / pi): the average absolute move implied by the volatility of the expiration (the same figure thinkorswim shows
    as +/- on the header of each expiration: its 23 NOV and 24 NOV values give the same T in days at that factor, tarea 3328). S is the reference
    price (`reference_price`), IV the expiration's (`_group`) and T as in `prob_itm`."""
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


def _service():
    from hokma.connections import runtime

    service = runtime.get_service()
    if not getattr(service, "loaded", True):
        service.load()
    return service


def _schwab_ready(service) -> bool:
    """Schwab can answer: keys saved, switched on and a live session (cheap, in memory)."""
    if not service.is_enabled(SCHWAB) or not service.has_key(SCHWAB):
        return False
    said = getattr(service, "signed_in", None)
    if said is not None:
        return bool(said(SCHWAB))
    from hokma.schwab import auth

    return bool(auth.is_connected())


def _providers(desk_id: int | None) -> list[str]:
    """The sources that can serve the chain now, in the order of the use "options" of Connections: connected, switched on
    and (for a desk chart) used by the desk. Raises `no_connection`, or `desk` when one is connected but the desk does not use it."""
    from hokma.connections import runtime

    service = _service()
    order = getattr(service, "order", None)
    wanted = list(order("options")) if order is not None else list(DEFAULT_ORDER)
    allowed = runtime._desk_providers(desk_id) if desk_id is not None else None
    ready: list[str] = []
    blocked = False
    for provider in wanted:
        if provider == SCHWAB:
            usable = _schwab_ready(service)
        elif provider == ALPACA:
            usable = service.is_enabled(ALPACA) and service.has_key(ALPACA)
        else:
            continue
        if not usable:
            continue
        if allowed is not None and provider not in allowed:
            blocked = True
            continue
        ready.append(provider)
    if not ready:
        raise _fail("desk" if blocked else "no_connection", 409)
    return ready


def _serve(providers: list[str], runners: dict[str, Callable[[], Any]]) -> Any:
    """Ask the sources one by one: the first that answers wins. A failure of a source (session, limit, server) goes on to
    the next in the same call; the last failure is the one raised when none answers."""
    last: OptionsError | None = None
    for provider in providers:
        try:
            return runners[provider]()
        except OptionsError as error:
            if error.code in NO_FALLBACK:
                raise
            last = error
    assert last is not None
    raise last


def _credentials(desk_id: int | None = None) -> _Creds:
    """The saved Alpaca key, read for the call that needs it (`_providers` already decided that Alpaca is to be used)."""
    raw = _service().read_secret(ALPACA)
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


# ---------------------------------------------------------------------------------------------- Schwab

def _schwab_error(error: schwab_http.SchwabError) -> OptionsError:
    """A failure of the Schwab door as the short code the dashboard translates (`options.err.schwab.*`). A session that
    ended, none, or an app without access is `schwab.auth` (status 401, with an `options.` code so the client does not
    close Hokma's own session); a limit is 429; anything else is 502."""
    if error.code in (schwab_http.EXPIRED, schwab_http.NOT_CONNECTED, schwab_http.DENIED):
        return OptionsError("schwab.auth", 401, _text("schwab.auth"))
    if error.code == schwab_http.RATE_LIMITED:
        return OptionsError("schwab.rate_limited", 429, _text("schwab.rate_limited"))
    return OptionsError("schwab.upstream", 502, _text("schwab.upstream"))


def _schwab_call(fn: Callable[[], Any], empty: Any = None) -> Any:
    """Run a call of `hokma.schwab.endpoints`; `schwab.err.not_found` is no data (`empty`), the rest is an OptionsError."""
    try:
        return fn()
    except schwab_http.SchwabError as error:
        if error.code == schwab_http.NOT_FOUND:
            return empty
        raise _schwab_error(error) from None


def _clean(value: Any) -> float | None:
    """A number, or None for a non finite value and for Schwab's "no data" (-999 or less)."""
    number = _num(value)
    return None if number is None or number <= NO_DATA else number


def _iv(value: Any) -> float | None:
    """`volatility` of a contract as a fraction (the unit is `analytics.VOLATILITY_UNIT`, fixed with the first real
    answer, T0)."""
    from hokma.schwab import analytics

    number = _clean(value)
    if number is None:
        return None
    return round(number / 100.0, 6) if analytics.VOLATILITY_UNIT == "percent" else number


def _whole_of(value: Any) -> int | None:
    """`openInterest` and the sizes come as doubles: a whole number, or None."""
    number = _clean(value)
    return None if number is None else int(round(number))


def _iso_ms(value: Any) -> str | None:
    """`quoteTimeInLong` and the like: milliseconds as a 64 bit integer (the REF says int32 and is wrong) to a UTC text."""
    number = _clean(value)
    if number is None or number <= 0:
        return None
    try:
        return _iso(int(number) / 1000.0)
    except (OverflowError, OSError, ValueError):
        return None


def _day_of(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _first(item: dict, *names: str) -> float | None:
    """The first of these keys of a Schwab contract that carries a number. The REF (A.6) names the prices `bidPrice`, `askPrice`,
    `lastPrice` and `markPrice`, but the real `/chains` answer sends `bid`, `ask`, `last` and `mark` (measured 22-nov-2026 with the
    diagnostic, tarea 3328): both spellings are read, the real one first. Reading only the REF's names left every Bid, Ask and Last as N/A."""
    for name in names:
        number = _clean(item.get(name))
        if number is not None:
            return number
    return None


def traded(value: float | None) -> float | None:
    """A last price of 0 is Schwab's way of saying "no trade yet" (the 22-nov-2026 chain brings `last: 0` beside a real bid and ask): no value."""
    return value if value is not None and value > 0 else None


def _schwab_quote(item: dict) -> dict:
    """One OptionContract (REF A.6) as the quote of 1.0.8: the same keys as `_quote`. Out of hours it carries the last values Schwab holds."""
    bid, ask = _first(item, "bid", "bidPrice"), _first(item, "ask", "askPrice")
    both = bid is not None and ask is not None and bid > 0 and ask > 0
    mid = round((bid + ask) / 2.0, 4) if both else None
    return {"bid": bid, "ask": ask, "mid": mid, "spread_pct": round((ask - bid) / mid * 100.0, 2) if both else None,
            "bid_exchange": None, "ask_exchange": None, "last": traded(_first(item, "last", "lastPrice")), "last_exchange": None,
            "volume": _whole_of(item.get("totalVolume")), "delta": _clean(item.get("delta")),
            "gamma": _clean(item.get("gamma")), "theta": _clean(item.get("theta")), "vega": _clean(item.get("vega")),
            "iv": _iv(item.get("volatility")), "quote_time": _iso_ms(item.get("quoteTimeInLong"))}


def _schwab_contracts(data: dict) -> list[tuple[dict, dict]]:
    """Every contract of a chain as `(contract, quote)`: the contract in the form `_contract` gives for Alpaca (the open
    interest date is None: Schwab does not say it, 12 point 18) and the quote of `_schwab_quote`. The expiration is the date
    of the map key `yyyy-MM-dd:days`, else `expirationDate`, else `expiration`."""
    found: list[tuple[dict, dict]] = []
    for kind, name in (("call", "callExpDateMap"), ("put", "putExpDateMap")):
        for key, strikes in (data.get(name) or {}).items():
            if not isinstance(strikes, dict):
                continue
            for strike_key, items in strikes.items():
                for item in items if isinstance(items, list) else [items]:
                    if not isinstance(item, dict):
                        continue
                    day = (_day_of(str(key).split(":")[0]) or _day_of(item.get("expirationDate"))
                           or _day_of(item.get("expiration")))
                    strike = _clean(item.get("strikePrice"))
                    if strike is None:
                        strike = _clean(strike_key)
                    symbol = item.get("symbol")
                    if day is None or strike is None or not isinstance(symbol, str) or not symbol:
                        continue
                    put_call = str(item.get("putCall") or "").upper()
                    contract = {"symbol": symbol, "type": {"CALL": "call", "PUT": "put"}.get(put_call, kind),
                                "strike": strike, "expiration": day, "size": _whole_of(item.get("multiplier")) or DEFAULT_MULTIPLIER,
                                "open_interest": _whole_of(item.get("openInterest")), "open_interest_date": None,
                                "in_money": item.get("isInTheMoney") if isinstance(item.get("isInTheMoney"), bool) else None}
                    found.append((contract, _schwab_quote(item)))
    return found


def _schwab_spot(data: dict) -> float | None:
    """The price of the underlying from the chain: `underlying.last`, else its `mark`, else `underlyingPrice`."""
    under = data.get("underlying") if isinstance(data.get("underlying"), dict) else {}
    for value in (under.get("last"), under.get("mark"), data.get("underlyingPrice")):
        number = _clean(value)
        if number is not None and number > 0:
            return number
    return None


def _schwab_delayed(data: dict) -> bool:
    under = data.get("underlying") if isinstance(data.get("underlying"), dict) else {}
    return data.get("isDelayed") is True or under.get("delayed") is True


def _schwab_chain(symbol: str, **params: Any) -> dict:
    """`GET /chains` for the symbol, as JSON (an empty one when Schwab has no chain for it), cached 30 seconds."""
    key = ("schwab-chain", symbol, tuple(sorted((k, str(v)) for k, v in params.items())))

    def build() -> dict:
        data = _schwab_call(lambda: schwab_models.dump(schwab_endpoints.option_chain(symbol, **params)), {})
        return data if isinstance(data, dict) else {}

    return _cached(key, 0, build)                        # SPEC-016 4.3: the structure is asked each time, no 30 second memory


def _market_hours_open() -> bool | None:
    """16.15: is the regular session open now, by `GET /markets` (options, else equity) with the holidays. None when Schwab
    cannot say (no session, a failure, a payload without the product): the answer is then "unknown" (None), never a fixed 09:30 to 16:00."""
    global _hours_failed_until
    from hokma.schwab import analytics

    now, today = _now(), _today().isoformat()
    if _hours_failed_until > now:
        return None
    for market, product in (("option", "EQO"), ("equity", "EQ")):
        try:
            payload = _cached(("hours", market, today), HOURS_SECONDS,
                              lambda m=market: schwab_models.dump(schwab_endpoints.market_hours(m, date=today)))
        except Exception:                                   # noqa: BLE001  (a label: whatever fails, the plain clock answers)
            _hours_failed_until = now + HOURS_FAILED_SECONDS
            return None
        section = payload.get(market) if isinstance(payload, dict) else None
        if isinstance(section, dict) and product in section:
            return bool(analytics.market_hours(payload, now, market, product)["is_open"])
    return None


def _alpaca_open(creds: "_Creds") -> bool | None:
    """Is the regular session open now, by Alpaca's own clock (`phase` of `/v3/clock`): `core` is open, `closed`, `pre` and `post` are not. None when
    Alpaca does not say. Hokma has no hours of its own, so an answer that is not there is "unknown" and never a guess."""
    from hokma.market import alpaca_hours

    def build() -> str | None:
        return alpaca_hours.phase(_fetch, creds.headers, creds.hosts())

    phase = _cached(("alpaca-phase", creds.ident), PHASE_SECONDS, build)
    if phase is None:
        return None
    return phase == "core"


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


def _label_feed(feed: str) -> tuple[str, str]:
    """(price_feed, greeks) of a feed."""
    return ("opra", "calculated") if feed == "opra" else ("indicative", "estimated")


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


def _hub_quote(symbol: str):
    try:
        from hokma.connections import runtime

        return runtime.get_hub().quote_sync(symbol)
    except Exception:                                    # noqa: BLE001  (no hub or no live source: the REST quote answers)
        return None


def _hub_spot(symbol: str) -> float | None:
    """The last price of the underlying that the live source of the hub has (the same Quote the chart shows), or None."""
    try:
        from hokma.connections import runtime

        return runtime.get_hub().spot_sync(symbol)
    except Exception:                                    # noqa: BLE001  (no hub or no live source: the chart's own price answers)
        return None


def _underlying_price(symbol: str, desk_id: int | None) -> float | None:
    from hokma.kit import market_tools

    live = _hub_spot(symbol)
    if live is not None:
        return live
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

    providers = _providers(desk_id)
    today = _today()

    def from_schwab() -> dict:
        window = ({"fromDate": exact.isoformat(), "toDate": exact.isoformat()} if exact else
                  {"fromDate": (today + timedelta(days=low_days)).isoformat(),
                   "toDate": (today + timedelta(days=high_days)).isoformat()})
        data = _schwab_chain(symbol, contractType=(type or "all").upper(), strategy="SINGLE",
                             includeUnderlyingQuote=True, **window)
        rows = [{"symbol": c["symbol"], "type": c["type"], "strike": c["strike"],
                 "expiration": c["expiration"].isoformat(), "days": (c["expiration"] - today).days, **q,
                 "open_interest": c["open_interest"], "open_interest_date": c["open_interest_date"]}
                for c, q in _schwab_contracts(data)]
        spot = _schwab_spot(data)
        return {"source": "Schwab", "price_feed": "schwab", "greeks": "provider", "note": None, "rows": rows,
                "truncated": False, "spot": spot if spot is not None else _underlying_price(symbol, desk_id),
                "as_of": (data.get("underlying") or {}).get("quoteTime"), "delayed": _schwab_delayed(data),
                "market_open": _market_hours_open(), "creds": None}

    def from_alpaca() -> dict:
        creds = _credentials(desk_id)
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
        price_feed, greeks = _label_feed(data["feed"])
        note = t("options.note.opra") if price_feed == "opra" else None          # no fixed text for the free plan (decision 226)
        rows = []
        for contract in data["contracts"]:
            snap = data["snaps"].get(contract["symbol"])
            if snap is None:
                continue                                        # a snapshot without contract is dropped; the reverse has no price
            rows.append({"symbol": contract["symbol"], "type": contract["type"], "strike": contract["strike"],
                         "expiration": contract["expiration"].isoformat(), "days": (contract["expiration"] - today).days,
                         **_quote(snap), "open_interest": contract["open_interest"],
                         "open_interest_date": contract["open_interest_date"]})
        return {"source": "Alpaca", "price_feed": price_feed, "greeks": greeks, "note": note, "rows": rows,
                "truncated": data["truncated"], "spot": _underlying_price(symbol, desk_id), "as_of": None,
                "market_open": _alpaca_open(creds), "creds": creds}

    got = _serve(providers, {SCHWAB: from_schwab, ALPACA: from_alpaca})
    rows = got["rows"]
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
    notes = [t("options.note.truncated")] if got["truncated"] else []
    names = ("symbol", "type", "strike", "expiration", "days", "bid", "ask", "mid", "spread_pct", "last", "volume",
             "open_interest", "open_interest_date", "delta", "gamma", "theta", "vega", "iv", "quote_time")
    out = {"underlying": symbol, "underlying_price": got["spot"], "source": got["source"],
           "price_feed": got["price_feed"], "greeks": got["greeks"], "note": got["note"],
           "market_open": got["market_open"],
           "as_of": _latest([r["quote_time"] for r in rows]) or _iso_ms(got["as_of"]) or _iso(_now()),
           "scanned": len(rows), "matched": len(matched), "notes": notes,
           "contracts": [{k: r[k] for k in names} for r in matched[:keep]]}
    if got["source"] == "Schwab":
        out["delayed"] = got["delayed"]                         # "Con retraso" (5.6): isDelayed or underlying.delayed
    return _scrubbed(out, got["creds"]) if got["creds"] else out


# ---------------------------------------------------------------------------------------------- dashboard: expirations

def expirations(symbol: Any, desk_id: int | None = None) -> dict:
    """The expirations of the next 400 days: date, days and multiplier (Schwab's `/expirationchain`, or the Alpaca contracts)."""
    symbol = _check_equity(symbol)
    providers = _providers(desk_id)
    today = _today()

    def from_schwab() -> dict:
        def build() -> dict:
            data = _schwab_call(lambda: schwab_models.dump(schwab_endpoints.expirations(symbol)), {})
            found: dict[date, int] = {}
            for item in (data.get("expirationList") if isinstance(data, dict) else None) or []:
                if not isinstance(item, dict):
                    continue
                day = _day_of(item.get("expiration")) or _day_of(item.get("expirationDate"))     # the REF has both names
                if day is not None and 0 <= (day - today).days <= EXPIRATION_DAYS:
                    found.setdefault(day, DEFAULT_MULTIPLIER)
            return {"symbol": symbol, "source": "Schwab", "expirations": [
                {"date": d.isoformat(), "days": (d - today).days, "multiplier": found[d]} for d in sorted(found)]}

        return _cached(("schwab-expirations", symbol, today.isoformat()), CONTRACT_SECONDS, build)

    def from_alpaca() -> dict:
        creds = _credentials(desk_id)

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

    return _serve(providers, {SCHWAB: from_schwab, ALPACA: from_alpaca})


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


def _cell(contract: dict, quote: dict, spot: float | None, years: float) -> dict:
    """One side of a strike. `quote` is `_quote(snapshot)` (Alpaca), `_schwab_quote(contract)` (Schwab) or empty (no price)."""
    itm = in_the_money(contract["type"], spot, contract["strike"]) if spot is not None else bool(contract.get("in_money"))
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
            snap = snaps.get(c["symbol"])
            by_strike[c["strike"]][c["type"]] = _cell(c, _quote(snap) if snap else {}, spot, years)
    return _group(day, today, contracts[0]["size"], [by_strike[s] for s in wanted], spot, years, feed, cut_a or cut_b)


def _group(day: date, today: date, multiplier: int, rows: list[dict], spot: float | None, years: float,
           feed: str | None, truncated: bool) -> dict:
    """An expiration group of the chain: its rows, the IV of the strike nearest the price (the mean of its call and put)
    and the expected move."""
    nearest = min(rows, key=lambda r: abs(r["strike"] - spot)) if spot is not None else rows[len(rows) // 2]
    ivs = [c["iv"] for c in (nearest["call"], nearest["put"]) if c and c["iv"] is not None]
    iv = round(sum(ivs) / len(ivs), 4) if ivs else None
    return {"date": day.isoformat(), "days": (day - today).days, "multiplier": multiplier, "iv": iv,
            "expected_move": expected_move(spot, iv, years), "rows": rows, "feed": feed, "truncated": truncated}


def _schwab_group(symbol: str, day: date, count: int | None, spot: float | None, today: date, market_open: bool | None = None,
                  bid: float | None = None) -> tuple[dict, dict]:
    """The group of one expiration from Schwab: ONE `/chains` call for that day (`strikeCount` strikes above and below
    the money, then trimmed to `count` around the price). Returns the group and the chain JSON (for the underlying)."""
    data = _schwab_chain(symbol, contractType="ALL", strategy="SINGLE", includeUnderlyingQuote=True,
                         fromDate=day.isoformat(), toDate=day.isoformat())          # no strikeCount: every strike Schwab has (SPEC-016 4.3)
    found = [(c, q) for c, q in _schwab_contracts(data) if c["expiration"] == day]
    if not found:
        return {"date": day.isoformat(), "days": (day - today).days, "multiplier": DEFAULT_MULTIPLIER, "iv": None,
                "expected_move": None, "rows": [], "feed": None, "truncated": False}, data
    spot = spot if spot is not None else _schwab_spot(data)      # the centre is the underlying price of the stream, then the answer's own
    under = data.get("underlying") if isinstance(data.get("underlying"), dict) else {}
    ref = reference_price(spot, bid if bid is not None else _clean(under.get("bid")), market_open)     # what intrinsic, ITM and Prob. ITM are measured against
    wanted = _window([c["strike"] for c, _ in found], spot, count)
    by_strike: dict[float, dict] = {s: {"strike": s, "call": None, "put": None} for s in wanted}
    years = years_to_expiry(day)
    for contract, quote in found:
        if contract["strike"] in by_strike:
            by_strike[contract["strike"]][contract["type"]] = _cell(contract, quote, ref, years)
    group = _group(day, today, found[0][0]["size"], [by_strike[s] for s in wanted], ref, years, SCHWAB, False)
    return group, data


def structure(symbol: Any, dates: Any, spot: float | None = None, provider: str = SCHWAB) -> dict:
    """The whole structure of up to 4 expirations of a stock or ETF from Schwab, every strike, for the live chain (SPEC-016 4.3): the groups
    with their rows and the first values of `/chains`. Prices move afterwards by the Streamer, not by asking again."""
    symbol = _check_equity(symbol)
    days = [x.strip() for x in dates.split(",") if x.strip()] if isinstance(dates, str) else list(dates or [])
    if not days or len(days) > MAX_EXPIRATIONS:
        raise _bad("expirations", f"from 1 to {MAX_EXPIRATIONS} dates as YYYY-MM-DD")
    today = _today()
    groups, delayed = [], False
    creds = _credentials(None) if provider == ALPACA else None
    market_open = _market_hours_open() if provider == SCHWAB else None
    for day in [_date(x, "expirations") for x in days]:
        if provider == ALPACA:                                  # every strike of the day, with the first prices of the snapshot (indicative on the free plan)
            group, data = _one_expiration(creds, symbol, day, None, spot, today), {}
        else:
            group, data = _schwab_group(symbol, day, None, spot, today, market_open)
        group.pop("truncated", None)
        group.pop("feed", None)
        groups.append(group)
        delayed = delayed or _schwab_delayed(data)
    return {"underlying": symbol, "groups": groups, "delayed": delayed, "today": today.isoformat(), "market_open": market_open}


def chain(symbol: Any, dates: Any, strikes: Any = None, desk_id: int | None = None) -> dict:
    """Up to 4 expirations of one US stock or ETF, calls and puts side by side, for the dashboard."""
    symbol = _check_equity(symbol)
    days = [x.strip() for x in dates.split(",") if x.strip()] if isinstance(dates, str) else list(dates or [])
    if not days or len(days) > MAX_EXPIRATIONS:
        raise _bad("expirations", f"from 1 to {MAX_EXPIRATIONS} dates as YYYY-MM-DD")
    parsed = [_date(x, "expirations") for x in days]
    count = _strike_count(strikes)
    providers = _providers(desk_id)
    today = _today()

    def from_schwab() -> dict:
        live_spot = _hub_spot(symbol)                       # the centre is the price of the stream; without one, the answer's own
        streamed = _hub_quote(symbol)
        bid = streamed.bid if streamed is not None else None
        market_open = _market_hours_open()
        groups, answers = [], []
        for day in parsed:
            group, data = _schwab_group(symbol, day, count, live_spot, today, market_open, bid)
            groups.append(group)
            answers.append(data)
        spot = live_spot if live_spot is not None else next((p for p in (_schwab_spot(d) for d in answers) if p is not None), None)
        if spot is None:
            spot = _underlying_price(symbol, desk_id)
        notes = [t("options.note.truncated")] if any(g.pop("truncated") for g in groups) else []
        for g in groups:
            g.pop("feed")
        times = [c["quote_time"] for g in groups for r in g["rows"] for c in (r["call"], r["put"]) if c]
        under_time = next((_iso_ms(d["underlying"].get("quoteTime")) for d in answers
                           if isinstance(d.get("underlying"), dict) and d["underlying"].get("quoteTime")), None)
        return {"underlying": symbol, "underlying_price": spot, "source": "Schwab", "price_feed": "schwab",
                "greeks": "provider", "market_open": market_open, "as_of": _latest(times) or under_time or _iso(_now()),
                "notes": notes, "delayed": any(_schwab_delayed(d) for d in answers), "expirations": groups}

    def from_alpaca() -> dict:
        creds = _credentials(desk_id)
        spot = _underlying_price(symbol, desk_id)
        groups = [_one_expiration(creds, symbol, day, count, spot, today) for day in parsed]
        feed = next((g["feed"] for g in groups if g["feed"]), None) or _feed_state(creds)
        price_feed, greeks = _label_feed(feed)
        notes = [t("options.note.truncated")] if any(g.pop("truncated") for g in groups) else []
        for g in groups:
            g.pop("feed")
        out = {"underlying": symbol, "underlying_price": spot, "source": "Alpaca", "price_feed": price_feed,
               "greeks": greeks, "market_open": _alpaca_open(creds),
               "as_of": _latest([c["quote_time"] for g in groups for r in g["rows"] for c in (r["call"], r["put"]) if c])
               or _iso(_now()), "notes": notes, "expirations": groups}
        return _scrubbed(out, creds)

    return _serve(providers, {SCHWAB: from_schwab, ALPACA: from_alpaca})


# ---------------------------------------------------------------------------------------------- dashboard: underlying

def _is_iex(source: Any) -> bool:
    """True when the chart's own source is Alpaca (IEX feed): its volume is one exchange's, not the consolidated one."""
    return isinstance(source, str) and source.strip().lower().startswith("alpaca")


def _schwab_bar(symbol: str) -> dict:
    """The bar above the chain from `GET /quotes` (the QuoteEquity of REF A.4): last, change, bid, ask, sizes, volume, open,
    high and low, and `delayed` when Schwab says `realtime: false`."""
    from hokma.market.adapters.schwab import map_quote

    streamed = _hub_quote(symbol)
    if streamed is not None and streamed.last is not None:          # the quote of the stream, the same one the chart shows (SPEC-016 5.7)
        net = streamed.last - streamed.prev_close if streamed.prev_close else None
        return {"symbol": symbol, "last": streamed.last, "last_exchange": None, "net_change": net, "bid": streamed.bid, "bid_exchange": None,
                "ask": streamed.ask, "ask_exchange": None, "bid_size": _whole_of(streamed.bid_size), "ask_size": _whole_of(streamed.ask_size),
                "volume": _whole_of(streamed.volume), "open": streamed.open, "high": streamed.high, "low": streamed.low,
                "as_of": _iso(streamed.ts_ms / 1000), "delayed": streamed.delayed}
    answer = _schwab_call(lambda: schwab_models.dump(schwab_endpoints.quotes(symbol, fields="quote")), {})
    item = answer.get(symbol) if isinstance(answer, dict) else None
    if not isinstance(item, dict):
        raise OptionsError("schwab.upstream", 502, _text("schwab.upstream"))
    q = map_quote(item)
    stamp = q["quote_time"] or q["trade_time"]
    return {"symbol": symbol, "last": _clean(q["last"]), "last_exchange": None, "net_change": _clean(q["change"]),
            "bid": _clean(q["bid"]), "bid_exchange": None, "ask": _clean(q["ask"]), "ask_exchange": None,
            "bid_size": _whole_of(q["bid_size"]), "ask_size": _whole_of(q["ask_size"]),
            "volume": _whole_of(q["volume"]), "open": _clean(q["open"]), "high": _clean(q["high"]),
            "low": _clean(q["low"]), "as_of": _iso_ms(stamp) or _iso(_now()), "delayed": q["delayed"]}


def underlying(symbol: Any) -> dict:
    """The bar above the chain: last, change, bid, ask, sizes, volume, open, high and low. From Schwab (`/quotes`) when it is
    the source; with Alpaca, the price, the change, the volume, the high and the low are the chart's own
    (`market_tools.quote`) and bid, ask, sizes and exchange letters come from the Alpaca stock snapshot on the IEX feed and
    the open from its daily bar. A source that fails goes on to the next; with none the chart's own numbers stand."""
    from hokma.kit import market_tools

    symbol = _check_equity(symbol)
    try:
        quote = market_tools.quote(symbol)
    except (LookupError, ValueError):
        quote = None
    snap: dict = {}
    creds: _Creds | None = None
    try:
        last_error: OptionsError | None = None
        for provider in _providers(None):
            try:
                if provider == SCHWAB:
                    return _schwab_bar(symbol)
                creds = _credentials(None)
                status, text = _call(f"{DATA_HOST}/v2/stocks/{urllib.parse.quote(symbol, safe='')}/snapshot?feed=iex", creds)
                if status == 200:
                    snap = _json(text)
                elif status == 429:
                    raise _fail("rate_limited", 429)
                last_error = None
                break
            except OptionsError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
    except OptionsError as exc:
        if quote is None or exc.code in RATE_CODES:
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

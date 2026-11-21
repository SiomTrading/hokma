"""Candles from the providers the student connected, in the order set in Connections. Yahoo is only the last fallback.

`ConnectedSource` is a `MarketDataSource`: for a symbol it takes the asset class, lists the providers that can serve
bars for that class (the order of Connections first, then the ones that only serve bars), keeps the ones that are
switched on and have their key (and, for a desk chart, the ones the desk uses) and asks them one by one. A provider
that answers nothing, fails, has no access (403) or is rate limited is skipped without an error; when none serves the
bars, the fallback source (Yahoo) does. `Series.source` carries the name of the one that really served.

Every answer goes through the disk cache of the market (same rules as Yahoo) and each provider has its own request
limiter and a short memory of its failures, so a refused provider is not asked again on every poll.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hokma.core import tls
from hokma.market import symbols as symbol_map
from hokma.market.cache import DiskCache, RateLimiter
from hokma.market.errors import BAD_RESPONSE, NETWORK, RATE_LIMITED, UNKNOWN_SYMBOL, UPSTREAM, MarketError
from hokma.market.models import TIMEFRAME_SECONDS, Candle, Series, SessionPeriod
from hokma.market.providers import ProviderCatalog
from hokma.market.sessions import session_for
from hokma.market.source import MarketDataSource
from hokma.market.tz import NEW_YORK

TIMEOUT_SECONDS = 8
MAX_BODY_BYTES = 16 * 1024 * 1024
# Seconds a cached answer is fresh (the same as Yahoo) and how long a failure or a refusal is remembered.
FRESH_FOR = {"1m": 20, "5m": 45, "15m": 90, "1h": 240, "1D": 900}
FAIL_MEMORY = 30
DENIED_MEMORY = 3600
# How far back each base asks (the longest the chart needs; Yahoo's own spans).
SPAN_DAYS = {"1m": 7, "5m": 60, "15m": 60, "1h": 730, "1D": 3650}
COINBASE_PAGE = 300
COINBASE_MAX_PAGES = 4                 # at most 1200 bars per answer: no storm of requests for a week of minutes
# Asset classes each provider serves bars for. It is not the live catalog: Alpaca streams only stocks but its bars
# also cover crypto. A provider without an entry here has no bars and is never asked.
HISTORY_CLASSES = {
    "alpaca": ("equity", "crypto"),
    "coinbase": ("crypto",),
    "finnhub": ("equity", "forex", "crypto"),
}
# Providers that serve bars but are not in the order of Connections for the class, by class, in the order they go last.
EXTRA_ORDER = {"crypto": ("alpaca",), "equity": ("alpaca",)}

HttpGet = Callable[[str, dict], tuple[int, str]]
Allowed = Callable[[str], bool] | None


class _KeepKeysOnHost(urllib.request.HTTPRedirectHandler):
    """A redirect to another host does not carry the provider's key headers (APCA-*)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            for store in (new.headers, new.unredirected_hdrs):
                for name in [n for n in store if n.lower().startswith("apca-")]:
                    del store[name]
        return new


def http_get(url: str, headers: dict, body: bytes | None = None) -> tuple[int, str]:
    """GET (POST with a body) `url` over `tls.outbound_context`. Returns (status, text); raises MarketError(network)."""
    request = urllib.request.Request(url, data=body, headers={"User-Agent": "Mozilla/5.0 (compatible; Hokma)",
                                                              "Accept": "application/json", **headers})
    try:
        with tls.opener(_KeepKeysOnHost).open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read(MAX_BODY_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            text = error.read(MAX_BODY_BYTES).decode("utf-8", "replace")
        except OSError:
            text = ""
        return error.code, text
    except (urllib.error.URLError, TimeoutError, OSError):
        raise MarketError(NETWORK, 502) from None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        raise MarketError(BAD_RESPONSE, 502) from None


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _candle(t, o, h, l, c, v) -> Candle | None:
    values = [_number(x) for x in (o, h, l, c)]
    if not isinstance(t, int) or None in values:
        return None
    return Candle(t, *values, _number(v) or 0.0)         # type: ignore[arg-type]


def _regular_hours(candles: list[Candle], base: str) -> list[Candle]:
    """Stock bars of the regular session only (09:30 to 16:00 New York), like Yahoo: the session grid of the chart
    starts at the open, so pre and after market bars would fall in the wrong session."""
    if base == "1D":
        return candles
    out = []
    for candle in candles:
        local = datetime.fromtimestamp(candle.t, NEW_YORK)
        minute = local.hour * 60 + local.minute
        if local.weekday() < 5 and 570 <= minute < 960:
            out.append(candle)
    return out


# ---------- one request builder and one parser per provider ----------

class Denied(Exception):
    """The provider answered that this account has no access (401, 402, 403)."""


def _check(status: int) -> None:
    if status in (401, 402, 403):
        raise Denied
    if status == 404:
        raise MarketError(UNKNOWN_SYMBOL, 404)
    if status == 429:
        raise MarketError(RATE_LIMITED, 429)
    if status != 200:
        raise MarketError(UPSTREAM, 502)


def _alpaca(fetch: HttpGet, secret: str, symbol: str, asset: str, base: str, now: float) -> list[Candle]:
    keys = _json(secret)
    headers = {"APCA-API-KEY-ID": keys["key_id"], "APCA-API-SECRET-KEY": keys["secret"]}
    timeframe = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour", "1D": "1Day"}[base]
    params = {"timeframe": timeframe, "start": _iso(now - SPAN_DAYS[base] * 86400), "limit": 10000, "sort": "asc"}
    if asset == "crypto":
        match = symbol_map.CRYPTO.match(symbol)
        if not match or match.group(2) not in ("USD", "USDT", "USDC", "BTC"):
            raise MarketError(UNKNOWN_SYMBOL, 404)
        pair = f"{match.group(1)}/{match.group(2)}"
        url = "https://data.alpaca.markets/v1beta3/crypto/us/bars?" + urllib.parse.urlencode({**params, "symbols": pair})
    else:
        mapped = symbol_map.to_provider("alpaca", symbol, asset)
        if mapped is None:
            raise MarketError(UNKNOWN_SYMBOL, 404)
        url = (f"https://data.alpaca.markets/v2/stocks/{urllib.parse.quote(mapped, safe='')}/bars?"
               + urllib.parse.urlencode({**params, "feed": "iex", "adjustment": "raw"}))
    status, text = fetch(url, headers)
    _check(status)
    data = _json(text)
    bars = data.get("bars") if isinstance(data, dict) else None
    if isinstance(bars, dict):
        bars = bars.get(pair) if asset == "crypto" else None
    if not isinstance(bars, list):
        raise MarketError(BAD_RESPONSE, 502)
    out = []
    for bar in bars:
        try:
            stamp = int(datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00")).timestamp())
        except (KeyError, ValueError):
            continue
        item = _candle(stamp, bar.get("o"), bar.get("h"), bar.get("l"), bar.get("c"), bar.get("v"))
        if item:
            out.append(item)
    return _regular_hours(out, base) if asset == "equity" else out


COINBASE_GRANULARITY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1D": 86400}


def _coinbase(fetch: HttpGet, secret: str, symbol: str, asset: str, base: str, now: float) -> list[Candle]:
    step = COINBASE_GRANULARITY[base]
    end = int(now)
    stop = end - SPAN_DAYS[base] * 86400
    found: dict[int, Candle] = {}
    for _ in range(COINBASE_MAX_PAGES):
        start = max(stop, end - COINBASE_PAGE * step)
        query = urllib.parse.urlencode({"granularity": step, "start": _iso(start), "end": _iso(end)})
        status, text = fetch(f"https://api.exchange.coinbase.com/products/{urllib.parse.quote(symbol, safe='')}/candles?{query}", {})
        _check(status)
        rows = _json(text)
        if not isinstance(rows, list):
            raise MarketError(BAD_RESPONSE, 502)
        for row in rows:
            if isinstance(row, list) and len(row) >= 6:
                item = _candle(row[0], row[3], row[2], row[1], row[4], row[5])     # time, low, high, open, close, volume
                if item:
                    found[item.t] = item
        if not rows or start <= stop:
            break
        end = start
    return sorted(found.values(), key=lambda candle: candle.t)


FINNHUB_RESOLUTION = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "1D": "D"}


def _finnhub(fetch: HttpGet, secret: str, symbol: str, asset: str, base: str, now: float) -> list[Candle]:
    mapped = symbol_map.to_provider("finnhub", symbol, asset)
    if mapped is None:
        raise MarketError(UNKNOWN_SYMBOL, 404)
    kind = {"equity": "stock", "crypto": "crypto", "forex": "forex"}[asset]
    query = urllib.parse.urlencode({"symbol": mapped, "resolution": FINNHUB_RESOLUTION[base],
                                    "from": int(now - SPAN_DAYS[base] * 86400), "to": int(now)})
    status, text = fetch(f"https://finnhub.io/api/v1/{kind}/candle?{query}", {"X-Finnhub-Token": secret})
    _check(status)
    data = _json(text)
    if not isinstance(data, dict) or data.get("s") == "no_data":
        raise MarketError(UNKNOWN_SYMBOL, 404)
    columns = [data.get(key) for key in ("t", "o", "h", "l", "c", "v")]
    if data.get("s") != "ok" or not all(isinstance(column, list) for column in columns[:5]):
        raise MarketError(BAD_RESPONSE, 502)
    volumes = columns[5] if isinstance(columns[5], list) else []
    out = []
    for index, stamp in enumerate(columns[0]):
        try:
            item = _candle(stamp, columns[1][index], columns[2][index], columns[3][index], columns[4][index],
                           volumes[index] if index < len(volumes) else 0)
        except IndexError:
            continue
        if item:
            out.append(item)
    return _regular_hours(out, base) if asset == "equity" else out


FETCHERS = {"alpaca": _alpaca, "coinbase": _coinbase, "finnhub": _finnhub}


# ---------- the series around the bars ----------

def _kind_of(asset: str) -> str:
    return {"future": "future", "crypto": "crypto", "forex": "currency"}.get(asset, "equity")


def _session_period(kind: str, now: int) -> SessionPeriod | None:
    """The regular session that is current or next, like Yahoo's `currentTradingPeriod`, from the instrument kind."""
    if kind == "crypto":
        start = now - now % 86400
        return SessionPeriod(start, start + 86400)
    session = session_for(kind, NEW_YORK)
    label = session.label(now)
    while label.weekday() >= 5:
        label += timedelta(days=1)
    start = session.start(label)
    if kind == "equity":
        return SessionPeriod(start, start + 6 * 3600 + 1800)
    return SessionPeriod(start, start + 23 * 3600)


class ConnectedSource:
    """The chain. `fallback` is the Yahoo source; `config` is the `ConnectionService` (enabled, key, order, secret)."""

    def __init__(self, fallback: MarketDataSource, config, catalog: ProviderCatalog, cache_dir: Path,
                 fetch: HttpGet = http_get, clock: Callable[[], float] = time.time,
                 fetchers: dict | None = None) -> None:
        self.fallback = fallback
        self.name = getattr(fallback, "name", "Yahoo Finance")
        self._config, self._catalog, self._clock = config, catalog, clock
        self._cache = DiskCache(cache_dir)
        self._fetch = fetch
        self._fetchers = fetchers if fetchers is not None else FETCHERS
        self._limiters = {pid: RateLimiter(limit=limit, window=60.0) for pid, limit in
                          {"alpaca": 150, "coinbase": 240, "finnhub": 50}.items()}
        self._memory: dict[tuple, float] = {}              # (provider, symbol, base) -> epoch until which it is skipped
        self._denied: dict[str, float] = {}                # provider -> epoch until which its refusal is remembered

    # ---- what the chain is made of ----

    def providers_for(self, symbol: str, allowed: Allowed = None) -> list[str]:
        """Ids of the providers that serve bars of `symbol` here, in the order they are asked."""
        asset = symbol_map.classify(symbol)
        if asset is None:
            return []
        order = [p for p in self._config.order(asset) if p in self._fetchers]
        order += [p for p in EXTRA_ORDER.get(asset, ()) if p in self._fetchers and p not in order]
        order += [p for p in self._fetchers if asset in HISTORY_CLASSES.get(p, ()) and p not in order]
        out = []
        for pid in order:
            spec = self._catalog.get(pid)
            if spec is None or asset not in HISTORY_CLASSES.get(pid, ()):
                continue
            if allowed is not None and not allowed(pid):
                continue
            if self._config.is_enabled(pid) and (not spec.needs_key or self._config.has_key(pid)):
                out.append(pid)
        return out

    def for_desk(self, allowed: Iterable[str] | None) -> "MarketDataSource":
        """The same chain restricted to the providers a desk uses; `None` is the whole chain."""
        return self if allowed is None else _Scoped(self, frozenset(allowed))

    # ---- MarketDataSource ----

    def search(self, query: str):
        return self.fallback.search(query)

    def history(self, symbol: str, timeframe: str, allowed: Allowed = None) -> Series:
        self._ready()
        asset = symbol_map.classify(symbol)
        for pid in self.providers_for(symbol, allowed):
            try:
                series = self._from(pid, symbol, asset, timeframe)
            except (MarketError, Denied):
                continue
            if series is not None:
                return series
        return self.fallback.history(symbol, timeframe)

    def _ready(self) -> None:
        if not getattr(self._config, "loaded", True):
            self._config.load()

    def _from(self, pid: str, symbol: str, asset: str, base: str) -> Series | None:
        now = int(self._clock())
        if self._denied.get(pid, 0) > now or self._memory.get((pid, symbol, base), 0) > now:
            return None
        key = f"{pid}|{symbol}|{base}"
        cached = self._cache.get(key)
        stale = False
        if cached and now - cached[0] <= FRESH_FOR[base]:
            candles, fetched_at = _load(cached[1]), cached[0]
        else:
            try:
                candles = self._download(pid, symbol, asset, base, now)
                fetched_at = now
                self._cache.put(key, json.dumps([[c.t, c.o, c.h, c.l, c.c, c.v] for c in candles]), now)
            except Denied:
                self._denied[pid] = now + DENIED_MEMORY
                raise
            except MarketError:
                self._memory[(pid, symbol, base)] = now + FAIL_MEMORY
                if not cached:
                    raise
                candles, fetched_at, stale = _load(cached[1]), cached[0], True      # the last answer, marked stale
        if not candles:
            self._memory[(pid, symbol, base)] = now + FAIL_MEMORY
            return None
        return self._series(pid, symbol, asset, base, candles, fetched_at, stale, now)

    def _download(self, pid: str, symbol: str, asset: str, base: str, now: int) -> list[Candle]:
        if not self._limiters[pid].allow():
            raise MarketError(RATE_LIMITED, 429)
        secret = self._config.read_secret(pid) if self._catalog.get(pid).needs_key else ""
        if secret is None:
            raise Denied
        return self._fetchers[pid](self._fetch, secret, symbol, asset, base, now)

    def _series(self, pid: str, symbol: str, asset: str, base: str, candles: list[Candle], fetched_at: int,
                stale: bool, now: int) -> Series:
        kind = _kind_of(asset)
        spec = self._catalog.get(pid)
        last = candles[-1]
        return Series(
            symbol=symbol, name=symbol, kind=kind, exchange=spec.name, currency="USD",
            timezone="UTC" if kind == "crypto" else "America/New_York", utc_offset_seconds=0,
            candles=candles, previous_close=None, last_trade_at=min(now, last.t + TIMEFRAME_SECONDS[base]),
            session=_session_period(kind, now), fetched_at=fetched_at, stale=stale, source=spec.name)


def _load(body: str) -> list[Candle]:
    try:
        return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in json.loads(body)]
    except (ValueError, TypeError, IndexError):
        return []


class _Scoped:
    """A `ConnectedSource` that only asks the providers of one desk."""

    def __init__(self, chain: ConnectedSource, allowed: frozenset[str]) -> None:
        self._chain, self._allowed = chain, allowed
        self.name = chain.name
        self.fallback = chain.fallback

    def search(self, query: str):
        return self._chain.search(query)

    def history(self, symbol: str, timeframe: str) -> Series:
        return self._chain.history(symbol, timeframe, self._allowed.__contains__)

"""Market data for the chart.

    GET /api/market/symbols?q=&class=     symbol search (empty q: a short list of well-known tickers). Each result says
                                          its asset class and which source would serve it today (`route`); `class`
                                          filters (future, equity, crypto, forex, index)
    GET /api/market/candles?symbol=&tf=   recent bars, last price, market state, delay and the opening high
    GET /api/market/options/underlying?symbol=              the bar above the option chain (SPEC-014)
    GET /api/market/options/expirations?symbol=             the expirations of the next 400 days
    GET /api/market/options/chain?symbol=&expirations=&strikes=   up to 4 expirations, calls and puts side by side

The three option routes answer from the first source of the use "options" that can (Schwab, then Alpaca, by default).
`source` names it; the chain adds `price_feed` ("schwab", "opra" or "indicative"), `greeks` ("provider", "calculated" or
"estimated") and, for Schwab, `delayed`. A Schwab failure is `options.schwab.auth` (401), `options.schwab.rate_limited`
(429) or `options.schwab.upstream` (502); every code starts with `options.`, so the client never reads one as the end of
Hokma's own session.

Every route needs the per-boot session token (`X-Hokma-Token` or `?token=`). Errors are `{"detail": {"code",
"message"}}` and the panel translates by `code`. The data source sits behind `MarketDataSource` (`source.py`);
`set_source` swaps it (tests, or a future provider).
"""
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from hokma.core.origin import token_matches
from hokma.core.config import hokma_data_dir
from hokma.i18n import t
from hokma.market import options
from hokma.market.cache import RateLimiter
from hokma.market.catalog import SUGGESTIONS
from hokma.market.errors import BAD_RESPONSE, NETWORK, RATE_LIMITED, UNKNOWN_SYMBOL, UPSTREAM, MarketError
from hokma.market.models import SymbolHit
from hokma.market.symbols import FUTURE, classify
from hokma.market.replay import ReplayFetcher
from hokma.market.service import build_chart
from hokma.market.source import MarketDataSource
from hokma.market.yahoo import YahooSource, valid_symbol
from hokma.rpc.ws import SESSION_TOKEN

router = APIRouter(prefix="/api/market")

# The panel is single-user; this only stops a runaway loop from hammering the provider.
_requests = RateLimiter(limit=120, window=60.0)
_source: MarketDataSource | None = None
_source_lock = threading.Lock()
_clock = time.time  # "now" for the market state; tests replace it with `set_clock`


def set_source(source: MarketDataSource | None) -> None:
    global _source
    with _source_lock:
        _source = source


def set_clock(clock) -> None:
    global _clock
    _clock = clock


def now() -> float:
    """The current time for the market state; tests replace it with `set_clock`."""
    return _clock()


def _connected(yahoo: YahooSource) -> MarketDataSource:
    """Candles from the connected providers in the order of Connections, Yahoo as the last fallback (`history.py`)."""
    from hokma.connections import runtime
    from hokma.market.history import ConnectedSource

    service = runtime.get_service()
    return ConnectedSource(yahoo, service, service.catalog, hokma_data_dir() / "market-cache")


def get_source() -> MarketDataSource:
    global _source
    with _source_lock:
        if _source is None:
            replay = os.environ.get("HOKMA_MARKET_REPLAY_DIR")
            extra = {"fetch": ReplayFetcher(Path(replay))} if replay else {}
            yahoo = YahooSource(hokma_data_dir() / "market-cache", **extra)
            _source = yahoo if extra else _connected(yahoo)
        return _source


class RouteOut(BaseModel):
    provider: str
    source: str
    data_state: Literal["live", "delayed"]


class SymbolOut(BaseModel):
    symbol: str
    name: str
    kind: str
    exchange: str
    asset_class: str | None = None
    route: RouteOut | None = None


class SymbolsOut(BaseModel):
    items: list[SymbolOut]
    suggested: bool
    live_classes: list[str] = []


SEARCH_CLASSES = ("", "future", "equity", "crypto", "forex", "index")


class CandleOut(BaseModel):
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float


class OpeningRangeOut(BaseModel):
    high: float
    session_date: str
    start: int
    end: int
    complete: bool


class ChartOut(BaseModel):
    symbol: str
    name: str
    kind: str
    exchange: str
    currency: str
    timezone: str
    timeframe: str
    candles: list[CandleOut]
    last_price: float | None
    previous_close: float | None
    change: float | None
    change_pct: float | None
    market_open: bool | None
    last_trade_at: int | None
    data_age_seconds: int | None
    delayed: bool
    opening_range: OpeningRangeOut | None
    source: str
    stale: bool
    fetched_at: int


def _message(code: str) -> str:
    if code == UNKNOWN_SYMBOL:
        return t("market.error.unknown_symbol")
    if code == RATE_LIMITED:
        return t("market.error.rate_limited")
    if code == NETWORK:
        return t("market.error.network")
    if code == UPSTREAM:
        return t("market.error.upstream_error")
    if code == BAD_RESPONSE:
        return t("market.error.bad_response")
    if code == "bad_timeframe":
        return t("market.error.bad_timeframe")
    if code == "bad_symbol":
        return t("market.error.bad_symbol")
    if code == "bad_class":
        return t("market.error.bad_request")
    return t("market.error.unauthorized")


def _fail(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": _message(code)})


def _guard(header_token: str, query_token: str) -> None:
    if not token_matches(header_token or query_token, SESSION_TOKEN):
        raise _fail(401, "unauthorized")
    if not _requests.allow():
        raise _fail(429, RATE_LIMITED)


def _announced(text: str, hub) -> list[SymbolHit]:
    """Futures roots that a connected provider announced and that match the text, offered as `{root}=F` even when
    the search source does not return them."""
    needle = text.upper()
    hits = []
    for root in hub.announced_roots():
        symbol = f"{root}=F"
        if FUTURE.match(symbol) and (not needle or needle in symbol):
            hits.append(SymbolHit(symbol, root, "future", ""))
    return hits


def _wanted(asset_class: str | None, kind: str, wanted: str) -> bool:
    if not wanted:
        return True
    if wanted == "index":
        return asset_class is None and kind == "index"
    return asset_class == wanted


@router.get("/symbols", response_model=SymbolsOut, summary="Search symbols: stocks, ETFs, indices, futures, crypto")
def symbols(q: str = Query(default="", max_length=40), asset_class: str = Query(default="", alias="class", max_length=10),
            x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> SymbolsOut:
    _guard(x_hokma_token, token)
    wanted = asset_class.strip().lower()
    if wanted not in SEARCH_CLASSES:
        raise _fail(400, "bad_class")
    from hokma.connections import runtime

    service, hub = runtime.get_service(), runtime.get_hub()
    if not service.loaded:
        service.load()
    text = q.strip()
    if not text:
        hits: list[SymbolHit] = list(SUGGESTIONS)
    else:
        try:
            hits = list(get_source().search(text))
        except MarketError as error:
            raise _fail(error.status, error.code) from None
    known = {hit.symbol for hit in hits}
    hits += [hit for hit in _announced(text, hub) if hit.symbol not in known]
    items = []
    for hit in hits:
        asset = classify(hit.symbol, hit.kind)
        if _wanted(asset, hit.kind, wanted):
            items.append(SymbolOut(**asdict(hit), asset_class=asset, route=RouteOut(**hub.preview_route(hit.symbol, asset))))
    return SymbolsOut(items=items, suggested=not text, live_classes=hub.live_classes())


@router.get("/candles", response_model=ChartOut, summary="Recent candles for a symbol")
def candles(symbol: str = Query(max_length=20), tf: Literal["1m", "5m", "15m", "1h", "1D"] = "5m",
            x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> ChartOut:
    _guard(x_hokma_token, token)
    if not valid_symbol(symbol):
        raise _fail(400, "bad_symbol")
    try:
        chart = build_chart(get_source(), symbol, tf, lambda: _clock())
    except MarketError as error:
        raise _fail(error.status, error.code) from None
    data = asdict(chart)
    data.pop("notes", None)
    return ChartOut(**data)


# ---------------------------------------------------------------------------------------------- option chain (SPEC-014)

def _options_fail(error: options.OptionsError) -> HTTPException:
    """`error.code` is `no_connection`, `auth_failed`... for Alpaca and `schwab.auth`, `schwab.rate_limited`, `schwab.upstream`
    for Schwab; the dashboard translates `options.err.<code>` (and a 401 with an `options.` code does not close the session)."""
    return HTTPException(status_code=error.status, detail={"code": f"options.{error.code}", "message": str(error)})


def _options_call(fn, *args, refresh: bool = False):
    if refresh:
        options.forget_cache()
    try:
        return fn(*args)
    except options.OptionsError as error:
        raise _options_fail(error) from None


@router.get("/options/underlying", summary="The bar above the option chain: last, change, bid, ask, size, volume, open, high, low")
def options_underlying(symbol: str = Query(max_length=20), x_hokma_token: str = Header(default=""),
                       token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    return _options_call(options.underlying, symbol)


@router.get("/options/expirations", summary="Expirations of one US stock or ETF for the next 400 days")
def options_expirations(symbol: str = Query(max_length=20), x_hokma_token: str = Header(default=""),
                        token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    return _options_call(options.expirations, symbol)


@router.get("/options/chain", summary="Up to 4 expirations of the option chain, calls and puts side by side")
def options_chain(symbol: str = Query(max_length=20), expirations: str = Query(max_length=80),
                  strikes: str = Query(default="14", max_length=4), refresh: bool = False,
                  x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    return _options_call(options.chain, symbol, expirations, strikes, refresh=refresh)

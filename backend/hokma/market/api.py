"""Market data for the chart.

    GET /api/market/symbols?q=            symbol search (empty q: a short list of well-known tickers)
    GET /api/market/candles?symbol=&tf=   recent bars, last price, market state, delay and the opening high

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
from hokma.market.cache import RateLimiter
from hokma.market.catalog import SUGGESTIONS
from hokma.market.errors import BAD_RESPONSE, NETWORK, RATE_LIMITED, UNKNOWN_SYMBOL, UPSTREAM, MarketError
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


def get_source() -> MarketDataSource:
    global _source
    with _source_lock:
        if _source is None:
            replay = os.environ.get("HOKMA_MARKET_REPLAY_DIR")
            extra = {"fetch": ReplayFetcher(Path(replay))} if replay else {}
            _source = YahooSource(hokma_data_dir() / "market-cache", **extra)
        return _source


class SymbolOut(BaseModel):
    symbol: str
    name: str
    kind: str
    exchange: str


class SymbolsOut(BaseModel):
    items: list[SymbolOut]
    suggested: bool


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
    market_open: bool
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
    return t("market.error.unauthorized")


def _fail(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": _message(code)})


def _guard(header_token: str, query_token: str) -> None:
    if not token_matches(header_token or query_token, SESSION_TOKEN):
        raise _fail(401, "unauthorized")
    if not _requests.allow():
        raise _fail(429, RATE_LIMITED)


@router.get("/symbols", response_model=SymbolsOut, summary="Search symbols: stocks, ETFs, indices, futures, crypto")
def symbols(q: str = Query(default="", max_length=40), x_hokma_token: str = Header(default=""),
            token: str = Query(default="")) -> SymbolsOut:
    _guard(x_hokma_token, token)
    text = q.strip()
    if not text:
        return SymbolsOut(items=[SymbolOut(**asdict(s)) for s in SUGGESTIONS], suggested=True)
    try:
        hits = get_source().search(text)
    except MarketError as error:
        raise _fail(error.status, error.code) from None
    return SymbolsOut(items=[SymbolOut(**asdict(h)) for h in hits], suggested=False)


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

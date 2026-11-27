"""Read-only market data for the agents of Hokma: the same source and the same code as the chart (`build_chart`).

`quote` and `candles` raise `LookupError` / `ValueError` with a message the model can read; the MCP server turns
those into tool errors. Nothing here writes anything."""
from __future__ import annotations

from typing import Any

from hokma.market.errors import MarketError
from hokma.market.intervals import IntervalError, parse_time
from hokma.market.service import build_chart
from hokma.market.yahoo import valid_symbol

MAX_CANDLES = 300
DEFAULT_CANDLES = 60
# What the agent is told for each failure of the source, in plain words.
_WHY = {
    "unknown_symbol": "the market connection does not have this asset",
    "rate_limited": "the market source is limiting requests right now; try again in a minute",
    "network": "there is no connection to the market source right now",
    "upstream_error": "the market source failed",
    "bad_response": "the market source answered something Hokma could not read",
    "interval_unavailable": "the market connection has no bars for this interval",
}


def _source(desk_id: int | None = None):
    """The same chain as the chart: the connected providers in the order of Connections, Yahoo last. A desk agent
    only gets the connections its desk uses."""
    from hokma.market import api

    source = api.get_source()
    if desk_id is not None and hasattr(source, "for_desk"):
        from hokma.connections.runtime import _desk_providers

        source = source.for_desk(_desk_providers(desk_id))
    return source, api.now


def _symbol(value: Any) -> str:
    symbol = value.strip().upper() if isinstance(value, str) else ""
    if not valid_symbol(symbol):
        raise ValueError(f"'{value}' is not a market symbol (examples: NQ=F, AAPL, BTC-USD)")
    return symbol


def _chart(symbol: str, interval: str, keep: int | None = None, desk_id: int | None = None):
    source, now = _source(desk_id)
    try:
        return build_chart(source, symbol, interval, now, keep=keep)
    except MarketError as exc:
        why = _WHY.get(exc.code, exc.code)
        raise LookupError(f"no market data for {symbol} ({interval}): {why} [{exc.code}]") from exc


def quality_of(source: str) -> str:
    """`practice` for a price of Alpaca (free feed, demo account): an agent must not take it as the price of an execution (SPEC-016 3.9). The
    panel does not draw this field. Any other source is `real`."""
    return "practice" if isinstance(source, str) and source.startswith("Alpaca") else "real"


def quote(symbol: Any, desk_id: int | None = None) -> dict[str, Any]:
    """Last price, change, the day's high and low, volume, time and source of one symbol."""
    symbol = _symbol(symbol)
    chart = _chart(symbol, "5m", keep=300, desk_id=desk_id)
    if chart.last_price is None:
        raise LookupError(f"no market data for {symbol}: the source returned no bars")
    high = low = volume = None
    span = "1D"
    try:
        day = _chart(symbol, "1D", keep=1, desk_id=desk_id).candles
    except LookupError:
        day = []
    if day:
        high, low, volume = day[-1].h, day[-1].l, day[-1].v
    else:                                       # no daily bars: the last 24 hours of the 5-minute bars
        span = "24h"
        recent = [c for c in chart.candles if c.t > chart.candles[-1].t - 86400]
        high, low, volume = max(c.h for c in recent), min(c.l for c in recent), sum(c.v for c in recent)
    return {"symbol": chart.symbol, "name": chart.name, "last": chart.last_price, "change": chart.change,
            "change_pct": chart.change_pct, "previous_close": chart.previous_close, "high": high, "low": low,
            "volume": volume, "range": span, "currency": chart.currency, "time": chart.last_trade_at,
            "market_open": chart.market_open, "delayed": chart.delayed, "stale": chart.stale,
            "source": chart.source, "quality": quality_of(chart.source)}


def candles(symbol: Any, interval: Any = "5m", limit: Any = DEFAULT_CANDLES,
            desk_id: int | None = None) -> dict[str, Any]:
    """The newest `limit` bars the chart would draw, oldest first. `t` is the start of the bar in epoch seconds UTC."""
    symbol = _symbol(symbol)
    interval = "5m" if interval is None else interval
    try:
        interval = parse_time(interval).canonical
    except IntervalError:
        raise ValueError("interval is a time interval such as 1m, 7m, 1h, 4h, 1D or 1W") from None
    limit = DEFAULT_CANDLES if limit is None else limit
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CANDLES:
        raise ValueError(f"limit must be a whole number from 1 to {MAX_CANDLES}")
    chart = _chart(symbol, interval, keep=limit, desk_id=desk_id)
    bars = [{"t": c.t, "o": c.o, "h": c.h, "l": c.l, "c": c.c, "v": c.v} for c in chart.candles[-limit:]]
    if not bars:
        raise LookupError(f"no market data for {symbol} ({interval}): the source returned no bars")
    return {"symbol": chart.symbol, "interval": chart.timeframe, "currency": chart.currency, "count": len(bars),
            "source": chart.source, "quality": quality_of(chart.source), "stale": chart.stale, "candles": bars}


def desk_prices(symbols: list[str], desk_id: int | None = None) -> list[dict[str, Any]]:
    """The assets of a desk with their last price; an asset the source cannot serve says why and the rest stay."""
    out: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            q = quote(symbol, desk_id)
            out.append({"symbol": symbol, "last": q["last"], "change_pct": q["change_pct"], "time": q["time"],
                        "source": q["source"], "quality": q["quality"]})
        except (LookupError, ValueError) as exc:
            out.append({"symbol": symbol, "last": None, "error": str(exc)})
    return out

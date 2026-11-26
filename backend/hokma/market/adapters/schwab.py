"""Schwab price, candles and options by STREAMING (SPEC-016 4.2, 4.3). One Streamer session (`hokma.schwab.stream`) serves every
symbol: LEVELONE_EQUITIES for the price and the fields of the day, CHART_EQUITY for the 1-minute candle in progress.

* Nothing is polled. A `Quote` is the whole last state of the symbol as Schwab sent it (it only sends what changed, the session keeps
  the last value of each field). A `Tick` is never made from a quote: Schwab has no trade by trade feed, so this source says
  `ticks = False` and the hub refuses the seconds and range charts with it.
* A CHART message is the candle of one minute. Whether Schwab sends the minute in progress or only the closed one is measured in
  T1 (providers.json `stream.chart_in_progress`): until then each message is an update of its minute, and the minute is marked
  final when a message of a later minute arrives, so both behaviors give the right candles.
* `delayed: true` in any message makes the source `delayed` ("Con retraso"); the state is told when it changes.
* Everything that ends the session for good (no sign-in, expired, denied) comes from the streamer as `down` with its
  `connections.*` code; the hub shows it and does NOT move the symbols to another source (SPEC-016 3.7).
* The session lives in `hokma.schwab.auth` and the socket in `hokma.schwab.stream`: nothing secret is kept or logged here.
"""
import logging
import math
import time

from hokma.market.errors import UNKNOWN_SYMBOL
from hokma.market.providers import ProviderSpec
from hokma.market.stream import (BarUpdate, LimitReached, LiveSource, OptionQuote, Quote, Rollover, SourceCapabilities,
                                 SourceStatus, SymbolError)
from hokma.schwab import stream as streamer

log = logging.getLogger("hokma.market.schwab")

UNKNOWN_SYMBOL_CODE = "market." + UNKNOWN_SYMBOL

# The nodes of `quote` (QuoteEquity, REF A.4) the source reads, and the names they get in `map_quote`.
QUOTE_FIELDS = {
    "lastPrice": "last", "bidPrice": "bid", "askPrice": "ask", "bidSize": "bid_size", "askSize": "ask_size",
    "totalVolume": "volume", "openPrice": "open", "highPrice": "high", "lowPrice": "low", "closePrice": "close",
    "netChange": "change", "netPercentChange": "change_pct", "mark": "mark", "quoteTime": "quote_time",
    "tradeTime": "trade_time",
}


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def map_quote(item: dict) -> dict:
    """One symbol of the answer as a flat record. `delayed` is true when Schwab says `realtime: false`; a quote that says
    nothing about it is taken as real time."""
    quote = item.get("quote") if isinstance(item.get("quote"), dict) else {}
    out = {"symbol": item.get("symbol"), "quote_type": item.get("quoteType"), "delayed": item.get("realtime") is False}
    for name, short in QUOTE_FIELDS.items():
        out[short] = _number(quote.get(name))
    return out



def _text(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def quote_of(symbol: str, fields: dict, delayed: bool, now_ms: int) -> Quote:
    """A `Quote` from the whole state of LEVELONE_EQUITIES (guide 7.8, the numbers are those of its table)."""
    n = lambda number: _number(fields.get(number))                  # noqa: E731
    bid, ask, mark = n("1"), n("2"), n("33")
    calculated = False
    if mark is None and bid is not None and ask is not None:
        mark, calculated = round((bid + ask) / 2, 6), True
    regular = fields.get("28")
    stamp = n("35") or n("34") or n("36")
    return Quote(symbol, int(stamp) if stamp else now_ms, bid, ask, n("3"), mark, n("4"), n("5"), n("9"), n("8"), n("17"),
                 n("10"), n("11"), n("12"), n("29"), "regular" if regular is True else "extended" if regular is False else None,
                 _text(fields.get("32")), delayed, calculated, int(n("36")) if n("36") else None, n("31"), n("43"), n("50"), n("51"))


def normalize_option(symbol: str) -> str:
    """The key of an option as the Streamer wants it: `RRRRRRYYMMDDsWWWWWddd`, the root padded with spaces to 6 (guide 7.9). `/chains` may
    send it with fewer spaces; both sides are compared through this form."""
    text = (symbol or "").upper()
    return text[:-15].strip().ljust(6) + text[-15:] if len(text) >= 15 else text


def option_quote_of(key: str, fields: dict, delayed: bool, now_ms: int) -> OptionQuote:
    """An `OptionQuote` from the whole state of LEVELONE_OPTIONS (guide 7.9: 2 bid, 3 ask, 4 last, 8 volume, 9 open interest, 10 volatility,
    16 and 17 sizes, 28 to 32 greeks, 34 theoretical, 35 underlying price, 37 mark, 38 and 39 times)."""
    n = lambda number: _number(fields.get(number))                  # noqa: E731
    stamp = n("38") or n("39")
    return OptionQuote(key, int(stamp) if stamp else now_ms, n("2"), n("3"), n("4"), n("37"), n("16"), n("17"), n("8"), n("9"), n("10"),
                       n("28"), n("29"), n("30"), n("31"), n("32"), n("34"), n("35"), _text(fields.get("33")), delayed)


def future_quote_of(symbol: str, fields: dict, delayed: bool, now_ms: int) -> Quote:
    """A `Quote` from the whole state of LEVELONE_FUTURES (guide 7.10: 1 bid, 2 ask, 3 last, 4 and 5 sizes, 8 volume, 9 last size, 10 quote time,
    11 trade time, 12 high, 13 low, 14 previous close, 18 open, 22 status, 24 mark). Open, high and low are the ones of the day as Schwab gives them."""
    n = lambda number: _number(fields.get(number))                  # noqa: E731
    bid, ask, mark = n("1"), n("2"), n("24")
    calculated = False
    if mark is None and bid is not None and ask is not None:
        mark, calculated = round((bid + ask) / 2, 6), True
    stamp = n("11") or n("10")
    return Quote(symbol, int(stamp) if stamp else now_ms, bid, ask, n("3"), mark, n("4"), n("5"), n("9"), n("8"), n("18"), n("12"), n("13"),
                 n("14"), None, None, _text(fields.get("22")), delayed, calculated)


def chart_futures_bar(key: str, fields: dict) -> BarUpdate | None:
    """A CHART_FUTURES message (guide 7.14). The ORDER IS NOT THAT OF CHART_EQUITY: 1 chart time (ms), 2 open, 3 high, 4 low, 5 close, 6 volume.
    One decoder per service: sharing the equity one would shift every price."""
    values = [_number(fields.get(i)) for i in ("2", "3", "4", "5", "6", "1")]
    if any(v is None for v in values[:4]) or values[5] is None:
        return None
    return BarUpdate(key, int(values[5]), values[0], values[1], values[2], values[3], values[4] or 0.0)


def chart_equity_bar(key: str, fields: dict) -> BarUpdate | None:
    """A CHART_EQUITY message (guide 7.14): 1 open, 2 high, 3 low, 4 close, 5 volume, 6 sequence, 7 time in ms."""
    values = [_number(fields.get(i)) for i in ("1", "2", "3", "4", "5", "7")]
    if any(v is None for v in values[:4]) or values[5] is None:
        return None
    return BarUpdate(key, int(values[5]), values[0], values[1], values[2], values[3], values[4] or 0.0)


class SchwabSource(LiveSource):
    provider_id = "schwab"
    data_state = "live"

    def __init__(self, spec: ProviderSpec, backoff: tuple[float, ...] | None = None, config=None) -> None:
        super().__init__(backoff)
        self.name = spec.name
        self.capabilities = SourceCapabilities(True, spec.max_symbols, spec.asset_classes, 0, ticks=False, clock_aligned=True, provider_days=True, live_options=True)
        self._config = config or streamer.StreamConfig.from_spec(spec.stream)
        self._streamer: streamer.SchwabStreamer | None = None
        self._last_bar: dict[str, BarUpdate] = {}                       # symbol -> the minute in progress
        self._active: dict[str, str] = {}                               # futures root -> the active contract the source named last (field 34)

    # ---- contract (the session has its own reconnection: the run loop of `LiveSource` is not used) ----

    async def connect(self) -> None:
        import asyncio

        self._queue = self._queue or asyncio.Queue(20000)
        if self._streamer is None:
            self._streamer = streamer.SchwabStreamer(self._config, self._data, self._status, on_limit=self._limit,
                                                     on_unavailable=self._unavailable, backoff=self._backoff, on_activity=self._activity)
        await self._streamer.start()

    async def subscribe(self, symbols: list[str]) -> None:
        fresh = [s for s in symbols if s not in self._symbols]
        self._symbols.update(symbols)
        if fresh and self._streamer is not None:
            futures = [s for s in fresh if s.startswith("/")]               # `/NQ`, `/ESH26` (guide 7.10): the services of futures
            stocks = [s for s in fresh if not s.startswith("/")]
            if stocks:
                self._streamer.want(streamer.LEVELONE_EQUITIES, stocks)
                self._streamer.want(streamer.CHART_EQUITY, stocks)
            if futures:
                self._streamer.want(streamer.LEVELONE_FUTURES, futures)
                self._streamer.want(streamer.CHART_FUTURES, futures)

    async def unsubscribe(self, symbols: list[str]) -> None:
        gone = [s for s in symbols if s in self._symbols]
        self._symbols.difference_update(symbols)
        if gone and self._streamer is not None:
            for services, group in (((streamer.LEVELONE_EQUITIES, streamer.CHART_EQUITY), [s for s in gone if not s.startswith("/")]),
                                    ((streamer.LEVELONE_FUTURES, streamer.CHART_FUTURES), [s for s in gone if s.startswith("/")])):
                for service in services:
                    if group:
                        self._streamer.drop(service, group)
            for symbol in gone:
                self._last_bar.pop(symbol, None)
                self._active.pop(symbol, None)

    @property
    def streamer(self) -> "streamer.SchwabStreamer | None":
        return self._streamer

    def diagnostic_want(self, plan: dict[str, list[str]]) -> dict[str, set[str]]:
        """The measuring run asks, through the session of the hub, for keys it needs. Returns only the keys it ADDED (those the hub already followed
        stay the hub's), so that `diagnostic_drop` never takes away what a chart is using."""
        added: dict[str, set[str]] = {}
        if self._streamer is None:
            return added
        for service, keys in plan.items():
            new = set(keys) - self._streamer.wanted(service)
            if new:
                self._streamer.want(service, new)
                added[service] = new
        return added

    def diagnostic_drop(self, added: dict[str, set[str]]) -> None:
        if self._streamer is not None:
            for service, keys in added.items():
                self._streamer.drop(service, keys)

    def watch_account_activity(self, on: bool) -> bool:
        """Follow (or stop following) the activity of the account by the Streamer (ACCT_ACTIVITY): a signal that a fill may be waiting, so the live
        freeze pulls the fills at once. False when the session is not open (it is asked again on the next pass). The session repeats it after a reconnection."""
        if self._streamer is None:
            return False
        if on:
            self._streamer.want(streamer.ACCT_ACTIVITY, [streamer.ACTIVITY_KEY])
        else:
            self._streamer.drop(streamer.ACCT_ACTIVITY, [streamer.ACTIVITY_KEY])
        return True

    def _activity(self, message_type: str) -> None:
        try:
            from hokma.exec import live

            from hokma.broker import pnl

            live.on_account_activity("schwab", message_type)
            pnl.on_account_activity("schwab", message_type)                 # one more read of the accounts of Schwab, at most one per second
        except Exception:                                                  # noqa: BLE001  (a failure of the freeze never stops the market)
            log.warning("the account activity could not be passed to the live freeze")

    async def subscribe_options(self, keys: list[str]) -> None:
        """Option contracts to follow (keys as `/chains` gives them: they are put in the Streamer's form)."""
        if self._streamer is not None and keys:
            self._streamer.want(streamer.LEVELONE_OPTIONS, [normalize_option(k) for k in keys])

    async def unsubscribe_options(self, keys: list[str]) -> None:
        if self._streamer is not None and keys:
            self._streamer.drop(streamer.LEVELONE_OPTIONS, [normalize_option(k) for k in keys])

    async def close(self) -> None:
        self._closing = True
        if self._streamer is not None:
            await self._streamer.stop()
            self._streamer = None
        if self._queue is not None and not self._queue.full():
            from hokma.market.stream import _STOP

            self._queue.put_nowait(_STOP)

    async def market_hours(self, asset_class: str, days: tuple[str, ...] = ()):
        """The hours of the market of this asset class, as Schwab says them (`GET /markets/{market}`), or None when it cannot say.
        Asked once a day and again after a reconnection by the hub; nothing is kept here. `days` (yyyy-MM-dd): other days that are on
        a chart; the pieces Schwab names for them are added to today's, a day it refuses has no shading (never a schedule of ours)."""
        import asyncio

        from hokma.market.hours import parse_schwab_hours
        from hokma.schwab import endpoints, models

        market = {"equity": "equity", "future": "future", "option": "option"}.get(asset_class)
        if market is None:
            return None
        try:
            payload = await asyncio.to_thread(lambda: models.dump(endpoints.market_hours(market)))
        except Exception:                                   # noqa: BLE001  (no hours is "unknown", never a guess)
            return None
        today = parse_schwab_hours(payload, market)
        if today is None or not days:
            return today
        extra = []
        for day in dict.fromkeys(days):
            if day == today.date:
                continue
            try:
                other = await asyncio.to_thread(lambda day=day: models.dump(endpoints.market_hours(market, date=day, past_ok=True)))
            except Exception:                               # noqa: BLE001  (Schwab may not give a past day: that day is not shaded)
                continue
            parsed = parse_schwab_hours(other, market)
            if parsed is not None:
                extra.append(parsed)
        return today.merged(extra)

    # ---- what the session reports ----

    def _status(self, state: str, code: str, detail: str) -> None:
        if state == "live":
            self.emit(SourceStatus(self.data_state, code))
        elif state == "idle":
            self.emit(SourceStatus(self.data_state, "idle"))
        else:
            self.emit(SourceStatus(state, code, detail))

    def _limit(self, service: str, keys) -> None:
        self.emit(LimitReached("options" if service == streamer.LEVELONE_OPTIONS else "equity", tuple(keys)))

    def _unavailable(self, service: str) -> None:
        """Code 11: the account does not have that service. For futures it is the symbol that cannot be served (the panel says the account has no
        futures data; there is no other source, 3.10); the stocks of the same session go on. Any other service ends the source as before."""
        if service in (streamer.LEVELONE_FUTURES, streamer.CHART_FUTURES):
            for symbol in sorted(s for s in self._symbols if s.startswith("/")):
                self.emit(SymbolError(symbol, "market.futures_not_enabled"))
            return
        self.emit(SourceStatus("down", "connections.schwab_denied", "service not available"))

    def _data(self, service: str, key: str, fields: dict, changed, delayed: bool, ts_ms: int | None) -> None:
        state = "delayed" if delayed else "live"
        if state != self.data_state:
            self.data_state = state
            self.emit(SourceStatus(state, "data"))
        if service == streamer.LEVELONE_EQUITIES:
            self.emit(quote_of(key, fields, delayed, ts_ms or int(time.time() * 1000)))
        elif service == streamer.LEVELONE_FUTURES:
            now = ts_ms or int(time.time() * 1000)
            active = _text(fields.get("34"))
            before = self._active.get(key)
            if active is not None:
                self._active[key] = active
                if before is not None and before != active:           # the front contract changed (guide 7.10, field 34): a Rollover, no date of ours
                    self.emit(Rollover(key, before, active, now))
            self.emit(future_quote_of(key, fields, delayed, now))
        elif service == streamer.CHART_FUTURES:
            bar = chart_futures_bar(key, fields)
            if bar is not None:
                self._emit_bar(key, bar)
        elif service == streamer.LEVELONE_OPTIONS:
            self.emit(option_quote_of(key, fields, delayed, ts_ms or int(time.time() * 1000)))
        elif service == streamer.CHART_EQUITY:
            bar = chart_equity_bar(key, fields)
            if bar is not None:
                self._emit_bar(key, bar)

    def _emit_bar(self, key: str, bar: BarUpdate) -> None:
        """A candle of one minute: each message updates its minute, and the minute is final when one of a later minute arrives."""
        before = self._last_bar.get(key)
        if before is not None and bar.start_ms > before.start_ms:
            self.emit(BarUpdate(key, before.start_ms, before.o, before.h, before.l, before.c, before.v, final=True))
        if before is None or bar.start_ms >= before.start_ms:
            self._last_bar[key] = bar
        self.emit(bar)

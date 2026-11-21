"""Schwab price of stocks and ETFs, by query (SPEC-015 5.4). Schwab has no stream here: every 2 seconds ONE call to
`GET /marketdata/v1/quotes?symbols=...&fields=quote,reference` with the subscribed symbols (at most 100 per call; a 400
that says the list is too long splits it in two and asks again). It is `PollingLiveSource` with another `_poll_once`.

* A new trade (another `tradeTime`, price or volume) becomes a `Tick`; its size is the growth of `totalVolume`.
* `realtime: false` in any answer marks the source `delayed` ("Con retraso"); the state is told after each good poll.
* 429 waits 60 seconds (shown as reconnecting). A 401 is already renewed and retried once by `schwab/http.py`; a session
  that cannot be renewed ends the source (`connections.schwab_expired`, or `connections.no_login` when there is none) so the hub
  moves the symbols to the next provider and the row of Connections says why. A 403 ends it with `connections.schwab_denied`.
* Calls go through `hokma.schwab.endpoints` (the only door is `schwab/http.py`); nothing secret is kept or logged here.
"""
import asyncio
import math
import time
from types import SimpleNamespace

from hokma.market.adapters.polling import PollingLiveSource
from hokma.market.errors import UNKNOWN_SYMBOL
from hokma.market.providers import PollingSpec, ProviderSpec
from hokma.market.stream import SourceCapabilities, SourceStatus, SymbolError, TerminalError, Tick
from hokma.schwab import endpoints, http, models

POLL_SECONDS = 2.0
RATE_PAUSE_SECONDS = 60.0
CHUNK = 100
FIELDS = "quote,reference"
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


class _RateLimited(Exception):
    """A 429: `classify_error` reads `response.status_code` and shows it as `rate_limited`."""

    response = SimpleNamespace(status_code=429)


class SchwabSource(PollingLiveSource):
    provider_id = "schwab"
    data_state = "live"

    def __init__(self, spec: ProviderSpec, backoff: tuple[float, ...] | None = None,
                 interval: float = POLL_SECONDS) -> None:
        super().__init__(lambda: None, PollingSpec(spec.name, interval), backoff)
        self.capabilities = SourceCapabilities(False, spec.max_symbols, spec.asset_classes, 0)
        self.last_quotes: dict[str, dict] = {}                # symbol -> `map_quote` of the last answer
        self._seen: dict[str, tuple] = {}                      # symbol -> (trade time, price, volume) of the last tick

    # ---- the loop: the first answer decides the data state, so the source is not "live" for a moment when it is not ----

    async def _session(self) -> None:
        self._poke = asyncio.Event()
        await self._poll_once()
        while True:
            try:
                await asyncio.wait_for(self._poke.wait(), self._interval)
            except asyncio.TimeoutError:
                pass
            self._poke.clear()
            await self._poll_once()

    async def _poll_once(self) -> None:
        symbols = sorted(self._symbols)
        delayed = False
        if symbols:
            quotes: dict[str, dict] = {}
            invalid: list[str] = []
            for start in range(0, len(symbols), CHUNK):
                await self._chunk(symbols[start:start + CHUNK], quotes, invalid)
            for symbol in invalid:
                self._symbols.discard(symbol)
                self.last_quotes.pop(symbol, None)
                self._seen.pop(symbol, None)
                self.emit(SymbolError(symbol, UNKNOWN_SYMBOL_CODE))
            delayed = any(q["delayed"] for q in quotes.values())
            for symbol, record in quotes.items():
                self.last_quotes[symbol] = record
                self._tick(symbol, record)
        state = self.data_state = "delayed" if delayed else "live"
        if not self._ready:
            self._mark_ready("polling")
        self.emit(SourceStatus(state, "polled"))

    async def _chunk(self, chunk: list[str], quotes: dict, invalid: list) -> None:
        """Ask for `chunk`; on a 400 split it in two (the REF gives no maximum), down to one symbol that Schwab refuses."""
        try:
            answer = await asyncio.to_thread(self._ask, chunk)
        except http.SchwabError as error:
            if error.code == http.BAD_REQUEST:
                if len(chunk) == 1:
                    invalid.append(chunk[0])
                    return
                half = len(chunk) // 2
                await self._chunk(chunk[:half], quotes, invalid)
                await self._chunk(chunk[half:], quotes, invalid)
                return
            self._fail(error)
        found, missing = answer
        quotes.update(found)
        invalid.extend(s for s in missing if s in self._symbols)

    @staticmethod
    def _ask(chunk: list[str]) -> tuple[dict, list[str]]:
        """Blocking (a thread): the quotes by symbol, and the symbols Schwab listed as invalid."""
        data = models.dump(endpoints.quotes(chunk, fields=FIELDS))
        found = {symbol: map_quote(item) for symbol, item in data.items()
                 if symbol != "errors" and isinstance(item, dict)}
        errors = data.get("errors") if isinstance(data.get("errors"), dict) else {}
        return found, [s for s in errors.get("invalidSymbols") or [] if isinstance(s, str)]

    def _fail(self, error: http.SchwabError) -> None:
        """What a failed call does to the source: end it for good, wait, or drop the connection and retry."""
        if error.code == http.RATE_LIMITED:
            self._retry_delay = RATE_PAUSE_SECONDS
            raise _RateLimited
        if error.code == http.NOT_CONNECTED:
            raise TerminalError("connections.no_login")
        if error.code == http.EXPIRED:
            raise TerminalError("connections.schwab_expired")
        if error.code == http.DENIED:
            raise TerminalError("connections.schwab_denied")
        raise OSError("schwab quotes unavailable")             # unreachable, upstream: reconnecting, then try again

    def _tick(self, symbol: str, record: dict) -> None:
        price = record["last"]
        if price is None:
            return
        stamp = record["trade_time"] or record["quote_time"]
        key = (stamp, price, record["volume"])
        before = self._seen.get(symbol)
        if before == key:
            return                                             # nothing traded since the last answer
        self._seen[symbol] = key
        grown = None
        if before is not None and record["volume"] is not None and before[2] is not None:
            grown = max(0.0, record["volume"] - before[2])
        self.emit(Tick(symbol, int(stamp) if stamp else int(time.time() * 1000), price, grown))

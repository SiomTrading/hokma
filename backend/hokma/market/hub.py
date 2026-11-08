"""The market hub: one object per engine between the providers and the panel.

- One connection per provider, shared. Panel subscriptions are counted per symbol: two tabs on the same symbol are
  one subscription upstream and closing the last one unsubscribes.
- Routing: the asset class of the symbol and the priority set in Connections pick the provider, among those that are
  switched on, have their key and are not in error. With none, the symbol is served by the delayed fallback
  (`PollingLiveSource` over the history source) and the panel is told so.
- `BarAggregator` (`aggregator.py`) builds the bar in progress of each interval with a subscription (at most 12 per
  symbol, ranges included) from ticks or 1m bars.
- A feed whose provider gives ticks keeps them in a `TickTape` (`tape.py`). Seconds bars and range bars are built from
  it, and the tape and the upstream subscription outlive the last panel subscription by `LINGER_SECONDS`, so changing
  the interval of a chart and coming back does not lose what was gathered. Range bars before the first tick of the
  tape are approximate: they come from the 1-minute bars of the history source (`rangebars.py`).
- Each panel subscription gets at most one live message every 250 ms (4 per second), always with the latest value.

Every message to the panel carries `source` and `data_state`; a delayed value is never labelled live.
"""
import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
from typing import Protocol

from hokma.i18n import t
from hokma.market import instruments, opening, symbols as symbol_map
from hokma.market.adapters import ADAPTERS, PollingLiveSource
from hokma.market.adapters.polling import PROVIDER_ID as POLLING_ID
from hokma.market.aggregator import BarAggregator
from hokma.market.errors import INTERVAL_UNAVAILABLE, SECONDS_NEED_TICKS, UNKNOWN_SYMBOL, MarketError
from hokma.market.intervals import Interval, IntervalError, RangeInterval, parse, visible_bars
from hokma.market.messages import DOWN_CODES, connection_message, interval_message, market_message
from hokma.market.models import Candle, ChartData, OpeningRange
from hokma.market.providers import ProviderCatalog, ProviderSpec
from hokma.market.rangebars import RangeBar, RangeBarBuilder, approximate_from_minutes, build_from_ticks, splice
from hokma.market.service import build_chart
from hokma.market.source import MarketDataSource
from hokma.market.stream import BarUpdate, LiveSource, Rollover, SourceStatus, SymbolError, Tick
from hokma.market.tape import TickTape
from hokma.market.tz import zone

log = logging.getLogger("hokma.market")

MIN_INTERVAL = 0.25                 # seconds between live messages of one subscription: 4 per second
MAX_SUBS_PER_CLIENT = 100
POLLING_MAX_SYMBOLS = 20
TEST_TIMEOUT = 10.0
UNREACHABLE_AFTER = 3              # failed connection attempts that end a test before its 10 seconds
ACK_GRACE = 2.0                     # a subscription acknowledged but silent (market closed) still counts as connected
OUTBOX_LIMIT = 2000
MAX_INTERVALS_PER_SYMBOL = 12       # distinct intervals built at once for one symbol (time and range together)
LINGER_SECONDS = 60                 # the tape and the upstream subscription outlive the last panel subscription
MINUTES_KEEP = 20000                # 1-minute bars kept for the charts that build their own bars (7 days is about 10000)
RANGE_KEEP = 400                    # range bars a subscription keeps (the snapshot carries the newest 300)
SECONDS_KEEP = 300                  # seconds bars of a snapshot

__all__ = ["BarAggregator", "ConnectionConfig", "HubError", "MarketHub", "PanelClient"]


class HubError(Exception):
    def __init__(self, code: str, params: dict | None = None, message: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.params = params or {}
        self.message = message


class ConnectionConfig(Protocol):
    """What the hub needs to know about the owner's choices. Implemented by `ConnectionService`."""

    def is_enabled(self, provider_id: str) -> bool: ...
    def has_key(self, provider_id: str) -> bool: ...
    def order(self, asset_class: str) -> list[str]: ...
    def read_secret(self, provider_id: str) -> str | None: ...       # blocking: the system store


class PanelClient:
    """One open `/ws/market` socket. The hub pushes dicts; the socket handler drains `outbox`."""

    def __init__(self) -> None:
        self.id = uuid.uuid4().hex
        self.outbox: asyncio.Queue[str] = asyncio.Queue(OUTBOX_LIMIT)
        self.subs: dict[str, "Sub"] = {}
        self.overflowed = False

    def push(self, message: dict) -> None:
        try:
            self.outbox.put_nowait(json.dumps(message, separators=(",", ":")))
        except asyncio.QueueFull:
            self.overflowed = True                           # a reader this slow is dropped by the socket handler


@dataclass
class Sub:
    client: PanelClient
    id: str
    symbol: str
    tf: str
    interval: Interval | None = None
    feed: "SymbolFeed | None" = None
    seq: int = 0
    candles: list[Candle] = field(default_factory=list)
    minutes: list[Candle] | None = None                  # 1m history of the charts that build their own bars
    rbars: list[RangeBar] = field(default_factory=list)
    range_offset: int = 0                                # approximate bars in front of the exact ones
    tick_size: float | None = None
    prev_close: float | None = None
    kind: str = "other"
    tz_name: str = "UTC"
    opening: OpeningRange | None = None
    state: str = "live"
    pending_final: list[Candle | RangeBar] = field(default_factory=list)
    bar_dirty: bool = False
    price_dirty: bool = False
    last_sent: float = 0.0
    last_bar: Candle | RangeBar | None = None
    last_price: float | None = None
    last_ts_ms: int = 0
    timer: asyncio.TimerHandle | None = None
    closed: bool = False


@dataclass
class SymbolFeed:
    symbol: str
    asset_class: str | None
    runtime: "ProviderRuntime"
    upstream: str
    aggregator: BarAggregator
    subs: dict[tuple[str, str], Sub] = field(default_factory=dict)
    newest_bar_ms: int = 0                               # start of the newest bar a provider has sent
    tape: TickTape | None = None                         # ticks of the provider; None for the delayed fallback
    ranges: dict[str, RangeBarBuilder] = field(default_factory=dict)
    tick_size: float | None = None
    linger: asyncio.TimerHandle | None = None
    refreshing: bool = False


@dataclass
class _Probe:
    upstream: str
    data_at: float | None = None
    acked_at: float | None = None
    reconnects: int = 0


class ProviderRuntime:
    def __init__(self, spec: ProviderSpec | None, provider_id: str, name: str, data_state: str) -> None:
        self.spec, self.id, self.name = spec, provider_id, name
        self.data_state = data_state
        self.state = "not_connected"           # not_connected | connected | reconnecting | error
        self.error_code: str | None = None
        self.last_code = ""
        self.source: LiveSource | None = None
        self.consumer: asyncio.Task | None = None
        self.feeds: dict[str, SymbolFeed] = {}
        self.by_upstream: dict[str, SymbolFeed] = {}
        self.probes: list[_Probe] = []

    def reset_error(self) -> None:
        if self.state == "error":
            self.state, self.error_code = "not_connected", None


class MarketHub:
    def __init__(self, catalog: ProviderCatalog, config: ConnectionConfig, history: Callable[[], MarketDataSource],
                 clock: Callable[[], float] = time.time, epoch: str | None = None) -> None:
        self.catalog, self.config, self._history, self._clock = catalog, config, history, clock
        self.epoch = epoch or uuid.uuid4().hex[:12]
        self._clients: set[PanelClient] = set()
        self._feeds: dict[str, SymbolFeed] = {}
        self._runtimes: dict[str, ProviderRuntime] = {pid: ProviderRuntime(spec, pid, spec.name, spec.data_state)
                                                     for pid, spec in catalog.providers.items()}
        self._fallback = ProviderRuntime(None, POLLING_ID, catalog.polling.name, "delayed")
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Task] = set()
        self._started = False
        self.linger_seconds = LINGER_SECONDS

    # ------------------------------------------------------------------ clients

    def _bind(self) -> None:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
            self._lock = asyncio.Lock()

    def attach_client(self, client: PanelClient) -> None:
        self._bind()
        self._clients.add(client)
        for runtime in self._runtimes.values():
            client.push(self._connection_message(runtime))

    def detach_client(self, client: PanelClient) -> "asyncio.Task":
        """Release everything the socket held. It runs as a task of the hub, so a socket that is torn down in the
        middle of its own cleanup still gets its subscriptions released."""
        self._clients.discard(client)
        return self._spawn(self._release_client(client))

    async def _release_client(self, client: PanelClient) -> None:
        for sub_id in list(client.subs):
            await self.unsubscribe(client, sub_id)

    def error_message(self, code: str, sub: str | None = None, params: dict | None = None,
                      message: str | None = None) -> dict:
        """An error for the panel. `params` fills the placeholders of the text and travels with it; `message`
        replaces the generic text of the code when the caller knows better (a refused interval says why)."""
        params = params or {}
        return {"type": "error", "sub": sub, "code": code, "message": message or market_message(code, params),
                "params": params, "epoch": self.epoch, "source": "none", "data_state": "delayed"}

    def refusal(self, code: str, sub: str | None) -> dict:
        """A `market.bad_request` for an interval the grammar refused; `code` is what `intervals.parse` raised."""
        return self.error_message("market.bad_request", sub, message=interval_message(code))

    # ------------------------------------------------------------------ subscribe / unsubscribe

    async def subscribe(self, client: PanelClient, sub_id: str, symbol: str, tf: str) -> None:
        self._bind()
        if sub_id in client.subs or len(client.subs) >= MAX_SUBS_PER_CLIENT:
            client.push(self.error_message("market.limit" if sub_id not in client.subs else "market.bad_request", sub_id))
            return
        try:
            interval = parse(tf)
        except IntervalError as error:
            client.push(self.refusal(error.code, sub_id))
            return
        sub = Sub(client, sub_id, symbol, interval.canonical, interval=interval)
        client.subs[sub_id] = sub
        try:
            self._check_interval_limit(symbol, sub.tf)
            await self._attach(sub, exclude=frozenset())
        except HubError as error:
            await self.unsubscribe(client, sub_id)
            client.push(self.error_message(error.code, sub_id, error.params, error.message))

    def _check_interval_limit(self, symbol: str, tf: str) -> None:
        feed = self._feeds.get(symbol)
        if feed is None:
            return
        watched = {*feed.aggregator.intervals, *feed.ranges}
        if tf not in watched and len(watched) >= MAX_INTERVALS_PER_SYMBOL:
            raise HubError("market.limit")

    async def unsubscribe(self, client: PanelClient, sub_id: str) -> None:
        sub = client.subs.pop(sub_id, None)
        if sub is None:
            return
        await self._release(sub)

    async def _release(self, sub: Sub, linger: bool = True) -> None:
        sub.closed = True
        if sub.timer is not None:
            sub.timer.cancel()
            sub.timer = None
        feed, sub.feed = sub.feed, None
        if feed is None:
            return
        feed.subs.pop((sub.client.id, sub.id), None)
        if not any(other.tf == sub.tf for other in feed.subs.values()):
            feed.aggregator.remove_interval(sub.tf)            # nobody else watches this interval: stop building it
            feed.ranges.pop(sub.tf, None)
        await self._drop_if_empty(feed, linger)

    async def _drop_if_empty(self, feed: SymbolFeed, linger: bool = True) -> None:
        """The last subscription left. A feed with a tape of ticks stays `linger_seconds` more, with its upstream
        subscription, so the ticks gathered are not lost when the chart changes interval and comes back."""
        if feed.subs:
            return
        if linger and feed.tape is not None and self._loop is not None and self.linger_seconds > 0:
            if feed.linger is None:
                feed.linger = self._loop.call_later(self.linger_seconds, self._expire, feed)
            return
        await self._drop_now(feed)

    def _expire(self, feed: SymbolFeed) -> None:
        feed.linger = None
        if not feed.subs and self._feeds.get(feed.symbol) is feed:
            self._spawn(self._drop_now(feed))

    async def _drop_now(self, feed: SymbolFeed) -> None:
        if feed.subs:
            return
        if feed.linger is not None:
            feed.linger.cancel()
            feed.linger = None
        runtime = feed.runtime
        if self._feeds.get(feed.symbol) is feed:
            del self._feeds[feed.symbol]
        runtime.feeds.pop(feed.symbol, None)
        runtime.by_upstream.pop(feed.upstream, None)
        if runtime.source is not None:
            await runtime.source.unsubscribe([feed.upstream])
        self._broadcast_connection(runtime)

    # ------------------------------------------------------------------ attaching a subscription

    async def _attach(self, sub: Sub, exclude: frozenset[str]) -> None:
        chart = await self._chart(sub)
        if sub.client.subs.get(sub.id) is not sub:
            return                                           # unsubscribed while the history was loading
        asset = symbol_map.classify(sub.symbol, chart.kind)
        sub.tick_size = self._tick_size(sub.symbol, asset)
        interval = sub.interval
        if isinstance(interval, RangeInterval) and interval.unit == "t" and sub.tick_size is None:
            raise HubError("market.bad_request", message=t("market.error.range_ticks_unknown"))
        seconds = not isinstance(interval, RangeInterval) and interval is not None and interval.unit == "s"
        assert self._lock is not None
        async with self._lock:
            feed = self._feeds.get(sub.symbol)
            self._check_interval_limit(sub.symbol, sub.tf)
            if feed is not None and feed.linger is not None:
                feed.linger.cancel()
                feed.linger = None
            if feed is not None and seconds and feed.runtime is self._fallback:
                raise HubError("market.seconds_need_ticks")
            if feed is None:
                runtime, upstream = self._route(sub.symbol, asset, exclude)
                if seconds and runtime is self._fallback:
                    raise HubError("market.seconds_need_ticks")      # the delayed fallback gives 1-minute bars at best
                feed = SymbolFeed(sub.symbol, asset, runtime, upstream,
                                  BarAggregator(zone(chart.timezone, 0), chart.kind, intervals=()),
                                  tape=None if runtime is self._fallback else TickTape(), tick_size=sub.tick_size)
                self._feeds[sub.symbol] = feed
                runtime.feeds[sub.symbol] = feed
                runtime.by_upstream[upstream] = feed
                try:
                    await self._ensure_source(runtime)
                    assert runtime.source is not None
                    await runtime.source.subscribe([upstream])
                except BaseException as error:
                    self._feeds.pop(sub.symbol, None)
                    runtime.feeds.pop(sub.symbol, None)
                    runtime.by_upstream.pop(upstream, None)
                    if isinstance(error, Exception) and runtime.state == "error" and runtime is not self._fallback:
                        feed = None                          # the provider refused us while we connected: next one
                    else:
                        raise
        if feed is None or (feed.runtime.state == "error" and not feed.subs and feed.runtime is not self._fallback):
            if feed is not None:
                await self._drop_if_empty(feed, linger=False)
            await self._attach(sub, exclude | {(feed.runtime if feed else runtime).id})
            return
        if sub.client.subs.get(sub.id) is not sub:
            await self._drop_if_empty(feed)                  # unsubscribed while connecting
            return
        sub.feed = feed
        feed.subs[(sub.client.id, sub.id)] = sub
        if isinstance(interval, RangeInterval):
            self._ensure_range(feed, sub, interval)
        else:
            feed.aggregator.add_interval(sub.tf)
        self._send_snapshot(sub, chart)
        self._broadcast_connection(feed.runtime)

    def _tick_size(self, symbol: str, asset: str | None) -> float | None:
        """The tick size of the instrument: the packaged table, or what a connected provider announced for a futures
        root the table does not have. A value a range cannot be built on is the same as none."""
        found = instruments.tick_size(symbol, asset)
        if found is None and asset == "future":
            match = symbol_map.FUTURE.match(symbol)
            for runtime in self._runtimes.values():
                announced = getattr(runtime.source, "announced", None)
                if match and announced is not None:
                    found = announced().get(match.group(1))
                    if found is not None:
                        break
        if found is None:
            return None
        try:
            RangeBarBuilder(found, found)
        except ValueError:
            return None
        return float(found)

    @staticmethod
    def _ensure_range(feed: SymbolFeed, sub: Sub, interval: RangeInterval) -> None:
        """The exact range bars of the feed: built from the whole tape the first time the range is watched, then fed
        tick by tick."""
        if sub.tf in feed.ranges:
            return
        size = interval.size * sub.tick_size if interval.unit == "t" and sub.tick_size else interval.size
        if feed.tape is not None and len(feed.tape):
            feed.ranges[sub.tf] = build_from_ticks(feed.tape, size, sub.tick_size)
        else:
            feed.ranges[sub.tf] = RangeBarBuilder(size, sub.tick_size)

    async def _chart(self, sub: Sub) -> ChartData:
        """The history of a time interval. Range and seconds charts build their own bars: they take the 1-minute bars
        (all of them, for the approximate range bars and the opening line) and everything else comes from the ticks."""
        own = self._builds_own_bars(sub)
        try:
            return await asyncio.to_thread(build_chart, self._history(), sub.symbol, "1m" if own else sub.tf,
                                           self._clock, MINUTES_KEEP if own else None)
        except MarketError as error:
            if error.code == UNKNOWN_SYMBOL:
                raise HubError("market.unknown_symbol") from None
            if error.code == INTERVAL_UNAVAILABLE:
                raise HubError("market.interval_unavailable", {**error.params, "interval": sub.tf} if own else error.params) from None
            if error.code == SECONDS_NEED_TICKS:
                raise HubError("market.seconds_need_ticks") from None
            raise HubError("market.down") from None

    @staticmethod
    def _builds_own_bars(sub: Sub) -> bool:
        interval = sub.interval
        return isinstance(interval, RangeInterval) or (interval is not None and interval.unit == "s")

    def _today(self):
        return datetime.fromtimestamp(self._clock(), timezone.utc).date()

    def _eligible(self, provider_id: str) -> bool:
        spec, runtime = self.catalog.get(provider_id), self._runtimes.get(provider_id)
        if spec is None or runtime is None or provider_id not in ADAPTERS or runtime.state == "error":
            return False
        return self.config.is_enabled(provider_id) and (not spec.needs_key or self.config.has_key(provider_id))

    def _candidates(self, symbol: str, asset: str | None, exclude: frozenset[str]) -> list[tuple[ProviderRuntime, str]]:
        """The providers that could serve the symbol, in the owner's priority order. Connects nothing."""
        candidates: list[tuple[ProviderRuntime, str]] = []
        if asset is not None:
            for provider_id in self.config.order(asset):
                spec = self.catalog.get(provider_id)
                if provider_id in exclude or spec is None or asset not in spec.asset_classes:
                    continue
                if not self._eligible(provider_id):
                    continue
                upstream = symbol_map.to_provider(provider_id, symbol, asset, self._today())
                if upstream is not None:
                    candidates.append((self._runtimes[provider_id], upstream))
        return candidates

    def preview_route(self, symbol: str, asset: str | None) -> dict:
        """Which source would serve `symbol` today: `{provider, source, data_state}`. The same choice as `_route`, with
        nothing connected, nothing subscribed and nothing counted."""
        candidates = self._candidates(symbol, asset, frozenset())
        chosen = next((runtime for runtime, upstream in candidates
                       if len(runtime.feeds) < runtime.spec.max_symbols or upstream in runtime.by_upstream),
                      candidates[0][0] if candidates else self._fallback)
        return {"provider": chosen.id, "source": chosen.name, "data_state": chosen.data_state}

    def live_classes(self) -> list[str]:
        """Asset classes with a provider that is switched on, has its key and gives live data."""
        out = []
        for asset in self.catalog.default_priority:
            for provider_id in self.config.order(asset):
                spec, runtime = self.catalog.get(provider_id), self._runtimes.get(provider_id)
                if spec and runtime and asset in spec.asset_classes and self._eligible(provider_id) and runtime.data_state == "live":
                    out.append(asset)
                    break
        return out

    def announced_roots(self) -> dict[str, float | None]:
        """Futures roots that connected providers announced, with their tick size when they said it."""
        found: dict[str, float | None] = {}
        for runtime in self._runtimes.values():
            announced = getattr(runtime.source, "announced", None)
            if announced is not None:
                found.update(announced())
        return found

    def _route(self, symbol: str, asset: str | None, exclude: frozenset[str]) -> tuple[ProviderRuntime, str]:
        candidates = self._candidates(symbol, asset, exclude)
        for runtime, upstream in candidates:
            if len(runtime.feeds) < runtime.spec.max_symbols or upstream in runtime.by_upstream:
                return runtime, upstream
        if candidates:
            raise HubError("market.limit")
        if len(self._fallback.feeds) >= POLLING_MAX_SYMBOLS and symbol not in self._fallback.feeds:
            raise HubError("market.limit")
        return self._fallback, symbol

    async def _ensure_source(self, runtime: ProviderRuntime) -> None:
        if runtime.source is not None:
            return
        if runtime is self._fallback:
            source: LiveSource = PollingLiveSource(self._history, self.catalog.polling)
        else:
            spec = runtime.spec
            assert spec is not None
            secret = await asyncio.to_thread(self.config.read_secret, spec.id) if spec.needs_key else None
            if spec.needs_key and not secret:
                raise HubError("market.down")
            try:
                source = ADAPTERS[spec.id](spec, secret, None)
            except Exception:
                log.warning("adapter %s could not be created", spec.id)
                raise HubError("market.down") from None
        runtime.source = source
        await source.connect()
        runtime.consumer = self._spawn(self._consume(runtime, source))

    async def _close_source(self, runtime: ProviderRuntime) -> None:
        source, runtime.source = runtime.source, None
        consumer, runtime.consumer = runtime.consumer, None
        if source is not None:
            await source.close()
        if consumer is not None:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        if runtime.state != "error":
            runtime.state = "not_connected"

    def _spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ events from providers

    async def _consume(self, runtime: ProviderRuntime, source: LiveSource) -> None:
        async for event in source.events():
            if runtime.source is not source:
                return
            try:
                self._on_event(runtime, event)
            except Exception:
                log.exception("market event failed")

    def _on_event(self, runtime: ProviderRuntime, event) -> None:
        if isinstance(event, (Tick, BarUpdate)):
            now = time.monotonic()
            for probe in runtime.probes:
                if probe.upstream == event.symbol and probe.data_at is None:
                    probe.data_at = now
            feed = runtime.by_upstream.get(event.symbol)
            if feed is None:
                return
            ranges: dict = {}
            if isinstance(event, Tick):
                changes = feed.aggregator.on_tick(event.ts_ms, event.price, event.size)
                price, ts_ms = event.price, event.ts_ms
                if feed.tape is not None:
                    first = not len(feed.tape)
                    stored = max(event.ts_ms, feed.tape.last_ts_ms or 0)       # the tape keeps time order
                    feed.tape.append(stored, event.price, event.size)
                    ranges = {key: builder.on_tick(stored, event.price, event.size) for key, builder in feed.ranges.items()}
                    if first and any(self._builds_own_bars(sub) for sub in feed.subs.values()):
                        self._spawn(self._resnapshot(feed, own_bars=True))     # where the exact bars begin is now known
            else:
                changes = feed.aggregator.on_bar_update(event)
                newest = event.start_ms >= feed.newest_bar_ms
                feed.newest_bar_ms = max(feed.newest_bar_ms, event.start_ms)
                price, ts_ms = (event.c if newest else None), event.start_ms
                if feed.ranges and feed.tape is None and newest and not feed.refreshing:
                    feed.refreshing = True                 # no ticks: the approximate range bars follow the 1m bars
                    self._spawn(self._refresh_ranges(feed))
            self._apply(feed, changes, price, ts_ms, ranges)
        elif isinstance(event, SourceStatus):
            self._on_status(runtime, event)
        elif isinstance(event, SymbolError):
            feed = runtime.by_upstream.get(event.symbol)
            if feed is not None:
                self._spawn(self._reject_feed(feed, event.code))
        elif isinstance(event, Rollover):
            feed = runtime.by_upstream.get(event.symbol)
            if feed is not None:
                feed.aggregator = BarAggregator(feed.aggregator.tz, feed.aggregator.kind, feed.aggregator.intervals)
                if feed.tape is not None:
                    feed.tape = TickTape()                 # another contract: the old ticks are at another price
                feed.ranges = {key: RangeBarBuilder(b.range_price, b.tick_size) for key, b in feed.ranges.items()}
                self._spawn(self._resnapshot(feed))

    async def _reject_feed(self, feed: SymbolFeed, code: str) -> None:
        for sub in list(feed.subs.values()):
            sub.client.subs.pop(sub.id, None)
            sub.client.push(self._envelope(sub, "error", code=code, message=market_message(code), params={}))
            await self._release(sub, linger=False)

    def _on_status(self, runtime: ProviderRuntime, event: SourceStatus) -> None:
        before = (runtime.state, runtime.data_state, runtime.error_code)
        previous_state, previous_data = runtime.state, runtime.data_state
        now = time.monotonic()
        if event.state in ("live", "delayed"):
            runtime.state, runtime.data_state, runtime.error_code = "connected", event.state, None
            if event.code == "subscribed":
                for probe in runtime.probes:
                    if probe.acked_at is None:
                        probe.acked_at = now
        elif event.state == "reconnecting":
            runtime.state, runtime.last_code = "reconnecting", event.code
            for probe in runtime.probes:
                probe.reconnects += 1
        elif event.state == "down":
            runtime.state = "error"
            runtime.error_code = DOWN_CODES.get(event.code, event.code if event.code.startswith("connections.")
                                                else "connections.unreachable")
        if before != (runtime.state, runtime.data_state, runtime.error_code):
            self._broadcast_connection(runtime)
        if runtime.feeds:
            self._tell_subs(runtime, event, previous_state)
            if (event.state in ("live", "delayed") and runtime.data_state != previous_data
                    and previous_state != "reconnecting"):
                for feed in runtime.feeds.values():          # the provider said its data state after the snapshot
                    self._spawn(self._resnapshot(feed))

    def _tell_subs(self, runtime: ProviderRuntime, event: SourceStatus, previous_state: str) -> None:
        if event.state == "down":
            self._spawn(self._failover(runtime))
            return
        if event.state == "reconnecting":
            state, code = "reconnecting", event.code or "network"
        elif previous_state == "reconnecting":
            for feed in runtime.feeds.values():
                self._spawn(self._resnapshot(feed))
            return
        else:
            return
        for feed in list(runtime.feeds.values()):
            for sub in list(feed.subs.values()):
                if sub.state != state:
                    sub.state = state
                    sub.client.push(self._envelope(sub, "status", state=state, code=code))

    async def _failover(self, runtime: ProviderRuntime) -> None:
        """The provider cannot serve any more: its subscriptions move to the next one or to the fallback."""
        moved: list[Sub] = [s for feed in list(runtime.feeds.values()) for s in feed.subs.values()]
        for sub in moved:
            await self._release(sub, linger=False)
            sub.closed = False
        for feed in list(runtime.feeds.values()):
            await self._drop_now(feed)                     # feeds that were only waiting out their linger
        await self._close_source(runtime)
        for sub in moved:
            if sub.client.subs.get(sub.id) is sub:
                try:
                    await self._attach(sub, exclude=frozenset({runtime.id}))
                except HubError as error:
                    sub.client.subs.pop(sub.id, None)
                    sub.client.push(self.error_message(error.code, sub.id, error.params, error.message))
        self._broadcast_connection(runtime)

    async def _resnapshot(self, feed: SymbolFeed, own_bars: bool = False, only_if_changed: bool = False) -> None:
        """A new snapshot for every subscription of the feed. `own_bars`: only those of range and seconds charts.
        `only_if_changed`: not for a range chart whose 1-minute bars are the same as in its last snapshot."""
        for sub in list(feed.subs.values()):
            if own_bars and not self._builds_own_bars(sub):
                continue
            try:
                chart = await self._chart(sub)
            except HubError as error:
                sub.client.push(self._envelope(sub, "error", code=error.code,
                                               message=market_message(error.code, error.params), params=error.params))
                continue
            if feed.subs.get((sub.client.id, sub.id)) is sub:
                if only_if_changed and sub.minutes is not None and chart.candles[-1:] == sub.minutes[-1:] \
                        and len(chart.candles) == len(sub.minutes):
                    continue                               # the 1m bars did not change: nothing to redraw
                self._send_snapshot(sub, chart)

    async def _refresh_ranges(self, feed: SymbolFeed) -> None:
        try:
            await self._resnapshot(feed, own_bars=True, only_if_changed=True)
        finally:
            feed.refreshing = False

    # ------------------------------------------------------------------ bars to subscriptions

    def _apply(self, feed: SymbolFeed, changes, price: float | None, ts_ms: int, ranges: dict | None = None) -> None:
        if not changes and not ranges:
            return                                           # a late tick or a correction of a bar already behind
        for sub in list(feed.subs.values()):
            touched = False
            for change in (ranges or {}).get(sub.tf, ()):
                bar = change.bar._replace(i=change.bar.i + sub.range_offset)
                self._keep_range_bar(sub, bar)
                if change.final:
                    sub.pending_final.append(bar)
                    sub.bar_dirty = touched = True
                elif bar != sub.last_bar:
                    sub.bar_dirty = touched = True
                    sub.last_bar = bar
            for timeframe, candle, final in changes:
                if timeframe != sub.tf:
                    continue
                start, end = feed.aggregator.bounds(candle.t, timeframe)
                merged = self._merge(sub, candle, start, end)
                if final:
                    sub.pending_final.append(merged)
                if final or merged != sub.last_bar:
                    sub.bar_dirty = touched = True
                    sub.last_bar = merged if not final else sub.last_bar
            if price is not None and (sub.last_price != price or sub.last_ts_ms != ts_ms):
                sub.price_dirty = touched = True
                sub.last_price, sub.last_ts_ms = price, ts_ms
            if touched:
                self._schedule(sub)

    @staticmethod
    def _keep_range_bar(sub: Sub, bar: RangeBar) -> None:
        bars = sub.rbars
        if bars and bars[-1].i == bar.i:
            bars[-1] = bar
        elif not bars or bars[-1].i < bar.i:
            bars.append(bar)
            del bars[:-RANGE_KEEP]

    @staticmethod
    def _merge(sub: Sub, live: Candle, start: int, end: int) -> Candle:
        candles = sub.candles
        exact = next((i for i in range(len(candles) - 1, -1, -1) if candles[i].t == live.t), None)
        for index in ([exact] if exact is not None else range(len(candles) - 1, -1, -1)):
            held = candles[index]
            if held.t < start:
                break
            if held.t < end:
                merged = Candle(held.t, held.o, max(held.h, live.h), min(held.l, live.l), live.c, max(held.v, live.v))
                candles[index] = merged
                return merged
        candles.append(live)
        del candles[:-(visible_bars(sub.tf) + 20)]
        return live

    def _schedule(self, sub: Sub) -> None:
        if sub.timer is not None or sub.closed or self._loop is None:
            return
        delay = max(0.0, sub.last_sent + MIN_INTERVAL - time.monotonic())
        sub.timer = self._loop.call_later(delay, self._flush, sub)

    def _flush(self, sub: Sub) -> None:
        """At most one message per call: a finished bar first, then the bar in progress, then the last price."""
        sub.timer = None
        if sub.closed:
            return
        if sub.state in ("closed", "reconnecting") and (sub.bar_dirty or sub.price_dirty):
            sub.state = sub.feed.runtime.data_state if sub.feed else "live"
            sub.client.push(self._envelope(sub, "status", state=sub.state, code="ok"))
            sub.last_sent = time.monotonic()
            self._schedule(sub)
            return
        if sub.pending_final:
            bar, final = sub.pending_final.pop(0), True
            message = self._bar_message(sub, bar, final)
        elif sub.bar_dirty:
            sub.bar_dirty = False
            bar = sub.last_bar or (sub.candles[-1] if sub.candles else None)
            if bar is None:
                return
            message = self._bar_message(sub, bar, False)
        elif sub.price_dirty:
            message = self._envelope(sub, "last", **self._price_fields(sub))
        else:
            return
        if sub.price_dirty and message["type"] in ("bar", "last"):
            sub.price_dirty = False
        sub.client.push(message)
        sub.last_sent = time.monotonic()
        if sub.pending_final or sub.bar_dirty or sub.price_dirty:
            self._schedule(sub)

    @staticmethod
    def _wire(bar: "Candle | RangeBar") -> dict:
        out = {"t": bar.t, "o": bar.o, "h": bar.h, "l": bar.l, "c": bar.c, "v": bar.v}
        if isinstance(bar, RangeBar):
            out["i"], out["approx"] = bar.i, bar.approx
        return out

    def _bar_message(self, sub: Sub, bar: "Candle | RangeBar", final: bool) -> dict:
        fields: dict = {"bar": self._wire(bar), "final": final, **self._price_fields(sub)}
        range_ = self._opening_range(sub)
        if range_ is not None:
            fields["opening_range"] = asdict(range_)
        return self._envelope(sub, "bar", **fields)

    def _opening_range(self, sub: Sub):
        """While the first 30 minutes of the session run, the high is recomputed from the live bars."""
        if sub.opening is not None and sub.opening.complete:
            return None
        if sub.minutes is not None:
            # Range and seconds charts: the line always comes from the 1-minute bars, whatever the bars on screen.
            held = {c.t: c for c in sub.minutes}
            if sub.feed is not None:
                held.update((c.t, c) for c in sub.feed.aggregator.minute_candles())
            seconds, source = 60, sorted(held.values(), key=lambda c: c.t)
        else:
            seconds, source = parse(sub.tf).seconds, sub.candles
        if not opening.supports(seconds):
            return None
        found = opening.opening_range(source, seconds, sub.kind, zone(sub.tz_name, 0))
        if found is None or found == sub.opening:
            return None
        sub.opening = found
        return found

    def _price_fields(self, sub: Sub) -> dict:
        price = sub.last_price if sub.last_price is not None else (sub.candles[-1].c if sub.candles else None)
        change = change_pct = None
        if price is not None and sub.prev_close:
            change = price - sub.prev_close
            change_pct = change / sub.prev_close * 100
        return {"price": price, "ts_ms": sub.last_ts_ms or int(self._clock() * 1000), "change": change,
                "change_pct": change_pct}

    # ------------------------------------------------------------------ messages

    def _runtime_of(self, sub: Sub) -> ProviderRuntime:
        return sub.feed.runtime if sub.feed is not None else self._fallback

    def _envelope(self, sub: Sub, message_type: str, /, **fields) -> dict:
        runtime = self._runtime_of(sub)
        sub.seq += 1
        return {"type": message_type, "sub": sub.id, "seq": sub.seq, "epoch": self.epoch, "source": runtime.name,
                "data_state": runtime.data_state, **fields}

    def _delay_seconds(self, runtime: ProviderRuntime, asset: str | None) -> int | None:
        if runtime.data_state == "live":
            return 0
        if runtime is self._fallback:
            return self.catalog.polling.delay_seconds.get(asset or "index", 900)
        return runtime.spec.delay_seconds if runtime.spec else None

    def _history_info(self, runtime: ProviderRuntime, asset: str | None) -> dict:
        """Where the bars before the first live trade come from. They always come from the polling source (Yahoo), even
        when the price runs live from another provider, so the panel can say so instead of calling the whole chart live."""
        delay = 0 if runtime is self._fallback else self.catalog.polling.delay_seconds.get(asset or "index", 900)
        return {"source": self.catalog.polling.name, "delay_seconds": delay}

    def _send_snapshot(self, sub: Sub, chart: ChartData) -> None:
        feed = sub.feed
        assert feed is not None
        runtime = feed.runtime
        interval = sub.interval or parse(sub.tf)
        sub.prev_close, sub.kind, sub.tz_name = chart.previous_close, chart.kind, chart.timezone
        sub.opening = chart.opening_range
        tape = feed.tape
        tape_from = tape.first_ts_ms // 1000 if tape is not None and len(tape) else None
        exact_from = approx_until = None
        protocol = interval.to_protocol(sub.tick_size)
        if isinstance(interval, RangeInterval):
            sub.minutes = list(chart.candles)
            builder = feed.ranges[sub.tf]
            exact = builder.bars()
            approx = approximate_from_minutes(sub.minutes, builder.range_price, builder.tick_size, until_ts=tape_from)
            merged, approx_until = splice(approx, exact, tape_from)
            sub.range_offset = len(merged) - len(exact)
            sub.candles, sub.rbars = [], merged[-RANGE_KEEP:]
            exact_from = tape_from
            if interval.unit == "p" and builder.range_price != interval.size:
                protocol["size"], protocol["rounded"] = builder.range_price, True      # rounded to the tick of the instrument
            shown: list = merged[-visible_bars(sub.tf):]
        elif interval.unit == "s":
            sub.minutes = list(chart.candles)
            sub.candles, sub.rbars = [], []
            if tape is not None and len(tape):
                since = (tape.last_ts_ms or 0) - (SECONDS_KEEP + 1) * interval.seconds * 1000
                sub.candles = feed.aggregator.replay(sub.tf, tape.iter_since(since))[-SECONDS_KEEP:]
            exact_from = tape_from
            shown = sub.candles
        else:
            sub.candles, sub.rbars = list(chart.candles), []
            live = feed.aggregator.current(sub.tf)
            if live is not None:
                start, end = feed.aggregator.bounds(live.t, sub.tf)
                self._merge(sub, live, start, end)
            shown = sub.candles[-visible_bars(sub.tf):]
        newest = (sub.rbars or sub.candles)[-1:]
        sub.last_bar = newest[0] if newest else None
        sub.pending_final.clear()
        sub.bar_dirty = sub.price_dirty = False
        last = newest[0].c if newest else chart.last_price
        sub.last_price, sub.last_ts_ms = last, chart.last_trade_at * 1000 if chart.last_trade_at else 0
        price = self._price_fields(sub)
        sub.state = ("reconnecting" if runtime.state == "reconnecting"
                     else "closed" if not chart.market_open and runtime is self._fallback else runtime.data_state)
        sub.client.push(self._envelope(
            sub, "snapshot", symbol=sub.symbol, tf=sub.tf, interval=protocol,
            exact_from=exact_from, approx_until=approx_until, tick_size=sub.tick_size, name=chart.name, kind=chart.kind,
            exchange=chart.exchange, currency=chart.currency, timezone=chart.timezone, asset_class=feed.asset_class,
            fallback=runtime is self._fallback, history=self._history_info(runtime, feed.asset_class),
            bars=[self._wire(c) for c in shown],
            last=price["price"], previous_close=chart.previous_close, change=price["change"],
            change_pct=price["change_pct"], opening_range=asdict(sub.opening) if sub.opening else None,
            market_open=chart.market_open, delay_seconds=self._delay_seconds(runtime, feed.asset_class)))
        sub.client.push(self._envelope(sub, "status", state=sub.state,
                                       code="market.closed" if sub.state == "closed" else "ok"))

    # ------------------------------------------------------------------ connections

    def _connection_message(self, runtime: ProviderRuntime) -> dict:
        spec = runtime.spec
        return {"type": "connection_status", "provider": runtime.id, "source": runtime.name, "epoch": self.epoch,
                "state": runtime.state, "data_state": runtime.data_state, "error_code": runtime.error_code,
                "enabled": self.config.is_enabled(runtime.id) if spec else True,
                "key_state": self._key_state(spec), "symbols_in_use": len(runtime.feeds)}

    def _key_state(self, spec: ProviderSpec | None) -> str:
        if spec is None or not spec.needs_key:
            return "not_required"
        return "saved" if self.config.has_key(spec.id) else "missing"

    def _broadcast_connection(self, runtime: ProviderRuntime) -> None:
        if runtime is self._fallback:
            return
        message = self._connection_message(runtime)
        for client in list(self._clients):
            client.push(message)

    def card(self, provider_id: str) -> dict:
        spec, runtime = self.catalog.providers[provider_id], self._runtimes[provider_id]
        priority = {c: self.config.order(c).index(provider_id) for c in spec.asset_classes}
        return {
            "id": provider_id, "name": spec.name, "asset_classes": list(spec.asset_classes),
            "needs_key": spec.needs_key, "key_fields": list(spec.key_fields),
            "key_state": self._key_state(spec), "enabled": self.config.is_enabled(provider_id),
            "state": runtime.state, "data_state": runtime.data_state, "error_code": runtime.error_code,
            "error_message": connection_message(runtime.error_code), "symbols_in_use": len(runtime.feeds),
            "max_symbols": spec.max_symbols, "priority": priority, "premium": spec.premium,
            "available": provider_id in ADAPTERS,
        }

    def cards(self) -> list[dict]:
        return [self.card(pid) for pid in self.catalog.providers]

    def groups(self) -> dict[str, list[str]]:
        return {c: self.config.order(c) for c in self.catalog.default_priority}

    async def ensure_started(self) -> None:
        """Warm the providers that are ready, once: they connect in the background, never in the caller's way."""
        self._bind()
        if self._started:
            return
        self._started = True
        for provider_id in self.catalog.providers:
            if self._eligible(provider_id):
                self._spawn(self.refresh(provider_id))

    async def refresh(self, provider_id: str, restart: bool = False) -> None:
        """The owner changed something about a provider: reconnect it, or move its subscriptions away."""
        self._bind()
        runtime = self._runtimes[provider_id]
        assert self._lock is not None
        if self._eligible(provider_id):
            async with self._lock:
                if restart:
                    await self._close_source(runtime)
                try:
                    await self._ensure_source(runtime)
                except HubError:
                    pass
                if runtime.source is not None:
                    await runtime.source.subscribe(list(runtime.by_upstream))
        else:
            await self._failover(runtime)
            runtime.reset_error()
        self._broadcast_connection(runtime)

    def forget_error(self, provider_id: str) -> None:
        self._runtimes[provider_id].reset_error()

    async def test_provider(self, provider_id: str, timeout: float | None = None) -> dict:
        """Open the provider's stream for real, subscribe to a test symbol of its class and wait for the first
        message. `latency_ms` is the time from the request to that message (it includes connecting when the stream
        was not open yet). A subscription the provider acknowledges but that stays silent (market closed) counts as
        connected with `first_data` false."""
        self._bind()
        timeout = TEST_TIMEOUT if timeout is None else timeout
        spec, runtime = self.catalog.providers[provider_id], self._runtimes[provider_id]
        if provider_id not in ADAPTERS:
            return self._test_failure(runtime, "connections.unavailable")
        if spec.needs_key and not self.config.has_key(provider_id):
            return self._test_failure(runtime, "connections.no_key")
        asset = symbol_map.classify(spec.test_symbol)
        upstream = symbol_map.to_provider(provider_id, spec.test_symbol, asset, self._today())
        if upstream is None:
            return self._test_failure(runtime, "connections.unavailable")
        runtime.reset_error()
        assert self._lock is not None
        started = time.monotonic()
        probe = _Probe(upstream)
        runtime.probes.append(probe)
        try:
            async with self._lock:
                try:
                    await self._ensure_source(runtime)
                except HubError:
                    return self._test_failure(runtime, "connections.no_key")
            assert runtime.source is not None
            await runtime.source.subscribe([upstream])
            deadline = started + timeout
            while time.monotonic() < deadline:
                if probe.reconnects >= UNREACHABLE_AFTER and probe.data_at is None:
                    break                                      # three failed attempts in a row: say so now
                if runtime.state == "error":
                    return self._test_failure(runtime, runtime.error_code or "connections.unreachable")
                if probe.data_at is not None:
                    return self._test_success(runtime, spec, probe.data_at - started, True)
                if probe.acked_at is not None and time.monotonic() - probe.acked_at >= ACK_GRACE:
                    return self._test_success(runtime, spec, probe.acked_at - started, False)
                await asyncio.sleep(0.02)
            if probe.data_at is not None:
                return self._test_success(runtime, spec, probe.data_at - started, True)
            network = runtime.state == "reconnecting" and runtime.last_code in ("network", "timeout", "dropped", "closed")
            return self._test_failure(runtime, "connections.unreachable" if network else "connections.timeout")
        finally:
            runtime.probes.remove(probe)
            if runtime.source is not None and upstream not in runtime.by_upstream:
                await runtime.source.unsubscribe([upstream])
            if runtime.source is not None and not self.config.is_enabled(provider_id) and not runtime.feeds:
                await self._close_source(runtime)
            self._broadcast_connection(runtime)

    def _test_success(self, runtime: ProviderRuntime, spec: ProviderSpec, seconds: float, first_data: bool) -> dict:
        return {"ok": True, "state": "connected", "data_state": runtime.data_state if runtime.state == "connected"
                else spec.data_state, "latency_ms": max(0, int(seconds * 1000)), "symbol": spec.test_symbol,
                "first_data": first_data, "error_code": None, "message": ""}

    def _test_failure(self, runtime: ProviderRuntime, code: str) -> dict:
        return {"ok": False, "state": runtime.state, "data_state": runtime.data_state, "latency_ms": None,
                "symbol": runtime.spec.test_symbol if runtime.spec else None, "first_data": False,
                "error_code": code, "message": connection_message(code)}

    # ------------------------------------------------------------------ shutdown

    async def close(self) -> None:
        for feed in list(self._feeds.values()):
            if feed.linger is not None:
                feed.linger.cancel()
                feed.linger = None
            for sub in feed.subs.values():
                sub.closed = True
                if sub.timer is not None:
                    sub.timer.cancel()
        for runtime in [*self._runtimes.values(), self._fallback]:
            await self._close_source(runtime)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._feeds.clear()
        self._clients.clear()

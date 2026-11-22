"""The option chain that moves by itself (SPEC-016 4.3): the `options` subscription of `/ws/market`.

    {"op":"options","sub":"o1","symbol":"QQQ","expirations":["2026-11-27"],"strikes":14}     {"op":"options_window","sub":"o1","strikes":20,"offset":2}
    {"op":"unsubscribe","sub":"o1"}

The structure (every strike of each expiration) is asked of Schwab ONCE, when the chain opens, when an expiration changes and after a reconnection.
The visible strikes are N/2 below and N/2 above the price of the underlying that the stream gives (the same Quote as the chart), cut from the
whole list. The Streamer is asked for those contracts plus a margin of strikes around them, and each message of a contract becomes a change of its
cell: bid, ask, last, mark, volume, open interest, IV and greeks as Schwab gives them. Prob. ITM, intrinsic value and the expected move are the
only things calculated here, from those inputs. Nothing refreshes by timer.

Messages to the panel (each with `sub`, `seq`, `epoch`, `source`, `data_state`): `options_snapshot` (the visible rows), `options` (changes by contract),
`options_limit` (the limit of symbols of the source: those contracts are marked with no live data and not asked again), `options_status`
(reconnecting or live again). A source without streaming options answers `market.options_not_live`: the panel keeps the photo of the REST chain.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field

from hokma.market import options, symbols as symbol_map
from hokma.market.adapters.schwab import normalize_option
from hokma.market.stream import LimitReached, OptionQuote

log = logging.getLogger("hokma.market")

SPOT_WAIT_SECONDS = 5.0
MAX_OPTION_SUBS_PER_CLIENT = 6
RING_MIN = 4                         # strikes of margin on each side of the visible ones


@dataclass
class OptionsSub:
    client: object
    id: str
    symbol: str
    runtime: object
    upstream: str
    groups: list[dict]               # every strike of every expiration, as `options.structure` gives them
    count: int | None
    today: str
    offset: int = 0
    delayed: bool = False
    seq: int = 0
    keys: dict[str, str] = field(default_factory=dict)         # normalized key -> the symbol of the chain, for what is followed
    visible: set[str] = field(default_factory=set)             # symbols of the chain on screen
    cells: dict[str, dict] = field(default_factory=dict)       # symbol of the chain -> last live values
    limited: set[str] = field(default_factory=set)
    dirty: set[str] = field(default_factory=set)
    spot: float | None = None
    checked: float = 0.0                                       # monotonic time of the last read of market_open
    market_open: bool | None = None                            # from /markets with the holidays: decides the reference price (options.reference_price)
    spot_dirty: bool = False
    timer: asyncio.TimerHandle | None = None
    last_sent: float = 0.0
    closed: bool = False


class OptionSubs:
    def __init__(self, hub, min_interval: float) -> None:
        self.hub = hub
        self.min_interval = min_interval
        self.subs: dict[tuple[str, str], OptionsSub] = {}
        self._refs: dict[tuple[str, str], int] = {}              # (provider, normalized key) -> subscriptions that follow it
        self._under: dict[tuple[str, str], int] = {}             # (provider, upstream underlying) -> options subscriptions on it

    # ---------------------------------------------------------------- open, move, close

    async def attach(self, client, sub_id: str, symbol: str, dates: list[str], strikes, desk_id: int | None) -> None:
        from hokma.market.hub import HubError                    # the hub imports this module: no import cycle at load

        hub = self.hub
        if (client.id, sub_id) in self.subs or sum(1 for k in self.subs if k[0] == client.id) >= MAX_OPTION_SUBS_PER_CLIENT:
            raise HubError("market.limit")
        try:
            symbol = options._check_equity(symbol)
            count = options._strike_count(strikes)
        except options.OptionsError as error:
            raise HubError("market.bad_request", message=str(error)) from None
        assert hub._lock is not None
        async with hub._lock:
            runtime, upstream = hub._route(symbol, "equity", frozenset())
            await hub._ensure_source(runtime)
        source = runtime.source
        if runtime is hub._fallback or source is None or not source.capabilities.live_options:
            raise HubError("market.options_not_live")
        key = (runtime.id, upstream)
        self._under[key] = self._under.get(key, 0) + 1
        await source.subscribe([upstream])
        try:
            spot = await self._wait_spot(runtime, upstream)
            data = await asyncio.to_thread(options.structure, symbol, dates, spot, runtime.id)
        except options.OptionsError as error:
            await self._release_underlying(runtime, upstream)
            raise HubError("market.bad_request" if error.code == "bad_request" else "market.down", message=str(error)) from None
        except BaseException:
            await self._release_underlying(runtime, upstream)
            raise
        sub = OptionsSub(client, sub_id, symbol, runtime, upstream, data["groups"], count, data["today"], delayed=data["delayed"], spot=spot, market_open=data.get("market_open"),
                         checked=time.monotonic())
        self.subs[(client.id, sub_id)] = sub
        client.subs[sub_id] = sub                                 # one list of subscriptions per socket: closing it releases this one too
        await self._plan(sub, send=True)

    async def window(self, client, sub_id: str, strikes, offset) -> None:
        from hokma.market.hub import HubError

        sub = self.subs.get((client.id, sub_id))
        if sub is None:
            raise HubError("market.bad_request")
        try:
            if strikes is not None:
                sub.count = options._strike_count(strikes)
        except options.OptionsError as error:
            raise HubError("market.bad_request", message=str(error)) from None
        if isinstance(offset, int) and not isinstance(offset, bool):
            sub.offset = max(-200, min(200, offset))
        sub.spot = self._spot_of(sub) or sub.spot
        await self._refresh_open(sub)
        await self._plan(sub, send=True)

    async def release(self, client, sub_id: str) -> bool:
        sub = self.subs.pop((client.id, sub_id), None)
        if sub is None:
            return False
        sub.closed = True
        if sub.timer is not None:
            sub.timer.cancel()
        await self._unfollow(sub, list(sub.keys))
        await self._release_underlying(sub.runtime, sub.upstream)
        return True

    # ---------------------------------------------------------------- the strikes on screen and the contracts followed

    async def _plan(self, sub: OptionsSub, send: bool) -> None:
        """Choose the visible strikes around the stream's price, ask the Streamer for them and a margin, and drop what left."""
        visible_rows: list[list[dict]] = []
        want: dict[str, str] = {}
        sub.visible = set()
        for group in sub.groups:
            strikes = sorted({row["strike"] for row in group["rows"]})
            centre = self._centre(strikes, sub.spot, sub.offset)
            shown = options._window(strikes, centre, sub.count)
            ring = options._window(strikes, centre, None if sub.count is None else sub.count + 2 * max(RING_MIN, sub.count // 2))
            by_strike = {row["strike"]: row for row in group["rows"]}
            visible_rows.append([by_strike[s] for s in shown])
            for strike in ring:
                for side in ("call", "put"):
                    cell = by_strike[strike].get(side)
                    if cell and cell.get("symbol"):
                        want[normalize_option(cell["symbol"])] = cell["symbol"]
            for strike in shown:
                for side in ("call", "put"):
                    cell = by_strike[strike].get(side)
                    if cell and cell.get("symbol"):
                        sub.visible.add(cell["symbol"])
        want = {k: v for k, v in want.items() if v not in sub.limited}
        add = [k for k in want if k not in sub.keys]
        gone = [k for k in sub.keys if k not in want]
        sub.keys = want
        await self._follow(sub, add)
        await self._unfollow(sub, gone, forget=False)
        if send:
            sub.client.push(self._snapshot(sub, visible_rows))

    @staticmethod
    def _centre(strikes: list[float], spot: float | None, offset: int) -> float | None:
        if not strikes:
            return spot
        if spot is None:
            return spot if not offset else strikes[len(strikes) // 2]
        index = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        return strikes[max(0, min(len(strikes) - 1, index + offset))] if offset else spot

    async def _follow(self, sub: OptionsSub, keys: list[str]) -> None:
        fresh = []
        for key in keys:
            ref = (sub.runtime.id, key)
            self._refs[ref] = self._refs.get(ref, 0) + 1
            if self._refs[ref] == 1:
                fresh.append(key)
        if fresh and sub.runtime.source is not None:
            await sub.runtime.source.subscribe_options(fresh)

    async def _unfollow(self, sub: OptionsSub, keys: list[str], forget: bool = True) -> None:
        stale = []
        for key in keys:
            ref = (sub.runtime.id, key)
            left = self._refs.get(ref, 0) - 1
            if left <= 0:
                self._refs.pop(ref, None)
                stale.append(key)
            else:
                self._refs[ref] = left
        if stale and sub.runtime.source is not None:
            await sub.runtime.source.unsubscribe_options(stale)
        if forget:
            sub.keys = {}

    async def _release_underlying(self, runtime, upstream: str) -> None:
        key = (runtime.id, upstream)
        left = self._under.get(key, 0) - 1
        if left > 0:
            self._under[key] = left
            return
        self._under.pop(key, None)
        if upstream not in runtime.by_upstream and runtime.source is not None:
            await runtime.source.unsubscribe([upstream])

    async def _wait_spot(self, runtime, upstream: str) -> float | None:
        end = time.monotonic() + SPOT_WAIT_SECONDS
        while time.monotonic() < end:
            quote = self.hub._quotes.get((runtime.id, upstream))
            if quote is not None and quote.last is not None:
                return quote.last
            await asyncio.sleep(0.02)
        return None

    async def _refresh_open(self, sub: OptionsSub) -> None:
        """Read again whether the regular session is open (`/markets` is cached 5 minutes): a chain left open across the bell changes its reference price."""
        sub.checked = time.monotonic()
        try:
            sub.market_open = await asyncio.to_thread(options._market_hours_open)
        except Exception:                                        # noqa: BLE001  (the last known state stays)
            pass

    def _reference(self, sub: OptionsSub) -> float | None:
        """The price intrinsic value, in the money, Prob. ITM and the expected move are measured against: the underlying's bid with the market
        closed (what thinkorswim does) and its last price otherwise (`options.reference_price`)."""
        quote = self.hub._quotes.get((sub.runtime.id, sub.upstream))
        return options.reference_price(sub.spot, quote.bid if quote is not None else None, sub.market_open)

    def _spot_of(self, sub: OptionsSub) -> float | None:
        quote = self.hub._quotes.get((sub.runtime.id, sub.upstream))
        return quote.last if quote is not None else None

    # ---------------------------------------------------------------- messages

    def _envelope(self, sub: OptionsSub, kind: str, **fields) -> dict:
        sub.seq += 1
        runtime = sub.runtime
        return {"type": kind, "sub": sub.id, "seq": sub.seq, "epoch": self.hub.epoch, "source": runtime.name,
                "data_state": runtime.data_state, **fields}

    def _snapshot(self, sub: OptionsSub, visible_rows: list[list[dict]]) -> dict:
        today = options.date.fromisoformat(sub.today)
        ref = self._reference(sub)
        groups = []
        for group, rows in zip(sub.groups, visible_rows):
            day = options.date.fromisoformat(group["date"])
            years = options.years_to_expiry(day)
            merged = [self._merge_row(sub, row, ref, years) for row in rows]
            built = options._group(day, today, group["multiplier"], merged, ref, years, None, False) if merged else dict(group, rows=[])
            built.pop("feed", None)
            built.pop("truncated", None)
            groups.append(built)
        return self._envelope(sub, "options_snapshot", underlying=sub.symbol, underlying_price=sub.spot, count=sub.count, offset=sub.offset,
                              delayed=sub.delayed, expirations=groups, limited=sorted(sub.limited))

    def _merge_row(self, sub: OptionsSub, row: dict, spot: float | None, years: float) -> dict:
        out = {"strike": row["strike"], "call": None, "put": None}
        for side in ("call", "put"):
            cell = row.get(side)
            if cell is None:
                continue
            live = sub.cells.get(cell["symbol"])
            out[side] = dict(cell, **self._live_cell(cell, live or {}, row["strike"], side, spot, years))      # the derived values follow the reference price even before the first message
            out[side]["live"] = live is not None and cell["symbol"] not in sub.limited
        return out

    @staticmethod
    def _live_cell(cell: dict, live: dict, strike: float, side: str, spot: float | None, years: float) -> dict:
        iv = live.get("iv") if live.get("iv") is not None else cell.get("iv")
        # A field the stream has not sent (None) never wipes what the REST chain gave: out of hours that left Last, Bid and Ask as N/A (tarea 3328)
        return {**{k: v for k, v in live.items() if k not in ("ts_ms",) and v is not None}, "iv": iv,
                "intrinsic": options.intrinsic_value(side, spot, strike), "itm": options.in_the_money(side, spot, strike) if spot is not None else cell.get("itm"),
                "prob_itm": options.prob_itm(side, spot, strike, iv, years)}

    # ---------------------------------------------------------------- what the source sends

    def on_spot(self, runtime, upstream: str, last: float | None) -> None:
        if last is None:
            return
        for sub in self.subs.values():
            if sub.runtime is runtime and sub.upstream == upstream and sub.spot != last:
                sub.spot, sub.spot_dirty = last, True
                self._schedule(sub)

    def on_quote(self, runtime, quote: OptionQuote) -> None:
        key = normalize_option(quote.symbol)
        for sub in self.subs.values():
            symbol = sub.keys.get(key)
            if sub.runtime is not runtime or symbol is None:
                continue
            sub.cells[symbol] = {"bid": quote.bid, "ask": quote.ask, "last": options.traded(quote.last), "mark": quote.mark, "volume": quote.volume,
                                 "open_interest": quote.open_interest, "iv": options._iv(quote.iv), "delta": quote.delta, "gamma": quote.gamma,
                                 "theta": quote.theta, "vega": quote.vega, "rho": quote.rho, "theoretical": quote.theoretical,
                                 "quote_time": options._iso(quote.ts_ms / 1000), "delayed": quote.delayed}
            if symbol in sub.visible:
                sub.dirty.add(symbol)
                self._schedule(sub)

    def on_limit(self, runtime, event: LimitReached) -> None:
        if event.kind != "options":
            return
        gone = {normalize_option(s) for s in event.symbols}
        for key in gone:
            self._refs.pop((runtime.id, key), None)
        for sub in self.subs.values():
            if sub.runtime is not runtime:
                continue
            hit = [sub.keys.pop(k) for k in list(sub.keys) if k in gone]
            if hit:
                sub.limited.update(hit)
                sub.dirty.update(s for s in hit if s in sub.visible)
                sub.client.push(self._envelope(sub, "options_limit", symbols=sorted(hit), open=len(sub.keys)))
                self._schedule(sub)

    def on_state(self, runtime, state: str, code: str) -> None:
        for sub in self.subs.values():
            if sub.runtime is runtime:
                sub.client.push(self._envelope(sub, "options_status", state=state, code=code))

    def _schedule(self, sub: OptionsSub) -> None:
        loop = self.hub._loop
        if sub.timer is not None or sub.closed or loop is None:
            return
        sub.timer = loop.call_later(max(0.0, sub.last_sent + self.min_interval - time.monotonic()), self._flush, sub)

    def _flush(self, sub: OptionsSub) -> None:
        sub.timer = None
        if sub.closed:
            return
        if time.monotonic() - sub.checked >= options.HOURS_SECONDS and self.hub._loop is not None:
            sub.checked = time.monotonic()
            self.hub._loop.create_task(self._refresh_open(sub))
        by_symbol = {}
        ref = self._reference(sub)
        for group in sub.groups:
            years = options.years_to_expiry(options.date.fromisoformat(group["date"]))
            for row in group["rows"]:
                for side in ("call", "put"):
                    cell = row.get(side)
                    if cell and (cell["symbol"] in sub.dirty or (sub.spot_dirty and cell["symbol"] in sub.visible)):
                        base = self._live_cell(cell, sub.cells.get(cell["symbol"], {}), row["strike"], side, ref, years)
                        by_symbol[cell["symbol"]] = {"symbol": cell["symbol"], **base, "live": cell["symbol"] not in sub.limited
                                                     and cell["symbol"] in sub.cells}
        sub.dirty.clear()
        sub.spot_dirty = False
        if not by_symbol:
            return
        sub.client.push(self._envelope(sub, "options", underlying_price=sub.spot, changes=list(by_symbol.values())))
        sub.last_sent = time.monotonic()

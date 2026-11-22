"""SPEC-016 T4: the hub with a streaming Schwab-like source. The source is a fake that emits what Schwab's Streamer gives (Quote and 1 minute
candles); the history is a fake of `pricehistory`. No network. Dates are Friday 20 November 2026 (New York is UTC-5)."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from hokma.market import hub as hub_module
from hokma.market.adapters import ADAPTERS, register_adapter
from hokma.market.hours import ProviderHours, Segment
from hokma.market.hub import MarketHub, PanelClient
from hokma.market.models import Candle, Series, SessionPeriod
from hokma.market.providers import get_catalog
from hokma.market.stream import BarUpdate, LiveSource, Quote, SourceCapabilities, SourceStatus
from hokma.market.tz import NEW_YORK


def ny(hour: int, minute: int = 0, day: int = 20) -> int:
    return int(datetime(2026, 11, day, hour, minute, tzinfo=NEW_YORK).timestamp())


DAY = ny(0)                                   # the daily candle of Schwab: midnight New York (the source decides; the hub follows)
NOW = ny(10, 0)


def day_hours() -> ProviderHours:
    """What Schwab's /markets says for the day: three pieces, with the names it gives."""
    return ProviderHours("equity", "EQ", "2026-11-20", True, (Segment("preMarket", ny(7), ny(9, 30)), Segment("regularMarket", ny(9, 30), ny(16)),
                                                          Segment("postMarket", ny(16), ny(20))))


class FakeStreamSource(LiveSource):
    provider_id = "schwab"
    name = "Schwab"
    streaming = True
    ticks = False

    def __init__(self, *args, streaming: bool = True, ticks: bool = False) -> None:
        super().__init__()
        self.capabilities = SourceCapabilities(streaming, 100, ("equity",), 0, ticks=ticks, clock_aligned=True)
        self.subscribed: list[str] = []
        self.hours: ProviderHours | None = day_hours()
        self.hours_calls = 0

    async def market_hours(self, asset_class):
        self.hours_calls += 1
        return self.hours

    async def connect(self) -> None:
        import asyncio as _a
        self._queue = self._queue or _a.Queue(1000)
        self._ready = True
        self.emit(SourceStatus("live", "connected"))

    async def subscribe(self, symbols):
        self.subscribed += list(symbols)

    async def unsubscribe(self, symbols):
        pass

    async def close(self) -> None:
        self._closing = True


class Config:
    loaded = True

    def order(self, use):
        return list(get_catalog().default_priority[use])

    def is_enabled(self, provider_id):
        return provider_id == "schwab"

    def has_key(self, provider_id):
        return True

    def signed_in(self, provider_id):
        return True

    def read_secret(self, provider_id):
        return "x"


class History:
    """`pricehistory` as the hub asks for it: daily candles and the minutes of the day, every session included."""
    name = "Schwab"

    def __init__(self, daily: list[Candle], five: list[Candle]) -> None:
        self.daily, self.five = daily, five

    def search(self, query):
        return []

    def history(self, symbol, base):
        candles = {"1D": self.daily, "5m": self.five}.get(base, self.five)
        return Series(symbol, symbol, "equity", "Schwab", "USD", "America/New_York", 0, list(candles), None, NOW,
                      SessionPeriod(ny(9, 30), ny(16)), NOW, source="Schwab", clock_aligned=True)


def daily_history() -> list[Candle]:
    return [Candle(ny(0, 0, 18), 498.0, 501.0, 497.0, 500.0, 5_000_000.0), Candle(ny(0, 0, 19), 500.0, 503.0, 499.0, 502.0, 6_000_000.0),
            Candle(DAY, 502.0, 505.0, 501.0, 504.0, 4_000_000.0)]


def five_history() -> list[Candle]:
    return [Candle(ny(9, 25), 503.9, 504.0, 503.8, 503.95, 100.0), Candle(ny(9, 30), 504.0, 504.5, 503.9, 504.4, 900.0)]


@pytest.fixture
def source(monkeypatch):
    holder: dict = {}
    previous = ADAPTERS["schwab"]

    def make(spec, secret, url):
        holder["source"] = FakeStreamSource(streaming=holder.get("streaming", True), ticks=holder.get("ticks", False))
        if "night" in holder:
            holder["source"].hours = holder["night"]
        return holder["source"]

    register_adapter("schwab", make)
    yield holder
    register_adapter("schwab", previous)


def hub_of(history=None, clock=lambda: NOW) -> MarketHub:
    return MarketHub(get_catalog(), Config(), lambda: history or History(daily_history(), five_history()), clock=clock)


async def open_chart(hub: MarketHub, tf: str, symbol: str = "QQQ") -> tuple[PanelClient, dict]:
    client = PanelClient()
    hub.attach_client(client)
    await hub.subscribe(client, "s1", symbol, tf)
    return client, await message(client, lambda m: m["type"] in ("snapshot", "error"))


async def message(client: PanelClient, predicate, timeout: float = 3.0) -> dict:
    end = asyncio.get_running_loop().time() + timeout
    seen = []
    while asyncio.get_running_loop().time() < end:
        while not client.outbox.empty():
            item = json.loads(client.outbox.get_nowait())
            seen.append(item)
            if predicate(item):
                return item
        await asyncio.sleep(0.01)
    raise AssertionError(f"message not seen; got {[(m['type'], m.get('code')) for m in seen]}")


def run(coroutine) -> None:
    asyncio.run(coroutine)


def quote(**fields) -> Quote:
    base = dict(symbol="QQQ", ts_ms=ny(10, 1) * 1000, bid=504.38, ask=504.42, last=504.4, mark=504.4, volume=4_100_000.0,
                open=502.0, high=505.0, low=501.0, prev_close=502.0, regular_last=504.4, session="regular",
                regular_ts_ms=ny(10, 1) * 1000)
    base.update(fields)
    return Quote(**base)


# ---------------------------------------------------------------------------------------------- price


def test_a_quote_moves_the_price_with_its_bid_ask_and_mark_and_the_change_uses_the_source_previous_close(source):
    async def go():
        hub = hub_of()
        client, snap = await open_chart(hub, "5m")
        assert snap["type"] == "snapshot" and snap["source"] == "Schwab" and snap["fallback"] is False
        source["source"].emit(quote(last=504.6, bid=504.58, ask=504.62, mark=504.6, prev_close=500.0))
        last = await message(client, lambda m: m["type"] == "last" and m["price"] == 504.6)
        assert last["quote"]["bid"] == 504.58 and last["quote"]["ask"] == 504.62 and last["quote"]["mark"] == 504.6
        assert round(last["change"], 2) == 4.6                        # against the previous close of the source (field 12)
        source["source"].emit(quote(last=504.6, bid=504.50, ask=504.62, mark=504.56, prev_close=500.0))
        again = await message(client, lambda m: m["type"] == "last" and m["quote"]["bid"] == 504.5)
        assert again["price"] == 504.6                                # a change of the bid alone still reaches the panel
        await hub.close()
    run(go())


def test_the_hub_pushes_at_most_20_messages_a_second_per_subscription():
    assert hub_module.MIN_INTERVAL == 0.05


# ---------------------------------------------------------------------------------------------- the day candle


def test_the_day_candle_is_the_one_of_the_source_and_never_two_for_the_same_day(source):
    async def go():
        hub = hub_of()
        client, snap = await open_chart(hub, "1D")
        assert [b["t"] for b in snap["bars"]][-1] == DAY
        source["source"].emit(quote(open=502.0, high=506.0, low=500.5, volume=4_200_000.0, regular_last=505.5, last=505.5))
        bar = await message(client, lambda m: m["type"] == "bar" and m["bar"]["c"] == 505.5)
        assert (bar["bar"]["t"], bar["bar"]["h"], bar["bar"]["l"], bar["bar"]["v"], bar["final"]) == (DAY, 506.0, 500.5, 4_200_000.0, False)
        sub = next(iter(hub._feeds["QQQ"].subs.values()))
        assert [c.t for c in sub.candles].count(DAY) == 1 and len(sub.candles) == 3
        await hub.close()
    run(go())


def test_a_trade_after_hours_moves_the_price_but_not_the_close_of_the_day_candle(source):
    async def go():
        hub = hub_of()
        client, _ = await open_chart(hub, "1D")
        source["source"].emit(quote(ts_ms=ny(17, 30) * 1000, last=506.5, regular_last=504.4, session="extended",
                                    regular_ts_ms=ny(16, 0) * 1000))
        bar = await message(client, lambda m: m["type"] == "bar" and m["bar"]["t"] == DAY)
        assert bar["price"] == 506.5 and bar["quote"]["session"] == "extended"      # the price is the last trade, any session
        assert bar["bar"]["c"] == 504.4                                             # the day candle closes with the regular session
        await hub.close()
    run(go())


def test_zeros_of_the_morning_reset_are_no_candle(source):
    async def go():
        hub = hub_of()
        client, _ = await open_chart(hub, "1D")
        source["source"].emit(quote(open=0.0, high=0.0, low=0.0, regular_last=None, last=504.4))
        await message(client, lambda m: m["type"] == "last" and m["price"] == 504.4)
        with pytest.raises(AssertionError):
            await message(client, lambda m: m["type"] == "bar", timeout=0.3)
        await hub.close()
    run(go())


def test_a_new_day_gets_a_new_candle_at_the_time_of_day_the_source_uses(source):
    async def go():
        hub = hub_of()
        client, _ = await open_chart(hub, "1D")
        monday = ny(10, 0, 23)
        source["source"].emit(quote(ts_ms=monday * 1000, regular_ts_ms=monday * 1000, open=506.0, high=507.0, low=505.0,
                                    regular_last=506.5, last=506.5))
        bar = await message(client, lambda m: m["type"] == "bar" and m["bar"]["c"] == 506.5)
        assert bar["bar"]["t"] == ny(0, 0, 23)
        await hub.close()
    run(go())


# ---------------------------------------------------------------------------------------------- candles of the minute and the sessions


def test_candles_of_every_session_arrive_as_the_source_sends_them_and_fold_on_the_clock(source):
    async def go():
        hub = hub_of()
        client, snap = await open_chart(hub, "5m")
        assert [b["t"] for b in snap["bars"]] == [ny(9, 25), ny(9, 30)]          # the pre-market bar is not dropped
        feed_source = source["source"]
        feed_source.emit(BarUpdate("QQQ", ny(9, 31) * 1000, 504.4, 504.7, 504.3, 504.6, 50.0))
        bar = await message(client, lambda m: m["type"] == "bar" and m["bar"]["t"] == ny(9, 30))
        assert bar["bar"]["h"] == 504.7 and bar["final"] is False
        feed_source.emit(BarUpdate("QQQ", ny(16, 5) * 1000, 505.0, 505.1, 504.9, 505.0, 5.0))        # after hours, not cut
        after = await message(client, lambda m: m["type"] == "bar" and m["bar"]["t"] == ny(16, 5))
        assert after["bar"]["c"] == 505.0
        await hub.close()
    run(go())


def test_the_hour_folds_on_the_clock_from_30_minute_bars():
    from hokma.market import history as history_module

    folded = history_module._fold_to_hours([Candle(ny(9, 30), 1, 2, 0.5, 1.5, 10), Candle(ny(10, 0), 1.5, 3, 1, 2, 20),
                                            Candle(ny(10, 30), 2, 2.5, 1.8, 2.2, 5)])
    assert [(c.t, c.o, c.h, c.l, c.c, c.v) for c in folded] == [(ny(9, 0), 1, 2, 0.5, 1.5, 10), (ny(10, 0), 1.5, 3, 1, 2.2, 25)]


# ---------------------------------------------------------------------------------------------- only streaming, no trades


@pytest.mark.parametrize("tf", ["15s", "5rp", "10r"])
def test_a_source_without_trades_refuses_seconds_and_ranges_with_its_reason(source, tf):
    async def go():
        hub = hub_of()
        _, answer = await open_chart(hub, tf)
        assert answer["type"] == "error" and answer["code"] == "market.seconds_need_ticks"
        await hub.close()
    run(go())


def test_an_adapter_that_is_not_a_stream_gives_no_live_data(source):
    source["streaming"] = False

    async def go():
        hub = hub_of()
        _, answer = await open_chart(hub, "5m")
        assert answer["type"] == "error" and answer["code"] == "market.down"
        assert hub._runtimes["schwab"].feeds == {}
        await hub.close()
    run(go())


# ---------------------------------------------------------------------------------------------- the hours of the provider (T23)


def test_the_snapshot_carries_the_pieces_of_the_day_the_provider_named_and_open_comes_from_them(source):
    async def go():
        hub = hub_of()
        _, snap = await open_chart(hub, "5m")
        assert snap["market_open"] is True
        assert [(s["name"], s["start"], s["end"]) for s in snap["sessions"]] == [("preMarket", ny(7), ny(9, 30)), ("regularMarket", ny(9, 30), ny(16)),
                                                                              ("postMarket", ny(16), ny(20))]
        await hub.close()
        late = hub_of(clock=lambda: ny(21, 0))
        _, snap = await open_chart(late, "5m")
        assert snap["market_open"] is False                          # between pieces or after the last one: closed, as Schwab's day says
        await late.close()
    run(go())


def test_a_new_piece_with_a_name_nobody_expected_is_kept_and_counts_as_open(source):
    async def go():
        hub = hub_of(clock=lambda: ny(22, 0))
        night = ProviderHours("equity", "EQ", "2026-11-20", True, day_hours().segments + (Segment("overnightMarket", ny(21), ny(23, 59)),))
        source["night"] = night
        _, snap = await open_chart(hub, "5m")
        assert snap["market_open"] is True and snap["sessions"][-1]["name"] == "overnightMarket"
        await hub.close()
    run(go())


def test_without_hours_from_the_provider_open_is_unknown_and_nothing_is_shaded(source):
    source["night"] = None

    async def go():
        hub = hub_of()
        _, snap = await open_chart(hub, "5m")
        assert snap["market_open"] is None and snap["sessions"] is None
        await hub.close()
    run(go())


def test_the_hours_are_asked_once_a_day_and_again_after_a_reconnection(source):
    async def go():
        hub = hub_of()
        client, _ = await open_chart(hub, "5m")
        feed_source = source["source"]
        await open_second(hub, client)
        assert feed_source.hours_calls == 1                            # a second chart of the same feed does not ask again
        feed_source.emit(SourceStatus("reconnecting", "network"))
        feed_source.emit(SourceStatus("live", "connected"))
        for _ in range(100):
            if feed_source.hours_calls == 2:
                break
            await asyncio.sleep(0.02)
        assert feed_source.hours_calls == 2
        await hub.close()
    run(go())


def test_a_subscription_released_while_the_hours_are_asked_gets_no_snapshot_and_breaks_nothing(source, monkeypatch):
    """The hours are asked after the subscription joins its feed. A release in that window (an unsubscribe, or a failover that attaches the
    sub again and sends its own snapshot) used to hit an assert and the panel got an error with source "none". The gates make the window
    last as long as the test needs: no sleeps."""
    async def go():
        asking, gate = asyncio.Event(), asyncio.Event()

        async def slow_hours(self, asset_class):
            asking.set()
            await gate.wait()
            return self.hours

        monkeypatch.setattr(FakeStreamSource, "market_hours", slow_hours)
        hub = hub_of()
        client = PanelClient()
        hub.attach_client(client)
        task = asyncio.create_task(hub.subscribe(client, "s1", "QQQ", "5m"))
        await asyncio.wait_for(asking.wait(), 5)
        await hub.unsubscribe(client, "s1")
        gate.set()
        await asyncio.wait_for(task, 5)                                 # it must end without raising
        sent = []
        while not client.outbox.empty():
            sent.append(json.loads(client.outbox.get_nowait()))
        assert not [m for m in sent if m["type"] in ("snapshot", "error")]
        await hub.close()
    run(go())


def test_at_the_end_of_the_day_the_candles_of_the_source_win_and_the_corrections_are_only_counted(source):
    async def go():
        history = History(daily_history(), five_history())
        hub = hub_of(history)
        client, snap = await open_chart(hub, "5m")
        feed = hub._feeds["QQQ"]
        assert feed.hours_timer is not None                              # armed for the end of the last piece plus the margin
        history.five = [Candle(ny(9, 25), 503.9, 504.0, 503.8, 503.95, 100.0), Candle(ny(9, 30), 504.0, 504.6, 503.9, 504.5, 950.0)]   # the source's final candle
        feed.hours_timer.cancel()
        hub._reconcile_due(feed)
        await message(client, lambda m: m["type"] == "snapshot" and m["bars"][-1]["c"] == 504.5)
        for _ in range(100):
            if hub.reconcile_diffs:
                break
            await asyncio.sleep(0.02)
        assert hub.reconcile_diffs == 1
        await hub.close()
    run(go())


async def open_second(hub, client) -> None:
    await hub.subscribe(client, "s2", "QQQ", "15m")
    await message(client, lambda m: m["type"] == "snapshot" and m["sub"] == "s2")

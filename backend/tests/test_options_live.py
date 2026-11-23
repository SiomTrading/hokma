"""SPEC-016 T5: the option chain that moves by itself over `/ws/market`. The source is a fake that streams option contracts; the structure
of the chain (what `options.structure` gets from `/chains`) is a fake too. No network."""
import asyncio
import json
from datetime import date

import pytest

from hokma.market import options
from hokma.market.adapters import ADAPTERS, register_adapter
from hokma.market.hub import MarketHub, PanelClient
from hokma.market.providers import get_catalog
from hokma.market.stream import LimitReached, LiveSource, OptionQuote, Quote, SourceCapabilities, SourceStatus
from test_hub_streaming import Config, History, daily_history, five_history, message, run

DAY = "2026-12-18"
STRIKES = [float(s) for s in range(490, 511)]                 # 21 strikes


def key(strike: float, side: str) -> str:
    return f"QQQ   261218{side}{int(strike * 1000):08d}"


def cell(strike: float, side: str) -> dict:
    return {"symbol": key(strike, side), "itm": False, "last": 1.0, "last_exchange": None, "bid": 0.9, "bid_exchange": None, "ask": 1.1,
            "ask_exchange": None, "volume": 10, "open_interest": 100, "theta": -0.1, "delta": 0.5, "iv": 0.2, "intrinsic": None,      # None: Schwab sent no intrinsicValue, Hokma computes it (tarea 3330)
            "prob_itm": 0.5, "quote_time": None}


def structure(symbol, dates, spot=None, provider=None):
    rows = [{"strike": s, "call": cell(s, "C"), "put": cell(s, "P")} for s in STRIKES]
    return {"underlying": symbol, "groups": [{"date": DAY, "days": 27, "multiplier": 100, "iv": None, "expected_move": None, "rows": rows}],
            "delayed": False, "today": "2026-11-21"}


class OptionSource(LiveSource):
    provider_id = "schwab"
    name = "Schwab"

    def __init__(self, live_options: bool = True) -> None:
        super().__init__()
        self.capabilities = SourceCapabilities(True, 100, ("equity",), 0, ticks=False, clock_aligned=True, live_options=live_options)
        self.following: set[str] = set()
        self.asked: list[str] = []
        self.unfollowed: list[str] = []
        self.spot = 500.4

    async def connect(self) -> None:
        import asyncio as _a
        self._queue = self._queue or _a.Queue(1000)
        self.emit(SourceStatus("live", "connected"))

    async def subscribe(self, symbols):
        for symbol in symbols:
            self.emit(Quote(symbol, 1_700_000_000_000, last=self.spot, bid=self.spot - 0.01, ask=self.spot + 0.01))

    async def unsubscribe(self, symbols):
        pass

    async def subscribe_options(self, keys):
        self.asked += list(keys)
        self.following.update(keys)

    async def unsubscribe_options(self, keys):
        self.following.difference_update(keys)
        self.unfollowed += list(keys)

    async def close(self) -> None:
        self._closing = True


@pytest.fixture
def source(monkeypatch):
    holder: dict = {}
    previous = ADAPTERS["schwab"]

    def make(spec, secret, url):
        holder["source"] = OptionSource(live_options=holder.get("live", True))
        return holder["source"]

    register_adapter("schwab", make)
    monkeypatch.setattr(options, "structure", structure)
    yield holder
    register_adapter("schwab", previous)


def hub_of() -> MarketHub:
    return MarketHub(get_catalog(), Config(), lambda: History(daily_history(), five_history()))


async def open_options(hub: MarketHub, strikes=4, sub="o1") -> tuple[PanelClient, dict]:
    client = PanelClient()
    hub.attach_client(client)
    await hub.subscribe_options(client, sub, "QQQ", [DAY], strikes)
    return client, await message(client, lambda m: m["type"] in ("options_snapshot", "error"))


def oq(strike: float, side: str = "C", **fields) -> OptionQuote:
    return OptionQuote(key(strike, side), 1_700_000_000_000, **fields)


def test_the_visible_strikes_are_half_below_and_half_above_the_price_of_the_stream_and_the_rest_is_a_margin(source):
    async def go():
        hub = hub_of()
        client, snap = await open_options(hub, 4)
        group = snap["expirations"][0]
        assert [r["strike"] for r in group["rows"]] == [499.0, 500.0, 501.0, 502.0]       # price 500.4: two below, two above
        assert snap["underlying_price"] == 500.4 and snap["source"] == "Schwab"
        following = source["source"].following
        assert len(following) == 24 and key(499.0, "C") in following and key(502.0, "P") in following      # 12 strikes, both sides
        assert key(490.0, "C") not in following                                             # beyond the margin: not asked
        await hub.close()
    run(go())


def test_each_message_of_a_contract_is_a_change_of_its_cell_with_the_values_of_the_source(source):
    async def go():
        hub = hub_of()
        client, _ = await open_options(hub, 4)
        source["source"].emit(oq(500.0, "C", bid=1.25, ask=1.3, last=1.27, mark=1.275, volume=999, open_interest=5000, iv=21.5,
                                 delta=0.52, gamma=0.03, theta=-0.2, vega=0.1, rho=0.01))
        update = await message(client, lambda m: m["type"] == "options")
        (change,) = update["changes"]
        assert change["symbol"] == key(500.0, "C") and (change["bid"], change["ask"], change["mark"], change["volume"]) == (1.25, 1.3, 1.275, 999)
        assert change["open_interest"] == 5000 and change["delta"] == 0.52 and change["live"] is True and round(change["intrinsic"], 2) == 0.4
        source["source"].emit(oq(490.0, "C", bid=9.0))                                     # in the margin, not on screen: no message
        with pytest.raises(AssertionError):
            await message(client, lambda m: m["type"] == "options", timeout=0.3)
        await hub.close()
    run(go())


def test_the_price_of_the_underlying_moves_the_intrinsic_value_of_the_cells_on_screen(source):
    async def go():
        hub = hub_of()
        client, _ = await open_options(hub, 4)
        source["source"].emit(Quote("QQQ", 1_700_000_001_000, last=501.5))
        update = await message(client, lambda m: m["type"] == "options")
        by = {c["symbol"]: c for c in update["changes"]}
        assert update["underlying_price"] == 501.5 and by[key(500.0, "C")]["intrinsic"] == 1.5 and by[key(502.0, "P")]["intrinsic"] == 0.5
        await hub.close()
    run(go())


def test_the_window_moves_by_asking_only_for_the_contracts_that_enter_and_dropping_those_that_leave(source):
    async def go():
        hub = hub_of()
        client, _ = await open_options(hub, 4)
        before = set(source["source"].following)
        await hub.options_window(client, "o1", 6, 3)
        snap = await message(client, lambda m: m["type"] == "options_snapshot")
        assert [r["strike"] for r in snap["expirations"][0]["rows"]] == [500.0, 501.0, 502.0, 503.0, 504.0, 505.0]        # 3 strikes up, 6 shown
        now = set(source["source"].following)
        assert now != before and key(505.0, "C") in now
        assert set(source["source"].unfollowed) == before - now
        await hub.close()
    run(go())


def test_the_limit_of_symbols_marks_those_contracts_without_live_and_they_are_not_asked_again(source):
    async def go():
        hub = hub_of()
        client, _ = await open_options(hub, 4)
        source["source"].emit(LimitReached("options", (key(502.0, "C"), key(502.0, "P"))))
        limit = await message(client, lambda m: m["type"] == "options_limit")
        assert limit["symbols"] == sorted([key(502.0, "C"), key(502.0, "P")]) and limit["open"] == 22
        update = await message(client, lambda m: m["type"] == "options")
        assert {c["symbol"]: c["live"] for c in update["changes"]} == {key(502.0, "C"): False, key(502.0, "P"): False}
        await hub.options_window(client, "o1", 4, 0)
        snap = await message(client, lambda m: m["type"] == "options_snapshot")
        assert snap["limited"] == sorted([key(502.0, "C"), key(502.0, "P")]) and source["source"].asked.count(key(502.0, "C")) == 1
        await hub.close()
    run(go())


def test_a_source_without_streaming_options_says_so_and_nothing_is_followed(source):
    source["live"] = False

    async def go():
        hub = hub_of()
        _, answer = await open_options(hub, 4)
        assert answer["type"] == "error" and answer["code"] == "market.options_not_live"
        await hub.close()
    run(go())


def test_two_chains_on_the_same_contract_release_it_only_when_both_are_closed(source):
    async def go():
        hub = hub_of()
        a, _ = await open_options(hub, 4, "o1")
        b, _ = await open_options(hub, 4, "o2")
        await hub.unsubscribe(a, "o1")
        assert source["source"].unfollowed == [] and len(source["source"].following) == 24
        await hub.unsubscribe(b, "o2")
        assert source["source"].following == set()
        await hub.close()
    run(go())


def test_closing_the_socket_releases_the_chain(source):
    async def go():
        hub = hub_of()
        client, _ = await open_options(hub, 4)
        await hub._release_client(client)
        assert source["source"].following == set() and hub.options.subs == {}
        await hub.close()
    run(go())


def test_a_reconnection_is_told_to_the_chain_so_the_panel_can_grey_the_values(source):
    async def go():
        hub = hub_of()
        client, _ = await open_options(hub, 4)
        source["source"].emit(SourceStatus("reconnecting", "network"))
        status = await message(client, lambda m: m["type"] == "options_status")
        assert status["state"] == "reconnecting"
        source["source"].emit(SourceStatus("live", "connected"))
        assert (await message(client, lambda m: m["type"] == "options_status"))["state"] == "live"
        await hub.close()
    run(go())

"""Tarea 3328 (1.0.11), the four bugs of Cristian's Schwab test on Sunday 22 Nov 2026 with the market closed. Names and numbers are those of the
evidence (QQQ: regular close 608.45, previous close 645.04, post-market bid 617.5 and last 617.74, securityStatus Closed). No network."""
import asyncio

from hokma.market.adapters.schwab import SchwabSource, quote_of
from hokma.market.hours import ProviderHours, Segment
from hokma.market.providers import get_catalog
from hokma.schwab import endpoints, models
from schwab_support import FakeSchwab
from test_hub_streaming import FakeStreamSource, hub_of, message, ny, open_chart, quote, run, source  # noqa: F401  (source is a fixture)


def sunday_closed() -> ProviderHours:
    """What Schwab says on a Sunday: the equity market, not open, and no sessionHours at all."""
    return ProviderHours("equity", "equity", "2026-11-22", False, ())


def closed_quote(**fields):
    base = dict(last=617.74, bid=617.5, ask=617.8, mark=608.45, regular_last=608.45, status="Closed", session="extended",
                regular_change=-36.59, regular_change_pct=-5.67, post_change=9.29, post_change_pct=1.53, prev_close=608.45)
    base.update(fields)
    return quote(**base)


# ---------------------------------------------------------------------------------------------- 1: the header is the regular session


def test_closed_market_header_is_the_regular_price_and_its_own_change_never_the_bid(source):
    source["night"] = sunday_closed()

    async def go():
        hub = hub_of()
        client, _ = await open_chart(hub, "5m")
        source["source"].emit(closed_quote())
        last = await message(client, lambda m: m["type"] == "last" and m["price"] == 608.45)
        assert (last["change"], last["change_pct"]) == (-36.59, -5.67)              # the change Schwab gives, not one against another close
        assert last["extended"] == {"price": 617.74, "change": 9.29, "change_pct": 1.53}      # the after-hours trade goes aside
        assert last["quote"]["bid"] == 617.5 and last["price"] != 617.5
        await hub.close()
    run(go())


def test_closed_market_header_falls_back_to_the_previous_close_of_the_daily_candles_when_schwab_sends_no_regular_change(source):
    source["night"] = sunday_closed()

    async def go():
        hub = hub_of()
        client, _ = await open_chart(hub, "5m")
        source["source"].emit(closed_quote(regular_change=None, regular_change_pct=None, prev_close=0.0, mark=504.0, regular_last=None, last=505.0))
        last = await message(client, lambda m: m["type"] == "last" and m["price"] == 504.0)            # mark when there is no regular last
        assert last["change_pct"] is not None and last["extended"]["price"] == 505.0
        await hub.close()
    run(go())


def test_open_market_header_stays_the_last_trade(source):
    async def go():
        hub = hub_of()
        client, _ = await open_chart(hub, "5m")
        source["source"].emit(quote(last=504.6, mark=504.55, regular_last=504.6, regular_change=1.0, regular_change_pct=0.2))
        last = await message(client, lambda m: m["type"] == "last" and m["price"] == 504.6)
        assert last["extended"] is None and last["change"] != 1.0
        await hub.close()
    run(go())


def test_the_level_one_of_schwab_keeps_the_regular_and_post_market_changes():
    q = quote_of("QQQ", {"3": 617.74, "29": 608.45, "31": -36.59, "43": -5.67, "50": 9.29, "51": 1.53, "32": "Closed", "33": 608.45}, False, 1)
    assert (q.regular_last, q.regular_change, q.regular_change_pct, q.post_change, q.post_change_pct, q.status) == (608.45, -36.59, -5.67, 9.29, 1.53, "Closed")


# ---------------------------------------------------------------------------------------------- 2: the badge says closed


def test_a_closed_market_is_told_closed_with_the_link_alive_and_a_quote_does_not_reopen_it(source):
    source["night"] = sunday_closed()

    async def go():
        hub = hub_of()
        client, snap = await open_chart(hub, "5m")
        assert snap["market_open"] is False and snap["data_state"] == "live"                  # connected, and the market closed
        status = await message(client, lambda m: m["type"] == "status")
        assert (status["state"], status["code"], status["market_open"]) == ("closed", "market.closed", False)
        source["source"].emit(closed_quote())
        await message(client, lambda m: m["type"] == "last")
        sub = next(iter(hub._feeds["QQQ"].subs.values()))
        assert sub.state == "closed"
        assert client.outbox.empty() or all(m["type"] != "status" for m in _drain(client))
        await hub.close()
    run(go())


def _drain(client):
    import json
    out = []
    while not client.outbox.empty():
        out.append(json.loads(client.outbox.get_nowait()))
    return out


def test_an_open_market_is_live_and_with_no_hours_the_state_is_not_closed(source):
    async def go():
        hub = hub_of()
        client, snap = await open_chart(hub, "5m")
        status = await message(client, lambda m: m["type"] == "status")
        assert (status["state"], status["market_open"]) == ("live", True) and snap["market_open"] is True
        await hub.close()
    run(go())
    source["night"] = None

    async def unknown():
        hub = hub_of()
        client, snap = await open_chart(hub, "5m")
        status = await message(client, lambda m: m["type"] == "status")
        assert status["state"] == "live" and status["market_open"] is None and snap["market_open"] is None      # unknown is not closed
        await hub.close()
    run(unknown())


def test_when_a_piece_of_the_day_ends_the_charts_are_told_closed(source):
    clock = [ny(19, 59)]

    async def go():
        hub = hub_of(clock=lambda: clock[0])
        client, _ = await open_chart(hub, "5m")
        await message(client, lambda m: m["type"] == "status" and m["state"] == "live")
        feed = hub._feeds["QQQ"]
        assert feed.hours.next_boundary(clock[0]) == ny(20)
        clock[0] = ny(20, 1)
        hub._boundary_due(feed)
        status = await message(client, lambda m: m["type"] == "status" and m["state"] == "closed")
        assert status["market_open"] is False
        await hub.close()
    run(go())


# ---------------------------------------------------------------------------------------------- 3: option and future hours


def option_body(day: str) -> dict:
    return {"option": {"EQO": {"date": day, "marketType": "OPTION", "product": "EQO", "productName": "equity option", "isOpen": True,
                               "sessionHours": {"regularMarket": [{"start": f"{day}T09:30:00-05:00", "end": f"{day}T16:00:00-05:00"}]}}}}


def test_a_single_market_that_schwab_refuses_on_its_own_route_is_asked_in_the_list_route():
    with FakeSchwab() as rest:
        rest.on("GET", "/marketdata/v1/markets/option", (400, {"errors": [{"status": 400, "title": "Bad Request"}]}))
        rest.on("GET", "/marketdata/v1/markets", option_body("2026-11-23"))
        result = endpoints.market_hours("option")
        assert rest.paths() == ["/marketdata/v1/markets/option", "/marketdata/v1/markets"]
        assert rest.calls[-1].query == {"markets": "option"}
        assert list(result) == ["option"] and result["option"]["EQO"].isOpen is True


def test_a_market_that_works_on_its_own_route_is_not_asked_twice():
    with FakeSchwab() as rest:
        rest.on("GET", "/marketdata/v1/markets/equity", {"equity": {"equity": {"date": "2026-11-22", "marketType": "EQUITY", "product": "equity", "isOpen": False}}})
        result = endpoints.market_hours("equity")
        assert rest.paths() == ["/marketdata/v1/markets/equity"] and result["equity"]["equity"].isOpen is False


def test_the_diagnostic_asks_the_three_markets_one_by_one_and_as_a_list():
    from hokma.schwab import diagnostic
    with FakeSchwab() as rest:
        rest.on("GET", "/marketdata/v1/markets/equity", {"equity": {"equity": {"date": "2026-11-22", "isOpen": False}}})
        rest.on("GET", "/marketdata/v1/markets/option", (400, {}))
        rest.on("GET", "/marketdata/v1/markets/future", (400, {}))
        rest.on("GET", "/marketdata/v1/markets", lambda call: {m: {} for m in call.query["markets"].split(",")})
        import datetime as dt
        out = diagnostic.collect_rest("QQQ", dt.date(2026, 11, 22))
    assert set(out["markets"]) == {"equity", "option", "future"} and "error" not in out["markets"]["equity"]
    assert set(out["markets_list"]) == {"equity", "option", "future"}


# ---------------------------------------------------------------------------------------------- 4: shading with the hours of the provider


def test_the_hub_asks_the_provider_for_the_days_on_the_chart_and_never_for_a_daily_chart(source, monkeypatch):
    asked = []

    async def hours(self, asset_class, days=()):
        asked.append((asset_class, tuple(days)))
        return self.hours

    monkeypatch.setattr(FakeStreamSource, "market_hours", hours)

    async def go():
        hub = hub_of()
        _, snap = await open_chart(hub, "5m")
        assert asked == [("equity", ("2026-11-20",))] and [s["name"] for s in snap["sessions"]] == ["preMarket", "regularMarket", "postMarket"]
        await hub.close()
        asked.clear()
        hub = hub_of()
        await open_chart(hub, "1D")
        assert asked == [("equity", ())]
        await hub.close()
    run(go())


def test_schwab_adapter_adds_the_pieces_of_the_days_it_gives_and_skips_the_ones_it_refuses():
    friday = {"equity": {"equity": {"date": "2026-11-20", "isOpen": True, "sessionHours": {
        "preMarket": [{"start": "2026-11-20T07:00:00-05:00", "end": "2026-11-20T09:30:00-05:00"}],
        "regularMarket": [{"start": "2026-11-20T09:30:00-05:00", "end": "2026-11-20T16:00:00-05:00"}],
        "postMarket": [{"start": "2026-11-20T16:00:00-05:00", "end": "2026-11-20T20:00:00-05:00"}]}}}}
    sunday = {"equity": {"equity": {"date": "2026-11-22", "marketType": "EQUITY", "product": "equity", "isOpen": False}}}

    def answer(call):
        day = call.query.get("date")
        if day == "2026-11-20":
            return friday
        if day == "2026-11-19":
            return 400, {}
        return sunday

    with FakeSchwab() as rest:
        rest.on("GET", "/marketdata/v1/markets/equity", answer)
        rest.on("GET", "/marketdata/v1/markets", (400, {}))
        source = SchwabSource(get_catalog().get("schwab"))
        hours = asyncio.run(source.market_hours("equity", days=("2026-11-19", "2026-11-20", "2026-11-22")))
    assert hours.date == "2026-11-22" and hours.is_open is False                       # today's flag stays today's
    assert [s.name for s in hours.segments] == ["preMarket", "regularMarket", "postMarket"]       # Friday's pieces, as Schwab named them
    assert hours.open_at(ny(12, day=20)) is True and hours.open_at(ny(12, day=22)) is False


def test_past_dates_are_refused_for_the_public_tools_and_allowed_for_the_chart():
    with FakeSchwab() as rest:
        rest.on("GET", "/marketdata/v1/markets/equity", {"equity": {"equity": {"date": "2026-01-02", "isOpen": True}}})
        try:
            endpoints.market_hours("equity", date="2026-01-02")
            refused = False
        except Exception:                                                                # noqa: BLE001
            refused = True
        assert refused and rest.calls == []
        assert endpoints.market_hours("equity", date="2026-01-02", past_ok=True)["equity"]["equity"].isOpen is True


def test_next_boundary_and_merge_of_provider_hours():
    a = ProviderHours("equity", "EQ", "d", True, (Segment("regularMarket", 100, 200),))
    b = ProviderHours("equity", "EQ", "e", False, (Segment("regularMarket", 10, 20), Segment("regularMarket", 100, 200)))
    merged = a.merged([b])
    assert [(s.start, s.end) for s in merged.segments] == [(10, 20), (100, 200)] and merged.date == "d" and merged.is_open is True
    assert merged.next_boundary(50) == 100 and merged.next_boundary(200) is None

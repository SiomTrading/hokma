"""SPEC-016 T21 (CA21, CA25): the hours are the provider's, tramo by tramo; stocks and options each with the hours of their own market; no hour of ours cuts data."""
import asyncio
import re
from pathlib import Path

from hokma.market.adapters import SchwabSource
from hokma.market.providers import get_catalog
from schwab_support import FakeSchwab

MARKET = Path(__file__).resolve().parents[1] / "hokma" / "market"


def body(market: str, product: str, regular: tuple[str, str], extra: dict | None = None) -> dict:
    session = {"regularMarket": [{"start": f"2026-11-20T{regular[0]}:00-05:00", "end": f"2026-11-20T{regular[1]}:00-05:00"}], **(extra or {})}
    return {market: {product: {"date": "2026-11-20", "isOpen": True, "sessionHours": session}}}


def test_stocks_and_options_each_take_the_hours_of_their_own_market_and_a_new_piece_keeps_its_name():
    rest = FakeSchwab()
    rest.on("GET", "/marketdata/v1/markets/equity", body("equity", "EQ", ("09:30", "16:00")))
    rest.on("GET", "/marketdata/v1/markets/option", body("option", "EQO", ("09:30", "16:15"), {"overnightMarket": [
        {"start": "2026-11-20T21:00:00-05:00", "end": "2026-11-20T23:59:00-05:00"}]}))
    rest.__enter__()
    try:
        source = SchwabSource(get_catalog().get("schwab"))
        stocks = asyncio.run(source.market_hours("equity"))
        options = asyncio.run(source.market_hours("option"))
    finally:
        rest.__exit__()
    assert [s.name for s in stocks.segments] == ["regularMarket"] and stocks.last_end() - stocks.segments[0].start == 6.5 * 3600
    assert [s.name for s in options.segments] == ["regularMarket", "overnightMarket"]      # the name Schwab gave, whatever it is
    assert options.segments[0].end - options.segments[0].start == 6.75 * 3600              # the options day is 15 minutes longer: its own hours
    assert [c.path for c in rest.calls] == ["/marketdata/v1/markets/equity", "/marketdata/v1/markets/option"]


def test_a_market_the_source_does_not_know_has_no_hours():
    source = SchwabSource(get_catalog().get("schwab"))
    assert asyncio.run(source.market_hours("crypto")) is None


def test_no_hour_of_ours_cuts_or_filters_the_data_of_a_source():
    """CA21: the code that cuts, filters or places data has no market hour written in it. `sessions.py` keeps the grid anchors of the sources that send
    no hours (Yahoo, the futures stream): they place bars on a grid, they never drop one, and a source that sends every session (Schwab, Alpaca) is
    folded on the clock instead (`clock_session`)."""
    for path in ("history.py", "hub.py", "aggregator.py", "service.py", "option_subs.py", "hours.py", "alpaca_hours.py"):
        text = (MARKET / path).read_text(encoding="utf-8")
        assert "_regular_hours" not in text, path
        assert not re.search(r"\b570\b|\b960\b|time\(9, ?30\)|hour *== *9", text), path

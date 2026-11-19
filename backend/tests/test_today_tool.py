"""today_show / today_configure in the chat (task 3294, C7-4b): declared as write (permission) and read, same service
as the route, strict schema, the change visible in the permission request."""
import json

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from test_company_tools import net  # noqa: F401
from test_today_data import serve

from hokma.engine.stream import summarize_tool
from hokma.i18n import t
from hokma.kit import hokma_tools, market_tools
from hokma.kit.hokma_tools import Caller
from hokma.today import service


def call(name, args, agent="hokma"):
    text, is_error = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": agent}))
    return (json.loads(text) if not is_error else text), is_error


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    monkeypatch.setattr(market_tools, "desk_prices", lambda symbols, desk_id=None: [
        {"symbol": s, "last": 2.0, "change_pct": 0.5, "time": None, "source": "Finnhub"} for s in symbols])


def test_declared_with_strict_schemas_write_needs_permission_read_does_not():
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    assert hokma_tools.TOOLS["today_configure"][4] is True and hokma_tools.TOOLS["today_show"][4] is False
    for name in ("today_show", "today_configure"):
        assert listed[name]["inputSchema"]["additionalProperties"] is False and name in hokma_tools.NO_UNIT
    assert listed["today_configure"]["inputSchema"]["required"] == []


def test_the_request_shows_the_owner_what_will_change():
    shown = summarize_tool("mcp__hokma__today_configure", {"extra_symbols": ["NVDA"], "sections": {"calendar": False}})
    assert shown == {"kind": "other", "target": 'extra_symbols=["NVDA"] | sections={"calendar": false}'}


def test_chat_request_news_of_nvda_and_no_calendar_shows_in_the_card(db, net):  # noqa: F811
    serve(net)
    out, err = call("today_configure", {"extra_symbols": ["NVDA"], "sections": {"calendar": False},
                                        "news": {"topics": ["NVDA"]}})
    assert not err and out["config"]["sections"] == {"watchlist": True, "calendar": False, "news": True, "briefing": True}
    shown, err = call("today_show", {})
    assert not err and shown["calendar"] is None and [a["symbol"] for a in shown["watchlist"]["assets"]] == ["NVDA"]
    assert shown["config"]["news"]["topics"] == ["NVDA"] and shown["news"]["notice"]


def test_a_change_keeps_what_it_does_not_mention(db):
    call("today_configure", {"calendar": {"countries": ["CL"]}})
    out, _ = call("today_configure", {"calendar": {"min_importance": "high"}})
    assert out["config"]["calendar"] == {"countries": ["CL"], "min_importance": "high", "events": []}


def test_an_impossible_request_is_a_clear_error_and_changes_nothing(db):
    text, err = call("today_configure", {"extra_symbols": [f"S{i}" for i in range(500)]})
    assert err and t("today.err.symbols", max=30) in text
    text, err = call("today_configure", {})
    assert err and t("today.err.nothing") in text
    assert service.get_config() == service.DEFAULTS


def test_a_desk_chief_cannot_change_the_card(db):
    text, err = call("today_configure", {"sections": {"news": False}}, agent="desk-chief")
    assert err and service.get_config() == service.DEFAULTS


def test_chat_sets_the_events_to_follow(db):
    out, err = call("today_configure", {"calendar": {"events": ["FOMC", "Non-Farm"]}})
    assert not err and out["config"]["calendar"]["events"] == ["FOMC", "Non-Farm"]
    assert out["config"]["calendar"]["countries"] == ["US"]

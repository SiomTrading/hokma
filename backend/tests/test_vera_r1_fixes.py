"""Corrections of Vera's round 1 on task 3294 (classes 4a to 3b): each finding has the test that would have caught it."""
import json
import re
from datetime import datetime
from pathlib import Path

import pytest
from agents_support import client, db, work  # noqa: F401  (fixtures)
from dbsupport import env  # noqa: F401  (fixture)
from test_company_tools import net  # noqa: F401  (fixture)
from test_today_data import forex

from hokma.broker import service as broker
from hokma.broker.costs import MAX_BPS
from hokma.db import ValidationError
from hokma.desks import service as desks
from hokma.i18n import t
from hokma.kit import company_tools, hokma_tools, today_data
from hokma.kit.hokma_tools import Caller
from hokma.today import service as today

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "kit" / "template"
SETTINGS = ROOT / "backend" / "hokma" / "kit" / "templates" / "settings.base.json"


def call(name, args, agent="hokma"):
    text, is_error = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": agent}))
    return text, is_error


# ------------------------------------------------------------------------------------------ A1 and B6

def agent_tools(slug="hokma") -> set[str]:
    front = (TEMPLATE / "agents" / f"{slug}.md").read_text(encoding="utf-8").split("---")[1]
    line = next(x for x in front.splitlines() if x.startswith("tools:"))
    return {x.strip().removeprefix("mcp__hokma__") for x in line.removeprefix("tools:").split(",")}


def cited(path: Path) -> set[str]:
    return {x for x in re.findall(r"`(\w+)`", path.read_text(encoding="utf-8")) if x in hokma_tools.TOOLS}


@pytest.mark.parametrize("routine", sorted((TEMPLATE / "routines").glob("*.md")), ids=lambda p: p.name)
def test_every_tool_a_routine_prompt_asks_for_is_in_the_tools_of_its_agent(routine):
    """A1: the briefing asked for `economic_calendar` while the agent file did not list it, so it could never call it."""
    assert cited(routine) <= agent_tools("hokma"), cited(routine) - agent_tools("hokma")


def test_hokma_can_call_the_calendar_the_news_and_the_watchlist_and_names_them():
    assert {"economic_calendar", "market_news", "watchlist_all", "desk_unassign_account"} <= agent_tools("hokma")
    body = (TEMPLATE / "agents" / "hokma.md").read_text(encoding="utf-8")
    assert "`economic_calendar`, `market_news` and `watchlist_all`" in body and "`desk_unassign_account`" in body


def test_an_unattended_routine_can_call_the_read_only_tools_it_asks_for():
    """B6: a scheduled run answers every permission request with a refusal (nobody is there), so what it needs and only
    reads must be pre-allowed; nothing that writes, sends or spends is."""
    allow = {x.removeprefix("mcp__hokma__") for x in json.loads(SETTINGS.read_text(encoding="utf-8"))["permissions"]["allow"]}
    read_only = {n for n, spec in hokma_tools.TOOLS.items() if not spec[4]}
    asked = cited(TEMPLATE / "routines" / "premarket_briefing.md")
    assert asked and asked <= read_only and asked <= allow
    assert {"today_show", "watchlist_all", "economic_calendar", "market_news", "earnings_calendar", "market_quote",
            "broker_list"} <= allow
    assert allow - {"trash_move"} <= read_only                         # nothing that writes is allowed without asking
    assert not allow & {"gmail_send", "gmail_read", "gmail_search", "today_configure", "account_create"}


# ------------------------------------------------------------------------------------------ B1, B2, C1

def make_job(db, name="Briefing"):
    with db.write() as u:
        return u.scheduled_jobs.create(name, "hokma", "Morning briefing", "daily", "07:00", "UTC").id


def delete_job(db, job_id):
    with db.write() as u:
        u.session.execute(f"DELETE FROM scheduled_jobs WHERE id = {int(job_id)}")


def test_a_deleted_briefing_routine_does_not_take_the_whole_card_down(db, net):  # noqa: F811
    job_id = make_job(db)
    today.change_config({"briefing_job_id": job_id}, db)
    delete_job(db, job_id)
    card = today.today(db)
    assert card["briefing"] == {"error": t("today.err.job")} and card["config"]["briefing_job_id"] == job_id
    assert set(card) >= {"watchlist", "calendar", "news", "briefing"}   # the card answered, section by section


def test_the_owner_can_unlink_or_change_other_settings_after_the_routine_is_gone(db):
    job_id = make_job(db)
    today.change_config({"briefing_job_id": job_id}, db)
    delete_job(db, job_id)
    assert today.change_config({"news": {"limit": 5}}, db)["news"]["limit"] == 5           # same id kept: not checked again
    assert today.change_config({"briefing_job_id": None}, db)["briefing_job_id"] is None
    with pytest.raises(Exception):
        today.change_config({"briefing_job_id": job_id}, db)          # a new, different link is still checked


def test_today_configure_accepts_null_to_unlink_the_routine(db):
    job_id = make_job(db)
    today.change_config({"briefing_job_id": job_id}, db)
    schema = next(d for d in hokma_tools.definitions() if d["name"] == "today_configure")["inputSchema"]
    assert schema["properties"]["briefing_job_id"]["type"] == ["integer", "null"]
    text, error = call("today_configure", {"briefing_job_id": None})
    assert not error and json.loads(text)["config"]["briefing_job_id"] is None


def test_the_template_button_links_the_routine_to_the_today_card(client, db):
    job = client.post("/api/scheduled-jobs/template/premarket-briefing").json()
    assert today.get_config(db)["briefing_job_id"] == job["id"]


def test_the_button_relinks_when_the_saved_routine_is_gone_and_keeps_a_live_one(client, db):
    other = make_job(db, "Mine")
    today.change_config({"briefing_job_id": other}, db)
    client.post("/api/scheduled-jobs/template/premarket-briefing")
    assert today.get_config(db)["briefing_job_id"] == other           # a live choice of the owner is respected
    delete_job(db, other)
    again = client.post("/api/scheduled-jobs/template/premarket-briefing").json()
    assert today.get_config(db)["briefing_job_id"] == again["id"]


def test_a_routine_with_that_name_and_another_prompt_is_opened_not_refused(client, db):
    from hokma.scheduler.service import BRIEFING_NAME

    mine = client.post("/api/scheduled-jobs", json={"name": BRIEFING_NAME, "agent": "hokma", "prompt": "Else.",
                                                    "kind": "daily", "expr": "08:00", "timezone": "UTC"}).json()
    res = client.post("/api/scheduled-jobs/template/premarket-briefing")
    assert res.status_code == 200 and res.json()["id"] == mine["id"] and res.json()["prompt"] == "Else."
    assert len(client.get("/api/scheduled-jobs").json()["jobs"]) == 1


# ------------------------------------------------------------------------------------------ B5

def test_refresh_forgets_the_answers_but_not_the_limiters_nor_the_refusals(net):  # noqa: F811
    net.status["finnhub.io"] = 403
    with pytest.raises(LookupError):
        company_tools.earnings_surprises("AAPL")
    company_tools._limiters["finnhub"] = object()
    company_tools._cache[("x",)] = (0.0, {})
    company_tools.forget_cache()
    assert not company_tools._cache and company_tools._denied and "finnhub" in company_tools._limiters
    company_tools.reset()
    assert not company_tools._denied and not company_tools._limiters


def test_the_refresh_route_does_not_call_the_full_reset(db, net, monkeypatch):  # noqa: F811
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hokma.rpc.ws import SESSION_TOKEN
    from hokma.today.api import router

    wiped = []
    monkeypatch.setattr(company_tools, "reset", lambda: wiped.append(1))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        assert c.post("/api/today/refresh", headers={"X-Hokma-Token": SESSION_TOKEN}).status_code == 200
    assert wiped == []                                                # the limiters and refusals survive a refresh


# ------------------------------------------------------------------------------------------ B4, C5, C6, C7, C8

def test_the_chat_can_take_an_account_off_a_desk_and_asks_first(db):
    assert hokma_tools.TOOLS["desk_unassign_account"][4] is True and "desk_unassign_account" in hokma_tools.NO_UNIT
    from hokma.engine.stream import summarize_tool

    assert summarize_tool("mcp__hokma__desk_unassign_account", {"desk_id": 3})["target"] == "desk_id=3"
    desk = desks.create_desk("Swing", 10000, template="minimal")
    made = json.loads(call("account_create", {"name": "A", "capital": 5000})[0])
    call("desk_assign_account", {"desk_id": desk["id"], "account_id": made["id"]})
    text, error = call("desk_unassign_account", {"desk_id": desk["id"]})
    assert not error and json.loads(text) == {"desk_id": desk["id"], "account": None}
    assert desks.get_desk(desk["id"])["account"] is None
    assert call("desk_assign_account", {"desk_id": desk["id"], "account_id": made["id"]})[1] is False   # free again
    assert call("desk_unassign_account", {"desk_id": desk["id"]}, agent="desk-chief")[1] is True


def test_creating_an_account_without_any_broker_says_so_instead_of_an_index_error(db, monkeypatch):
    monkeypatch.setattr(broker, "list_brokers", lambda *a, **k: [])
    text, error = call("account_create", {"name": "A", "capital": 5000})
    assert error and "index" not in text and t("broker.err.broker_not_found") in text


def test_two_assignments_at_once_lose_with_account_in_use_not_a_generic_error(db, monkeypatch):
    from hokma.db import IntegrityViolation

    desk = desks.create_desk("Swing", 10000, template="minimal")
    broker.create_broker("B")
    made = broker.create_account(broker.list_brokers()[0]["id"], "A", 5000)

    def clash(self, desk_id, account_id):
        raise IntegrityViolation("unique")

    monkeypatch.setattr("hokma.db.repos.broker.BrokerRepo.link_desk", clash, raising=False)
    with pytest.raises(broker.BrokerError) as info:
        broker.assign_account(desk["id"], made["id"])
    assert info.value.code == "account_in_use" and info.value.status == 409


def test_a_repeated_account_name_is_name_taken_by_type_not_by_the_words_of_the_message(db):
    from hokma.db.repos.broker import NameTaken

    assert issubclass(NameTaken, ValidationError)
    broker.create_broker("B")
    b = broker.list_brokers()[0]["id"]
    broker.create_account(b, "A", 5000)
    with pytest.raises(broker.BrokerError) as info:
        broker.create_account(b, "A", 5000)
    assert info.value.code == "name_taken" and info.value.status == 409


@pytest.mark.parametrize("bad", [MAX_BPS + 1, 1e9])
def test_costs_have_a_ceiling(db, bad):
    with pytest.raises(broker.BrokerError) as info:
        broker.create_broker("X", None, bad)
    assert info.value.code == "invalid"
    with pytest.raises(broker.BrokerError):
        broker.create_broker("Y", {"type": "bps", "value": bad, "min": 0})
    assert broker.create_broker("Z", {"type": "bps", "value": MAX_BPS, "min": 0}, MAX_BPS)["slippage_bps"] == MAX_BPS


# ------------------------------------------------------------------------------------------ C9

def test_today_means_the_day_of_the_owner_not_the_utc_day(net, monkeypatch):  # noqa: F811
    """An event at 20:00 in New York is 01:00 UTC of the next day: it still belongs to the owner's day."""
    from zoneinfo import ZoneInfo

    new_york = ZoneInfo("America/New_York")
    assert today_data._local_day("2026-11-19T01:00:00Z", new_york) == "2026-11-18"
    assert today_data._local_day("2026-11-19T01:00:00Z", today_data.timezone.utc) == "2026-11-19"
    forex(net, today="2026-11-18")
    week = today_data._forex_week()
    late = next(e for e in week["events"] if e["time"][11:13] < "05")           # early UTC hours: the evening before in New York
    utc_day = late["time"][:10]
    monkeypatch.setattr(today_data, "_owner_zone", lambda: new_york)
    local_day = today_data._local_day(late["time"], new_york)
    assert local_day != utc_day
    names = lambda day: {e["event"] for e in today_data.economic_calendar(desde=day, hasta=day)["events"]}   # noqa: E731
    assert late["event"] in names(local_day) and late["event"] not in names(utc_day)

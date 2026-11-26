"""Analysis of an asset, backend (SPEC-028, delivery 4, piece 3337-A): start, read and list from the panel's door and from the chat tools, the
progress pushed by the chat socket (never asked for), the report of each seat saved in the record of the run (a seat that did not deliver is
absent or failed with its cause, never an empty finding) and the desks that have no analysis. A fake LLM engine and recorded data: nothing
here reaches the network or a broker."""
import asyncio
import dataclasses
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from b12_support import AS_OF
from desks.test_swo_end_to_end import call, rows, swo  # noqa: F401  (fixture and helpers)
from exec_support import db, desks_db, env, ready  # noqa: F401  (fixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.backoffice import runs
from hokma.desks import service
from hokma.desks.api import router as desks_router
from hokma.desks.errors import DeskError
from hokma.desks.plantel import SEATS
from hokma.desks.swo import analysis, graph, report
from hokma.engine.engine import ChatEngine, Connection
from hokma.i18n import t as msg, use_language
from hokma.rpc.ws import SESSION_TOKEN


class Feed:
    """What the subscribers of the analysis feed receive. `gate` holds the cycle at the entry of a step so a test can look at a run that goes."""

    def __init__(self):
        self.events, self.done, self.gate, self.hold = [], threading.Event(), None, None
        self.stop = analysis.subscribe(self)

    def __call__(self, event):
        self.events.append(event)
        if self.gate is not None and event["event"] == "step" and event["phase"] == "enter" and event["node"] == self.hold:
            self.gate.wait(60)
        if event["event"] in ("finished", "failed"):
            self.done.set()

    def entered(self):
        return [e["node"] for e in self.events if e["event"] == "step" and e["phase"] == "enter"]


@pytest.fixture
def feed():
    f = Feed()
    yield f
    if f.gate is not None:
        f.gate.set()
    f.stop()
    deadline = time.time() + 60
    while analysis._live and time.time() < deadline:               # the thread of a run frees its asset after its last event
        time.sleep(0.05)


def settle(feed):
    assert feed.done.wait(180)
    deadline = time.time() + 30
    while analysis._live and time.time() < deadline:
        time.sleep(0.05)


def run_done(feed, desk_id, symbol="AAPL"):
    """Start the analysis from the door of the panel and wait for its last event: the answer of the run, as the report of the cycle gives it."""
    feed.done.clear()
    out = analysis.start(desk_id, symbol, as_of=AS_OF)
    settle(feed)
    last = [e for e in feed.events if e["event"] in ("finished", "failed") and e["run_id"] == out["run_id"]][-1]
    return {**out, "status": last.get("status", "failed"), **{k: last.get(k) for k in ("rating", "proposal_id", "exec_status", "absent", "stopped_at", "reason", "detail")}}


def hold_at(feed, node):
    feed.gate, feed.hold = threading.Event(), node


def wait_for(predicate, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("the wait ended")


# ---- the progress ----------------------------------------------------------------------------------------------------------------

def test_start_answers_with_the_run_id_and_every_step_is_pushed_in_order(swo, feed):
    out = analysis.start(swo.swo_id, "aapl", as_of=AS_OF)
    assert out["status"] == "running" and out["symbol"] == "AAPL" and isinstance(out["run_id"], int)
    settle(feed)
    first, last = feed.events[0], feed.events[-1]
    assert first["event"] == "started" and first["run_id"] == out["run_id"] and first["steps"] == list(report.STEPS)
    assert last["event"] == "finished" and last["status"] == "done" and last["rating"] == "APPROVE" and last["proposal_id"]
    assert feed.entered()[:8] == list(report.STEPS)
    exits = [e for e in feed.events if e["event"] == "step" and e["phase"] == "exit"]
    assert [e["node"] for e in exits][:8] == list(report.STEPS) and all(e["ok"] for e in exits[:8])
    assert {e["desk_id"] for e in feed.events} == {swo.swo_id} and {e["run_id"] for e in feed.events} == {out["run_id"]}
    assert [e["step"] for e in feed.events if e["event"] == "step" and e["phase"] == "enter"][:8] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_a_run_that_goes_is_readable_with_its_steps_and_its_asset(swo, feed):
    hold_at(feed, "analysts")
    out = analysis.start(swo.swo_id, "AAPL", as_of=AS_OF)
    wait_for(lambda: "analysts" in feed.entered())
    live = analysis.get(swo.swo_id, out["run_id"])
    assert live["status"] == "running" and live["symbol"] == "AAPL" and live["agents"] == [] and live["rating"] is None
    assert {s["node"]: s["state"] for s in live["steps"]} == {"plan": "done", "analysts": "running", **{n: "pending" for n in report.STEPS[2:]}}
    listed = analysis.list_runs(swo.swo_id)
    assert [a["status"] for a in listed["analyses"]] == ["running"] and listed["analyses"][0]["symbol"] == "AAPL" and listed["available"] is True
    feed.gate.set()
    settle(feed)


def test_a_second_analysis_of_the_same_asset_is_refused_with_the_run_that_goes_even_from_another_process(swo, feed):
    hold_at(feed, "analysts")
    out = analysis.start(swo.swo_id, "AAPL", as_of=AS_OF)
    wait_for(lambda: "analysts" in feed.entered())
    with pytest.raises(DeskError) as same:
        analysis.start(swo.swo_id, "AAPL", as_of=AS_OF)
    assert same.value.code == "analysis_running" and same.value.status == 409 and same.value.extra["run_id"] == out["run_id"]
    with analysis._lock:                                              # another process does not share this memory: the checkpoints say the same
        saved = dict(analysis._live)
        analysis._live.clear()
    with pytest.raises(DeskError) as other:
        analysis.start(swo.swo_id, "AAPL", as_of=AS_OF)
    assert other.value.code == "analysis_running" and other.value.extra["run_id"] == out["run_id"]
    with analysis._lock:
        analysis._live.update(saved)
    feed.gate.set()
    settle(feed)
    again = analysis.start(swo.swo_id, "AAPL", as_of=AS_OF)           # the asset is free once the run ended
    assert again["run_id"] != out["run_id"]
    feed.done.clear()
    settle(feed)


# ---- the report ----------------------------------------------------------------------------------------------------------------------

def test_the_report_has_the_grade_the_proposal_the_decision_and_one_entry_per_seat(swo, feed):
    out = run_done(feed, swo.swo_id)
    assert out["status"] == "done" and out["rating"] == "APPROVE" and out["proposal_id"] and out["run_id"]
    full = analysis.get(swo.swo_id, out["run_id"])
    assert full["status"] == "done" and full["symbol"] == "AAPL" and full["rating"] == "APPROVE" and full["grade"] and full["summary"] and full["why"]
    assert [s["state"] for s in full["steps"]] == ["done"] * 8 and full["stopped_at"] is None
    assert full["proposal"]["underlying"] == "AAPL" and full["proposal"]["structure"] and full["proposal"]["net_premium"]
    decision = full["decision"]
    assert decision["proposal_id"] == out["proposal_id"] and decision["status"] == "analysis_only" and decision["strategy_key"] == "principal"
    assert decision["account_id"] and decision["pending"] is False and full["exec_status"] == "analysis_only"
    by_step = {}
    for seat in full["agents"]:
        assert set(seat) >= {"seat", "name", "role", "step", "status", "summary", "report", "why", "tools"} and seat["status"] in report.STATUSES
        by_step.setdefault(seat["step"], []).append(seat["seat"])
    assert {"A01", "A02", "A04", "A06", "A07"} <= set(by_step["analysts"]) and len(by_step["company_filter"]) == 7
    assert by_step["debate"] == ["A08", "A09", "A10"] and by_step["judge"] == ["A10"] and by_step["trader"] == ["A11"]
    assert {"A12", "A13", "A14"} <= set(by_step["committee"]) and by_step["manager"] == ["A15"]
    simon = next(s for s in full["agents"] if s["seat"] == "A09")
    assert simon["name"] == SEATS["A09"].name and simon["status"] == "ok" and simon["report"]
    sam = next(s for s in full["agents"] if s["seat"] == "A02")          # the fake engine answers for Sam only: the others did not deliver
    assert sam["status"] == "ok" and sam["report"] and sam["role"] == "research"
    silent = [s for s in full["agents"] if s["step"] == "analysts" and s["status"] != "ok"]
    assert silent and all(s["report"] == "" and s["why"] for s in silent)


def test_an_analyst_that_fails_is_marked_with_its_cause_and_the_reasons_mention_it(swo, feed):
    class Raises:
        def chat(self, *a, **k):
            raise RuntimeError("model endpoint refused")

    swo.engines.models["A01"] = Raises()
    out = run_done(feed, swo.swo_id)
    assert out["status"] == "done" and out["proposal_id"] and "market" in out["absent"]
    full = analysis.get(swo.swo_id, out["run_id"])
    adam = next(s for s in full["agents"] if s["seat"] == "A01")
    assert adam["status"] == "failed" and adam["report"] == "" and "RuntimeError: model endpoint refused" in adam["why"]
    assert any(line.startswith(f"{adam['name']}:") and "model endpoint refused" in line for line in full["why"])
    assert full["absent"] == out["absent"]


def test_an_analyst_the_plan_leaves_out_is_absent_and_says_so(swo, feed):
    cause = msg("desks.cycle.reason.provider_key_missing")
    original = graph._FACTORY

    def patched(desk_id):
        rt = original(desk_id)
        load = rt.load
        rt.load = lambda d, s, a: dataclasses.replace(load(d, s, a), problems={"fundamentals": cause})     # the data of that analyst did not come
        return rt

    graph.register_runtime_factory(patched)
    out = run_done(feed, swo.swo_id)
    full = analysis.get(swo.swo_id, out["run_id"])
    ezra = next(s for s in full["agents"] if s["seat"] == "A06")
    assert ezra["status"] == "absent" and ezra["why"] == cause and ezra["report"] == ""


def test_a_cycle_that_stops_early_keeps_the_seats_that_ran_and_marks_the_step_it_stopped_at(swo, feed):
    swo.engines.models.clear()                                          # no model answers: no analyst reports and the cycle stops at the analysts
    out = run_done(feed, swo.swo_id)
    full = analysis.get(swo.swo_id, out["run_id"])
    assert out["proposal_id"] is None and full["stopped_at"] == "analysts" and full["rating"] is None
    states = {s["node"]: s["state"] for s in full["steps"]}
    assert states["plan"] == "done" and states["analysts"] == "stopped" and all(states[n] == "skipped" for n in report.STEPS[2:])
    assert full["agents"] and all(s["step"] == "analysts" for s in full["agents"]) and full["decision"] is None


def test_a_record_from_before_this_delivery_still_reads_with_no_agents():
    record = {"id": 9, "desk_id": 1, "status": "done", "started_at": "2026-11-20 10:00:00", "ended_at": "2026-11-20 10:01:00",
              "record": {"entries": {"symbol": "MSFT", "as_of": "2026-11-20T10:00:00+00:00"},
                         "outputs": {"trail": ["plan", "analysts", "close"], "stopped_at": "analysts", "reason": "no_analyst_report", "absent": ["market"]}}}
    full = report.view(record)
    assert full["agents"] == [] and full["symbol"] == "MSFT" and full["stopped_at"] == "analysts" and full["decision"] is None
    assert report.brief(record)["has_proposal"] is False


def test_the_text_of_a_seat_is_cut_and_a_status_outside_the_three_is_refused():
    entry = report.seat_entry("A09", step="debate", status="ok", summary="s" * 5000, report="r" * 50_000, why="w" * 5000, tools=["a", "a", "b"])
    assert len(entry["summary"]) == report.MAX_SUMMARY and len(entry["report"]) == report.MAX_REPORT and len(entry["why"]) == report.MAX_WHY
    assert entry["tools"] == ["a", "b"] and entry["name"] == SEATS["A09"].name
    with pytest.raises(ValueError):
        report.seat_entry("A09", step="debate", status="empty")


# ---- the door: HTTP ----------------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(swo):
    app = FastAPI()
    app.include_router(desks_router)
    with TestClient(app) as c:
        c.headers.update({"X-Hokma-Token": SESSION_TOKEN})
        yield c


def test_the_routes_start_with_202_list_and_read_and_refuse_a_second_run_with_409(client, swo, feed):
    hold_at(feed, "analysts")
    started = client.post(f"/api/desks/{swo.swo_id}/analysis", json={"symbol": "AAPL"})
    assert started.status_code == 202 and started.json()["status"] == "running"
    run_id = started.json()["run_id"]
    wait_for(lambda: "analysts" in feed.entered())
    second = client.post(f"/api/desks/{swo.swo_id}/analysis", json={"symbol": "aapl"})
    assert second.status_code == 409 and second.json()["detail"]["code"] == "analysis_running" and second.json()["detail"]["run_id"] == run_id
    assert client.get(f"/api/desks/{swo.swo_id}/analysis/{run_id}").json()["status"] == "running"
    feed.gate.set()
    settle(feed)
    report_ = client.get(f"/api/desks/{swo.swo_id}/analysis/{run_id}")
    assert report_.status_code == 200 and report_.json()["rating"] == "APPROVE" and report_.json()["agents"]
    listing = client.get(f"/api/desks/{swo.swo_id}/analysis?limit=5").json()
    assert listing["available"] is True and [a["run_id"] for a in listing["analyses"]] == [run_id] and listing["analyses"][0]["has_proposal"] is True
    assert client.get(f"/api/desks/{swo.swo_id}/analysis/999999").status_code == 404
    assert client.get(f"/api/desks/{swo.swo_id}/analysis?limit=0").status_code == 400
    assert client.post(f"/api/desks/{swo.swo_id}/analysis", json={"symbol": "not a symbol!"}).status_code == 400
    assert client.post(f"/api/desks/{swo.swo_id}/analysis", json={"symbol": "AAPL", "strategy": "nope"}).status_code == 400
    assert client.post("/api/desks/9999/analysis", json={"symbol": "AAPL"}).status_code == 404
    assert client.get(f"/api/desks/{swo.swo_id}/analysis", headers={"X-Hokma-Token": "bad"}).status_code == 401


def test_a_run_of_another_desk_does_not_open_from_this_one(client, swo, feed):
    out = run_done(feed, swo.swo_id)
    other = service.create_desk("Other swing", 5000)
    assert client.get(f"/api/desks/{other['id']}/analysis/{out['run_id']}").status_code == 404


# ---- desks with no analysis ----------------------------------------------------------------------------------------------------------

def test_a_desk_that_is_not_for_options_has_no_analysis_and_says_why(client, swo):
    plain = service.create_desk("Futures NQ", 5000)
    found = analysis.availability(plain["id"])
    assert found["available"] is False and found["reason"] == "not_options_desk" and found["message"] == msg("desks.err.analysis_not_options_desk")
    listing = client.get(f"/api/desks/{plain['id']}/analysis").json()
    assert listing["available"] is False and listing["analyses"] == [] and listing["message"]
    refused = client.post(f"/api/desks/{plain['id']}/analysis", json={"symbol": "AAPL"})
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "analysis_unavailable" and refused.json()["detail"]["reason"] == "not_options_desk"
    assert refused.json()["detail"]["message"] == msg("desks.err.analysis_not_options_desk")
    added = client.post(f"/api/desks/{plain['id']}/panels", json={"id": "analysis"})
    assert added.status_code == 422 and added.json()["detail"]["code"] == "analysis_unavailable"
    assert "analysis" not in client.get(f"/api/desks/{plain['id']}").json()["panels"]
    ok = client.post(f"/api/desks/{swo.swo_id}/panels", json={"id": "analysis"})
    assert ok.status_code == 201 and "analysis" in ok.json()["panels"] and ok.json()["panels"].index("analysis") < ok.json()["panels"].index("rules")
    assert client.delete(f"/api/desks/{swo.swo_id}/panels/analysis").status_code == 200
    tool, err = call("desk_analysis_start", {"desk_id": plain["id"], "symbol": "AAPL"})
    assert err and tool["error_code"] == "blocked" and msg("desks.err.analysis_not_options_desk") in tool["message"]


# ---- the chat socket ---------------------------------------------------------------------------------------------------------------

def test_the_socket_pushes_the_progress_to_the_panels_that_watch_the_desk_and_to_no_other(swo):
    engine = ChatEngine()
    watching, stranger = [], []

    async def go():
        done = asyncio.Event()

        async def send_watching(text):
            message = json.loads(text)
            watching.append(message)
            if message.get("event") in ("finished", "failed"):
                done.set()

        async def send_stranger(text):
            stranger.append(json.loads(text))

        mine, other = Connection(send_watching), Connection(send_stranger)
        await engine.handle(mine, json.dumps({"type": "watch_desk", "desk_id": swo.swo_id}))
        await engine.handle(other, json.dumps({"type": "ping"}))
        await engine.handle(mine, json.dumps({"type": "watch_desk", "desk_id": True}))
        await engine.handle(mine, json.dumps({"type": "watch_desk"}))
        out = await asyncio.get_running_loop().run_in_executor(None, lambda: analysis.start(swo.swo_id, "AAPL", as_of=AS_OF))
        await asyncio.wait_for(done.wait(), 180)
        await engine.shutdown()
        return out

    out = asyncio.run(go())
    assert watching[0] == {"type": "watching", "desk_id": swo.swo_id}
    assert [m["code"] for m in watching if m["type"] == "error"] == ["bad_message", "bad_message"]
    pushed = [m for m in watching if m["type"] == "analysis"]
    assert pushed[0]["event"] == "started" and pushed[0]["run_id"] == out["run_id"] and pushed[-1]["event"] == "finished"
    assert [m["node"] for m in pushed if m["event"] == "step" and m["phase"] == "enter"][:8] == list(report.STEPS)
    assert stranger == [{"type": "pong"}]                              # a socket that does not watch the desk receives nothing of it


# ---- limits, the chat door and the process that runs the cycle (the fix of the rejection of 3337-A) ------------------------------------------

def test_the_limits_are_two_per_desk_and_four_in_all():
    assert analysis.MAX_PER_DESK == 2 and analysis.MAX_TOTAL == 4 and analysis.MAX_PER_DESK < analysis.MAX_TOTAL


def test_a_third_analysis_on_one_desk_is_refused_busy_in_both_languages(swo, feed, monkeypatch):
    hold_at(feed, "analysts")
    first = analysis.start(swo.swo_id, "AAPL", as_of=AS_OF)
    second = analysis.start(swo.swo_id, "MSFT", as_of=AS_OF)
    assert first["run_id"] != second["run_id"]
    wait_for(lambda: feed.entered().count("analysts") == 2)
    with pytest.raises(DeskError) as busy:
        analysis.start(swo.swo_id, "NVDA", as_of=AS_OF)
    assert busy.value.code == "analysis_busy" and busy.value.status == 409 and busy.value.extra == {"scope": "desk", "limit": 2}
    assert len(rows(swo, "SELECT id FROM run_records WHERE kind = :k", k=graph.KIND)) == 2          # the refused one left no record
    said = {}
    for lang in ("en", "es"):
        with use_language(lang):
            said[lang] = busy.value.message
            assert said[lang] == msg("desks.err.analysis_busy_desk", limit=2) and "2" in said[lang]
    assert said["en"] != said["es"] and msg("desks.err.analysis_busy_desk") != msg("desks.err.analysis_busy_total")
    feed.gate.set()
    wait_for(lambda: not analysis._live)


def test_the_global_limit_counts_every_desk_and_the_chat_door_obeys_it_too(swo, feed, monkeypatch):
    monkeypatch.setattr(analysis, "MAX_PER_DESK", 5)
    monkeypatch.setattr(analysis, "MAX_TOTAL", 2)
    hold_at(feed, "analysts")
    analysis.start(swo.swo_id, "AAPL", as_of=AS_OF)
    queued, err = call("desk_analysis_start", {"desk_id": swo.swo_id, "symbol": "MSFT"})
    assert not err and queued["status"] == "queued"                    # a request opened by the tool server counts from the moment it opens
    with pytest.raises(DeskError) as busy:
        analysis.start(swo.swo_id, "NVDA", as_of=AS_OF)
    assert busy.value.code == "analysis_busy" and busy.value.extra == {"scope": "total", "limit": 2}
    refused, err = call("desk_analysis_start", {"desk_id": swo.swo_id, "symbol": "NVDA"})
    assert err and refused["error_code"] == "blocked" and "analysis_busy" in str(refused)
    feed.gate.set()
    analysis.drain_requests(swo.db)
    wait_for(lambda: not analysis._live)


def test_a_desk_over_its_weekly_budget_opens_no_analysis(swo, feed, monkeypatch):
    seen = []

    def exhausted(project_id, db=None, now=None):
        seen.append(project_id)
        raise DeskError("desk_budget_exhausted", 409)

    monkeypatch.setattr(service, "assert_can_run", exhausted)
    with pytest.raises(DeskError) as panel:
        analysis.start(swo.swo_id, "AAPL", as_of=AS_OF)
    refused, err = call("desk_analysis_start", {"desk_id": swo.swo_id, "symbol": "AAPL"})
    assert panel.value.code == "desk_budget_exhausted" and err and refused["error_code"] == "blocked" and len(seen) == 2 and seen[0] == seen[1]
    assert rows(swo, "SELECT id FROM run_records WHERE kind = :k", k=graph.KIND) == []


def test_the_tool_answers_at_once_with_the_run_id_and_runs_nothing_in_its_own_process(swo, feed):
    started, err = call("desk_analysis_start", {"desk_id": swo.swo_id, "symbol": "aapl"})
    assert not err and started["status"] == "queued" and started["symbol"] == "AAPL" and isinstance(started["run_id"], int)
    time.sleep(0.5)
    assert feed.events == [] and not analysis._live                    # no thread, no event: the cycle did not start here
    record = runs.get(started["run_id"], db=swo.db)
    assert record["status"] == "running" and record["record"]["entries"]["symbol"] == "AAPL" and record["record"]["entries"]["requested"] is True
    live = call("desk_analysis_get", {"desk_id": swo.swo_id, "run_id": started["run_id"]})[0]
    assert live["status"] == "running" and live["symbol"] == "AAPL"       # readable at once, before any checkpoint exists
    assert analysis.drain_requests(swo.db) == [started["run_id"]]
    settle(feed)


def test_the_request_of_the_chat_runs_in_the_app_and_pushes_every_step(swo, feed):
    started, err = call("desk_analysis_start", {"desk_id": swo.swo_id, "symbol": "AAPL"})
    assert not err
    analysis.drain_requests(swo.db)                                   # what the scheduler of the app does in its tick
    settle(feed)
    assert feed.events[0]["event"] == "started" and feed.events[0]["run_id"] == started["run_id"]
    assert feed.entered()[:8] == list(report.STEPS) and feed.events[-1]["event"] == "finished" and feed.events[-1]["status"] == "done"
    assert analysis.get(swo.swo_id, started["run_id"])["rating"] == "APPROVE"


def test_the_scheduler_tick_picks_the_request_up_without_a_timer_of_its_own(swo, feed):
    from hokma.scheduler import loop

    started, err = call("desk_analysis_start", {"desk_id": swo.swo_id, "symbol": "AAPL"})
    assert not err
    loop.TICK_HOOKS.append(analysis.drain_requests)

    async def go():
        scheduler = loop.Scheduler(db=swo.db, tick=0.05)
        await scheduler.start()
        try:
            for _ in range(200):
                if feed.events:
                    break
                await asyncio.sleep(0.05)
        finally:
            await scheduler.stop()

    try:
        asyncio.run(go())
    finally:
        loop.TICK_HOOKS.remove(analysis.drain_requests)
    assert feed.events and feed.events[0]["run_id"] == started["run_id"]
    settle(feed)


def test_a_hook_that_fails_does_not_stop_the_tick(swo):
    from hokma.scheduler import loop

    calls = []

    def bad(db):
        calls.append("bad")
        raise RuntimeError("boom")

    loop.TICK_HOOKS[:] = [bad, lambda db: calls.append("good")]
    try:
        loop.Scheduler(db=swo.db)._run_hooks()
    finally:
        loop.TICK_HOOKS.clear()
    assert calls == ["bad", "good"]


def test_panel_and_chat_at_once_never_run_the_same_asset_twice(swo, feed):
    hold_at(feed, "analysts")
    out = analysis.start(swo.swo_id, "AAPL", as_of=AS_OF)
    wait_for(lambda: "analysts" in feed.entered())
    refused, err = call("desk_analysis_start", {"desk_id": swo.swo_id, "symbol": "AAPL"})
    assert err and refused["error_code"] == "blocked" and "analysis_running" in str(refused) and str(out["run_id"]) in str(refused)
    assert analysis.drain_requests(swo.db) == []                      # a run of the panel is not a request
    feed.gate.set()
    settle(feed)
    queued, err = call("desk_analysis_start", {"desk_id": swo.swo_id, "symbol": "MSFT"})
    assert not err
    with pytest.raises(DeskError) as same:                            # the other way round: the chat asked first
        analysis.start(swo.swo_id, "MSFT", as_of=AS_OF)
    assert same.value.code == "analysis_running" and same.value.extra["run_id"] == queued["run_id"]
    assert analysis.drain_requests(swo.db) == [queued["run_id"]]
    assert analysis.drain_requests(swo.db) == []                      # picked once: a second pass does not start it again
    feed.done.clear()
    settle(feed)
    assert len(rows(swo, "SELECT id FROM run_records WHERE kind = :k", k=graph.KIND)) == 2


def test_the_runs_a_dead_process_left_open_are_closed_as_failed_at_start(swo, feed):
    orphan = runs.begin(swo.swo_id, graph.KIND, signature="dead", entries={"symbol": "AAPL", "requested": False}, db=swo.db)
    bare = runs.begin(swo.swo_id, graph.KIND, signature="dead-too", db=swo.db)                # an older door left no asset in its record
    with pytest.raises(DeskError) as blocked:
        analysis.start(swo.swo_id, "AAPL", as_of=AS_OF)
    assert blocked.value.code == "analysis_running" and blocked.value.extra["run_id"] == orphan
    assert sorted(analysis.recover_orphans(swo.db)) == [orphan, bare]
    for run_id in (orphan, bare):
        closed = runs.get(run_id, db=swo.db)
        assert closed["status"] == "failed" and closed["ended_at"]
    assert analysis.recover_orphans(swo.db) == []
    out = run_done(feed, swo.swo_id)                                  # the asset is free again
    assert out["status"] == "done"


def test_a_request_nobody_picks_up_is_closed_as_failed(swo):
    started, err = call("desk_analysis_start", {"desk_id": swo.swo_id, "symbol": "AAPL"})
    assert not err
    later = datetime.now(timezone.utc) + analysis.REQUEST_TTL + timedelta(seconds=5)
    assert analysis.drain_requests(swo.db, now=later) == []
    assert runs.get(started["run_id"], db=swo.db)["status"] == "failed"


def test_a_run_that_cannot_start_in_the_app_is_closed_failed_and_tells_the_panels(swo, feed, monkeypatch):
    started, err = call("desk_analysis_start", {"desk_id": swo.swo_id, "symbol": "AAPL"})
    assert not err

    def broken(desk_id, db=None):
        raise graph.CycleError("no_runtime", "no runtime")

    monkeypatch.setattr(graph, "preflight", broken)
    analysis.drain_requests(swo.db)
    settle(feed)
    assert feed.events[-1]["event"] == "failed" and feed.events[-1]["run_id"] == started["run_id"]
    assert runs.get(started["run_id"], db=swo.db)["status"] == "failed"


# ---- the chat tools ----------------------------------------------------------------------------------------------------------------

def test_the_owner_side_tools_start_read_and_list_analyses(swo, feed):
    started, err = call("desk_analysis_start", {"desk_id": swo.swo_id, "symbol": "AAPL"})
    assert not err, started
    assert started["status"] == "queued" and started["panel"] == "analysis" and started["run_id"]
    run_id = started["run_id"]
    assert analysis.drain_requests(swo.db) == [run_id]                  # the app picks the request up and runs it
    settle(feed)
    got, err = call("desk_analysis_get", {"desk_id": swo.swo_id, "run_id": run_id})
    assert not err and got["run_id"] == run_id and got["agents"] and all(len(s["report"]) <= 1200 for s in got["agents"])
    simon, err = call("desk_analysis_get", {"desk_id": swo.swo_id, "run_id": run_id, "seat": "simon"})
    assert not err and [s["seat"] for s in simon["agents"]] == ["A09"] and simon["agents"][0]["report"]
    full = analysis.get(swo.swo_id, run_id)
    assert simon["agents"][0]["report"] == next(s for s in full["agents"] if s["seat"] == "A09")["report"]
    missing, err = call("desk_analysis_get", {"desk_id": swo.swo_id, "run_id": run_id, "seat": "Nobody"})
    assert err and missing["error_code"] == "no_data"
    listed, err = call("desk_analysis_list", {"desk_id": swo.swo_id, "limit": 5})
    assert not err and [a["run_id"] for a in listed["analyses"]] == [run_id]
    unknown, err = call("desk_analysis_get", {"desk_id": swo.swo_id, "run_id": 987654})
    assert err and unknown["error_code"] == "no_data"
    assert call("desk_analysis_list", {"desk_id": swo.swo_id, "limit": 99})[1] is True
    assert runs.get(run_id, db=swo.db)["kind"] == graph.KIND            # one record, the same one the cycle tools read
    status, err = call("desk_cycle_status", {"desk_id": swo.swo_id, "run_id": run_id})
    assert not err and status["cycle"]["rating"] == full["rating"] == "APPROVE"


def test_the_cycle_tool_is_the_same_door_it_only_queues_and_obeys_the_limits(swo, feed, monkeypatch):
    import inspect
    import pathlib

    import hokma.kit as kit_package

    for path in pathlib.Path(kit_package.__file__).parent.glob("*.py"):
        assert "run_cycle(" not in path.read_text(encoding="utf-8"), path.name        # the tool server never runs a cycle
    first, err = call("desk_cycle_run", {"desk_id": swo.swo_id, "symbol": "AAPL", "as_of": AS_OF.isoformat()})
    assert not err and first["status"] == "queued" and not analysis._live and feed.events == []
    again, err = call("desk_cycle_run", {"desk_id": swo.swo_id, "symbol": "AAPL"})                   # the same asset: the guard answers with the run that goes
    assert err and again["error_code"] == "blocked" and f"run {first['run_id']}" in again["message"]
    assert call("desk_cycle_run", {"desk_id": swo.swo_id, "symbol": "MSFT"})[1] is False            # a second asset fits (two per desk)
    busy, err = call("desk_cycle_run", {"desk_id": swo.swo_id, "symbol": "NVDA"})
    assert err and busy["error_code"] == "blocked" and "analysis_busy" in busy["message"]
    monkeypatch.setattr(analysis, "MAX_PER_DESK", 9)
    monkeypatch.setattr(analysis, "MAX_TOTAL", 2)
    total, err = call("desk_cycle_run", {"desk_id": swo.swo_id, "symbol": "TSLA"})
    assert err and "analysis_busy" in total["message"]
    status, err = call("desk_cycle_status", {"desk_id": swo.swo_id, "run_id": first["run_id"]})
    assert not err and status["cycle"]["status"] == "running" and status["cycle"]["in_progress"] is True and status["cycle"]["symbol"] == "AAPL"


def test_recover_orphans_leaves_alone_a_run_this_process_is_running(swo, feed):
    mine = runs.begin(swo.swo_id, graph.KIND, signature="mine", entries={"symbol": "AAPL", "requested": False}, db=swo.db)
    dead = runs.begin(swo.swo_id, graph.KIND, signature="dead", entries={"symbol": "MSFT", "requested": False}, db=swo.db)
    analysis._live[(swo.swo_id, "AAPL")] = mine
    try:
        assert analysis.recover_orphans(swo.db) == [dead]
        assert runs.get(mine, db=swo.db)["status"] == "running" and runs.get(dead, db=swo.db)["status"] == "failed"
    finally:
        analysis._live.pop((swo.swo_id, "AAPL"), None)
        runs.finish(mine, "failed", db=swo.db)


def test_a_desk_chief_cannot_analyze_on_a_desk_that_is_not_its_own(swo):
    other = service.create_desk("Other swing", 5000)
    refused, err = call("desk_analysis_start", {"desk_id": other["id"], "symbol": "AAPL"}, agent="desk-chief")
    assert err and refused["error_code"] == "blocked"


def test_postgres_takes_the_advisory_lock_before_the_guard_counts_and_sqlite_does_not():
    from contextlib import contextmanager
    from types import SimpleNamespace

    from hokma.backoffice import runs

    for engine, expected in (("postgres", True), ("sqlite", False)):
        sql: list[str] = []

        class Session:
            def __init__(self):
                self.engine = engine

            def execute(self, text, params=None):
                sql.append(text)
                assert params == {"k": runs.BEGIN_LOCK} or "pg_advisory" not in text
                return 1

            def insert(self, table, values):
                sql.append("INSERT " + table)
                return 7

        class FakeDb:
            @contextmanager
            def write(self):
                yield SimpleNamespace(session=Session())

        assert runs.begin(1, "cycle", signature="s", guard=lambda u: sql.append("GUARD"), db=FakeDb()) == 7
        lock = [i for i, s in enumerate(sql) if "pg_advisory_xact_lock" in s]
        assert bool(lock) is expected
        if expected:
            assert lock == [0] and sql[1:] == ["GUARD", "INSERT run_records"]
        else:
            assert sql == ["GUARD", "INSERT run_records"]


def test_the_learning_of_step_9_asks_the_weekly_budget_before_the_model(swo, feed, monkeypatch):
    seen = []

    def exhausted(project_id, db=None, now=None):
        seen.append(project_id)
        raise DeskError("desk_budget_exhausted", 409)

    monkeypatch.setattr(service, "assert_can_run", exhausted)
    refused, err = call("desk_cycle_learn", {"desk_id": swo.swo_id, "run_id": 1})
    assert err and refused["error_code"] == "blocked" and "desk_budget_exhausted" in str(refused) and len(seen) == 1

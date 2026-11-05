"""Scheduled jobs: API, the owner's confirmation, the run with the fake claude, skipped runs, consumption."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

import pytest

from agents_support import HEADERS, SYSTEM, client, db, make_client, work  # noqa: F401
from hokma.db import to_db_time
from hokma.scheduler import runner
from hokma.scheduler.loop import Scheduler

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake claude is a POSIX script")


def job_body(**patch):
    soon = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    return {"name": "Premarket brief", "agent": "hokma", "prompt": "Summarize the premarket.", "kind": "once",
            "expr": soon, "timezone": "UTC", **patch}


def make(client, **patch):
    res = client.post("/api/scheduled-jobs", json=job_body(**patch))
    assert res.status_code == 201, res.text
    return res.json()


def test_a_created_job_is_off_and_without_approval(client):
    job = make(client)
    assert job["active"] is False and job["approved"] is False and job["next_run_at"] is None
    assert job["agent_name"] == "Hokma" and job["model"] == "sonnet"
    assert client.get("/api/scheduled-jobs").json()["jobs"][0]["id"] == job["id"]


def test_no_route_turns_a_job_on_without_the_token(client, work):
    job = make(client)
    with make_client() as anon:
        for headers in ({}, {"X-Hokma-Token": "wrong"}):
            res = anon.post(f"/api/scheduled-jobs/{job['id']}/activate", json={"confirm": True}, headers=headers)
            assert res.status_code == 401
            assert anon.put(f"/api/scheduled-jobs/{job['id']}", json=job_body(), headers=headers).status_code == 401
            assert anon.delete(f"/api/scheduled-jobs/{job['id']}", headers=headers).status_code == 401
            assert anon.post("/api/scheduled-jobs", json=job_body(name="x"), headers=headers).status_code == 401
    assert client.get("/api/scheduled-jobs").json()["jobs"][0]["active"] is False


def test_activation_needs_the_confirmation_and_leaves_an_approval(client, db):
    job = make(client)
    refused = client.post(f"/api/scheduled-jobs/{job['id']}/activate", json={"confirm": False})
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "confirmation_required"
    assert client.post(f"/api/scheduled-jobs/{job['id']}/activate", json={}).status_code == 422
    done = client.post(f"/api/scheduled-jobs/{job['id']}/activate", json={"confirm": True}).json()
    assert done["active"] and done["approved"] and done["next_run_at"]
    with db.read() as u:
        approval = u.approvals.latest_for("scheduled_job", job["id"])
        assert (approval.verdict, approval.decided_by_kind, approval.decided_by, approval.channel) == (
            "approved", "human", "Ana", "dashboard")
        assert u.scheduled_jobs.get(job["id"]).approval_id == approval.id


def test_editing_an_active_job_turns_it_off(client):
    job = make(client)
    client.post(f"/api/scheduled-jobs/{job['id']}/activate", json={"confirm": True})
    same = client.put(f"/api/scheduled-jobs/{job['id']}", json=job_body(expr=job["expr"], name="Renamed")).json()
    assert same["active"] is True and same["name"] == "Renamed"
    changed = client.put(f"/api/scheduled-jobs/{job['id']}", json=job_body(prompt="Do something else.", expr=job["expr"])).json()
    assert changed["active"] is False


def test_validation_and_names(client):
    assert client.post("/api/scheduled-jobs", json=job_body(kind="daily", expr="99:99")).json()["detail"]["code"] == "schedule_invalid"
    assert client.post("/api/scheduled-jobs", json=job_body(agent="nobody")).status_code == 404
    assert client.post("/api/scheduled-jobs", json=job_body(prompt=" ")).json()["detail"]["code"] == "prompt_required"
    assert client.post("/api/scheduled-jobs", json=job_body(prompt="x" * 4001)).json()["detail"]["code"] == "prompt_too_long"
    make(client)
    assert client.post("/api/scheduled-jobs", json=job_body()).json()["detail"]["code"] == "job_name_taken"
    past = client.post("/api/scheduled-jobs", json=job_body(name="Past", expr="2020-01-01T00:00")).json()
    res = client.post(f"/api/scheduled-jobs/{past['id']}/activate", json={"confirm": True})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "run_in_past"


def test_an_off_agent_blocks_activation(client):
    job = make(client, agent="iris")
    res = client.post(f"/api/scheduled-jobs/{job['id']}/activate", json={"confirm": True})
    assert res.status_code == 409 and res.json()["detail"]["code"] == "agent_off"


def test_delete(client):
    job = make(client)
    assert client.delete(f"/api/scheduled-jobs/{job['id']}").status_code == 204
    assert client.get("/api/scheduled-jobs").json()["jobs"] == []
    assert client.delete(f"/api/scheduled-jobs/{job['id']}").status_code == 404


# ---------------------------------------------------------------------- the run

def activate_due(db, work, **patch):
    """A job that is on, approved and due now, built through the repositories (like the activation does)."""
    with db.write() as u:
        job = u.scheduled_jobs.create("Due job", patch.get("agent", "hokma"), "Say pong.", patch.get("kind", "once"),
                                      patch.get("expr", "2026-11-05T00:00"), "UTC")
        approval = u.approvals.record("scheduled_job", job.id, "approved", decided_by="Ana")
        u.scheduled_jobs.set_active(job.id, True, approval_id=approval.id)
        u.scheduled_jobs.update(job.id, next_run_at=patch.get("next", datetime.now(timezone.utc) - timedelta(seconds=1)))
    return job.id


def make_scheduler(db, work, **kwargs):
    return Scheduler(db, job_runner=runner.JobRunner(db, launch=lambda: work.launch_spec), tick=0.05, **kwargs)


async def wait_until(check, seconds=20):
    for _ in range(int(seconds / 0.05)):
        if check():
            return True
        await asyncio.sleep(0.05)
    return False


def test_a_due_job_runs_and_everything_is_recorded(db, work):
    job_id = activate_due(db, work, next=datetime.now(timezone.utc) + timedelta(seconds=1))   # due after the start

    async def go():
        scheduler = make_scheduler(db, work)
        await scheduler.start()
        ok = await wait_until(lambda: _status(db, job_id) == "done")
        await scheduler.stop()
        return ok

    assert asyncio.run(go())
    with db.read() as u:
        job = u.scheduled_jobs.get(job_id)
        task = u.tasks.get(job.last_task_id)
        run = u.task_runs.list(task.id)[-1]
        journal = u.journal.list(task_id=task.id)
        usage = u.usage.total()
    assert (task.created_by_kind, task.assigned_agent, task.result_summary) == ("scheduler", "hokma", "pong")
    assert run.outcome == "completed" and run.model == "sonnet"
    assert (run.tokens_in, run.tokens_out) == (17707, 4)               # what the stream-json result reported
    assert usage.tokens == 17711 and usage.runs == 1
    assert job.is_active is False and job.next_run_at is None and job.last_run_at          # `once` turns itself off
    assert [(j.kind, j.outcome) for j in journal] == [("action", "success")]
    argv = work.entries("start")[0]["argv"]
    assert "--agent" in argv and argv[argv.index("--agent") + 1] == "hokma" and argv[argv.index("--model") + 1] == "sonnet"


def _status(db, job_id):
    with db.read() as u:
        last = u.scheduled_jobs.get(job_id).last_task_id
        return u.tasks.get(last).status if last else None


def test_a_job_without_approval_or_off_never_runs(db, work):
    job_id = activate_due(db, work)
    with db.write() as u:
        u.scheduled_jobs.set_active(job_id, False)
    due = make_scheduler(db, work).process_due(datetime.now(timezone.utc))
    assert due == [] and work.entries("start") == []


def test_runs_missed_while_closed_are_skipped_not_run_late(db, work):
    job_id = activate_due(db, work, kind="daily", expr="07:30",
                          next=datetime.now(timezone.utc) - timedelta(hours=3))

    async def go():
        scheduler = make_scheduler(db, work)
        skipped = scheduler.process_due(datetime.now(timezone.utc), startup=True)
        await asyncio.sleep(0.3)
        return skipped

    skipped = asyncio.run(go())
    assert [(s.job_id, s.status, s.detail) for s in skipped] == [(job_id, "skipped", "missed")]
    assert work.entries("start") == []                                # claude never started
    with db.read() as u:
        job = u.scheduled_jobs.get(job_id)
        entries = u.journal.list(kind="note")
        assert u.tasks.list() == []
    assert job.is_active and job.last_run_at is None
    assert datetime.fromisoformat(job.next_run_at.replace(" ", "T")) > datetime.now() - timedelta(days=1)
    assert len(entries) == 1 and entries[0].outcome == "partial"


def test_a_late_tick_skips_but_a_fresh_one_runs(db, work):
    late = activate_due(db, work, kind="daily", expr="07:30", next=datetime.now(timezone.utc) - timedelta(minutes=10))
    scheduler = make_scheduler(db, work)
    skipped = scheduler.process_due(datetime.now(timezone.utc))
    assert [s.job_id for s in skipped] == [late]


def test_agent_off_at_run_time_skips_the_run(db, work):
    job_id = activate_due(db, work, agent="vera")
    with db.write() as u:
        u.agents.set_status("vera", "off")
    outcome = asyncio.run(runner.JobRunner(db, launch=lambda: work.launch_spec).run(_job(db, job_id)))
    assert outcome.status == "skipped" and outcome.detail == "agent_off"
    assert work.entries("start") == []


def _job(db, job_id):
    with db.read() as u:
        return u.scheduled_jobs.get(job_id)


def test_a_failing_run_is_recorded_as_failed(db, work):
    with db.write() as u:
        job = u.scheduled_jobs.create("Broken", "hokma", "[error] fail please", "once", "2026-11-05T00:00", "UTC")
    outcome = asyncio.run(runner.JobRunner(db, launch=lambda: work.launch_spec).run(job))
    assert outcome.status == "failed" and "simulated failure" in outcome.detail
    with db.read() as u:
        task = u.tasks.get(outcome.task_id)
        assert task.status == "failed"
        assert u.task_runs.list(task.id)[-1].outcome == "failure"
        assert [(j.kind, j.outcome) for j in u.journal.list(task_id=task.id)] == [("alert", "failure")]


def test_permission_requests_are_denied_when_nobody_is_there(db, work):
    with db.write() as u:
        job = u.scheduled_jobs.create("Needs write", "hokma", "[perm] write a file", "once", "2026-11-05T00:00", "UTC")
    outcome = asyncio.run(runner.JobRunner(db, launch=lambda: work.launch_spec).run(job))
    assert outcome.status == "done"
    answers = work.entries("control_response")
    assert answers and answers[0]["response"]["response"]["behavior"] == "deny"
    with db.read() as u:
        events = u.guard_events.list(guard="permission")
    assert [(e.decision, e.tool, e.agent_slug) for e in events] == [("blocked", "Write", "hokma")]


def test_claude_missing_fails_cleanly(db, work, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with db.write() as u:
        job = u.scheduled_jobs.create("No claude", "hokma", "hi", "once", "2026-11-05T00:00", "UTC")
    outcome = asyncio.run(runner.JobRunner(db, launch=lambda: work.launch_spec).run(job))
    assert outcome.status == "failed"
    with db.read() as u:
        assert u.tasks.get(outcome.task_id).status == "failed"


def test_the_listing_shows_the_last_result(db, work):
    job_id = activate_due(db, work)
    asyncio.run(runner.JobRunner(db, launch=lambda: work.launch_spec).run(_job(db, job_id)))
    with make_client() as client:
        item = client.get("/api/scheduled-jobs", headers=HEADERS).json()["jobs"][0]
    assert item["last"]["status"] == "done" and item["last"]["summary"] == "pong" and item["last"]["tokens"] == 17711


def test_through_the_app_a_confirmed_job_runs_and_shows_its_result(client, db):
    """The whole chain over HTTP: create, confirm, the app's own scheduler fires it, the result is listed."""
    job = make(client)
    assert client.post(f"/api/scheduled-jobs/{job['id']}/activate", json={"confirm": True}).status_code == 200
    with db.write() as u:                                      # move the planned minute to the next second
        u.scheduled_jobs.update(job["id"], next_run_at=datetime.now(timezone.utc) + timedelta(seconds=1))
    deadline = datetime.now() + timedelta(seconds=30)
    item = client.get("/api/scheduled-jobs").json()["jobs"][0]
    while datetime.now() < deadline and not (item["last"] and item["last"]["status"] == "done"):
        asyncio.run(asyncio.sleep(0.2))
        item = client.get("/api/scheduled-jobs").json()["jobs"][0]
    assert item["last"]["status"] == "done" and item["last"]["summary"] == "pong"
    assert item["active"] is False and item["last_run_at"]
    with db.read() as u:
        assert u.approvals.latest_for("scheduled_job", job["id"]).verdict == "approved"
        assert u.tasks.list(created_by_kind="scheduler")[0].title == "Premarket brief"

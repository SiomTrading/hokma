"""Scheduled jobs: API, the owner's confirmation, the run with the fake claude, skipped runs, consumption."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

import pytest

from agents_support import HEADERS, SYSTEM, client, db, make_client, work  # noqa: F401
from hokma.db import to_db_time
from hokma.scheduler import runner
from hokma.scheduler import loop
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


def test_runs_missed_beyond_the_window_are_skipped_and_asked(db, work):
    job_id = activate_due(db, work, kind="daily", expr="07:30",
                          next=datetime.now(timezone.utc) - timedelta(hours=30))

    async def go():
        scheduler = make_scheduler(db, work)
        skipped = scheduler.process_due(datetime.now(timezone.utc), startup=True)
        await asyncio.sleep(0.3)
        return skipped

    skipped = asyncio.run(go())
    assert [(s.job_id, s.status, s.detail) for s in skipped] == [(job_id, "skipped", "asked")]
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


def test_a_run_with_denied_permissions_is_not_a_clean_success(db, work):
    # bug 1129: "finished" alone hid that the agent was refused; the outcome, the task and the listing say so
    with db.write() as u:
        job = u.scheduled_jobs.create("Needs write", "hokma", "[perm] write a file", "once", "2026-11-05T00:00", "UTC")
    outcome = asyncio.run(runner.JobRunner(db, launch=lambda: work.launch_spec).run(job))
    assert outcome.status == "done" and outcome.denied == 1               # the status contract is unchanged
    assert "1" in outcome.detail.splitlines()[0] and "not everything was done" in outcome.detail
    with db.read() as u:
        task = u.tasks.get(outcome.task_id)
        assert task.result_metadata["denied"] == 1
        assert [(j.kind, j.outcome) for j in u.journal.list(task_id=task.id)] == [("action", "partial")]
    from hokma.scheduler.service import JobsService
    assert JobsService(db).get(job.id)["last"]["denied"] == 1


def test_a_clean_run_has_no_denied_permissions(db, work):
    with db.write() as u:
        job = u.scheduled_jobs.create("Plain", "hokma", "hello", "once", "2026-11-05T00:00", "UTC")
    outcome = asyncio.run(runner.JobRunner(db, launch=lambda: work.launch_spec).run(job))
    assert outcome.status == "done" and outcome.denied == 0
    from hokma.scheduler.service import JobsService
    assert JobsService(db).get(job.id)["last"]["denied"] == 0


# ------------------------------------------------------------------ 0.0.36: hotfix batch (bugs 1134 to 1141)

def test_stopping_mid_run_never_leaves_a_run_marked_running(db, work):
    """Bug 1134: shut down, restart or update during a run must not leave tasks.status/task_runs.outcome = running."""
    job_id = activate_due(db, work, next=datetime.now(timezone.utc) + timedelta(seconds=1))
    with db.write() as u:
        u.scheduled_jobs.update(job_id, prompt="[slow] keep going")

    async def go():
        scheduler = make_scheduler(db, work)
        await scheduler.start()
        started = await wait_until(lambda: _status(db, job_id) == "running")
        await scheduler.stop()
        return started

    assert asyncio.run(go())
    with db.read() as u:
        job = u.scheduled_jobs.get(job_id)
        task = u.tasks.get(job.last_task_id)
        runs = u.task_runs.list(task.id)
        alerts = u.journal.list(task_id=task.id, kind="alert")
    assert task.status == "failed" and runs[-1].outcome != "running" and runs[-1].ended_at
    assert alerts and "interrupted" in (alerts[0].body or "")
    with make_client() as client:
        item = client.get("/api/scheduled-jobs", headers=HEADERS).json()
    assert item["running"] == [] and item["jobs"][0]["last"]["status"] != "running"


def test_a_run_left_running_by_a_crash_is_recovered_on_start(db, work):
    """Bug 1134 (b): the process was killed, nothing could clean up; the next start marks it interrupted."""
    job_id = activate_due(db, work)
    job = _job(db, job_id)
    assert runner._prepare(db, job, datetime.now(timezone.utc)) not in (None,)      # claimed: running, never finished
    assert _status(db, job_id) == "running"

    async def go():
        scheduler = make_scheduler(db, work)
        await scheduler.start()
        await scheduler.stop()

    asyncio.run(go())
    assert _status(db, job_id) == "failed"
    with db.read() as u:
        task = u.tasks.get(_job(db, job_id).last_task_id)
        assert u.task_runs.list(task.id)[-1].outcome == "failure"
        assert u.journal.list(task_id=task.id, kind="alert")


def test_the_listing_shows_the_last_skipped_run(db, work):
    """Bug 1136: a skipped run was only in the journal; the list now carries it."""
    job_id = activate_due(db, work)
    runner.skip(db, _job(db, job_id), datetime.now(timezone.utc), "agent_off")
    with make_client() as client:
        item = client.get("/api/scheduled-jobs", headers=HEADERS).json()["jobs"][0]
    assert item["last_skip"]["reason"] == "agent_off" and item["last_skip"]["at"]


def test_the_listing_marks_a_run_that_went_out_late(db, work):
    """C7-3b: the journal note of a late run carries `late_planned`; the listing shows it on `last`, and not otherwise."""
    job_id = activate_due(db, work)
    with make_client() as client:
        assert client.get("/api/scheduled-jobs", headers=HEADERS).json()["jobs"][0]["last"]["late_planned"] is None
    task_id = _job(db, job_id).last_task_id
    with db.write() as u:
        u.journal.add("action", "done", actor=runner.SYSTEM, task_id=task_id,
                      refs={"scheduled_job": job_id, "late_planned": "2026-11-06 07:00:00"})
    with make_client() as client:
        item = client.get("/api/scheduled-jobs", headers=HEADERS).json()["jobs"][0]
    assert item["last"]["late_planned"] == "2026-11-06 07:00:00"


def test_a_later_run_clears_the_skip(db, work):
    job_id = activate_due(db, work)
    runner.skip(db, _job(db, job_id), datetime.now(timezone.utc), "missed")
    with db.write() as u:
        u.scheduled_jobs.mark_ran(job_id, task_id=None, last_run_at=datetime.now(timezone.utc) + timedelta(seconds=5))
    with make_client() as client:
        assert client.get("/api/scheduled-jobs", headers=HEADERS).json()["jobs"][0]["last_skip"] is None


def test_startup_skips_notify_the_listeners(db, work):
    job_id = activate_due(db, work, next=datetime.now(timezone.utc) - timedelta(hours=30))
    seen = []
    from hokma.scheduler import loop
    loop.OUTCOME_LISTENERS.append(lambda job, outcome: seen.append(outcome.status))
    try:
        make_scheduler(db, work).process_due(datetime.now(timezone.utc), startup=True)
    finally:
        loop.OUTCOME_LISTENERS.clear()
    assert seen == ["skipped"] and job_id


def test_a_name_over_200_characters_gets_a_clear_error(client):
    res = client.post("/api/scheduled-jobs", json=job_body(name="n" * 201))
    assert res.status_code == 422 and res.json()["detail"]["code"] == "name_too_long"
    assert res.json()["detail"]["message"]


def test_the_confirmation_must_be_a_real_boolean(client):
    job = make(client)
    for bad in ("true", 1, "yes"):
        assert client.post(f"/api/scheduled-jobs/{job['id']}/activate", json={"confirm": bad}).status_code == 422
    assert client.get("/api/scheduled-jobs").json()["jobs"][0]["active"] is False


def test_a_running_job_cannot_be_edited_turned_off_or_deleted(client, db):
    """Bug 1140 (6): 409 job_running, with a message."""
    from hokma.agents import api as agents_api
    job = make(client)
    assert client.post(f"/api/scheduled-jobs/{job['id']}/activate", json={"confirm": True}).status_code == 200

    class Busy:
        def running_ids(self):
            return [job["id"]]

    agents_api._running["scheduler"], real = Busy(), agents_api._running["scheduler"]
    try:
        for res in (client.put(f"/api/scheduled-jobs/{job['id']}", json=job_body()),
                    client.post(f"/api/scheduled-jobs/{job['id']}/deactivate"),
                    client.delete(f"/api/scheduled-jobs/{job['id']}")):
            assert res.status_code == 409 and res.json()["detail"]["code"] == "job_running"
            assert res.json()["detail"]["message"]
    finally:
        agents_api._running["scheduler"] = real
    assert client.post(f"/api/scheduled-jobs/{job['id']}/deactivate").status_code == 200


def test_an_interval_counts_from_the_real_tick(db, work):
    """Bug 1140 (7), accepted decision: the next run is `tick + interval`, not `planned + interval`."""
    now = datetime(2026, 11, 5, 10, 0, 40, tzinfo=timezone.utc)           # 40 s after the planned 10:00:00
    job_id = activate_due(db, work, kind="interval", expr="30m", next=datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    following, keep = runner.advance(_job(db, job_id), now)
    assert keep and following == now + timedelta(minutes=30)


# ---------------------------------------------------------------------- run now (C7-1b-b)

def test_run_now_runs_without_moving_the_next_planned_run(db, work):
    planned = datetime.now(timezone.utc) + timedelta(hours=5)
    job_id = activate_due(db, work, kind="daily", expr="07:30", next=planned)
    heard = []

    async def go():
        scheduler = make_scheduler(db, work)
        loop.OUTCOME_LISTENERS.append(lambda job, outcome: heard.append((job.id, outcome.status, outcome.detail)))
        try:
            assert scheduler.run_now(_job(db, job_id)) is True
            assert scheduler.run_now(_job(db, job_id)) is False             # already running
            ok = await wait_until(lambda: _status(db, job_id) == "done")
        finally:
            loop.OUTCOME_LISTENERS.pop()
        return ok

    assert asyncio.run(go())
    after = _job(db, job_id)
    assert after.next_run_at == _job_planned(planned) and after.is_active and after.last_run_at
    assert heard == [(job_id, "done", "pong")]                              # the same notice as a timed run


def _job_planned(moment):
    from hokma.db import to_db_time
    return to_db_time(moment)


def test_run_now_keeps_a_once_job_on(db, work):
    job_id = activate_due(db, work, next=datetime.now(timezone.utc) + timedelta(hours=5))   # kind "once"
    asyncio.run(runner.JobRunner(db, launch=lambda: work.launch_spec).run(_job(db, job_id), move_on=False))
    after = _job(db, job_id)
    assert after.is_active and after.next_run_at and _status(db, job_id) == "done"


def test_run_now_with_the_agent_off_does_not_touch_the_job(db, work):
    job_id = activate_due(db, work, agent="vera", next=datetime.now(timezone.utc) + timedelta(hours=5))
    with db.write() as u:
        u.agents.set_status("vera", "off")
    before = _job(db, job_id)
    outcome = asyncio.run(runner.JobRunner(db, launch=lambda: work.launch_spec).run(before, move_on=False))
    assert (outcome.status, outcome.detail) == ("skipped", "agent_off")
    assert _job(db, job_id).next_run_at == before.next_run_at and work.entries("start") == []


def test_run_now_waits_for_a_free_slot(db, work):
    async def go():
        scheduler = make_scheduler(db, work)
        ids = []
        for n in range(5):
            with db.write() as u:
                job = u.scheduled_jobs.create(f"Job {n}", "hokma", "Say pong.", "daily", "07:30", "UTC")
                approval = u.approvals.record("scheduled_job", job.id, "approved", decided_by="Ana")
                u.scheduled_jobs.set_active(job.id, True, approval_id=approval.id)
            ids.append(job.id)
        for job_id in ids:
            scheduler.run_now(_job(db, job_id))
        peak = 0
        for _ in range(400):
            with db.read() as u:
                states = [u.tasks.get(u.scheduled_jobs.get(i).last_task_id).status
                          if u.scheduled_jobs.get(i).last_task_id else "queued" for i in ids]
            peak = max(peak, states.count("running"))
            if all(s == "done" for s in states):
                break
            await asyncio.sleep(0.05)
        await scheduler.stop()
        return peak, states

    peak, states = asyncio.run(go())
    assert states == ["done"] * 5 and peak <= loop.MAX_PARALLEL


def test_run_now_refuses_what_it_should(client, db):
    from hokma.agents import api as agents_api
    assert client.post("/api/scheduled-jobs/999/run").status_code == 404
    job = make(client)
    res = client.post(f"/api/scheduled-jobs/{job['id']}/run")                  # never turned on: not approved
    assert res.status_code == 409 and res.json()["detail"]["code"] == "job_not_approved" and res.json()["detail"]["message"]
    client.post(f"/api/scheduled-jobs/{job['id']}/activate", json={"confirm": True})
    agents_api._running["scheduler"], app_scheduler = None, agents_api._running["scheduler"]
    assert client.post(f"/api/scheduled-jobs/{job['id']}/run").json()["detail"]["code"] == "scheduler_off"

    class Fake:
        started = []

        def running_ids(self):
            return []

        def run_now(self, job):
            self.started.append(job.id)
            return True

    agents_api._running["scheduler"], real = Fake(), app_scheduler
    try:
        res = client.post(f"/api/scheduled-jobs/{job['id']}/run")
        assert res.status_code == 202 and Fake.started == [job["id"]]
        agents_api._running["scheduler"].running_ids = lambda: [job["id"]]
        res = client.post(f"/api/scheduled-jobs/{job['id']}/run")
        assert res.status_code == 409 and res.json()["detail"]["code"] == "job_running"
        with db.write() as u:
            u.agents.set_status("hokma", "off")
        assert client.post(f"/api/scheduled-jobs/{job['id']}/run").json()["detail"]["code"] == "agent_off"
    finally:
        agents_api._running["scheduler"] = real

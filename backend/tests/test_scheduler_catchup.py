"""C7-3a: a routine missed while Hokma was closed runs once when it opens (24 h window, 3 h for the briefing); older
than that it is skipped and the owner is asked by Telegram (two buttons) and the answer goes to the journal."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agents_support import db, work  # noqa: F401
from hokma.i18n import t
from hokma.scheduler import loop, runner
from hokma.scheduler.loop import Scheduler
from hokma.scheduler.service import BRIEFING_NAME
from test_scheduler_jobs import _job, _status, activate_due, make_scheduler, wait_until
from hokma.telegram.glue import TelegramGlue
from test_telegram_glue import FakeApp, FakeEngine, MemoryKeys, aio, button, linked, tap

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake claude is a POSIX script")


@pytest.fixture
def env(work, db, tmp_path, monkeypatch):
    """The Telegram glue of test_telegram_glue, on the same database as the scheduler."""
    monkeypatch.setenv("HOKMA_LANG", "en")
    app, engine = FakeApp(), FakeEngine()

    async def factory(token):
        return app

    glue = TelegramGlue(lambda: db, lambda: engine, keys=MemoryKeys(), application_factory=factory,
                        data_dir=tmp_path / "pairing")
    return SimpleNamespace(db=db, app=app, engine=engine, glue=glue)


def hours_ago(h):
    return datetime.now(timezone.utc) - timedelta(hours=h)


async def open_hokma_async(db, work):
    """What `_loop` does on its first tick, then waits for the runs to end. Returns the skips."""
    scheduler = make_scheduler(db, work)
    skipped = scheduler.process_due(datetime.now(timezone.utc), startup=True)
    await wait_until(lambda: not scheduler.running_ids())
    return skipped


def open_hokma(db, work):
    return asyncio.run(open_hokma_async(db, work))


def late_refs(db, job_id):
    with db.read() as u:
        return [e.refs for e in u.journal.list() if isinstance(e.refs, dict) and e.refs.get("scheduled_job") == job_id]


def test_a_daily_routine_missed_runs_once_and_the_journal_says_it_was_late(db, work):
    job_id = activate_due(db, work, kind="daily", expr="07:30", next=hours_ago(3))
    planned = _job(db, job_id).next_run_at
    assert open_hokma(db, work) == []
    after = _job(db, job_id)
    assert _status(db, job_id) == "done" and len(work.entries("start")) == 1
    assert datetime.fromisoformat(after.next_run_at.replace(" ", "T")).replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
    assert {"scheduled_job": job_id, "late_planned": planned} in late_refs(db, job_id)


def test_an_interval_missed_five_times_runs_once_and_the_next_is_in_the_future(db, work):
    job_id = activate_due(db, work, kind="interval", expr="30m", next=hours_ago(3))
    open_hokma(db, work)
    assert len(work.entries("start")) == 1
    assert _job(db, job_id).next_run_at > datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def test_beyond_the_window_it_is_skipped_and_asked_not_run(db, work):
    job_id = activate_due(db, work, kind="daily", expr="07:30", next=hours_ago(30))
    skipped = open_hokma(db, work)
    assert [(s.job_id, s.status, s.detail) for s in skipped] == [(job_id, "skipped", "asked")]
    assert work.entries("start") == [] and _job(db, job_id).last_run_at is None
    after = _job(db, job_id)
    assert after.is_active and after.next_run_at > hours_ago(0).strftime("%Y-%m-%d %H:%M:%S")
    assert late_refs(db, job_id)[0]["reason"] == "asked"


def test_the_briefing_window_is_three_hours(db, work):
    inside = activate_due(db, work, kind="daily", expr="08:00", next=hours_ago(2))
    with db.write() as u:
        u.scheduled_jobs.update(inside, name=BRIEFING_NAME)
    assert open_hokma(db, work) == [] and _status(db, inside) == "done"
    other = activate_due(db, work, kind="daily", expr="08:00", next=hours_ago(4))
    with db.write() as u:
        u.scheduled_jobs.update(other, name=BRIEFING_NAME + " 2")
        u.scheduled_jobs.update(inside, name="x")
    # the briefing is the routine chosen in the Today settings, whatever its name
    from hokma.today.service import change_config
    change_config({"briefing_job_id": other}, db)
    skipped = open_hokma(db, work)
    assert [(s.job_id, s.detail) for s in skipped] == [(other, "asked")]


def test_a_once_routine_missed_runs_once_and_turns_off(db, work):
    job_id = activate_due(db, work, kind="once", next=hours_ago(5))
    open_hokma(db, work)
    after = _job(db, job_id)
    assert _status(db, job_id) == "done" and after.is_active is False and after.next_run_at is None


def test_an_off_agent_skips_it_as_agent_off(db, work):
    job_id = activate_due(db, work, agent="vera", next=hours_ago(5))
    with db.write() as u:
        u.agents.set_status("vera", "off")
    open_hokma(db, work)
    assert work.entries("start") == [] and late_refs(db, job_id)[0]["reason"] == "agent_off"


def test_a_routine_without_approval_does_not_run(db, work):
    job_id = activate_due(db, work, next=hours_ago(5))
    with db.write() as u:
        u.scheduled_jobs.set_active(job_id, False)
    assert open_hokma(db, work) == [] and work.entries("start") == []


def test_six_routines_missed_run_at_most_three_at_a_time(db, work):
    ids = []
    for n in range(6):
        with db.write() as u:
            job = u.scheduled_jobs.create(f"Job {n}", "hokma", "Say pong.", "daily", "07:30", "UTC")
            approval = u.approvals.record("scheduled_job", job.id, "approved", decided_by="Ana")
            u.scheduled_jobs.set_active(job.id, True, approval_id=approval.id)
            u.scheduled_jobs.update(job.id, next_run_at=hours_ago(4))
        ids.append(job.id)

    async def go():
        scheduler = make_scheduler(db, work)
        scheduler.process_due(datetime.now(timezone.utc), startup=True)
        peak = 0
        for _ in range(400):
            with db.read() as u:
                states = [u.tasks.get(j.last_task_id).status if j.last_task_id else "queued"
                          for j in (u.scheduled_jobs.get(i) for i in ids)]
            peak = max(peak, states.count("running"))
            if all(s == "done" for s in states):
                break
            await asyncio.sleep(0.05)
        return peak, states

    peak, states = asyncio.run(go())
    assert states == ["done"] * 6 and peak <= loop.MAX_PARALLEL


# ---------------------------------------------------------------------- Telegram

def test_a_late_run_is_announced_as_late(db, work, env):
    job_id = activate_due(db, work, kind="daily", expr="07:30", next=hours_ago(3))
    planned = _job(db, job_id).next_run_at
    heard = []

    async def go():
        await linked(env)
        loop.OUTCOME_LISTENERS.append(lambda j, o: heard.append((j, o)))
        loop.OUTCOME_LISTENERS.append(env.glue.notify_job)
        try:
            await open_hokma_async(db, work)
        finally:
            loop.OUTCOME_LISTENERS.clear()
        for task in list(env.glue._tasks):
            await task
    asyncio.run(go())
    assert heard[0][1].late_planned == planned
    text = env.app.bot.sent[-1]["text"]
    assert text.startswith(f"Routine ran late: Due job. It was due {planned} UTC; it ran when Hokma was opened.")
    assert text.endswith("pong")


@aio
async def test_too_old_asks_with_two_buttons_and_the_answer_is_in_the_journal(env, db, work):
    await linked(env)
    job_id = activate_due(db, work, kind="daily", expr="07:30", next=hours_ago(30))
    job = _job(db, job_id)
    scheduler = make_scheduler(db, work)
    loop.OUTCOME_LISTENERS.append(env.glue.notify_job)
    try:
        scheduler.process_due(datetime.now(timezone.utc), startup=True)
        for task in list(env.glue._tasks):
            await task
    finally:
        loop.OUTCOME_LISTENERS.clear()
    sent = env.app.bot.sent[-1]
    labels = [b.text for b in sent["reply_markup"].inline_keyboard[0]]
    assert labels == ["Run now", "Skip"] and job.name in sent["text"] and "Run it now?" in sent["text"]
    first, second = [b.callback_data for b in sent["reply_markup"].inline_keyboard[0]]
    assert first.startswith("lr:run:") and second == "lr:skip:" + first.split(":")[2]
    # a stranger's tap does nothing
    stranger, answers = tap(button(env.app.bot, 0), 999)
    await env.glue.link._ui.handle_callback_query(stranger)
    assert work.entries("start") == [] and answers
    # the owner skips: journal answer, nothing runs
    update, _ = tap(button(env.app.bot, 1), 111)
    await env.glue.link._ui.handle_callback_query(update)
    assert work.entries("start") == []
    assert {"scheduled_job": job_id, "late_answer": "skip"} in late_refs(db, job_id)
    # the token is single use, and a forged one finds nothing
    again, answers = tap(first, 111)
    await env.glue.link._ui.handle_callback_query(again)
    forged, forged_answers = tap("lr:run:000000000000", 111)
    await env.glue.link._ui.handle_callback_query(forged)
    assert answers == forged_answers == [env.glue.link._t("approval_gone")] and work.entries("start") == []


@aio
async def test_run_now_button_runs_the_routine_and_answers_in_the_journal(env, db, work):
    from hokma.agents import api as agents_api
    await linked(env)
    job_id = activate_due(db, work, kind="daily", expr="07:30", next=hours_ago(30))
    scheduler = make_scheduler(db, work)
    agents_api._running["scheduler"] = scheduler
    try:
        await env.glue.link.send_late_prompt(_job(db, job_id))
        data = button(env.app.bot, 0)
        update, _ = tap(data, 111)
        await env.glue.link._ui.handle_callback_query(update)
        again, _ = tap(data, 111)
        await env.glue.link._ui.handle_callback_query(again)               # the same button twice: one run only
        for _ in range(400):
            if _status(db, job_id) == "done":
                break
            await asyncio.sleep(0.05)
    finally:
        agents_api._running["scheduler"] = None
    assert _status(db, job_id) == "done" and len(work.entries("start")) == 1
    assert {"scheduled_job": job_id, "late_answer": "ran"} in late_refs(db, job_id)


@aio
async def test_a_switched_off_routine_is_not_run_by_an_old_button(env, db, work):
    from hokma.agents import api as agents_api
    await linked(env)
    job_id = activate_due(db, work, kind="daily", expr="07:30", next=hours_ago(30))
    with db.write() as u:
        u.scheduled_jobs.set_active(job_id, False)
    agents_api._running["scheduler"] = make_scheduler(db, work)
    try:
        await env.glue.link.send_late_prompt(_job(db, job_id))
        update, _ = tap(button(env.app.bot, 0), 111)
        await env.glue.link._ui.handle_callback_query(update)
        await asyncio.sleep(0.3)
    finally:
        agents_api._running["scheduler"] = None
    assert work.entries("start") == []
    assert {"scheduled_job": job_id, "late_answer": "refused"} in late_refs(db, job_id)      # not "run": it did not run
    assert await env.glue.on_late_choice(job_id, "run") == t("telegram.late_refused", name=_job(db, job_id).name)

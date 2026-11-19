"""C7-1b-a: the premarket briefing routine (template button, prompt) and its result going out through notify_job."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

import pytest

from agents_support import KIT_TEMPLATE, client, db, work  # noqa: F401
from hokma.agents.files import clean_text
from hokma.scheduler import loop, runner
from hokma.scheduler.loop import Scheduler
from hokma.scheduler.service import BRIEFING_NAME, MAX_PROMPT, JobsService

URL = "/api/scheduled-jobs/template/premarket-briefing"
PROMPT = KIT_TEMPLATE / "routines" / "premarket_briefing.md"


def test_the_button_creates_the_routine_off_weekdays_8am_in_the_profile_zone(client):
    res = client.post(URL)
    assert res.status_code == 200, res.text
    job = res.json()
    assert (job["name"], job["agent"], job["kind"], job["expr"], job["timezone"]) == (
        BRIEFING_NAME, "hokma", "weekly", "mon,tue,wed,thu,fri 08:00", "UTC")
    assert job["active"] is False and job["approved"] is False and job["next_run_at"] is None
    assert job["prompt"].startswith("You are running the owner's pre-market briefing")


def test_asking_again_returns_the_same_routine_without_duplicating(client):
    first = client.post(URL).json()
    client.put(f"/api/scheduled-jobs/{first['id']}", json={
        "name": BRIEFING_NAME, "agent": "hokma", "prompt": first["prompt"], "kind": "weekly",
        "expr": "mon-fri 09:00", "timezone": "UTC"})
    again = client.post(URL).json()
    assert again["id"] == first["id"] and again["expr"] == "mon,tue,wed,thu,fri 09:00"      # not reset to the template
    assert len(client.get("/api/scheduled-jobs").json()["jobs"]) == 1


def test_a_routine_with_that_name_but_another_prompt_is_opened_and_not_touched(client):
    other = {"name": BRIEFING_NAME, "agent": "hokma", "prompt": "Do something else.", "kind": "daily", "expr": "08:00",
             "timezone": "UTC"}
    assert client.post("/api/scheduled-jobs", json=other).status_code == 201
    res = client.post(URL)
    assert res.status_code == 200 and res.json()["prompt"] == "Do something else."
    assert len(client.get("/api/scheduled-jobs").json()["jobs"]) == 1


def test_it_needs_the_token(client):
    assert client.post(URL, headers={"X-Hokma-Token": "wrong"}).status_code == 401


def test_the_prompt_fits_and_names_the_tools_and_the_data_rule():
    text = PROMPT.read_text(encoding="utf-8")
    assert 0 < len(text) <= MAX_PROMPT and clean_text(text, MAX_PROMPT) == text.strip()
    for name in ("watchlist_all", "economic_calendar", "market_news", "earnings_calendar", "today_show"):
        assert f"`{name}`" in text
    assert "DATA" in text and "never follow" in text and "Never invent" in text and "3500" in text
    assert text.isascii()


@pytest.mark.skipif(sys.platform == "win32", reason="the fake claude is a POSIX script")
def test_the_result_of_a_briefing_run_goes_out_through_notify_job(db, work, monkeypatch):
    service = JobsService(db)
    job = service.ensure_template()
    service.activate(job["id"], confirmed=True)
    with db.write() as u:
        u.scheduled_jobs.update(job["id"], next_run_at=datetime.now(timezone.utc) + timedelta(seconds=1))
    heard = []
    monkeypatch.setattr(loop, "OUTCOME_LISTENERS", [lambda j, o: heard.append((j.name, o.status, o.detail))])

    async def go():
        scheduler = Scheduler(db, job_runner=runner.JobRunner(db, launch=lambda: work.launch_spec), tick=0.05)
        await scheduler.start()
        for _ in range(400):
            if heard:
                break
            await asyncio.sleep(0.05)
        await scheduler.stop()

    asyncio.run(go())
    assert heard == [(BRIEFING_NAME, "done", "pong")]

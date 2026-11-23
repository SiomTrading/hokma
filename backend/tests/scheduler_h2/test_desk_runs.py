"""A desk agent programs a run of its own desk (SPEC-018 B7: OA-07). The tool `schedule_run` of the kit calls these two functions."""
from datetime import datetime, timezone

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures and helper)

from hokma.agents.service import AgentsError
from hokma.broker.service import Who, owner_who
from hokma.scheduler import service as jobs
from hokma.scheduler.service import JobsService, list_desk_runs, schedule_desk_run

EXPR = "cron 30 9 * * mon-fri tz=America/New_York"


@pytest.fixture
def two_desks(desks_db):
    full = make_desk(desks_db, name="Full desk", capital=1000)                       # has the research agents (iris, vera, axel)
    small = make_desk(desks_db, name="Small desk", capital=1000, template="minimal")  # chief, data and risk only
    return desks_db, full["id"], small["id"]


def code_of(call):
    with pytest.raises(AgentsError) as err:
        call()
    return err.value.code


def job_rows(db):
    with db.read() as u:
        return u.scheduled_jobs.list()


def test_an_agent_of_the_desk_programs_a_run_with_an_iana_zone_and_it_lands_in_scheduled_jobs_off(two_desks):
    db, full, _ = two_desks
    out = schedule_desk_run(full, expr=EXPR, instruction="Review the overnight gap and write the plan.", who=Who(agent="iris"), db=db)
    (row,) = job_rows(db)
    assert (row.id, row.agent_slug, row.schedule_kind, row.schedule_expr, row.timezone) == (
        out["job_id"], "iris", "cron", "30 9 * * mon-fri", "America/New_York")
    assert row.is_active is False and row.approval_id is None and row.next_run_at is None       # the owner turns it on
    assert row.name == out["name"] and row.name.startswith(f"desk-{full}-run-") and len(row.name) <= 80
    assert "Review the overnight gap" in row.prompt and f"desk #{full}" in row.prompt and "Full desk" in row.prompt
    assert out["active"] is False and out["timezone"] == "America/New_York" and out["kind"] == "cron"
    assert datetime.strptime(out["first_run_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)


def test_the_owner_can_turn_it_on_with_the_normal_confirmation_and_the_next_run_follows_the_zone(two_desks):
    db, full, _ = two_desks
    out = schedule_desk_run(full, expr="every 6h", instruction="Check the book.", who=Who(agent="vera"), db=db)
    assert (out["kind"], out["expr"]) == ("interval", "6h")
    item = JobsService(db).activate(out["job_id"], confirmed=True)
    assert item["active"] and item["approved"] and item["next_run_at"]


def test_programming_for_another_desk_fails(two_desks):
    db, full, small = two_desks
    # iris and vera work on the full desk only: the small desk is not theirs, whatever they say
    assert code_of(lambda: schedule_desk_run(small, expr=EXPR, instruction="x", who=Who(agent="iris"), db=db)) == "not_desk_agent"
    assert code_of(lambda: schedule_desk_run(full, expr=EXPR, instruction="x", who=Who(agent="nobody"), db=db)) == "not_desk_agent"
    assert code_of(lambda: schedule_desk_run(full, expr=EXPR, instruction="x", who=Who(), db=db)) == "not_desk_agent"
    assert code_of(lambda: schedule_desk_run(999, expr=EXPR, instruction="x", who=Who(agent="iris"), db=db)) == "desk_not_found"
    assert job_rows(db) == []


def test_an_agent_that_is_off_on_the_desk_cannot_program_it(two_desks):
    db, full, _ = two_desks
    with db.write() as u:
        u.desks.set_agent(full, "iris", active=False)
    assert code_of(lambda: schedule_desk_run(full, expr=EXPR, instruction="x", who=Who(agent="iris"), db=db)) == "not_desk_agent"


def test_a_claimed_owner_without_the_secret_is_only_an_agent_and_the_real_owner_runs_the_chief(two_desks):
    db, full, _ = two_desks
    fake = Who(agent="nobody", owner=True)                       # owner=True without the start secret is dropped by Who itself
    assert fake.owner is False
    assert code_of(lambda: schedule_desk_run(full, expr=EXPR, instruction="x", who=fake, db=db)) == "not_desk_agent"
    out = schedule_desk_run(full, expr="daily 08:00", instruction="Morning plan.", who=owner_who(), db=db)
    assert out["agent"] == "desk-chief"


def test_the_zone_is_the_text_then_the_argument_then_the_owner_profile(two_desks):
    db, full, _ = two_desks
    who = Who(agent="iris")
    with db.read() as u:
        profile_zone = u.profile.get_or_default().timezone
    assert schedule_desk_run(full, expr="daily 08:00", instruction="a", who=who, db=db)["timezone"] == profile_zone
    assert schedule_desk_run(full, expr="daily 08:00", instruction="b", who=who, db=db, timezone="Europe/Madrid")["timezone"] == "Europe/Madrid"
    assert schedule_desk_run(full, expr="daily 08:00 tz=Asia/Tokyo", instruction="c", who=who, db=db, timezone="Europe/Madrid")["timezone"] == "Asia/Tokyo"


def test_invalid_schedules_and_instructions_are_refused_with_stable_codes_and_save_nothing(two_desks):
    db, full, _ = two_desks
    who = Who(agent="iris")
    cases = [("cron 61 * * * * tz=UTC", "x", "schedule_invalid"), ("cron * * * * * tz=UTC", "x", "interval_out_of_range"),
             ("daily 08:00 tz=Mars/Base", "x", "timezone_invalid"), ("weekly", "x", "schedule_invalid"), ("bogus 1", "x", "kind_invalid"),
             ("at 2001-01-01T00:00:00Z", "x", "run_in_past"), ("every 1m", "x", "interval_out_of_range"),
             ("daily 08:00", "", "prompt_required"), ("daily 08:00", "   ", "prompt_required"), ("daily 08:00", "x" * 5000, "prompt_too_long")]
    for expr, text, code in cases:
        assert code_of(lambda: schedule_desk_run(full, expr=expr, instruction=text, who=who, db=db)) == code, (expr, text)
    assert job_rows(db) == []


def test_the_same_run_twice_and_too_many_runs_are_refused(two_desks):
    db, full, small = two_desks
    who = Who(agent="iris")
    schedule_desk_run(full, expr="daily 08:00", instruction="same", who=who, db=db)
    assert code_of(lambda: schedule_desk_run(full, expr="daily 08:00", instruction="same", who=who, db=db)) == "job_name_taken"
    for n in range(jobs.MAX_DESK_RUNS - 1):
        schedule_desk_run(full, expr="daily 08:00", instruction=f"run {n}", who=who, db=db)
    assert code_of(lambda: schedule_desk_run(full, expr="daily 08:00", instruction="one more", who=who, db=db)) == "too_many_runs"
    schedule_desk_run(small, expr="daily 08:00", instruction="the other desk has its own count", who=Who(agent="desk-chief"), db=db)


def test_the_owner_sees_the_runs_of_that_desk_only(two_desks):
    db, full, small = two_desks
    a = schedule_desk_run(full, expr=EXPR, instruction="plan A", who=Who(agent="iris"), db=db)
    schedule_desk_run(small, expr="every 2h", instruction="plan B", who=Who(agent="desk-chief"), db=db)
    JobsService(db).activate(a["job_id"], confirmed=True)
    with db.write() as u:                                   # a job of another desk that only borrows the name: its agent is not a member
        u.scheduled_jobs.create(f"desk-{small}-run-spoof", "iris", "spoof", "daily", "08:00", "UTC")
    runs = list_desk_runs(full, db)
    assert [(r["job_id"], r["agent"], r["instruction"], r["kind"], r["expr"], r["timezone"], r["active"], r["approved"]) for r in runs] == [
        (a["job_id"], "iris", "plan A", "cron", "30 9 * * mon-fri", "America/New_York", True, True)]
    assert runs[0]["next_run_at"] and runs[0]["last_status"] is None
    assert [r["instruction"] for r in list_desk_runs(small, db)] == ["plan B"]
    assert list_desk_runs(12345, db) == []


def test_the_panel_create_route_stores_the_kinds_the_database_knows(db):
    service = JobsService(db)
    cron = service.create("Cron job", "hokma", "report", "cron", "0 9 * * mon-fri", "America/New_York")
    every = service.create("Every job", "hokma", "report", "every", "1h30m", "UTC")
    once = service.create("At job", "hokma", "report", "at", "2099-01-01T07:30:00Z", "UTC")
    assert (cron["kind"], cron["expr"]) == ("cron", "0 9 * * mon-fri")
    assert (every["kind"], every["expr"]) == ("interval", "90m") and (once["kind"], once["expr"]) == ("once", "2099-01-01T07:30:00Z")
    activated = service.activate(cron["id"], confirmed=True)
    assert activated["next_run_at"] and activated["timezone"] == "America/New_York"
    with pytest.raises(AgentsError) as err:
        service.create("Bad cron", "hokma", "report", "cron", "61 * * * *", "UTC")
    assert err.value.code == "schedule_invalid"

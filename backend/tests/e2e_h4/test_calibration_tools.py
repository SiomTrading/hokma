"""The tools of the calibration (SPEC-020, section 4.11): `premium_calibrate` (only the owner and the monthly routine) and `premium_switch_state` (read only), and the
monthly job. The caller is read from the environment of the engine, never from an argument."""
import json
from decimal import Decimal

import pytest
from backtest.calibration_support import fair_history, rich_history
from exec_support import db, desks_db, env, ready  # noqa: F401  (fixtures)

from hokma.backtest.calibration import premium
from hokma.exec.guards.premium_selling import switch_state
from hokma.kit import hokma_tools

OWNER = hokma_tools.Caller({})                                           # a session with no agent is the owner's


def who(agent, task=None):
    env = {"HOKMA_AGENT": agent}
    if task is not None:
        env["HOKMA_TASK_ID"] = str(task)
    return hokma_tools.Caller(env)


def call(name, args, caller):
    text, is_error = hokma_tools.call(name, args, caller)
    try:
        return json.loads(text), is_error
    except ValueError:
        return text, is_error


@pytest.fixture
def swo(ready, monkeypatch):
    with ready.db.write() as u:
        u.session.update("desk_profile", {"desk_id": ready.desk_id}, {"line": "SWO"})
        u.session.update("desk_limits", {"desk_id": ready.desk_id}, {"config": json.dumps({"max_drawdown_pct": 15})})     # the desk is born with its row (1.1.0)
    history = {"value": rich_history()}
    monkeypatch.setattr(premium, "default_source", lambda name, as_of: history["value"])
    ready.history = history
    return ready


def test_the_two_tools_are_published_with_a_strict_schema_and_no_argument_can_name_a_threshold():
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    for name in ("premium_calibrate", "premium_switch_state"):
        schema = listed[name]["inputSchema"]
        assert schema["additionalProperties"] is False and set(schema["properties"]) == {"desk_id", "underlying"} and schema["required"] == ["desk_id"]
        assert name in hokma_tools.NO_UNIT
    assert hokma_tools.TOOLS["premium_calibrate"][4] is True and hokma_tools.TOOLS["premium_switch_state"][4] is False
    for smuggled in ({"threshold": 1.0}, {"open": True}, {"status": "calibrated"}, {"evidence": {}}, {"gate": {}}, {"as_of": "2000-01-01"}, {"source": "x"}):
        text, is_error = hokma_tools.call("premium_calibrate", {"desk_id": 1, **smuggled}, OWNER)
        assert is_error and "unknown argument" in text


def test_an_agent_cannot_ask_for_a_calibration(swo):
    for agent in ("desk-chief", "joel", "strategy-researcher", "hokma"):
        answer, is_error = call("premium_calibrate", {"desk_id": swo.desk_id, "underlying": "SPY"}, who(agent, task=1))
        assert is_error and answer["error_code"] == "blocked", agent
    assert not switch_state(swo.desk_id, "SPY", db=swo.db).is_open


def test_the_owner_calibrates_by_the_tool_and_the_answer_comes_from_the_code(swo):
    answer, is_error = call("premium_calibrate", {"desk_id": swo.desk_id, "underlying": "spy"}, OWNER)
    assert not is_error
    (result,) = answer["results"]
    assert (result["status"], result["underlying"], result["metric"]) == ("calibrated", "SPY", "iv_rv_ratio") and result["reasons"] == []
    assert "walk_forward" not in result["measures"] and result["measures"]["oos_trades"] >= 30
    assert switch_state(swo.desk_id, "SPY", db=swo.db).is_open
    state, is_error = call("premium_switch_state", {"desk_id": swo.desk_id}, OWNER)
    assert not is_error and state["desk_id"] == swo.desk_id
    (row,) = state["switches"]
    assert (row["underlying"], row["state"], row["metric"], row["backtest_run_ref"]) == ("SPY", "open", "iv_rv_ratio", result["backtest_run_ref"])
    assert Decimal(row["threshold"]) == Decimal(result["threshold"])


def test_the_tool_measures_again_and_closes_a_switch_that_no_longer_passes(swo):
    call("premium_calibrate", {"desk_id": swo.desk_id, "underlying": "SPY"}, OWNER)
    swo.history["value"] = fair_history()
    answer, is_error = call("premium_calibrate", {"desk_id": swo.desk_id}, OWNER)                       # no underlying: every calibrated one
    assert not is_error and [(r["status"], r["retired_id"] is not None) for r in answer["results"]] == [("not_passed", True)]
    assert not switch_state(swo.desk_id, "SPY", db=swo.db).is_open
    closed, _ = call("premium_switch_state", {"desk_id": swo.desk_id, "underlying": "SPY"}, OWNER)
    assert closed["switches"][0]["state"] == "closed" and "closed" in closed["switches"][0]["text"]


def test_without_a_history_the_tool_says_data_missing_and_an_empty_desk_has_nothing_to_measure(swo, monkeypatch):
    monkeypatch.setattr(premium, "default_source", lambda name, as_of: None)
    answer, is_error = call("premium_calibrate", {"desk_id": swo.desk_id, "underlying": "SPY"}, OWNER)
    assert not is_error and answer["results"][0]["status"] == "data_missing" and answer["results"][0]["reasons"] == ["no_chain_history"]
    empty, is_error = call("premium_calibrate", {"desk_id": swo.desk_id}, OWNER)
    assert not is_error and empty == {"results": []}
    none, _ = call("premium_switch_state", {"desk_id": swo.desk_id}, OWNER)
    assert none["switches"] == []


def test_bad_arguments_are_classified(swo):
    text, is_error = call("premium_calibrate", {"desk_id": "x"}, OWNER)
    assert is_error and "desk_id must be integer" in text                                    # the schema refuses it before the tool runs
    for args in ({"desk_id": 0}, {"desk_id": swo.desk_id, "underlying": "../etc"}, {"desk_id": swo.desk_id, "underlying": ""}, {"desk_id": 99999, "underlying": "SPY"}):
        answer, is_error = call("premium_calibrate", args, OWNER)
        assert is_error and answer["error_code"] == "bad_input", args
    answer, is_error = call("premium_switch_state", {"desk_id": swo.desk_id, "underlying": "a b"}, OWNER)
    assert is_error and answer["error_code"] == "bad_input"


# ---- the monthly job --------------------------------------------------------------------------------------------------------------

def test_the_monthly_routine_is_created_off_once_and_never_edited(swo):
    job = premium.ensure_recalibration_job(db=swo.db)
    assert (job.name, job.agent_slug, job.schedule_kind, job.schedule_expr, job.is_active) == (premium.JOB_NAME, "hokma", "cron", "0 7 1 * *", False)
    assert "premium_calibrate" in job.prompt and "premium_switch_state" in job.prompt and "never" in job.prompt.lower()
    again = premium.ensure_recalibration_job(db=swo.db)
    assert again.id == job.id
    with swo.db.write() as u:
        u.scheduled_jobs.update(job.id, prompt="the owner edited it")
    assert premium.ensure_recalibration_job(db=swo.db).prompt == "the owner edited it"


def run_task(swo, job):
    """What the scheduler does when the job fires: a task of the agent of the job, claimed, and the job pointing at it."""
    from datetime import datetime, timezone

    from hokma.db import Actor

    with swo.db.write() as u:
        task = u.tasks.create(job.name, actor=Actor.system("scheduler"), body=job.prompt, assigned_agent=job.agent_slug, status="ready")
        u.tasks.claim(task.id, job.agent_slug, actor=Actor.system("scheduler"), run_model="x")
        u.scheduled_jobs.mark_ran(job.id, task_id=task.id, last_run_at=datetime.now(timezone.utc), next_run_at=None)
    return task.id


def finish(swo, task, how):
    from hokma.db import Actor

    with swo.db.write() as u:
        if how == "done":
            u.tasks.complete(task, "x", actor=Actor.system("test"))
        elif how == "failed":
            u.tasks.fail(task, "x")
        else:
            u.tasks.release(task, "x")                                                      # back to ready: not running any more


def routine_ready(swo):
    job = premium.ensure_recalibration_job(db=swo.db)
    task = run_task(swo, job)
    with swo.db.write() as u:
        u.scheduled_jobs.set_active(job.id, True)
    return job, task


def ask(swo, caller, underlying="SPY"):
    return call("premium_calibrate", {"desk_id": swo.desk_id, "underlying": underlying}, caller)


def test_the_owner_may_always_ask_and_the_routine_only_while_active(swo):
    job = premium.ensure_recalibration_job(db=swo.db)
    task = run_task(swo, job)
    assert premium.caller_may_calibrate(None, None, db=swo.db) is True
    assert premium.caller_may_calibrate("hokma", task, db=swo.db) is False                  # the routine is off
    with swo.db.write() as u:
        u.scheduled_jobs.set_active(job.id, True)
    assert premium.caller_may_calibrate("hokma", task, db=swo.db) is True
    assert premium.caller_may_calibrate("hokma", None, db=swo.db) is False


def denied(swo, agent, task, underlying="SPY"):
    with pytest.raises(premium.CalibrationDenied) as found:
        premium.authorize_calibration(agent, task, underlying, swo.desk_id, db=swo.db)
    return found.value.code


def test_an_old_task_id_is_refused(swo):
    job, task = routine_ready(swo)
    newer = run_task(swo, job)
    assert denied(swo, "hokma", task) == "wrong_task"
    answer, is_error = ask(swo, who("hokma", task))
    assert is_error and answer["error_code"] == "blocked"
    assert premium.caller_may_calibrate("hokma", newer, db=swo.db) is True


def test_a_different_agent_is_refused_even_with_the_right_task(swo):
    _, task = routine_ready(swo)
    assert denied(swo, "joel", task) == "wrong_agent"
    answer, is_error = ask(swo, who("joel", task))
    assert is_error and answer["error_code"] == "blocked"


def test_a_finished_task_is_refused(swo):
    job, task = routine_ready(swo)
    for status in ("done", "failed", "released"):
        task = task if status == "done" else run_task(swo, job)
        finish(swo, task, status)
        assert denied(swo, "hokma", task) == "task_not_running", status
    answer, is_error = ask(swo, who("hokma", task))
    assert is_error and answer["error_code"] == "blocked"


def test_a_first_calibration_from_the_routine_is_refused_and_writes_nothing(swo):
    _, task = routine_ready(swo)
    assert denied(swo, "hokma", task) == "first_calibration_by_routine"
    answer, is_error = ask(swo, who("hokma", task))
    assert is_error and answer["error_code"] == "blocked"
    assert not switch_state(swo.desk_id, "SPY", db=swo.db).is_open
    assert premium.calibrated_rows(swo.desk_id, db=swo.db) == []


def test_the_owner_first_calibration_and_the_routine_recalibration_still_pass(swo):
    answer, is_error = ask(swo, OWNER)
    assert not is_error and answer["results"][0]["status"] == "calibrated"
    _, task = routine_ready(swo)
    premium.authorize_calibration("hokma", task, "spy", swo.desk_id, db=swo.db)
    answer, is_error = ask(swo, who("hokma", task))
    assert not is_error and answer["results"][0]["status"] == "calibrated"
    answer, is_error = call("premium_calibrate", {"desk_id": swo.desk_id}, who("hokma", task))
    assert not is_error and answer["results"][0]["status"] == "calibrated"


def test_the_routine_cannot_reopen_an_underlying_that_was_retired(swo):
    ask(swo, OWNER)
    swo.history["value"] = fair_history()
    call("premium_calibrate", {"desk_id": swo.desk_id}, OWNER)                              # retires SPY
    assert not switch_state(swo.desk_id, "SPY", db=swo.db).is_open
    _, task = routine_ready(swo)
    assert denied(swo, "hokma", task) == "first_calibration_by_routine"

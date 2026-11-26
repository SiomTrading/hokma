"""The decision of a cycle, the issues, the health of the routines and the benchmark (SPEC-018: TA-48, OA-35, OA-34, TA-46)."""
from decimal import Decimal
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from backoffice_support import db, desks_db, env, ready  # noqa: F401
from exec_support import AGENT, proposal

from hokma import exec as hexec
from hokma.backoffice import benchmarks, decisions, health, issues, runs
from hokma.db import Actor
from hokma.exec import approvals, levels


def decision_row(r, decision_id):
    with r.db.read() as u:
        return u.decisions.get(decision_id)


# ----------------------------------------------------------------------------------------------------- TA-48

def test_the_decision_of_a_cycle_is_pending_and_linked_to_the_proposal_TA_48(ready):
    out = hexec.submit(proposal(ready), who=AGENT, db=ready.db)                            # level 1: analysis only, no order
    assert out.status == "analysis_only"
    decision_id = decisions.record(ready.desk_id, "Buy one NQ above 21000 if the open holds", proposal_id=out.proposal_id, db=ready.db)
    d = decision_row(ready, decision_id)
    assert d.status == "proposed" and d.topic.startswith(f"Desk {ready.desk_id} cycle:") and d.proposed_by == "desk-chief"
    assert json.loads(d.context)["proposal_id"] == out.proposal_id
    with ready.db.read() as u:
        assert u.session.one("SELECT decision_id FROM exec_proposals WHERE id = :i", {"i": out.proposal_id})["decision_id"] == decision_id
    assert decisions.record(ready.desk_id, "A second summary", proposal_id=out.proposal_id, db=ready.db) == decision_id     # one decision per proposal
    assert approvals.list_pending(ready.desk_id, db=ready.db) == []                         # a cycle decision is not a question for the owner
    assert decisions.resolve(out.proposal_id, db=ready.db) is None                          # analysis only never fills: it stays pending
    assert decision_row(ready, decision_id).status == "proposed"


def test_a_filled_order_resolves_the_decision_with_its_order_id_TA_48(ready):
    levels.set_level(ready.desk_id, 3, rules={"id": "r", "max_qty": 5, "order_types": ["market"]}, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready), who=AGENT, db=ready.db)
    assert out.status == "filled"
    decision_id = decisions.record(ready.desk_id, "Buy one NQ at the market", proposal_id=out.proposal_id, db=ready.db)
    assert decision_row(ready, decision_id).status == "proposed"
    resolution = decisions.resolve(out.proposal_id, db=ready.db)
    with ready.db.read() as u:
        order_id = u.session.one("SELECT o.broker_order_id AS i FROM exec_proposals p JOIN broker_orders o ON o.id = p.broker_order_record_id WHERE p.id = :i",
                                 {"i": out.proposal_id})["i"]
    assert resolution["order_id"] == order_id and order_id.startswith("S") and resolution["status"] == "filled"
    closed = decision_row(ready, decision_id)
    assert closed.status == "approved" and closed.decided_by == "hokma" and closed.options["resolution"] == resolution
    assert decisions.resolve(out.proposal_id, db=ready.db) == resolution                    # twice is the same
    with ready.db.read() as u:
        assert u.journal.count(kind="decision", search=f"Decision {decision_id} closed") == 1


def test_an_order_that_ends_without_a_fill_resolves_the_decision_as_rejected_TA_48(ready):
    levels.set_level(ready.desk_id, 3, rules={"id": "r", "max_qty": 5, "order_types": ["limit"]}, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready, order_type="limit", limit="20990", stop_loss=Decimal("20980")), who=AGENT, db=ready.db)
    assert out.status == "sent"
    decision_id = decisions.record(ready.desk_id, "Buy on a pullback", proposal_id=out.proposal_id, db=ready.db)
    assert decisions.resolve(out.proposal_id, db=ready.db) is None                          # still working: nothing to close
    with ready.db.write() as u:
        u.session.update("exec_proposals", {"id": out.proposal_id}, {"status": "expired"})
    resolution = decisions.resolve(out.proposal_id, db=ready.db)
    assert resolution["status"] == "expired" and resolution["order_id"]                     # the order id it carried is kept
    assert decision_row(ready, decision_id).status == "rejected"


def test_the_decision_of_an_owner_question_keeps_its_own_flow_TA_48(ready):
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready), who=AGENT, db=ready.db)
    assert out.status == "awaiting_owner" and out.decision_id
    assert decisions.record(ready.desk_id, "ignored", proposal_id=out.proposal_id, db=ready.db) == out.decision_id
    done = hexec.approve(out.proposal_id, who=ready.owner, db=ready.db)
    assert done.status == "filled" and decision_row(ready, out.decision_id).status == "approved"        # approved by the owner, not touched by the back office
    resolution = decisions.resolve(out.proposal_id, db=ready.db)
    assert resolution["status"] == "filled" and decision_row(ready, out.decision_id).decided_by == "owner" and decision_row(ready, out.decision_id).options["resolution"]


def test_a_decision_is_checked_TA_48(ready):
    other = hexec.submit(proposal(ready), who=AGENT, db=ready.db)
    with pytest.raises(decisions.DecisionError):
        decisions.record(ready.desk_id + 99, "x", proposal_id=other.proposal_id, db=ready.db)         # the proposal is not that desk's
    for summary in ("", "   ", "x" * 4001, None):
        with pytest.raises(decisions.DecisionError):
            decisions.record(ready.desk_id, summary, db=ready.db)
    free = decisions.record(ready.desk_id, "A cycle with no order", db=ready.db)                     # no proposal: only the pending decision
    assert decision_row(ready, free).status == "proposed" and decision_row(ready, free).context is None


# ----------------------------------------------------------------------------------------------------- OA-35

def make_break(r, bucket="quantity", key="EQ:AAPL", diff="5"):
    with r.db.write() as u:
        recon = u.session.insert("reconciliations", {"account_id": r.account_id, "as_of": "2026-03-12 12:00:00", "status": "breaks", "summary_json": "{}"})
        return u.session.insert("recon_breaks", {"recon_id": recon, "bucket": bucket, "key": key, "diff": diff, "cause": "mapping", "explanation": "the broker lists one more lot"})


def tasks_with(r, key):
    with r.db.read() as u:
        return [t for t in u.session.query("SELECT * FROM tasks ORDER BY id") if (t["body"] or "").startswith(f"issue-key: {key}\n")]


def test_a_material_break_makes_one_task_with_an_owner_and_a_date_and_never_two_OA_35(ready):
    break_id = make_break(ready)
    first = issues.for_break(break_id, owner="joel", due=date(2026, 3, 16), db=ready.db)
    assert first.created and first.key == f"recon-break:{ready.account_id}:quantity:EQ:AAPL"
    rows = tasks_with(ready, first.key)
    assert len(rows) == 1 and rows[0]["assigned_agent"] == "joel" and rows[0]["status"] == "ready" and rows[0]["id"] == first.task_id
    assert "due: 2026-03-16" in rows[0]["body"] and "owner: joel" in rows[0]["body"] and "diff: 5" in rows[0]["body"]
    again = issues.for_break(break_id, owner="joel", due=date(2026, 3, 16), db=ready.db)
    same_break_found_later = issues.for_break(make_break(ready), owner="vera", db=ready.db)         # the same account, bucket and key found by a later reconciliation
    assert again == issues.IssueResult(first.task_id, False, first.key) and same_break_found_later.task_id == first.task_id and not same_break_found_later.created
    assert len(tasks_with(ready, first.key)) == 1
    with ready.db.read() as u:
        link = u.session.one("SELECT task_id, owner, due_at FROM recon_breaks WHERE id = :i", {"i": break_id})
    assert link["task_id"] == first.task_id and link["owner"] == "joel" and link["due_at"].startswith("2026-03-16")


def test_a_break_that_is_not_material_makes_no_task_OA_35(ready):
    assert issues.for_break(make_break(ready, bucket="timing", key="EQ:T1", diff="100"), owner="joel", db=ready.db) is None
    assert issues.for_break(make_break(ready, bucket="amount", key="EQ:T2", diff="0.40"), owner="joel", db=ready.db) is None
    assert issues.for_break(make_break(ready, bucket="amount", key="EQ:T3", diff="-1.00"), owner="joel", db=ready.db).created
    assert issues.for_break(make_break(ready, bucket="one_side", key="EQ:T4", diff=None), owner="joel", db=ready.db).created
    assert issues.is_material("quantity") and not issues.is_material("timing", "999") and issues.is_material("amount", "1") and not issues.is_material("amount", "0.99")
    with ready.db.write() as u:
        u.session.execute("UPDATE recon_breaks SET action = 'suppress' WHERE key = 'EQ:T4'")
    assert issues.for_break(make_break(ready, bucket="one_side", key="EQ:T4"), owner="joel", db=ready.db).created is False     # already open; a suppressed break alone makes none
    with pytest.raises(issues.IssueError):
        issues.for_break(9999, owner="joel", db=ready.db)


def test_a_failed_run_makes_one_task_and_a_run_that_did_not_fail_makes_none_OA_35(ready):
    bad = runs.record(ready.desk_id, "cycle", signature="s", outputs={"error": "the engine stopped"}, status="failed", db=ready.db)
    good = runs.record(ready.desk_id, "cycle", signature="s", db=ready.db)
    assert issues.for_failed_run(good, owner="joel", db=ready.db) is None
    first = issues.for_failed_run(bad, owner="joel", due=date(2026, 3, 13), db=ready.db)
    second = issues.for_failed_run(bad, owner="joel", due=date(2026, 3, 20), db=ready.db)
    assert first.created and not second.created and first.task_id == second.task_id and first.key == f"run-failed:{bad}"
    row = tasks_with(ready, first.key)[0]
    assert row["assigned_agent"] == "joel" and "due: 2026-03-13" in row["body"] and "the engine stopped" in row["body"]
    with pytest.raises(issues.IssueError):
        issues.for_failed_run(99999, owner="joel", db=ready.db)


def test_an_issue_needs_a_known_owner_and_a_clean_key_and_keeps_no_secret_OA_35(ready):
    with pytest.raises(issues.IssueError):
        issues.open_issue(key="k-owner", title="t", detail="d", owner="nobody", db=ready.db)
    for key in ("", "ab", "x" * 161, "two\nlines"):
        with pytest.raises(issues.IssueError):
            issues.open_issue(key=key, title="t", detail="d", owner="joel", db=ready.db)
    made = issues.open_issue(key="secret-check", title="t", detail="call with token=SENTINEL-TOKEN-ABCDEF12 now", owner="joel", db=ready.db,
                             now=datetime(2026, 3, 12, tzinfo=timezone.utc))
    row = tasks_with(ready, "secret-check")[0]
    assert "SENTINEL-TOKEN-ABCDEF12" not in row["body"] and "due: 2026-03-14" in row["body"] and made.created        # two days when no date is given
    wild = issues.open_issue(key="a_b%c", title="t", detail="d", owner="joel", db=ready.db)
    other = issues.open_issue(key="aXbYc", title="t", detail="d", owner="joel", db=ready.db)
    assert wild.created and other.created and wild.task_id != other.task_id                  # LIKE wildcards in a key do not make two issues one


def test_a_closed_issue_can_be_raised_again_when_it_comes_back_OA_35(ready):
    first = issues.open_issue(key="closed-then-back", title="t", detail="d", owner="joel", db=ready.db)
    with ready.db.write() as u:
        u.tasks.transition(first.task_id, "archived", actor=Actor.human("owner"), reason="resolved")
    assert issues.open_issue(key="closed-then-back", title="t", detail="d", owner="joel", db=ready.db).created


# ----------------------------------------------------------------------------------------------------- OA-34

def job(r, name, *, active=True, approved=True, kind="interval", expr="1h"):
    with r.db.write() as u:
        made = u.scheduled_jobs.create(name, "hokma", "run the desk", kind, expr, "UTC")
        if active and approved:
            approval = u.approvals.record("scheduled_job", made.id, "approved", decided_by="owner", channel="dashboard")
            made = u.scheduled_jobs.set_active(made.id, True, approval_id=approval.id)
        elif active:
            made = u.scheduled_jobs.set_active(made.id, True)
        return made


def link_telegram(r):
    with r.db.write() as u:
        u.session.insert_row("telegram_link", {"id": 1, "chat_id": "123"})


def outbox(r):
    with r.db.read() as u:
        return [x["text"] for x in u.session.query("SELECT text FROM telegram_outbox ORDER BY id")]


def after(made, hours):
    from hokma.db import from_db_time

    return from_db_time(made.created_at) + timedelta(hours=hours, seconds=30)


def test_a_routine_that_is_on_and_missed_two_runs_raises_one_notice_OA_34(ready):
    link_telegram(ready)
    made = job(ready, f"desk-{ready.desk_id}-premarket")
    assert health.check(db=ready.db, now=after(made, 1.5))[0].state == "not_started"       # one run is due and is not two
    assert outbox(ready) == []
    states = health.check(db=ready.db, now=after(made, 3))
    assert [(s.name, s.state, s.missed, s.desk_id) for s in states] == [(made.name, "missed", 2, ready.desk_id)]
    sent = outbox(ready)
    assert len(sent) == 1 and made.name in sent[0] and "2 runs did not finish" in sent[0]
    health.check(db=ready.db, now=after(made, 4))                          # the same stretch of missed runs: no second notice
    assert len(outbox(ready)) == 1
    with ready.db.read() as u:
        alert = u.journal.list(kind="alert")[0]
    assert alert.outcome == "failure" and alert.refs["scheduled_job"] == made.id


def test_a_routine_that_is_off_does_not_notify_OA_34(ready):
    link_telegram(ready)
    made = job(ready, f"desk-{ready.desk_id}-weekly", active=False)
    out = health.check(db=ready.db, now=after(made, 10))
    assert [(s.state, s.missed) for s in out] == [("inactive", 0)] and outbox(ready) == []


def test_the_notice_goes_out_when_the_channel_exists_and_not_before_OA_34(ready):
    made = job(ready, f"desk-{ready.desk_id}-premarket")
    now = after(made, 3)
    assert health.check(db=ready.db, now=now)[0].state == "missed" and outbox(ready) == []
    with ready.db.read() as u:
        assert u.journal.list(kind="alert") == []                           # nothing was marked as told
    link_telegram(ready)
    health.check(db=ready.db, now=now)
    assert len(outbox(ready)) == 1


def test_a_run_that_finished_clears_the_stretch_and_a_new_one_notifies_again_OA_34(ready):
    link_telegram(ready)
    made = job(ready, f"desk-{ready.desk_id}-premarket")
    health.check(db=ready.db, now=after(made, 3))
    ran = after(made, 3)
    with ready.db.write() as u:
        u.scheduled_jobs.mark_ran(made.id, task_id=None, last_run_at=ran, next_run_at=ran + timedelta(hours=1))
    assert health.check(db=ready.db, now=ran + timedelta(minutes=5))[0].state == "healthy"
    assert health.check(db=ready.db, now=ran + timedelta(hours=3, minutes=1))[0].state == "missed"
    assert len(outbox(ready)) == 2


def test_only_routines_of_a_desk_that_exists_are_watched_OA_34(ready):
    link_telegram(ready)
    plain = job(ready, "Briefing")
    ghost = job(ready, "desk-9999-premarket")
    now = after(plain, 5)
    assert health.check(db=ready.db, now=now) == [] and outbox(ready) == [] and ghost.id
    assert health.desk_of("desk-12-premarket") == 12 and health.desk_of("desk-12") == 12 and health.desk_of("desk-x-1") is None and health.desk_of("desktop-3") is None


def test_the_states_of_a_routine_follow_the_live_stores_OA_34():
    now = datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc)

    def make(**kw):
        base = dict(id=1, name="desk-3-x", is_active=True, approval_id=5, schedule_kind="interval", schedule_expr="1h", timezone="UTC",
                    last_run_at="2026-03-12 11:30:00", created_at="2026-03-01 00:00:00", next_run_at="2026-03-12 12:30:00")
        return SimpleNamespace(**{**base, **kw})

    assert health.project(make(is_active=False), now).state == "inactive"
    assert health.project(make(approval_id=None), now).state == "blocked"
    assert health.project(make(), now, last_task_status="running").state == "running"
    assert health.project(make(), now).state == "healthy"
    assert health.project(make(last_run_at=None, next_run_at="2026-03-12 11:00:00", created_at="2026-03-12 11:30:00"), now).state == "due"
    assert health.project(make(), now, last_task_status="failed").state == "failed"
    assert health.project(make(last_run_at="2026-03-12 08:30:00"), now).state == "missed"
    assert health.project(make(schedule_kind="once", schedule_expr="2026-03-01 00:00", last_run_at=None), now).missed == 0      # a one time job cannot miss two runs
    assert health.project(make(name="Briefing"), now) is None


# ----------------------------------------------------------------------------------------------------- TA-46

def set_benchmark(r, value):
    with r.db.write() as u:
        u.session.execute("UPDATE desk_profile SET benchmark = :b WHERE desk_id = :d", {"b": value, "d": r.desk_id})


DESK = [("2026-03-02", "100000"), ("2026-03-09", "101500"), ("2026-03-16", "103000")]
INDEX = [("2026-03-02", "500"), ("2026-03-09", "505"), ("2026-03-16", "510")]


def test_a_desk_with_no_declared_benchmark_gets_no_relative_report_and_it_says_so_TA_46(ready):
    assert benchmarks.declared(ready.desk_id, db=ready.db) is None
    out = benchmarks.relative_report(ready.desk_id, DESK, INDEX, db=ready.db)
    assert out.status == "no_benchmark" and "declares no benchmark" in out.message and out.excess_return is None and out.desk_return is None
    assert out.to_dict()["excess_return"] is None
    assert benchmarks.declared(ready.desk_id + 50, db=ready.db) is None                      # no profile at all


def test_a_declared_benchmark_gives_the_relative_return_TA_46(ready):
    set_benchmark(ready, "SPY")
    assert benchmarks.declared(ready.desk_id, db=ready.db) == "SPY"
    out = benchmarks.relative_report(ready.desk_id, DESK, INDEX, db=ready.db)
    assert out.status == "ok" and out.benchmark == "SPY" and out.points == 3
    assert (out.desk_return, out.benchmark_return, out.excess_return) == (D("0.03"), D("0.02"), D("0.01"))        # 103000 / 100000 - 1, 510 / 500 - 1
    assert out.to_dict()["excess_return"] == "0.01"
    few = benchmarks.relative_report(ready.desk_id, DESK[:1], INDEX[:1], db=ready.db)
    assert few.status == "no_data" and few.excess_return is None
    shifted = benchmarks.relative_report(ready.desk_id, DESK, [("2026-03-02", "500"), ("2026-03-23", "520")], db=ready.db)
    assert shifted.status == "no_data" and shifted.points == 1                              # only the dates both series share count
    with pytest.raises(ValueError):
        benchmarks.relative_report(ready.desk_id, [("2026-03-02", "0"), ("2026-03-09", "1")], INDEX, db=ready.db)


@pytest.mark.parametrize("value", ["", "  ", "../etc", "a b", "x" * 41])
def test_a_benchmark_that_is_not_a_plain_symbol_is_not_believed_TA_46(ready, value):
    try:
        set_benchmark(ready, value)
    except Exception:
        return                                                                              # the table refuses it already
    assert benchmarks.declared(ready.desk_id, db=ready.db) is None

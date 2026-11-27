"""The trigger cases of the data model, run against the real engine, plus the bug rules."""
import pytest
from dbsupport import HUMAN, SYSTEM, agent, db, env  # noqa: F401  (fixtures)

from hokma.db import Actor, IntegrityViolation, RuleViolation, ValidationError


def new_task(u, title="t", **kw):
    kw.setdefault("actor", HUMAN)
    kw.setdefault("assigned_agent", "joel")
    return u.tasks.create(title, **kw)


def force(u, task_id, sql_set: str, **params):
    """Direct SQL, as a hostile or buggy writer would do."""
    u.session.execute(f"UPDATE tasks SET {sql_set} WHERE id = :id", {"id": task_id, **params})


def run_to_running(u, task):
    return u.tasks.claim(task.id, "joel", run_model="sonnet")


def test_agent_closes_a_task_without_review_and_it_passes(db):
    with db.write() as u:
        t = run_to_running(u, new_task(u))
        done = u.tasks.complete(t.id, "all good", actor=agent("joel"))
        assert done.status == "done" and done.completed_at and done.result_summary == "all good"


def test_an_agent_closes_a_task_that_has_review_configured_with_no_review(db):
    with db.write() as u:
        t = run_to_running(u, new_task(u, requires_review=True, reviewer_agent="vera"))
        closed = u.tasks.move(t.id, "done", actor=agent("joel"), reason="not worth a review")          # 1.2.9: the review is a path, not a lock
        assert closed.status == "done" and closed.result_summary == "not worth a review" and u.task_runs.current(t.id) is None


def test_any_agent_can_give_the_review_and_the_implementer_too(db):
    with db.write() as u:
        t = new_task(u, requires_review=True, reviewer_agent="vera")
        t = run_to_running(u, t)
        u.tasks.request_review(t.id, "ready for review", actor=agent("joel"))
        assert u.tasks.review_approve(t.id, actor=agent("joel")).status == "done"          # 1.2.9: nobody checks who gives the review
        again = u.tasks.move(t.id, "review", actor=agent("iris"))
        assert u.tasks.review_approve(again.id, actor=agent("iris")).status == "done"


def test_the_reviewer_can_claim_the_task_and_does_not_become_its_implementer(db):
    with db.write() as u:
        t = new_task(u, requires_review=True, reviewer_agent="vera", assigned_agent="iris")
        claimed = u.tasks.claim(t.id, "vera")
        assert claimed.status == "running" and claimed.claimed_by == "vera" and claimed.implementer_agent is None and claimed.assigned_agent == "vera"


def test_real_money_task_cannot_be_closed_by_an_agent(db):
    with db.write() as u:
        t = new_task(u, touches_real_money=True)
        assert t.requires_approval is True                           # the CHECK-backed rule is applied on creation
        t = run_to_running(u, t)
        with pytest.raises(RuleViolation):
            force(u, t.id, "status = 'done', result_summary = 'x', completed_at = '2026-01-01 00:00:00', "
                           "claim_token = NULL, changed_by_kind = 'agent', changed_by = 'joel'")
        waiting = u.tasks.complete(t.id, "done, awaiting you", actor=agent("joel"))
        assert waiting.status == "awaiting_approval"
        with pytest.raises(RuleViolation):                           # an agent cannot approve it either
            force(u, t.id, "status = 'done', completed_at = '2026-01-01 00:00:00', changed_by_kind = 'agent', "
                           "changed_by = 'joel'")


def test_done_by_a_person_needs_an_approval_dated_after_the_wait_began(db):
    with db.write() as u:
        t = run_to_running(u, new_task(u, requires_approval=True))
        u.tasks.complete(t.id, "ready", actor=agent("joel"))
        with pytest.raises(RuleViolation):                           # a person, but no approval row yet
            force(u, t.id, "status = 'done', completed_at = '2026-01-01 00:00:00', changed_by_kind = 'human', "
                           "changed_by = 'owner'")
        approved = u.tasks.approve(t.id, decided_by="owner", channel="cli")
        assert approved.status == "done"
        assert u.approvals.latest_for("task", t.id).decided_by_kind == "human"


def test_an_old_approval_does_not_count_for_a_new_wait(db):
    with db.write() as u:
        t = run_to_running(u, new_task(u, requires_approval=True))
        u.tasks.complete(t.id, "first", actor=agent("joel"))
        u.tasks.approve(t.id, decided_by="owner")
        u.tasks.reopen(t.id, actor=HUMAN)
        run_to_running(u, u.tasks.get(t.id))
        u.tasks.complete(t.id, "second", actor=agent("joel"))
        u.session.execute("UPDATE tasks SET updated_at = '2999-01-01 00:00:00' WHERE id = :i", {"i": t.id})
        with pytest.raises(RuleViolation):                           # the earlier approval predates this wait
            force(u, t.id, "status = 'done', completed_at = '2026-01-01 00:00:00', changed_by_kind = 'human', "
                           "changed_by = 'owner'")


def test_agent_cannot_lower_requires_approval_or_the_money_lock_but_can_lower_review(db):
    with db.write() as u:
        t = new_task(u, touches_real_money=True, requires_review=True, reviewer_agent="vera")
        for sql in ("requires_approval = 0", "touches_real_money = 0"):
            with pytest.raises((RuleViolation, IntegrityViolation)):
                force(u, t.id, f"{sql}, changed_by_kind = 'agent', changed_by = 'joel'")
        plain = new_task(u, requires_approval=True)
        with pytest.raises(RuleViolation):
            force(u, plain.id, "requires_approval = 0, changed_by_kind = 'agent', changed_by = 'joel'")
        assert u.tasks.update(plain.id, actor=HUMAN, requires_approval=False).requires_approval is False
        assert u.tasks.update(t.id, actor=agent("joel"), requires_review=False).requires_review is False          # the review is a path: an agent may take it off


def test_repo_update_by_an_agent_can_raise_but_not_lower(db):
    with db.write() as u:
        t = new_task(u)
        raised = u.tasks.update(t.id, actor=agent("joel"), requires_approval=True)
        assert raised.requires_approval is True
        with pytest.raises(RuleViolation):
            u.tasks.update(t.id, actor=agent("joel"), requires_approval=False)


def test_unfinished_parent_does_not_block_a_move_or_a_claim(db):
    with db.write() as u:
        parent = new_task(u, "parent")
        child = new_task(u, "child", parents=[parent.id])
        assert child.status == "todo"
        assert u.tasks.transition(child.id, "ready", actor=HUMAN).status == "ready"
        assert u.tasks.claim(child.id, "joel").status == "running"          # 1.2.9: the dispatcher still waits for the parents; an agent that takes the task does not


def test_counters_are_data_not_a_lock(db):
    with db.write() as u:
        t = run_to_running(u, new_task(u))
        force(u, t.id, "block_count = 1, review_cycles = 1")
        force(u, t.id, "block_count = 0, review_cycles = 0")
        assert u.tasks.complete(t.id, "ok", actor=agent("joel")).block_count == 0


@pytest.mark.parametrize("table,sets", [
    ("journal_entries", "title = 'changed'"), ("approvals", "note = 'changed'"), ("task_events", "reason = 'x'"),
    ("guard_events", "reason = 'x'"), ("task_comments", "body = 'x'")])
def test_append_only_tables_refuse_update_and_delete(db, table, sets):
    with db.write() as u:
        t = new_task(u)
        u.journal.add("note", "hello", actor=HUMAN)
        u.approvals.record("task", t.id, "approved", decided_by="owner")
        u.guard_events.record("broker", "blocked", "no orders")
        u.task_comments.add(t.id, "a note", actor=HUMAN)
    for statement in (f"UPDATE {table} SET {sets}", f"DELETE FROM {table}"):
        with db.write() as u:
            with pytest.raises(RuleViolation):
                u.session.execute(statement)


def test_approvals_only_accept_a_human(db):
    with db.write() as u:
        with pytest.raises(IntegrityViolation):
            u.session.execute("INSERT INTO approvals (subject_type, subject_id, verdict, decided_by_kind, decided_by, "
                              "channel) VALUES ('task', 1, 'approved', 'agent', 'joel', 'cli')")


def test_an_agent_moves_a_task_to_any_state_and_the_history_records_it(db):
    with db.write() as u:
        t = new_task(u)
        for target in ("done", "review", "failed", "triage", "todo", "blocked", "archived", "ready"):
            assert u.tasks.move(t.id, target, actor=agent("joel"), reason="because").status == target
        assert [e.to_status for e in u.task_events.list(t.id)][-3:] == ["blocked", "archived", "ready"]
        with pytest.raises(ValidationError):
            u.tasks.move(t.id, "running", actor=agent("joel"))          # running comes from a claim
        money = new_task(u, touches_real_money=True)
        with pytest.raises(RuleViolation):                               # the lock that stays: an agent does not close what needs the owner
            u.tasks.move(money.id, "done", actor=agent("joel"))
        assert u.tasks.move(money.id, "awaiting_approval", actor=agent("joel")).status == "awaiting_approval"


def test_task_history_is_written_by_the_triggers(db):
    with db.write() as u:
        t = run_to_running(u, new_task(u))
        u.tasks.complete(t.id, "ok", actor=agent("joel"))
        events = [(e.from_status, e.to_status, e.actor_kind, e.actor) for e in u.task_events.list(t.id)]
        assert events == [(None, "ready", "human", "owner"), ("ready", "running", "agent", "joel"),
                          ("running", "done", "agent", "joel")]


# ---------------------------------------------------------------------------------------------- bugs


def fixed_bug(u, fixer: Actor):
    task = new_task(u)
    bug = u.bugs.create("it breaks", actor=HUMAN, severity="high")
    u.bugs.transition(bug.id, "in_progress", actor=fixer)
    return u.bugs.transition(bug.id, "fixed", actor=fixer, fixed_by_task_id=task.id)


def test_a_bug_can_be_verified_by_who_fixed_it(db):
    with db.write() as u:
        bug = fixed_bug(u, agent("joel"))
        verified = u.bugs.transition(bug.id, "verified", actor=agent("joel"))          # 1.2.9: no one checks who verifies
        assert verified.status == "verified"
        closed = u.bugs.transition(bug.id, "closed", actor=HUMAN)
        assert closed.closed_at is not None
        assert u.bugs.transition(bug.id, "open", actor=HUMAN).closed_at is None


@pytest.mark.parametrize("path,final", [
    (["wont_fix"], "wont_fix"), (["in_progress", "open"], "open"), (["in_progress", "wont_fix"], "wont_fix")])
def test_allowed_bug_paths(db, path, final):
    with db.write() as u:
        bug = u.bugs.create("b", actor=HUMAN)
        for step in path:
            bug = u.bugs.transition(bug.id, step, actor=HUMAN)
        assert bug.status == final


@pytest.mark.parametrize("target", ["fixed", "verified", "closed", "in_progress", "wont_fix"])
def test_a_bug_goes_from_open_to_any_state_with_no_task(db, target):
    with db.write() as u:
        bug = u.bugs.create("b", actor=HUMAN)
        assert u.bugs.transition(bug.id, target, actor=agent("hokma")).status == target          # the case of 1.2.8: an agent closing bugs #1 to #5 from open


def test_fixed_can_name_the_task_that_fixed_it_but_does_not_need_it(db):
    with db.write() as u:
        bug = u.bugs.create("b", actor=HUMAN)
        task = new_task(u)
        assert u.bugs.transition(bug.id, "fixed", actor=HUMAN, fixed_by_task_id=task.id).fixed_by_task_id == task.id

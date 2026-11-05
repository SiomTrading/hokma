"""Task lifecycle through the repositories: dispatch, claim, run, review, approval, block, release, links."""
import threading

import pytest
from dbsupport import HUMAN, SYSTEM, agent, db, env  # noqa: F401  (fixtures)

from hokma.db import Actor, Conflict, NotFound, RuleViolation, ValidationError


def make(u, title="t", **kw):
    kw.setdefault("actor", HUMAN)
    kw.setdefault("assigned_agent", "joel")
    return u.tasks.create(title, **kw)


def test_create_validations_and_defaults(db):
    with db.write() as u:
        t = make(u, "  Plain  ", body="b", priority=2)
        assert (t.title, t.status, t.priority, t.created_by_kind, t.created_by) == ("Plain", "ready", 2, "human", "owner")
        assert t.max_runtime_seconds == 1800 and t.max_attempts == 2 and t.attempts == 0
        assert make(u, actor=SYSTEM).created_by_kind == "scheduler"
        assert make(u, actor=agent("iris")).created_by_kind == "agent"
        for bad in ({"title": ""}, {"title": "x" * 201}, {"priority": 3}, {"status": "running"}, {"body": "x" * 8001},
                    {"requires_review": True}, {"requires_review": True, "reviewer_agent": "joel"}):
            kw = {"title": "ok", **bad}
            with pytest.raises(ValidationError):
                u.tasks.create(kw.pop("title"), actor=HUMAN, assigned_agent="joel", **kw)
        with pytest.raises(NotFound):
            make(u, parents=[999])
        with pytest.raises(NotFound):
            make(u, project_id=999)
        assert make(u, status="triage").status == "triage"


def test_future_run_at_and_open_parents_park_the_task_in_todo(db):
    with db.write() as u:
        later = make(u, run_at="2999-01-01T00:00:00Z")
        assert later.status == "todo" and later.run_at == "2999-01-01 00:00:00"
        past = make(u, run_at="2000-01-01 00:00:00")
        assert past.status == "ready"
        parent = make(u)
        child = make(u, parents=[parent.id, parent.id])
        assert child.status == "todo" and child.parent_ids == (parent.id,)


def test_promote_due_follows_parents_and_time(db):
    with db.write() as u:
        parent = make(u, "parent")
        child = make(u, "child", parents=[parent.id])
        timed = make(u, "timed", run_at="2999-01-01 00:00:00")
        assert u.tasks.promote_due() == []
        t = u.tasks.claim(parent.id, "joel")
        u.tasks.complete(t.id, "done", actor=agent("joel"))
        assert u.tasks.promote_due() == [child.id]
        assert u.tasks.get(child.id).status == "ready"
        assert u.tasks.promote_due(now="3000-01-01 00:00:00") == [timed.id]


def test_dispatch_order_priority_then_age_and_only_active_agents(db):
    with db.write() as u:
        low = make(u, "low", priority=-1)
        a = make(u, "a", priority=1)
        b = make(u, "b", priority=1)
        top = make(u, "top", priority=2, assigned_agent="iris")
        off = make(u, "off", assigned_agent="axel")                 # axel is not active
        make(u, "nobody", assigned_agent=None)
        order = [t.title for t in u.tasks.list_ready()]
        assert order == ["top", "a", "b", "low"]
        assert [t.title for t in u.tasks.list_ready(only_active=False)][:2] == ["top", "a"]
        assert "off" not in order and off.id not in [t.id for t in u.tasks.list_ready()]
        assert [t.title for t in u.tasks.list_ready(agents=["joel"])] == ["a", "b", "low"]
        assert u.tasks.list_ready(agents=[]) == []
        assert u.tasks.list_ready(limit=1)[0].id == top.id


def test_claim_is_conditional(db):
    with db.write() as u:
        t = make(u)
        claimed = u.tasks.claim(t.id, "joel", run_model="sonnet")
        assert claimed.status == "running" and claimed.claim_token and claimed.attempts == 1
        assert claimed.implementer_agent == "joel" and claimed.claimed_by == "joel"
        run = u.task_runs.current(t.id)
        assert run.agent_slug == "joel" and run.outcome == "running" and run.role == "implement"
        with pytest.raises(Conflict):
            u.tasks.claim(t.id, "iris")
        parent = make(u, "p")
        child = make(u, "c", parents=[parent.id])
        with pytest.raises(Conflict):
            u.tasks.claim(child.id, "joel")                          # it is todo, not ready


def test_claim_next_skips_blocked_candidates(db):
    with db.write() as u:
        a = make(u, "first", priority=2)
        b = make(u, "second")
        got = u.tasks.claim_next("joel")
        assert got.id == a.id
        assert u.tasks.claim_next("joel").id == b.id
        assert u.tasks.claim_next("joel") is None
        assert u.tasks.claim_next("iris") is None


def test_two_writers_cannot_claim_the_same_task(db):
    with db.write() as u:
        task = make(u)
    winners, losers = [], []

    def worker(slug):
        try:
            with db.write() as u:
                u.tasks.claim(task.id, slug)
            winners.append(slug)
        except Conflict:
            losers.append(slug)

    threads = [threading.Thread(target=worker, args=(s,)) for s in ("joel", "iris", "vera", "hokma")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(winners) == 1 and len(losers) == 3


def test_heartbeat_renews_task_and_run(db):
    with db.write() as u:
        t = u.tasks.claim(make(u).id, "joel", run_model="sonnet")
        u.session.execute("UPDATE tasks SET heartbeat_at = '2000-01-01 00:00:00' WHERE id = :i", {"i": t.id})
        u.session.execute("UPDATE task_runs SET heartbeat_at = '2000-01-01 00:00:00' WHERE task_id = :i", {"i": t.id})
        u.tasks.heartbeat(t.id, claim_token=t.claim_token)
        assert u.tasks.get(t.id).heartbeat_at > "2020"
        assert u.task_runs.current(t.id).heartbeat_at > "2020"
        with pytest.raises(Conflict):
            u.tasks.heartbeat(t.id, claim_token="wrong")
        idle = make(u, "idle")
        with pytest.raises(Conflict):
            u.tasks.heartbeat(idle.id)


def test_full_flow_with_review_and_approval(db):
    with db.write() as u:
        t = make(u, requires_review=True, reviewer_agent="vera", requires_approval=True)
        t = u.tasks.claim(t.id, "joel", run_model="sonnet")
        t = u.tasks.complete(t.id, "built", actor=agent("joel"), metadata={"files": 2})
        assert t.status == "review" and t.result_metadata == {"files": 2} and t.claim_token is None
        assert [r.outcome for r in u.task_runs.list(t.id)] == ["review_requested"]
        assert [x.id for x in u.tasks.list_reviewable()] == [t.id]
        t = u.tasks.claim_review(t.id, "vera", run_model="opus")
        assert u.task_runs.current(t.id).role == "review" and u.tasks.list_reviewable() == []
        with pytest.raises(Conflict):
            u.tasks.claim_review(t.id, "vera")
        t = u.tasks.review_approve(t.id, actor=agent("vera"), note="looks right")
        assert t.status == "awaiting_approval"
        assert [r.outcome for r in u.task_runs.list(t.id)] == ["review_requested", "approved_by_reviewer"]
        t = u.tasks.approve(t.id, decided_by="owner", channel="telegram", note="go")
        assert t.status == "done" and t.completed_at
        assert u.approvals.latest_for("task", t.id).channel == "telegram"
        detail = u.tasks.detail(t.id)
        assert [e.to_status for e in detail.events] == ["ready", "running", "review", "awaiting_approval", "done"]
        assert [c.body for c in detail.comments] == ["looks right"]


def test_request_changes_counts_cycles_then_goes_to_triage(db):
    with db.write() as u:
        t = make(u, requires_review=True, reviewer_agent="vera")
        for cycle in (1, 2):
            t = u.tasks.claim(t.id, "joel", run_model="sonnet")
            u.tasks.complete(t.id, f"attempt {cycle}", actor=agent("joel"))
            t = u.tasks.request_changes(t.id, "fix it", actor=agent("vera"))
            assert t.status == "ready" and t.review_cycles == cycle
        t = u.tasks.claim(t.id, "joel")
        u.tasks.complete(t.id, "attempt 3", actor=agent("joel"))
        t = u.tasks.request_changes(t.id, "still wrong", actor=agent("vera"))
        assert t.status == "triage" and "review limit" in t.change_reason
        with pytest.raises(ValidationError):
            u.tasks.request_changes(t.id, " ", actor=agent("vera"))


def test_a_person_reviewing_counts_as_the_approval(db):
    with db.write() as u:
        t = make(u, requires_review=True, reviewer_agent="vera", requires_approval=True)
        t = u.tasks.claim(t.id, "joel")
        u.tasks.complete(t.id, "x", actor=agent("joel"))
        done = u.tasks.review_approve(t.id, actor=HUMAN)
        assert done.status == "done" and u.approvals.latest_for("task", t.id).verdict == "approved"


def test_reject_goes_back_to_ready_or_to_the_archive(db):
    with db.write() as u:
        a = u.tasks.claim(make(u, requires_approval=True).id, "joel")
        u.tasks.complete(a.id, "x", actor=agent("joel"))
        assert u.tasks.reject(a.id, decided_by="owner", note="redo").status == "ready"
        assert u.approvals.latest_for("task", a.id).verdict == "changes_requested"
        a = u.tasks.claim(a.id, "joel")
        u.tasks.complete(a.id, "again", actor=agent("joel"))
        assert u.tasks.reject(a.id, decided_by="owner", archive=True).status == "archived"
        with pytest.raises(RuleViolation):
            u.tasks.approve(a.id, decided_by="owner")


def test_block_unblock_and_the_block_limit(db):
    with db.write() as u:
        t = u.tasks.claim(make(u).id, "joel", run_model="sonnet")
        b = u.tasks.block(t.id, "needs_input", "which broker?", actor=agent("joel"))
        assert b.status == "blocked" and b.block_kind == "needs_input" and b.block_count == 1
        assert u.task_runs.list(t.id)[-1].outcome == "blocked"
        r = u.tasks.unblock(t.id, actor=HUMAN, reason="answered")
        assert r.status == "ready" and r.block_kind is None and r.block_reason is None
        t = u.tasks.claim(t.id, "joel")
        assert u.tasks.block(t.id, "transient", "net down", actor=agent("joel")).block_count == 2
        u.tasks.unblock(t.id, actor=HUMAN)
        t = u.tasks.claim(t.id, "joel")
        triaged = u.tasks.block(t.id, "transient", "again", actor=agent("joel"))
        assert triaged.status == "triage" and "block limit" in triaged.change_reason
        with pytest.raises(ValidationError):
            u.tasks.block(t.id, "nonsense", "x", actor=agent("joel"))
        with pytest.raises(ValidationError):
            u.tasks.block(t.id, "transient", "  ", actor=agent("joel"))


def test_release_does_not_count_unless_asked_and_fail_after_the_attempts(db):
    with db.write() as u:
        t = u.tasks.claim(make(u).id, "joel", run_model="sonnet")
        released = u.tasks.release(t.id, "plan limit reached")
        assert released.status == "ready" and released.attempts == 0 and released.claim_token is None
        assert u.task_runs.list(t.id)[-1].outcome == "released"
        t = u.tasks.claim(t.id, "joel", run_model="sonnet")
        crashed = u.tasks.release(t.id, "process died", count_attempt=True, outcome="crashed")
        assert crashed.status == "ready" and crashed.attempts == 1
        t = u.tasks.claim(t.id, "joel", run_model="sonnet")
        timed = u.tasks.release(t.id, "ran too long", count_attempt=True, outcome="timed_out")
        assert timed.status == "failed" and timed.attempts == 2 and timed.completed_at
        assert [r.outcome for r in u.task_runs.list(t.id)] == ["released", "crashed", "timed_out"]
        with pytest.raises(RuleViolation):
            u.tasks.release(t.id, "not running")
        with pytest.raises(ValidationError):
            u.tasks.release(make(u).id, "x", outcome="completed")


def test_fail_and_reopen_reset_the_attempts(db):
    with db.write() as u:
        t = u.tasks.claim(make(u).id, "joel", run_model="sonnet")
        failed = u.tasks.fail(t.id, "broke badly")
        assert failed.status == "failed" and u.task_runs.list(t.id)[-1].outcome == "failure"
        reopened = u.tasks.reopen(t.id, actor=HUMAN, reason="try again")
        assert reopened.status == "ready" and reopened.attempts == 0 and reopened.completed_at is None


def test_reopening_a_done_task_sends_unfinished_children_back_to_todo(db):
    with db.write() as u:
        parent = u.tasks.claim(make(u, "parent").id, "joel")
        u.tasks.complete(parent.id, "ok", actor=agent("joel"))
        child = make(u, "child", parents=[parent.id])
        u.tasks.promote_due()
        assert u.tasks.get(child.id).status == "ready"
        u.tasks.reopen(parent.id, actor=HUMAN)
        assert u.tasks.get(child.id).status == "todo"


def test_archiving_an_unfinished_parent_sends_children_to_triage(db):
    with db.write() as u:
        parent = make(u, "parent")
        child = make(u, "child", parents=[parent.id])
        grandchild = make(u, "grandchild", parents=[child.id])
        u.tasks.archive(parent.id, actor=HUMAN, reason="not needed")
        assert u.tasks.get(child.id).status == "triage"
        assert u.tasks.get(grandchild.id).status == "todo"          # waits behind the triaged child
        done = u.tasks.claim(make(u, "finished").id, "joel")
        u.tasks.complete(done.id, "ok", actor=agent("joel"))
        keep = make(u, "kept", parents=[done.id])
        u.tasks.archive(done.id, actor=HUMAN)
        assert u.tasks.get(keep.id).status == "ready"               # a finished parent still released its child


def test_links_reject_cycles_and_park_ready_children(db):
    with db.write() as u:
        a, b, c = make(u, "a"), make(u, "b"), make(u, "c")
        u.task_links.add(a.id, b.id)
        u.task_links.add(a.id, b.id)                                  # idempotent
        u.task_links.add(b.id, c.id)
        assert u.tasks.get(b.id).status == "todo" and u.tasks.get(c.id).status == "todo"
        assert u.task_links.parents(c.id) == [b.id] and u.task_links.children(a.id) == [b.id]
        for parent, child in ((c.id, a.id), (b.id, a.id), (a.id, a.id)):
            with pytest.raises(ValidationError):
                u.task_links.add(parent, child)
        with pytest.raises(NotFound):
            u.task_links.add(a.id, 999)
        u.task_links.remove(a.id, b.id)
        assert u.task_links.parents(b.id) == []


def test_update_edit_rules(db):
    with db.write() as u:
        t = make(u)
        e = u.tasks.update(t.id, actor=HUMAN, title="New", body="B", priority=-2, assigned_agent="iris",
                           run_at="2999-01-01T00:00:00Z", max_runtime_seconds=3600)
        assert (e.title, e.assigned_agent, e.run_at, e.max_runtime_seconds, e.changed_by_kind) == (
            "New", "iris", "2999-01-01 00:00:00", 3600, "human")
        assert u.tasks.update(t.id, actor=HUMAN) == e
        assert u.tasks.update(t.id, actor=HUMAN, assigned_agent=None).assigned_agent is None
        with pytest.raises(ValidationError):
            u.tasks.update(t.id, actor=HUMAN, title=" ")
        with pytest.raises(ValidationError):
            u.tasks.update(t.id, actor=HUMAN, requires_review=True)
        assert u.tasks.update(t.id, actor=HUMAN, requires_review=True, reviewer_agent="vera").requires_review


def test_list_filters_and_running(db):
    with db.write() as u:
        p = u.projects.create("P", "/p")
        a = make(u, "a", project_id=p.id)
        b = make(u, "b", assigned_agent="iris")
        make(u, "c", status="triage")
        u.tasks.claim(a.id, "joel")
        assert [t.id for t in u.tasks.running()] == [a.id]
        assert [t.id for t in u.tasks.list(project_id=p.id)] == [a.id]
        assert [t.id for t in u.tasks.list(assigned_agent="iris")] == [b.id]
        assert {t.title for t in u.tasks.list(statuses=["triage", "ready"])} == {"b", "c"}
        assert [t.title for t in u.tasks.list(status="running")] == ["a"]
        assert len(u.tasks.list(limit=2)) == 2 and len(u.tasks.list(limit=2, offset=2)) == 1


def test_run_repository_rules(db):
    with db.write() as u:
        t = u.tasks.claim(make(u).id, "joel")
        run = u.task_runs.start(t.id, "joel", model="sonnet", pid=123)
        with pytest.raises(Exception):
            u.task_runs.start(t.id, "joel", model="sonnet")           # one running attempt per task
        u.task_runs.set_pid(run.id, 456)
        u.task_runs.add_tokens(run.id, 10, 20)
        u.task_runs.add_tokens(run.id, 1, 2)
        assert (u.task_runs.get(run.id).pid, u.task_runs.get(run.id).tokens_in, u.task_runs.get(run.id).tokens_out) == (456, 11, 22)
        with pytest.raises(ValidationError):
            u.task_runs.finish(run.id, "completed")                    # needs a summary
        with pytest.raises(ValidationError):
            u.task_runs.finish(run.id, "running")
        with pytest.raises(ValidationError):
            u.task_runs.add_tokens(run.id, -1, 0)
        done = u.task_runs.finish(run.id, "failure", error="x", metadata={"k": 1}, tokens_in=50)
        assert done.ended_at and done.metadata == {"k": 1} and done.tokens_in == 50
        assert u.task_runs.current(t.id) is None


def test_comments(db):
    with db.write() as u:
        t = make(u)
        u.task_comments.add(t.id, " hello ", actor=agent("joel"))
        assert [(c.author_kind, c.author, c.body) for c in u.task_comments.list(t.id)] == [("agent", "joel", "hello")]
        with pytest.raises(ValidationError):
            u.task_comments.add(t.id, "", actor=HUMAN)
        with pytest.raises(ValidationError):
            u.task_comments.add(t.id, "x" * 2001, actor=HUMAN)


def test_transition_conflict_and_noop(db):
    with db.write() as u:
        t = make(u)
        assert u.tasks.transition(t.id, "ready", actor=HUMAN).id == t.id          # already there: no change
        with pytest.raises(NotFound):
            u.tasks.transition(999, "ready", actor=HUMAN)
        assert u.tasks.to_triage(t.id, "look at this").status == "triage"
        assert Actor.system().kind == "system"

"""decisions, journal, guard events, extensions, scheduled jobs, consumption."""
from datetime import datetime, timedelta, timezone

import pytest
from dbsupport import HUMAN, SYSTEM, agent, db, env  # noqa: F401  (fixtures)

from hokma.db import IntegrityViolation, NotFound, RuleViolation, ValidationError, to_db_time


def test_decisions(db):
    with db.write() as u:
        d = u.decisions.propose("Use ETFs", "Buy index funds", proposed_by="hokma", context="c",
                                options=[{"name": "etf"}, {"name": "stocks"}])
        assert d.status == "proposed" and d.options == [{"name": "etf"}, {"name": "stocks"}]
        with pytest.raises(ValidationError):
            u.decisions.decide(d.id, "approved", actor=HUMAN)                 # a person needs an approval id
        with pytest.raises(ValidationError):
            u.decisions.decide(d.id, "approved", actor=SYSTEM)
        approval = u.approvals.record("decision", d.id, "approved", decided_by="owner")
        done = u.decisions.decide(d.id, "approved", actor=HUMAN, approval_id=approval.id)
        assert done.status == "approved" and done.decided_by_kind == "human" and done.approval_id == approval.id
        with pytest.raises(RuleViolation):
            u.decisions.decide(d.id, "rejected", actor=HUMAN, approval_id=approval.id)
        by_agent = u.decisions.propose("Small thing", "Do it", proposed_by="joel")
        assert u.decisions.decide(by_agent.id, "rejected", actor=agent("hokma")).decided_by_kind == "agent"
        newer = u.decisions.propose("Use ETFs v2", "Buy more", proposed_by="hokma")
        assert u.decisions.supersede(d.id, newer.id).status == "superseded"
        with pytest.raises(ValidationError):
            u.decisions.supersede(d.id, d.id)
        assert [x.id for x in u.decisions.list(status="proposed")] == [newer.id]
        with pytest.raises(ValidationError):
            u.decisions.propose("", "p", proposed_by="hokma")
        with pytest.raises(IntegrityViolation):
            u.decisions.propose("t", "p", proposed_by="ghost")


def test_journal_is_searchable_and_append_only(db):
    with db.write() as u:
        p = u.projects.create("P", "/p")
        t = u.tasks.create("t", actor=HUMAN)
        first = u.journal.add("fact", "Spread widened", actor=agent("iris"), body="NQ spread 3 ticks", refs={"k": [1]})
        assert first.agent_slug == "iris" and first.refs == {"k": [1]} and first.outcome == "info"
        u.journal.add("action", "Ran backtest", actor=agent("joel"), outcome="success", task_id=t.id, project_id=p.id)
        u.journal.add("alert", "Cap reached 100%", actor=SYSTEM, outcome="failure")
        u.journal.add("note", "mine", actor=HUMAN)
        assert [e.title for e in u.journal.list(limit=2)] == ["mine", "Cap reached 100%"]
        assert [e.title for e in u.journal.list(agent_slug="iris")] == ["Spread widened"]
        assert [e.title for e in u.journal.list(task_id=t.id)] == ["Ran backtest"]
        assert [e.title for e in u.journal.list(project_id=p.id, kind="action")] == ["Ran backtest"]
        assert [e.title for e in u.journal.list(search="ticks")] == ["Spread widened"]
        assert [e.title for e in u.journal.list(search="100%")] == ["Cap reached 100%"]
        assert u.journal.list(search="50%") == []
        assert u.journal.count() == 4 and u.journal.count(kind="note") == 1
        newest = u.journal.list(limit=1)[0]
        assert len(u.journal.list(before_id=newest.id)) == 3
        assert len(u.journal.list(since="2000-01-01")) == 4 and u.journal.list(since="2999-01-01") == []
        with pytest.raises(IntegrityViolation):
            u.journal.add("rumor", "bad kind", actor=HUMAN)
        with pytest.raises(IntegrityViolation):
            u.journal.add("note", "", actor=HUMAN)


def test_guard_events(db):
    with db.write() as u:
        u.guard_events.record("broker", "blocked", "orders are not allowed", agent_slug="joel", tool="place_order")
        u.guard_events.record("approval", "asked", "needs a click")
        assert [g.guard for g in u.guard_events.list()] == ["approval", "broker"]
        assert [g.tool for g in u.guard_events.list(guard="broker")] == ["place_order"]
        assert len(u.guard_events.list(decision="asked")) == 1 and u.guard_events.list(since="2999-01-01") == []
        assert len(u.guard_events.record("secrets", "blocked", "x" * 2000).reason) == 1000
        with pytest.raises(IntegrityViolation):
            u.guard_events.record("magic", "blocked", "x")


def test_bug_edits_and_filters(db):
    with db.write() as u:
        p = u.projects.create("P", "/p")
        b = u.bugs.create("Slippage", actor=agent("iris"), severity="critical", project_id=p.id)
        assert (b.found_by_kind, b.found_by, b.changed_by) == ("agent", "iris", "iris")
        u.bugs.create("Typo", actor=HUMAN, severity="low")
        assert [x.title for x in u.bugs.list(severity="critical")] == ["Slippage"]
        assert [x.title for x in u.bugs.list(project_id=p.id)] == ["Slippage"]
        assert u.bugs.update(b.id, actor=HUMAN, title="Slippage on open", severity="high").severity == "high"
        with pytest.raises(ValidationError):
            u.bugs.update(b.id, actor=HUMAN, severity="meh")
        with pytest.raises(ValidationError):
            u.bugs.create("", actor=HUMAN)


def test_extensions_and_requests(db):
    with db.write() as u:
        pre = u.extensions.add("skills-base", "hokma", "skill", is_preinstalled=True, enabled=True)
        assert pre.is_preinstalled and pre.enabled
        again = u.extensions.add("skills-base", "hokma", "skill", version="2", enabled=False)
        assert again.id == pre.id and again.version == "2" and again.enabled is False
        assert u.extensions.find("hokma", "skills-base").id == pre.id
        assert [e.name for e in u.extensions.list(kind="skill")] == ["skills-base"]
        assert u.extensions.set_enabled(pre.id, True).enabled and len(u.extensions.list(enabled=True)) == 1
        with pytest.raises(ValidationError):
            u.extensions.add("x", "m", "theme")

        req = u.extension_requests.create("install", "plugin", "charts", "community", "to plot equity",
                                          actor=agent("iris"), version="1.0", license="MIT")
        assert req.status == "pending" and req.requested_by_kind == "agent"
        with pytest.raises(IntegrityViolation):                               # an agent cannot self-approve
            u.session.execute("UPDATE extension_requests SET status = 'approved', decided_at = '2026-01-01 00:00:00' "
                              "WHERE id = :i", {"i": req.id})
        with pytest.raises(RuleViolation):
            u.extension_requests.mark_applied(req.id, pre.id)                 # not approved yet
        approved = u.extension_requests.approve(req.id, decided_by="owner", note="ok")
        assert approved.status == "approved" and approved.approval_id and approved.decided_at
        assert u.approvals.get(approved.approval_id).subject_type == "extension"
        with pytest.raises(RuleViolation):
            u.extension_requests.reject(req.id, decided_by="owner")
        ext = u.extensions.add("charts", "community", "plugin", approval_id=approved.approval_id, enabled=True)
        applied = u.extension_requests.mark_applied(req.id, ext.id)
        assert applied.status == "applied" and applied.applied_at and applied.extension_id == ext.id

        other = u.extension_requests.create("enable", "mcp", "data", "community", "needs data", actor=HUMAN)
        assert u.extension_requests.reject(other.id, decided_by="owner").status == "rejected"
        third = u.extension_requests.create("install", "skill", "s", "community", "r", actor=agent("joel"))
        u.extension_requests.approve(third.id, decided_by="owner")
        assert u.extension_requests.mark_failed(third.id, "download failed").error == "download failed"
        with pytest.raises(RuleViolation):
            u.extension_requests.mark_failed(other.id, "x")
        assert {r.status for r in u.extension_requests.list()} == {"applied", "rejected", "failed"}
        assert len(u.extension_requests.list(status="rejected")) == 1
        with pytest.raises(ValidationError):
            u.extension_requests.create("install", "plugin", "n", "m", "", actor=HUMAN)
        with pytest.raises(ValidationError):
            u.extension_requests.create("install", "plugin", "n", "m", "r", actor=SYSTEM)
        with pytest.raises(IntegrityViolation):
            u.extensions.remove(ext.id)                                       # an applied request points at it
        u.extensions.remove(pre.id)


def test_old_pending_requests_expire(db):
    with db.write() as u:
        old = u.extension_requests.create("install", "plugin", "a", "m", "r", actor=agent("joel"))
        fresh = u.extension_requests.create("install", "plugin", "b", "m", "r", actor=agent("joel"))
        u.session.execute("UPDATE extension_requests SET requested_at = '2000-01-01 00:00:00' WHERE id = :i",
                          {"i": old.id})
        assert u.extension_requests.expire_older_than(72) == 1
        assert u.extension_requests.get(old.id).status == "expired"
        assert u.extension_requests.get(fresh.id).status == "pending"


def test_scheduled_jobs(db):
    with db.write() as u:
        job = u.scheduled_jobs.create("Premarket brief", "hokma", "Write the brief", "daily", "08:30",
                                      "America/New_York", next_run_at="2026-11-06T13:30:00Z")
        assert job.is_active is False and job.next_run_at == "2026-11-06 13:30:00"
        with pytest.raises(IntegrityViolation):
            u.scheduled_jobs.create("Premarket brief", "hokma", "dup", "daily", "09:00", "UTC")
        with pytest.raises(ValidationError):
            u.scheduled_jobs.create("x", "hokma", "p", "hourly", "1", "UTC")
        assert u.scheduled_jobs.due(now="2030-01-01 00:00:00") == []             # inactive jobs never come due
        approval = u.approvals.record("scheduled_job", job.id, "approved", decided_by="owner")
        assert u.scheduled_jobs.set_active(job.id, True, approval_id=approval.id).approval_id == approval.id
        assert u.scheduled_jobs.due(now="2026-11-06 13:29:59") == []
        assert [j.id for j in u.scheduled_jobs.due(now="2026-11-06 13:30:00")] == [job.id]
        t = u.tasks.create("brief run", actor=SYSTEM, assigned_agent="hokma")
        ran = u.scheduled_jobs.mark_ran(job.id, task_id=t.id, next_run_at="2026-11-07 13:30:00")
        assert ran.last_task_id == t.id and ran.last_run_at and ran.next_run_at == "2026-11-07 13:30:00"
        assert u.scheduled_jobs.due(now="2026-11-06 13:30:00") == []
        assert u.scheduled_jobs.update(job.id, prompt="New prompt", schedule_expr="09:00").schedule_expr == "09:00"
        assert u.scheduled_jobs.find_by_name("Premarket brief").id == job.id
        assert [j.id for j in u.scheduled_jobs.list(active=True)] == [job.id] and u.scheduled_jobs.list(active=False) == []
        u.scheduled_jobs.delete(job.id)
        with pytest.raises(NotFound):
            u.scheduled_jobs.get(job.id)


def test_usage_sums_messages_and_runs(db):
    with db.write() as u:
        c = u.conversations.create(engine="claude_code")
        u.messages.add(c.id, "user", "q")
        u.messages.add(c.id, "assistant", "a", agent_slug="joel", model="sonnet", tokens_in=100, tokens_out=50)
        u.messages.add(c.id, "assistant", "b", agent_slug="iris", model="opus", tokens_in=10, tokens_out=5)
        t = u.tasks.claim(u.tasks.create("w", actor=HUMAN, assigned_agent="joel").id, "joel")
        run = u.task_runs.start(t.id, "joel", model="sonnet")
        u.task_runs.add_tokens(run.id, 1000, 500)
        total = u.usage.total()
        assert (total.tokens_in, total.tokens_out, total.tokens, total.messages, total.runs) == (1110, 555, 1665, 3, 1)
        assert [(r.key, r.tokens_in + r.tokens_out) for r in u.usage.by_agent()] == [("joel", 1650), ("iris", 15)]
        assert [(r.key, r.tokens_in + r.tokens_out) for r in u.usage.by_model()] == [("sonnet", 1650), ("opus", 15)]
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert [r.key for r in u.usage.by_day(since="2000-01-01")] == [day]
        assert u.usage.total(since="2999-01-01").tokens == 0
        assert u.usage.total(until="2000-01-01").messages == 0


def test_week_usage_uses_the_profile_timezone_and_cap(db):
    with db.write() as u:
        u.profile.save(owner_name="Ana", timezone="America/New_York", weekly_token_cap=2000)
        c = u.conversations.create(engine="claude_code")
        u.messages.add(c.id, "assistant", "x", agent_slug="joel", tokens_in=300, tokens_out=200)
        week = u.usage.week()
        assert week.used == 500 and week.cap == 2000 and week.pct == 25.0
        start = datetime.strptime(week.starts_at, "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(week.ends_at, "%Y-%m-%d %H:%M:%S")
        assert end - start == timedelta(days=7)
        assert start.hour in (4, 5) and start.weekday() in (0, 6)          # Monday 00:00 in New York, as UTC
        far = u.usage.week(now="2000-01-05 12:00:00")
        assert far.used == 0 and far.starts_at == "2000-01-03 05:00:00"
        u.profile.save(weekly_token_cap=None)
        assert u.usage.week().pct is None and u.usage.week().cap is None


def test_week_usage_without_a_profile_falls_back_to_utc(db):
    with db.read() as u:
        week = u.usage.week(now="2026-11-05 12:00:00")
        assert week.starts_at == "2026-11-02 00:00:00" and week.cap is None
        assert to_db_time(week.ends_at) == "2026-11-09 00:00:00"

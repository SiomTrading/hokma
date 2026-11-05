"""Repositories: profile, agents, projects, conversations, messages, attachments, onboarding, units of work."""
import json

import pytest
from dbsupport import HUMAN, db, env  # noqa: F401  (fixtures)

from hokma.db import (Conflict, IntegrityViolation, NotFound, ValidationError, from_db_time, get_database,
                      open_database, to_db_time, utcnow)
from hokma.db import services


# ------------------------------------------------------------------------------------------------ units
def test_write_unit_commits_and_rolls_back_on_error(db):
    with db.write() as u:
        u.projects.create("Kept", "/p/kept")
    with pytest.raises(RuntimeError):
        with db.write() as u:
            u.projects.create("Lost", "/p/lost")
            raise RuntimeError("boom")
    with db.read() as u:
        assert [p.name for p in u.projects.list()] == ["Kept"]


def test_read_unit_does_not_keep_writes(db):
    with db.read() as u:
        u.projects.create("Temp", "/p/temp")
    with db.read() as u:
        assert u.projects.list() == []


def test_singleton_follows_the_data_folder(env, monkeypatch):
    first = get_database()
    assert get_database() is first
    monkeypatch.setenv("HOKMA_DATA_DIR", str(env / "other"))
    assert get_database() is not first
    assert open_database(env / "x" / "y.db").path == env / "x" / "y.db"


def test_time_helpers():
    assert len(utcnow()) == 19
    assert to_db_time("2026-11-05T10:00:00Z") == "2026-11-05 10:00:00"
    assert to_db_time("2026-11-05T05:00:00-05:00") == "2026-11-05 10:00:00"
    assert to_db_time(None) is None
    assert from_db_time("2026-11-05 10:00:00").tzinfo is not None


# ----------------------------------------------------------------------------------------------- profile
def test_profile_lifecycle(db):
    with db.write() as u:
        assert u.profile.get() is None
        default = u.profile.get_or_default()
        assert default.exists is False and default.owner_name == "" and default.theme == "dark"
        with pytest.raises(ValidationError):
            u.profile.save(language="en")                            # a new row needs the owner name
        with pytest.raises(ValidationError):
            u.profile.save(owner_name="   ")
        with pytest.raises(ValidationError):
            u.profile.save(owner_name="x" * 61)
        p = u.profile.save(owner_name="  Ana   Maria ", fund_name="Fondo", workdir="/w")
        assert p.exists and p.owner_name == "Ana Maria" and p.fund_name == "Fondo" and p.workdir == "/w"
        assert p.database_engine == "sqlite" and p.claude_status == "unknown" and p.theme == "dark"
        assert u.profile.is_setup_complete() is False
    with db.write() as u:
        p = u.profile.save(theme="light", language="en", telemetry_enabled=True, max_parallel_agents=3,
                           weekly_token_cap=1000, fund_name=None)
        assert (p.theme, p.language, p.telemetry_enabled, p.max_parallel_agents, p.weekly_token_cap, p.fund_name) == (
            "light", "en", True, 3, 1000, None)
        assert u.profile.save(max_parallel_agents=None, weekly_token_cap=None).max_parallel_agents is None
        for bad in ({"theme": "pink"}, {"language": "fr"}, {"engine": "x"}, {"database_engine": "mysql"},
                    {"max_parallel_agents": 0}, {"weekly_token_cap": -5}, {"max_parallel_agents": True},
                    {"claude_status": "weird"}):
            with pytest.raises(ValidationError):
                u.profile.save(**bad)
        p = u.profile.set_claude_status("connected", "Max")
        assert p.claude_status == "connected" and p.claude_plan == "Max" and p.claude_checked_at
        done = u.profile.complete_setup()
        assert done.setup_completed_at and u.profile.is_setup_complete()
        again = u.profile.complete_setup()
        assert again.setup_completed_at == done.setup_completed_at     # set once


def test_set_claude_status_and_complete_need_a_profile(db):
    with db.write() as u:
        with pytest.raises(ValidationError):
            u.profile.set_claude_status("connected")
        with pytest.raises(ValidationError):
            u.profile.complete_setup()


def test_license_checks(db):
    with db.write() as u:
        assert u.license_checks.latest() is None
        u.license_checks.record("valid", latest_version="0.0.5")
        u.license_checks.record("offline", detail="x" * 900)
        assert u.license_checks.latest().result == "offline" and len(u.license_checks.latest().detail) == 500
        assert [c.result for c in u.license_checks.list()] == ["offline", "valid"]
        with pytest.raises(ValidationError):
            u.license_checks.record("fine")


def test_services_save_owner_and_language(db, env):
    services.save_owner(db, "Ana", "Fondo")
    from hokma.kit import owner

    assert owner.load() == {"owner_name": "Ana", "fund_name": "Fondo"}
    with db.read() as u:
        assert u.profile.get().owner_name == "Ana"
    services.set_language(db, "es")
    assert (env / "data" / "language").read_text().strip() == "es"
    with db.read() as u:
        assert u.profile.get().language == "es"
    with pytest.raises(ValidationError):
        services.set_language(db, "fr")


# ------------------------------------------------------------------------------------------------- agents
def test_agents(db):
    with db.write() as u:
        assert u.agents.count() == 10 and u.agents.count("active") == 4
        mine = u.agents.create_custom("my-agent", "Mine", description="d", model_tier="deep")
        assert mine.is_custom and mine.status == "off"
        for bad in ("A", "Upper", "x", "has space", "a" * 41):
            with pytest.raises(ValidationError):
                u.agents.create_custom(bad, "n")
        with pytest.raises(IntegrityViolation):
            u.agents.create_custom("my-agent", "dup")
        assert u.agents.set_status("my-agent", "active", enabled_session=3).enabled_session == 3
        assert u.agents.set_model_tier("my-agent", "fast").model_tier == "fast"
        assert u.agents.update("my-agent", display_name="Renamed").display_name == "Renamed"
        with pytest.raises(ValidationError):
            u.agents.set_status("my-agent", "paused")
        with pytest.raises(NotFound):
            u.agents.get("ghost")
        assert u.agents.find("ghost") is None
        assert [a.slug for a in u.agents.list(status="active", include_custom=False)] == ["hokma", "iris", "joel", "vera"]
        assert u.agents.mark_missing_off(["hokma"]) == 3


# ----------------------------------------------------------------------------------------------- projects
def test_projects(db):
    with db.write() as u:
        a = u.projects.create("Mi IPS", "/w/ips", kind="ips")
        b = u.projects.create("Mi IPS", "/w/ips2")
        assert a.slug == "mi-ips" and b.slug == "mi-ips-2"
        assert u.projects.get_by_slug("mi-ips").id == a.id and u.projects.get_by_slug("none") is None
        r = u.projects.create("Review me", "/w/r", default_requires_review=True, default_reviewer_agent="vera",
                              default_requires_approval=True)
        assert r.default_requires_review and r.default_requires_approval
        with pytest.raises(ValidationError):
            u.projects.create("No reviewer", "/w", default_requires_review=True)
        with pytest.raises(ValidationError):
            u.projects.create("", "/w")
        with pytest.raises(ValidationError):
            u.projects.create("n", "")
        with pytest.raises(ValidationError):
            u.projects.create("n", "/w", kind="bogus")
        assert u.projects.update(a.id, name="Renamed", status="paused", description="d").status == "paused"
        with pytest.raises(ValidationError):
            u.projects.update(a.id, default_requires_review=True)       # needs a reviewer
        assert u.projects.update(a.id, default_requires_review=True, default_reviewer_agent="vera").default_requires_review
        assert [p.name for p in u.projects.list(status="paused")] == ["Renamed"]
        assert {p.kind for p in u.projects.list(kind="ips")} == {"ips"}
        u.projects.delete(b.id)
        with pytest.raises(NotFound):
            u.projects.get(b.id)


def test_project_defaults_flow_into_new_tasks(db):
    with db.write() as u:
        p = u.projects.create("P", "/p", default_requires_review=True, default_reviewer_agent="vera",
                              default_requires_approval=True)
        t = u.tasks.create("inherits", actor=HUMAN, project_id=p.id, assigned_agent="joel")
        assert (t.requires_review, t.reviewer_agent, t.requires_approval) == (True, "vera", True)
        o = u.tasks.create("overrides", actor=HUMAN, project_id=p.id, assigned_agent="joel", requires_review=False,
                           requires_approval=False)
        assert (o.requires_review, o.requires_approval) == (False, False)


def test_project_with_journal_cannot_be_deleted_but_tasks_are_kept(db):
    with db.write() as u:
        p = u.projects.create("P", "/p")
        t = u.tasks.create("t", actor=HUMAN, project_id=p.id)
        u.journal.add("note", "n", actor=HUMAN, project_id=p.id)
    with pytest.raises(IntegrityViolation):
        with db.write() as u:
            u.projects.delete(p.id)
    with db.write() as u:
        q = u.projects.create("Q", "/q")
        c = u.conversations.create(engine="claude_code", project_id=q.id)
        u.projects.delete(q.id)
        assert u.conversations.get(c.id).project_id is None             # the chat survives, unlinked


# ------------------------------------------------------------------------------------ chat and attachments
def test_conversations_and_messages(db):
    with db.write() as u:
        p = u.projects.create("P", "/p")
        c1 = u.conversations.create(engine="claude_code", title="First")
        c2 = u.conversations.create(engine="claude_code", title="Second", project_id=p.id, model="opus",
                                    agent_slug="joel", pinned=True)
        assert c2.pinned and c2.agent_slug == "joel" and c2.message_count == 0 and c1.last_message_at is None
        m1 = u.messages.add(c1.id, "user", "hello 100% sure_thing")
        u.messages.add(c1.id, "assistant", "hi", agent_slug="joel", model="sonnet", tokens_in=3, tokens_out=5)
        assert u.conversations.get(c1.id).message_count == 2 and u.conversations.get(c1.id).last_message_at
        assert [c.title for c in u.conversations.list()] == ["Second", "First"]       # pinned first
        assert [c.title for c in u.conversations.list(pinned=True)] == ["Second"]
        assert [c.title for c in u.conversations.list(project_id=p.id)] == ["Second"]
        assert [c.title for c in u.conversations.list(search="sure_thing")] == ["First"]
        assert u.conversations.list(search="100%") and not u.conversations.list(search="50%")
        assert u.conversations.list(search="Seco")[0].title == "Second"
        assert u.conversations.rename(c1.id, "  New  ").title == "New"
        assert u.conversations.rename(c1.id, "  ").title is None
        assert u.conversations.pin(c1.id, True).pinned
        assert u.conversations.set_project(c1.id, p.id).project_id == p.id
        assert u.conversations.set_model(c1.id, "haiku").model == "haiku"
        assert u.conversations.set_agent(c1.id, "vera").agent_slug == "vera"
        assert u.conversations.set_engine_session(c1.id, "sess-1").engine_session == "sess-1"
        with pytest.raises(NotFound):
            u.conversations.get(999)
        with pytest.raises(ValidationError):
            u.conversations.create(engine="")
        with pytest.raises(ValidationError):
            u.messages.add(c1.id, "robot", "x")
        with pytest.raises(IntegrityViolation):
            u.messages.add(999, "user", "orphan")
        streaming = u.messages.add(c1.id, "assistant", "", agent_slug="joel")
        done = u.messages.update(streaming.id, content="final", tokens_in=1, tokens_out=2)
        assert done.content == "final" and done.tokens_out == 2
        assert u.messages.count(c1.id) == 3
        assert [m.content for m in u.messages.search("final")] == ["final"]
        assert m1.content in [m.content for m in u.messages.search("sure_thing")]
        u.conversations.delete(c1.id)
        assert u.conversations.find(c1.id) is None and u.messages.count(c1.id) == 0


def test_message_paging(db):
    with db.write() as u:
        c = u.conversations.create(engine="claude_code")
        ids = [u.messages.add(c.id, "user", f"m{n}").id for n in range(10)]
        assert [m.content for m in u.messages.list(c.id)] == [f"m{n}" for n in range(10)]
        assert [m.content for m in u.messages.list(c.id, limit=3)] == ["m7", "m8", "m9"]
        assert [m.content for m in u.messages.list(c.id, limit=3, before_id=ids[5])] == ["m2", "m3", "m4"]
        assert [m.content for m in u.messages.list(c.id, limit=2, after_id=ids[5])] == ["m6", "m7"]
        assert [m.content for m in u.messages.list(c.id, after_id=ids[7])] == ["m8", "m9"]


def attachment(u, name="a.png", **kw):
    values = dict(kind="image", stored_path=f"/data/{name}", mime_type="image/png", size_bytes=10, sha256="a" * 64)
    values.update(kw)
    return u.attachments.add(**values)


def test_attachments(db):
    with db.write() as u:
        p = u.projects.create("P", "/p")
        c = u.conversations.create(engine="claude_code")
        m = u.messages.add(c.id, "user", "see")
        a = attachment(u, "one.png", message_id=m.id, project_id=p.id)
        audio = attachment(u, "v.webm", kind="audio", mime_type="audio/webm", transcript="hola")
        assert audio.transcript == "hola" and a.message_id == m.id
        assert [x.id for x in u.attachments.list(kind="audio")] == [audio.id]
        assert [x.id for x in u.attachments.list(project_id=p.id)] == [a.id]
        assert u.attachments.link_message(audio.id, m.id).message_id == m.id
        assert u.attachments.set_transcript(audio.id, "nuevo").transcript == "nuevo"
        with pytest.raises(IntegrityViolation):
            attachment(u, "one.png")                                     # stored_path is unique
        with pytest.raises(IntegrityViolation):
            attachment(u, "big.png", size_bytes=26214401)
        with pytest.raises(IntegrityViolation):
            attachment(u, "bad.png", sha256="short")
        with pytest.raises(IntegrityViolation):
            attachment(u, "t.png", transcript="only audio has one")
        u.attachments.delete(a.id)
        with pytest.raises(NotFound):
            u.attachments.get(a.id)
        u.conversations.delete(c.id)                                     # attachments outlive their message
        assert u.attachments.get(audio.id).message_id is None


def test_onboarding(db):
    steps = ["workdir", "claude", "agent", "project"]
    with db.write() as u:
        assert u.onboarding.progress(steps).done == 0
        u.onboarding.mark("claude")
        u.onboarding.mark("claude")
        u.onboarding.mark("project")
        progress = u.onboarding.progress(steps)
        assert (progress.done, progress.total) == (2, 4) and progress.steps["claude"] and not progress.steps["agent"]
        assert set(u.onboarding.done()) == {"claude", "project"}
        u.onboarding.unmark("claude")
        assert u.onboarding.progress(steps).done == 1
        with pytest.raises(ValidationError):
            u.onboarding.mark("")


def test_records_export_json_safe_dicts(db):
    with db.write() as u:
        t = u.tasks.create("export", actor=HUMAN)
        json.dumps(t.to_dict())
        json.dumps(u.tasks.detail(t.id).to_dict())
        assert t.to_dict()["parent_ids"] == ()

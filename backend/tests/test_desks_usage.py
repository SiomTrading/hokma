"""Usage and budget per desk (SPEC-014 P3): totals that do not mix between desks, the same week as the home screen,
the three states and the one-warning-per-week-and-level rule."""
from datetime import UTC, datetime, timedelta

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.db import Actor
from hokma.db.timeutil import FORMAT
from hokma.desks import budget, service
from hokma.desks.errors import DeskError
from hokma.i18n import use_language

SLUGS = ("desk-chief", "hugo", "risk", "iris", "signals", "axel", "vera", "bull-case", "bear-case", "judge")
OWNER = Actor.human("owner")


@pytest.fixture
def desks_db(db):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
        for slug in SLUGS:
            u.agents.upsert_kit(slug, slug.title(), f"{slug} agent", "balanced", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
    return db


def make(db, name, **kw):
    kw.setdefault("capital", 10000)
    kw.setdefault("template", "minimal")
    return service.create_desk(name, db=db, **kw)


def chat(u, project_id, agent, tin, tout, *, when=None):
    """One assistant message of `agent` in a conversation of the project."""
    conv = u.conversations.create(engine="claude_code", project_id=project_id, agent_slug=agent)
    msg = u.messages.add(conv.id, "assistant", "x", agent_slug=agent, model="sonnet", tokens_in=tin, tokens_out=tout)
    if when:
        u.session.execute("UPDATE messages SET created_at = :t WHERE id = :i", {"t": when, "i": msg.id})


def run(u, project_id, agent, tin, tout, *, when=None):
    """One task run of `agent` for a task of the project."""
    task = u.tasks.create("work", actor=OWNER, project_id=project_id)
    r = u.task_runs.start(task.id, agent, model="haiku")
    u.task_runs.add_tokens(r.id, tin, tout)
    if when:
        u.session.execute("UPDATE task_runs SET started_at = :t WHERE id = :i", {"t": when, "i": r.id})


def days_ago(n):
    return (datetime.now(UTC) - timedelta(days=n)).strftime(FORMAT)


# ------------------------------------------------------------------------------------------ totals

def test_two_desks_do_not_mix_their_totals(desks_db):
    a, b = make(desks_db, "Desk A"), make(desks_db, "Desk B")
    with desks_db.write() as u:
        chat(u, a["project_id"], "desk-chief", 100, 50)
        run(u, a["project_id"], "hugo", 20, 10)
        chat(u, b["project_id"], "desk-chief", 7, 3)
        run(u, b["project_id"], "risk", 5, 5)
        chat(u, None, "desk-chief", 9000, 9000)                  # a general chat belongs to no desk
    ua, ub = budget.usage(a["id"], db=desks_db), budget.usage(b["id"], db=desks_db)
    assert (ua["used"], ua["tokens_in"], ua["tokens_out"]) == (180, 120, 60)
    assert (ub["used"], ub["tokens_in"], ub["tokens_out"]) == (20, 12, 8)
    assert {r["key"]: r["tokens_in"] + r["tokens_out"] for r in ua["by_agent"]} == {"desk-chief": 150, "hugo": 30}
    assert {r["key"]: r["tokens_in"] + r["tokens_out"] for r in ub["by_agent"]} == {"desk-chief": 10, "risk": 10}
    assert [r["key"] for r in ua["by_agent"]] == ["desk-chief", "hugo"]            # heaviest first
    assert len(ua["by_day"]) == 1 and ua["by_day"][0]["tokens_in"] == 120


def test_a_chat_message_without_its_own_agent_counts_for_the_agent_of_its_conversation(desks_db):
    desk = make(desks_db, "Desk A")
    with desks_db.write() as u:
        conv = u.conversations.create(engine="claude_code", project_id=desk["project_id"], agent_slug="desk-chief")
        u.messages.add(conv.id, "assistant", "x", model="sonnet", tokens_in=70, tokens_out=30)     # the way the chat saves it
    rows = service.list_agents(desk["id"], desks_db)
    assert {r["agent_slug"]: r["tokens_week"] for r in rows}["desk-chief"] == 100


def test_a_desk_without_activity_is_all_zero(desks_db):
    info = budget.usage(make(desks_db, "Quiet")["id"], db=desks_db)
    assert info["used"] == 0 and info["by_agent"] == [] and info["by_day"] == []
    assert info["state"] == "ok" and info["token_budget"] is None and info["pct"] is None


def test_the_week_is_the_one_of_the_home_screen_and_older_tokens_are_left_out(desks_db):
    desk = make(desks_db, "Weekly", token_budget=1000)
    with desks_db.write() as u:
        chat(u, desk["project_id"], "desk-chief", 30, 10)
        chat(u, desk["project_id"], "desk-chief", 500, 500, when=days_ago(9))        # before this week
        run(u, desk["project_id"], "hugo", 400, 400, when=days_ago(9))
    with desks_db.read() as u:
        week = u.usage.week()
    info = budget.usage(desk["id"], db=desks_db)
    assert (info["starts_at"], info["ends_at"]) == (week.starts_at, week.ends_at)
    assert info["used"] == 40
    assert info["pct"] == 4.0


def test_a_desk_does_not_count_the_whole_fund(desks_db):
    a, b = make(desks_db, "Desk A"), make(desks_db, "Desk B")
    with desks_db.write() as u:
        chat(u, b["project_id"], "desk-chief", 1000, 1000)
    with desks_db.read() as u:
        fund = u.usage.week().used
    assert fund == 2000 and budget.usage(a["id"], db=desks_db)["used"] == 0


# ------------------------------------------------------------------------------------------ states

@pytest.mark.parametrize("used,expected", [(0, "ok"), (799, "ok"), (800, "warn"), (999, "warn"),
                                           (1000, "exhausted"), (5000, "exhausted")])
def test_states_against_a_budget(used, expected):
    assert budget.state_for(used, 1000) == expected


def test_without_a_budget_the_state_is_always_ok():
    assert budget.state_for(10**9, None) == "ok" and budget.state_for(10**9, 0) == "ok"


def test_state_follows_the_tokens_the_desk_used(desks_db):
    desk = make(desks_db, "Limited", token_budget=1000)
    assert budget.state(desk["id"], db=desks_db) == "ok"
    with desks_db.write() as u:
        chat(u, desk["project_id"], "desk-chief", 500, 300)
    assert budget.state(desk["id"], db=desks_db) == "warn"
    with desks_db.write() as u:
        run(u, desk["project_id"], "hugo", 100, 100)
    assert budget.state(desk["id"], db=desks_db) == "exhausted"
    service.update_desk(desk["id"], {"token_budget": 5000}, db=desks_db)             # a higher budget resumes it
    assert budget.state(desk["id"], db=desks_db) == "ok"


def test_a_new_week_starts_from_zero_by_itself(desks_db):
    desk = make(desks_db, "Reset", token_budget=100)
    with desks_db.write() as u:
        chat(u, desk["project_id"], "desk-chief", 90, 90)
    assert budget.state(desk["id"], db=desks_db) == "exhausted"
    later = datetime.now(UTC) + timedelta(days=8)
    assert budget.state(desk["id"], db=desks_db, now=later) == "ok"


# ------------------------------------------------------------------------------------------ warnings

def alerts(db, project_id):
    with db.read() as u:
        return u.journal.list(kind="alert", project_id=project_id)


def test_the_warning_is_written_once_per_week_and_level(desks_db):
    desk = make(desks_db, "Alerts", token_budget=1000)
    pid = desk["project_id"]
    assert budget.check_alert(desk["id"], db=desks_db) is None                    # ok: nothing to say
    with desks_db.write() as u:
        chat(u, pid, "desk-chief", 450, 400)
    assert budget.check_alert(desk["id"], db=desks_db) == 80
    assert budget.check_alert(desk["id"], db=desks_db) is None                    # same week, same level
    assert len(alerts(desks_db, pid)) == 1
    with desks_db.write() as u:
        chat(u, pid, "desk-chief", 100, 100)
    assert budget.check_alert(desk["id"], db=desks_db) == 100                     # the next level is a new warning
    assert budget.check_alert(desk["id"], db=desks_db) is None
    entries = alerts(desks_db, pid)
    assert len(entries) == 2 and all(e.actor_kind == "system" for e in entries)
    assert {e.refs["level"] for e in entries} == {80, 100}
    with desks_db.read() as u:
        stored = u.desks.get(desk["id"])
    assert stored.budget_alert_level == 100 and stored.budget_alert_week == budget.usage(desk["id"], db=desks_db)["starts_at"]


def test_jumping_straight_to_100_writes_only_the_100_warning(desks_db):
    desk = make(desks_db, "Jump", token_budget=100)
    with desks_db.write() as u:
        chat(u, desk["project_id"], "desk-chief", 100, 100)
    assert budget.check_alert(desk["id"], db=desks_db) == 100
    assert len(alerts(desks_db, desk["project_id"])) == 1


def test_a_new_week_warns_again(desks_db):
    desk = make(desks_db, "Weeks", token_budget=100)
    pid = desk["project_id"]
    old = days_ago(8)
    with desks_db.write() as u:
        chat(u, pid, "desk-chief", 50, 40, when=old)
    past = datetime.now(UTC) - timedelta(days=8)
    assert budget.check_alert(desk["id"], db=desks_db, now=past) == 80
    assert budget.check_alert(desk["id"], db=desks_db) is None                    # this week is still empty
    with desks_db.write() as u:
        chat(u, pid, "desk-chief", 50, 40)
    assert budget.check_alert(desk["id"], db=desks_db) == 80                      # new week, same level, written again
    assert len(alerts(desks_db, pid)) == 2


def test_no_budget_never_warns(desks_db):
    desk = make(desks_db, "Free")
    with desks_db.write() as u:
        chat(u, desk["project_id"], "desk-chief", 10**6, 10**6)
    assert budget.check_alert(desk["id"], db=desks_db) is None
    assert alerts(desks_db, desk["project_id"]) == []


def test_the_warning_title_comes_from_the_catalog_in_both_languages(desks_db):
    desk = make(desks_db, "Bilingual", token_budget=100)
    with desks_db.write() as u:
        chat(u, desk["project_id"], "desk-chief", 50, 50)
    with use_language("es"):
        budget.check_alert(desk["id"], db=desks_db)
    title_es = alerts(desks_db, desk["project_id"])[0].title
    with use_language("en"):
        from hokma.i18n import t
        title_en = t("desks.budget.exhausted", name="Bilingual", pct=100, used=100, budget=100)
    assert "Bilingual" in title_es and title_es != title_en and "desks.budget" not in title_es


def test_unknown_desk_is_not_found(desks_db):
    for call in (budget.usage, budget.state, budget.check_alert):
        with pytest.raises(DeskError) as exc:
            call(999, db=desks_db)
        assert exc.value.code == "not_found" and exc.value.status == 404

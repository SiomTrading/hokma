"""Ian, Sam and Ben (1.2.1) are agents of the fund from the first start and after an update: they are registered from the agents folder of the kit,
start on, take the chat of the owner and work for every desk without joining its team."""
import json
import shutil

from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixture)

from hokma.agents.service import AgentService
from hokma.core.config import hokma_home
from hokma.db import get_database, migrate
from hokma.db.seeds import seed_agents
from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import CREATOR_SERVICES, Caller
from hokma.kit.integrity import app_root


def _install_kit_agents():
    """What the installer does: the kit of the app goes into the working folder."""
    target = hokma_home() / "agents"
    target.mkdir(parents=True, exist_ok=True)
    for f in (app_root() / "kit" / "template" / "agents").glob("*.md"):
        shutil.copy(f, target / f.name)
    return target


def test_after_seeding_the_three_creator_services_are_active_agents(env):
    _install_kit_agents()
    migrate()
    AgentService().reconcile()
    with get_database().read() as u:
        agents = {a.slug: a for a in u.agents.list()}
    for slug in CREATOR_SERVICES:
        assert slug in agents and agents[slug].status == "active" and not agents[slug].is_custom, slug
        assert (hokma_home() / ".claude" / "agents" / f"{slug}.md").is_file(), slug          # loaded: the main agent can launch it
    assert agents["hokma"].status == "active"


def test_an_installation_that_was_on_1_2_0_gets_them_on_the_next_start(env):
    target = _install_kit_agents()
    migrate()
    with get_database().write() as u:                       # 1.2.0: they were never registered
        for slug in CREATOR_SERVICES:
            u.session.execute("DELETE FROM agents WHERE slug = :s", {"s": slug})
            (target / f"{slug}.md").unlink(missing_ok=True)
    from hokma.kit.sync import sync_kit                     # the update puts the missing files of the kit back, then the seeds register them

    sync_kit(app_root() / "kit" / "template", version="1.2.6")
    with get_database().write() as u:
        seed_agents(u.session)
    AgentService().reconcile()
    with get_database().read() as u:
        assert {a.slug for a in u.agents.list() if a.status == "active"} >= set(CREATOR_SERVICES)


def test_from_a_desk_the_seeded_services_take_its_tasks_without_being_on_its_team(desks_db):
    _install_kit_agents()
    with desks_db.write() as u:
        seed_agents(u.session)
        for slug in CREATOR_SERVICES:
            u.agents.set_status(slug, "active")
    desk = make_desk(desks_db)
    for slug in CREATOR_SERVICES:
        text, is_error = hokma_tools.call("task_create", {"title": f"work of {slug}", "project_id": desk["project_id"], "assigned_agent": slug}, Caller({"HOKMA_AGENT": "desk-chief"}))
        assert not is_error, text
        task = json.loads(text)["id"]
        taken, is_error = hokma_tools.call("task_claim", {"task_id": task}, Caller({"HOKMA_AGENT": slug}))
        assert not is_error and json.loads(taken)["claimed_by"] == slug, taken
    with desks_db.read() as u:
        assert {m.agent_slug for m in u.desks.list_agents(desk["id"])}.isdisjoint(CREATOR_SERVICES)        # the team of the desk is not changed

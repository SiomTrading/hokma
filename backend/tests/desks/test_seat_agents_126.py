"""One agent per seat for every desk (task 3427): the state of a desk stays its own, a desk never acts on another, and a desk of 1.2.5 moves to the shared agents
without deleting a file the owner changed."""
from pathlib import Path

import pytest
from dbsupport import SYSTEM, db, env  # noqa: F401  (fixtures)

from hokma.agents import files as agent_files
from hokma.core.config import hokma_home
from hokma.db.team_merge import merge_seat_agents
from hokma.desks import service
from hokma.desks.plantel import SEATS, seats_for
from hokma.desks.swo.create import create_swo_desk, desk_agent_slug
from hokma.kit import hokma_tools


@pytest.fixture
def owner(db):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Test Fund")
    return db


def rows(db, sql, **params):
    with db.read() as u:
        return u.session.query(sql, params)


def make_legacy(db, desk: dict, edited: str | None = None) -> list[str]:
    """Turn a desk made by this version into one of 1.2.5: an agent, a file and a copy in the folder per seat, named after the seat and the desk."""
    desk_id = desk["id"]
    with db.read() as u:
        folder = Path(u.projects.get(desk["project_id"]).path)
        project_id = desk["project_id"]
    kit = hokma_home() / "agents"
    old_slugs = []
    for seat in seats_for("SWO", None):
        old = f"{seat.slug}-{desk_id}"
        text = (kit / f"{seat.slug}.md").read_text(encoding="utf-8")
        for key, value in (("name", old), ("slug", old)):
            text = agent_files.set_frontmatter_value(text, key, value)
        if seat.slug == edited:
            text += "\nThe owner added this line.\n"
        (kit / f"{old}.md").write_text(text, encoding="utf-8")
        (folder / "team" / "agents").mkdir(parents=True, exist_ok=True)
        (folder / "team" / "agents" / f"{seat.slug}.md").write_text(text, encoding="utf-8")
        with db.write() as u:
            u.agents.upsert_kit(old, seat.name, "legacy", "balanced", f"agents/{old}.md", is_custom=True)
            row = u.session.one("SELECT role, model, active, added_at FROM desk_agents WHERE desk_id = :d AND agent_slug = :a", {"d": desk_id, "a": seat.slug})
            u.session.execute("UPDATE desk_agents SET role = 'custom' WHERE desk_id = :d AND agent_slug = :n", {"d": desk_id, "n": seat.slug})
            u.session.execute("INSERT INTO desk_agents (desk_id, agent_slug, role, model, active, added_at) VALUES (:d, :a, :r, :m, :x, :t)",
                              {"d": desk_id, "a": old, "r": row["role"], "m": row["model"], "x": row["active"], "t": row["added_at"]})
            u.session.execute("UPDATE desk_seats SET agent_slug = :o WHERE desk_id = :d AND agent_slug = :n", {"o": old, "d": desk_id, "n": seat.slug})
            u.session.execute("DELETE FROM desk_agents WHERE desk_id = :d AND agent_slug = :n", {"d": desk_id, "n": seat.slug})
        old_slugs.append(old)
    with db.write() as u:
        u.conversations.create(engine="claude", title="chat", project_id=project_id, agent_slug=f"desk-manager-{desk_id}", project_mode="fixed")
        u.scheduled_jobs.create(f"desk-{desk_id}-run-1", f"desk-manager-{desk_id}", "look", "daily", "09:00", "UTC", is_active=False)
        u.tasks.create("analyze", actor=SYSTEM, project_id=project_id, status="ready", assigned_agent=f"market-analyst-{desk_id}")
    return old_slugs


def test_two_desks_do_not_act_on_each_other_with_the_same_agent(owner):
    first = create_swo_desk("One", 10_000, db=owner)
    second = create_swo_desk("Two", 10_000, db=owner)
    assert desk_agent_slug(SEATS["A01"], first["id"], owner) == desk_agent_slug(SEATS["A01"], second["id"], owner) == "market-analyst"
    with owner.write() as u:
        conversation = u.conversations.create(engine="claude", title="chat", project_id=first["project_id"], agent_slug="market-analyst", project_mode="fixed")
    who = hokma_tools.Caller({"HOKMA_AGENT": "market-analyst", "HOKMA_CONVERSATION_ID": str(conversation.id)})
    assert hokma_tools._own_desk_id(who) == first["id"]                         # the desk of the session comes from its project, not from the name of the agent
    hokma_tools._own_desk_only(who, first["id"])
    with pytest.raises(LookupError):
        hokma_tools._own_desk_only(who, second["id"])
    service.set_agent(first["id"], "market-analyst", model="opus", active=False, db=owner)
    mine = rows(owner, "SELECT model, active FROM desk_agents WHERE desk_id = :d AND agent_slug = 'market-analyst'", d=first["id"])[0]
    other = rows(owner, "SELECT model, active FROM desk_agents WHERE desk_id = :d AND agent_slug = 'market-analyst'", d=second["id"])[0]
    assert (mine["model"], mine["active"], other["model"], other["active"]) == ("opus", 0, "sonnet", 1)


def test_a_desk_of_125_moves_to_the_shared_agents_and_keeps_its_state(owner):
    old_desk = create_swo_desk("Old", 10_000, db=owner)
    other = create_swo_desk("Other", 10_000, db=owner)
    service.set_agent(old_desk["id"], "trader", model="opus", db=owner)
    olds = make_legacy(owner, old_desk, edited="market-analyst")
    assert len(olds) == 20
    with owner.write() as u:
        moved = merge_seat_agents(u.session)
    assert len(moved) == 19 and f"market-analyst-{old_desk['id']}" not in moved                  # the seat the owner changed stays on its own agent
    seats = {r["seat_code"]: r["agent_slug"] for r in rows(owner, "SELECT seat_code, agent_slug FROM desk_seats WHERE desk_id = :d", d=old_desk["id"])}
    code = {s.slug: s.code for s in SEATS.values()}
    assert seats[code["market-analyst"]] == f"market-analyst-{old_desk['id']}" and seats[code["trader"]] == "trader" and seats[code["desk-manager"]] == "desk-manager"
    assert desk_agent_slug(SEATS[code["market-analyst"]], old_desk["id"], owner) == f"market-analyst-{old_desk['id']}" and desk_agent_slug(SEATS[code["trader"]], old_desk["id"], owner) == "trader"
    trader = rows(owner, "SELECT role, model, active FROM desk_agents WHERE desk_id = :d AND agent_slug = 'trader'", d=old_desk["id"])
    assert len(trader) == 1 and trader[0]["model"] == "opus" and trader[0]["active"] == 1       # model and switch of the desk travel with the seat
    assert rows(owner, "SELECT count(*) AS n FROM desk_agents WHERE desk_id = :d", d=old_desk["id"])[0]["n"] == 20
    assert rows(owner, "SELECT agent_slug FROM conversations WHERE project_id = :p", p=old_desk["project_id"])[0]["agent_slug"] == "desk-manager"
    assert rows(owner, "SELECT agent_slug FROM scheduled_jobs")[0]["agent_slug"] == "desk-manager"
    assert rows(owner, "SELECT assigned_agent FROM tasks")[0]["assigned_agent"] == f"market-analyst-{old_desk['id']}"        # that seat was not moved
    agents = hokma_home() / "agents"
    assert (agents / f"market-analyst-{old_desk['id']}.md").is_file() and "The owner added this line." in (agents / f"market-analyst-{old_desk['id']}.md").read_text(encoding="utf-8")
    assert not (agents / f"trader-{old_desk['id']}.md").exists() and (agents / "trader.md").is_file()
    with owner.read() as u:
        folder = Path(u.projects.get(old_desk["project_id"]).path)
    assert sorted(p.name for p in (folder / "team" / "agents").glob("*.md")) == ["market-analyst.md"]    # the copies nobody changed are gone, the changed one stays
    assert rows(owner, "SELECT count(*) AS n FROM desk_agents WHERE desk_id = :d", d=other["id"])[0]["n"] == 20
    assert len(merge_seat_agents_again(owner)) == 0                                                # a second run moves nothing


def merge_seat_agents_again(db):
    with db.write() as u:
        return merge_seat_agents(u.session)


def test_a_moved_desk_hides_the_old_agents_and_the_work_continues_on_the_shared_one(owner):
    from hokma.agents.service import AgentService

    desk = create_swo_desk("Old", 10_000, db=owner)
    make_legacy(owner, desk)
    with owner.write() as u:
        assert len(merge_seat_agents(u.session)) == 20
    still = rows(owner, "SELECT status, file_path FROM agents WHERE slug = :s", s=f"trader-{desk['id']}")[0]
    assert still["status"] == "off" and still["file_path"] is None               # kept for the journal that points at it
    listed = {a["slug"] for a in AgentService(owner).list()["agents"]}
    assert "trader" in listed and not [s for s in listed if s.endswith(f"-{desk['id']}")]
    assert [a["agent_slug"] for a in service.list_agents(desk["id"], owner) if a["agent_slug"] == "trader"] == ["trader"]
    assert service.get_desk(desk["id"], owner)["agents"]


@pytest.mark.parametrize("how", ["new", "migrated", "migrated_own_copy"])
def test_the_real_schedule_run_of_the_manager_works_on_a_new_and_a_moved_desk(owner, how):
    from hokma.desks.agents.manager import GRANT, grant_toolbox

    seat = SEATS[GRANT]
    desk = create_swo_desk("Desk", 10_000, db=owner)
    if how != "new":
        make_legacy(owner, desk, edited=seat.slug if how == "migrated_own_copy" else None)
        with owner.write() as u:
            merge_seat_agents(u.session)
    expected = f"{seat.slug}-{desk['id']}" if how == "migrated_own_copy" else seat.slug
    assert desk_agent_slug(seat, desk["id"], owner) == expected
    result = grant_toolbox(desk["id"], db=owner).call("schedule_run", {"expr": "every 6h", "instruction": "Check the book."})   # the real body, no handler replaced
    assert result.ok, (result.error_kind, result.message)
    assert result.data["active"] is False

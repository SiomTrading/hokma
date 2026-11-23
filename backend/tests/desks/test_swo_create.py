"""Creating a SWO desk with its plantel (SPEC-019: B6): rows, team files, atomicity and isolation between desks."""
import json
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from swo_kit_support import build_kit

from hokma.agents import files as agent_files
from hokma.core.config import hokma_home
from hokma.desks import service
from hokma.desks.agents.manager import Mandate, parse_mandate
from hokma.desks.errors import DeskError
from hokma.desks.plantel import SEATS, seats_for
from hokma.desks.swo.create import create_swo_desk, desk_agent_slug


@pytest.fixture
def world(db, tmp_path):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Test Fund")
    skills = {"A07": ["chain-reading"], "A16": ["quality-checks"], "A06": ["quality-checks"]}
    return db, build_kit(tmp_path / "kit", skills=skills)


def agent_count(db):
    return rows(db, "SELECT count(*) AS n FROM agents")[0]["n"]


def rows(db, sql, **params):
    with db.read() as u:
        return u.session.query(sql, params)


def test_a_traditional_desk_has_20_agents_20_seats_and_the_profile_autonomy_and_mandate_rows(world):
    db, kit = world
    out = create_swo_desk("Options SWO", 50_000, kit_root=kit, db=db)
    desk_id = out["id"]
    assert out["template"] == "custom" and (out["line"], out["variant"]) == ("SWO", "traditional")
    seats = rows(db, "SELECT seat_code, agent_slug, line, variant, active FROM desk_seats WHERE desk_id = :d ORDER BY seat_code", d=desk_id)
    assert [s["seat_code"] for s in seats] == [s.code for s in seats_for("SWO", "traditional")] and len(seats) == 20
    assert all(s["line"] == "SWO" and s["variant"] == "traditional" and s["active"] == 1 for s in seats)
    agents = rows(db, "SELECT agent_slug, role, model, active FROM desk_agents WHERE desk_id = :d", d=desk_id)
    assert {a["agent_slug"] for a in agents} == {s["agent_slug"] for s in seats} and len(agents) == 20
    by_slug = {a["agent_slug"]: a for a in agents}
    for seat in seats_for("SWO", "traditional"):
        row = by_slug[desk_agent_slug(seat, desk_id)]
        assert row["role"] == seat.desk_role and row["model"] == seat.default_model and row["active"] == 1
    assert [a["role"] for a in agents].count("chief") == 1
    assert by_slug[f"desk-manager-{desk_id}"]["role"] == "chief"
    profile = rows(db, "SELECT line, variant FROM desk_profile WHERE desk_id = :d", d=desk_id)
    assert [(p["line"], p["variant"]) for p in profile] == [("SWO", "traditional")]
    auto = rows(db, "SELECT level, rules_json, approved_by FROM desk_autonomy WHERE desk_id = :d", d=desk_id)
    assert [(a["level"], a["rules_json"], a["approved_by"]) for a in auto] == [(1, None, None)]
    mand = rows(db, "SELECT spec_json, version, approved_by FROM desk_mandates WHERE desk_id = :d", d=desk_id)
    assert len(mand) == 1 and mand[0]["version"] == 1 and mand[0]["approved_by"] is None
    stored = Mandate.from_json(mand[0]["spec_json"])
    assert stored.line == "SWO" and stored.variant == "traditional" and len(stored.strategies) == 2
    assert out["mandate"] == stored.summary()
    from hokma.exec import levels

    assert levels.get_level(desk_id, db=db) == (1, None)


def test_a_pead_desk_carries_drew_too(world):
    db, kit = world
    out = create_swo_desk("Drift", 20_000, variant="pead", kit_root=kit, db=db)
    codes = {r["seat_code"] for r in rows(db, "SELECT seat_code FROM desk_seats WHERE desk_id = :d", d=out["id"])}
    assert len(codes) == 21 and "A23" in codes
    assert rows(db, "SELECT variant FROM desk_profile WHERE desk_id = :d", d=out["id"])[0]["variant"] == "pead"
    assert "A23" in {s["seat_code"] for s in out["seats"]}


def test_the_mandate_of_the_owner_is_stored_and_marked_approved_when_the_owner_asked(world):
    db, kit = world
    given = {"name": "Mine", "line": "SWO", "variant": "traditional",
             "analysts": [{"key": "options"}, {"key": "market"}],
             "strategies": [{"name": "only", "horizon": "weeks", "models": [{"name": "A16"}]}]}
    out = create_swo_desk("With mandate", 10_000, mandate=given, owner_approved=True, kit_root=kit, db=db)
    row = rows(db, "SELECT spec_json, approved_by FROM desk_mandates WHERE desk_id = :d", d=out["id"])[0]
    assert row["approved_by"] == "owner" and json.loads(row["spec_json"])["name"] == "Mine"
    assert out["analysts"] == ["options", "market"]
    as_object = create_swo_desk("As object", 10_000, mandate=parse_mandate(given), kit_root=kit, db=db)
    assert as_object["id"] != out["id"]


@pytest.mark.parametrize("patch", [{"line": "DT", "variant": None}, {"variant": "pead"}, {"leverage": 2}, {"strategies": []},
                                   {"universe": ["AAPL"]}])
def test_a_bad_or_mismatched_mandate_creates_nothing(world, patch):
    db, kit = world
    given = {"name": "Mine", "line": "SWO", "variant": "traditional",
             "strategies": [{"name": "only", "horizon": "weeks", "models": [{"name": "A16"}]}], **patch}
    with pytest.raises(DeskError) as err:
        create_swo_desk("Nothing", 10_000, mandate=given, kit_root=kit, db=db)
    assert err.value.code == "bad_mandate"
    assert rows(db, "SELECT count(*) AS n FROM desks")[0]["n"] == 0 and rows(db, "SELECT count(*) AS n FROM projects")[0]["n"] == 0


@pytest.mark.parametrize("kwargs,code", [({"variant": "other"}, "bad_template"), ({"capital": 1}, "bad_capital"),
                                         ({"capital": "x"}, "bad_capital"), ({"name": "  "}, "bad_name"),
                                         ({"assets": "AAPL"}, "bad_assets"), ({"assets": ["not a symbol!"]}, "bad_symbol")])
def test_out_of_domain_arguments_are_refused_before_any_write(world, kwargs, code):
    db, kit = world
    args = {"name": "Desk", "capital": 10_000, **kwargs}
    with pytest.raises(DeskError) as err:
        create_swo_desk(args.pop("name"), args.pop("capital"), kit_root=kit, db=db, **args)
    assert err.value.code == code
    assert rows(db, "SELECT count(*) AS n FROM desks")[0]["n"] == 0


def test_a_missing_seat_file_refuses_the_whole_desk_and_leaves_no_trace(world):
    db, kit = world
    (kit / "agents" / "trader.md").unlink()
    seeded = agent_count(db)
    before = sorted(p.name for p in service.projects.projects_root(db).glob("*")) if service.projects.projects_root(db).exists() else []
    with pytest.raises(DeskError) as err:
        create_swo_desk("No trader", 10_000, kit_root=kit, db=db)
    assert (err.value.code, err.value.status) == ("agent_missing", 409)
    assert rows(db, "SELECT count(*) AS n FROM desks")[0]["n"] == 0 and agent_count(db) == seeded
    root = service.projects.projects_root(db)
    assert (sorted(p.name for p in root.glob("*")) if root.exists() else []) == before


@pytest.mark.parametrize("breakage", ["no_frontmatter", "other_line", "bad_skills", "missing_skill", "bad_skill_file"])
def test_a_seat_file_that_is_not_for_a_swo_desk_or_has_a_bad_skill_is_refused(world, breakage):
    db, kit = world
    path = kit / "agents" / "market-analyst.md"
    text = path.read_text(encoding="utf-8")
    if breakage == "no_frontmatter":
        path.write_text("You are Adam.\n", encoding="utf-8")
    elif breakage == "other_line":
        path.write_text(text.replace("lines: [SWO, SWC, LP]", "lines: [DT, LP]"), encoding="utf-8")
    elif breakage == "bad_skills":
        path.write_text(text.replace("skills: []", "skills: [../outside]"), encoding="utf-8")
    elif breakage == "missing_skill":
        path.write_text(text.replace("skills: []", "skills: [not-in-the-kit]"), encoding="utf-8")
    else:
        (kit / "skills" / "chain-reading" / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
    with pytest.raises(DeskError) as err:
        create_swo_desk("Broken", 10_000, kit_root=kit, db=db)
    assert err.value.code == "agent_missing" and rows(db, "SELECT count(*) AS n FROM desks")[0]["n"] == 0


def test_the_team_copies_the_agent_and_skill_files_into_the_folder_of_the_desk(world):
    db, kit = world
    out = create_swo_desk("Team", 10_000, kit_root=kit, db=db)
    with db.read() as u:
        folder = Path(u.projects.get(out["project_id"]).path)
    team = folder / "team"
    assert sorted(p.name for p in (team / "agents").glob("*.md")) == sorted(f"{s.slug}.md" for s in seats_for("SWO", None))
    assert sorted(p.parent.name for p in (team / "skills").glob("*/SKILL.md")) == ["chain-reading", "quality-checks"]
    adam = agent_files.frontmatter_value((team / "agents" / "market-analyst.md").read_text(encoding="utf-8"), "name")
    assert adam == f"market-analyst-{out['id']}"
    assert (folder / "CLAUDE.md").is_file() and "Ana" in (folder / "CLAUDE.md").read_text(encoding="utf-8")
    source = (hokma_home() / "agents" / f"market-analyst-{out['id']}.md").read_text(encoding="utf-8")
    assert agent_files.frontmatter_value(source, "slug") == f"market-analyst-{out['id']}"
    assert agent_files.frontmatter_value(source, "display_name") == "Adam"


def test_each_desk_has_its_own_team_and_a_skill_only_reaches_the_seats_that_declare_it(world):
    db, kit = world
    seeded = agent_count(db)
    first = create_swo_desk("One", 10_000, kit_root=kit, db=db)
    second = create_swo_desk("Two", 10_000, kit_root=kit, db=db)
    slugs = lambda d: {r["agent_slug"] for r in rows(db, "SELECT agent_slug FROM desk_seats WHERE desk_id = :d", d=d)}  # noqa: E731
    assert slugs(first["id"]).isdisjoint(slugs(second["id"]))
    assert agent_count(db) == seeded + 40
    declared = {name for seat, names in {"A07": ["chain-reading"], "A16": ["quality-checks"], "A06": ["quality-checks"]}.items() for name in names}
    with db.read() as u:
        folders = [Path(u.projects.get(d["project_id"]).path) for d in (first, second)]
    for folder in folders:
        on_disk = {p.parent.name for p in (folder / "team" / "skills").glob("*/SKILL.md")}
        assert on_disk == declared
        for agent_file in (folder / "team" / "agents").glob("*.md"):
            text = agent_file.read_text(encoding="utf-8")
            assert "SWO" in text.split("---")[1]


def test_a_failure_in_the_middle_removes_what_this_call_wrote(world, monkeypatch):
    db, kit = world
    seeded = agent_count(db)
    from hokma.desks.swo import create as module

    real = module.service._claude_text
    monkeypatch.setattr(module.service, "_claude_text", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(DeskError) as err:
        create_swo_desk("Failing", 10_000, kit_root=kit, db=db)
    assert err.value.code == "folder_failed"
    monkeypatch.setattr(module.service, "_claude_text", real)
    assert agent_count(db) == seeded
    for table in ("desks", "desk_seats", "desk_agents", "desk_profile", "desk_autonomy", "desk_mandates", "projects"):
        assert rows(db, f"SELECT count(*) AS n FROM {table}")[0]["n"] == 0, table
    assert not list((hokma_home() / "agents").glob("*-1.md")) if (hokma_home() / "agents").exists() else True
    root = service.projects.projects_root(db)
    assert not list(root.glob("*")) if root.exists() else True
    ok = create_swo_desk("Failing", 10_000, kit_root=kit, db=db)
    assert len(ok["seats"]) == 20


def test_deleting_the_desk_row_takes_its_seats_profile_autonomy_and_mandate_with_it(world):
    db, kit = world
    out = create_swo_desk("Remove", 10_000, kit_root=kit, db=db)
    with db.write() as u:
        u.session.execute("DELETE FROM desk_signals WHERE desk_id = :d", {"d": out["id"]})
        u.desks.delete(out["id"])
    for table in ("desk_seats", "desk_agents", "desk_profile", "desk_autonomy", "desk_mandates"):
        assert rows(db, f"SELECT count(*) AS n FROM {table} WHERE desk_id = :d", d=out["id"])[0]["n"] == 0, table

"""The real kit of the SWO desk (SPEC-019: B12): every seat file the plantel names exists, is English, is written for its lines, and a desk is created
from them (TA-41, AH-29, TA-13)."""
import importlib.util
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.desks.agents import analyst_kit as kit
from hokma.desks.agents.manager import load_mandate
from hokma.desks.core.signal import LINES
from hokma.desks.core.skills import parse_skill, split_frontmatter
from hokma.desks.plantel import SEATS, seats_for
from hokma.desks.swo.create import create_swo_desk, default_kit_root

ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / "kit" / "template" / "desk"
_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)


@pytest.fixture
def owner(db):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Test Fund")
    return db


def test_TA_13_the_plantel_has_21_seats_and_every_one_of_their_files_exists_with_its_name_and_slug():
    assert len(SEATS) == 21
    for seat in SEATS.values():
        path = ROOT / seat.kit_file
        assert path.is_file(), seat.kit_file
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"), seat.kit_file)
        assert meta["display_name"] == seat.name and meta["slug"] == seat.slug and meta["name"] == seat.slug
        assert body.strip().startswith("#") or body.strip().startswith("You are"), seat.code
        assert meta["model"] == seat.default_model


def test_TA_13_every_seat_file_is_english_has_no_origin_and_passes_the_repo_rules():
    files = [seat.kit_file for seat in SEATS.values()]
    files += [f"kit/template/desk/skills/{name}/SKILL.md" for name in ("morning-note", "signal-postmortem")]
    assert repo_rules.check(ROOT, files) == []


def test_TA_13_every_seat_file_declares_the_lines_it_works_on_and_a_seat_is_refused_on_any_other_line():
    for seat in SEATS.values():
        meta, _ = split_frontmatter((ROOT / seat.kit_file).read_text(encoding="utf-8"), seat.kit_file)
        lines = meta["lines"]
        assert "SWO" in lines and set(lines) <= set(seat.lines) and set(lines) <= set(LINES), seat.code
        seat_file = kit.load_seat_file(seat.code)
        for line in LINES:
            if line in lines:
                kit._check_line(seat_file, line)
            else:
                with pytest.raises(kit.AnalystKitError):
                    kit._check_line(seat_file, line)


def test_TA_13_the_skills_a_seat_file_declares_all_exist_and_a_skill_enters_only_the_seats_that_declare_it():
    declared: dict[str, set[str]] = {}
    for seat in SEATS.values():
        seat_file = kit.load_seat_file(seat.code)
        for skill in seat_file.skills:
            parse_skill((KIT / "skills" / skill.name / "SKILL.md").read_text(encoding="utf-8"), skill.name)
            declared.setdefault(skill.name, set()).add(seat.code)
    assert declared["morning-note"] == {"A15"} and declared["signal-postmortem"] == {"A15"}
    for name, codes in declared.items():
        for seat in SEATS.values():
            prompt_has = f"## Skill: {name}" in kit.system_prompt(seat.code, {"type": "object", "properties": {}}, line="SWO", desk_id=1,
                                                                    as_of=__import__("datetime").datetime(2026, 1, 2, 15, tzinfo=__import__("datetime").timezone.utc))
            assert prompt_has == (seat.code in codes), (name, seat.code)


@pytest.mark.parametrize("variant, drew", [("traditional", False), ("pead", True)])
def test_TA_13_create_swo_desk_works_against_the_real_kit(owner, variant, drew):
    desk = create_swo_desk(f"Desk {variant}", 100_000, variant=variant, db=owner)
    assert len(desk["seats"]) == (21 if drew else 20) and any(s["seat_code"] == "A15" for s in desk["seats"]) == True
    assert any(s["seat_code"] == "A23" for s in desk["seats"]) is drew
    assert desk["analysts"] == (["market", "news", "fundamentals", "options"] if drew else ["market", "sentiment", "news", "fundamentals", "options"])
    folder = Path(desk["folder"]) if desk.get("folder") else None
    with owner.read() as u:
        agents = u.session.query("SELECT agent_slug FROM desk_seats WHERE desk_id = :d", {"d": desk["id"]})
    assert len(agents) == len(desk["seats"]) and f"desk-manager-{desk['id']}" in {a["agent_slug"] for a in agents}
    if folder is not None:
        team = folder / "team"
        assert (team / "agents" / "desk-manager.md").is_file() and (team / "skills" / "morning-note" / "SKILL.md").is_file()


@pytest.mark.parametrize("name, variant", [("swo-traditional.yaml", "traditional"), ("swo-pead.yaml", "pead")])
def test_AH_29_the_two_example_mandates_create_a_valid_desk_from_the_real_kit(owner, name, variant):
    mandate = load_mandate(default_kit_root() / "mandates" / name)
    assert mandate.line == "SWO" and (mandate.variant or "traditional") == variant
    desk = create_swo_desk("From the example", 50_000, variant=variant, mandate=mandate, db=owner)
    assert desk["mandate"].startswith(mandate.name)
    codes = {s.code for s in seats_for("SWO", variant)}
    assert {m.name for s in mandate.strategies for m in s.models} <= codes
    if variant == "pead":
        assert "A23" in {m.name for s in mandate.strategies for m in s.models} and "future" in mandate.asset_classes

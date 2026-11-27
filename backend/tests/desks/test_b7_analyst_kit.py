"""The analysts of the SWO desk as a group (SPEC-019: B7): agent files, prompts, line scoping, tools and the check of an answer."""
import ast
import importlib.util
import json
from pathlib import Path

import pytest
from b7_support import AS_OF, KIT, ROOT, ScriptedChat, answer, load_registry

from hokma.desks.agents import analyst_kit as kit
from hokma.desks.agents import fundamentals, market, news, options, sentiment
from hokma.desks.core.signal import contains_execution_terms
from hokma.desks.core.skills import parse_skill, split_frontmatter
from hokma.desks.core.tools import ToolNotAllowed
from hokma.desks.plantel import SEATS
from hokma.desks.swo import create

_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)

MODULES = {"A01": market, "A02": sentiment, "A04": news, "A06": fundamentals, "A07": options}
NEW_SKILLS = ("filing-events", "thesis-tracker", "financial-ratios-dcf", "volatility-premium")
MY_FILES = [f"kit/template/agents/{SEATS[c].slug}.md" for c in MODULES] + [f"kit/template/desk/skills/{s}/SKILL.md" for s in NEW_SKILLS]


@pytest.fixture(autouse=True)
def registry_ready():
    load_registry()


def test_every_agent_file_is_in_english_without_origin_or_em_dash_TA_41():
    assert repo_rules.check(ROOT, MY_FILES) == []
    for path in MY_FILES:
        assert "\u2014" not in (ROOT / path).read_text(encoding="utf-8"), path


@pytest.mark.parametrize("code", list(MODULES))
def test_the_agent_file_carries_the_lines_and_skills_the_desk_creation_expects_TA_41(code):
    seat = SEATS[code]
    member = create._load_member(seat, KIT)                    # the same loader the creation of a SWO desk uses
    meta, _ = split_frontmatter(member.text, seat.kit_file)
    assert "SWO" in meta["lines"] and set(meta["lines"]) <= set(seat.lines)
    assert [name for name, _ in member.skills] == meta["skills"]
    assert meta["slug"] == seat.slug and meta["display_name"] == seat.name


@pytest.mark.parametrize("code", list(MODULES))
def test_the_prompt_has_the_rules_the_profile_and_only_the_declared_skills_TA_41(code):
    seat_file = kit.load_seat_file(code)
    text = MODULES[code].system_prompt(line="SWO", desk_id=3, as_of=AS_OF)
    assert "## Rule: no-lookahead" in text and "## Line\nLine SWO" in text and "## Numbers" in text and "## Answer format" in text
    headers = [line[len("## Skill: "):] for line in text.splitlines() if line.startswith("## Skill: ")]
    assert headers == [s.name for s in seat_file.skills] and headers
    assert text.isascii()


@pytest.mark.parametrize("code", list(MODULES))
def test_the_profile_of_an_analyst_holds_no_sizing_exit_or_timing_words_TA_41(code):
    text = kit.base_prompt(code, line="SWO", desk_id=1, as_of=AS_OF)
    assert contains_execution_terms(text) is None


@pytest.mark.parametrize("code,line", [("A01", "DT"), ("A01", "RF"), ("A06", "SWC"), ("A07", "LP"), ("A04", "DT"), ("A02", "RF")])
def test_a_line_that_the_file_does_not_carry_is_refused_so_no_skill_of_another_line_enters_TA_41(code, line):
    with pytest.raises(kit.AnalystKitError):
        MODULES[code].system_prompt(line=line, desk_id=1, as_of=AS_OF)


def test_a_file_edited_to_lose_the_swo_line_is_refused_TA_41(tmp_path):
    root = tmp_path / "desk"
    (root / "agents").mkdir(parents=True)
    text = (KIT.parent / "agents" / "market-analyst.md").read_text(encoding="utf-8").replace("lines: [SWC, SWO, LP]", "lines: [SWC, LP]")
    (root / "agents" / "market-analyst.md").write_text(text, encoding="utf-8")
    with pytest.raises(kit.AnalystKitError):
        market.system_prompt(line="SWO", desk_id=1, as_of=AS_OF, kit_root=root)


def test_a_skill_that_the_file_names_but_the_kit_lacks_is_refused_TA_41(tmp_path):
    root = tmp_path / "desk"
    (root / "agents").mkdir(parents=True)
    (root / "skills").mkdir()
    (root / "agents" / "market-analyst.md").write_text((KIT.parent / "agents" / "market-analyst.md").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(kit.AnalystKitError):
        kit.load_seat_file("A01", root)


@pytest.mark.parametrize("name", NEW_SKILLS)
def test_the_new_skills_follow_the_skill_format_and_are_in_the_manifest_TA_41(name):
    skill = parse_skill((KIT / "skills" / name / "SKILL.md").read_text(encoding="utf-8"), name)
    assert skill.name == name and skill.avoid and skill.patterns
    manifest = json.loads((KIT / "skills" / "skills-manifest.json").read_text(encoding="utf-8"))
    assert manifest["skills"][name] == {"version": skill.version, "digest": skill.digest()}


def test_no_skill_of_an_analyst_reaches_another_seat_file_TA_41():
    declared = {code: {s.name for s in kit.load_seat_file(code).skills} for code in MODULES}
    for a in declared:
        for b in declared:
            if a != b:
                assert not declared[a] & declared[b], (a, b)


@pytest.mark.parametrize("code", list(MODULES))
def test_each_analyst_gets_exactly_the_tools_of_its_seat_and_no_order_tool_TA_15(code):
    ctx = kit.RunContext(1, "AAPL", AS_OF, "SWO")
    box = MODULES[code].toolbox(ctx)
    assert box.names() == SEATS[code].tools
    assert not {"order_place", "exec_propose", "exec_status"} & set(box.names())
    with pytest.raises(ToolNotAllowed):
        box.call("order_place", {})


def test_a_quant_call_outside_the_modules_of_the_seat_is_blocked_TA_15():
    box = market.toolbox(kit.RunContext(1, "AAPL", AS_OF, "SWO"))
    result = box.call("quant_call", {"name": "options.vol_richness", "params": {}})
    assert not result.ok and result.error_kind == "blocked" and "indicators" in result.message
    unknown = box.call("quant_call", {"name": "indicators.nothing_like_this"})
    assert not unknown.ok and unknown.error_kind == "bad_input"


def test_a_tool_argument_that_does_not_exist_is_refused_TA_15():
    box = news.toolbox(kit.RunContext(1, "AAPL", AS_OF, "SWO"))
    result = box.call("data_macro", {"series_id": "CPIAUCSL", "as_of": "2024-06-14", "path": "/etc/passwd"})
    assert not result.ok and result.error_kind == "bad_input"


def test_the_agent_modules_import_no_network_or_disk_library_TA_15():
    banned = {"requests", "httpx", "urllib", "urllib3", "socket", "http", "aiohttp", "subprocess", "pandas", "numpy", "scipy"}
    for code, module in MODULES.items():
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        assert not names & banned, (code, names & banned)
    kit_tree = ast.parse(Path(kit.__file__).read_text(encoding="utf-8"))
    kit_names = {n.module.split(".")[0] for n in ast.walk(kit_tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not kit_names & banned


def test_the_answer_check_rejects_order_fields_and_unverified_numbers_TA_37():
    spec = market.SPEC
    ctx = kit.RunContext(1, "SPY", AS_OF, "SWO")
    evidence = kit.EvidenceLog({"close": 730.83}, None)
    base = {"summary": "Close 730.83.", "trend": {"daily": "up", "weekly": "up"}, "levels": [], "scenarios": [], "screens": [], "data_gaps": ["scenarios"]}
    assert kit.check_value(base, spec, ctx, evidence).problems == ("the weekly trend is stated but indicators.screens.weekly_structure was never called",)
    flat = {**base, "trend": {"daily": "unknown", "weekly": "unknown"}}
    assert kit.check_value(flat, spec, ctx, evidence).valid
    assert not kit.check_value({**flat, "stop_loss": 10}, spec, ctx, evidence).valid
    bad = kit.check_value({**flat, "summary": "Close 731.5."}, spec, ctx, evidence)
    assert not bad.valid and bad.unverified == ("731.5",)
    assert not kit.check_value({**flat, "summary": "Wait for the entry price."}, spec, ctx, evidence).valid


def test_a_degraded_answer_keeps_its_text_and_never_validates_TA_35():
    model = ScriptedChat(answer({"nonsense": 1}))
    result = market.run(model, desk_id=1, symbol="SPY", as_of=AS_OF, snapshot={"close": 1})
    assert result.degraded and not result.valid and result.report.data_gaps == ("structured_output_degraded",)
    assert len(model.calls) == 2                                  # one retry, then the degraded result

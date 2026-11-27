"""1.2.2: any agent or chat may start and carry the creator flow (the main session, a desk chief, any subagent). The order rules stay: no code without a requirement, the one that saved the code
never gives its verdict, no version without an approved verdict, and the hook of the folder lets every agent through. Ian can launch Sam and Ben; the main agent and the desk chief hold the creator tools."""
import json
import subprocess
import sys
from pathlib import Path

from code_support import BRIEF, make_bank, put_files, rows
from code_support import db, env, pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)
from e2e_h4.support import caller, failed, ok

from hokma.core.config import hokma_data_dir, hokma_home
from hokma.db.seeds import parse_frontmatter
from hokma.kit import settings

ROOT = Path(__file__).resolve().parents[2]
SLUG = "any_agent"


def gate(tool, args, agent):
    payload = {"tool_name": tool, "tool_input": args, "cwd": str(hokma_home())}
    if agent:
        payload["agent_type"] = agent
    return subprocess.run([sys.executable, "-I", str(settings.GATE_FILE), "--data", str(hokma_data_dir()), "--workdir", str(hokma_home())], input=json.dumps(payload), capture_output=True, text=True).returncode


def test_the_main_session_and_any_subagent_carry_the_flow_and_nobody_approves_their_own_code(db):
    bank_id = make_bank(db)
    args = {"bank_id": bank_id, "slug": SLUG, "title": "Any agent", "brief": BRIEF}
    assert gate("mcp__hokma__strategy_brief_write", args, None) == 0          # the main session, no subagent
    made = ok("strategy_brief_write", args, caller("hokma"))
    path = put_files(SLUG) / "strategy.py"
    assert gate("Write", {"file_path": str(path)}, "general-purpose") == 0 and gate("Write", {"file_path": str(path)}, None) == 0          # a requirement exists
    saved = ok("strategy_code_save", {"rule_id": made["rule_id"]}, caller("general-purpose"))          # any subagent saves the code
    assert saved["saved"] is True
    version = {"rule_version_id": saved["rule_version_id"], "verdict": "approved"}
    assert failed("strategy_code_verdict", version, caller("general-purpose"))["error_code"] == "blocked"          # the author never approves
    assert rows(db, "SELECT status FROM user_rule_versions")[0]["status"] == "draft"
    done = ok("strategy_code_verdict", version, caller("hokma"))          # another agent: here the main session
    assert done["status"] == "generated"
    assert rows(db, "SELECT status FROM user_rule_versions")[0]["status"] == "generated"


def test_no_code_without_a_requirement_for_any_agent(db):
    make_bank(db, spec=False)
    path = put_files("no_brief") / "strategy.py"                    # the folder of the card exists; its spec.md does not
    assert gate("Write", {"file_path": str(path)}, "hokma") == 2


def tools_of(name):
    return {t.strip() for t in parse_frontmatter((ROOT / "kit" / "template" / name).read_text(encoding="utf-8"))["tools"].split(",")}


def test_the_main_agent_and_the_desk_chief_hold_the_creator_tools_and_ian_launches_sam_and_ben():
    creator = {"mcp__hokma__" + t for t in ("source_ingest", "bank_create", "bank_list", "bank_get", "strategy_brief_write", "strategy_code_save", "strategy_code_verdict", "backtest_optimize", "optimizer_verdict")}
    for name in ("agents/hokma.md", "agents/desk-chief.md"):
        assert creator <= tools_of(name), name
    assert "Agent" not in tools_of("agents/desk-chief.md") and "Agent" in tools_of("agents/hokma.md") and "Agent" in tools_of("agents/strategy-researcher.md")
    for name in ("agents/strategy-coder.md", "agents/strategy-reviewer.md", "agents/strategy-researcher.md"):
        assert not {"mcp__hokma__order_place", "mcp__hokma__order_preview", "mcp__hokma__desk_configure"} & tools_of(name), name
    for name in ("agents/strategy-coder.md", "agents/strategy-reviewer.md", "agents/strategy-researcher.md"):
        assert {"Read", "Write", "Edit", "Glob", "Grep", "Bash", "Agent"} <= tools_of(name), name          # they work like Diego, Joel and Vera: the normal tools of the engine
    rule = "launch strategy-researcher (Ian) right away; Ian writes the requirement, then launches strategy-coder (Sam) and strategy-reviewer (Ben)"
    assert "launch strategy-researcher (Ian) right away" in (ROOT / "kit" / "template" / "agents" / "desk-chief.md").read_text(encoding="utf-8")          # the chief has no dispatch tool: it assigns a task to Ian
    for name in ("CLAUDE.md", "desks/CLAUDE.desk.md"):
        assert rule in (ROOT / "kit" / "template" / name).read_text(encoding="utf-8"), name

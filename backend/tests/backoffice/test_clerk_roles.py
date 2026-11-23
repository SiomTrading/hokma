"""Tool sets by role of the back office clerk (SPEC-018: AFS-003)."""
import re
from pathlib import Path

import pytest

from hokma.backoffice import clerk_roles as roles
from hokma.backoffice.clerk_roles import RoleViolation

ROOT = Path(__file__).resolve().parents[3]
AGENT = ROOT / "kit" / "template" / "service" / "agents" / "back-office-clerk.md"


def access(role):
    return {roles.TOOLS[n].access for n in roles.tools_for(role)}


def test_the_statement_reader_has_no_write_tool_and_no_data_of_its_own():
    assert roles.tools_for("statement_reader") == {"statement_read"}
    assert access("statement_reader") == {roles.THIRD_PARTY}
    for tool in ("recon_run", "report_account", "report_weekly", "export_book", "book_positions", "book_snapshot", "book_pnl", "recon_breaks", "task_complete"):
        with pytest.raises(RoleViolation):
            roles.check("statement_reader", tool)


def test_the_critic_is_read_only_and_reads_the_book():
    assert access("critic") == {roles.OWN_READ}
    assert {"book_positions", "recon_breaks"} <= roles.tools_for("critic")
    for tool in ("recon_run", "report_account", "export_book", "statement_read"):
        with pytest.raises(RoleViolation):
            roles.check("critic", tool)


def test_only_the_formatter_writes_and_the_orchestrator_never_does():
    writers = [r for r in roles.ROLES if roles.WRITE in access(r)]
    assert writers == ["formatter"]
    assert roles.WRITE not in access("orchestrator") and access("orchestrator") == {roles.CONTROL}
    for tool in ("recon_run", "report_account", "report_weekly", "export_book"):
        assert roles.check("formatter", tool).access == roles.WRITE
        with pytest.raises(RoleViolation):
            roles.check("orchestrator", tool)


def test_asking_for_a_tool_outside_the_role_fails_before_anything_runs():
    ran = []
    handlers = {"recon_run": lambda: ran.append("recon_run"), "book_pnl": lambda: ran.append("book_pnl")}
    roles.call("formatter", "recon_run", handlers)
    roles.call("critic", "book_pnl", handlers)
    assert ran == ["recon_run", "book_pnl"]
    with pytest.raises(RoleViolation):
        roles.call("critic", "recon_run", handlers)
    with pytest.raises(RoleViolation):
        roles.call("nobody", "book_pnl", handlers)
    with pytest.raises(RoleViolation):
        roles.call("formatter", "order_place", handlers)
    assert ran == ["recon_run", "book_pnl"]


def test_no_role_holds_an_order_tool_and_the_table_is_sound():
    assert roles.self_check() == []
    everything = set().union(*roles.ROLE_TOOLS.values())
    assert not any(re.search(r"order|trade|place|flatten|propose", name) for name in everything)


def test_the_agent_file_lists_the_tools_of_the_roles_that_have_a_hokma_tool():
    head = AGENT.read_text(encoding="utf-8").split("---")[1]
    listed = set(re.findall(r"mcp__hokma__(\w+)", head))
    wanted = set().union(*roles.ROLE_TOOLS.values()) - {"statement_read"}          # statement_read is the operation the server gives the reader role
    assert listed == wanted

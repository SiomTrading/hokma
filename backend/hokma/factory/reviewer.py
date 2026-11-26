"""Ben, the code reviewer of the strategy creator (SPEC-031 A2): his name, his tools and his model. One copy serves every desk.

He reads the requirement and the code, runs the checks again and gives the verdict that opens or closes the second gate. He works like Vera with the normal tools of the engine, and he has no tool that
reaches a broker, an order or a desk.
"""
from __future__ import annotations

MCP_PREFIX = "mcp__hokma__"
AGENT_SLUG = "strategy-reviewer"
DEFAULT_MODEL = "opus"
DEFAULT_TIER = "deep"
ENGINE_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep", "Bash", "Agent")
TOOLS: tuple[str, ...] = ("strategy_code_get", "strategy_code_list", "strategy_code_verdict", "bank_get",
         "task_show", "task_list", "task_claim", "task_heartbeat", "task_review_approve", "task_request_changes", "task_comment", "bug_report", "bug_update", "bug_list", "task_create", "journal_write", "journal_search", "ips_read")

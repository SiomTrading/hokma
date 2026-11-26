"""Sam, the strategy coder (SPEC-031 A2): his name, his tools and his model. One copy serves every desk.

He writes the test of a strategy first and the strategy after it, as two Python files in strategies/<slug>/, and asks the bank to check and keep them. He has the tools of the
engine to read and write files and no tool that reaches a broker, an order or a desk.
"""
from __future__ import annotations

MCP_PREFIX = "mcp__hokma__"
AGENT_SLUG = "strategy-coder"
DEFAULT_MODEL = "opus"
DEFAULT_TIER = "deep"
ENGINE_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep", "Bash", "Agent")
TOOLS: tuple[str, ...] = ("strategy_code_get", "strategy_code_list", "strategy_code_save", "bank_get", "factory_describe", "library_list",
         "task_show", "task_list", "task_claim", "task_heartbeat", "task_complete", "task_block", "task_comment", "task_request_review", "task_create", "task_link", "task_unblock", "project_show", "journal_write", "journal_search", "ips_read")

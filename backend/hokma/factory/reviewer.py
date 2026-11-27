"""Ben, the reviewer of the logic of the strategies of the Creator (1.3.0, task 3433): his name, his tools and his model. One copy serves every desk.

He reads the spec and the code of a strategy that already passed the checks of Hokma and says whether the logic does what the spec says. He does not repeat the checks. His approval is needed before
paper or real money. He has no tool of the engine to write files and no tool that reaches a broker, an order or a desk.
"""
from __future__ import annotations

MCP_PREFIX = "mcp__hokma__"
AGENT_SLUG = "strategy-reviewer"
DEFAULT_MODEL = "opus"
DEFAULT_TIER = "deep"
ENGINE_TOOLS: tuple[str, ...] = ()
TOOLS: tuple[str, ...] = ("strategy_code_get", "strategy_code_list", "strategy_code_verdict", "bank_get", "bank_list", "backtest_status", "backtest_result", "task_show", "task_list", "task_claim",
                          "task_complete", "task_comment", "task_block", "journal_search")

"""The Creator, the strategy creator of 1.3.0 (task 3433): his name, his tools and his model. One copy serves every desk.

He writes a strategy as text (PARAMS and generate_signals), checks it with strategy_dry_run and creates it with strategy_create: Hokma does the paperwork. He has no tool of the engine to read or
write files and no tool that reaches a broker, an order or a desk.
"""
from __future__ import annotations

MCP_PREFIX = "mcp__hokma__"
AGENT_SLUG = "strategy-creator"
DEFAULT_MODEL = "opus"
DEFAULT_TIER = "deep"
ENGINE_TOOLS: tuple[str, ...] = ()
TOOLS: tuple[str, ...] = ("strategy_dry_run", "strategy_create", "creation_report", "strategy_code_get", "strategy_code_list", "source_ingest", "bank_list", "bank_get", "backtest_run",
                          "backtest_status", "backtest_result", "backtest_optimize", "backtest_walkforward", "backtest_montecarlo", "backtest_evidence", "optimizer_verdict", "optimizer_verdict_get",
                          "audit_thresholds_get", "data_bars", "data_snapshot", "library_list", "task_show", "task_list", "task_claim", "task_complete", "task_comment", "journal_search", "ips_read")

# Where is what

Find the place before you change something. Verify with the tests of that place, not by reading only.

| I need | Look in | Check with |
|---|---|---|
| A metric (Sharpe, drawdown, alpha) | `backend/hokma/quant/metrics/` | `uv run pytest backend/tests/quant/metrics` |
| A risk figure (VaR, CVaR, stress) | `backend/hokma/quant/risk/` | `uv run pytest backend/tests/quant/risk` |
| Overfitting evidence (PSR, DSR, PBO, purged splits) | `backend/hokma/quant/validation/` | `uv run pytest backend/tests/quant/validation` |
| Limits on sizes and time of a quant call | `backend/hokma/quant/limits.py` | `uv run pytest backend/tests/quant/test_limits.py` |
| Market, fundamental, macro or SEC data | `backend/hokma/data/` | `uv run pytest backend/tests/data` |
| The signal contract, graph, prompts, safety | `backend/hokma/desks/core/` | `uv run pytest backend/tests/desks` |
| Rules, recipes and templates that desks receive | `kit/template/desk/` | `uv run pytest backend/tests/desks/test_prompt_integrity.py` |
| Session commands (checkpoint, catchup) | `kit/template/commands/` | `uv run pytest backend/tests/desks/test_prompt_integrity.py` |
| A model client or a structured answer | `backend/hokma/llm/` | `uv run pytest backend/tests/llm` |
| A lesson of a desk | `backend/hokma/lessons/` and migration `0021_lessons.sql` | `uv run pytest backend/tests/lessons` |
| An agent tool for quant or data | `backend/hokma/kit/quant_tools.py`, `data_tools.py` | `uv run pytest backend/tests/test_quant_data_tools.py` |
| The terminal command | `cli/hokma_cli/quant_cmd.py` | `uv run pytest backend/tests/test_quant_cli.py` |
| Why a number differs between runs | `docs/reproducibility.md` | rerun with the same seed and snapshot hash |
| Rules everyone follows | `docs/architecture/engineering-rules.md` | `python scripts/check_repo_rules.py` |

## Verification, not only tests

A change is verified when the behaviour it promises is observed: the function is in the registry with its piece ids, the tool answers through the MCP layer with fixture data, the migration runs on SQLite and Postgres, the generated file passes the repository rules. A green test that never touched the changed path is not verification.

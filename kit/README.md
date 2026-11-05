# Hokma kit

What the installer copies into the owner's fund folder, plus the skills the agents use.

## Contents

| Path | What it is |
|---|---|
| `template/HOKMA.md` | Fund instructions: tone, routing, tasks, autonomy, IPS, fixed rules. Provider-neutral |
| `template/CLAUDE.md` | One line that loads `HOKMA.md` |
| `template/docs/learn/README.md` | Short guide for the owner and the agents: Hokma, agents, names, update, doctor |
| `template/agents/` | The 7 fund agents and Joel's 3 subagents |
| `skills-manifest.json` | Every skill the agents use, who uses it and how it is installed |
| `skills/` | Skills shipped with the kit (`bundle`) |

## Agents

| Agent | Role | Model tier | Turns on |
|---|---|---|---|
| Hokma | Orchestrator: routing, parallel dispatch, tasks, journal, decisions | balanced | Base kit |
| Diego | Requirements, specs, plans, machine-time estimates, delivery validation | balanced | Base kit |
| Joel | Strategies, indicators and robots (NinjaScript, Pine Script, thinkScript, Python), fund tools | balanced | Base kit |
| joel-dev-python | Python scripts, local backtests, reports, services | balanced | Base kit, with Joel |
| joel-dev-frontend | Custom screens and components | balanced | Base kit, with Joel |
| joel-mcp-builder | Custom MCP servers | balanced | Base kit, with Joel |
| Vera | QA of code and strategies, backtest validity, IPS rules | balanced | Base kit |
| Axel | Publishing to the fund repository and simulation activation, only with Vera's approval | fast | Later release |
| Hugo | Data, backtest metrics and performance | fast | Later release |
| Iris | Market research, papers, platform documentation | balanced | Later release |

## Skills

- **bundle:** shipped inside `skills/{id}/` and installed with the kit.
- **fetch:** downloaded by the installer on the owner's computer from its official source,
  resolved through the Hokma skills registry. Never shipped in the kit.

The full list is in `skills-manifest.json`: 82 bundle, 11 fetch.

## Hokma MCP tools the agents call

`task_*`, `project_*`, `journal_*`, `bug_*`, `decision_*`, `schedule_*`, `robot_status`,
`robot_set_state` (simulation only), `ips_read`, `extension_request`, `agents_list`.

These tools are provided by the Hokma MCP server.

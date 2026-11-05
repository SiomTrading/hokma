# Learn Hokma

Hokma is an installable harness: a team of AI agents that works for
the personal investment fund of {{owner_name}}. It builds and tests strategies, indicators and robots, keeps a journal and a task list,
and never trades real money on its own.

## The agents

| Agent | What it does |
|---|---|
| Hokma | Receives every message, routes the work, keeps the journal |
| Diego | Turns an idea into a spec and tasks |
| Joel | Builds strategies, indicators and fund code |
| Vera | Validates all code and every strategy |
| Axel | Publishes, only with the approval of Vera |
| Hugo | Data, backtest metrics and performance |
| Iris | Market research and papers |

## Names

The owner name and the fund name are asked the first time `hokma start` runs in a terminal. To change
them, run `hokma owner` in a terminal. Files the owner did not edit are refreshed; files the owner edited
stay as they are, and the new version is written next to them with the suffix `.hokma-new`.

## Commands

- `hokma update` brings the latest Hokma. The work of the owner is never touched.
- `hokma doctor` checks that the installation is healthy.

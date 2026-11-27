"""Tools of the strategy creator of 1.3.0 (task 3433): the measure of a creation and, in the next steps of this version, the dry run and the creation of a strategy written in pandas.

Who calls is set by the code from the environment of the engine, never by an argument. Nothing here reaches a broker, an order or a desk.
"""
from __future__ import annotations

from typing import Any

from hokma.kit.backtest_tools import _dump, _i, _s
from hokma.kit.source_tools import _guarded
from hokma.kit.tool_registry import ToolSet

TOOLSET = ToolSet()


@TOOLSET.tool("creation_report", "The cost of a strategy creation against the baseline of 1.2.8 (Sam: 15 min 03 s, 75 calls, 254,880 tokens): seconds, calls, dry runs and tokens. strategy_create already "
              "gives it when it finishes; call this later to read the tokens of the last turn, which are saved when the turn ends. Without creation_id it is the last creation of this conversation.", {
    "creation_id": _i("The id of the measure that strategy_create gave (default: the last creation of this conversation).")})
@_guarded
def creation_report(u, c, a: dict) -> str:
    from hokma.factory import creation_metrics

    return _dump(creation_metrics.report(a.get("creation_id"), conversation_id=getattr(c, "conversation_id", None)))


LINES = ("RF", "DT", "SWC", "SWO", "LP")


@TOOLSET.tool("strategy_dry_run", "Check a strategy written in pandas before keeping it. Give the code as text (PARAMS and generate_signals(df, params)); nothing is saved and you give no path. It runs on a "
              "fixed table of 10 sessions and checks, in this order: that it has the format and loads, that it RUNS (default parameters and every parameter at its min and at its max), "
              "that it DECIDES (at least one entry), that it is DETERMINISTIC and that it does NOT LOOK AT THE FUTURE (30 to 50 random cuts plus every change of session). Each problem has its rule, its line or "
              "field and the exact change. df has open, high, low, close, volume, session_id (the Globex session that starts at 18:00 New York) and is_rth (9:30 to 16:00 New York). The answer is a DataFrame "
              "with entry_long, entry_short, exit_long, exit_short and stop (a price fixed on the bar of the signal), and optionally target and limit_price. The signal is made at the close and the order goes at "
              "the next open.", {
    "code": _s("The text of the strategy."), "spec": _s("The spec in text (optional here, required by strategy_create)."), "timeframe": _s("The bar of the sample, such as 1m, 5m or 15m (default 5m): it is a parameter of the run, not of the strategy.")},
              ["code"])
@_guarded
def strategy_dry_run(u, c, a: dict) -> str:
    from hokma.factory import creation

    _text, report = creation.dry_run(a["code"], a.get("timeframe"))
    return _dump({**report.to_data(), "next": "ok: call strategy_create with the same code" if report.ok else "fix the problems (each says the change) and call strategy_dry_run again"})


@TOOLSET.tool("strategy_create", "Create a strategy written in pandas from text, with ALL the paperwork done by Hokma: it runs the checks again (nothing is saved if they fail and you get the problems), then makes the card of the bank "
              "and its number STR-NNN, the folder with spec.md, checklist.md, plan.md, tasks.md and versions/v1/strategy.py, the requirement, the saved version linked to its spec, the tasks (the review of Ben and "
              "the backtest) and the journal entry. You give the spec and the code as text, never a path. The strategy is ready for backtest_run and backtest_optimize at once (the optimizer walks PARAMS "
              "when you give no space); Ben reviews the logic against the spec before paper or real money, and the owner approves real money. With bank_id it saves the next version of that strategy. The answer "
              "carries the measure of the creation against the baseline of 1.2.8: show it to the owner.", {
    "title": _s("Name of the strategy, 1 to 160 characters."), "summary": _s("What it does in one or two sentences, 10 to 1000 characters."),
    "spec": _s("The spec in text (markdown): what it trades, entry, exit, stop, session, parameters and what it must not do."), "code": _s("The text of the strategy: PARAMS and generate_signals(df, params)."),
    "line": _s("RF, DT, SWC, SWO or LP (default DT).", enum=list(LINES)), "market": _s("The market, such as NQ."), "timeframe": _s("The bar the dry run used, such as 5m (default 5m)."),
    "bank_id": _i("Save the next version of this strategy of the bank instead of a new one.")}, ["title", "summary", "spec", "code"], writes=True)
@_guarded
def strategy_create(u, c, a: dict) -> str:
    from hokma.factory import creation
    from hokma.kit.source_tools import _caller, own_desk

    return _dump(creation.create(title=a["title"], summary=a["summary"], spec=a["spec"], code=a["code"], caller=_caller(c), origin=own_desk(c), conversation_id=getattr(c, "conversation_id", None),
                                 line=a.get("line"), market=a.get("market"), timeframe=a.get("timeframe"), bank_id=a.get("bank_id")))


TOOLS = TOOLSET.tools

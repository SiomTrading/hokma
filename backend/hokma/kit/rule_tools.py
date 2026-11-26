"""Tools of the strategy creator for the code (SPEC-031 A2, section 5): the requirement, the save, the verdict, the list and the reading of a strategy written as code.

They are tools of the bank, not of programming: the coder writes its two files with the tools of the engine and asks here to check them and keep them. Who calls
and where the strategy is created (Hokma or a desk) are set by the code from the environment of the engine, never by an argument. Nothing here reaches a broker,
an order or a desk: the strategy ends in the bank, and only the owner can approve it for real money.
"""
from __future__ import annotations

from typing import Any

from hokma.kit.backtest_tools import _b, _dump, _i, _o, _s
from hokma.kit.source_tools import _caller, _guarded, own_desk
from hokma.kit.tool_registry import ToolSet

TOOLSET = ToolSet()
STAGES = ("draft", "failed", "generated", "owner_reviewed", "rejected", "retired")
FINDING = {"type": "object", "description": "severity (blocker, major, minor or nit), message and optionally line.", "properties": {
    "severity": {"type": "string", "enum": ["blocker", "major", "minor", "nit"]}, "message": {"type": "string"}, "line": {"type": "integer"}},
    "required": ["severity", "message"], "additionalProperties": False}


@TOOLSET.tool("strategy_brief_write", "Register in the bank the spec of a strategy that will be programmed. The requirement itself is the numbered spec.md (spec-four-phases): write projects/{project}/specs/{NNN}-{slug}/spec.md "
              "with Write first, then call this with bank_id, slug and title the first time (the strategy rule is created with its folder) or rule_id for the next version. The brief is only its summary; the "
              "path of the spec is found and kept. Without that spec.md no code is saved.", {
    "bank_id": _i("The strategy of the bank that needs code (first requirement)."), "slug": _s("Lower case letters, digits and underscores, 2 to 40 characters (first requirement)."),
    "title": _s("Name of the strategy, 1 to 160 characters (first requirement)."), "rule_id": _i("The rule of an earlier requirement, to write its next version."),
    "brief": _o("summary (the spec in one or two sentences) and optionally quote (the locator of the source, never its text), market and timeframe.")}, ["brief"], writes=True)
@_guarded
def strategy_brief_write(u, c, a: dict) -> str:
    from hokma.factory import user_rules

    user_rules.require_role(_caller(c), user_rules.RESEARCHER, "the requirement")          # the slug is the one the engine set in the environment, checked before anything runs
    made = user_rules.write_brief(bank_id=a.get("bank_id"), slug=a.get("slug"), title=a.get("title"), rule_id=a.get("rule_id"), brief=a["brief"], caller=_caller(c), origin=own_desk(c))
    return _dump({**made, "next": "Sam reads the spec.md and takes the tasks; he writes test_strategy.py first and strategy.py after it in the folder, and checks them with strategy_code_save and dry_run true"})


@TOOLSET.tool("strategy_code_save", "Check and keep the strategy that was written in service/strategies/STR-{NNN}-{slug}/versions/v{N}/ (strategy.py and test_strategy.py): the static check, its own tests and the fixed tests of the "
              "future and of repetition run in a process apart. Green: the code is saved as a draft of the next version with its hash and its requirement. Red: nothing is saved and the answer "
              "is the output of the checks. With dry_run true the checks run and nothing is saved, even when green: use it to see the tests fail first and pass later. Without a current "
              "requirement the answer is blocked. The reviewer rejected it three times: blocked.", {
    "rule_id": _i("The strategy rule."), "bank_id": _i("Or the strategy of the bank (its rule is found)."), "version": _i("The version written in versions/v{N}/ (default: the next one)."),
    "dry_run": _b("true: run the checks on the folder and store nothing.")}, [], writes=True)
@_guarded
def strategy_code_save(u, c, a: dict) -> str:
    from hokma.factory import user_rules

    user_rules.require_role(_caller(c), user_rules.CODER, "saving code")
    rule_id = a.get("rule_id")
    if rule_id is None:
        rule_id = user_rules.rule_of_bank(a.get("bank_id"))
    return _dump(user_rules.save_code(rule_id=rule_id, caller=_caller(c), dry_run=bool(a.get("dry_run", False)), origin=own_desk(c), version=a.get("version")))


@TOOLSET.tool("strategy_code_verdict", "The verdict of the code reviewer on a draft: approved or rejected, with findings (severity blocker, major, minor or nit, a message and the line). A rejection "
              "needs findings and sends the draft back to the coder (three rejections stop the strategy). An approval needs no blocker or major finding and the checks green on this same code "
              "(they run again here): it generates the version, adds the version of the bank with its origin and gives the spec that the optimizer needs, all together.", {
    "rule_version_id": _i("The draft that is judged."), "verdict": _s("approved or rejected.", enum=["approved", "rejected"]),
    "findings": {"type": "array", "items": FINDING, "description": "Required when rejecting."}}, ["rule_version_id", "verdict"], writes=True)
@_guarded
def strategy_code_verdict(u, c, a: dict) -> str:
    from hokma.factory import user_rules

    user_rules.require_role(_caller(c), user_rules.REVIEWER, "the verdict")
    return _dump(user_rules.verdict(rule_version_id=a["rule_version_id"], verdict=a["verdict"], findings=a.get("findings"), reviewer=_caller(c)))


@TOOLSET.tool("strategy_code_list", "List the strategies written as code with their stage (draft, failed, generated, owner_reviewed, rejected or retired), their registry id and where they were created "
              "(hokma or a desk). Reading only.", {
    "bank_id": _i("Only the strategies of this bank strategy."), "stage": _s("Only this stage.", enum=list(STAGES)),
    "origin": _s("hokma: created in the main chat; desk: created in a desk.", enum=["hokma", "desk"]), "limit": _i("Rows, 1 to 200 (default 50).")})
@_guarded
def strategy_code_list(u, c, a: dict) -> str:
    from hokma.factory import user_rules

    return _dump(user_rules.list_code(bank_id=a.get("bank_id"), stage=a.get("stage"), origin=a.get("origin"), limit=a.get("limit", 50)))


@TOOLSET.tool("strategy_code_get", "Read a strategy written as code, with or without code yet: its folder, its numbered spec, its files, its current requirement, its code and its test file, the output of its checks, the verdicts, the version of the bank and the "
              "spec that stand for it, and where it was created. Give rule_version_id, registry_id or rule_id (the last version). With rerun true the checks run again on the saved copy "
              "and the answer carries their fresh output: this is how the reviewer runs the tests.", {
    "rule_version_id": _i("A version."), "registry_id": _s("Or its registry id, like u_inside_bar_v1."), "rule_id": _i("Or the rule, for its last version."),
    "bank_id": _i("Or the strategy of the bank: its rule, even when it has no code yet."), "slug": _s("Or its slug."),
    "rerun": _b("true: run the checks again on the saved copy.")})
@_guarded
def strategy_code_get(u, c, a: dict) -> str:
    from hokma.factory import user_rules

    return _dump(user_rules.get_code(rule_version_id=a.get("rule_version_id"), rule_id=a.get("rule_id"), registry_id=a.get("registry_id"), rerun=bool(a.get("rerun", False)),
                                      bank_id=a.get("bank_id"), slug=a.get("slug")))


TOOLS = TOOLSET.tools

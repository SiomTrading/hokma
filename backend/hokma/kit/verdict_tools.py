"""Tools of the verdict of the optimizer for the agents (SPEC-032, sections 9.2 and 11): `optimizer_verdict`, `optimizer_verdict_get`, `audit_thresholds_get`,
`library_list` and `library_request`.

They take ids and values and never a path. The actor of what they write is the agent that calls (`c.agent`), set by the code and never by an argument. There is no tool
that takes a check out of a verdict, edits a limit or answers a question to the owner: those belong to the owner, in the screen. The verdict is advice (`advisory` is always
true): no tool here changes the state of anything because of it. Failures come back classified: `no_data` (no finished run, no verdict, no walk forward), `stale` (the bars
changed since the walk forward), `bad_input` (a value, a line with no limits, a strategy with no saved spec) and `unavailable` (the engine of this machine). No road to a
broker, an order or a desk lives here: the evaluation only runs history.
"""
from __future__ import annotations

import functools
import json
from collections.abc import Callable
from typing import Any

from hokma.kit import backtest_tools
from hokma.kit.tool_registry import ToolError, ToolSet

TOOLSET = ToolSet()
MAX_LIBRARY = 200


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _s(desc: str = "", **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **extra}


def _i(desc: str = "") -> dict[str, Any]:
    return {"type": "integer", "description": desc}


def _o(desc: str = "") -> dict[str, Any]:
    return {"type": "object", "description": desc}


def _a(desc: str = "") -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


def classified(exc: Exception) -> ToolError | None:
    """The classified error of an exception of the verdict, of its bridge or of the backtest service, or None when it is a bug (it then propagates)."""
    from hokma.verdict import service

    if isinstance(exc, ToolError):
        return exc
    found = service.failure_of(exc)
    if found is None:
        return None
    suggestions = {"no_run": ["backtest_run"], "no_walk_forward": ["backtest_walkforward"], "no_verdict": ["optimizer_verdict"]}.get(found.code, [])
    try:
        return ToolError(found.kind, found.message, suggestions)
    except ValueError:                                                  # a class of the backtest tools that is not one of the codes of the registry
        return backtest_tools.ClassifiedError(found.kind, found.message, suggestions)


def safe(fn: Callable[[Any, Any, dict], str]) -> Callable[[Any, Any, dict], str]:
    """A tool handler whose known failures come back as classified errors; the rest propagate, because they are bugs and not answers."""
    @functools.wraps(fn)
    def wrapper(u: Any, c: Any, a: dict) -> str:
        try:
            return fn(u, c, a)
        except ToolError:
            raise
        except Exception as exc:
            error = classified(exc)
            if error is None:
                raise
            raise error from None
    return wrapper


def _version(a: dict) -> int:
    return backtest_tools._whole(a["version"], "version", 1, 100_000)


def _ident(a: dict) -> str:
    return backtest_tools._text_id(a["strategy_id"], "strategy_id")


# ------------------------------------------------------------------------------------------------------------------------- the tools
@TOOLSET.tool("optimizer_verdict", "Evaluate a version of a strategy and save the verdict of the optimizer: the 24 checks of the audit, the three sentences, the changes it suggests to the "
              "creator (each with a stable action code) and the ideal result (the centre of the widest plateau, only when it holds its neighbors and the walk forward). It needs a "
              "finished run; without a walk forward it asks for evidence. It can run the neighbors of the parameters (backtests, counted once in the ledger). The verdict is advice: "
              "advisory is always true and it changes nothing by itself.", {
    "strategy_id": _s("Id of the strategy (the one of its saved spec)."), "version": _i("Version of the saved spec.")}, ["strategy_id", "version"], writes=True)
@safe
def optimizer_verdict(u, c, a: dict) -> str:
    from hokma.factory import verdict
    from hokma.verdict import service

    svc = backtest_tools.services()
    strategy_id, version = _ident(a), _version(a)
    with backtest_tools._slot():
        found = verdict.evaluate(strategy_id, version, actor=c.slug, services=svc)
    return _dump(service.payload(found, language="en", services=svc))


@TOOLSET.tool("optimizer_verdict_get", "Read the verdict of a version of a strategy without running anything: the latest one, or the one with verdict_id. Same fields as optimizer_verdict.", {
    "strategy_id": _s("Id of the strategy."), "version": _i("Version of the saved spec."), "verdict_id": _i("A verdict id of that version; the latest when it is left out.")},
              ["strategy_id", "version"])
@safe
def optimizer_verdict_get(u, c, a: dict) -> str:
    from hokma.factory import verdict
    from hokma.verdict import service

    svc = backtest_tools.services()
    strategy_id, version = _ident(a), _version(a)
    if a.get("verdict_id") is not None:
        found = verdict.get(backtest_tools._whole(a["verdict_id"], "verdict_id"), db=svc.database())
        if (found.strategy_id, found.strategy_version) != (strategy_id, version):
            raise ToolError("bad_input", f"verdict {found.id} belongs to {found.strategy_id} version {found.strategy_version}")
    else:
        found = verdict.latest(strategy_id, version, db=svc.database())
        if found is None:
            raise ToolError("no_data", f"no_verdict: {strategy_id} version {version} has no verdict yet", ["optimizer_verdict"])
    return _dump(service.payload(found, language="en", services=svc))


@TOOLSET.tool("audit_thresholds_get", "The limits of the audit for a line (DT, SWC, SWO, LP or RF): the values in force, their version, who set them and the 13 measures with their range, "
              "the checks each one decides and a short description. Read only: only the owner changes them, in the screen.", {
    "line": _s("Line of the strategy.", enum=["DT", "SWC", "SWO", "LP", "RF"])}, ["line"])
@safe
def audit_thresholds_get(u, c, a: dict) -> str:
    from hokma import i18n
    from hokma.factory import thresholds
    from hokma.verdict import service

    current = thresholds.current(a["line"], db=backtest_tools.services().database())
    catalog = i18n.catalog("en")
    measures = [{"key": m.key, "kind": m.kind, "low": m.low, "high": m.high, "step": m.step, "effect": m.effect, "checks": list(m.checks),
                 "value": current.values.get(m.key), "description": catalog[f"verdict.measure.{m.key}"]} for m in thresholds.MEASURES]
    return _dump({**service.threshold_dict(current), "measures": measures})


@TOOLSET.tool("library_list", "List what a strategy can use: indicators (ready, or existing in H3 but not wired yet), alphas, sizers, exits, execution parts and filters, and the open requests "
              "for an indicator that is missing. Sorted by kind and name.", {
    "kind": _s("Only this kind.", enum=["indicator", "alpha", "sizer", "exit", "execution", "filter"]),
    "status": _s("Only this status.", enum=["ready", "h3_only", "requested"]),
    "limit": _i(f"Rows to return, 1 to {MAX_LIBRARY} (default 50).")})
@safe
def library_list(u, c, a: dict) -> str:
    from hokma.factory import library_catalog

    limit = backtest_tools._whole(a.get("limit", 50), "limit", 1, MAX_LIBRARY)
    rows = library_catalog.catalog(kind=a.get("kind"), status=a.get("status"), db=backtest_tools.services().database())
    return _dump({"total": len(rows), "items": [r.as_dict() for r in rows[:limit]]})


@TOOLSET.tool("library_request", "Ask for an indicator the library does not have. The request is saved with your name and stays open until the owner or a new version of the kit resolves "
              "it (wired, built, user rule or declined). A name that already exists and is ready is refused: use it as it is. A name that exists in H3 is saved and the answer says it "
              "only needs wiring.", {
    "name": _s("Name of the indicator (1 to 48 characters)."), "description": _s("What it measures and why the strategy needs it (20 to 2000 characters)."),
    "fields": _a("Bar fields it reads: open, high, low, close, volume."), "params": _o("Its parameters with a plain value each, for example {\"length\": 14}."),
    "bank_version_id": _i("The version of the strategy bank that needs it, when there is one.")}, ["name", "description", "fields", "params"], writes=True)
@safe
def library_request(u, c, a: dict) -> str:
    from hokma.factory import library_catalog

    name = a["name"]
    made = library_catalog.request(name, description=a["description"], fields=a["fields"], params=backtest_tools._params(a["params"]), bank_version_id=a.get("bank_version_id"), caller=c.slug,
                                   db=backtest_tools.services().database())
    notice = library_catalog.request_notice(name)
    return _dump({"request_id": made, "status": "open", "requested_by": c.slug, **({"notice": notice} if notice else {})})


TOOLS = TOOLSET.tools

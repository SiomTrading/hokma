"""Tools of the SWO desk: create it, run its cycle, read its state and its signals, and propose lessons (SPEC-019, B13, section 4.6).

H3 adds no tool of calculation: the five quant modules are called with `quant_call` (H1). These tools only open the doors of the desk services.
Every tool validates its sizes before it does anything, takes no path and no pattern from the agent, and works only on the desk of the caller.
A tool never decides a control: the line, the effect, the origin, the prices and the risk of what the cycle proposes are derived by the
code of the cycle and of `hokma.exec`, and the cycle hands its proposal to `exec.submit`, which applies the filter and the level of the desk.
Nothing here calls a broker. Failures are classified (`no_data`, `unavailable`, `bad_input`, `blocked`, `refused`).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from hokma.kit.tool_registry import ToolError, ToolSet

TOOLSET = ToolSet()
MAX_NAME = 80
MAX_ASSETS = 50
MAX_MANDATE_BYTES = 12_000
MAX_LESSONS = 10
MAX_LESSON_CHARS = 1000
MAX_ROWS = 100
MAX_SYMBOL = 40
CLOCK_SKEW = timedelta(minutes=5)


def _s(desc: str = "", **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **extra}


def _i(desc: str = "") -> dict[str, Any]:
    return {"type": "integer", "description": desc}


def _o(desc: str = "") -> dict[str, Any]:
    return {"type": "object", "description": desc}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _desk(c: Any, desk_id: Any) -> int:
    """The desk of the call: a whole number, and the desk of the project of a caller that works inside a desk."""
    from hokma.kit import hokma_tools

    if isinstance(desk_id, bool) or not isinstance(desk_id, int) or desk_id < 1:
        raise ToolError("bad_input", "desk_id must be a desk id", ["desk_list"])
    try:
        hokma_tools._own_desk_only(c, desk_id)
    except LookupError as exc:
        raise ToolError("blocked", str(exc)) from None
    return desk_id


def _swo_desk(c: Any, desk_id: Any) -> int:
    """A desk of this tool family is a SWO desk: any other line has its own cycle."""
    from hokma.db import get_database

    desk = _desk(c, desk_id)
    with get_database().read() as u:
        row = u.session.one("SELECT line, variant FROM desk_profile WHERE desk_id = :d", {"d": desk})
    if row is None or row["line"] != "SWO":
        raise ToolError("bad_input", f"desk {desk} is not a SWO desk", ["desk_list"])
    return desk


def _symbol(value: Any) -> str:
    from hokma.market.yahoo import valid_symbol

    if not isinstance(value, str) or not 1 <= len(value.strip()) <= MAX_SYMBOL or not valid_symbol(value.strip()):
        raise ToolError("bad_input", "symbol must be the symbol of a share, for example AAPL")
    return value.strip().upper()


def _as_of(value: Any) -> datetime:
    """The cut-off of a cycle. Never in the future: a cycle that reads past the clock would read what is not yet public."""
    from hokma.data.sentinels import parse_time

    now = datetime.now(timezone.utc)
    if value is None:
        return now
    try:
        cut = parse_time(value)
    except ValueError as exc:
        raise ToolError("bad_input", f"as_of: {exc}", ["Use an ISO date such as 2026-01-31T15:00:00Z"]) from None
    cut = cut if cut.tzinfo else cut.replace(tzinfo=timezone.utc)
    if cut > now + CLOCK_SKEW:
        raise ToolError("bad_input", "as_of cannot be in the future")
    return cut


def _int_id(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ToolError("bad_input", f"{name} must be a whole number of 1 or more")
    return value


def _is_desk_agent(c: Any) -> bool:
    """Whether the caller is an agent of some desk (a seat of a plantel or a desk chief): those never create desks."""
    from hokma.db import get_database
    from hokma.kit import hokma_tools

    agent = getattr(c, "agent", None)
    if not agent:
        return False
    if agent == hokma_tools.DESK_CHIEF:
        return True
    with get_database().read() as u:
        return u.session.one("SELECT 1 AS x FROM desk_agents WHERE agent_slug = :a", {"a": agent}) is not None


def _plain_error(exc: Exception) -> ToolError:
    """A desk or cycle failure as a classified error. The text is the stable code plus the localized message when there is one."""
    from hokma.broker.service import BrokerError
    from hokma.desks.errors import DeskError

    if isinstance(exc, (DeskError, BrokerError)):
        code = "no_data" if getattr(exc, "code", "") in ("not_found", "desk_not_found") else "bad_input"
        return ToolError(code, f"{exc.code}: {exc.message}")
    return ToolError("bad_input", str(exc)[:300])


@TOOLSET.tool("desk_create_swo", "Create a stock options swing (SWO) desk with its whole team and its mandate. It is the owner's call: an agent of a desk is refused and the owner creates the desk in the app or asks in the chat. The desk starts at autonomy level 1 (it only analyzes). Premium selling is built but stays closed until a calibrated threshold exists. variant is traditional or pead. Without a mandate the example of the variant is used. Set owner_approved only when the owner asked for this exact mandate.", {
    "name": _s("Desk name, 1 to 80 characters, unique."), "capital": _i("Optional capital of the desk in USD, a whole number. Leave it out unless the owner gave a figure. The desk never gets an account from this tool."),
    "variant": _s("traditional (default) or pead.", enum=["traditional", "pead"]),
    "mandate": _o("Typed mandate: {name, line: SWO, variant?, strategies, analysts, ...}. Leave it out to use the example of the variant."),
    "assets": {"type": "array", "items": {"type": "string"}, "description": "Symbols of the shares, at most 50."},
    "objective_pct": {"type": "number", "description": "Objective in percent, 0 to 100."}, "token_budget": _i("Weekly token budget."),
    "owner_approved": {"type": "boolean", "description": "True only when the owner asked for this mandate in the chat."}},
    ["name"], writes=True)
def desk_create_swo(u, c, a: dict) -> str:
    from hokma.desks.errors import DeskError
    from hokma.desks.swo.create import create_swo_desk
    from hokma.kit import hokma_tools

    if _is_desk_agent(c):
        raise ToolError("blocked", "creating a desk is the owner's: it is refused here and the owner creates it in the app or asks for it in the chat")
    name = a["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= MAX_NAME:
        raise ToolError("bad_input", f"name must be text of 1 to {MAX_NAME} characters")
    assets = a.get("assets")
    if assets is not None and (not isinstance(assets, list) or len(assets) > MAX_ASSETS or not all(isinstance(x, str) for x in assets)):
        raise ToolError("bad_input", f"assets is a list of at most {MAX_ASSETS} symbols")
    mandate = a.get("mandate")
    if mandate is not None and len(json.dumps(mandate, default=str)) > MAX_MANDATE_BYTES:
        raise ToolError("bad_input", "the mandate is too large")
    approved = a.get("owner_approved", False)
    try:
        hokma_tools._name_taken(name)
    except (LookupError, ValueError) as exc:
        raise ToolError("bad_input", str(exc)) from None
    try:
        desk = create_swo_desk(name, a.get("capital"), variant=a.get("variant", "traditional"), mandate=mandate, assets=assets,
                               objective_pct=a.get("objective_pct"), token_budget=a.get("token_budget"), owner_approved=approved is True)
    except (DeskError, ValueError) as exc:
        raise _plain_error(exc) from None
    return _dump({"desk_id": desk["id"], "name": desk["name"], "line": desk["line"], "variant": desk["variant"], "level": 1,
                  "seats": desk["seats"], "analysts": desk["analysts"], "mandate_approved_by_owner": approved is True,
                  "connections": desk.get("connections")})


@TOOLSET.tool("desk_mandate_save", "Replace the typed mandate of your SWO desk (the same door as the desk API). The line and the variant never change. The mandate counts as the owner's only when the owner is the one who edits it: here it is saved as the agent's and the owner approves it in the app.", {
    "desk_id": _i("Desk id."), "mandate": _o("The whole typed mandate.")}, ["desk_id", "mandate"], writes=True)
def desk_mandate_save(u, c, a: dict) -> str:
    from hokma.desks.agents import manager
    from hokma.desks.core.spec_loader import SpecError

    desk_id = _swo_desk(c, a["desk_id"])
    if len(json.dumps(a["mandate"], default=str)) > MAX_MANDATE_BYTES:
        raise ToolError("bad_input", "the mandate is too large")
    try:
        saved = manager.save_mandate(desk_id, a["mandate"], by_owner=False)
    except (manager.MandateError, SpecError, ValueError) as exc:
        raise ToolError("bad_input", str(exc)[:400]) from None
    return _dump({"desk_id": desk_id, "mandate": saved.name, "approved_by_owner": False})


@TOOLSET.tool("desk_cycle_run", "Queue one cycle of your SWO desk for one share: plan, analysts, company filter, debate, judge, trader, risk committee and sub-manager. It is the same door as desk_analysis_start (same limits: one live run per desk and share, two per desk, four in all, and the weekly budget of the desk). It answers at once with status queued and the run_id and does NOT wait: the app runs the cycle in its own process; read it later with desk_cycle_status. The cycle ends with a report and maybe one proposal. The level of the desk decides the rest: 1 only analyzes, 2 asks the owner, 3 sends under the rules the owner approved. The cycle never sends an order by itself and the proposal goes through the filter of the desk.", {
    "desk_id": _i("Desk id."), "symbol": _s("The share, for example AAPL."), "as_of": _s("Cut-off, ISO date or time, never in the future. Default now."),
    "strategy": _s("Name of one strategy of the mandate, to run only that one.")}, ["desk_id", "symbol"], writes=True)
def desk_cycle_run(u, c, a: dict) -> str:
    from hokma.broker.service import BrokerError
    from hokma.desks.errors import DeskError
    from hokma.desks.swo import analysis

    desk_id = _swo_desk(c, a["desk_id"])
    symbol = _symbol(a["symbol"])
    cut = _as_of(a.get("as_of"))
    strategy = a.get("strategy")
    if strategy is not None and (not isinstance(strategy, str) or not 1 <= len(strategy.strip()) <= 80):
        raise ToolError("bad_input", "strategy must be the name of a strategy of the mandate")
    try:
        out = analysis.request(desk_id, symbol, strategy.strip() if strategy else None, as_of=cut)      # the cycle never runs in this process
    except DeskError as exc:
        raise _analysis_error(exc) from None
    except BrokerError as exc:
        raise _plain_error(exc) from None
    return _dump({**out, "read_with": "desk_cycle_status"})


@TOOLSET.tool("desk_cycle_status", "The record of a cycle of your SWO desk: how far it went, why it stopped, the rating, the proposal and what the filter answered. Without run_id it gives the latest cycles of the desk.", {
    "desk_id": _i("Desk id."), "run_id": _i("A run id from desk_cycle_run or desk_analysis_start."), "limit": _i("How many recent cycles, 1 to 20 (default 5).")}, ["desk_id"])
def desk_cycle_status(u, c, a: dict) -> str:
    from hokma.backoffice import runs
    from hokma.desks.swo.graph import KIND

    desk_id = _swo_desk(c, a["desk_id"])
    if a.get("run_id") is not None:
        record = runs.get(_int_id(a["run_id"], "run_id"))
        if record is None or record["desk_id"] != desk_id or record["kind"] != KIND:
            raise ToolError("no_data", f"there is no cycle {a['run_id']} in desk {desk_id}")
        return _dump({"desk_id": desk_id, "cycle": _cycle_row(record, full=True)})
    limit = a.get("limit", 5)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ToolError("bad_input", "limit is a whole number from 1 to 20")
    rows = runs.list_runs(desk_id, kind=KIND, limit=limit)
    if not rows:
        raise ToolError("no_data", f"desk {desk_id} has not run a cycle yet", ["desk_cycle_run"])
    return _dump({"desk_id": desk_id, "cycles": [_cycle_row(r, full=False) for r in rows]})


def _cycle_row(record: dict, *, full: bool) -> dict[str, Any]:
    body = record["record"]
    out = body.get("outputs") or {}
    row = {"run_id": record["id"], "status": record["status"], "symbol": (body.get("entries") or {}).get("symbol"),
           "as_of": (body.get("entries") or {}).get("as_of"), "stopped_at": out.get("stopped_at"), "reason": out.get("reason"),
           "rating": (out.get("final") or {}).get("rating"), "absent": out.get("absent") or [],
           "detail": out.get("detail"), "analyst_status": out.get("analyst_status") or {}, "analyst_causes": out.get("analyst_causes") or {},
           "proposal_ids": [o.get("proposal_id") for o in out.get("orders") or []], "delivery": out.get("delivery"), "learning": out.get("learning"), "in_progress": record["status"] == "running"}
    if full:
        row.update({"trail": out.get("trail"), "regime": out.get("regime"), "filter": out.get("filter"), "judge": out.get("judge"),
                    "committee": out.get("committee"), "final": out.get("final"), "error": out.get("error")})
    return row


def _analysis_error(exc: Any) -> ToolError:
    """The refusal of the analysis door in the words the chief reads: the reason and the way on."""
    kind = "blocked" if exc.code in ("analysis_unavailable", "analysis_running", "analysis_busy", "desk_budget_exhausted") else "no_data" if exc.code == "analysis_not_found" else "bad_input"
    hints = ["desk_analysis_get", "desk_analysis_list"] if exc.code in ("analysis_running", "analysis_busy") else []
    return ToolError(kind, f"{exc.code}: {exc.message}" + (f" (run {exc.extra['run_id']})" if exc.extra.get("run_id") else ""), hints)


@TOOLSET.tool("desk_analysis_start", "Analyze one share on your options desk with the whole team (plan, analysts, company filter, debate, judge, trader, risk committee and manager) and save the report of each agent. It is the same cycle as desk_cycle_run, opened from the Analysis panel's door: one live analysis per desk and share (two per desk and four in all), and the report stays in the list of analyses. It answers at once with the run_id and does NOT wait: the app runs the cycle and the owner sees every step arrive in the Analysis panel; read the result later with desk_analysis_get. The cycle sends no order; the level of the desk and the rules decide what follows. A desk that is not a stock options desk has no analysis.", {
    "desk_id": _i("Desk id."), "symbol": _s("The share, for example AAPL."), "strategy": _s("Name of one strategy of the mandate, to analyze only with that one.")},
    ["desk_id", "symbol"], writes=True)
def desk_analysis_start(u, c, a: dict) -> str:
    from hokma.broker.service import BrokerError
    from hokma.desks.errors import DeskError
    from hokma.desks.swo import analysis

    desk_id = _desk(c, a["desk_id"])
    try:
        out = analysis.request(desk_id, _symbol(a["symbol"]), a.get("strategy"))
    except DeskError as exc:
        raise _analysis_error(exc) from None
    except BrokerError as exc:
        raise _plain_error(exc) from None
    return _dump({**out, "panel": "analysis"})


# Agents' texts are long: the chief gets them cut unless it asks for one seat.
BRIEF_REPORT = 1200


@TOOLSET.tool("desk_analysis_get", "The report of one analysis of your options desk: the steps and how each ended, the grade and the reasons of the manager, the proposal and what became of it, and what each agent wrote (report, why, tools it used). An agent that did not deliver is marked absent or failed with its cause. The agents' texts come cut; pass seat (a name such as Simon or a code such as A09) to read one agent's whole text.", {
    "desk_id": _i("Desk id."), "run_id": _i("The run id from desk_analysis_start or desk_analysis_list."), "seat": _s("Name or code of one agent, to read its text whole.")},
    ["desk_id", "run_id"])
def desk_analysis_get(u, c, a: dict) -> str:
    from hokma.desks.errors import DeskError
    from hokma.desks.swo import analysis

    desk_id = _desk(c, a["desk_id"])
    try:
        out = analysis.get(desk_id, _int_id(a["run_id"], "run_id"))
    except DeskError as exc:
        raise _analysis_error(exc) from None
    seat = a.get("seat")
    if seat is not None:
        if not isinstance(seat, str) or not 1 <= len(seat.strip()) <= 40:
            raise ToolError("bad_input", "seat is the name or the code of an agent")
        wanted = seat.strip().lower()
        out["agents"] = [s for s in out["agents"] if s["name"].lower() == wanted or s["seat"].lower() == wanted]
        if not out["agents"]:
            raise ToolError("no_data", f"no agent {seat!r} took part in this analysis")
    else:
        out["agents"] = [{**s, "report": s["report"][:BRIEF_REPORT]} for s in out["agents"]]
    return _dump(out)


@TOOLSET.tool("desk_analysis_list", "The latest analyses of your options desk, newest first: asset, when, how it ended, the rating and whether it produced a proposal.", {
    "desk_id": _i("Desk id."), "limit": _i("How many, 1 to 50 (default 20).")}, ["desk_id"])
def desk_analysis_list(u, c, a: dict) -> str:
    from hokma.desks.errors import DeskError
    from hokma.desks.swo import analysis

    desk_id = _desk(c, a["desk_id"])
    try:
        return _dump(analysis.list_runs(desk_id, a.get("limit", analysis.DEFAULT_LIMIT)))
    except DeskError as exc:
        raise _analysis_error(exc) from None


@TOOLSET.tool("desk_cycle_learn", "Step 9 of a cycle whose order left after the cycle ended (level 2, once the owner approved it): measure the lesson from the real entry fill and let the sub-manager reflect. It learns only from an order that executed; otherwise it says so.", {
    "desk_id": _i("Desk id."), "run_id": _i("The run id of the cycle that made the proposal.")}, ["desk_id", "run_id"], writes=True)
def desk_cycle_learn(u, c, a: dict) -> str:
    from hokma.desks.swo import graph

    from hokma.desks.errors import DeskError
    from hokma.desks.swo import analysis

    desk_id = _swo_desk(c, a["desk_id"])
    run_id = _int_id(a["run_id"], "run_id")
    try:
        analysis.assert_budget(desk_id)                          # the model calls of this step count against the weekly budget like any other turn
    except DeskError as exc:
        raise _analysis_error(exc) from None
    ensure_runtime()
    try:
        learning = graph.learn_after_fill(desk_id, run_id, runtime=graph.current_runtime(desk_id))
    except graph.CycleError as exc:
        raise ToolError("unavailable" if exc.code == "no_runtime" else "no_data", f"{exc.code}: {exc}") from None
    if learning is None:
        raise ToolError("no_data", f"the cycle {run_id} produced no proposal, so there is nothing to learn from")
    return _dump({"desk_id": desk_id, "run_id": run_id, "status": learning.status, "reason": learning.reason})


@TOOLSET.tool("desk_signals", "The views of the company filter of your SWO desk (the investor profiles) stored for one share or one cycle, newest first. A view that abstained has no value.", {
    "desk_id": _i("Desk id."), "symbol": _s("A share."), "run_id": _i("A run id."), "limit": _i("Rows, 1 to 100 (default 50).")}, ["desk_id"])
def desk_signals(u, c, a: dict) -> str:
    from hokma.db import get_database

    desk_id = _swo_desk(c, a["desk_id"])
    limit = a.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ROWS:
        raise ToolError("bad_input", f"limit is a whole number from 1 to {MAX_ROWS}")
    sql, params = "SELECT id, run_id, seat_code, symbol, as_of, value, confidence, abstained, signal_json FROM desk_signals WHERE desk_id = :d", {"d": desk_id, "n": limit}
    if a.get("symbol") is not None:
        sql, params["y"] = f"{sql} AND symbol = :y", _symbol(a["symbol"])
    if a.get("run_id") is not None:
        sql, params["r"] = f"{sql} AND run_id = :r", _int_id(a["run_id"], "run_id")
    with get_database().read() as unit:
        rows = unit.session.query(f"{sql} ORDER BY id DESC LIMIT :n", params)
    if not rows:
        raise ToolError("no_data", "this desk has stored no views for that filter yet")
    out = []
    for row in rows:
        try:
            detail = json.loads(row["signal_json"])
        except ValueError:
            detail = None
        out.append({"id": row["id"], "run_id": row["run_id"], "seat": row["seat_code"], "symbol": row["symbol"], "as_of": row["as_of"],
                    "value": row["value"], "confidence": row["confidence"], "abstained": bool(row["abstained"]), "signal": detail})
    return _dump({"desk_id": desk_id, "signals": out})


@TOOLSET.tool("lessons_propose", "Propose lessons of your desk. A lesson is accepted only for an order that really executed and is measured from its real entry fill; an order that did not execute is reported as rejected. Each item is the decision id, the order id and the text.", {
    "desk_id": _i("Desk id."), "items": {"type": "array", "items": _o("{decision_id, order_id, text}"), "description": "One to 10 lessons."}},
    ["desk_id", "items"], writes=True)
def lessons_propose(u, c, a: dict) -> str:
    from hokma.desks.agents import manager

    desk_id = _desk(c, a["desk_id"])
    items = a["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_LESSONS:
        raise ToolError("bad_input", f"items is a list of 1 to {MAX_LESSONS} lessons")
    clean: list[tuple[int, int, str]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) - {"decision_id", "order_id", "text"}:
            raise ToolError("bad_input", f"items[{index}] needs decision_id, order_id and text")
        text = item.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_LESSON_CHARS:
            raise ToolError("bad_input", f"items[{index}].text is text of 1 to {MAX_LESSON_CHARS} characters")
        clean.append((_int_id(item.get("decision_id"), f"items[{index}].decision_id"), _int_id(item.get("order_id"), f"items[{index}].order_id"), text.strip()))
    batch = manager.propose_lessons(desk_id, clean, proposed_by="desk-manager")
    return _dump({"desk_id": desk_id, "written": [{"lesson_id": x.id, "status": x.status, "order_id": x.order_id} for x in batch.written],
                  "rejected": [{"order_id": order_id, "why": why} for order_id, why in batch.rejected]})


def ensure_runtime() -> None:
    """Register the runtime of the app once: the models of the desk agents and the data of the cycle. A runtime that a test or the app
    already registered is left as it is."""
    from hokma.desks.swo import app_runtime, graph

    if graph._FACTORY is None:
        graph.register_runtime_factory(app_runtime.build_runtime)


TOOLS = TOOLSET.tools

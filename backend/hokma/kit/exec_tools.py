"""Tools of execution, contracts and the calendar for the desk agents (SPEC-018, B10).

Every tool validates its sizes before it does anything, takes no path and no pattern from the agent, and runs through the services of `hokma.exec`,
`hokma.contracts`, `hokma.econ_calendar` and `hokma.scheduler`. An agent proposes (`exec_propose`); it never sends: the order leaves only through
`hokma.exec.submit`, after the filter and the level of the desk. A desk chief operates only the desk of its own project. Failures are classified
(`no_data`, `unavailable`, `stale`, `bad_input`, `blocked`, plus `refused` and `awaiting_owner`)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from hokma.kit.tool_registry import ToolError, ToolSet

TOOLSET = ToolSet()
MAX_TEXT = 4000
MAX_LEGS = 4
MAX_NAMES = 20
MAX_COUNTRIES = 10
MAX_DAYS = 31
MAX_RULES_BYTES = 4000


def _s(desc: str = "", **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **extra}


def _i(desc: str = "") -> dict[str, Any]:
    return {"type": "integer", "description": desc}


def _o(desc: str = "") -> dict[str, Any]:
    return {"type": "object", "description": desc}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _who(c: Any):
    from hokma.kit import hokma_tools

    return hokma_tools._who(c)


def _desk(c: Any, desk_id: Any) -> int:
    from hokma.kit import hokma_tools

    if isinstance(desk_id, bool) or not isinstance(desk_id, int) or desk_id < 1:
        raise ToolError("bad_input", "desk_id must be a desk id", ["desk_list"])
    try:
        hokma_tools._own_desk_only(c, desk_id)
    except LookupError as exc:
        raise ToolError("blocked", str(exc)) from None
    return desk_id


def _text(value: Any, name: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ToolError("bad_input", f"{name} must be text of 1 to {limit} characters")
    return value.strip()


def _when(value: Any, name: str) -> datetime:
    from hokma.data.sentinels import parse_time

    try:
        out = parse_time(value)
    except ValueError as exc:
        raise ToolError("bad_input", f"{name}: {exc}", ["Use an ISO date such as 2026-01-31T15:00:00Z"]) from None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def _service_error(exc: Exception) -> ToolError:
    from hokma.broker.service import BrokerError

    if isinstance(exc, BrokerError):
        code = {"owner_only": "blocked", "owner_required": "blocked", "no_trading_account": "blocked", "orders_off": "blocked",
                "needs_account": "blocked", "account_not_in_desk": "blocked", "desk_inactive": "blocked", "decision_not_found": "no_data", "desk_not_found": "no_data"}.get(exc.code, "bad_input")
        return ToolError(code, exc.message)
    return ToolError("bad_input", str(exc)[:300])


@TOOLSET.tool("exec_propose", "Propose an order for your desk. You only propose: the code applies the limits of the committee, and what happens next depends on the autonomy level of the desk (1 only analyzes, 2 asks the owner, 3 sends alone under rules the owner approved). Legs use canonical contract ids (see contract_get). Amounts are numbers or text of numbers. The thesis is required: it travels as the message the owner reads. On a SWO desk an entry may add structure, expiration, net_premium, max_loss_usd and underlying, which are checked against the legs. Never put a size or a stop you did not compute.", {
    "desk_id": _i("Desk id."), "proposal": _o("{line, order_type, limit_price?, stop_price?, tif?, stop_loss?, take_profit?, reduce_only?, strategy_key? (default principal; the account is never yours to name), thesis, legs:[{contract_id, side, effect, qty}], SWO only: structure?, expiration?, net_premium?, max_loss_usd?, underlying?}")},
    ["desk_id", "proposal"], writes=True)
def exec_propose(u, c, a: dict) -> str:
    from hokma import exec as hexec
    from hokma.broker.service import BrokerError
    from hokma.exec.proposal import OrderProposal, ProposalError

    desk_id = _desk(c, a["desk_id"])
    body = a["proposal"]
    if not isinstance(body.get("legs"), list) or len(body["legs"]) > MAX_LEGS:
        raise ToolError("bad_input", f"a proposal has 1 to {MAX_LEGS} legs")
    if len(json.dumps(body, default=str)) > 20_000:
        raise ToolError("bad_input", "the proposal is too large")
    from hokma.db import get_database
    from hokma.exec.submit import profile_line

    real_line = profile_line(get_database(), desk_id)
    if real_line is None:
        raise ToolError("refused", "no_line: the desk has no line set")
    if body.get("line", real_line) != real_line:
        raise ToolError("refused", f"line_mismatch: the line of this desk is {real_line}")
    thesis = _text(body.get("thesis"), "thesis")                       # the thesis is the message of the proposal (OA-51)
    try:
        from hokma.desks.swo.schema import SWO_KEYS, SWOProposal

        if real_line == "SWO" and any(k in body for k in SWO_KEYS):
            # the structure and the premium are checked against the legs; what is sent is the plain order, with the same fingerprint
            proposal = SWOProposal.from_dict({**body, "line": real_line, "thesis": thesis}, desk_id=desk_id, origin="desk",
                                             created_by=c.agent or "agent").to_order()
        else:
            proposal = OrderProposal.from_dict({**body, "line": real_line, "thesis": thesis}, desk_id=desk_id, origin="desk", created_by=c.agent or "agent")
        result = hexec.submit(proposal, who=_who(c))
    except ProposalError as exc:
        raise ToolError("bad_input", str(exc)) from None
    except BrokerError as exc:
        raise _service_error(exc) from None
    if result.status == "refused":
        raise ToolError("refused", f"{result.code}: {result.why}")
    return _dump(result.to_dict())


@TOOLSET.tool("exec_status", "The state of a proposal of your desk: analysis_only, refused, awaiting_owner, sent, filled, rejected_by_broker, expired or canceled.", {
    "proposal_id": _i("Proposal id.")}, ["proposal_id"])
def exec_status(u, c, a: dict) -> str:
    from hokma import exec as hexec
    from hokma.broker.service import BrokerError
    from hokma.db import get_database

    pid = a["proposal_id"]
    with get_database().read() as unit:
        row = unit.session.one("SELECT desk_id FROM exec_proposals WHERE id = :i", {"i": pid})
    if row is None:
        raise ToolError("no_data", f"there is no proposal {pid}")
    _desk(c, row["desk_id"])
    try:
        return _dump(hexec.status(pid).to_dict())
    except BrokerError as exc:
        raise _service_error(exc) from None


@TOOLSET.tool("exec_pending", "The orders of a desk that wait for the owner.", {"desk_id": _i("Desk id.")}, ["desk_id"])
def exec_pending(u, c, a: dict) -> str:
    from hokma.exec import approvals

    return _dump({"desk_id": _desk(c, a["desk_id"]), "pending": approvals.list_pending(a["desk_id"])})


@TOOLSET.tool("exec_flatten", "Close every position of the desk with market orders that only reduce. The owner can ask for it, and so can a rule the owner approved with flatten set at level 3. Without one of those it is refused.", {
    "desk_id": _i("Desk id."), "reason": _s("Why.")}, ["desk_id", "reason"], writes=True)
def exec_flatten(u, c, a: dict) -> str:
    from hokma import exec as hexec
    from hokma.broker.service import BrokerError

    desk_id = _desk(c, a["desk_id"])
    try:
        results = hexec.flatten(desk_id, reason=_text(a["reason"], "reason", 500), who=_who(c))
    except BrokerError as exc:
        raise _service_error(exc) from None
    return _dump({"desk_id": desk_id, "orders": [r.to_dict() for r in results]})


@TOOLSET.tool("desk_level_get", "The autonomy level of a desk (1, 2 or 3) and, at level 3, the rules the owner approved.", {"desk_id": _i("Desk id.")}, ["desk_id"])
def desk_level_get(u, c, a: dict) -> str:
    from hokma.exec import levels

    level, rules = levels.get_level(_desk(c, a["desk_id"]))
    return _dump({"desk_id": a["desk_id"], "level": level, "rules": rules})


@TOOLSET.tool("desk_level_set", "Change the autonomy level of a desk. Lowering it is free; raising it, and approving the rules of level 3, is the owner's: it is refused here and the owner changes it in the app.", {
    "desk_id": _i("Desk id."), "level": _i("1, 2 or 3."), "rules": _o("Rules of level 3: {id, max_qty or max_risk_usd, symbols?, order_types?, sides?, max_orders_per_day?, flatten?}")},
    ["desk_id", "level"], writes=True)
def desk_level_set(u, c, a: dict) -> str:
    from hokma import exec as hexec
    from hokma.broker.service import BrokerError
    from hokma.exec.levels import LevelError

    desk_id = _desk(c, a["desk_id"])
    rules = a.get("rules")
    if rules is not None and len(json.dumps(rules, default=str)) > MAX_RULES_BYTES:
        raise ToolError("bad_input", "the rules are too long")
    try:
        return _dump(hexec.set_level(desk_id, a["level"], rules=rules, who=_who(c)))
    except LevelError as exc:
        raise ToolError("bad_input", str(exc)) from None
    except BrokerError as exc:
        raise _service_error(exc) from None


@TOOLSET.tool("contract_get", "The contract sheet of an instrument: canonical id, multiplier, tick, expiration, strike, underlying. It comes from the provider; never type a multiplier from memory.", {
    "symbol": _s("A symbol or a canonical id, for example AAPL, NQZ26, OPT:SPY:2026-12-18:C:600.")}, ["symbol"])
def contract_get(u, c, a: dict) -> str:
    from hokma.contracts import sheet
    from hokma.contracts.identity import NoContract

    try:
        return _dump(sheet.describe(sheet.get(_text(a["symbol"], "symbol", 80))))
    except NoContract as exc:
        raise ToolError("no_data", str(exc), ["Use a canonical id such as FUT:NQ:2026-12 or EQ:AAPL"]) from None


@TOOLSET.tool("calendar_events", "Macro events between two dates (Forex Factory, Finnhub as backup). status UNVERIFIED_EVENT_RISK means the calendar could not be read: it is not an empty calendar.", {
    "start": _s("ISO date or time."), "end": _s("ISO date or time, at most 31 days after start."),
    "countries": {"type": "array", "items": {"type": "string"}, "description": "Country codes, at most 10."},
    "names": {"type": "array", "items": {"type": "string"}, "description": "Words of the event names, at most 20."}}, ["start", "end"])
def calendar_events(u, c, a: dict) -> str:
    from hokma.econ_calendar import events

    start, end = _when(a["start"], "start"), _when(a["end"], "end")
    if end < start or (end - start).days > MAX_DAYS:
        raise ToolError("bad_input", f"end must be after start by at most {MAX_DAYS} days")
    countries = [x.strip().upper()[:3] for x in (a.get("countries") or ["US"])[:MAX_COUNTRIES] if isinstance(x, str)]
    names = [x.strip()[:60] for x in (a.get("names") or [])[:MAX_NAMES] if isinstance(x, str)] or None
    result = events.events(start=start, end=end, countries=countries, names=names)
    if result.status == "UNVERIFIED_EVENT_RISK":
        raise ToolError("unavailable", "the calendar could not be read: the event risk is unverified, not absent")
    return _dump({"status": result.status, "events": [vars(e) if not hasattr(e, "__dataclass_fields__") else {k: getattr(e, k) for k in e.__dataclass_fields__} for e in result.events]})


@TOOLSET.tool("calendar_windows", "The event windows of a desk that are active now: the periods around macro events in which it opens nothing.", {
    "desk_id": _i("Desk id.")}, ["desk_id"])
def calendar_windows(u, c, a: dict) -> str:
    from hokma.econ_calendar import windows

    desk_id = _desk(c, a["desk_id"])
    now = datetime.now(timezone.utc)
    active = windows.active(windows.windows(desk_id, at=now), now)
    return _dump({"desk_id": desk_id, "windows": [{"match": w.match, "event": w.event, "starts_at": w.starts_at, "ends_at": w.ends_at, "unverified": w.unverified} for w in active]})


@TOOLSET.tool("schedule_run", "Schedule a run of your own desk with a time zone (at, every or cron expression). It can only be for your desk.", {
    "desk_id": _i("Desk id."), "expr": _s("Schedule expression, for example 'cron 0 9 * * 1-5 America/New_York'."), "instruction": _s("What the run does.")},
    ["desk_id", "expr", "instruction"], writes=True)
def schedule_run(u, c, a: dict) -> str:
    from hokma.scheduler import service

    desk_id = _desk(c, a["desk_id"])
    try:
        job = service.schedule_desk_run(desk_id, expr=_text(a["expr"], "expr", 120), instruction=_text(a["instruction"], "instruction", 2000), who=_who(c))
    except (ValueError, LookupError) as exc:
        raise ToolError("bad_input", str(exc)[:300]) from None
    return _dump(job)


TOOLS = TOOLSET.tools

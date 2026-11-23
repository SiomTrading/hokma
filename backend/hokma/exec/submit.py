"""The only door to a broker (SPEC-018: AH-66, OA-05, OA-41). `submit` takes an order proposal and decides, by the level of the desk and the
filter, whether it is only recorded, refused, asks the owner or leaves. `dispatch` is the single function of Hokma that calls the
order methods of a broker: the proposals of this module, the approvals that continue them and the native orders of SPEC-015
(`hokma.broker.service`) all reach a broker through it, after their own rules. A scan test fails if any other module calls an order method.

Blocking (the filter and the broker are network calls): call it from a thread.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from hokma.contracts import discipline, symbols
from hokma.contracts.identity import ContractSheet
from hokma.db import Database, get_database
from hokma.db.timeutil import utcnow
from hokma.exec import levels, prefilter, ticket
from hokma.exec.money import text as dtext
from hokma.exec.proposal import Leg, OrderProposal

log = logging.getLogger("hokma.exec")

Status = Literal["analysis_only", "refused", "awaiting_owner", "sent", "filled", "partially_filled", "rejected_by_broker", "expired", "canceled"]
SENT_STATUSES = ("sent", "filled", "partially_filled")


@dataclass(frozen=True)
class ExecResult:
    proposal_id: int
    status: Status
    code: str                              # stable id: "level_1", "hash_mismatch", "no_contract", guard codes...
    why: str                               # in the language of the owner
    fingerprint: str
    broker_order_record_id: int | None = None
    decision_id: int | None = None
    ticket: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"proposal_id": self.proposal_id, "status": self.status, "code": self.code, "why": self.why, "fingerprint": self.fingerprint,
                "broker_order_record_id": self.broker_order_record_id, "decision_id": self.decision_id, "ticket": self.ticket}


class Refusal(Exception):
    """The proposal cannot go on; `code` is stable."""

    def __init__(self, code: str, why: str) -> None:
        super().__init__(code)
        self.code, self.why = code, why


# --------------------------------------------------------------------------------------------------------- the one door

def dispatch(broker: Any, account_id: int, action: str, order: Any = None, order_id: Any = None) -> tuple[dict | None, int, str]:
    """The only call to the order methods of a broker. Returns (what the broker answered, HTTP status, outcome word)."""
    if action == "place":
        return broker.place_order(account_id, order), 201, "sent"
    if action == "replace":
        return broker.replace_order(account_id, order_id, order), 201, "replaced"
    if action == "cancel":
        return broker.cancel_order(account_id, order_id), 200, "canceled"
    raise ValueError(f"action must be place, replace or cancel; got {action!r}")


@contextmanager
def open_broker(database: Database, desk_id: int):
    """The broker and the account of a desk, valid inside the block: the broker holds the session of an open unit."""
    from hokma.broker import service

    with database.read() as u:
        yield service.broker_of_desk(u, desk_id)


# ------------------------------------------------------------------------------------------------------------ contracts

def resolve_contracts(p: OrderProposal) -> dict[str, ContractSheet]:
    """OA-05: the sheet of every contract of the proposal before anything else. A symbol with no sheet is refused `no_contract`;
    with its sheet, the multiplier and the tick are the ones of the sheet in everything that follows."""
    ids = [g.contract_id for g in p.legs]
    loaded = discipline.load_sheets(ids)
    for cid in ids:
        sheet = loaded.get(cid)
        if sheet is None:
            raise Refusal("no_contract", ticket.why("no_contract", contract=cid))
        try:
            discipline.check_sheet(sheet, cid)
        except discipline.DisciplineError as exc:
            raise Refusal(exc.code, exc.why) from exc
    return {cid: loaded[cid] for cid in ids}


# ------------------------------------------------------------------------------------------------- the order of a broker

_SCHWAB_TYPES = {"market": "MARKET", "limit": "LIMIT", "stop": "STOP", "stop_limit": "STOP_LIMIT"}
_ASSET = {"equity": "EQUITY", "etf": "EQUITY", "option": "OPTION", "future": "FUTURE", "future_option": "OPTION", "cfd": "CFD"}


def _instruction(leg: Leg, sheet: ContractSheet) -> str:
    base = "BUY" if leg.side == "buy" else "SELL"
    if sheet.asset_type in ("option", "future_option"):
        return f"{base}_TO_{'OPEN' if leg.effect == 'open' else 'CLOSE'}"
    return base


def _schwab_leg(leg: Leg, sheet: ContractSheet, *, reverse: bool = False) -> dict[str, Any]:
    side = leg.side if not reverse else ("sell" if leg.side == "buy" else "buy")
    flipped = Leg(leg.contract_id, side, "close" if reverse else leg.effect, leg.qty)
    instrument: dict[str, Any] = {"symbol": symbols.to_provider(leg.contract_id, "schwab"), "assetType": _ASSET.get(sheet.asset_type, "EQUITY")}
    if instrument["assetType"] == "OPTION":
        instrument.update(putCall="CALL" if sheet.put_call == "C" else "PUT", underlyingSymbol=sheet.underlying or "")
    return {"instruction": _instruction(flipped, sheet), "quantity": float(leg.qty), "instrument": instrument}


def _shares_and_options(p: OrderProposal, sheets: dict[str, ContractSheet]) -> bool:
    """Shares and options in one order (a covered call): the stop and the target of the structure are premium levels of the option, not a price
    of the shares, so reversing every leg of the order into TRIGGER + OCO exits would sell the shares at an option price (decision 1264)."""
    kinds = {sheets[g.contract_id].asset_type for g in p.legs}
    return bool(kinds & {"option", "future_option"}) and bool(kinds & {"equity", "etf"})


def exits_apart(p: OrderProposal, sheets: dict[str, ContractSheet]) -> None:
    """Shares and options in one order with a stop or a target: refused, never sent without them. The exits of such a structure are orders of their
    own, written before the fingerprint, so what the owner approves is what goes out."""
    if (p.stop_loss is not None or p.take_profit is not None) and _shares_and_options(p, sheets):
        raise Refusal("exits_apart_required", "an order of shares and options carries no stop or target: its exits go as separate orders")


def to_native(p: OrderProposal, sheets: dict[str, ContractSheet], kind: str) -> dict[str, Any]:
    """The proposal in the shape of one broker. Schwab: its order request (a stop and a target become a trigger with an OCO of
    two exits). Any other broker: the neutral order, with every amount as text, which its connector translates."""
    if kind == "schwab":
        main: dict[str, Any] = {"orderStrategyType": "SINGLE", "session": "NORMAL", "duration": "DAY" if p.tif == "day" else "GOOD_TILL_CANCEL",
                                "orderType": _SCHWAB_TYPES[p.order_type],
                                "orderLegCollection": [_schwab_leg(g, sheets[g.contract_id]) for g in p.legs]}
        if p.limit_price is not None:
            main["price"] = float(p.limit_price)
        if p.stop_price is not None:
            main["stopPrice"] = float(p.stop_price)
        if len(p.legs) > 1:
            main["complexOrderStrategyType"] = "CUSTOM"
        if p.stop_loss is not None or p.take_profit is not None:
            exits = []
            if p.take_profit is not None:
                exits.append({"orderStrategyType": "SINGLE", "session": "NORMAL", "duration": "GOOD_TILL_CANCEL", "orderType": "LIMIT",
                              "price": float(p.take_profit), "orderLegCollection": [_schwab_leg(g, sheets[g.contract_id], reverse=True) for g in p.legs]})
            if p.stop_loss is not None:
                exits.append({"orderStrategyType": "SINGLE", "session": "NORMAL", "duration": "GOOD_TILL_CANCEL", "orderType": "STOP",
                              "stopPrice": float(p.stop_loss), "orderLegCollection": [_schwab_leg(g, sheets[g.contract_id], reverse=True) for g in p.legs]})
            main = {"orderStrategyType": "TRIGGER", **{k: v for k, v in main.items() if k != "orderStrategyType"},
                    "childOrderStrategies": [exits[0] if len(exits) == 1 else {"orderStrategyType": "OCO", "childOrderStrategies": exits}]}
        return main
    return {"fingerprint": p.fingerprint(), "type": p.order_type, "tif": p.tif, "limit_price": dtext(p.limit_price),
            "stop_price": dtext(p.stop_price), "stop_loss": dtext(p.stop_loss), "take_profit": dtext(p.take_profit), "reduce_only": p.reduce_only,
            "legs": [{"contract_id": g.contract_id, "symbol": _provider_symbol(g.contract_id, kind), "side": g.side, "effect": g.effect,
                      "qty": dtext(g.qty), "multiplier": dtext(sheets[g.contract_id].multiplier), "tick_size": dtext(sheets[g.contract_id].tick_size),
                      "asset_type": sheets[g.contract_id].asset_type}
                     for g in p.legs]}


def _provider_symbol(contract_id: str, kind: str) -> str:
    try:
        return symbols.to_provider(contract_id, kind if kind in symbols.PROVIDERS else "crypto")
    except Exception:                                     # the sim broker has no provider: the id is its own symbol
        return contract_id


# ---------------------------------------------------------------------------------------------------------------- records

def _who_name(who: Any) -> str:
    if getattr(who, "owner", False):
        return "owner"
    return (getattr(who, "agent", None) or "system")[:80]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _insert(u: Any, p: OrderProposal, *, level: int, status: str, code: str, who: Any, prefilter_json: Any = None, clamps: Any = None,
            decision_id: int | None = None, record_id: int | None = None, approved_by: str | None = None, approved: bool = False) -> int:
    fp = p.fingerprint()
    values = {"desk_id": p.desk_id, "line": p.line, "origin": p.origin, "level": level, "proposal_json": _json(p.to_json()), "fingerprint": fp,
              "status": status, "code": code[:64], "prefilter_json": None if prefilter_json is None else _json(prefilter_json),
              "clamps_json": None if not clamps else _json(list(clamps)), "decision_id": decision_id, "broker_order_record_id": record_id,
              "approved_fingerprint": fp if approved else None, "approved_by": approved_by, "created_by": (p.created_by or _who_name(who))[:80],
              "created_at": utcnow(), "updated_at": utcnow()}
    return u.session.insert("exec_proposals", values)


def _record_attempt(u: Any, p: OrderProposal, *, who: Any, account: Any, kind: str, request: Any, rule_result: str, why: str,
                    approved_by: str | None = None, http_status: int | None = None, broker_order_id: str | None = None,
                    response: Any = None) -> int:
    row = u.broker.record_order(
        broker_kind=kind, action="place", desk_id=p.desk_id, account_id=None if account is None else account.id,
        agent_slug=who.agent if getattr(who, "agent", None) and u.agents.find(who.agent) is not None else None,
        conversation_id=getattr(who, "conversation_id", None), task_id=getattr(who, "task_id", None), reason=(p.thesis or None) and p.thesis[:2000],
        request=request, rule_result=rule_result, rule_why=why[:4000] or None, approved_by=approved_by, http_status=http_status,
        broker_order_id=broker_order_id, response=response)
    return row.id


def _decision(u: Any, p: OrderProposal, *, desk_name: str, ticket_text: str, who: Any) -> int:
    from hokma.i18n import t

    summary = ticket.legs_text(p, {})
    proposer = next((s for s in (getattr(who, "agent", None), p.created_by, "desk-chief") if s and u.agents.find(s) is not None), None)
    if proposer is None:
        known = u.agents.list()
        if not known:
            raise Refusal("no_agent", "no agent can propose the decision")
        proposer = known[0].slug
    body = _json(p.to_json())
    return u.decisions.propose(t("exec.decision.title", desk=desk_name, summary=summary)[:200], ticket_text, proposed_by=proposer,
                               context=body if len(body) <= 8000 else None, project_id=None).id


def _notify(u: Any, text: str) -> None:
    from hokma.exec import telegram_approval

    telegram_approval.notice(u, text)


# ------------------------------------------------------------------------------------------------------------------ submit

def submit(p: OrderProposal, *, who: Any, db: Database | None = None) -> ExecResult:
    """The door. Records the proposal in `exec_proposals` and every attempt to send in `broker_orders`."""
    from hokma.broker import service

    database = db or get_database()
    if p.origin == "chat" and not getattr(who, "owner", False):
        p = dataclasses.replace(p, origin="desk")      # the origin comes from who asks, never from the proposal: only the owner is "chat"
    real_line = profile_line(database, p.desk_id)
    if real_line is not None and p.line != real_line and getattr(who, "owner", False):
        p = dataclasses.replace(p, line=real_line)              # the line is the desk's, from desk_profile; the owner's order takes it
    level, rules = levels.get_level(p.desk_id, db=database)
    with database.read() as u:
        desk = u.desks.find(p.desk_id)
        if desk is None:
            raise service.BrokerError("desk_not_found", 404)
        pair = service.broker_of_desk(u, p.desk_id)
        desk_name = desk.name
    account = None if pair is None else pair[1]
    kind = "none" if pair is None else pair[0].kind
    fp = p.fingerprint()

    def finish(status: Status, code: str, why: str, *, prefilter_json: Any = None, clamps: Any = None, record: bool = True,
               proposal: OrderProposal | None = None, ticket_text: str = "", decision_id: int | None = None) -> ExecResult:
        final = proposal or p
        with database.write() as u:
            rid = None
            if record and status == "refused":
                rid = _record_attempt(u, final, who=who, account=account, kind=kind, request=final.to_json(), rule_result="refused", why=why)
            pid = _insert(u, final, level=level, status=status, code=code, who=who, prefilter_json=prefilter_json, clamps=clamps,
                          decision_id=decision_id, record_id=rid)
        return ExecResult(pid, status, code, why, final.fingerprint(), rid, decision_id, ticket_text)

    if real_line is None:
        return finish("refused", "no_line", ticket.why("no_line"))
    if p.line != real_line:                                      # a line the agent wrote is never believed
        return finish("refused", "line_mismatch", ticket.why("line_mismatch", line=real_line))
    if p.origin == "rule" and level != 3:
        return finish("refused", "rule_level", ticket.why("rule_level"))
    try:
        sheets = resolve_contracts(p)
        exits_apart(p, sheets)
    except Refusal as refusal:
        return finish("refused", refusal.code, refusal.why)
    if pair is None:
        return finish("refused", "no_account", ticket.why("no_account"))
    preview = None
    if kind == "schwab":
        with open_broker(database, p.desk_id) as live:
            preview = _schwab_preview(live[0], live[1], to_native(p, sheets, kind))
    result = prefilter.run(p.desk_id, p, db=database, preview=preview, sheets=sheets)
    detail = result.to_json()
    if result.verdict == "BLOCK":
        block = result.blocked_by
        return finish("refused", block.code if block else "blocked", block.why if block else "", prefilter_json=detail, clamps=result.clamps)
    final = result.proposal
    text = ticket.ticket(final, desk_name=desk_name, level=level, contracts=sheets, clamps=result.clamps)

    if p.origin == "desk" and level == 1:
        return finish("analysis_only", "level_1", ticket.why("level_1"), prefilter_json=detail, clamps=result.clamps, record=False,
                      proposal=final, ticket_text=text)

    risk_why = _owner_risk_why(database, p, final, sheets, preview) if p.origin == "chat" and getattr(who, "owner", False) else None
    auto, approver = False, None
    if p.origin == "chat":
        if risk_why:
            pass                                                # the owner is warned and decides: the order waits for the confirmation
        elif not result.needs_owner:
            auto, approver = True, "owner" if getattr(who, "owner", False) else "auto"
        elif getattr(who, "owner", False):
            auto, approver = True, "owner"
    elif level == 3 and not result.needs_owner:
        orders_today = 0
        with database.read() as u:
            orders_today = u.broker.count_sent_orders(p.desk_id, utcnow()[:10] + " 00:00:00")
        allowed, rule_id = levels.rules_allow(rules, final, orders_today=orders_today, sheets=sheets, marks=result.marks,
                                                protective=kind in levels.PROTECTIVE_STOP_KINDS)
        if allowed:
            auto, approver = True, f"rule:{rule_id}"

    if auto:
        with database.write() as u:
            pid = _insert(u, final, level=level, status="awaiting_owner", code="approved", who=who, prefilter_json=detail, clamps=result.clamps,
                          approved=True, approved_by=approver if approver == "owner" or (approver or "").startswith("rule:") else None)
        from hokma.exec import approvals

        out = approvals.send_approved(pid, db=database, auto_by=approver or "auto")
        return ExecResult(out.proposal_id, out.status, out.code, out.why, out.fingerprint, out.broker_order_record_id, None, text)

    with database.write() as u:
        decision_id = _decision(u, final, desk_name=desk_name, ticket_text=text, who=who)
        rid = _record_attempt(u, final, who=who, account=account, kind=kind, request=final.to_json(), rule_result="needs_owner",
                              why=risk_why or next((r.why for r in result.results if r.code == "needs_owner"), ticket.why("awaiting")))
        pid = _insert(u, final, level=level, status="awaiting_owner", code="owner_risk_confirm" if risk_why else "awaiting_owner", who=who, prefilter_json=detail, clamps=result.clamps,
                      decision_id=decision_id, record_id=rid)
        _notify(u, (risk_why or ticket.why("awaiting", desk=desk_name)) + "\n" + text)
    from hokma.exec import telegram_approval

    telegram_approval.request_buttons(pid, final.fingerprint(), text)
    if risk_why:
        return ExecResult(pid, "awaiting_owner", "owner_risk_confirm", risk_why, final.fingerprint(), rid, decision_id, text)
    return ExecResult(pid, "awaiting_owner", "awaiting_owner", ticket.why("awaiting", desk=desk_name), final.fingerprint(), rid, decision_id, text)


def _owner_risk_why(database: Database, p: OrderProposal, final: OrderProposal, sheets: dict[str, ContractSheet], preview: Any) -> str | None:
    """The warning for an order of the owner that leaves an option sold with no cover once the closes are netted (H3, decision 1265).
    Plain words, asked once: the confirmation goes through the approval of the order, which does not warn again. A reduce-only order cannot open."""
    from hokma.exec.guards.premium_selling import owner_risk

    if final.reduce_only:
        return None
    ctx = prefilter.load_context(p.desk_id, final, db=database, preview=preview, sheets=sheets)
    risk = owner_risk(final, ctx.contracts, marks=ctx.marks, positions=ctx.positions, open_orders=ctx.open_orders)
    if risk is None:
        return None
    _kind, contract, qty = risk
    return ticket.why("owner_risk_short", qty=dtext(qty), contract=contract)


def _schwab_preview(broker: Any, account: Any, native: dict) -> Any:
    from hokma.schwab import accounts as schwab_accounts

    try:
        return schwab_accounts.public(broker.preview_order(account.id, native))
    except Exception:                                         # no preview: the rules read a proposal without one (as with a broker that has none)
        log.warning("the preview of an order could not be read")
        return None


# ---------------------------------------------------------------------------------------------------------------- reads

def _row(database: Database, proposal_id: int) -> dict[str, Any]:
    from hokma.broker.service import BrokerError

    with database.read() as u:
        row = u.session.one("SELECT * FROM exec_proposals WHERE id = :i", {"i": proposal_id})
    if row is None:
        raise BrokerError("decision_not_found", 404, text=ticket.why("not_found"))
    return row


def status(proposal_id: int, *, db: Database | None = None) -> ExecResult:
    row = _row(db or get_database(), proposal_id)
    return ExecResult(row["id"], row["status"], row["code"] or "", "", row["fingerprint"], row["broker_order_record_id"], row["decision_id"], "")


def flatten(desk_id: int, *, reason: str, who: Any, db: Database | None = None) -> list[ExecResult]:
    """Close every position of the desk: one reduce-only market order per position, sent as a close. The owner, or a rule the owner
    approved with `flatten: true` at level 3, can ask for it."""
    from hokma.broker import service

    database = db or get_database()
    level, rules = levels.get_level(desk_id, db=database)
    if not getattr(who, "owner", False) and not (level == 3 and rules and rules.get("flatten") is True):
        raise service.owner_only("flattening a desk")
    with database.read() as u:
        pair = service.broker_of_desk(u, desk_id)
        desk = u.desks.find(desk_id)
        if desk is None:
            raise service.BrokerError("desk_not_found", 404)
    if pair is None or not pair[0].capabilities.positions:
        raise service.BrokerError("no_trading_account", 409)
    with database.read() as u:                                    # refuse BEFORE canceling anything: a refused close must not leave the book without its orders
        facts = service._load(u, desk_id)
        status, enabled = facts.desk.status, facts.enabled
    owner = getattr(who, "owner", False) is True
    if status == "archived" or (not owner and status != "active"):
        raise service.BrokerError("desk_inactive", 409)
    if not owner and not enabled:
        raise service.BrokerError("orders_off", 409)
    line = _line_of(database, desk_id)
    cancel_live_orders(database, desk_id, reason=reason, who=who)          # nothing live can stand between the position and its close
    with database.read() as u:
        pair = service.broker_of_desk(u, desk_id)
        broker, account = pair
        positions = prefilter._normalize_positions(broker.positions(account.id))       # read AFTER the cancels: a fill while they went out changes the quantity
    out: list[ExecResult] = []
    marker = prefilter.EMERGENCY_CLOSE.set((desk_id, "owner") if getattr(who, "owner", False) is True else None)   # only the owner's closes get past a pause
    try:
        return _flatten_positions(database, desk_id, positions, line, reason, who, out)
    finally:
        prefilter.EMERGENCY_CLOSE.reset(marker)


def _flatten_positions(database: Database, desk_id: int, positions: Any, line: str, reason: str, who: Any, out: list[ExecResult]) -> list[ExecResult]:
    for pos in positions:
        qty = Decimal(str(pos.qty))
        if qty == 0 or not getattr(pos, "contract_id", None):
            continue
        leg = Leg(pos.contract_id, "sell" if qty > 0 else "buy", "close", abs(qty))
        proposal = OrderProposal(desk_id, line, "chat" if getattr(who, "owner", False) else "rule", (leg,), "market", reduce_only=True,
                                 thesis=(reason or "flatten")[:4000], created_by=_who_name(who))
        out.append(submit(proposal, who=who, db=database))
    return out


CANCEL_WAIT_SECONDS = 10.0


def _live_orders(broker: Any, account_id: int) -> list[Any]:
    from hokma.exec.guards import _DONE

    if not broker.capabilities.orders:
        return []
    return [o for o in (broker.orders(account_id) or ()) if str(getattr(o, "status", "") or "").upper() not in _DONE]


def cancel_live_orders(database: Database, desk_id: int, *, reason: str, who: Any, wait: float = CANCEL_WAIT_SECONDS) -> list[str]:
    """First step of a flatten: cancel every live order that touches a contract the desk holds, or every live order of the account when one of
    them cannot be read whole, and wait until the broker confirms that none is live. Raises BrokerError cancel_unconfirmed when it cannot
    be confirmed: the close is then NOT sent, because a live order of the other side could still cross the position through zero."""
    import time

    from hokma.broker import service
    from hokma.exec.guards import scan_orders

    with open_broker(database, desk_id) as live:
        broker, account = live
        held = {getattr(p, "contract_id", None) for p in prefilter._normalize_positions(broker.positions(account.id)) if Decimal(str(p.qty)) != 0}
        orders = _live_orders(broker, account.id)
        everything = not scan_orders(orders)[1]
        targets = [o for o in orders if everything or not scan_orders([o])[1] or {c for c, _s, _q in scan_orders([o])[0]} & held]
        ids = [str(o.order_id) for o in targets if getattr(o, "order_id", None) is not None]
        if len(ids) != len(targets):
            raise service.BrokerError("cancel_unconfirmed", 409)               # a live order with no id cannot be canceled: nothing is sent
        for oid in ids:
            try:
                dispatch(broker, account.id, "cancel", order_id=oid)
            except Exception:                                                  # noqa: BLE001  (already done or refused: the confirmation below decides)
                pass
        deadline = time.monotonic() + wait
        while True:
            still = {str(o.order_id) for o in _live_orders(broker, account.id)} & set(ids)
            if not still:
                break
            if time.monotonic() >= deadline:
                raise service.BrokerError("cancel_unconfirmed", 409)
            time.sleep(0.2)
    if ids:
        with database.write() as u:
            u.broker.record_order(broker_kind=broker.kind, action="cancel", desk_id=desk_id, account_id=account.id, request={"order_ids": ids, "why": "flatten"},
                                  rule_result="auto", reason=(reason or "flatten")[:2000])
    return ids


def profile_line(database: Database, desk_id: int) -> str | None:
    """The line of a desk from `desk_profile`, or None while the desk has no profile."""
    with database.read() as u:
        row = u.session.one("SELECT line FROM desk_profile WHERE desk_id = :d", {"d": desk_id})
    return row["line"] if row else None


def _line_of(database: Database, desk_id: int) -> str:
    return profile_line(database, desk_id) or "DT"           # flatten only closes; the line it carries is not a control


class _CallableModule(types.ModuleType):
    """`hokma.exec.submit(proposal, who=...)` calls the door: the module is callable."""

    def __call__(self, *args: Any, **kwargs: Any) -> ExecResult:
        return submit(*args, **kwargs)


sys.modules[__name__].__class__ = _CallableModule

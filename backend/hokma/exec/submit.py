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
import threading
import types
from contextlib import contextmanager
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from hokma.contracts import discipline, symbols
from hokma.contracts.identity import ContractSheet
from hokma.db import Database, get_database
from hokma.db.repos.broker import PRINCIPAL
from hokma.db.timeutil import utcnow
from hokma.exec import levels, prefilter, ticket
from hokma.exec.money import ZERO, dec_or_none, text as dtext
from hokma.exec.proposal import Leg, OrderProposal, ProposalError

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

_account_locks: dict[int, threading.RLock] = {}
_account_locks_guard = threading.Lock()


def account_lock(account_id: int) -> threading.RLock:
    """One lock per ACCOUNT, never per desk (SPEC-027 section 5): two desks that share an account send one after the other, so what the
    second reads of the account already counts what the first sent. SPEC-026 builds the shared risk budget on it. It is re-entrant: a
    caller that holds it around its own check and send calls `dispatch`, which takes it again, without waiting for itself. The app is one
    process; the broker keeps its own rules besides."""
    with _account_locks_guard:
        return _account_locks.setdefault(account_id, threading.RLock())


def dispatch(broker: Any, account_id: int, action: str, order: Any = None, order_id: Any = None, *,
             owner: tuple[int | None, str | None] | None = None) -> tuple[dict | None, int, str]:
    """The only call to the order methods of a broker, under the lock of the account. Returns (what the broker answered, HTTP status,
    outcome word). `owner` is the (desk, strategy) the order belongs to: a simulated broker books its fills with it."""
    from hokma.backoffice import desk_owner

    with account_lock(account_id), desk_owner.placing(*(owner or (None, None))):
        if action == "place":
            return broker.place_order(account_id, order), 201, "sent"
        if action == "replace":
            return broker.replace_order(account_id, order_id, order), 201, "replaced"
        if action == "cancel":
            return broker.cancel_order(account_id, order_id), 200, "canceled"
    raise ValueError(f"action must be place, replace or cancel; got {action!r}")


@contextmanager
def open_broker(database: Database, desk_id: int, account_id: int):
    """The broker and the account of an order, valid inside the block: the broker holds the session of an open unit. The account must be
    one of the desk's (`account_not_in_desk` otherwise)."""
    from hokma.broker import service

    with database.read() as u:
        yield service.broker_of(u, desk_id, account_id)


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
            decision_id: int | None = None, record_id: int | None = None, approved_by: str | None = None, approved: bool = False,
            account_id: int | None = None) -> int:
    fp = p.fingerprint()
    values = {"desk_id": p.desk_id, "account_id": account_id, "strategy_key": p.strategy_key, "line": p.line, "origin": p.origin, "level": level, "proposal_json": _json(p.to_json()), "fingerprint": fp,
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
        broker_order_id=broker_order_id, response=response, strategy_key=None if account is None else p.strategy_key)
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

def submit(p: OrderProposal, *, who: Any, db: Database | None = None, account_id: int | None = None) -> ExecResult:
    """The door. Records the proposal in `exec_proposals` and every attempt to send in `broker_orders`.

    The account of the order is resolved here (SPEC-027 section 5) and no caller or agent writes it: a desk with no account records the
    proposal as `analysis_only` and sends nothing; a strategy with no account waits for the owner (`awaiting_owner`, code
    `needs_account`); otherwise the order goes out by the account of its strategy. `account_id` is only for code that has the owner's
    answer (`approvals.choose_account`) or a flatten of one account: it must be an account of the desk."""
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
        resolved = service.resolve_account(u, p.desk_id, p.strategy_key, account_id)
        pair = None if resolved.account is None else (service._broker(u, resolved.account.broker_id), resolved.account)
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
                          decision_id=decision_id, record_id=rid, account_id=None if account is None else account.id)
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
    if resolved.state == "analysis_only":               # a desk without accounts runs its cycle and keeps what it proposes: no order, no error
        return finish("analysis_only", "no_account", ticket.why("analysis_only_no_account"), record=False)
    if resolved.state == "needs_account":                # the desk has accounts but this strategy has none: the owner says which one
        return _ask_account(database, p, who=who, level=level, desk_name=desk_name, sheets=sheets)
    assert pair is not None and account is not None
    preview = None
    if kind == "schwab":
        with open_broker(database, p.desk_id, account.id) as live:
            preview = _schwab_preview(live[0], live[1], to_native(p, sheets, kind))
    result = prefilter.run(p.desk_id, p, db=database, preview=preview, sheets=sheets, account_id=account.id)
    detail = result.to_json()
    if result.verdict == "BLOCK":
        block = result.blocked_by
        return finish("refused", block.code if block else "blocked", block.why if block else "", prefilter_json=detail, clamps=result.clamps)
    final = result.proposal
    text = ticket.ticket(final, desk_name=desk_name, level=level, contracts=sheets, clamps=result.clamps)

    if p.origin == "desk" and level == 1:
        return finish("analysis_only", "level_1", ticket.why("level_1"), prefilter_json=detail, clamps=result.clamps, record=False,
                      proposal=final, ticket_text=text)

    risk = _owner_risk(database, final, sheets, preview, account_id=account.id) if p.origin == "chat" and getattr(who, "owner", False) else None
    risk_why = None if risk is None else owner_risk_why(risk)
    if risk is not None:
        detail = {**detail, "owner_risk_ack": str(risk[2])}      # what the owner was warned of: a bigger exposure at the approval warns again
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
                          account_id=account.id, approved=True, approved_by=approver if approver == "owner" or (approver or "").startswith("rule:") else None)
        from hokma.exec import approvals

        out = approvals.send_approved(pid, db=database, auto_by=approver or "auto")
        return ExecResult(out.proposal_id, out.status, out.code, out.why, out.fingerprint, out.broker_order_record_id, None, text)

    from hokma.exec import notices, telegram_approval

    notice_rules = notices.rules_of(result)[0] if result.needs_owner else []         # a notice a grant of the day already covers does not ask again
    notice: dict[str, Any] | None = None
    with database.write() as u:
        decision_id = _decision(u, final, desk_name=desk_name, ticket_text=text, who=who)
        rid = _record_attempt(u, final, who=who, account=account, kind=kind, request=final.to_json(), rule_result="needs_owner",
                              why=risk_why or next((r.why for r in result.results if r.code == "needs_owner"), ticket.why("awaiting")))
        pid = _insert(u, final, level=level, status="awaiting_owner", code="owner_risk_confirm" if risk_why else "awaiting_owner", who=who, prefilter_json=detail, clamps=result.clamps,
                      decision_id=decision_id, record_id=rid, account_id=account.id)
        if notice_rules:                                  # a rule in notice mode tripped: the order waits with its own deadline and its three answers (SPEC-026, 3390b)
            notice = notices.attach(u, pid, desk_id=p.desk_id, account_id=account.id, rules=notice_rules, result=result)
            _notify(u, notices.message(notice, text, account.name))
        else:
            _notify(u, (risk_why or ticket.why("awaiting", desk=desk_name)) + "\n" + text)
    if notice is not None:
        notices.announce(database, pid, notice, desk_name=desk_name, account_label=account.name, ticket_text=text)
    else:
        telegram_approval.request_buttons(pid, final.fingerprint(), (risk_why + "\n" + text) if risk_why else text)
    if risk_why:
        return ExecResult(pid, "awaiting_owner", "owner_risk_confirm", risk_why, final.fingerprint(), rid, decision_id, text)
    return ExecResult(pid, "awaiting_owner", "awaiting_owner", ticket.why("awaiting", desk=desk_name), final.fingerprint(), rid, decision_id, text)


def _ask_account(database: Database, p: OrderProposal, *, who: Any, level: int, desk_name: str, sheets: dict[str, ContractSheet]) -> ExecResult:
    """The strategy of the proposal has no account and the desk has several to offer: the proposal waits for the owner with code
    `needs_account` and the chief asks in the chat which account it goes out by. Nothing is sent and no account is guessed."""
    text = ticket.ticket(p, desk_name=desk_name, level=level, contracts=sheets, clamps=())
    why = ticket.why("needs_account", desk=desk_name)
    with database.write() as u:
        decision_id = _decision(u, p, desk_name=desk_name, ticket_text=why + "\n" + text, who=who)
        pid = _insert(u, p, level=level, status="awaiting_owner", code="needs_account", who=who, decision_id=decision_id)
        _notify(u, why)
    return ExecResult(pid, "awaiting_owner", "needs_account", why, p.fingerprint(), None, decision_id, text)


def _pending_owner_orders(database: Database, desk_id: int, exclude_id: int | None = None) -> list[OrderProposal]:
    """The other orders of the owner in the chat that wait for his answer: they are not in the account yet, but the owner may confirm them all."""
    with database.read() as u:
        rows = u.session.query("SELECT id, proposal_json FROM exec_proposals WHERE desk_id = :d AND origin = 'chat' AND status = 'awaiting_owner' "
                               "AND id <> :i ORDER BY id", {"d": desk_id, "i": exclude_id or 0})
    out = []
    for row in rows:
        try:
            out.append(OrderProposal.from_dict(json.loads(row["proposal_json"])))
        except (ValueError, ProposalError):
            continue
    return out


def _field(order: Any, key: str) -> Any:
    return order.get(key) if isinstance(order, Mapping) else getattr(order, key, None)


def _unfilled(p: OrderProposal, order: Any) -> OrderProposal | None:
    """The part of a sent order of the owner that the broker has not filled: the proposal with every leg cut to the share that is left, or None
    when nothing is left. Criterion, in this order: a broker status that is done (filled, canceled, rejected, expired, replaced) leaves nothing,
    because a fill is already in the real position and the other four never reach it; otherwise what is left is `remaining_qty`, or
    `qty - filled_qty` when the broker gives no remaining, and a partial fill counts only that rest. When the quantities cannot be read the whole
    order counts: it can only warn more, never less. A broker without the `orders` capability leaves `open_orders` empty, so no sent order
    counts there (a known undercount)."""
    from hokma.exec.guards import _DONE

    if str(_field(order, "status") or "").upper() in _DONE:
        return None
    total, left = dec_or_none(_field(order, "qty")), dec_or_none(_field(order, "remaining_qty"))
    if left is None and total is not None:
        left = total - (dec_or_none(_field(order, "filled_qty")) or ZERO)
    if total is None or left is None or total <= 0:
        return p
    if left <= 0:
        return None
    if left >= total:
        return p
    return dataclasses.replace(p, legs=tuple(dataclasses.replace(g, qty=g.qty * left / total) for g in p.legs))


def _sent_unfilled_orders(database: Database, desk_id: int, open_orders: Sequence[Any],
                          exclude_id: int | None = None) -> tuple[list[OrderProposal], set[str]]:
    """The orders of the owner in the chat that were confirmed and sent to the broker and are still live there (bug 1243), each one as what it
    has left to fill (`_unfilled`). An order counts only while the broker still lists it as live, matched by its broker order id: one that
    is no longer listed was filled (it is in the positions already) or died, so counting it would be counting it twice. A sent order with no
    broker order id cannot be matched and does not count. Returns those orders and the broker order ids it matched: the caller takes those
    live orders out of the list it reads as live orders, so each sent order counts ONCE (as a pending order that moves the positions) and not
    again as a live order that reserves the position."""
    with database.read() as u:
        rows = u.session.query("SELECT p.proposal_json, b.broker_order_id FROM exec_proposals p JOIN broker_orders b ON b.id = p.broker_order_record_id "
                               "WHERE p.desk_id = :d AND p.origin = 'chat' AND p.status IN ('sent', 'partially_filled') AND p.id <> :i "
                               "AND b.broker_order_id IS NOT NULL ORDER BY p.id", {"d": desk_id, "i": exclude_id or 0})
    live = {str(_field(o, "order_id")): o for o in open_orders or () if _field(o, "order_id") is not None}
    out, matched = [], set()
    for row in rows:
        order = live.get(str(row["broker_order_id"]))
        if order is None:
            continue
        try:
            left = _unfilled(OrderProposal.from_dict(json.loads(row["proposal_json"])), order)
        except (ValueError, ProposalError):
            continue
        if left is not None:
            out.append(left)
            matched.add(str(row["broker_order_id"]))
    return out, matched


def _owner_risk(database: Database, final: OrderProposal, sheets: dict[str, ContractSheet], preview: Any, *, exclude_id: int | None = None,
                with_pending: bool = True, account_id: int | None = None) -> tuple[str, str, Decimal] | None:
    """What an order of the owner leaves open once the closes are netted against the account, the live orders, the orders of the owner already
    sent and not yet filled (bug 1243, always counted) and, with `with_pending`, the other orders of the owner that wait for his answer (H3,
    decisions 1265 and bug 1242). A reduce-only order cannot open."""
    from hokma.exec.guards.premium_selling import owner_risk

    if final.reduce_only:
        return None
    ctx = prefilter.load_context(final.desk_id, final, db=database, preview=preview, sheets=sheets, account_id=account_id)
    sent, matched = _sent_unfilled_orders(database, final.desk_id, ctx.open_orders, exclude_id)
    live = [o for o in ctx.open_orders or () if str(_field(o, "order_id")) not in matched]       # a sent order counts once: as pending, not as a live order too
    pending = [*(_pending_owner_orders(database, final.desk_id, exclude_id) if with_pending else ()), *sent]
    return owner_risk(final, ctx.contracts, marks=ctx.marks, positions=ctx.positions, open_orders=live, pending=pending)


def owner_risk_why(risk: tuple[str, str, Decimal]) -> str:
    """The warning in plain words, with the contract as a person reads it."""
    kind, contract, qty = risk
    return ticket.why("owner_risk_spread" if kind == "spread" else "owner_risk_short", qty=dtext(qty), contract=ticket.contract_label(contract))


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


def flatten(desk_id: int, *, reason: str, who: Any, db: Database | None = None, account_id: int | None = None,
            only: Mapping[str, Decimal] | None = None) -> list[ExecResult]:
    """Close every position of ONE account of the desk: one reduce-only market order per position, sent as a close. The account is
    `account_id` or the one of the principal strategy; a desk whose principal has no account needs it named. The owner, or a rule the
    owner approved with `flatten: true` at level 3, can ask for it. The positions are the account's: with an account shared by two desks
    this closes both desks' lots, unless `only` is given: then it closes just those contracts and never more than the signed quantity named for each
    (what a list the owner confirmed says, `hokma.exec.close_plan`), and cancels only the live orders that touch them."""
    from hokma.broker import service

    database = db or get_database()
    level, rules = levels.get_level(desk_id, db=database)
    if not getattr(who, "owner", False) and not (level == 3 and rules and rules.get("flatten") is True):
        raise service.owner_only("flattening a desk")
    with database.read() as u:
        desk = u.desks.find(desk_id)
        if desk is None:
            raise service.BrokerError("desk_not_found", 404)
        pair = service.broker_of_resolved(u, desk_id, account_id=account_id)
    if pair is None or not pair[0].capabilities.positions:
        raise service.BrokerError("no_trading_account", 409)
    account_id = pair[1].id
    with database.read() as u:                                    # refuse BEFORE canceling anything: a refused close must not leave the book without its orders
        facts = service._load(u, desk_id, account_id)
        status, enabled = facts.desk.status, facts.enabled
    owner = getattr(who, "owner", False) is True
    if status == "archived" or (not owner and status != "active"):
        raise service.BrokerError("desk_inactive", 409)
    if not owner and not enabled:
        raise service.BrokerError("orders_off", 409)
    line = _line_of(database, desk_id)
    cancel_live_orders(database, desk_id, reason=reason, who=who, account_id=account_id, contracts=None if only is None else set(only))          # nothing live can stand between the position and its close
    with database.read() as u:
        broker, account = service.broker_of(u, desk_id, account_id)
        positions = prefilter._normalize_positions(broker.positions(account.id))       # read AFTER the cancels: a fill while they went out changes the quantity
    if only is not None:
        positions = _within(positions, only)
    out: list[ExecResult] = []
    marker = prefilter.EMERGENCY_CLOSE.set((desk_id, "owner") if getattr(who, "owner", False) is True else None)   # only the owner's closes get past a pause
    try:
        return _flatten_positions(database, desk_id, positions, line, reason, who, out, account_id)
    finally:
        prefilter.EMERGENCY_CLOSE.reset(marker)


def _within(positions: Any, only: Mapping[str, Decimal]) -> list[Any]:
    """The positions a confirmed list allows to close: the contracts it names, on the side it names, never more than it says and never more than is held now."""
    from types import SimpleNamespace

    out = []
    for pos in positions:
        want, have = only.get(getattr(pos, "contract_id", None), Decimal(0)), Decimal(str(pos.qty))
        if want == 0 or have == 0 or (want > 0) != (have > 0):
            continue
        out.append(SimpleNamespace(contract_id=pos.contract_id, qty=min(abs(want), abs(have)) * (1 if have > 0 else -1)))
    return out


def _flatten_positions(database: Database, desk_id: int, positions: Any, line: str, reason: str, who: Any, out: list[ExecResult],
                       account_id: int | None = None) -> list[ExecResult]:
    for pos in positions:
        qty = Decimal(str(pos.qty))
        if qty == 0 or not getattr(pos, "contract_id", None):
            continue
        leg = Leg(pos.contract_id, "sell" if qty > 0 else "buy", "close", abs(qty))
        proposal = OrderProposal(desk_id, line, "chat" if getattr(who, "owner", False) else "rule", (leg,), "market", reduce_only=True,
                                 thesis=(reason or "flatten")[:4000], created_by=_who_name(who))
        out.append(submit(proposal, who=who, db=database, account_id=account_id))
    return out


CANCEL_WAIT_SECONDS = 10.0


def _live_orders(broker: Any, account_id: int) -> list[Any]:
    from hokma.exec.guards import _DONE

    if not broker.capabilities.orders:
        return []
    return [o for o in (broker.orders(account_id) or ()) if str(getattr(o, "status", "") or "").upper() not in _DONE]


def cancel_live_orders(database: Database, desk_id: int, *, reason: str, who: Any, wait: float = CANCEL_WAIT_SECONDS,
                       account_id: int | None = None, contracts: set[str] | None = None) -> list[str]:
    """First step of a flatten: cancel every live order that touches a contract the desk holds, or every live order of the account when one of
    them cannot be read whole, and wait until the broker confirms that none is live. Raises BrokerError cancel_unconfirmed when it cannot
    be confirmed: the close is then NOT sent, because a live order of the other side could still cross the position through zero.
    With `contracts` only the live orders on those contracts are canceled (the orders of the rest of the account are not touched), and a live order that
    cannot be read whole stops the close instead of being canceled with the rest."""
    import time

    from hokma.broker import service
    from hokma.exec.guards import scan_orders

    if account_id is None:                                      # the account of the principal strategy, as flatten does
        with database.read() as u:
            pair = service.broker_of_resolved(u, desk_id)
        if pair is None:
            raise service.BrokerError("no_trading_account", 409)
        account_id = pair[1].id
    with open_broker(database, desk_id, account_id) as live:
        broker, account = live
        held = set(contracts) if contracts is not None else {getattr(p, "contract_id", None) for p in prefilter._normalize_positions(broker.positions(account.id))
                                                             if Decimal(str(p.qty)) != 0}
        orders = _live_orders(broker, account.id)
        everything = not scan_orders(orders)[1]
        if everything and contracts is not None:
            raise service.BrokerError("cancel_unconfirmed", 409)               # it cannot be told which orders are the desk's: nothing is canceled or sent
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
                                  rule_result="auto", reason=(reason or "flatten")[:2000], strategy_key=PRINCIPAL)
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

"""Approval and the send of what was approved (SPEC-018: OA-39, OA-40, K7). The flow is proposal, fingerprint, approval, send:

- level 2: the owner approves the order (`approve`); level 3: a rule the owner approved covers it (`rule:<id>`); an order of the
  owner in the chat is its own approval. The row of `exec_proposals` keeps the approved fingerprint and who approved.
- `send_approved` sends only what was approved: it recomputes the fingerprint of the saved proposal and compares it with the approved
  one (`hash_mismatch` stops everything), claims the row atomically (two sends of the same fingerprint let ONE out), runs the filter
  again with the prices of this moment and then calls `submit.dispatch`."""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from decimal import Decimal, InvalidOperation
from typing import Any

from hokma.db import Actor, Database, IntegrityViolation, RuleViolation, get_database
from hokma.db.repos.broker import PRINCIPAL
from hokma.db.timeutil import from_db_time, utcnow
from hokma.exec import prefilter, ticket
from hokma.exec.guards import cooldown as cooldown_guard
from hokma.exec.proposal import OrderProposal, ProposalError
from hokma.exec.submit import (SENT_STATUSES, ExecResult, Refusal, account_lock, _insert, _json, _notify, _owner_risk, _record_attempt, dispatch, exits_apart, owner_risk_why,
                               resolve_contracts, to_native)

log = logging.getLogger("hokma.exec")

_desk_locks: dict[int, threading.RLock] = {}
_desk_locks_guard = threading.Lock()


def _desk_lock(desk_id: int) -> threading.RLock:
    """One lock per desk: the reading of the risk, the approval and the send of an order of the owner go together, so two confirmations at
    the same time are counted one after the other (bug 1242). The app is one process; the send keeps its own atomic claim besides."""
    with _desk_locks_guard:
        return _desk_locks.setdefault(desk_id, threading.RLock())


_FILLED = {"FILLED": "filled", "PARTIALLY_FILLED": "partially_filled", "WORKING": "sent", "ACCEPTED": "sent", "QUEUED": "sent", "PENDING_ACTIVATION": "sent"}


def _result(row: dict[str, Any], status: str, code: str, why: str = "", record_id: int | None = None) -> ExecResult:
    return ExecResult(row["id"], status, code, why, row["fingerprint"], record_id if record_id is not None else row.get("broker_order_record_id"),
                      row.get("decision_id"), "")


def _set(u: Any, pid: int, status: str, code: str, **extra: Any) -> None:
    u.session.update("exec_proposals", {"id": pid}, {"status": status, "code": code[:64], "updated_at": utcnow(), **extra})


def send_approved(proposal_id: int, *, db: Database | None = None, auto_by: str | None = None) -> ExecResult:
    """Send the approved order. Only a row that is `awaiting_owner` with an approved fingerprint can be claimed."""
    database = db or get_database()
    with database.read() as u:
        row = u.session.one("SELECT * FROM exec_proposals WHERE id = :i", {"i": proposal_id})
    if row is None:
        return ExecResult(proposal_id, "refused", "not_found", ticket.why("not_found"), "")
    try:
        with database.write() as u:
            claimed = u.session.execute(
                "UPDATE exec_proposals SET status = 'sent', code = 'claimed', updated_at = :t "
                "WHERE id = :i AND status = 'awaiting_owner' AND approved_fingerprint IS NOT NULL", {"i": proposal_id, "t": utcnow()})
    except IntegrityViolation:
        return _refuse_tampered(database, row)
    if claimed != 1:
        return _result(row, "refused", "already_sent", ticket.why("already_sent"))
    try:
        p = OrderProposal.from_dict(json.loads(row["proposal_json"]))
    except (ValueError, ProposalError):
        return _refuse_tampered(database, row)
    approved = row["approved_fingerprint"]
    if p.fingerprint() != approved or row["fingerprint"] != approved:
        return _refuse_tampered(database, row)
    return _send(database, row, p, auto_by or row.get("approved_by") or "owner")


def _refuse_tampered(database: Database, row: dict[str, Any]) -> ExecResult:
    why = ticket.why("hash_mismatch")
    with database.write() as u:
        u.session.execute("UPDATE exec_proposals SET status = 'refused', code = 'hash_mismatch', updated_at = :t, "
                          "approved_fingerprint = NULL WHERE id = :i", {"i": row["id"], "t": utcnow()})
        pair = _broker_pair(u, row)
        rid = u.broker.record_order(broker_kind="none" if pair is None else pair[0].kind, action="place", desk_id=row["desk_id"],
                                    account_id=None if pair is None else pair[1].id, request={"proposal_id": row["id"]}, rule_result="refused",
                                    rule_why=why, strategy_key=_strategy_of(row) if pair is not None else None).id
    return _result(row, "refused", "hash_mismatch", why, rid)


def _strategy_of(row: dict[str, Any]) -> str:
    return row.get("strategy_key") or PRINCIPAL


def _broker_pair(u: Any, row: dict[str, Any]):
    """The broker and the account the row was resolved to (`exec_proposals.account_id`). A row from before the account was saved resolves by its
    strategy. None when the order has no account, or the account is no longer one of the desk's: the caller refuses, it never takes another."""
    from hokma.broker import service

    try:
        if row.get("account_id") is not None:
            return service.broker_of(u, row["desk_id"], row["account_id"])
        return service.broker_of_resolved(u, row["desk_id"], _strategy_of(row))
    except service.BrokerError:
        return None


def _send(database: Database, row: dict[str, Any], p: OrderProposal, approver: str) -> ExecResult:
    with database.read() as u:
        pair = _broker_pair(u, row)
        desk = u.desks.find(p.desk_id)
        info = None if pair is None else (pair[0].kind, pair[1])
    if info is None or desk is None:
        return _finish(database, row, "refused", "no_account", ticket.why("no_account"), p, None, None)
    kind, account = info
    with account_lock(account.id):                         # the check and the send of one account go together: the next desk reads what this one sent
        return _send_locked(database, row, p, approver, kind, account, desk)


def _send_locked(database: Database, row: dict[str, Any], p: OrderProposal, approver: str, kind: str, account: Any, desk: Any) -> ExecResult:
    pid = row["id"]
    try:
        sheets = resolve_contracts(p)
        exits_apart(p, sheets)
    except Refusal as refusal:
        return _finish(database, row, "refused", refusal.code, refusal.why, p, account, kind)
    native = to_native(p, sheets, kind)
    preview = None
    if kind == "schwab":
        from hokma.exec.submit import _schwab_preview, open_broker

        with open_broker(database, p.desk_id, account.id) as live:
            preview = _schwab_preview(live[0], live[1], native)
    result = prefilter.run(p.desk_id, p, db=database, preview=preview, sheets=sheets, account_id=account.id)
    if result.verdict == "BLOCK":
        block = result.blocked_by
        return _finish(database, row, "refused", block.code if block else "blocked", block.why if block else "", p, account, kind)
    if result.proposal.fingerprint() != row["approved_fingerprint"]:
        return _finish(database, row, "refused", "changed_after_approval",
                       "the limits now cut this order, so it is not what was approved; it needs a new proposal", p, account, kind)
    if result.needs_owner and not approver == "owner":
        return _finish(database, row, "refused", "needs_owner", next((r.why for r in result.results if r.code == "needs_owner"), ""), p, account,
                       kind)
    if result.closing and kind != "schwab":
        native["reduce_only"] = True                          # a close the guards derived goes out as reduce-only: the broker cuts it to the real position at the fill
    try:
        from hokma.exec.submit import open_broker

        with open_broker(database, p.desk_id, account.id) as live:
            sent, http_status, _word = dispatch(live[0], live[1].id, "place", native, owner=(p.desk_id, p.strategy_key))
    except Exception as exc:                                  # the broker refused or did not answer: the record says so, nothing else changes
        code = str(getattr(exc, "code", None) or "broker_error")[:64]
        text = str(getattr(exc, "detail", None) or "the broker did not take the order")[:200]
        return _finish(database, row, "rejected_by_broker", code, text, p, account, kind)
    sent = sent or {}
    word = str(sent.get("status") or "WORKING").upper()
    status = _FILLED.get(word, "sent")
    if word in ("REJECTED", "CANCELED", "EXPIRED"):
        return _finish(database, row, "rejected_by_broker", str(sent.get("reason") or word.lower())[:64], str(sent.get("reason") or word.lower()), p, account,
                       kind, response=sent, http_status=http_status)
    stamp = None
    with database.write() as u:
        rec = u.broker.record_order(
            broker_kind=kind, action="place", desk_id=p.desk_id, account_id=account.id, agent_slug=p.created_by if u.agents.find(p.created_by) else None,
            reason=(p.thesis or None) and p.thesis[:2000], request=native, rule_result="auto", approved_by="owner" if approver == "owner" else "auto",
            http_status=http_status, broker_order_id=str(sent.get("order_id")) if sent.get("order_id") is not None else None,
            response={k: v for k, v in sent.items() if k in ("order_id", "status", "filled_qty", "avg_price", "correl_id", "reason")},
            strategy_key=p.strategy_key)
        _set(u, pid, status, "sent" if status == "sent" else status, broker_order_record_id=rec.id,
             **({} if row.get("approved_by") else {"approved_by": approver if approver == "owner" or approver.startswith("rule:") else None}))
        if status in ("filled", "partially_filled"):
            _notify(u, _fill_text(desk.name, p, sent))
        record_id = rec.id
        stamp = dt.datetime.now(dt.timezone.utc)
    cooldown_guard.record_sent(p.desk_id, [g.contract_id for g in p.legs], at=stamp, db=database)
    return _result(row, status, status if status != "sent" else "sent", "", record_id)


def _fill_text(desk: str, p: OrderProposal, sent: dict) -> str:
    from hokma.i18n import t

    return t("exec.notice.filled", desk=desk, summary=ticket.legs_text(p, {}), price=str(sent.get("avg_price") or "-"))


def _finish(database: Database, row: dict[str, Any], status: str, code: str, why: str, p: OrderProposal, account: Any, kind: str | None, *,
            response: Any = None, http_status: int | None = None) -> ExecResult:
    with database.write() as u:
        rid = u.broker.record_order(
            broker_kind=kind or "none", action="place", desk_id=p.desk_id, account_id=None if account is None else account.id, request=p.to_json(),
            rule_result="refused", rule_why=why[:4000] or None, http_status=http_status, response=response,
            strategy_key=None if account is None else p.strategy_key).id
        _set(u, row["id"], status, code, broker_order_record_id=rid)
        if status == "rejected_by_broker":
            _notify(u, ticket.why("rejected", desk=str(p.desk_id), why=why))
    return _result(row, status, code, why, rid)


# ------------------------------------------------------------------------------------------------------- owner answers

def _need_owner(who: Any) -> None:
    from hokma.broker.service import BrokerError

    if who is None or not getattr(who, "owner", False):
        raise BrokerError("owner_required", 403)


def _expired(decision: Any, row: dict[str, Any] | None = None) -> bool:
    """Past the wait: the one of the notice rule that asked (`exec_proposals.expires_at`), or the 30 minutes of every other pending order."""
    from hokma.broker.service import PENDING_MINUTES
    from hokma.exec import notices

    end = None if row is None else notices.deadline(row)
    if end is not None:
        return dt.datetime.now(dt.UTC) >= end
    return dt.datetime.now(dt.UTC) - from_db_time(decision.created_at) >= dt.timedelta(minutes=PENDING_MINUTES)


def _pending(database: Database, proposal_id: int):
    from hokma.broker.service import BrokerError

    with database.read() as u:
        row = u.session.one("SELECT * FROM exec_proposals WHERE id = :i", {"i": proposal_id})
        decision = u.decisions.get(row["decision_id"]) if row and row["decision_id"] else None
    if row is None:
        raise BrokerError("decision_not_found", 404)
    if row["status"] != "awaiting_owner" or decision is None or decision.status != "proposed":
        raise BrokerError("decision_closed", 409)
    return row, decision


def approve(proposal_id: int, *, who: Any, db: Database | None = None, scope: str | None = None) -> ExecResult:
    """The owner approves the order of a proposal. Past its wait (30 minutes, or the one of the notice rule) it expires instead. What is sent is the
    approved fingerprint. An order that waits because of a rule in NOTICE mode is not approved without `scope` ("order" or "day"): the owner is asked
    every time, and "day" is a temporary permission that ends with the session (SPEC-026, 3390b)."""
    from hokma.broker.service import BrokerError
    from hokma.exec import notices
    from hokma.i18n import t

    _need_owner(who)
    database = db or get_database()
    row, _decision = _pending(database, proposal_id)
    with _desk_lock(row["desk_id"]):
        row, decision = _pending(database, proposal_id)          # read again inside the lock: another confirmation may have closed it
        if _expired(decision, row):
            _expire(database, row, decision)
            raise BrokerError("decision_expired", 409)
        notice = notices.notice_of(row)
        if notice is not None and scope not in notices.SCOPES:
            raise BrokerError("scope_required", 409, text=t("exec.notice.scope_required"))
        if row["code"] == "needs_account":                        # nothing to approve until an account is chosen; once the strategy has one, it goes on
            return _answer_needs_account(database, row, decision, who)
        warned = _warn_again(database, row)
        if warned is not None:
            return warned
        with database.write() as u:
            approval = u.approvals.record("decision", decision.id, "approved", decided_by="owner", channel="dashboard")
            try:
                u.decisions.decide(decision.id, "approved", actor=Actor.human("owner"), approval_id=approval.id)
            except RuleViolation as exc:
                raise BrokerError("decision_closed", 409) from exc
            u.session.execute("UPDATE exec_proposals SET approved_fingerprint = fingerprint, approved_by = 'owner', updated_at = :t "
                              "WHERE id = :i AND status = 'awaiting_owner'", {"i": proposal_id, "t": utcnow()})
            if notice is not None and scope == "day":
                p = OrderProposal.from_dict(json.loads(row["proposal_json"]))
                until = notices.session_end([leg.contract_id for leg in p.legs])
                notices.grant_day(u, desk_id=row["desk_id"], account_id=row["account_id"], rules=notice.get("rules", []), proposal_id=proposal_id, until=until)
                _notify(u, t("exec.notice.followed_day", rules=", ".join(notice.get("rules", [])), until=until.strftime("%Y-%m-%d %H:%M UTC")))
        out = send_approved(proposal_id, db=database, auto_by="owner")
        _close_notice(row, "followed_day" if scope == "day" else "followed")
        return out


def _answer_needs_account(database: Database, row: dict[str, Any], decision: Any, who: Any) -> ExecResult:
    """`approve` on a proposal that waits for an account: if its strategy has an account by now (the owner tied it in the chat) the proposal goes
    through the door again by that account; if not it stays `needs_account`, never approved and never sent."""
    from hokma.broker.service import BrokerError, resolve_account

    p = OrderProposal.from_dict(json.loads(row["proposal_json"]))
    with database.read() as u:
        found = resolve_account(u, row["desk_id"], p.strategy_key)
    if found.account is None:
        raise BrokerError("needs_account", 409)
    return _resubmit(database, row, decision, p, found.account.id, who)


def _resubmit(database: Database, row: dict[str, Any], decision: Any, p: OrderProposal, account_id: int, who: Any) -> ExecResult:
    """Close the question (the owner answered it) and put the same proposal through the door with its account. The new row carries the
    account; the old one is `canceled` with code `account_chosen` so the pending list shows one proposal, not two."""
    from hokma.exec.submit import submit

    with database.write() as u:
        try:
            u.decisions.decide(decision.id, "rejected", actor=Actor.agent("hokma"))
        except RuleViolation as exc:
            from hokma.broker.service import BrokerError

            raise BrokerError("decision_closed", 409) from exc
        _set(u, row["id"], "canceled", "account_chosen")
    return submit(p, who=who, db=database, account_id=account_id)


def choose_account(proposal_id: int, account_id: int, *, who: Any, bind: bool = False, db: Database | None = None) -> ExecResult:
    """The owner says by which account a proposal that waits for one goes out (SPEC-027 section 5, point 3). `bind` also ties the strategy of
    the proposal to that account, so the next orders of the strategy need no question; without it only this order uses it. The account must
    be one of the desk's. The owner alone: an agent never chooses the account of an order."""
    from hokma.broker.service import BrokerError

    _need_owner(who)
    database = db or get_database()
    row, decision = _pending(database, proposal_id)
    if row["code"] != "needs_account":
        raise BrokerError("decision_closed", 409)
    with _desk_lock(row["desk_id"]):
        row, decision = _pending(database, proposal_id)
        if row["code"] != "needs_account":
            raise BrokerError("decision_closed", 409)
        if _expired(decision, row):
            _expire(database, row, decision)
            raise BrokerError("decision_expired", 409)
        p = OrderProposal.from_dict(json.loads(row["proposal_json"]))
        with database.write() as u:
            if account_id not in {a.id for a in u.broker.accounts_of_desk(row["desk_id"])}:
                raise BrokerError("account_not_in_desk", 409)
            if bind:
                u.broker.bind_strategy(row["desk_id"], p.strategy_key, account_id)
        return _resubmit(database, row, decision, p, account_id, who)


def _warn_again(database: Database, row: dict[str, Any]) -> ExecResult | None:
    """Bug 1242: an order of the owner is warned again when, by the time he confirms it, the account is more exposed than what he was told
    (nothing, for an order that never carried a warning). The new warning replaces the old one and the next confirmation goes through.
    A sale that cannot be measured is not approved: it fails closed. None when there is nothing to warn about: the approval goes on."""
    if row["origin"] != "chat":
        return None
    p = None
    try:
        p = OrderProposal.from_dict(json.loads(row["proposal_json"]))
        sheets = resolve_contracts(p)
        preview = None
        with database.read() as u:
            pair = _broker_pair(u, row)
        if pair is not None and pair[0].kind == "schwab":
            from hokma.exec.submit import _schwab_preview, open_broker

            with open_broker(database, p.desk_id, pair[1].id) as live:
                preview = _schwab_preview(live[0], live[1], to_native(p, sheets, "schwab"))
        risk = _owner_risk(database, p, sheets, preview, exclude_id=row["id"], with_pending=False, account_id=None if pair is None else pair[1].id)
    except (ValueError, ProposalError, Refusal):
        return None if p is not None and not any(x.side == "sell" for x in p.legs) else _unreadable(row)
    try:
        detail = json.loads(row["prefilter_json"] or "{}")
        ack = Decimal(str(detail.get("owner_risk_ack", "0")))
    except (ValueError, InvalidOperation):
        return _unreadable(row)
    if risk is None or risk[2] <= ack:
        return None
    why = owner_risk_why(risk)
    with database.write() as u:
        u.session.update("exec_proposals", {"id": row["id"]}, {"prefilter_json": _json({**detail, "owner_risk_ack": str(risk[2])}), "updated_at": utcnow()})
        _notify(u, why)
    from hokma.exec import telegram_approval

    telegram_approval.request_buttons(row["id"], row["fingerprint"], why)
    return _result(row, "awaiting_owner", "owner_risk_confirm", why)


def _unreadable(row: dict[str, Any]) -> ExecResult:
    """The risk of a sale could not be measured: nothing is approved or sent and the owner is told in words."""
    return _result(row, "awaiting_owner", "owner_risk_unreadable", ticket.why("owner_risk_unreadable"))


def reject(proposal_id: int, *, who: Any, db: Database | None = None) -> ExecResult:
    _need_owner(who)
    database = db or get_database()
    row, decision = _pending(database, proposal_id)
    from hokma.broker.service import BrokerError

    with database.write() as u:
        approval = u.approvals.record("decision", decision.id, "rejected", decided_by="owner", channel="dashboard")
        try:
            u.decisions.decide(decision.id, "rejected", actor=Actor.human("owner"), approval_id=approval.id)
        except RuleViolation as exc:
            raise BrokerError("decision_closed", 409) from exc
        _set(u, proposal_id, "canceled", "rejected_by_owner")
        pair = _broker_pair(u, row)
        rid = u.broker.record_order(broker_kind="none" if pair is None else pair[0].kind, action="place", desk_id=row["desk_id"],
                                    account_id=None if pair is None else pair[1].id, request={"proposal_id": proposal_id}, rule_result="refused",
                                    rule_why=ticket.why("owner_rejected")[:200], strategy_key=_strategy_of(row) if pair is not None else None).id
        u.session.update("exec_proposals", {"id": proposal_id}, {"broker_order_record_id": rid})
    _close_notice(row, "no")
    return _result(row, "canceled", "rejected_by_owner", "", rid)


def _close_notice(row: dict[str, Any], outcome: str) -> None:
    """The wait of a notice order is over (answered or expired): its timer is off and the open chats are told so the card goes."""
    from hokma.exec import notices

    if notices.notice_of(row) is None:
        return
    notices.disarm(row["id"])
    notices.publish(notices.closed_event(row["id"], outcome))


def close_ask(proposal_id: int, *, who: Any, db: Database | None = None, nonce: str | None = None) -> dict[str, Any]:
    """The third button, Close, FIRST step: nothing is closed. It works out exactly which positions of which desks would be closed (`close_plan`), keeps the plan
    on the pending order, moves the notice to the stage `close` with a new token (the old buttons stop working) and returns the plan with the text the owner
    must read. Owner only. `nonce` is the token of the tap in Telegram: the change is conditional on it, so two taps let one through."""
    import json as _json

    from hokma.broker.service import BrokerError
    from hokma.exec import close_plan, notices

    _need_owner(who)
    database = db or get_database()
    row, decision = _pending(database, proposal_id)
    notice = notices.notice_of(row)
    if notice is None:
        raise BrokerError("not_a_notice", 409)
    if row.get("account_id") is None:
        raise BrokerError("no_account", 409, text=ticket.why("no_account"))
    with _desk_lock(row["desk_id"]):
        row, decision = _pending(database, proposal_id)
        if _expired(decision, row):
            _expire(database, row, decision)
            raise BrokerError("decision_expired", 409)
        notice = notices.notice_of(row) or {}
        if notice.get("stage", "ask") not in ("ask", "close"):
            raise BrokerError("wrong_stage", 409)
        plan = close_plan.build(database, row["desk_id"], row["account_id"])
        fresh = notices.new_nonce()
        old = row.get("notice_nonce") if nonce is None else nonce
        with database.write() as u:
            changed = u.session.execute("UPDATE exec_proposals SET notice_nonce = :n, notice_json = :j, updated_at = :t "
                                        "WHERE id = :i AND status = 'awaiting_owner' AND notice_nonce = :o",
                                        {"n": fresh, "j": _json.dumps({**notice, "stage": "close", "close_plan": plan}, ensure_ascii=False), "t": utcnow(),
                                         "i": proposal_id, "o": old})
        if changed != 1:
            raise BrokerError("stale", 409)
    with database.read() as u:
        account = u.session.one("SELECT name FROM broker_accounts WHERE id = :a", {"a": row["account_id"]})
    return {"stage": "close", "plan": plan, "plan_token": close_plan.token(plan), "nonce": fresh, "text": close_plan.text(plan, account["name"] if account else "")}


def close_account(proposal_id: int, *, who: Any, db: Database | None = None, plan: str | None = None) -> str:
    """Close, SECOND step: only after `close_ask` showed the list. The order is rejected and the positions of that list, and no others, are closed with orders
    that only reduce. Without the confirmation stage nothing is rejected or closed; `plan` (the token of the list the chat showed) must be the one kept now. Owner only. The code of the result: `rejected_flat`, or
    `rejected_flatten_failed` when the order was rejected but the close could not be sent (the owner is told)."""
    from hokma.broker.service import BrokerError
    from hokma.exec import close_plan, notices, submit
    from hokma.i18n import t

    _need_owner(who)
    database = db or get_database()
    row, _decision = _pending(database, proposal_id)
    notice = notices.notice_of(row) or {}
    if notice.get("stage") != "close" or not isinstance(notice.get("close_plan"), dict):
        raise BrokerError("close_not_confirmed", 409, text=t("exec.notice.close_not_confirmed"))
    if row.get("account_id") is None:
        raise BrokerError("no_account", 409, text=ticket.why("no_account"))
    if plan is not None and plan != close_plan.token(notice["close_plan"]):
        raise BrokerError("stale_plan", 409, text=t("exec.notice.close_not_confirmed"))
    only = close_plan.only_of(notice["close_plan"])
    reject(proposal_id, who=who, db=database)
    if not only:
        with database.write() as u:
            _notify(u, t("exec.notice.closed", n="0"))
        return "rejected_flat"
    try:
        sent = submit.flatten(row["desk_id"], reason=t("exec.notice.close_reason"), who=who, db=database, account_id=row["account_id"], only=only)
    except BrokerError as exc:
        with database.write() as u:
            _notify(u, t("exec.notice.close_failed", why=exc.readable))
        return "rejected_flatten_failed"
    with database.write() as u:
        _notify(u, t("exec.notice.closed", n=str(len(sent))))
    return "rejected_flat"


def _expire(database: Database, row: dict[str, Any], decision: Any) -> bool:
    from hokma.exec import notices as notices_mod
    from hokma.i18n import t

    with database.write() as u:
        try:
            u.decisions.decide(decision.id, "rejected", actor=Actor.agent("hokma"))
        except RuleViolation:
            return False
        _set(u, row["id"], "expired", "expired")
        pair = _broker_pair(u, row)
        notice = notices_mod.notice_of(row)
        u.broker.record_order(broker_kind="none" if pair is None else pair[0].kind, action="place", desk_id=row["desk_id"],
                              account_id=None if pair is None else pair[1].id, request={"proposal_id": row["id"]}, rule_result="refused",
                              rule_why=ticket.why("expired")[:200], strategy_key=_strategy_of(row) if pair is not None else None)
        _notify(u, ticket.why("expired") if notice is None else t("exec.notice.expired", rules=", ".join(notice.get("rules", []))))
    _close_notice(row, "expired")
    return True


def expire_pending(db: Database | None = None) -> int:
    """Every proposal that waits for the owner for 30 minutes or more is rejected by Hokma. Returns how many expired."""
    database = db or get_database()
    with database.read() as u:
        rows = u.session.query("SELECT * FROM exec_proposals WHERE status = 'awaiting_owner' AND decision_id IS NOT NULL ORDER BY id")
        waiting = [(r, u.decisions.get(r["decision_id"])) for r in rows]
    return sum(1 for r, d in waiting if d.status == "proposed" and _expired(d, r) and _expire(database, r, d))


def list_pending(desk_id: int | None = None, *, db: Database | None = None) -> list[dict[str, Any]]:
    """The proposals that wait for the owner, oldest first, in the shape of the pending orders of SPEC-015."""
    from hokma.broker.service import PENDING_MINUTES
    from hokma.exec import notices

    database = db or get_database()
    expire_pending(database)
    sql = "SELECT * FROM exec_proposals WHERE status = 'awaiting_owner' AND decision_id IS NOT NULL"
    params: dict[str, Any] = {}
    if desk_id is not None:
        sql, params = sql + " AND desk_id = :d", {"d": desk_id}
    out = []
    with database.read() as u:
        for r in u.session.query(sql + " ORDER BY id", params):
            decision = u.decisions.get(r["decision_id"])
            if decision.status != "proposed":
                continue
            expires = notices.deadline(r) or from_db_time(decision.created_at) + dt.timedelta(minutes=PENDING_MINUTES)
            notice = notices.notice_of(r)
            try:
                body = json.loads(r["proposal_json"])
            except ValueError:
                body = {}
            out.append({"proposal_id": r["id"], "decision_id": decision.id, "action": "place", "agent": r["created_by"], "reason": body.get("thesis"),
                        "why": decision.proposal, "order": body, "replaces_order_id": None, "created_at": decision.created_at,
                        "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S"), "fingerprint": r["fingerprint"], "code": r["code"],
                        "needs_account": r["code"] == "needs_account", "strategy_key": r.get("strategy_key") or PRINCIPAL,
                        "account_id": r.get("account_id"),
                        "notice": None if notice is None else {"rules": notice.get("rules", []), "whys": notice.get("whys", []), "timeout_min": notice.get("timeout_min"),
                                                               "stage": notice.get("stage", "ask")}})
    return out


def proposal_of_decision(decision_id: int, *, db: Database | None = None) -> dict[str, Any] | None:
    with (db or get_database()).read() as u:
        return u.session.one("SELECT * FROM exec_proposals WHERE decision_id = :d", {"d": decision_id})


def proposal_row(proposal_id: int, *, db: Database | None = None) -> dict[str, Any] | None:
    with (db or get_database()).read() as u:
        return u.session.one("SELECT * FROM exec_proposals WHERE id = :i", {"i": proposal_id})

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
from hokma.db.timeutil import from_db_time, utcnow
from hokma.exec import prefilter, ticket
from hokma.exec.guards import cooldown as cooldown_guard
from hokma.exec.proposal import OrderProposal, ProposalError
from hokma.exec.submit import (SENT_STATUSES, ExecResult, Refusal, _insert, _json, _notify, _owner_risk, _record_attempt, dispatch, exits_apart, owner_risk_why,
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
        pair = _broker_pair(u, row["desk_id"])
        rid = u.broker.record_order(broker_kind="none" if pair is None else pair[0].kind, action="place", desk_id=row["desk_id"],
                                    account_id=None if pair is None else pair[1].id, request={"proposal_id": row["id"]}, rule_result="refused",
                                    rule_why=why).id
    return _result(row, "refused", "hash_mismatch", why, rid)


def _broker_pair(u: Any, desk_id: int):
    from hokma.broker import service

    return service.broker_of_desk(u, desk_id)


def _send(database: Database, row: dict[str, Any], p: OrderProposal, approver: str) -> ExecResult:
    from hokma.broker import service

    pid = row["id"]
    with database.read() as u:
        pair = _broker_pair(u, p.desk_id)
        desk = u.desks.find(p.desk_id)
        info = None if pair is None else (pair[0].kind, pair[1])
    if info is None or desk is None:
        return _finish(database, row, "refused", "no_account", ticket.why("no_account"), p, None, None)
    kind, account = info
    try:
        sheets = resolve_contracts(p)
        exits_apart(p, sheets)
    except Refusal as refusal:
        return _finish(database, row, "refused", refusal.code, refusal.why, p, account, kind)
    native = to_native(p, sheets, kind)
    preview = None
    if kind == "schwab":
        from hokma.exec.submit import _schwab_preview, open_broker

        with open_broker(database, p.desk_id) as live:
            preview = _schwab_preview(live[0], live[1], native)
    result = prefilter.run(p.desk_id, p, db=database, preview=preview, sheets=sheets)
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

        with open_broker(database, p.desk_id) as live:
            sent, http_status, _word = dispatch(live[0], live[1].id, "place", native)
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
            response={k: v for k, v in sent.items() if k in ("order_id", "status", "filled_qty", "avg_price", "correl_id", "reason")})
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
            rule_result="refused", rule_why=why[:4000] or None, http_status=http_status, response=response).id
        _set(u, row["id"], status, code, broker_order_record_id=rid)
        if status == "rejected_by_broker":
            _notify(u, ticket.why("rejected", desk=str(p.desk_id), why=why))
    return _result(row, status, code, why, rid)


# ------------------------------------------------------------------------------------------------------- owner answers

def _need_owner(who: Any) -> None:
    from hokma.broker.service import BrokerError

    if who is None or not getattr(who, "owner", False):
        raise BrokerError("owner_required", 403)


def _expired(decision: Any) -> bool:
    from hokma.broker.service import PENDING_MINUTES

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


def approve(proposal_id: int, *, who: Any, db: Database | None = None) -> ExecResult:
    """The owner approves the order of a proposal. Past 30 minutes it expires instead. What is sent is the approved fingerprint."""
    from hokma.broker.service import BrokerError

    _need_owner(who)
    database = db or get_database()
    row, _decision = _pending(database, proposal_id)
    with _desk_lock(row["desk_id"]):
        row, decision = _pending(database, proposal_id)          # read again inside the lock: another confirmation may have closed it
        if _expired(decision):
            _expire(database, row, decision)
            raise BrokerError("decision_expired", 409)
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
        return send_approved(proposal_id, db=database, auto_by="owner")


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
            pair = _broker_pair(u, p.desk_id)
        if pair is not None and pair[0].kind == "schwab":
            from hokma.exec.submit import _schwab_preview, open_broker

            with open_broker(database, p.desk_id) as live:
                preview = _schwab_preview(live[0], live[1], to_native(p, sheets, "schwab"))
        risk = _owner_risk(database, p, sheets, preview, exclude_id=row["id"], with_pending=False)
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
        pair = _broker_pair(u, row["desk_id"])
        rid = u.broker.record_order(broker_kind="none" if pair is None else pair[0].kind, action="place", desk_id=row["desk_id"],
                                    account_id=None if pair is None else pair[1].id, request={"proposal_id": proposal_id}, rule_result="refused",
                                    rule_why=ticket.why("owner_rejected")[:200]).id
        u.session.update("exec_proposals", {"id": proposal_id}, {"broker_order_record_id": rid})
    return _result(row, "canceled", "rejected_by_owner", "", rid)


def _expire(database: Database, row: dict[str, Any], decision: Any) -> bool:
    with database.write() as u:
        try:
            u.decisions.decide(decision.id, "rejected", actor=Actor.agent("hokma"))
        except RuleViolation:
            return False
        _set(u, row["id"], "expired", "expired")
        pair = _broker_pair(u, row["desk_id"])
        u.broker.record_order(broker_kind="none" if pair is None else pair[0].kind, action="place", desk_id=row["desk_id"],
                              account_id=None if pair is None else pair[1].id, request={"proposal_id": row["id"]}, rule_result="refused",
                              rule_why=ticket.why("expired")[:200])
        _notify(u, ticket.why("expired"))
    return True


def expire_pending(db: Database | None = None) -> int:
    """Every proposal that waits for the owner for 30 minutes or more is rejected by Hokma. Returns how many expired."""
    database = db or get_database()
    with database.read() as u:
        rows = u.session.query("SELECT * FROM exec_proposals WHERE status = 'awaiting_owner' AND decision_id IS NOT NULL ORDER BY id")
        waiting = [(r, u.decisions.get(r["decision_id"])) for r in rows]
    return sum(1 for r, d in waiting if d.status == "proposed" and _expired(d) and _expire(database, r, d))


def list_pending(desk_id: int | None = None, *, db: Database | None = None) -> list[dict[str, Any]]:
    """The proposals that wait for the owner, oldest first, in the shape of the pending orders of SPEC-015."""
    from hokma.broker.service import PENDING_MINUTES

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
            expires = from_db_time(decision.created_at) + dt.timedelta(minutes=PENDING_MINUTES)
            try:
                body = json.loads(r["proposal_json"])
            except ValueError:
                body = {}
            out.append({"proposal_id": r["id"], "decision_id": decision.id, "action": "place", "agent": r["created_by"], "reason": body.get("thesis"),
                        "why": decision.proposal, "order": body, "replaces_order_id": None, "created_at": decision.created_at,
                        "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S"), "fingerprint": r["fingerprint"]})
    return out


def proposal_of_decision(decision_id: int, *, db: Database | None = None) -> dict[str, Any] | None:
    with (db or get_database()).read() as u:
        return u.session.one("SELECT * FROM exec_proposals WHERE decision_id = :d", {"d": decision_id})


def proposal_row(proposal_id: int, *, db: Database | None = None) -> dict[str, Any] | None:
    with (db or get_database()).read() as u:
        return u.session.one("SELECT * FROM exec_proposals WHERE id = :i", {"i": proposal_id})

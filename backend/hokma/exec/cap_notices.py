"""The notice of a cap with no order in the way (SPEC-026 v2, piece 3390e4, task 3390). A rule the owner made a NOTICE reaches its cap in the reading of the broker: the monitor
(`hokma.exec.live`) raises ONE notice here, with what was measured (account, rule, the broker's number, the cap, the time of the reading), and the owner answers it with three
buttons, in the chat and in Telegram, the same way he answers the notice of an order (`hokma.exec.notices`):

- Keep trading today: it writes the permission of the day (`notice_grants`, per account, desk and rule: the notice of an order of that rule does not ask again that session) and the
  notice is not raised again in that session, unless the cap is a higher one (a scale of steps) and the number reaches it. It never edits the rule.
- Block entries today: the session becomes a restriction that only blocks entries (`live.trip` with `block_entries`: the day of the account is closed, or the stop of the desk,
  until its session ends). Closes are never blocked.
- Close positions: two steps. The first one shows exactly what would be closed and what stays (shares and ETFs held long stay unless the rule says to close everything) and changes the
  token; the second one runs `live.close_positions` on that list. If the book changed between the two, the list is shown again and nothing is closed.

With no answer when the wait of the rule ends, the notice expires and does NOTHING else: it does not close and does not block; it stays written, and the openings keep going through the
notice of each order. Nobody answers for the owner: every answer takes a Who with owner=True (the routes of the app and the authenticated Telegram buttons), and the token of a Telegram
button is used once."""
from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hokma.broker import rule_modes
from hokma.db import Database, get_database
from hokma.db.timeutil import FORMAT, from_db_time, utcnow
from hokma.i18n import t

log = logging.getLogger("hokma.exec.cap")

NONCE_HEX = 12
_CALLBACK = re.compile(r"oc:([sbcxk]):([0-9]{1,12}):([0-9a-f]{12})")
FOLLOW, BLOCK, CLOSE, CONFIRM_CLOSE, BACK = "s", "b", "c", "x", "k"
FLATTEN_ALL = "block_and_flatten_all"


def new_nonce() -> str:
    return secrets.token_hex(NONCE_HEX // 2)


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _money(value: Any) -> str:
    return f"{_d(value):,.2f}"


def _need_owner(who: Any) -> None:
    from hokma.broker.service import BrokerError

    if who is None or not getattr(who, "owner", False):
        raise BrokerError("owner_required", 403)


# ----------------------------------------------------------------------------------------------------------------------- raising it

def _get(database: Database, cap_id: int) -> dict[str, Any] | None:
    with database.read() as u:
        return u.session.one("SELECT * FROM cap_notices WHERE id = :i", {"i": cap_id})


def _labels(database: Database, row: Mapping[str, Any]) -> tuple[str, str]:
    """(the name of the account, the name of the desk of the rule or '')."""
    with database.read() as u:
        account = u.session.one("SELECT name FROM broker_accounts WHERE id = :a", {"a": row["account_id"]})
        desk = None if row.get("desk_id") is None else u.desks.find(row["desk_id"])
    return (account["name"] if account else str(row["account_id"])), (desk.name if desk else "")


def _suppressed(rows: list[Mapping[str, Any]], cap: Decimal) -> bool:
    """Is there an answer of the owner, or a notice still waiting, that covers this reach in this session? A notice that expired unanswered does not: it is raised again after the
    number fell below the cap and reached it again (the monitor decides that; here only what the owner already said counts)."""
    for r in rows:
        if r["status"] == "waiting":
            return True
        if r["status"] == "blocked":
            return True                                          # the entries are blocked for the session: nothing else to ask
        if r["status"] in ("followed", "closed") and _d(r["cap"]) >= cap:
            return True                                          # he already answered for this cap; only a higher one asks again
    return False


FAILED = -1


def raise_notice(database: Database, m: Any, ev: Any) -> int | None:
    """Open the notice of a rule in notice mode that reached its cap, and tell the owner (chat, Telegram, the text in the outbox). Returns the id of the notice raised; None when this
    session already has an answer or a notice that waits (a deliberate suppression: the caller may stop asking); FAILED when the notice could not be raised or reached the owner by no
    channel (the log has the reason, nothing stays written that hides a new one, and the caller must try again on the next reading). It never raises: a failure here must not stop the
    monitor from evaluating the other rules."""
    try:
        return _raise(database, m, ev)
    except Exception:                                              # noqa: BLE001
        log.exception("the notice of a cap could not be raised")
        return FAILED


def _undo(database: Database, cap_id: int) -> None:
    """A notice that reached the owner by no channel is closed as undelivered (it does not wait and does not suppress the next one)."""
    try:
        with database.write() as u:
            u.session.execute("UPDATE cap_notices SET status = 'expired', outcome = 'undelivered', nonce = NULL, answered_at = :t, answered_by = 'hokma' WHERE id = :i AND status = 'waiting'",
                              {"t": utcnow(), "i": cap_id})
    except Exception:                                              # noqa: BLE001
        log.exception("the undelivered notice of a cap could not be closed")


def _raise(database: Database, m: Any, ev: Any) -> int | None:
    from hokma.exec import live

    rule, cap, value = ev.rule, _d(ev.cap), _d(ev.value)
    expire_due(database)                                           # a notice whose wait is over is closed first: it does not hide a new one
    minutes = int(rule.timeout_min or rule_modes.DEFAULT_TIMEOUT_MIN)
    nonce = new_nonce()
    from datetime import UTC, datetime, timedelta

    expires = (datetime.now(UTC) + timedelta(minutes=minutes)).strftime(FORMAT)
    with database.write() as u:
        rows = u.session.query("SELECT status, cap FROM cap_notices WHERE account_id = :a AND rule_id = :r AND session_key = :s", {"a": m.account_id, "r": rule.rule_id, "s": m.session_key})
        if _suppressed(rows, cap):
            return None
        cap_id = u.session.insert("cap_notices", {
            "account_id": m.account_id, "rule_id": rule.rule_id[:60], "control": rule.control, "metric": rule.metric, "scope": rule.scope, "desk_id": rule.desk_id,
            "on_trip": rule.on_trip, "session_key": m.session_key, "session_ends_at": m.session_ends_at, "value": str(value), "cap": str(cap), "read_at": ev.at,
            "timeout_min": minutes, "nonce": nonce, "stage": "ask", "status": "waiting", "created_at": utcnow(), "expires_at": expires})
    from hokma.exec import notices, telegram_approval

    try:
        row = _get(database, cap_id) or {}
        account, desk = _labels(database, row)
        text = message(row, account)
    except Exception:
        _undo(database, cap_id)
        raise
    channels = (lambda: live.notify_reached(database, text),                            # the queued text: it is all the owner gets when there is no Telegram link
                lambda: telegram_approval.request_cap_buttons(cap_id, nonce, text),
                lambda: notices.publish_reached(card(row, account=account, desk=desk)))
    reached = 0
    for send in channels:                                           # each channel alone: one that fails never stops the others
        try:
            if send() is True:                                      # only a real success counts: False (failed) and None (channel not set up) do not
                reached += 1
        except Exception:                                          # noqa: BLE001
            log.exception("a channel of the notice of a cap failed")
    if not reached:
        _undo(database, cap_id)
        raise RuntimeError("the notice of a cap reached the owner by no channel")
    try:
        arm(cap_id, expires, database)
    except Exception:                                              # noqa: BLE001  (the sweep of every tick closes it at its time)
        log.exception("the timer of the notice of a cap could not be armed")
    return cap_id


def message(row: Mapping[str, Any], account_label: str) -> str:
    """The text the owner reads, in his language: account, rule, the broker's number and the cap, the time of the reading and what happens if he does not answer."""
    what = t("live.freeze.what.drawdown") if row["metric"] == "drawdown" else t("live.freeze.what.loss")
    return t("live.notice.reached", account=account_label, rule=row["rule_id"], what=what, value=_money(row["value"]), cap=_money(row["cap"]), at=row.get("read_at") or "?",
             minutes=str(row["timeout_min"]))


def card(row: Mapping[str, Any], *, account: str = "", desk: str = "") -> dict[str, Any]:
    """The card of the chat: everything the owner needs to answer, with no token (the chat answers through the routes of the app)."""
    return {"type": "cap_notice", "cap_id": row["id"], "account_id": row["account_id"], "account": account, "desk": desk, "rule_id": row["rule_id"], "control": row["control"],
            "metric": row["metric"], "value": _money(row["value"]), "cap": _money(row["cap"]), "read_at": row.get("read_at"), "timeout_min": row["timeout_min"],
            "expires_at": row["expires_at"], "stage": row["stage"]}


def closed_event(cap_id: int, outcome: str) -> dict[str, Any]:
    return {"type": "cap_notice_closed", "cap_id": cap_id, "outcome": outcome}


def pending_cards(db: Database | None = None) -> list[dict[str, Any]]:
    """The cards of every notice of a cap that waits now, oldest first: the chat asks for them when it opens, so a card sent while it was closed is not lost."""
    database = db or get_database()
    expire_due(database)
    with database.read() as u:
        rows = u.session.query("SELECT * FROM cap_notices WHERE status = 'waiting' ORDER BY id")
    out = []
    for row in rows:
        account, desk = _labels(database, row)
        out.append(card(row, account=account, desk=desk))
    return out


# ----------------------------------------------------------------------------------------------------------------------- the wait

_timers: dict[int, threading.Timer] = {}
_timers_guard = threading.Lock()


def arm(cap_id: int, expires_at: str, db: Database | None = None) -> None:
    """Wake the sweep at the exact end of the wait of this notice (one timer per notice of the main process). The monitor also sweeps on every tick."""
    import datetime as dt

    delay = max(0.0, (from_db_time(expires_at) - dt.datetime.now(dt.UTC)).total_seconds()) + 1.0

    def fire() -> None:
        with _timers_guard:
            _timers.pop(cap_id, None)
        try:
            expire_due(db)
        except Exception:                                          # noqa: BLE001
            log.warning("the sweep of the notices of a cap failed at the end of a wait")

    timer = threading.Timer(delay, fire)
    timer.daemon = True
    with _timers_guard:
        old = _timers.pop(cap_id, None)
        _timers[cap_id] = timer
    if old is not None:
        old.cancel()
    timer.start()


def disarm(cap_id: int) -> None:
    with _timers_guard:
        timer = _timers.pop(cap_id, None)
    if timer is not None:
        timer.cancel()


def rearm_all(db: Database | None = None) -> int:
    """At start: the notices that wait get their timer again (the process restarted); the ones already late expire now."""
    database = db or get_database()
    expire_due(database)
    with database.read() as u:
        rows = u.session.query("SELECT id, expires_at FROM cap_notices WHERE status = 'waiting'")
    for r in rows:
        arm(r["id"], r["expires_at"], database)
    return len(rows)


def expire_due(db: Database | None = None) -> int:
    """Every notice whose wait is over and nobody answered is marked expired. NOTHING else happens: it does not close and does not block, and the openings keep going through the
    notice of each order. Returns how many expired."""
    from hokma.exec import live, notices

    database = db or get_database()
    with database.read() as u:
        due = u.session.query("SELECT * FROM cap_notices WHERE status = 'waiting' AND expires_at <= :n ORDER BY id", {"n": utcnow()})
    done = 0
    for row in due:
        with database.write() as u:
            changed = u.session.execute("UPDATE cap_notices SET status = 'expired', outcome = 'expired', nonce = NULL, answered_at = :t, answered_by = 'hokma' WHERE id = :i AND status = 'waiting'",
                                        {"t": utcnow(), "i": row["id"]})
        if changed != 1:
            continue
        done += 1
        disarm(row["id"])
        account, _desk = _labels(database, row)
        live._notify(database, t("cap.notice.expired", account=account, rule=row["rule_id"]))
        notices.publish(closed_event(row["id"], "expired"))
    return done


# ----------------------------------------------------------------------------------------------------------------------- the answers

def _open(database: Database, cap_id: int) -> dict[str, Any]:
    """The notice, when it still waits and its wait is not over. A late one expires instead of being answered."""
    from hokma.broker.service import BrokerError

    row = _get(database, cap_id)
    if row is None:
        raise BrokerError("decision_not_found", 404)
    if row["status"] != "waiting":
        raise BrokerError("decision_closed", 409)
    if row["expires_at"] <= utcnow():
        expire_due(database)
        raise BrokerError("decision_expired", 409)
    return row


def _settle(u: Any, row: Mapping[str, Any], status: str, outcome: str, nonce: str | None) -> None:
    """Close the notice with the answer of the owner, in the transaction of the caller. Conditional on its state (and on the token when it comes from Telegram): two answers at once let one through."""
    from hokma.broker.service import BrokerError

    sql = "UPDATE cap_notices SET status = :s, outcome = :o, nonce = NULL, answered_at = :t, answered_by = 'owner' WHERE id = :i AND status = 'waiting'"
    params: dict[str, Any] = {"s": status, "o": outcome, "t": utcnow(), "i": row["id"]}
    if nonce is not None:
        sql, params["n"] = sql + " AND nonce = :n", nonce
    if u.session.execute(sql, params) != 1:
        raise BrokerError("stale", 409)


def _end(cap_id: int, outcome: str) -> None:
    from hokma.exec import notices

    disarm(cap_id)
    notices.publish(closed_event(cap_id, outcome))


def follow(cap_id: int, *, who: Any, db: Database | None = None, nonce: str | None = None) -> dict[str, Any]:
    """Keep trading today: the permission of the day is written for the rule and the account (one row per desk of the account, or the desk of a desk rule) and ends with the
    session. Nothing else changes: the rule keeps its limits."""
    from hokma.exec import live

    _need_owner(who)
    database = db or get_database()
    row = _open(database, cap_id)
    with database.write() as u:
        _settle(u, row, "followed", "followed", nonce)
        desks = [row["desk_id"]] if row.get("desk_id") is not None else u.broker.desks_of_account(row["account_id"])
        for desk_id in desks:
            u.session.insert("notice_grants", {"account_id": row["account_id"], "desk_id": desk_id, "rule_id": row["control"][:60], "proposal_id": None, "granted_by": "owner",
                                               "granted_at": utcnow(), "expires_at": row["session_ends_at"]})
    account, _desk = _labels(database, row)
    live._notify(database, t("cap.notice.followed", rule=row["rule_id"], account=account, until=f"{row['session_ends_at']} UTC"))
    _end(cap_id, "followed")
    return {"status": "followed", "cap_id": cap_id}


def block(cap_id: int, *, who: Any, db: Database | None = None, nonce: str | None = None) -> dict[str, Any]:
    """Block entries today: this session becomes a restriction that only blocks entries, with the number the notice measured. Closes are never blocked."""
    from hokma.exec import live

    _need_owner(who)
    database = db or get_database()
    row = _open(database, cap_id)
    try:
        rule = live.Rule(row["rule_id"], row["control"], row["metric"], row["scope"], None if row["scope"] == "desk" else row["account_id"], row["desk_id"], on_trip="block_entries", mode="restriction")
        measured = live.measure(row["account_id"], db=database)
        live.trip(database, measured, live.Eval(rule, "reached", _d(row["value"]), _d(row["cap"]), "", row.get("read_at")))        # first the block: the answer is written only if it worked
    except Exception as exc:                                       # noqa: BLE001  (said to the owner exactly: nothing is blocked, the notice keeps waiting and the button can be pressed again)
        log.exception("the block of a notice of a cap failed")
        live._notify(database, t("cap.notice.block_failed", reason=str(exc) or type(exc).__name__))
        return {"status": "block_failed", "cap_id": cap_id}
    with database.write() as u:
        _settle(u, row, "blocked", "blocked", nonce)
    _end(cap_id, "blocked")
    return {"status": "blocked", "cap_id": cap_id}


def _desk_of(row: Mapping[str, Any]) -> int | None:
    return row["desk_id"] if row.get("desk_id") is not None else None


def _plan(database: Database, row: Mapping[str, Any]) -> dict[str, Any]:
    """What Close positions would do, from the book (the same one `live.close_positions` reads, after pulling the fills): the positions of the account, or of the desk of a desk rule,
    without shares or ETFs held long unless a rule of the owner says to close everything (`block_and_flatten_all`, the same answer as the notice of an order). Quantities are text."""
    from hokma.exec import close_plan, live

    desk_id = _desk_of(row)
    with database.read() as u:
        anchor = desk_id if desk_id is not None else next(iter(u.broker.desks_of_account(row["account_id"])), None)
        include_long = row.get("on_trip") == FLATTEN_ALL or (anchor is not None and close_plan.flatten_all_chosen(u, anchor, row["account_id"]))      # only a rule of the owner that closes everything
    live._pull_quietly(database, row["account_id"])
    held = live._open_of(database, row["account_id"], desk_id)
    names: list[str] = []
    if desk_id is not None:
        with database.read() as u:
            desk = u.desks.find(desk_id)
        names = [desk.name] if desk else []
    plan: dict[str, Any] = {"all": include_long, "close": [], "keep": []}
    for cid, qty in sorted(held.items()):
        line = close_plan._line(cid, qty, names)
        if include_long or not live._is_long_holding(cid, qty):
            plan["close"].append(line)
        else:
            plan["keep"].append({**line, "why": "long_holding"})
    return plan


def plan_text(plan: Mapping[str, Any], account_label: str) -> str:
    """The confirmation the owner reads, in his language: what will be closed and what stays."""
    def one(line: Mapping[str, Any]) -> str:
        who = ", ".join(line.get("desks") or []) or t("exec.notice.close_unknown_desk")
        why = "" if "why" not in line else " [" + t("exec.notice.close_keep_long_holding") + "]"
        return f"- {str(line['side']).upper()} {str(line['qty']).lstrip('-')} {line['contract_id']} ({who}){why}"

    parts = [t("cap.close.head", account=account_label)]
    if plan.get("all"):
        parts.append(t("exec.notice.close_all"))
    parts += [one(line) for line in plan.get("close", [])] or [t("cap.close.none")]
    if plan.get("keep"):
        parts.append(t("exec.notice.close_keep"))
        parts += [one(line) for line in plan["keep"]]
    parts.append(t("exec.notice.close_ask"))
    return "\n".join(parts)


def close_ask(cap_id: int, *, who: Any, db: Database | None = None, nonce: str | None = None) -> dict[str, Any]:
    """Close positions, FIRST step: nothing is closed. It works out the list, keeps it on the notice, moves it to the stage `close` with a new token (the old buttons stop working)
    and returns the list with the text the owner must read. `nonce` is the token of the tap in Telegram: the change is conditional on it."""
    from hokma.broker.service import BrokerError

    _need_owner(who)
    database = db or get_database()
    row = _open(database, cap_id)
    plan = _plan(database, row)
    fresh = new_nonce()
    sql = "UPDATE cap_notices SET nonce = :n, stage = 'close', close_plan = :p WHERE id = :i AND status = 'waiting'"
    params: dict[str, Any] = {"n": fresh, "p": json.dumps(plan, ensure_ascii=False), "i": cap_id}
    if nonce is not None:
        sql, params["o"] = sql + " AND nonce = :o", nonce
    with database.write() as u:
        if u.session.execute(sql, params) != 1:
            raise BrokerError("stale", 409)
    from hokma.exec import close_plan

    account, _desk = _labels(database, row)
    return {"stage": "close", "plan": plan, "plan_token": close_plan.token(plan), "nonce": fresh, "text": plan_text(plan, account)}


def back(cap_id: int, *, who: Any, db: Database | None = None, nonce: str | None = None) -> dict[str, Any]:
    """The owner looked at the list and did not confirm: the notice goes back to its three answers with a new token. Nothing was closed."""
    from hokma.broker.service import BrokerError

    _need_owner(who)
    database = db or get_database()
    row = _open(database, cap_id)
    fresh = new_nonce()
    sql = "UPDATE cap_notices SET nonce = :n, stage = 'ask', close_plan = NULL WHERE id = :i AND status = 'waiting' AND stage = 'close'"
    params: dict[str, Any] = {"n": fresh, "i": cap_id}
    if nonce is not None:
        sql, params["o"] = sql + " AND nonce = :o", nonce
    with database.write() as u:
        if u.session.execute(sql, params) != 1:
            raise BrokerError("stale", 409)
    account, _desk = _labels(database, row)
    return {"stage": "ask", "nonce": fresh, "text": message({**row, "stage": "ask"}, account)}


def close_confirm(cap_id: int, *, who: Any, db: Database | None = None, plan: str | None = None, nonce: str | None = None) -> str:
    """Close positions, SECOND step: only after `close_ask` showed the list. `plan` (the token of the list the chat showed) must be the one kept now, and the list worked out again must
    still be that one: if the book changed, nothing is closed and the owner is told to look at the new list. Returns `closed`, or `close_failed` when the close could not be sent (the
    owner is told exactly)."""
    from hokma.broker.service import BrokerError
    from hokma.exec import close_plan, live

    _need_owner(who)
    database = db or get_database()
    row = _open(database, cap_id)
    if row["stage"] != "close" or not row.get("close_plan"):
        raise BrokerError("close_not_confirmed", 409, text=t("exec.notice.close_not_confirmed"))
    shown = json.loads(row["close_plan"])
    if plan is not None and plan != close_plan.token(shown):
        raise BrokerError("stale_plan", 409, text=t("exec.notice.close_not_confirmed"))
    fresh = _plan(database, row)
    if close_plan.token(fresh) != close_plan.token(shown):
        raise BrokerError("stale_plan", 409, text=t("cap.close.changed"))
    with database.write() as u:
        _settle(u, row, "closed", "closed", nonce)
    desk_id, include_long = _desk_of(row), bool(fresh["all"])
    status, outcome = "closed", "closed"
    try:
        text, remaining = live.close_positions(database, row["account_id"], desk_id, include_long_shares=include_long)
        monitor = live.monitor()
        if remaining and monitor is not None:
            monitor.retry_close(row["account_id"], desk_id, include_long)          # what was left open is closed again on each tick, and the owner is told when it ends
        account, _desk = _labels(database, row)
        live._notify(database, t("cap.notice.closed", account=account, text=text))
    except Exception:                                              # noqa: BLE001  (said to the owner exactly: close them by hand)
        log.exception("the close of a notice of a cap failed")
        status = outcome = "close_failed"
        with database.write() as u:
            u.session.execute("UPDATE cap_notices SET outcome = 'close_failed' WHERE id = :i", {"i": cap_id})
        live._notify(database, t("cap.notice.close_failed", rule=row["rule_id"]))
    _end(cap_id, outcome)
    return status


# ----------------------------------------------------------------------------------------------------------------------- Telegram

@dataclass(frozen=True)
class Tap:
    """What a tap of a Telegram button did: the code of the result and, when the notice goes on to another step, the new token and the text for the next buttons."""
    code: str
    cap_id: int | None = None
    nonce: str | None = None
    text: str = ""
    stage: str = ""


def parse_callback(data: str) -> tuple[str, int, str] | None:
    match = _CALLBACK.fullmatch(data or "")
    return None if match is None else (match.group(1), int(match.group(2)), match.group(3))


def callback_data(choice: str, cap_id: int, nonce: str) -> str:
    return f"oc:{choice}:{cap_id}:{nonce}"


def tap(data: str, *, who: Any, db: Database | None = None) -> Tap:
    """The owner tapped a button of a notice of a cap in Telegram. The token must be the one of this moment and the answer spends it."""
    from hokma.broker.service import BrokerError

    _need_owner(who)
    parsed = parse_callback(data)
    if parsed is None:
        return Tap("invalid")
    choice, cap_id, nonce = parsed
    database = db or get_database()
    row = _get(database, cap_id)
    if row is None or row["status"] != "waiting" or not row.get("nonce") or not hmac.compare_digest(str(row["nonce"]), nonce):
        return Tap("stale", cap_id)
    try:
        if choice == FOLLOW:
            return Tap(follow(cap_id, who=who, db=database, nonce=nonce)["status"], cap_id)
        if choice == BLOCK:
            return Tap(block(cap_id, who=who, db=database, nonce=nonce)["status"], cap_id)
        if choice == CLOSE and row["stage"] == "ask":
            asked = close_ask(cap_id, who=who, db=database, nonce=nonce)
            return Tap("close_confirm", cap_id, asked["nonce"], asked["text"], "close")
        if choice == CONFIRM_CLOSE and row["stage"] == "close":
            return Tap(close_confirm(cap_id, who=who, db=database, nonce=nonce), cap_id)
        if choice == BACK and row["stage"] == "close":
            went = back(cap_id, who=who, db=database, nonce=nonce)
            return Tap("back", cap_id, went["nonce"], went["text"], "ask")
        return Tap("wrong_stage", cap_id)
    except BrokerError as exc:
        return Tap({"decision_expired": "expired", "decision_closed": "stale", "decision_not_found": "stale", "stale_plan": "changed", "close_not_confirmed": "wrong_stage"}.get(exc.code, "other"), cap_id)

"""The notice mode with buttons (SPEC-026, piece 3390b). When a rule the owner made a NOTICE trips, the order does not go out and does not die: it waits
for him as a card in the chat and a message in Telegram with three answers, Follow, No and Close. This module keeps what that needs:

- what the pending order remembers (which rules tripped, its own deadline, the one-use token of the Telegram buttons);
- "Follow" asks the scope EVERY time: only this order, or the rest of the day. The rest of the day is a temporary permission (notice_grants), per
  account, desk and rule, that ends with the session of the account and leaves a row. It never touches the rule: the limits stay as they were, and a
  RESTRICTION is never covered by a grant;
- the deadline of the rule: when it passes the order is NOT sent. A timer of the app wakes the sweep at the exact moment (no polling of anything), and the
  sweep that already runs when the list is read stays as the second net;
- the card of the chat: the events that tell the open chats a notice is waiting or is over.

Nobody here answers for the owner: every answer takes a Who with owner=True (the routes of the app and the authenticated Telegram buttons) and an agent is
refused with 403. The Telegram callback carries a token that is used once: an answer rotates it, so a copy of an old button does nothing."""
from __future__ import annotations

import datetime as dt
import hmac
import json
import logging
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from hokma.broker import rule_modes
from hokma.db import Database, get_database
from hokma.db.timeutil import FORMAT, from_db_time, utcnow

log = logging.getLogger("hokma.exec")

SCOPES = ("order", "day")
NONCE_HEX = 12
_CALLBACK = re.compile(r"on:([fncx]|o|d):([0-9]{1,12}):([0-9a-f]{12})")
FOLLOW, NO, CLOSE, ONLY_ORDER, REST_OF_DAY, CONFIRM_CLOSE = "f", "n", "c", "o", "d", "x"


def new_nonce() -> str:
    return secrets.token_hex(NONCE_HEX // 2)


# ------------------------------------------------------------------------------------------------------------- what tripped

def rules_of(result: Any) -> tuple[list[str], bool]:
    """(the rules in notice mode that tripped, whether something else also asks the owner) from the results of the guards of a PASS."""
    rules: list[str] = []
    other = False
    for guard in getattr(result, "results", ()):
        if guard.code != "needs_owner":
            continue
        found = [f[len("notice:"):] for f in guard.flags if f.startswith("notice:")]            # a size control is its own rule: a grant never covers the others
        if found:
            rules += [r for r in found if r not in rules]
        else:
            other = True
    return rules, other


def _timeout_of(limits: Mapping[str, Any], rules: list[str]) -> int:
    modes = limits.get("rule_modes") if isinstance(limits.get("rule_modes"), Mapping) else {}
    values = []
    for rule in rules:
        entry = modes.get(rule)
        if isinstance(entry, Mapping) and entry.get("mode") == "notice":
            values.append(int(entry.get("timeout_min") or rule_modes.DEFAULT_TIMEOUT_MIN))
    return min(values) if values else rule_modes.DEFAULT_TIMEOUT_MIN


def _whys_of(result: Any) -> list[str]:
    return [g.why for g in getattr(result, "results", ()) if g.code == "needs_owner" and any(f.startswith("notice:") for f in g.flags) and g.why]


def attach(u: Any, proposal_id: int, *, desk_id: int, account_id: int, rules: list[str], result: Any) -> dict[str, Any]:
    """Write on the pending order what the buttons need, in the same transaction that created it. Returns the notice (with the nonce)."""
    timeout = _timeout_of(u.broker.get_limits(desk_id), rules)
    now = dt.datetime.now(dt.UTC)
    expires = (now + dt.timedelta(minutes=timeout)).strftime(FORMAT)
    nonce = new_nonce()
    notice = {"rules": rules, "whys": _whys_of(result)[:4], "timeout_min": timeout, "account_id": account_id, "desk_id": desk_id, "stage": "ask"}
    u.session.update("exec_proposals", {"id": proposal_id}, {"notice_json": json.dumps(notice, ensure_ascii=False), "notice_nonce": nonce, "expires_at": expires})
    return {**notice, "nonce": nonce, "expires_at": expires, "proposal_id": proposal_id}


def notice_of(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The notice of a pending order (a row of exec_proposals), or None when it does not come from a notice rule."""
    raw = row.get("notice_json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def deadline(row: Mapping[str, Any], created_at: str | None = None) -> dt.datetime | None:
    """When a pending order that carries its own wait expires, or None to use the 30 minutes of every other pending order."""
    return from_db_time(row["expires_at"]) if row.get("expires_at") else None


# --------------------------------------------------------------------------------------------------------------- the grants

def session_end(contract_ids: list[str], now: dt.datetime | None = None) -> dt.datetime:
    """When the session of the account ends, for a grant "for the rest of the day". The measure of live sessions is piece 3390c; until it lands the end is the
    next close of the market of the order: 17:00 in Chicago for futures, 16:00 in New York for everything else. A permission approved while the market is
    closed never reaches into the next session: it ends when that session opens (18:00 Chicago for futures, Sunday evening after the weekend; 09:30 New York
    for the rest, the weekend skipped; before the open of a weekday it ends at that same open), so the first order of a new session asks again. This is the only place to change."""
    from zoneinfo import ZoneInfo

    from hokma.contracts.identity import NoContract, parse_symbol

    futures = False
    for cid in contract_ids:
        try:
            futures = futures or str(parse_symbol(cid).asset_type) in ("future", "future_option")
        except NoContract:
            continue
    local = (now or dt.datetime.now(dt.UTC)).astimezone(ZoneInfo("America/Chicago" if futures else "America/New_York"))
    at = lambda days, hour, minute=0: (local + dt.timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)      # noqa: E731
    day, minutes = local.weekday(), local.hour * 60 + local.minute
    if futures:
        if day == 4 and minutes >= 17 * 60:                       # Friday close: the week opens again on Sunday at 18:00
            end = at(2, 18)
        elif day == 5:
            end = at(1, 18)
        elif day == 6 and minutes < 18 * 60:
            end = at(0, 18)
        elif day < 4 and 17 * 60 <= minutes < 18 * 60:            # the daily break
            end = at(0, 18)
        else:
            end = at(0, 17) if minutes < 17 * 60 and day < 5 else at(1, 17)
    elif day < 5 and minutes < 9 * 60 + 30:                      # before the open: the permission never reaches into the session that has not begun
        end = at(0, 9, 30)
    elif day < 5 and minutes < 16 * 60:
        end = at(0, 16)
    else:                                                         # after the close or on a weekend: until the next open
        step = 1
        while (local + dt.timedelta(days=step)).weekday() >= 5:
            step += 1
        end = at(step, 9, 30)
    return end.astimezone(dt.UTC)


def covered(u: Any, desk_id: int, account_id: int, rules: list[str]) -> bool:
    """True when EVERY rule that tripped has a grant of the rest of the day in force for this account and desk."""
    if not rules:
        return False
    now = utcnow()
    for rule in rules:
        if u.session.one("SELECT id FROM notice_grants WHERE account_id = :a AND desk_id = :d AND rule_id = :r AND ended_at IS NULL AND expires_at > :n",
                         {"a": account_id, "d": desk_id, "r": rule, "n": now}) is None:
            return False
    return True


def grant_day(u: Any, *, desk_id: int, account_id: int, rules: list[str], proposal_id: int, until: dt.datetime) -> list[int]:
    """Record "follow for the rest of the day": one row per rule, who and until when. It changes NO limit."""
    ids = []
    for rule in rules:
        ids.append(u.session.insert("notice_grants", {"account_id": account_id, "desk_id": desk_id, "rule_id": rule[:60], "proposal_id": proposal_id,
                                                      "granted_by": "owner", "granted_at": utcnow(), "expires_at": until.strftime(FORMAT)}))
    return ids


def grants_of(u: Any, account_id: int) -> list[dict[str, Any]]:
    return u.session.query("SELECT id, desk_id, rule_id, granted_at, expires_at, ended_at FROM notice_grants WHERE account_id = :a ORDER BY id DESC LIMIT 50",
                           {"a": account_id})


def apply_grants(result: Any, desk_id: int, account_id: int | None, db: Database | None = None) -> Any:
    """Called by the prefilter: an order whose ONLY reason to wait is notice rules that all have a grant of the day goes through as if they had not
    tripped. A rule that is a restriction never gets here (it blocks), and anything else that asks the owner still asks."""
    if account_id is None or not getattr(result, "needs_owner", False) or getattr(result, "verdict", "") != "PASS":
        return result
    rules, other = rules_of(result)
    if not rules or other:
        return result
    with (db or get_database()).read() as u:
        if not covered(u, desk_id, account_id, rules):
            return result
    import dataclasses

    return dataclasses.replace(result, needs_owner=False)


# --------------------------------------------------------------------------------------------------------------- the answers

@dataclass(frozen=True)
class Tap:
    """What a tap of a Telegram button did: the code of the result and, when Follow asked the scope, the new token to put on the next buttons."""
    code: str
    ask_scope: bool = False
    nonce: str | None = None
    proposal_id: int | None = None
    ask_close: bool = False                       # Close was tapped: the list of what would be closed is to be shown, and it is confirmed with the new token
    text: str = ""


def parse_callback(data: str) -> tuple[str, int, str] | None:
    match = _CALLBACK.fullmatch(data or "")
    return None if match is None else (match.group(1), int(match.group(2)), match.group(3))


def callback_data(choice: str, proposal_id: int, nonce: str) -> str:
    return f"on:{choice}:{proposal_id}:{nonce}"


def _need_owner(who: Any) -> None:
    from hokma.broker.service import BrokerError

    if who is None or not getattr(who, "owner", False):
        raise BrokerError("owner_required", 403)


def tap(data: str, *, who: Any, db: Database | None = None) -> Tap:
    """The owner tapped a button of the notice in Telegram. The token must be the one of this moment and it is spent by the answer."""
    from hokma.broker.service import BrokerError
    from hokma.exec import approvals

    _need_owner(who)
    parsed = parse_callback(data)
    if parsed is None:
        return Tap("invalid")
    choice, pid, nonce = parsed
    database = db or get_database()
    row = approvals.proposal_row(pid, db=database)
    if row is None or row["status"] != "awaiting_owner" or not row.get("notice_nonce") or not hmac.compare_digest(str(row["notice_nonce"]), nonce):
        return Tap("stale", proposal_id=pid)
    notice = notice_of(row) or {}
    stage = notice.get("stage", "ask")
    try:
        if choice == FOLLOW and stage == "ask":
            fresh = _ask_scope(database, row, notice, nonce)
            return Tap("scope", True, fresh, pid) if fresh else Tap("stale", proposal_id=pid)
        if choice in (ONLY_ORDER, REST_OF_DAY) and stage == "scope":
            out = approvals.approve(pid, who=who, db=database, scope="order" if choice == ONLY_ORDER else "day")
            return Tap(out.status, proposal_id=pid)
        if choice == NO:
            return Tap(approvals.reject(pid, who=who, db=database).status, proposal_id=pid)
        if choice == CLOSE and stage == "ask":                           # first step: nothing is closed, the list is shown and the token changes
            asked = approvals.close_ask(pid, who=who, db=database, nonce=nonce)
            return Tap("close_confirm", nonce=asked["nonce"], proposal_id=pid, ask_close=True, text=asked["text"])
        if choice == CONFIRM_CLOSE and stage == "close":                 # second step: exactly the list that was shown
            return Tap(approvals.close_account(pid, who=who, db=database), proposal_id=pid)
        return Tap("wrong_stage", proposal_id=pid)                    # a button of another step of the notice: nothing runs
    except BrokerError as exc:
        return Tap({"decision_expired": "expired", "decision_closed": "closed", "decision_not_found": "stale", "close_not_confirmed": "wrong_stage"}.get(exc.code, exc.code),
                   proposal_id=pid)


def _ask_scope(database: Database, row: Mapping[str, Any], notice: dict[str, Any], old: str) -> str | None:
    """Follow was tapped: the question of the scope is asked on a new token, so the tap of Follow cannot be used again. The change is conditional on the
    token of this tap, so two taps at once let one through. None when the token was already spent."""
    nonce = new_nonce()
    with database.write() as u:
        changed = u.session.execute("UPDATE exec_proposals SET notice_nonce = :n, notice_json = :j, updated_at = :t "
                                    "WHERE id = :i AND status = 'awaiting_owner' AND notice_nonce = :o",
                                    {"n": nonce, "j": json.dumps({**notice, "stage": "scope"}, ensure_ascii=False), "t": utcnow(), "i": row["id"], "o": old})
    return nonce if changed == 1 else None


# ----------------------------------------------------------------------------------------------------------- timer and chat

_timers: dict[int, threading.Timer] = {}
_timers_guard = threading.Lock()
_publisher: Callable[[dict[str, Any]], None] | None = None


def set_publisher(publisher: Callable[[dict[str, Any]], None] | None) -> None:
    """The app registers how an event reaches the open chats (set at start; None in tests)."""
    global _publisher
    _publisher = publisher


def publish(event: dict[str, Any]) -> None:
    publish_reached(event)


def publish_reached(event: dict[str, Any]) -> bool | None:
    """`publish` that says what happened: True when the chat's publisher took it, False when it failed, None when no publisher is set (this channel does not count)."""
    publisher = _publisher
    if publisher is None:
        return None
    try:
        publisher(event)
        return True
    except Exception:                                                  # a card that cannot be shown never blocks the order or its answer
        log.warning("the card of a notice could not be sent to the chat")
        return False


def arm(proposal_id: int, expires_at: str, db: Database | None = None) -> None:
    """Wake the sweep when the wait of this order ends. One timer per order of the main process; the sweep still runs whenever the list is read."""
    delay = max(0.0, (from_db_time(expires_at) - dt.datetime.now(dt.UTC)).total_seconds()) + 1.0

    def fire() -> None:
        with _timers_guard:
            _timers.pop(proposal_id, None)
        try:
            from hokma.exec import approvals

            approvals.expire_pending(db)
        except Exception:
            log.warning("the sweep of the pending orders failed at the end of a wait")

    timer = threading.Timer(delay, fire)
    timer.daemon = True
    with _timers_guard:
        old = _timers.pop(proposal_id, None)
        _timers[proposal_id] = timer
    if old is not None:
        old.cancel()
    timer.start()


def disarm(proposal_id: int) -> None:
    with _timers_guard:
        timer = _timers.pop(proposal_id, None)
    if timer is not None:
        timer.cancel()


def rearm_all(db: Database | None = None) -> int:
    """At start: the pending orders that carry a wait get their timer again (the process restarted). The ones already late expire now."""
    from hokma.exec import approvals

    database = db or get_database()
    approvals.expire_pending(database)
    with database.read() as u:
        rows = u.session.query("SELECT id, expires_at FROM exec_proposals WHERE status = 'awaiting_owner' AND notice_json IS NOT NULL AND expires_at IS NOT NULL")
    armed = 0
    for r in rows:
        try:                                              # a bad row must not leave the next ones without their timer
            arm(r["id"], r["expires_at"], database)
            armed += 1
        except Exception:                                 # noqa: BLE001  (a label: whatever fails with one row, the others still get theirs)
            log.exception("notice timer not re-armed for proposal %s", r["id"])
    from hokma.exec import cap_notices

    cap_notices.rearm_all(database)                                   # the notices of a cap that waited (no order in the way) get their timer too
    return armed


def card(row: Mapping[str, Any], *, desk_name: str = "", account_mask: str = "", ticket_text: str = "") -> dict[str, Any]:
    """The card of the chat: everything the owner needs to answer, with no token (the chat answers through the routes of the app)."""
    notice = notice_of(row) or {}
    return {"type": "order_notice", "proposal_id": row["id"], "desk_id": row["desk_id"], "desk": desk_name, "account": account_mask, "rules": notice.get("rules", []),
            "whys": notice.get("whys", []), "timeout_min": notice.get("timeout_min"), "expires_at": row.get("expires_at"), "stage": notice.get("stage", "ask"),
            "order": ticket_text}


def closed_event(proposal_id: int, outcome: str) -> dict[str, Any]:
    return {"type": "order_notice_closed", "proposal_id": proposal_id, "outcome": outcome}


# ------------------------------------------------------------------------------------------------------- the message and the sends

def message(notice: Mapping[str, Any], ticket_text: str, account_label: str) -> str:
    """The text the owner reads in Telegram, in his language: which rule, which account, what was measured, the wait and the order."""
    from hokma.i18n import t

    head = t("exec.notice.header", rules=", ".join(notice.get("rules", [])), account=account_label, minutes=str(notice.get("timeout_min")))
    whys = "\n".join(f"- {w}" for w in notice.get("whys", []))
    return "\n".join(part for part in (head, whys, ticket_text) if part)


def scope_message(notice: Mapping[str, Any]) -> str:
    from hokma.i18n import t

    return t("exec.notice.scope_ask", rules=", ".join(notice.get("rules", [])))


def announce(database: Database, proposal_id: int, notice: dict[str, Any], *, desk_name: str, account_label: str, ticket_text: str) -> None:
    """After the pending order is saved: Telegram (buttons, or the queued text when there is no link), the card of the chat and the timer of the wait."""
    from hokma.exec import telegram_approval

    telegram_approval.request_notice_buttons(proposal_id, notice["nonce"], message(notice, ticket_text, account_label))
    publish({"type": "order_notice", "proposal_id": proposal_id, "desk_id": notice["desk_id"], "desk": desk_name, "account": account_label, "rules": notice["rules"],
             "whys": notice["whys"], "timeout_min": notice["timeout_min"], "expires_at": notice["expires_at"], "stage": "ask", "order": ticket_text})
    arm(proposal_id, notice["expires_at"], database)


def pending_cards(db: Database | None = None) -> list[dict[str, Any]]:
    """The cards of every notice that waits now, oldest first: the chat asks for them when it opens, so a card sent while it was closed is not lost."""
    from hokma.exec import approvals

    database = db or get_database()
    approvals.expire_pending(database)
    out = []
    with database.read() as u:
        rows = u.session.query("SELECT * FROM exec_proposals WHERE status = 'awaiting_owner' AND notice_json IS NOT NULL ORDER BY id")
        for row in rows:
            desk = u.desks.find(row["desk_id"])
            account = None if row.get("account_id") is None else u.session.one("SELECT name FROM broker_accounts WHERE id = :a", {"a": row["account_id"]})
            try:
                order = json.loads(row["proposal_json"])
            except ValueError:
                order = {}
            out.append({**card(row, desk_name=desk.name if desk else "", account_mask=account["name"] if account else ""), "summary": _summary(order)})
    return out


def _summary(order: Mapping[str, Any]) -> str:
    legs = order.get("legs") or []
    return "; ".join(f"{str(l.get('side', '')).upper()} {l.get('qty')} {l.get('contract_id')}" for l in legs if isinstance(l, Mapping))[:300]

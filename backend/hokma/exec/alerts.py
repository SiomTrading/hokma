"""Alerts of a desk (SPEC-018: DTS-09). Four kinds of rule on one engine: a price crosses a level, a price moves a percentage, the desk falls
a percentage from its peak, and an order stays unfilled for some minutes. An alert rings ONCE per crossing and arms again only when the
condition has stopped being true. The state (`armed`, `last_fired_at`, the reference of the session) is in `exec_alerts`, so it survives a restart.
The session clock is the provider's (decision 1251): the reference price of a percentage rule is the mark at the start of the session that
`hokma.market.sessions` gives for the kind of instrument; a new session takes a new reference."""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from hokma.db import Database, get_database
from hokma.db.timeutil import utcnow
from hokma.exec.money import MoneyError, dec, text

RULE_TYPES = ("price_cross", "pct_change", "desk_drawdown", "unfilled_order")
MAX_RULES_PER_DESK = 50


class AlertError(ValueError):
    code = "bad_alert"


@dataclass(frozen=True)
class Fired:
    alert_id: int
    rule: dict[str, Any]
    message: str


def session_label(kind: str, now: datetime) -> str:
    """The label of the session that holds `now`, from the provider's session rule for this kind of instrument."""
    from hokma.market.sessions import session_for
    from hokma.market.tz import NEW_YORK

    return session_for(kind, NEW_YORK).label(int(now.timestamp())).isoformat()


def validate_rule(rule: Any) -> dict[str, Any]:
    if not isinstance(rule, Mapping) or rule.get("type") not in RULE_TYPES:
        raise AlertError(f"rule type must be one of {', '.join(RULE_TYPES)}")
    kind = rule["type"]
    out: dict[str, Any] = {"type": kind}
    try:
        if kind in ("price_cross", "pct_change"):
            cid = rule.get("contract_id")
            if not isinstance(cid, str) or ":" not in cid or len(cid) > 80:
                raise AlertError("the rule needs the canonical contract_id")
            out["contract_id"] = cid
        if kind == "price_cross":
            if rule.get("direction") not in ("above", "below"):
                raise AlertError("direction is above or below")
            out.update(direction=rule["direction"], level=text(dec(rule.get("level"), "level")))
        elif kind == "pct_change":
            pct = dec(rule.get("pct"), "pct")
            if not 0 < pct <= 1000:
                raise AlertError("pct is above 0 and up to 1000")
            out.update(pct=text(pct), kind=str(rule.get("kind") or "future") if str(rule.get("kind") or "future") in ("future", "forex", "crypto", "equity") else "future")
        elif kind == "desk_drawdown":
            pct = dec(rule.get("pct"), "pct")
            if not 0 < pct <= 100:
                raise AlertError("pct is above 0 and up to 100")
            out["pct"] = text(pct)
        else:
            minutes = rule.get("minutes")
            if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 10080:
                raise AlertError("minutes is a whole number from 1 to 10080")
            out["minutes"] = minutes
    except MoneyError as exc:
        raise AlertError(str(exc)) from exc
    return out


def add_alert(desk_id: int, rule: Any, *, db: Database | None = None) -> int:
    clean = validate_rule(rule)
    with (db or get_database()).write() as u:
        if int(u.session.scalar("SELECT COUNT(*) FROM exec_alerts WHERE desk_id = :d", {"d": desk_id}) or 0) >= MAX_RULES_PER_DESK:
            raise AlertError(f"a desk has at most {MAX_RULES_PER_DESK} alerts")
        return u.session.insert("exec_alerts", {"desk_id": desk_id, "rule_json": json.dumps(clean, sort_keys=True), "armed": 1, "created_at": utcnow()})


def list_alerts(desk_id: int, *, db: Database | None = None) -> list[dict[str, Any]]:
    with (db or get_database()).read() as u:
        rows = u.session.query("SELECT * FROM exec_alerts WHERE desk_id = :d ORDER BY id", {"d": desk_id})
    return [{"id": r["id"], "rule": json.loads(r["rule_json"]), "armed": bool(r["armed"]), "last_fired_at": r["last_fired_at"]} for r in rows]


def _condition(rule: dict[str, Any], *, marks: Mapping[str, Decimal], equity: Decimal | None, peak: Decimal | None,
               working: Iterable[Mapping[str, Any]], now: datetime) -> tuple[bool | None, str, dict[str, Any]]:
    """(is the condition true, the message, changes to the saved rule). None means the fact needed is not known: nothing rings, nothing re-arms."""
    kind = rule["type"]
    if kind == "price_cross":
        price = marks.get(rule["contract_id"])
        if price is None:
            return None, "", {}
        level = Decimal(rule["level"])
        hit = price >= level if rule["direction"] == "above" else price <= level
        return hit, f"{rule['contract_id']} is {rule['direction']} {rule['level']} (now {text(price)})", {}
    if kind == "pct_change":
        price = marks.get(rule["contract_id"])
        if price is None:
            return None, "", {}
        label = session_label(rule["kind"], now)
        changes: dict[str, Any] = {}
        if rule.get("session") != label or not rule.get("ref"):
            changes = {"session": label, "ref": text(price)}              # a new session starts from the mark of its first read
            return False, "", changes
        ref = Decimal(rule["ref"])
        move = (price - ref) / ref * 100 if ref else Decimal(0)
        return abs(move) >= Decimal(rule["pct"]), f"{rule['contract_id']} moved {move:.2f}% in the session", {}
    if kind == "desk_drawdown":
        if equity is None or peak is None or peak <= 0:
            return None, "", {}
        drop = (peak - equity) / peak * 100
        return drop >= Decimal(rule["pct"]), f"the desk is {drop:.2f}% under its peak", {}
    stale = [w for w in working if int(w.get("age_minutes", 0)) >= rule["minutes"]]
    return bool(stale), f"{len(stale)} order(s) unfilled for {rule['minutes']} minutes or more", {}


def evaluate(desk_id: int, *, now: datetime | None = None, marks: Mapping[str, Decimal] | None = None, equity: Decimal | None = None,
             peak: Decimal | None = None, working: Iterable[Mapping[str, Any]] = (), db: Database | None = None) -> list[Fired]:
    """Check every alert of a desk against the facts. Rings the ones whose condition became true while armed (once), and arms again the ones whose
    condition turned false."""
    now = now or datetime.now(timezone.utc)
    working = list(working)
    fired: list[Fired] = []
    with (db or get_database()).write() as u:
        for row in u.session.query("SELECT * FROM exec_alerts WHERE desk_id = :d ORDER BY id", {"d": desk_id}):
            rule = json.loads(row["rule_json"])
            state, message, changes = _condition(rule, marks=marks or {}, equity=equity, peak=peak, working=working, now=now)
            values: dict[str, Any] = {}
            if changes:
                values["rule_json"] = json.dumps({**rule, **changes}, sort_keys=True)
            if state is True and row["armed"]:
                values.update(armed=0, last_fired_at=utcnow())
                fired.append(Fired(row["id"], rule, message))
            elif state is False and not row["armed"]:
                values["armed"] = 1
            if values:
                u.session.update("exec_alerts", {"id": row["id"]}, values)
    return fired

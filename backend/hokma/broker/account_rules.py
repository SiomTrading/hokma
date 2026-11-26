"""Rules of an ACCOUNT, and of one desk inside an account (SPEC-026, piece 3390a, sections 3 and 3.3).

A rule of an ACCOUNT is measured over the whole account, adding what every desk that uses it does; when it is reached the account is
closed for the day and every one of those desks stops in it. A rule of a DESK IN AN ACCOUNT (desk_id set) is measured only over the part
of that desk and stops only that desk, in that account. A rule of a desk for all its accounts is not here: it lives in the config of the
desk (hokma.broker.desk_rules). THIS piece keeps the rules, their mode and their values; the live measure that applies them is piece 3390c.

The owner writes them (`set_rule` and `remove_rule` need him). An agent only proposes, through the proposals of the desk
(hokma.broker.desk_rules.propose with `account_id`), and approving one that loosens a rule asks for a confirmation on purpose."""
from __future__ import annotations

import json
from typing import Any

from hokma.broker import desk_rules, rule_modes, rules, service
from hokma.broker.service import BrokerError, Who
from hokma.db import Database
from hokma.db.timeutil import utcnow

SCOPES = ("desk", "account", "desk_account")          # all the accounts of the desk (its config) | the whole account | one desk in one account


def rule_id_of(account_id: int, control_id: str, desk_id: int | None) -> str:
    return f"a{account_id}:d{desk_id}:{control_id}" if desk_id else f"a{account_id}:{control_id}"


def _control(control_id: str) -> desk_rules.Control:
    control = desk_rules.BY_ID.get(control_id)
    if control is None or not control.picker or control.id == "events":
        raise desk_rules._bad("a rule of an account is one of: " + ", ".join(c.id for c in desk_rules.CONTROLS if c.picker and c.id != "events"))
    return control


def _linked(u, desk_id: int, account_id: int) -> None:
    if u.session.one("SELECT 1 AS x FROM desk_account_links WHERE desk_id = :d AND account_id = :a", {"d": desk_id, "a": account_id}) is None:
        raise BrokerError("account_not_linked", 409, text="the account is not one of the accounts of this desk")


def _row_view(row: dict[str, Any]) -> dict[str, Any]:
    control = desk_rules.BY_ID[row["control_id"]]
    value = json.loads(row["value"])
    out = {"id": rule_id_of(row["account_id"], row["control_id"], row["desk_id"]), "kind": "control", "control_id": row["control_id"],
           "scope": "desk_account" if row["desk_id"] else "account", "account_id": row["account_id"], "desk_id": row["desk_id"],
           "value": desk_rules._shown(control, value), "unit": control.unit, "mode": row["mode"], "source": row["source"], "on": True}
    if row["on_trip"]:
        out["on_trip"] = row["on_trip"]
    if row["timeout_min"]:
        out["timeout_min"] = row["timeout_min"]
    return out


def list_for_desk(u, desk_id: int) -> list[dict[str, Any]]:
    """The rules of account that reach this desk: the ones of each of its accounts that name no desk, and the ones that name this desk."""
    rows = u.session.query(
        "SELECT r.* FROM account_rules r JOIN desk_account_links l ON l.account_id = r.account_id AND l.desk_id = :d "
        "WHERE r.desk_id IS NULL OR r.desk_id = :d ORDER BY r.account_id, r.desk_id, r.control_id", {"d": desk_id})
    return [_row_view(r) for r in rows]


def _config_of(control: desk_rules.Control, stored: Any, entry: dict[str, Any] | None, rule_id: str) -> dict[str, Any]:
    """A scratch limits object with this one rule in it, so the loosening check of the desk applies to a rule of an account too."""
    cfg = rules.validate_limits(desk_rules._stored_changes({}, control, stored), {}) if stored is not None else {}
    if entry is not None:
        cfg["rule_modes"] = {rule_id: entry}
    return cfg


def apply(u, desk_id: int, account_id: int, control_id: str, value: Any, *, mode: Any, on_trip: Any = None, timeout_min: Any = None,
          only_desk: bool = False, source: str = "hand", confirm_loosen: bool | None = None) -> dict[str, Any]:
    """Write one rule of account inside the open unit. `confirm_loosen` None means a direct edit of the owner (never asked); a bool means a
    proposal that is being approved: a rule that gets weaker answers 409 `loosens_limit` until it is True."""
    control = _control(control_id)
    if source not in desk_rules.SOURCES:
        raise desk_rules._bad("bad source")
    _linked(u, desk_id, account_id)
    if only_desk and control.id in rule_modes.TRIP_CONTROLS:
        from hokma.exec import live

        live.require_exclusive(u, control.id, [account_id])          # SPEC-026 v2, decision 1299: a rule of ONE desk in a shared account is not measurable; the rule goes on the account
    stored = desk_rules._stored(control, value)
    try:
        rules.validate_limits(desk_rules._stored_changes({}, control, stored), {})
        entry = rule_modes.entry_for(control.id, mode, on_trip, timeout_min)
    except ValueError as exc:
        raise desk_rules._bad(str(exc)) from exc
    if entry["mode"] == "restriction" and control.id in rule_modes.TRIP_CONTROLS:
        from hokma.exec import live

        live.require_available(u, [account_id])          # A12: a restriction of loss needs the live freeze on that account
    scope_desk = desk_id if only_desk else None
    rid = rule_id_of(account_id, control.id, scope_desk)
    where = {"a": account_id, "d": scope_desk or 0, "c": control.id}
    old = u.session.one("SELECT * FROM account_rules WHERE account_id = :a AND COALESCE(desk_id, 0) = :d AND control_id = :c", where)
    if old is not None and confirm_loosen is False:
        before = _config_of(control, json.loads(old["value"]), {k: v for k, v in (("mode", old["mode"]), ("on_trip", old["on_trip"]),
                                                                                  ("timeout_min", old["timeout_min"])) if v}, rid)
        if rules.loosened_limits(before, _config_of(control, stored, entry, rid)):
            raise BrokerError("loosens_limit", 409, text="this proposal loosens a rule the account already has: confirm it to apply it")
    now = utcnow()
    values = {"v": json.dumps(stored), "m": entry["mode"], "t": entry.get("on_trip"), "o": entry.get("timeout_min"), "s": source, "n": now}
    if old is None:
        u.session.insert_row("account_rules", {"account_id": account_id, "desk_id": scope_desk, "control_id": control.id, "value": values["v"],
                                               "mode": values["m"], "on_trip": values["t"], "timeout_min": values["o"], "source": source,
                                               "created_at": now, "updated_at": now})
    else:
        u.session.execute("UPDATE account_rules SET value = :v, mode = :m, on_trip = :t, timeout_min = :o, source = :s, updated_at = :n WHERE id = :i",
                          {**values, "i": old["id"]})
    return {"id": rid}


def set_rule(desk_id: int, account_id: int, control_id: str, value: Any, who: Who, *, mode: Any, on_trip: Any = None, timeout_min: Any = None,
             only_desk: bool = False, source: str = "hand", db: Database | None = None) -> dict[str, Any]:
    """The owner adds or changes a rule of an account. Owner only; no extra confirmation to loosen it (it is his own edit)."""
    desk_rules._owner(who, "adding or changing a rule of an account")
    with service._db(db).write() as u:
        if u.desks.find(desk_id) is None:
            raise BrokerError("desk_not_found", 404)
        apply(u, desk_id, account_id, control_id, value, mode=mode, on_trip=on_trip, timeout_min=timeout_min, only_desk=only_desk, source=source)
    return desk_rules.view(desk_id, db)


def remove_rule(desk_id: int, rule_id: str, who: Who, db: Database | None = None) -> dict[str, Any]:
    desk_rules._owner(who, "removing a rule of an account")
    with service._db(db).write() as u:
        mine = {r["id"]: r for r in list_for_desk(u, desk_id)}
        found = mine.get(rule_id)
        if found is None:
            raise BrokerError("rule_not_found", 404, text="that rule does not exist")
        u.session.execute("DELETE FROM account_rules WHERE account_id = :a AND COALESCE(desk_id, 0) = :d AND control_id = :c",
                          {"a": found["account_id"], "d": found["desk_id"] or 0, "c": found["control_id"]})
    return desk_rules.view(desk_id, db)


def edit_rule(desk_id: int, rule_id: str, who: Who, *, value: Any = None, mode: Any = None, on_trip: Any = None, timeout_min: Any = None,
              db: Database | None = None) -> dict[str, Any]:
    """The owner changes the value or the mode of a rule of an account that exists. What he does not give stays as it is."""
    desk_rules._owner(who, "changing a rule of an account")
    with service._db(db).write() as u:
        found = next((r for r in list_for_desk(u, desk_id) if r["id"] == rule_id), None)
        if found is None:
            raise BrokerError("rule_not_found", 404, text="that rule does not exist")
        shown = found["value"] if value is None else value
        apply(u, desk_id, found["account_id"], found["control_id"], shown, mode=mode or found["mode"],
              on_trip=on_trip if on_trip is not None or mode else found.get("on_trip"),
              timeout_min=timeout_min if timeout_min is not None or mode else found.get("timeout_min"),
              only_desk=bool(found["desk_id"]), source=found["source"])
    return desk_rules.view(desk_id, db)

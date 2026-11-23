"""The three levels of autonomy of a desk (SPEC-018: AH-66, decision 1217).

1 only analyzes (the default): a proposal of the desk is recorded and nothing leaves; 2 proposes and asks the owner yes or no for
each order; 3 operates alone under rules the owner approved. Raising the level asks the owner (decision 1245); lowering it
is free for anyone. Level 3 needs its rules and the owner's approval in the same call. The limits of the committee are applied
by code at every level (`hokma.exec.prefilter`)."""
from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from hokma.db import Database, get_database
from hokma.db.timeutil import utcnow
from hokma.exec.money import MoneyError, dec
from hokma.exec.proposal import OrderProposal

LEVELS = (1, 2, 3)
RULE_KEYS = ("id", "symbols", "max_qty", "max_risk_usd", "order_types", "sides", "max_orders_per_day", "flatten")
MAX_RULES_JSON = 4000


class LevelError(ValueError):
    code = "bad_level"


def validate_rules(rules: Any) -> dict[str, Any]:
    """The rules of level 3: every condition must hold for an order to leave alone. A rule with no `max_risk_usd` or
    `max_qty` is refused: an order cannot be unlimited."""
    if not isinstance(rules, Mapping):
        raise LevelError("rules must be an object")
    extra = sorted(set(rules) - set(RULE_KEYS))
    if extra:
        raise LevelError(f"unknown rule key: {str(extra[0])[:40]}")
    out: dict[str, Any] = {}
    rid = rules.get("id")
    if not isinstance(rid, str) or not 1 <= len(rid) <= 40 or not rid.replace("-", "").replace("_", "").isalnum():
        raise LevelError("a rule needs an id of letters, digits, - and _")
    out["id"] = rid
    if "symbols" in rules:
        symbols = rules["symbols"]
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= 50 or not all(isinstance(s, str) and 1 <= len(s) <= 40 for s in symbols):
            raise LevelError("symbols is a list of 1 to 50 names")
        out["symbols"] = sorted({s.strip().upper() for s in symbols})
    for key in ("max_qty", "max_risk_usd"):
        if key in rules:
            try:
                value = dec(rules[key], key)
            except MoneyError as exc:
                raise LevelError(str(exc)) from exc
            if value <= 0:
                raise LevelError(f"{key} must be above zero")
            out[key] = str(value)
    if "max_qty" not in out and "max_risk_usd" not in out:
        raise LevelError("a rule needs max_qty or max_risk_usd")
    if "order_types" in rules:
        types = rules["order_types"]
        if not isinstance(types, list) or not types or not set(types) <= {"market", "limit", "stop", "stop_limit"}:
            raise LevelError("order_types is a list of market, limit, stop, stop_limit")
        out["order_types"] = sorted(set(types))
    if "sides" in rules:
        sides = rules["sides"]
        if not isinstance(sides, list) or not sides or not set(sides) <= {"buy", "sell"}:
            raise LevelError("sides is a list of buy, sell")
        out["sides"] = sorted(set(sides))
    if "max_orders_per_day" in rules:
        n = rules["max_orders_per_day"]
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 1000:
            raise LevelError("max_orders_per_day is a whole number from 1 to 1000")
        out["max_orders_per_day"] = n
    if "flatten" in rules:
        if not isinstance(rules["flatten"], bool):
            raise LevelError("flatten is true or false")
        out["flatten"] = rules["flatten"]
    if len(json.dumps(out)) > MAX_RULES_JSON:
        raise LevelError("the rules are too long")
    return out


def get_level(desk_id: int, *, db: Database | None = None) -> tuple[int, dict[str, Any] | None]:
    """(level, rules) of a desk. A desk with no row is at level 1."""
    with (db or get_database()).read() as u:
        row = u.session.one("SELECT level, rules_json FROM desk_autonomy WHERE desk_id = :d", {"d": desk_id})
    if row is None:
        return 1, None
    try:
        rules = json.loads(row["rules_json"]) if row.get("rules_json") else None
    except ValueError:
        rules = None
    return int(row["level"]), rules if isinstance(rules, dict) else None


def set_level(desk_id: int, level: int, *, rules: dict | None = None, who: Any, db: Database | None = None) -> dict[str, Any]:
    """Change the level. Raising it, and approving rules, needs the owner; lowering it never does."""
    from hokma.broker.service import BrokerError, owner_only

    if isinstance(level, bool) or level not in LEVELS:
        raise LevelError("level must be 1, 2 or 3")
    database = db or get_database()
    current, current_rules = get_level(desk_id, db=database)
    if level > current and not getattr(who, "owner", False):
        raise owner_only("raising the autonomy level of a desk")
    clean: dict[str, Any] | None = None
    if level == 3:
        if rules is None:
            raise LevelError("level 3 needs the rules the owner approves")
        if not getattr(who, "owner", False):
            raise owner_only("approving the rules of level 3")
        clean = validate_rules(rules)
    elif level == 2 and current == 3 and current_rules:
        clean = None                                   # leaving 3 drops the rules: coming back asks again
    with database.write() as u:
        if u.desks.find(desk_id) is None:
            raise BrokerError("desk_not_found", 404)
        values = {"level": level, "rules_json": None if clean is None else json.dumps(clean, sort_keys=True),
                  "approved_by": "owner" if level == 3 else None, "approved_at": utcnow() if level == 3 else None,
                  "updated_at": utcnow()}
        if u.session.one("SELECT 1 AS x FROM desk_autonomy WHERE desk_id = :d", {"d": desk_id}) is None:
            u.session.insert_row("desk_autonomy", {"desk_id": desk_id, **values})
        else:
            u.session.update("desk_autonomy", {"desk_id": desk_id}, values)
    return {"desk_id": desk_id, "level": level, "rules": clean, "previous": current}


def _covers(entry: str, contract_id: str) -> bool:
    """A rule names an instrument by its full canonical id, by `CLASS:ROOT` (FUT:NQ, OPT:SPY) or by a bare name, and a bare name covers only
    cash instruments (shares, ETFs, crypto, cfd): futures and options are covered only when the rule names them."""
    entry = entry.upper()
    if entry == contract_id.upper():
        return True
    parts = contract_id.upper().split(":")
    if len(parts) < 2:
        return False
    if ":" in entry:
        return entry == f"{parts[0]}:{parts[1]}"
    return parts[0] in ("EQ", "ETF", "CRY", "CFD") and parts[1] == entry


PROTECTIVE_STOP_KINDS = frozenset({"schwab"})   # brokers whose adapter places the stop with the order (a bracket or an OCO in one submission)
MIN_STOP_TICKS = Decimal(4)
MIN_STOP_FRACTION = Decimal("0.0002")            # of the mark


def _stop_counts(p: OrderProposal, leg: Leg, sheet: Any, entry: Decimal, protective: bool) -> bool:
    """A stop lowers the computed risk only when the order really carries a protective stop (the adapter of the broker places it with the order)
    and the stop is at a real distance: several ticks and a fraction of the mark. Otherwise the risk is the whole notional."""
    if not protective or p.stop_loss is None or len(p.legs) != 1:
        return False
    distance = (entry - p.stop_loss) if leg.side == "buy" else (p.stop_loss - entry)
    floor = max(MIN_STOP_TICKS * (sheet.tick_size or Decimal(0)), MIN_STOP_FRACTION * entry)
    return distance >= floor


def computed_risk(p: OrderProposal, sheets: Mapping[str, Any], marks: Mapping[str, Any], *, protective: bool = False) -> Decimal | None:
    """The loss of the opening legs if the stop is hit, from the legs, the stop, the multiplier of the sheet and the entry price (the greater
    of the given price and the market mark). With no stop the risk is the whole value. None when a price or a sheet is missing. The
    risk the agent declared is never read."""
    from hokma.exec.guards import priced_with_mark

    total = Decimal(0)
    for leg in p.legs:
        if leg.effect != "open":
            continue
        sheet, entry = sheets.get(leg.contract_id), priced_with_mark(p, leg, marks)
        if sheet is None or entry is None:
            return None
        if _stop_counts(p, leg, sheet, entry, protective):
            per = (entry - p.stop_loss) if leg.side == "buy" else (p.stop_loss - entry)
            total += leg.qty * sheet.multiplier * per
        else:
            total += leg.qty * entry * sheet.multiplier
    return total


def rules_allow(rules: Mapping[str, Any] | None, p: OrderProposal, *, orders_today: int = 0, sheets: Mapping[str, Any] | None = None,
                marks: Mapping[str, Any] | None = None, protective: bool = False) -> tuple[bool, str]:
    """Whether the approved rules cover this proposal. Returns (True, rule id) or (False, why). Nothing the agent declares (the effect, the
    risk, the symbol spelling) is believed: the symbols are matched by class, the risk is computed."""
    if not rules:
        return False, "no_rules"
    rid = str(rules.get("id") or "rule")
    if "symbols" in rules:
        for leg in p.legs:
            if not any(_covers(entry, leg.contract_id) for entry in rules["symbols"]):
                return False, f"symbol {leg.contract_id} is outside rule {rid}"
    if "order_types" in rules and p.order_type not in rules["order_types"]:
        return False, f"order type {p.order_type} is outside rule {rid}"
    if "sides" in rules and any(g.side not in rules["sides"] for g in p.legs):
        return False, f"side is outside rule {rid}"
    if "max_qty" in rules and any(g.qty > Decimal(rules["max_qty"]) for g in p.legs):
        return False, f"quantity is above max_qty of rule {rid}"
    if "max_risk_usd" in rules:
        risk = computed_risk(p, sheets or {}, marks or {}, protective=protective)
        if risk is None:
            return False, f"rule {rid} needs the risk of the order and it cannot be computed (no price or sheet)"
        if risk > Decimal(rules["max_risk_usd"]):
            return False, f"computed risk {risk:,.2f} is above max_risk_usd of rule {rid}"
    if "max_orders_per_day" in rules and orders_today >= rules["max_orders_per_day"]:
        return False, f"rule {rid} reached its orders for the day"
    return True, rid
